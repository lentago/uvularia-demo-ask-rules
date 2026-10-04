"""The Function URL entrypoint: one POST, one grounded answer, with the guards.

The request path, in order, each guard cheaper than the one it protects:

  1. CORS. Answer the preflight; refuse a browser origin that is not the site's.
  2. Parse. A POST with a ``question``; anything else is a 4xx, no model touched.
  3. Kill switch. If ``policy.yaml`` says ``enabled: false`` the box returns a
     maintenance line and nothing else — before a bot check, a cap slot, or a
     token is spent.
  4. Turnstile. The bot check (off by default). A failure is a 403.
  5. Cap. One atomic DynamoDB increment; a full day is a 429. This bounds the
     month's model spend no matter what traffic arrives.
  6. Answer. mitchella's engine replies, the citation gate keeps only the ids it
     verified against the corpus, and the reply carries which signals degraded.

The turn log line goes to stdout (CloudWatch): the outcome, the latency, the cap
state, and the question truncated to 500 characters. No origin, no IP, no token —
nothing that identifies who asked.
"""

from __future__ import annotations

import base64
import json
import time

from secrets import SecretUnavailable
from dataclasses import dataclass
from typing import Callable

from config import Config

QUESTION_LOG_LIMIT = 500


@dataclass
class Deps:
    """Everything the request handler calls out to, injectable for tests."""

    config: Config
    get_deployment: Callable          # (now: float) -> Deployment
    cap_reserve: Callable             # (day, cap, now) -> (allowed: bool, count: int)
    verify_turnstile: Callable        # (token: str, remote_ip: str) -> bool
    answer_question: Callable         # (engine, question: str) -> Answer-like
    clock: Callable = time.time
    log: Callable = lambda rec: print(json.dumps(rec), flush=True)  # noqa: E731


# --------------------------------------------------------------------------- #
# HTTP plumbing.                                                               #
# --------------------------------------------------------------------------- #

def _cors_headers(cfg: Config) -> dict:
    return {
        "Access-Control-Allow-Origin": cfg.allowed_origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
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
        deps.log({"event": "turn", "kind": "maintenance",
                  "question": question[:QUESTION_LOG_LIMIT]})
        return _reply(200, {"kind": "maintenance", "reply": cfg.maintenance_message,
                            "source_ids": [], "degraded_signals": []}, cfg)

    # 4. Turnstile.
    token = (body.get("turnstile_token") or "").strip()
    if not deps.verify_turnstile(token, _source_ip(event)):
        return _reply(403, {"kind": "declined",
                            "reply": "Could not verify the request. Please try again."}, cfg)

    # 5. Daily cap — policy.yaml's pinned value wins over the module's fallback.
    cap = policy.daily_cap if policy.daily_cap is not None else cfg.daily_cap
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    allowed, count = deps.cap_reserve(day, cap, now)
    if not allowed:
        deps.log({"event": "turn", "kind": "capped", "cap": cap,
                  "question": question[:QUESTION_LOG_LIMIT]})
        return _reply(429, {"kind": "declined",
                            "reply": "This assistant has answered its limit of questions "
                                     "for today. Please try again tomorrow.",
                            "source_ids": [], "degraded_signals": []}, cfg)

    # 6. Answer. The engine's gate has already dropped any id that is not a real
    # record, so answer.sources carries only verified citations.
    try:
        answer = deps.answer_question(deployment.engine, question)
    except SecretUnavailable as exc:
        deps.log({"event": "turn", "kind": "unconfigured", "error": str(exc),
                  "question": question[:QUESTION_LOG_LIMIT]})
        return _reply(503, {"kind": "maintenance",
                            "reply": "This assistant is not set up yet. "
                                     "Please check back soon.",
                            "source_ids": [], "degraded_signals": ["api-key"]}, cfg)
    source_ids = [s.doc_id for s in answer.sources]
    degraded = list(answer.degraded_signals)

    deps.log({
        "event": "turn",
        "kind": answer.kind.value,
        "latency_ms": round((deps.clock() - now) * 1000),
        "cap_used": count,
        "cap_remaining": max(cap - count, 0),
        "digest": deployment.resolved_digest,
        "source_ids": source_ids,
        "degraded_signals": degraded,
        "question": question[:QUESTION_LOG_LIMIT],
    })

    return _reply(200, {
        "kind": answer.kind.value,
        "reply": answer.text,
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
    from turnstile import verify as turnstile_verify

    cfg = Config.from_env()

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
        get_deployment=lambda now: get_deployment(cfg, client, now=now),
        cap_reserve=lambda day, c, now: cap.reserve(day, c, int(now)),
        verify_turnstile=lambda token, ip: turnstile_verify(
            enabled=cfg.turnstile_enabled, token=token,
            secret=turnstile_secret(), remote_ip=ip),
        answer_question=_answer_question,
    )


def handler(event, context):
    """AWS Lambda entrypoint. Builds its dependencies once, then serves."""
    global _DEPS
    if _DEPS is None:
        _DEPS = _build_deps()
    return handle(event, _DEPS)
