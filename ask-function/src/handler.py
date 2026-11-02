"""The Function URL entrypoint: one POST, one source-linked answer, with the guards.

Plus one read-only route, ``GET /health``: what the box is serving (the corpus
digest and the rules tag), whether it is switched on, and how much of today's
cap is used. It handles no question, claims no cap slot, and calls no model.
The records vault's watch workflow reads it to notice a stale digest or a cap
running out (see ``health_body``), and the rules repo's heartbeat workflow
calls it every 15 minutes so a quiet box still sends its ``served`` event.

The request path, in order, each guard cheaper than the one it protects:

  1. CORS. Answer the preflight; refuse a browser origin that is not the site's.
  2. Parse. A POST with a ``question``; anything else is a 4xx, no model touched.
  3. Kill switch. If ``policy.yaml`` says ``enabled: false`` the box returns a
     maintenance line and nothing else — before a bot check, a cap slot, or a
     token is spent.
  4. Turnstile. The bot check (off by default). A failure is a 403.
  5. Cap. One atomic DynamoDB increment; a full day is a 429. This bounds the
     month's model spend no matter what traffic arrives.
  6. Answer. mitchella's engine replies, and its citation gate drops any cited id
     that is not a record in the corpus. An ``answered`` reply with no source
     left is not sent as an answer: the reader gets the escalation reply
     instead (``UNSOURCED_REPLY``). The reply carries which signals degraded.

The gate checks that a cited record exists, not that it says what the answer
says. The answer is source-linked, not proven; the widget shows the sources and
asks the reader to check them.

The turn log line goes to stdout (CloudWatch): the outcome, the latency, the cap
state, and the question truncated to ``question_log_chars`` (500 at most; 0
leaves the question out). No origin, no IP, no token — nothing that identifies
who asked. When Loki is configured, the same turn also goes out as one ``asked``
event (see ``telemetry.py``), built from an explicit field list so nothing
identifying can ride along; without it, nothing is sent. The question itself
always goes to the model provider: that is how the answer is made.
"""

from __future__ import annotations

import base64
import json
import time

from secrets import SecretUnavailable
from dataclasses import dataclass
from typing import Callable

from config import Config
from telemetry import asked_payload, first_subject

# The most of a question the turn log or the ``asked`` event will ever keep.
# ``Config.question_log_chars`` can lower it, or set it to 0 to keep none.
QUESTION_LOG_LIMIT = 500

# What the reader gets instead of an ``answered`` reply that cites no real
# record. It follows step 3 of the rules' instructions.md: do not guess, and
# say how to ask the organization directly.
UNSOURCED_REPLY = ("I can't point to a published record that answers that, so I "
                   "won't guess. Please ask the organization directly.")


@dataclass
class Deps:
    """Everything the request handler calls out to, injectable for tests."""

    config: Config
    get_deployment: Callable          # (now: float) -> Deployment
    cap_reserve: Callable             # (day, cap, now) -> (allowed: bool, count: int)
    verify_turnstile: Callable        # (token: str, remote_ip: str) -> bool
    answer_question: Callable         # (engine, question: str) -> Answer-like
    cap_used: Callable = lambda day: None  # noqa: E731 — (day) -> int | None; a read
    clock: Callable = time.time
    log: Callable = lambda rec: print(json.dumps(rec), flush=True)  # noqa: E731
    emit: Callable = lambda stage, payload: False  # noqa: E731 — telemetry; never raises


# --------------------------------------------------------------------------- #
# HTTP plumbing.                                                               #
# --------------------------------------------------------------------------- #

def _cors_headers(cfg: Config) -> dict:
    return {
        "Access-Control-Allow-Origin": cfg.allowed_origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "content-type",
        "Vary": "Origin",
        "Content-Type": "application/json",
    }


def _reply(status: int, body: dict, cfg: Config) -> dict:
    return {
        "statusCode": status,
        "headers": _cors_headers(cfg),
        "body": json.dumps(body),
    }


def _method(event: dict) -> str:
    return event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()


def _path(event: dict) -> str:
    return (event.get("rawPath") or "/").rstrip("/") or "/"


def _source_ip(event: dict) -> str:
    return event.get("requestContext", {}).get("http", {}).get("sourceIp", "")


def _lower_headers(event: dict) -> dict:
    return {k.lower(): v for k, v in (event.get("headers") or {}).items()}


def _parse_body(event: dict) -> dict:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _logged_question(question: str, cfg: Config) -> str | None:
    """The question as the turn log and the ``asked`` event may keep it: cut to
    ``cfg.question_log_chars``, never past ``QUESTION_LOG_LIMIT``. None when
    that is 0 (or less), and the caller then leaves the field out entirely."""
    limit = min(max(cfg.question_log_chars, 0), QUESTION_LOG_LIMIT)
    return question[:limit] if limit else None


def _log_turn(deps: Deps, record: dict, question: str) -> None:
    """One turn log line, with the question only as far as the config allows."""
    logged = _logged_question(question, deps.config)
    if logged is not None:
        record["question"] = logged
    deps.log(record)


