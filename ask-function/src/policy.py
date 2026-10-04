"""Read the handful of scalars the function needs out of ``policy.yaml``.

``policy.yaml`` is the governance half of the rules release — the kill switch,
the model pin, the daily cap, the disclaimer, and the allowed subjects. The
function pulls only the few values it acts on, and it does so with a deliberate one-key reader rather than a
YAML library, so the function's dependency list stays at mitchella + anthropic +
the runtime's boto3. This mirrors ``evals/run.py`` in the rules template, which
reads the same file the same way.

The engine's behaviour (its instructions) comes from ``instructions.md``, not
from here; this file only governs what the box is *allowed* to do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _scan_scalar(text: str, key: str):
    """First ``key: value`` scalar at any indent, quotes stripped."""
    pat = re.compile(rf"^\s*{re.escape(key)}:\s*(.+?)\s*$", re.MULTILINE)
    m = pat.search(text)
    if not m:
        return None
    value = m.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _scan_list(text: str, key: str) -> tuple[str, ...]:
    """A top-level ``key:`` list of plain strings, block (``- a``) or flow
    (``[a, b]``) style. Comments and quotes are stripped; empty when absent."""
    m = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", text, re.MULTILINE)
    if not m:
        return ()
    rest = m.group(1).split("#", 1)[0].strip()
    if rest.startswith("["):
        items = rest.strip("[]").split(",")
    elif rest:
        return ()
    else:
        items = []
        for line in text[m.end():].splitlines()[1:]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            item = re.match(r"^\s+-\s*(.*)$", line)
            if not item:
                break
            items.append(item.group(1))
    out = []
    for item in items:
        item = item.split("#", 1)[0].strip().strip("\"'").strip()
        if item:
            out.append(item)
    return tuple(out)


@dataclass(frozen=True)
class Policy:
    enabled: bool
    model: str
    daily_cap: int | None
    disclaimer: str
    # The subjects the records cover, in the file's order. Only telemetry reads
    # them: an ``asked`` event names the first one a question mentions.
    allowed_subjects: tuple[str, ...] = ()


def parse_policy(text: str, *, default_model: str = "claude-sonnet-5-5") -> Policy:
    """Parse the scalars the runtime enforces out of ``policy.yaml`` text.

    ``enabled`` defaults to *false* when the key is missing or unreadable: an
    unparseable policy must take the box down, not leave it answering with no
    governance behind it. ``daily_cap`` is None when the file does not pin one,
    in which case the module's own cap variable stands.
    """
    enabled_raw = _scan_scalar(text, "enabled")
    enabled = (enabled_raw or "").strip().lower() == "true"

    model = _scan_scalar(text, "model") or default_model

    cap_raw = _scan_scalar(text, "daily_cap")
    try:
        daily_cap = int(cap_raw) if cap_raw is not None else None
    except ValueError:
        daily_cap = None

    disclaimer = _scan_scalar(text, "disclaimer") or ""

    return Policy(enabled=enabled, model=model, daily_cap=daily_cap, disclaimer=disclaimer,
                  allowed_subjects=_scan_list(text, "allowed_subjects"))
