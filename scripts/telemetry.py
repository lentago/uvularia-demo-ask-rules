#!/usr/bin/env python3
"""Describe one pipeline stage as a telemetry event, or say telemetry is off.

Every uvularia workflow ends with two telemetry steps. This script is the first:
it decides whether telemetry is configured, and if it is, builds the one JSON
event the second step (drosera's ``loki-event`` action) pushes to your own
Grafana Cloud Loki. It never sends anything itself and never fails the job.

    python3 scripts/telemetry.py published --out-dir _out
    python3 scripts/telemetry.py reviewed --standing standing.json --validate success --pr 12
    python3 scripts/telemetry.py intake --issue 42 --is-form true --scaffold success \\
        --branch-exists false --pr-result opened --pr-number 43
    python3 scripts/telemetry.py evals --summary evals-summary.json --corpus present
    python3 scripts/telemetry.py rules_released --tag rules-v7
    python3 scripts/telemetry.py intake --scheduled --snapshot intake-snapshot.json

It reads its configuration from the environment the workflow passes in:

  * ``LOKI_PUSH_URL`` — the repository variable. Empty means telemetry is not
    configured: one notice line, no event, exit 0.
  * ``LOKI_TOKEN_SET`` — "true" when the ``LOKI_WRITE_TOKEN`` secret is present
    (the workflow passes the test, never the token). A URL with no token is a
    notice too — a pull request from a fork never sees secrets.
  * ``LOKI_CLUSTER`` — optional repository variable naming your organization.
    Defaults to the repository owner, lowercased.

and writes three step outputs to ``$GITHUB_OUTPUT``: ``enabled`` ("true" or
"false"), ``cluster``, and ``payload`` (one line of JSON). The payload is the
stage's detail — counts, digests, outcomes. It never carries a person's name,
an email, or anything typed into an issue beyond its number.

Every payload carries ``at``: epoch seconds of the thing the event is about.
For a publish that is the receipt's ``published_at``, the server-side publish
time; for everything else it is this run's own clock. A Grafana query cannot
read a log line's own timestamp as a number, so the time rides in the payload.

``--snapshot FILE`` merges counts another script has already gathered (the
records template's ``scripts/pipeline_snapshot.py``: open intake, pull requests
waiting on a reviewer, announcement latency). Only the fields listed for the
stage in ``SNAPSHOT_FIELDS`` are taken, and only as whole numbers; a missing or
unreadable file adds nothing, so the dashboard reads "no data", not zero.
``--scheduled`` marks the daily snapshot run: it is about no single issue or
pull request, so its event carries the snapshot and nothing else.

The same file ships in the records and the rules templates; uvularia's own CI
keeps the two copies byte-identical. Python 3.12, standard library only.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STAGES = ("intake", "reviewed", "published", "evals", "rules_released")

# The fields --snapshot may add, per stage. Field names are drosera's event
# contract (docs/clients/uvularia.md in lentago/drosera); anything else in the
# file is ignored, so nothing can ride along by accident.
SNAPSHOT_FIELDS = {
    "intake": ("open", "oldest_opened_at"),
    "reviewed": ("awaiting", "oldest_green_at"),
    "published": ("announcement_latency_s",),
}
NOT_CONFIGURED = ("telemetry not configured — set the LOKI_PUSH_URL variable and the "
                  "LOKI_WRITE_TOKEN secret to send pipeline events to your Grafana Cloud Loki")


# --------------------------------------------------------------------------- #
# Configuration.                                                               #
# --------------------------------------------------------------------------- #

def cluster_slug(explicit, owner):
    """The ``cluster`` label: your org as a lowercase slug Loki will accept.

    The loki-event action rejects anything outside ``[a-z0-9][a-z0-9_-]*``, and
    a GitHub owner name can carry capitals, so it is folded here rather than
    left to fail at push time.
    """
    raw = (explicit or owner or "").strip().lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-_")[:63]
    return slug or "unknown"


def config_state(env):
    """``(enabled, message)`` from the environment. The message is printed as-is."""
    if not (env.get("LOKI_PUSH_URL") or "").strip():
        return False, f"::notice title=telemetry::{NOT_CONFIGURED}"
    if (env.get("LOKI_TOKEN_SET") or "").strip().lower() != "true":
        return False, ("::notice title=telemetry::LOKI_PUSH_URL is set but the LOKI_WRITE_TOKEN "
                       "secret is not available to this run (not set, or a pull request from a "
                       "fork); no event sent")
    return True, ""


# --------------------------------------------------------------------------- #
# Small readers. Each tolerates a missing file: a failed run still reports.    #
# --------------------------------------------------------------------------- #

def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def standing_summary(rows):
    """``{green, amber, red, no_data, total}`` from standing.json rows, or None.

    None — not zeros — when there is no standing to count, so a dashboard shows
    "no data" rather than an empty, green-looking board.
    """
    if not isinstance(rows, list):
        return None
    counts = {"green": 0, "amber": 0, "red": 0, "no_data": 0}
    for row in rows:
        state = row.get("state") if isinstance(row, dict) else None
        key = "no_data" if state == "no-data" else state
        if key in counts:
            counts[key] += 1
    counts["total"] = len(rows)
    return counts


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def epoch_seconds(value):
    """``2026-10-04T12:00:00Z`` as whole Unix seconds, or None if unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def snapshot_fields(stage, path):
    """The allowed ``--snapshot`` fields for this stage, whole numbers only.

    A field the file does not carry is left out — never filled with zero — so a
    snapshot that could not be taken reads as "no data" on the dashboard.
    """
    data = _read_json(path) if path else None
    if not isinstance(data, dict):
        return {}
    out = {}
    for key in SNAPSHOT_FIELDS.get(stage, ()):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    return out