def _asked(deps: Deps, start: float, kind: str, question: str, deployment, *,
           cap_used=None, cap_remaining=None, degraded=()) -> None:
    """One ``asked`` event for this turn. Best-effort: the pusher never raises.

    ``at`` is when the question arrived; ``subject`` is the first of the rules'
    allowed subjects the question mentions (see ``telemetry.first_subject``),
    matched on the whole question even when none of it is logged.
    """
    deps.emit("asked", asked_payload(
        at=start, kind=kind, latency_ms=round((deps.clock() - start) * 1000),
        cap_used=cap_used, cap_remaining=cap_remaining,
        digest=getattr(deployment, "resolved_digest", "") or None,
        subject=first_subject(question,
                              getattr(deployment.policy, "allowed_subjects", ())),
        degraded_signals=degraded, question=_logged_question(question, deps.config)))


# --------------------------------------------------------------------------- #
# GET /health.                                                                 #
# --------------------------------------------------------------------------- #

def _today_cap(deployment, cfg: Config) -> int:
    """policy.yaml's pinned cap wins over the module's fallback, as for a POST."""
    cap = deployment.policy.daily_cap
    return cap if cap is not None else cfg.daily_cap


def health_body(deployment, cfg: Config, *, day: str, cap_used) -> dict:
    """What ``GET /health`` returns, from an explicit list of fields.

    The served digest and rules tag, whether the box is on, and today's cap.
    Never the key, a token, an SSM path, or anything about who is asking. A
    paused box loads no corpus, so its ``digest`` is null. ``cap_used`` is null
    when the counter could not be read — unknown, not zero.
    """
    return {
        "status": "ok",
        "enabled": bool(deployment.policy.enabled),
        "digest": deployment.resolved_digest or None,
        # An explicit digest in the deploy config is a deliberate pin: the box is
        # meant to lag the vault, so a watcher should not call it stale.
        "digest_pinned": cfg.corpus_digest not in ("", "latest"),
        "rules_tag": getattr(deployment, "resolved_tag", "") or None,
        "day": day,
        "cap_used": cap_used,
        "cap": _today_cap(deployment, cfg),
    }


def _health(deps: Deps) -> dict:
    cfg = deps.config
    now = deps.clock()
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    try:
        deployment = deps.get_deployment(now)
    except Exception as exc:  # noqa: BLE001 — a health check reports, it never crashes
        deps.log({"event": "health", "status": "error",
                  "error": f"{type(exc).__name__}: {exc}"})
        return _reply(503, {"status": "error",
                            "reply": "The box could not load its rules or records."}, cfg)
    try:
        used = deps.cap_used(day)
    except Exception as exc:  # noqa: BLE001
        deps.log({"event": "health", "status": "cap_unreadable",
                  "error": f"{type(exc).__name__}: {exc}"})
        used = None
    return _reply(200, health_body(deployment, cfg, day=day, cap_used=used), cfg)


# --------------------------------------------------------------------------- #
# The request handler.                                                         #
# --------------------------------------------------------------------------- #

