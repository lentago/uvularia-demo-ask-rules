#!/usr/bin/env python3
"""Bring the template's later changes into this repository, ready for review.

What it does: fetches the template this repository was made from (named in
.github/template-update.toml), works out what the template changed since this
repository last took an update, and makes the same changes here. They land in
the working tree, staged, and nothing else happens: no commit, no push. The
template-update workflow commits them and opens a pull request with your token;
you review it and merge it like any other.

Why bother: a repository made with "Use this template" is a copy. Without this,
it never hears about a fixed check, a clearer guide, or a new workflow.

How long: a few seconds.

How you know it worked: `git diff --cached --stat` lists the changes, and the
summary it writes (--body) says what happened to each file and why.

For each file, three versions are compared: the template as it was at your last
update (the base, kept in .github/template-base), the template now, and yours.

  you had not changed it since the base     -> it becomes the template's version
  the template has not changed it           -> yours stays as it is
  you both changed it                       -> merged line by line; where the
                                               same lines changed on both sides,
                                               both versions are left in the file
                                               between <<<<<<< and >>>>>>> for you
                                               to choose, and the summary says so
  the template removed it, you had not       -> removed
  the template removed it, you had changed it -> yours stays, listed
  you removed it, the template changed it   -> stays removed, listed

A path under `keep` in the config is never added, changed or removed, and
neither is the config itself or the base file.

    python3 scripts/template-update.py                # update this checkout
    python3 scripts/template-update.py --dry-run      # only say what would change
    python3 scripts/template-update.py --body /tmp/body.md --issue-body /tmp/issue.md

Standard library and git only. Python 3.11 or newer.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG = ".github/template-update.toml"
BASE_FILE = ".github/template-base"
ALWAYS_KEEP = (CONFIG, BASE_FILE)
NEW_REF = "refs/template-update/new"


class UpdateError(Exception):
    """A problem the person running this needs to fix; printed without a traceback."""


def git(*args: str, root: Path) -> str:
    return git_bytes(*args, root=root).decode().strip()


def git_bytes(*args: str, root: Path) -> bytes:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True)
    if r.returncode != 0:
        raise UpdateError(f"git {' '.join(args)} failed: {r.stderr.decode().strip()}")
    return r.stdout


# --- configuration -----------------------------------------------------------

@dataclass
class Config:
    source: str            # owner/name of the template repository
    branch: str            # the template branch to follow
    keep: list[str]        # paths that are yours alone

    def kept(self, path: str) -> bool:
        for k in (*self.keep, *ALWAYS_KEEP):
            if path == k or (k.endswith("/") and path.startswith(k)):
                return True
        return False


def load_config(root: Path) -> Config:
    p = root / CONFIG
    if not p.is_file():
        raise UpdateError(f"{CONFIG} not found. Copy it from the template you used, "
                          "then run this again.")
    with p.open("rb") as f:
        data = tomllib.load(f)
    source = data.get("source")
    if not isinstance(source, str) or source.count("/") != 1:
        raise UpdateError(f'{CONFIG}: `source` must be "owner/name" of the template repository')
    keep = data.get("keep", [])
    if not isinstance(keep, list) or not all(isinstance(k, str) and k for k in keep):
        raise UpdateError(f"{CONFIG}: `keep` must be a list of paths")
    return Config(source=source, branch=str(data.get("branch", "main")), keep=keep)


def read_base(root: Path) -> str | None:
    p = root / BASE_FILE
    if not p.is_file():
        return None
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def base_text(source: str, sha: str) -> str:
    return (f"# The commit of {source} this repository was last updated to.\n"
            "# Written by scripts/template-update.py. Change it only if you bring a\n"
            "# template change in by hand.\n"
            f"{sha}\n")


# --- trees -------------------------------------------------------------------

Entry = tuple[str, str]  # (mode, blob sha)


def tree(root: Path, commit: str) -> dict[str, Entry]:
    out: dict[str, Entry] = {}
    raw = git_bytes("ls-tree", "-r", "-z", "--full-tree", commit, root=root)
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, kind, sha = meta.decode().split(" ")
        if kind == "blob":
            out[path.decode()] = (mode, sha)
    return out


def infer_base(root: Path, new: str) -> str | None:
    """The template commit this repository was created from: "Use this template"
    makes a first commit whose tree is exactly the template's tree at that time."""
    if git("rev-parse", "--is-shallow-repository", root=root) == "true":
        return None
    roots = git("rev-list", "--max-parents=0", "HEAD", root=root).split()
    trees = {git("rev-parse", f"{r}^{{tree}}", root=root) for r in roots}
    for line in git("log", "--format=%H %T", new, root=root).splitlines():
        sha, t = line.split(" ")
        if t in trees:
            return sha
    return None