def receipt_record_counts(text):
    """``{added, changed, retracted}`` counts from a receipt's frontmatter.

    bundle.py writes each list on one line as a JSON-compatible flow list
    (``  added: ["2026-01-15-board-minutes"]``), so a line match is enough.
    """
    counts = {}
    for key in ("added", "changed", "retracted"):
        m = re.search(rf"^\s+{key}:\s*(\[.*\])\s*$", text, re.MULTILINE)
        try:
            counts[key] = len(json.loads(m.group(1))) if m else None
        except ValueError:
            counts[key] = None
    return counts


# --------------------------------------------------------------------------- #
# One payload builder per stage.                                               #
# --------------------------------------------------------------------------- #

def intake_payload(a):
    """Returns None for an issue that is not the "Add a record" form — it is not
    part of the pipeline, so it is not an event."""
    if a.scheduled:
        return {}
    if a.is_form != "true":
        return None
    if a.scaffold != "success":
        outcome = "form_unreadable"
    elif a.branch_exists == "true":
        outcome = "already_open"
    else:
        outcome = {"opened": "pr_opened", "exists": "pr_opened",
                   "blocked": "branch_pushed"}.get(a.pr_result, "failed")
    return {"issue": _int_or_none(a.issue), "outcome": outcome,
            "pr": _int_or_none(a.pr_number)}


def reviewed_payload(a):
    if a.scheduled:
        return {}
    return {"pr": _int_or_none(a.pr), "validate": a.validate or "skipped",
            "standing": standing_summary(_read_json(a.standing))}


def published_payload(a):
    out = Path(a.out_dir)
    corpora = sorted(out.glob("corpus-*.json"))
    bundle = _read_json(corpora[0]) if corpora else None
    receipts = sorted((out / "receipts").glob("*.md"))
    records = {"total": None, "added": None, "changed": None, "retracted": None}
    if isinstance(bundle, dict) and isinstance(bundle.get("records"), list):
        records["total"] = len(bundle["records"])
    if receipts:
        records.update(receipt_record_counts(receipts[0].read_text(encoding="utf-8")))
    return {
        "digest": bundle.get("digest") if isinstance(bundle, dict) else None,
        "published_at": bundle.get("published_at") if isinstance(bundle, dict) else None,
        "records": records,
        "standing": standing_summary(_read_json(out / "standing.json")),
        "receipt": receipts[0].name if receipts else None,
    }


