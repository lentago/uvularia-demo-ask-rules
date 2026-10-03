"""Cloudflare Turnstile verification — the bot check in front of the box.

Off by default: a local run, a CI smoke test, or an internal deployment that does
not face the open web needs no Turnstile secret and no network round-trip. When
you turn it on (``turnstile_enabled`` in the module), every question must carry a
token the widget obtained from the Turnstile challenge, and the function verifies
it with Cloudflare before spending a cap slot or a token.

The verifier is pure-stdlib (``urllib``) so the function's dependency list stays
at mitchella, anthropic, and the runtime's boto3 — nothing is added for this.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def _post(url: str, fields: dict, timeout: float) -> dict:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed URL)
        return json.loads(resp.read().decode("utf-8"))


def verify(
    *,
    enabled: bool,
    token: str,
    secret: str,
    remote_ip: str = "",
    timeout: float = 5.0,
    http_post=_post,
) -> bool:
    """True if the request may proceed.

    When ``enabled`` is false this is the bypass flag: it returns True without a
    token and without any network call, which is what lets local and CI runs work
    with no secret. When enabled, a missing token fails closed, and the token is
    checked against Cloudflare; any error (network, malformed reply) also fails
    closed — a bot check that cannot run must not wave traffic through.
    """
    if not enabled:
        return True
    if not token or not secret:
        return False
    try:
        result = http_post(
            SITEVERIFY_URL,
            {"secret": secret, "response": token, "remoteip": remote_ip},
            timeout,
        )
    except Exception:
        return False
    return bool(result.get("success"))
