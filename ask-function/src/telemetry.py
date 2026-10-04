"""Pipeline events from the Ask function to your own Grafana Cloud Loki — optional.

Two events, both in the ``ask`` pipeline. Field names follow drosera's event
contract (``docs/clients/uvularia.md`` in lentago/drosera), which the pipeline
pane reads:

  * ``served`` — each time the function re-polls and builds a fresh deployment,
    and at least every ``HEARTBEAT_SECONDS`` while it is being called (the rules
    repo's heartbeat workflow calls ``GET /health`` every 15 minutes): the
    corpus digest and the rules tag it is answering from, and ``at``;
  * ``asked`` — once per question: ``at``, the outcome kind, the latency, the
    cap used and remaining, the digest it answered from, the first allowed
    subject the question mentions (``subject``), which signals degraded, and the
    question truncated to 500 characters. Never the origin, the IP, a token, or
    anything else that says who asked. ``asked_payload`` builds the line from an
    explicit list of fields, so nothing else can slip in.

``at`` is whole Unix seconds: a Grafana query cannot read a log line's own
timestamp as a number, so the time rides in the payload.

With no ``loki_push_url`` the pusher is a no-op: nothing is read from SSM and
nothing is sent. With one, every push is best-effort — a short timeout, and any
failure (an unreadable token, a Loki outage, a rejected label) becomes one
warning line in CloudWatch. A push never fails or changes an answer.

The wire format is drosera's ``loki_push.py``, vendored unchanged beside this
file, so the labels match the workflows' loki-event steps exactly.
"""

from __future__ import annotations

import json
import re
from typing import Callable

QUESTION_LIMIT = 500
PUSH_TIMEOUT_SECONDS = 2.0
SOURCE = "uvularia"
PIPELINE = "ask"

# The only keys an ``asked`` event may carry. A test holds this to the issues'
# lists (#52, #59); adding a field here is a deliberate, reviewed change.
ASKED_FIELDS = ("at", "kind", "latency_ms", "cap_used", "cap_remaining", "digest",
                "subject", "degraded_signals", "question")
NO_SUBJECT = "unmatched"


def first_subject(question: str, subjects) -> str | None:
    """The allowed subject the question mentions first, ``"unmatched"`` if it
    mentions none, or None when there is no subject list to match against.

    Matching is mitchella's own rule for incident subjects: a case-insensitive
    substring of the question. "First" is the earliest in the question; a tie
    goes to the subject listed first in ``policy.yaml``. Only the subject goes
    in the event — never the words around it.
    """
    subjects = [s for s in (subjects or ()) if s]
    if not subjects:
        return None
    text = (question or "").lower()
    hits = [(text.find(s.lower()), i, s) for i, s in enumerate(subjects)]
    hits = [h for h in hits if h[0] >= 0]
    return min(hits)[2] if hits else NO_SUBJECT


def asked_payload(*, at: int, kind: str, latency_ms: int, cap_used, cap_remaining,
                  digest, subject, degraded_signals, question: str) -> dict:
    """One question's event. Keyword-only, so a caller cannot pass the request
    (and with it the origin or IP) by accident. ``subject`` is None when the
    rules list no subjects, and is then left out: nobody knows it."""
    payload = {
        "at": int(at),
        "kind": kind,
        "latency_ms": latency_ms,
        "cap_used": cap_used,
        "cap_remaining": cap_remaining,
        "digest": digest or None,
        "subject": subject,
        "degraded_signals": list(degraded_signals or ()),
        "question": (question or "")[:QUESTION_LIMIT],
    }
    if subject is None:
        del payload["subject"]
    return payload


def served_payload(deployment, *, at: float) -> dict:
    """What the box is answering from, as of ``at``. ``digest`` is None while
    the box is paused (a paused box loads no corpus)."""
    return {
        "at": int(at),
        "digest": getattr(deployment, "resolved_digest", "") or None,
        "rules_tag": getattr(deployment, "resolved_tag", "") or None,
        "enabled": bool(getattr(getattr(deployment, "policy", None), "enabled", False)),
    }


def cluster_slug(explicit: str, rules_repo: str) -> str:
    """The ``cluster`` label: your org slug. Defaults to the rules repo's owner,
    lowercased, so it matches the workflows' default (the repository owner)."""
    raw = (explicit or rules_repo.split("/", 1)[0] or "").strip().lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-_")[:63]
    return slug or "unknown"


def _print_json(rec: dict) -> None:
    print(json.dumps(rec), flush=True)


class Pusher:
    """Sends one event per call, or does nothing when no URL is configured."""

    def __init__(self, url: str, cluster: str, repo: str,
                 read_token: Callable[[], str], *, push: Callable | None = None,
                 log: Callable[[dict], None] = _print_json,
                 timeout: float = PUSH_TIMEOUT_SECONDS):
        self.url = (url or "").strip()
        self.cluster = cluster
        self.repo = repo
        self._read_token = read_token
        self._push = push
        self._log = log
        self.timeout = timeout
        self._token: str | None = None
        self._disabled_reason = ""

    @property
    def enabled(self) -> bool:
        return bool(self.url) and not self._disabled_reason

    def emit(self, stage: str, payload: dict) -> bool:
        """Push one event. Returns True if Loki accepted it; never raises."""
        if not self.enabled:
            return False
        try:
            if self._token is None:
                try:
                    self._token = self._read_token()
                except Exception as exc:  # noqa: BLE001
                    # An unreadable token will stay unreadable for this
                    # container: warn once and stop trying, rather than pay an
                    # SSM call on every question.
                    self._disabled_reason = f"write token unreadable: {exc}"
                    self._log({"event": "telemetry_warning", "stage": stage,
                               "error": self._disabled_reason})
                    return False
            push = self._push
            if push is None:
                from loki_push import push_event as push
            push(self.url, self._token, cluster=self.cluster, source=SOURCE,
                 pipeline=PIPELINE, stage=stage, repo=self.repo, payload=payload,
                 timeout=self.timeout)
            return True
        except Exception as exc:  # noqa: BLE001 — telemetry never fails an answer
            self._log({"event": "telemetry_warning", "stage": stage,
                       "error": f"{type(exc).__name__}: {exc}"})
            return False


def make_pusher(cfg, read_secure_string: Callable[[str, str], str], **kw) -> Pusher:
    """The pusher for this deployment's config. No URL → a no-op pusher that
    never touches SSM."""
    return Pusher(
        url=cfg.loki_push_url,
        cluster=cluster_slug(cfg.loki_cluster, cfg.rules_repo),
        repo=cfg.rules_repo,
        read_token=lambda: read_secure_string(cfg.loki_token_ssm_path, cfg.aws_region),
        **kw,
    )