def evals_payload(a):
    s = _read_json(a.summary) if a.summary else None
    s = s if isinstance(s, dict) else {}
    return {"corpus": a.corpus or "unknown", "mode": s.get("mode"),
            "total": s.get("total"), "passed": s.get("passed"),
            "failed": s.get("failed"), "pr": _int_or_none(a.pr)}


def rules_released_payload(a):
    return {"tag": a.tag or None}


BUILDERS = {
    "intake": intake_payload,
    "reviewed": reviewed_payload,
    "published": published_payload,
    "evals": evals_payload,
    "rules_released": rules_released_payload,
}


def build_payload(stage, args, env, now=None):
    """The stage payload plus the run's identity, or None for "no event"."""
    body = BUILDERS[stage](args)
    if body is None:
        return None
    body.update(snapshot_fields(stage, args.snapshot))
    if args.scheduled and not body:
        return None    # a snapshot run that gathered nothing has nothing to say
    if stage == "published":
        body["at"] = epoch_seconds(body.get("published_at"))
    else:
        body["at"] = int(time.time() if now is None else now)
    body["job_status"] = args.job_status or "unknown"
    body["run_id"] = env.get("GITHUB_RUN_ID") or None
    body["sha"] = env.get("GITHUB_SHA") or None
    return body


# --------------------------------------------------------------------------- #
# CLI.                                                                          #
# --------------------------------------------------------------------------- #

def _parser():
    p = argparse.ArgumentParser(description="Build one uvularia telemetry event for this run.")
    p.add_argument("stage", choices=STAGES)
    p.add_argument("--job-status", help="the job's status so far (success / failure)")
    p.add_argument("--snapshot", help="JSON file of counts to add (see SNAPSHOT_FIELDS)")
    p.add_argument("--scheduled", action="store_true",
                   help="the daily snapshot run: no single issue or pull request")
    # intake
    p.add_argument("--issue")
    p.add_argument("--is-form")
    p.add_argument("--scaffold")
    p.add_argument("--branch-exists")
    p.add_argument("--pr-result")
    p.add_argument("--pr-number")
    # reviewed
    p.add_argument("--standing", default="standing.json")
    p.add_argument("--validate")
    p.add_argument("--pr")
    # published
    p.add_argument("--out-dir", default="_out")
    # evals
    p.add_argument("--summary")
    p.add_argument("--corpus")
    # rules_released
    p.add_argument("--tag")
    return p


def _write_outputs(outputs, env):
    path = env.get("GITHUB_OUTPUT")
    lines = "".join(f"{k}={v}\n" for k, v in outputs.items())
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines)
    else:
        sys.stdout.write(lines)


def main(argv=None, env=None, now=None):
    env = os.environ if env is None else env
    args = _parser().parse_args(argv)
    enabled, message = config_state(env)
    outputs = {"enabled": "false",
               "cluster": cluster_slug(env.get("LOKI_CLUSTER"), env.get("GITHUB_REPOSITORY_OWNER")),
               "payload": "{}"}
    if not enabled:
        print(message)
        _write_outputs(outputs, env)
        return 0
    try:
        payload = build_payload(args.stage, args, env, now)
    except Exception as exc:  # noqa: BLE001 — telemetry never fails the job
        print(f"::warning title=telemetry::could not describe the {args.stage} stage ({exc}); no event sent")
        _write_outputs(outputs, env)
        return 0
    if payload is None:
        print(f"telemetry: nothing to report for this {args.stage} run; no event sent")
        _write_outputs(outputs, env)
        return 0
    outputs["enabled"] = "true"
    outputs["payload"] = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    print(f"telemetry: {args.stage} event for cluster {outputs['cluster']}: {outputs['payload']}")
    _write_outputs(outputs, env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
