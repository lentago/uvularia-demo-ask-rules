#!/usr/bin/env python3
"""Run the golden question set against your records, as a gate on every change.

Two modes, and the default needs no API key and costs nothing:

  * ``--mode dry`` (the default). Reads the **currently published** corpus and
    checks every golden entry against it *without calling a model*:

      - each required ``source_ids`` id actually exists in the published corpus,
        so a question that claims to be answerable really has the records behind
        it (and a typo'd id is caught);
      - the expected outcome is consistent with those ids — an ``answered`` entry
        must cite at least one real record; a ``declined`` or ``escalated`` entry
        must cite none, because by definition the corpus does not answer it;
      - each ``subjects`` entry maps to a known record — it is a tag the corpus
        carries, or it appears in a record's id or title — so the subject is one
        the records actually cover and not a stray topic.

    This is the structural gate. It runs on every pull request, finds the errors
    a reviewer would otherwise miss, and must pass completely.

  * ``--mode live`` (gated on ``MITCHELLA_API_KEY``). Loads the same published
    corpus into mitchella's engine and asks each golden question for real, then
    scores the engine's outcome and citations against what the golden set
    expected. Fails below ``policy.yaml``'s ``eval.threshold``. This is the only
    mode that spends tokens, and it only runs where a key is set.

Usage::

    python3 evals/run.py                         # dry, reads policy.yaml for the URL
    python3 evals/run.py --bundle ./corpus-latest.json
    python3 evals/run.py --mode live --report report.md

The published corpus can be an ``https://`` URL, a ``file://`` URL, a local file,
or a directory holding ``corpus-latest.json``. With no ``--bundle``, the base URL
comes from ``policy.yaml``'s ``published_base``.

Dry mode is Python 3.12 standard library only — no pip install, ever. Live mode
imports mitchella and the Anthropic SDK lazily, so they are needed only when a
key is present.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE_ROOT = HERE.parent                      # templates/ask-rules
POLICY = TEMPLATE_ROOT / "policy.yaml"
DEFAULT_GOLDEN = HERE / "golden.jsonl"

OUTCOME_KINDS = ("answered", "incident", "escalated", "declined")


# --------------------------------------------------------------------------- #
# Reading the golden set and the published corpus.                             #
# --------------------------------------------------------------------------- #

def load_golden(path):
    """Parse ``golden.jsonl`` — one JSON object per non-blank line.

    Each entry: ``id``, ``question``, ``expect`` (an outcome kind), optional
    ``source_ids`` (required citations), optional ``subjects`` (topics), optional
    ``note`` (why, for a human). Lines beginning with ``#`` are ignored so the
    file can carry a header.
    """
    entries = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"error: {path}:{lineno}: not valid JSON: {exc}")
        if not isinstance(obj, dict):
            raise SystemExit(f"error: {path}:{lineno}: each line must be a JSON object")
        obj.setdefault("source_ids", [])
        obj.setdefault("subjects", [])
        entries.append(obj)
    if not entries:
        raise SystemExit(f"error: {path}: no golden entries found")
    return entries


def _read_text(src):
    """Read text from an http(s) URL, a file:// URL, or a local path."""
    if src.startswith(("http://", "https://")):
        with urllib.request.urlopen(src, timeout=20) as resp:  # noqa: S310 (fixed scheme above)
            return resp.read().decode("utf-8")
    if src.startswith("file://"):
        return Path(src[len("file://"):]).read_text(encoding="utf-8")
    return Path(src).read_text(encoding="utf-8")


def resolve_bundle_location(bundle, policy_path=POLICY):
    """Where to read ``corpus-latest.json`` from.

    An explicit ``--bundle`` wins; it may name the file directly or a directory
    that holds it. With none, fall back to ``policy.yaml``'s ``published_base``.
    """
    if bundle:
        p = Path(bundle)
        if p.is_dir():
            return str(p / "corpus-latest.json")
        return bundle
    base = _scan_scalar(Path(policy_path).read_text(encoding="utf-8"), "published_base")
    if not base:
        raise SystemExit("error: no --bundle given and policy.yaml has no published_base")
    return base.rstrip("/") + "/corpus-latest.json"


def load_bundle(location):
    """Load and lightly sanity-check a published corpus bundle."""
    try:
        data = json.loads(_read_text(location))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        # Exit 3 is "no published corpus yet", distinct from a failing eval: a fresh
        # repo made from the template has nothing to check against until its vault
        # publishes, and the workflow treats 3 as a notice rather than a failure.
        print(f"notice: no published corpus could be read at {location} ({exc}). "
              "Set published_base in policy.yaml to your records vault's published URL; "
              "the golden set is checked once the vault has published.", file=sys.stderr)
        raise SystemExit(3)
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise SystemExit(f"error: {location} is not a corpus bundle (no 'records' array)")
    return data


