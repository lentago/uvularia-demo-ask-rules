"""Assemble mitchella's engine from the pinned rules release and the published corpus.

This is where the three pipelines meet. The function fetches, at a fixed version:

  * ``policy.yaml`` and ``instructions.md`` from the rules repo's pinned release —
    the governance (kill switch, model, cap) and the frozen system[0];
  * ``corpus-<digest>.json`` from the vault's published base — the records, by
    digest, so the box reads exactly what the board was built from;
  * ``standing.json`` and ``feed.xml`` from the same base, wired into mitchella's
    ``StandingProvider`` and ``AnnouncementProvider`` so the box structurally
    cannot claim compliance the board denies, and leads with active notices;
  * ``signals/incidents.toml`` from the rules repo at the pinned ref — the manual
    override channel.

The result is cached and re-polled every ``refresh_seconds``. A new rules release
(when the tag is "latest") or a new published digest (when the digest is "latest")
takes effect within that window with no redeploy — that is how a kill-switch flip
reaches the box in minutes.

Each re-poll reports what the box now serves (the ``served`` telemetry event),
and so does any call that finds the last report ``HEARTBEAT_SECONDS`` old, so
the rules repo's 15-minute ``GET /health`` ping keeps a quiet box reporting even
when ``refresh_seconds`` is set longer than that.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from policy import Policy, parse_policy

INCIDENTS_PATH = "/tmp/uvularia-incidents.toml"  # noqa: S108 — Lambda's only writable dir
# Report what is served at least this often while the function is being called.
# Under the heartbeat workflow's 15 minutes, so a ping that lands a little early
# (GitHub's schedules drift) still reports.
HEARTBEAT_SECONDS = 600


def _http_get(url: str, timeout: float = 10.0, accept: str = "") -> str:
    headers = {"User-Agent": "uvularia-ask-function"}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8")


def resolve_rules_tag(repo: str, tag: str, http_get=_http_get) -> str:
    """Turn "latest" into the newest ``rules-vN`` tag; pass a real tag through."""
    if tag and tag != "latest":
        return tag
    body = http_get(
        f"https://api.github.com/repos/{repo}/releases/latest",
        accept="application/vnd.github+json",
    )
    return json.loads(body)["tag_name"]


def _release_asset_url(repo: str, tag: str, name: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{name}"


def _raw_url(repo: str, tag: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{tag}/{path}"


def fetch_incidents(repo: str, tag: str, http_get=_http_get) -> str | None:
    """The pinned ``incidents.toml`` from the rules repo, or None if absent.

    It is not a release asset; it is read raw at the pinned ref, so the override
    channel is pinned to the same rules version as the policy. A missing file is
    the normal, empty-desk case, not an error.
    """
    try:
        return http_get(_raw_url(repo, tag, "signals/incidents.toml"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def corpus_url(published_base: str, digest: str) -> str:
    if not digest or digest == "latest":
        return f"{published_base}/corpus-latest.json"
    return f"{published_base}/corpus-{digest}.json"


def _load_obligations(url: str, http_get=_http_get):
    """Optional obligation list so a breach maps to subjects, not a bare id."""
    if not url:
        return None
    try:
        return json.loads(http_get(url))
    except Exception:  # noqa: BLE001 — the enrichment is optional; ids still work
        return None


@dataclass
class Deployment:
    policy: Policy
    engine: object | None  # a mitchella.Engine, or None while the box is paused
    resolved_tag: str
    resolved_digest: str
    fetched_at: float


def build(cfg, client, *, now: float, http_get=_http_get) -> Deployment:
    """Fetch everything at the pinned versions and build the engine.

    When ``policy.enabled`` is false the engine is not built at all — no corpus
    fetch, no model client exercised — because the only reply the box will give
    is the maintenance line.
    """
    tag = resolve_rules_tag(cfg.rules_repo, cfg.rules_tag, http_get)
    policy_text = http_get(_release_asset_url(cfg.rules_repo, tag, "policy.yaml"))
    policy = parse_policy(policy_text)

    if not policy.enabled:
        return Deployment(policy=policy, engine=None, resolved_tag=tag,
                          resolved_digest="", fetched_at=now)

    # mitchella and the structured imports are pulled in only when the box is
    # live, so a paused box (and the unit tests) need neither installed.
    import mitchella.engine as mengine
    from mitchella import (
        AnnouncementProvider,
        Config,
        Engine,
        ManualOverrideProvider,
        SignalPlane,
        StandingProvider,
        load_corpus,
    )

    instructions = http_get(_release_asset_url(cfg.rules_repo, tag, "instructions.md"))
    # Everything below the leading HTML comment is the verbatim system[0].
    mengine.INSTRUCTIONS = instructions.split("-->", 1)[-1].strip()

    curl = corpus_url(cfg.published_base, cfg.corpus_digest)
    corpus = load_corpus(curl, "bundle", timeout=10.0)

    providers = [
        StandingProvider(
            f"{cfg.published_base}/standing.json",
            obligations=_load_obligations(cfg.obligations_url, http_get),
            timeout=10.0,
        ),
        AnnouncementProvider(f"{cfg.published_base}/feed.xml", timeout=10.0),
    ]
    incidents = fetch_incidents(cfg.rules_repo, tag, http_get)
    if incidents is not None:
        Path(INCIDENTS_PATH).write_text(incidents, encoding="utf-8")
        providers.append(ManualOverrideProvider(INCIDENTS_PATH))

    engine = Engine(
        corpus=corpus,
        signal_plane=SignalPlane(providers),
        client=client,
        config=Config(model=policy.model),
    )
    return Deployment(policy=policy, engine=engine, resolved_tag=tag,
                      resolved_digest=corpus.fingerprint, fetched_at=now)


_STATE: dict = {"deployment": None, "reported_at": None}


def get_deployment(cfg, client, *, now: float | None = None, http_get=_http_get,
                   on_refresh=None, heartbeat_seconds: float = HEARTBEAT_SECONDS) -> Deployment:
    """Return a cached deployment, re-polling once ``refresh_seconds`` has passed.

    ``on_refresh`` is called with each freshly built deployment, and with the
    cached one when the last call to it is ``heartbeat_seconds`` old — the
    ``served`` telemetry event hangs off it. It must not raise (the pusher
    never does).
    """
    if now is None:
        now = time.time()
    current = _STATE["deployment"]
    if current is None or (now - current.fetched_at) >= cfg.refresh_seconds:
        current = build(cfg, client, now=now, http_get=http_get)
        _STATE["deployment"] = current
        report = True
    else:
        last = _STATE.get("reported_at")
        report = last is None or (now - last) >= heartbeat_seconds
    if report and on_refresh is not None:
        _STATE["reported_at"] = now
        on_refresh(current)
    return current
