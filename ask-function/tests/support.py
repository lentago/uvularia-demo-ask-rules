"""Shared fakes and path setup for the Ask-function unit tests.

The unit tests exercise the function's own logic — the cap, the kill switch, the
citation surface, the Turnstile bypass — with no network, no API key, no DynamoDB,
and without mitchella or anthropic installed. Importing this module first puts
``src/`` on the path and gives every test the same small stand-ins for the engine
result and the deployment.
"""

from __future__ import annotations

import sys
from collections import namedtuple
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FakeSource = namedtuple("FakeSource", "doc_id title path")


class FakeKind:
    """Stands in for mitchella's AnswerKind — all the handler reads is ``.value``."""

    def __init__(self, value: str):
        self.value = value


class FakeAnswer:
    def __init__(self, kind: str, text: str, sources=(), degraded=()):
        self.kind = FakeKind(kind)
        self.text = text
        self.sources = tuple(sources)
        self.degraded_signals = tuple(degraded)


class FakeDeployment:
    def __init__(self, policy, engine=object(), digest="deadbeef"):
        self.policy = policy
        self.engine = engine
        self.resolved_digest = digest


def make_event(method="POST", body=None, origin=None, raw_body=None):
    """A minimal Function URL (payload v2.0) event."""
    import json

    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if raw_body is not None:
        payload = raw_body
    elif body is not None:
        payload = json.dumps(body)
    else:
        payload = ""
    return {
        "requestContext": {"http": {"method": method, "sourceIp": "203.0.113.7"}},
        "headers": headers,
        "body": payload,
        "isBase64Encoded": False,
    }