def handle(event: dict, deps: Deps) -> dict:
    cfg = deps.config
    method = _method(event)
    headers = _lower_headers(event)
    origin = headers.get("origin", "")

    if method == "OPTIONS":
        h = _cors_headers(cfg)
        h["Access-Control-Max-Age"] = "86400"
        return {"statusCode": 204, "headers": h, "body": ""}

    # A browser from the wrong origin is refused outright. A caller with no Origin
    # header (curl, a smoke test) is allowed through so the box can be exercised
    # without a page; "*" disables the check entirely.
    if cfg.allowed_origin != "*" and origin and origin != cfg.allowed_origin:
        return _reply(403, {"kind": "declined",
                            "reply": "This assistant only answers from its own site."}, cfg)

    if _path(event) == "/health":
        if method != "GET":
            return _reply(405, {"status": "error", "reply": "Read /health with GET."}, cfg)
        return _health(deps)

    if method != "POST":
        return _reply(405, {"kind": "declined", "reply": "Send a question with POST."}, cfg)

    body = _parse_body(event)
    question = (body.get("question") or "").strip()
    if not question:
        return _reply(400, {"kind": "declined",
                            "reply": "Ask a question in the \"question\" field."}, cfg)

    now = deps.clock()
    deployment = deps.get_deployment(now)
    policy = deployment.policy

    # 3. Kill switch — the cheapest, fastest lever, checked before any spend.
    if not policy.enabled:
        _log_turn(deps, {"event": "turn", "kind": "maintenance"}, question)
        _asked(deps, now, "maintenance", question, deployment)
        return _reply(200, {"kind": "maintenance", "reply": cfg.maintenance_message,
                            "source_ids": [], "degraded_signals": []}, cfg)

    # 4. Turnstile.
    token = (body.get("turnstile_token") or "").strip()
    if not deps.verify_turnstile(token, _source_ip(event)):
        return _reply(403, {"kind": "declined",
                            "reply": "Could not verify the request. Please try again."}, cfg)

    # 5. Daily cap — policy.yaml's pinned value wins over the module's fallback.
    cap = _today_cap(deployment, cfg)
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    allowed, count = deps.cap_reserve(day, cap, now)
    if not allowed:
        _log_turn(deps, {"event": "turn", "kind": "capped", "cap": cap}, question)
        _asked(deps, now, "capped", question, deployment, cap_used=cap, cap_remaining=0)
        return _reply(429, {"kind": "declined",
                            "reply": "This assistant has answered its limit of questions "
                                     "for today. Please try again tomorrow.",
                            "source_ids": [], "degraded_signals": []}, cfg)

    # 6. Answer. The engine's gate has already dropped any cited id that is not a
    # record in the corpus, so answer.sources holds only records that exist.
    try:
        answer = deps.answer_question(deployment.engine, question)
    except SecretUnavailable as exc:
        _log_turn(deps, {"event": "turn", "kind": "unconfigured", "error": str(exc)},
                  question)
        _asked(deps, now, "unconfigured", question, deployment, cap_used=count,
               cap_remaining=max(cap - count, 0), degraded=["api-key"])
        return _reply(503, {"kind": "maintenance",
                            "reply": "This assistant is not set up yet. "
                                     "Please check back soon.",
                            "source_ids": [], "degraded_signals": ["api-key"]}, cfg)
    source_ids = [s.doc_id for s in answer.sources]
    degraded = list(answer.degraded_signals)
    # ``kind`` is what the turn log and the event record; ``reply_kind`` is what
    # the reader is sent. They differ only for the fallback below, the same way
    # a capped or unconfigured turn is logged under its own name.
    kind = reply_kind = answer.kind.value
    reply = answer.text

    # An answer with no real record behind it is never sent as an answer. The
    # reader gets the escalation reply instead, and the turn is logged as
    # ``unsourced`` so the operator can see how often it happens.
    if kind == "answered" and not source_ids:
        kind, reply_kind, reply = "unsourced", "escalated", UNSOURCED_REPLY

    _log_turn(deps, {
        "event": "turn",
        "kind": kind,
        "latency_ms": round((deps.clock() - now) * 1000),
        "cap_used": count,
        "cap_remaining": max(cap - count, 0),
        "digest": deployment.resolved_digest,
        "source_ids": source_ids,
        "degraded_signals": degraded,
    }, question)
    _asked(deps, now, kind, question, deployment, cap_used=count,
           cap_remaining=max(cap - count, 0), degraded=degraded)

    return _reply(200, {
        "kind": reply_kind,
        "reply": reply,
        "source_ids": source_ids,
        "degraded_signals": degraded,
    }, cfg)


# --------------------------------------------------------------------------- #
# Cold-start wiring for the real Lambda (built once per container).            #
# --------------------------------------------------------------------------- #

_DEPS: Deps | None = None


def _answer_question(engine, question: str):
    from mitchella.contract import Query

    return engine.answer(Query(text=question, surface="lambda"))


def _build_deps() -> Deps:
    import anthropic

    from cap import DailyCap
    from engine_build import get_deployment
    from secrets import read_secure_string
    from telemetry import make_pusher, served_payload
    from turnstile import verify as turnstile_verify

    cfg = Config.from_env()
    # No-op unless UVULARIA_LOKI_PUSH_URL is set; never raises either way.
    pusher = make_pusher(cfg, read_secure_string)

    # The API key is read lazily, on the first call that actually needs the
    # model: the guards above it (origin, kill switch, Turnstile, cap) must all
    # work on a box whose key has not been written yet, and a missing key must
    # surface as a polite reply, not a crash at cold start.
    class _LazyClient:
        _real = None

        def __getattr__(self, name):
            if self._real is None:
                api_key = read_secure_string(cfg.ssm_key_path, cfg.aws_region)
                self._real = anthropic.Anthropic(api_key=api_key)
            return getattr(self._real, name)

    client = _LazyClient()

    def turnstile_secret():
        if cfg.turnstile_enabled and cfg.turnstile_secret_ssm_path:
            return read_secure_string(cfg.turnstile_secret_ssm_path, cfg.aws_region)
        return ""

    cap = DailyCap(cfg.cap_table, cfg.aws_region)

    return Deps(
        config=cfg,
        get_deployment=lambda now: get_deployment(
            cfg, client, now=now,
            on_refresh=lambda d: pusher.emit("served", served_payload(d, at=now))),
        cap_reserve=lambda day, c, now: cap.reserve(day, c, int(now)),
        cap_used=cap.used,
        verify_turnstile=lambda token, ip: turnstile_verify(
            enabled=cfg.turnstile_enabled, token=token,
            secret=turnstile_secret(), remote_ip=ip),
        answer_question=_answer_question,
        emit=pusher.emit,
    )


def handler(event, context):
    """AWS Lambda entrypoint. Builds its dependencies once, then serves."""
    global _DEPS
    if _DEPS is None:
        _DEPS = _build_deps()
    return handle(event, _DEPS)