def corpus_index(bundle):
    """Return ``(doc_ids, subject_haystacks)`` for the dry checks.

    ``doc_ids`` is the set of published record ids. ``subject_haystacks`` is, per
    record, its lowercased tags plus its id and title — the surface a golden
    subject must hit to count as "maps to a known record".
    """
    doc_ids = set()
    haystacks = []
    for rec in bundle.get("records", []):
        doc_id = rec.get("doc_id", "")
        doc_ids.add(doc_id)
        tags = {str(t).lower() for t in rec.get("tags", [])}
        text = f"{doc_id} {rec.get('title', '')}".lower()
        haystacks.append((tags, text))
    return doc_ids, haystacks


def subject_maps(subject, haystacks):
    """A subject maps if it equals a tag, or appears in some record's id/title.

    Substring matching mirrors how the engine itself matches subjects against a
    question (mitchella's signal plane): crude, auditable, and predictable. A
    missed match is a subject the records do not clearly cover — worth catching.
    """
    s = subject.strip().lower()
    if not s:
        return False
    return any(s in tags or s in text for tags, text in haystacks)


# --------------------------------------------------------------------------- #
# Dry mode: structural checks, no model.                                       #
# --------------------------------------------------------------------------- #

def check_dry(entries, bundle):
    """Return a list of human-readable errors; empty means the golden set passes."""
    doc_ids, haystacks = corpus_index(bundle)
    errors = []
    seen_ids = set()

    for i, e in enumerate(entries):
        where = e.get("id") or f"entry #{i + 1}"

        if e.get("id"):
            if e["id"] in seen_ids:
                errors.append(f"{where}: duplicate golden id")
            seen_ids.add(e["id"])
        if not e.get("question"):
            errors.append(f"{where}: missing 'question'")

        expect = e.get("expect")
        if expect not in OUTCOME_KINDS:
            errors.append(f"{where}: 'expect' must be one of {OUTCOME_KINDS}, got {expect!r}")

        source_ids = e.get("source_ids", [])
        for sid in source_ids:
            if sid not in doc_ids:
                errors.append(f"{where}: required source id '{sid}' is not in the published corpus")

        # The expected outcome has to be consistent with the citations. This is
        # what catches a "deliberately wrong expected outcome": an answered entry
        # that cites nothing real, or a declined/escalated entry that cites a
        # record the corpus plainly does answer from.
        if expect == "answered" and not source_ids:
            errors.append(f"{where}: expected 'answered' but lists no source_ids to cite")
        if expect in ("declined", "escalated") and source_ids:
            errors.append(
                f"{where}: expected '{expect}' but lists source_ids "
                f"{source_ids} — a corpus that can cite those can answer it")

        for subject in e.get("subjects", []):
            if not subject_maps(subject, haystacks):
                errors.append(f"{where}: subject '{subject}' maps to no published record")

    return errors


# --------------------------------------------------------------------------- #
# Live mode: ask the engine for real.                                          #
# --------------------------------------------------------------------------- #

def _corpus_from_bundle(bundle):
    """Build a mitchella Corpus from a published bundle, skipping archived records.

    The bundle is already in the engine's entry shape, so this reconstructs the
    exact corpus (and its byte-stable fingerprint) the Ask function would serve —
    without re-reading the vault.
    """
    import hashlib
    from mitchella import corpus as mcorpus
    from mitchella.entry import Entry

    entries = tuple(sorted(
        (
            Entry(
                doc_id=r["doc_id"], title=r["title"], tags=tuple(r.get("tags", [])),
                volatility=r.get("volatility", "stable"), body=r.get("body", ""),
                path=r.get("path", r["doc_id"]), certainty=r.get("certainty", "unknown"),
            )
            for r in bundle.get("records", []) if not r.get("archived")
        ),
        key=lambda e: e.doc_id,
    ))
    if not entries:
        raise SystemExit("error: the published corpus has no non-archived records to ask against")
    rendered = mcorpus._render(entries)
    fingerprint = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]
    return mcorpus.Corpus(entries=entries, rendered=rendered, fingerprint=fingerprint, source="bundle")


def _engine(bundle, instructions_path, incidents_path, model):
    """Wire up mitchella's engine the way the Ask function will.

    instructions.md is installed as the engine's frozen system[0] so the live
    run exercises the very text a release pins — not mitchella's built-in desk
    instructions. incidents.toml feeds the manual-override signal provider.
    """
    from mitchella import engine as mengine
    from mitchella.config import Config
    from mitchella.signals import ManualOverrideProvider, SignalPlane

    mengine.INSTRUCTIONS = Path(instructions_path).read_text(encoding="utf-8").split("-->", 1)[-1].strip()
    corpus = _corpus_from_bundle(bundle)
    plane = SignalPlane([ManualOverrideProvider(incidents_path)])
    return mengine.Engine(corpus=corpus, signal_plane=plane, config=Config(model=model))