def have_commit(root: Path, sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=root,
                          capture_output=True).returncode == 0


# --- the plan ----------------------------------------------------------------

@dataclass
class Plan:
    source: str
    new: str
    base: str | None
    base_how: str                                   # "recorded", "inferred", or why unknown
    take: list[tuple[str, Entry, str]] = field(default_factory=list)   # path, entry, added|updated
    remove: list[str] = field(default_factory=list)
    merged: list[tuple[str, bytes, str]] = field(default_factory=list)  # path, content, mode
    conflicts: list[tuple[str, bytes, str]] = field(default_factory=list)
    replaced_no_base: list[tuple[str, Entry]] = field(default_factory=list)
    yours_template_removed: list[str] = field(default_factory=list)
    removed_by_you: list[str] = field(default_factory=list)
    binary_both: list[str] = field(default_factory=list)
    kept_changed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.take or self.remove or self.merged or self.conflicts
                     or self.replaced_no_base)


def blob(root: Path, sha: str) -> bytes:
    return git_bytes("cat-file", "blob", sha, root=root)


def merge3(root: Path, yours: bytes, base: bytes, theirs: bytes) -> tuple[bytes, int]:
    """git merge-file: (merged content, number of conflicting hunks)."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, data in (("yours", yours), ("base", base), ("theirs", theirs)):
            p = Path(tmp) / name
            p.write_bytes(data)
            paths.append(str(p))
        r = subprocess.run(["git", "merge-file", "-p",
                            "-L", "yours", "-L", "template at your last update",
                            "-L", "template now", *paths], cwd=root, capture_output=True)
    if not 0 <= r.returncode <= 127:
        raise UpdateError(f"git merge-file failed: {r.stderr.decode().strip()}")
    return r.stdout, r.returncode


def plan(root: Path, cfg: Config, new: str, base: str | None, base_how: str) -> Plan:
    pl = Plan(source=cfg.source, new=new, base=base, base_how=base_how)
    new_t = tree(root, new)
    base_t = tree(root, base) if base else {}
    head_t = tree(root, "HEAD")

    for path in sorted(set(base_t) | set(new_t)):
        b, n, h = base_t.get(path), new_t.get(path), head_t.get(path)
        if cfg.kept(path):
            # Report what the template changed under keep. Its config arriving for the
            # first time is not news: you have your own copy already.
            if base and b != n and not (path == CONFIG and b is None):
                pl.kept_changed.append(path)
            continue
        if n == h:
            continue                                   # already the same
        if base is None:
            if n is not None:                          # no base: the template's wins, listed
                pl.replaced_no_base.append((path, n))
            continue                                   # never remove without a base
        if n == b:
            continue                                   # the template did not change it
        if h == b:                                     # you did not change it
            if n is None:
                pl.remove.append(path)
            else:
                pl.take.append((path, n, "added" if h is None else "updated"))
            continue
        # Both sides changed it.
        if n is None:
            pl.yours_template_removed.append(path)
            continue
        if h is None:
            pl.removed_by_you.append(path)
            continue
        yours, theirs = blob(root, h[1]), blob(root, n[1])
        old = blob(root, b[1]) if b else b""
        if b"\0" in yours or b"\0" in theirs or b"\0" in old:
            pl.binary_both.append(path)
            continue
        content, n_conflicts = merge3(root, yours, old, theirs)
        mode = n[0] if (b is None or b[0] != n[0]) else h[0]
        if not n_conflicts and content == yours and mode == h[0]:
            continue                                   # you already have the template's change
        (pl.conflicts if n_conflicts else pl.merged).append((path, content, mode))
    return pl


# --- applying it -------------------------------------------------------------

def apply(root: Path, pl: Plan) -> None:
    for path, (mode, sha), _how in pl.take:
        git("update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}", root=root)
        git("checkout-index", "-f", "-q", "--", path, root=root)
    for path, (mode, sha) in pl.replaced_no_base:
        git("update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}", root=root)
        git("checkout-index", "-f", "-q", "--", path, root=root)
    for path in pl.remove:
        git("rm", "-q", "-f", "--", path, root=root)
    for path, content, mode in (*pl.merged, *pl.conflicts):
        (root / path).write_bytes(content)
        git("add", "--", path, root=root)
        git("update-index", "--chmod=" + ("+x" if mode == "100755" else "-x"), "--", path,
            root=root)
    (root / BASE_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / BASE_FILE).write_text(base_text(pl.source, pl.new))
    git("add", "--", BASE_FILE, root=root)


# --- what it says ------------------------------------------------------------

def summary(pl: Plan, server: str = "https://github.com") -> str:
    repo = f"{server}/{pl.source}"
    lines = [f"Brings this repository up to date with [`{pl.source}`]({repo}) at "
             f"[`{pl.new[:7]}`]({repo}/commit/{pl.new})."]
    if pl.base:
        lines.append(f"What the template changed since this repository's last update: "
                     f"[{pl.base[:7]}…{pl.new[:7]}]({repo}/compare/{pl.base}...{pl.new}).")
    lines += ["", "Opened by this repository's **template-update** workflow. Your checks run "
              "on it like on any pull request, and nothing changes until you merge it. "
              "If a change should not come in, push a commit to this branch that undoes "
              "it, or add the path to `keep` in `.github/template-update.toml` and close "
              "this pull request."]

    def section(title: str, why: str, items: list[str]) -> None:
        if items:
            lines.extend(["", f"### {title} ({len(items)})", "", why, ""])
            lines.extend(f"- `{p}`" for p in items)

    section("Needs you: changed on both sides",
            "You changed these and so did the template, on the same lines. Each file holds "
            "both versions between `<<<<<<< yours` and `>>>>>>> template now`. Keep what "
            "you want, delete the markers, and push to this branch.",
            [p for p, _c, _m in pl.conflicts])
    section("Needs you: replaced without a base to compare against",
            f"There was no record of the template version this repository last took "
            f"({pl.base_how}), so these now match the template. Check the diff for any "
            "change of yours that this undid, and put it back on this branch. Nothing was "
            "removed.",
            [p for p, _e in pl.replaced_no_base])
    section("Taken from the template",
            "You had not changed these since your last update.",
            [f"{p} ({how})" for p, _e, how in pl.take] + [f"{p} (removed)" for p in pl.remove])
    section("Merged with your changes",
            "You changed these and so did the template, on different lines; both sets "
            "of changes are in.",
            [p for p, _c, _m in pl.merged])
    section("Left as you have them",
            "The template removed these, but you had changed them. Remove them yourself "
            "if you no longer need them.",
            pl.yours_template_removed)
    section("Left removed",
            "You removed these and the template has since changed them.",
            pl.removed_by_you)
    section("Left as you have them: files that are not text",
            "You and the template both changed these, and they cannot be merged line by "
            "line. Compare yours with the template's and pick one.",
            pl.binary_both)
    section("Not touched: on your keep list",
            "The template changed these, but `.github/template-update.toml` lists them "
            "as yours.",
            pl.kept_changed)
    return "\n".join(lines) + "\n"


def issue_summary(pl: Plan) -> str:
    return (
        "A template update is waiting, and this repository's **template-update** workflow "
        "could not open it as a pull request: the secret `TEMPLATE_UPDATE_TOKEN` is not "
        "set. The built-in workflow token cannot change files under `.github/workflows/`, "
        "and pull requests it opens do not run your checks, so the update needs a token "
        "of yours.\n\n"
        "**To receive updates as pull requests** (about three minutes, once): create a "
        "fine-grained personal access token for this repository only, with *Contents*, "
        "*Pull requests* and *Workflows* set to read and write; save it in **Settings → "
        "Secrets and variables → Actions** as `TEMPLATE_UPDATE_TOKEN`; then run "
        "**template-update** from the Actions tab. This issue is updated each week while "
        "an update waits; close it once the pull request is open.\n\n"
        "---\n\n**The pull request it will open, once the token is set:**\n\n" + summary(pl))


# --- main --------------------------------------------------------------------

def output(**kv: str) -> None:
    for k, v in kv.items():
        print(f"{k}={v}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def run(root: Path, template_url: str | None, dry_run: bool, body: Path | None,
        issue_body: Path | None) -> Plan | None:
    cfg = load_config(root)
    here = os.environ.get("GITHUB_REPOSITORY", "")
    if here.lower() == cfg.source.lower():
        print(f"template-update: this is the template itself ({cfg.source}); nothing to do")
        output(changed="false", attention="0")
        return None
    if git("status", "--porcelain", "--untracked-files=no", root=root):
        raise UpdateError("the working tree has uncommitted changes; commit or stash them first")

    url = template_url or f"https://github.com/{cfg.source}.git"
    git("fetch", "--quiet", "--no-tags", url, f"+refs/heads/{cfg.branch}:{NEW_REF}", root=root)
    new = git("rev-parse", NEW_REF, root=root)

    base, how = read_base(root), "recorded"
    if base and not have_commit(root, base):
        subprocess.run(["git", "fetch", "--quiet", "--no-tags", url, base], cwd=root,
                       capture_output=True)
        if not have_commit(root, base):
            base, how = None, f"the recorded base {base[:7]} is not in the template's history"
    elif base is None:
        base = infer_base(root, new)
        how = "inferred" if base else ("this repository's first commit does not match "
                                       "any commit of the template")

    pl = plan(root, cfg, new, base, how)
    print(f"template-update: {cfg.source}@{new[:7]}, base "
          f"{base[:7] + ' (' + how + ')' if base else 'unknown: ' + how}")
    if pl.changed and not dry_run:
        apply(root, pl)
    text = summary(pl)
    print(text if pl.changed else "template-update: already up to date with the template")
    if body:
        body.write_text(text)
    if issue_body:
        issue_body.write_text(issue_summary(pl))
    # Files that need a person: conflict markers, or replaced with no base to say
    # whether they held a change of yours. Either one opens the pull request as a draft.
    output(changed=str(pl.changed).lower(),
           attention=str(len(pl.conflicts) + len(pl.replaced_no_base)),
           new=new, short=new[:7], source=cfg.source)
    return pl


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", default=".", help="this repository's checkout (default: .)")
    p.add_argument("--template-url", help="fetch the template from here instead of GitHub "
                   "(the tests use a local path)")
    p.add_argument("--dry-run", action="store_true", help="say what would change; change nothing")
    p.add_argument("--body", type=Path, help="write the pull-request description here")
    p.add_argument("--issue-body", type=Path, help="write the no-token issue text here")
    a = p.parse_args(argv)
    try:
        root = Path(git("rev-parse", "--show-toplevel", root=Path(a.root)))
        run(root, a.template_url, a.dry_run, a.body, a.issue_body)
    except UpdateError as e:
        print(f"template-update: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
