"""Runtime configuration for the Ask function, read once from the environment.

Everything the function needs to find its records, its rules, and its guards
arrives as an environment variable set by the Terraform module. Nothing secret
is in here: the Anthropic key is read from SSM at cold start (see ``secrets.py``),
never from the environment.

The config is a frozen snapshot taken at import time. A change to any of these
values is a Terraform apply — a new function version — not a live edit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """The function's deploy-time settings, all from the environment."""

    # Where the published records live (the vault's `published` branch on Pages).
    published_base: str
    # The corpus to serve: a 64-char digest, or "latest" to follow the newest.
    corpus_digest: str
    # The rules repo (owner/name) and the release the box pins. "latest" follows
    # the newest rules-vN release so a kill-switch flip propagates within the
    # refresh window; an explicit "rules-vN" freezes behaviour until you move it.
    rules_repo: str
    rules_tag: str
    # The hard ceiling on questions answered per UTC day. policy.yaml's own
    # daily_cap (pinned by the rules release) overrides this when present; this
    # is the fallback the module always supplies.
    daily_cap: int
    # The one site origin allowed to call the box. "*" disables the origin check
    # (useful for a curl smoke test; never what a browser-facing box should run).
    allowed_origin: str
    # The DynamoDB table holding one counter row per UTC day.
    cap_table: str
    # SSM SecureString path for the Anthropic API key.
    ssm_key_path: str
    # Turnstile (bot check). Off by default so local and CI runs need no secret.
    turnstile_enabled: bool
    # SSM SecureString path for the Turnstile secret; only read when enabled.
    turnstile_secret_ssm_path: str
    # How long a fetched corpus/rules snapshot is served before re-polling, in
    # seconds. This is the "polls every few minutes" window.
    refresh_seconds: int
    # Shown when policy.yaml says enabled: false.
    maintenance_message: str
    # Optional obligations list (JSON) mapping obligation ids to subjects, so a
    # breach surfaced by standing.json matches on real topics, not bare ids.
    obligations_url: str
    aws_region: str
    # Optional pipeline events to your own Grafana Cloud Loki (see telemetry.py).
    # An empty URL means no events and no SSM read. The write token sits in an
    # SSM SecureString like the API key; the cluster label defaults to the rules
    # repo's owner.
    loki_push_url: str = ""
    loki_token_ssm_path: str = ""
    loki_cluster: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            published_base=os.environ.get("UVULARIA_PUBLISHED_BASE", "").rstrip("/"),
            corpus_digest=os.environ.get("UVULARIA_CORPUS_DIGEST", "latest").strip(),
            rules_repo=os.environ.get("UVULARIA_RULES_REPO", "").strip(),
            rules_tag=os.environ.get("UVULARIA_RULES_TAG", "latest").strip(),
            daily_cap=_int("UVULARIA_DAILY_CAP", 500),
            allowed_origin=os.environ.get("UVULARIA_ALLOWED_ORIGIN", "*").strip(),
            cap_table=os.environ.get("UVULARIA_CAP_TABLE", "").strip(),
            ssm_key_path=os.environ.get("UVULARIA_SSM_KEY_PATH", "").strip(),
            turnstile_enabled=_bool("UVULARIA_TURNSTILE_ENABLED", False),
            turnstile_secret_ssm_path=os.environ.get(
                "UVULARIA_TURNSTILE_SECRET_SSM_PATH", ""
            ).strip(),
            refresh_seconds=_int("UVULARIA_REFRESH_SECONDS", 300),
            maintenance_message=os.environ.get(
                "UVULARIA_MAINTENANCE_MESSAGE",
                "This records assistant is paused for maintenance. "
                "The published records and the board are still available.",
            ),
            obligations_url=os.environ.get("UVULARIA_OBLIGATIONS_URL", "").strip(),
            aws_region=os.environ.get("AWS_REGION", "us-east-1").strip(),
            loki_push_url=os.environ.get("UVULARIA_LOKI_PUSH_URL", "").strip(),
            loki_token_ssm_path=os.environ.get("UVULARIA_LOKI_TOKEN_SSM_PATH", "").strip(),
            loki_cluster=os.environ.get("UVULARIA_LOKI_CLUSTER", "").strip(),
        )