def check_live(entries, bundle, *, instructions_path, incidents_path, model):
    """Ask each question and score the result. Returns ``(rows, pass_rate)``."""
    from mitchella.contract import Query

    engine = _engine(bundle, instructions_path, incidents_path, model)
    rows = []
    passed = 0
    for e in entries:
        answer = engine.answer(Query(text=e["question"], surface="eval"))
        got_kind = answer.kind.value
        cited = {s.doc_id for s in answer.sources}
        required = set(e.get("source_ids", []))
        kind_ok = got_kind == e.get("expect")
        cites_ok = required.issubset(cited)
        ok = kind_ok and cites_ok
        passed += ok
        rows.append({
            "id": e.get("id") or e["question"][:40], "expect": e.get("expect"),
            "got": got_kind, "required": sorted(required), "cited": sorted(cited),
            "ok": ok,
        })
    return rows, (passed / len(entries) if entries else 0.0)


# --------------------------------------------------------------------------- #
# Reporting.                                                                    #
# --------------------------------------------------------------------------- #

def dry_report(entries, errors):
    lines = ["<!-- uvularia-evals -->", "## Ask-box evals — dry run", ""]
    if errors:
        lines.append(f"❌ **{len(errors)} problem(s)** in the golden set. Nothing merges until these are fixed.")
        lines += ["", *(f"- {msg}" for msg in errors)]
    else:
        lines.append(f"✅ All **{len(entries)}** golden questions line up with the published corpus.")
    lines += ["", "_Dry mode is structural: it checks ids, outcomes, and subjects "
              "against the currently published corpus without calling a model._"]
    return "\n".join(lines) + "\n"


def live_report(rows, pass_rate, threshold, warn_below):
    pct = round(pass_rate * 100)
    lines = ["<!-- uvularia-evals-live -->", "## Ask-box evals — live run", ""]
    verdict = "✅ pass" if pass_rate >= threshold else "❌ fail"
    flag = " ⚠️ below the comfort line" if threshold <= pass_rate < warn_below else ""
    lines.append(f"{verdict} — **{pct}%** of golden questions matched "
                 f"(threshold {round(threshold * 100)}%){flag}.")
    lines += ["", "| | Question | Expected | Got | Required cites | Cited |",
              "|---|---|---|---|---|---|"]
    for r in rows:
        mark = "✅" if r["ok"] else "❌"
        lines.append("| {m} | `{id}` | {exp} | {got} | {req} | {cit} |".format(
            m=mark, id=r["id"], exp=r["expect"], got=r["got"],
            req=", ".join(r["required"]) or "—", cit=", ".join(r["cited"]) or "—"))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# A one-key YAML scalar reader (dry mode stays dependency-free).               #
# --------------------------------------------------------------------------- #

def _scan_scalar(text, key):
    """First ``key: value`` scalar in a small YAML file, at any indent.

    Deliberately not a YAML parser: dry mode reads only the two scalars it needs
    (``published_base``, ``eval.threshold``) and must not drag in a dependency.
    """
    pat = re.compile(rf"^\s*{re.escape(key)}:\s*(.+?)\s*$", re.MULTILINE)
    m = pat.search(text)
    if not m:
        return None
    value = m.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _policy_threshold(policy_path, key, default):
    raw = _scan_scalar(Path(policy_path).read_text(encoding="utf-8"), key)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# CLI.                                                                          #
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the golden set against the published records.")
    parser.add_argument("--mode", choices=("dry", "live"), default="dry",
                        help="dry (structural, no key) or live (asks the engine)")
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN), help="the golden.jsonl file")
    parser.add_argument("--bundle", help="corpus-latest.json: URL, file, or directory (default: policy.yaml)")
    parser.add_argument("--policy", default=str(POLICY), help="policy.yaml (for published_base and thresholds)")
    parser.add_argument("--instructions", default=str(TEMPLATE_ROOT / "instructions.md"))
    parser.add_argument("--incidents", default=str(TEMPLATE_ROOT / "signals" / "incidents.toml"))
    parser.add_argument("--report", help="write the markdown report here as well as stdout")
    parser.add_argument("--threshold", type=float, help="override policy.yaml's live pass-rate threshold")
    args = parser.parse_args(argv)

    entries = load_golden(args.golden)
    location = resolve_bundle_location(args.bundle, args.policy)
    bundle = load_bundle(location)

    if args.mode == "dry":
        errors = check_dry(entries, bundle)
        report = dry_report(entries, errors)
        _emit(report, args.report)
        return 1 if errors else 0

    # live
    model = _scan_scalar(Path(args.policy).read_text(encoding="utf-8"), "model") or "claude-sonnet-5-5"
    threshold = args.threshold if args.threshold is not None else _policy_threshold(args.policy, "threshold", 0.8)
    warn_below = _policy_threshold(args.policy, "warn_below", max(threshold, 0.9))
    try:
        rows, pass_rate = check_live(
            entries, bundle, instructions_path=args.instructions,
            incidents_path=args.incidents, model=model)
    except ImportError as exc:
        raise SystemExit(f"error: live mode needs mitchella installed ({exc}). "
                         "Run dry mode, or `pip install` the engine and set MITCHELLA_API_KEY.")
    report = live_report(rows, pass_rate, threshold, warn_below)
    _emit(report, args.report)
    return 0 if pass_rate >= threshold else 1


def _emit(report, report_path):
    print(report)
    if report_path:
        Path(report_path).write_text(report, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
