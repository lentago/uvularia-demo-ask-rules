"""`GET /health` reports what the box serves and today's cap — and nothing else.

It must handle no question, claim no cap slot, call no model, carry the same
CORS headers as the POST, and never return the key, an SSM path, or anything
about the caller.
"""

import json
import unittest

import support
from cap import DailyCap
from handler import Deps, handle
from policy import Policy
from test_kill_switch import _config

DIGEST = "a" * 64
SITE = "https://example.github.io"


class _Tripwire:
    def __init__(self):
        self.reserved = 0

    def cap_reserve(self, day, cap, now):
        self.reserved += 1
        return True, 1

    def verify_turnstile(self, token, ip):
        raise AssertionError("/health must not run the bot check")

    def answer_question(self, engine, question):
        raise AssertionError("/health must not call the engine")


def _deps(*, enabled=True, daily_cap=None, used=37, tripwire=None, logs=None,
          get_deployment=None, **config):
    policy = Policy(enabled=enabled, model="claude-sonnet-5-5",
                    daily_cap=daily_cap, disclaimer="")
    deployment = support.FakeDeployment(policy=policy, digest=DIGEST if enabled else "",
                                        tag="rules-v7")
    tripwire = tripwire or _Tripwire()
    if callable(used):
        cap_used = used
    else:
        def cap_used(day):
            return used
    return Deps(
        config=_config(**config),
        get_deployment=get_deployment or (lambda now: deployment),
        cap_reserve=tripwire.cap_reserve,
        verify_turnstile=tripwire.verify_turnstile,
        answer_question=tripwire.answer_question,
        cap_used=cap_used,
        clock=lambda: 1_791_115_200.0,  # 2026-10-04T12:00:00Z
        log=(logs.append if logs is not None else lambda rec: None),
    )


def _get(deps, **kw):
    resp = handle(support.make_event(method="GET", path="/health", **kw), deps)
    return resp, json.loads(resp["body"])


class HealthTest(unittest.TestCase):
    def test_reports_digest_tag_enabled_and_cap(self):
        tripwire = _Tripwire()
        resp, body = _get(_deps(tripwire=tripwire))
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(body, {
            "status": "ok", "enabled": True, "digest": DIGEST, "digest_pinned": False,
            "rules_tag": "rules-v7", "day": "2026-10-04", "cap_used": 37, "cap": 500,
        })
        self.assertEqual(tripwire.reserved, 0, "a health check must not spend a cap slot")

    def test_policy_cap_wins_over_the_module_fallback(self):
        _, body = _get(_deps(daily_cap=40))
        self.assertEqual(body["cap"], 40)

    def test_paused_box_has_no_digest(self):
        _, body = _get(_deps(enabled=False))
        self.assertFalse(body["enabled"])
        self.assertIsNone(body["digest"])

    def test_explicit_digest_is_reported_as_pinned(self):
        _, body = _get(_deps(corpus_digest="b" * 64))
        self.assertTrue(body["digest_pinned"])

    def test_unreadable_counter_is_unknown_not_zero(self):
        def boom(day):
            raise RuntimeError("throttled")

        logs = []
        resp, body = _get(_deps(used=boom, logs=logs))
        self.assertEqual(resp["statusCode"], 200)
        self.assertIsNone(body["cap_used"])
        self.assertEqual(logs[-1]["status"], "cap_unreadable")

    def test_unloadable_deployment_is_a_503(self):
        def broken(now):
            raise OSError("rules release not found")

        resp, body = _get(_deps(get_deployment=broken))
        self.assertEqual(resp["statusCode"], 503)
        self.assertEqual(body["status"], "error")

    def test_cors_matches_the_post(self):
        deps = _deps(allowed_origin=SITE)
        health, _ = _get(deps, origin=SITE)
        post = handle(support.make_event(body={}, origin=SITE), deps)
        self.assertEqual(health["headers"], post["headers"])
        self.assertIn("GET", health["headers"]["Access-Control-Allow-Methods"])

    def test_wrong_origin_is_refused_as_for_the_post(self):
        resp, _ = _get(_deps(allowed_origin=SITE), origin="https://elsewhere.example")
        self.assertEqual(resp["statusCode"], 403)

    def test_post_to_health_is_not_a_question(self):
        resp = handle(support.make_event(method="POST", path="/health",
                                         body={"question": "hi"}), _deps())
        self.assertEqual(resp["statusCode"], 405)

    def test_get_elsewhere_is_still_refused(self):
        resp = handle(support.make_event(method="GET", path="/"), _deps())
        self.assertEqual(resp["statusCode"], 405)

    def test_never_carries_a_secret_or_identity(self):
        deps = _deps(ssm_key_path="/secret/anthropic-key",
                     turnstile_secret_ssm_path="/secret/turnstile")
        resp, body = _get(deps, origin=None)
        text = resp["body"]
        for leak in ("/secret/", "203.0.113.7", "sk-ant", "caps"):
            self.assertNotIn(leak, text)
        self.assertEqual(set(body), {"status", "enabled", "digest", "digest_pinned",
                                     "rules_tag", "day", "cap_used", "cap"})


class FakeDynamoRead:
    def __init__(self, item):
        self.item = item
        self.writes = 0

    def get_item(self, **kw):
        return {"Item": self.item} if self.item is not None else {}

    def update_item(self, **kw):
        self.writes += 1
        raise AssertionError("reading the cap must not write")


class CapUsedTest(unittest.TestCase):
    def test_reads_the_counter_without_writing(self):
        fake = FakeDynamoRead({"day": {"S": "2026-10-04"}, "count": {"N": "12"}})
        self.assertEqual(DailyCap("t", "us-east-1", client=fake).used("2026-10-04"), 12)
        self.assertEqual(fake.writes, 0)

    def test_a_day_with_no_row_is_zero(self):
        self.assertEqual(DailyCap("t", "us-east-1", client=FakeDynamoRead(None))
                         .used("2026-10-04"), 0)


if __name__ == "__main__":
    unittest.main()
