"""Turnstile is off by default (the bypass) and fails closed when on."""

import json
import unittest

import support
from config import Config
from handler import Deps, handle
from policy import Policy
from turnstile import verify


class _Recorder:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def __call__(self, url, fields, timeout):
        self.calls += 1
        return self.reply


class TurnstileUnitTest(unittest.TestCase):
    def test_disabled_is_a_bypass_with_no_network(self):
        poster = _Recorder({"success": True})
        # No token, no secret, disabled -> allowed, and the verifier is never called.
        self.assertTrue(verify(enabled=False, token="", secret="", http_post=poster))
        self.assertEqual(poster.calls, 0)

    def test_enabled_needs_a_token(self):
        poster = _Recorder({"success": True})
        self.assertFalse(verify(enabled=True, token="", secret="sek", http_post=poster))
        self.assertEqual(poster.calls, 0)

    def test_enabled_checks_the_token_with_cloudflare(self):
        ok = _Recorder({"success": True})
        self.assertTrue(verify(enabled=True, token="tok", secret="sek", http_post=ok))
        self.assertEqual(ok.calls, 1)

        bad = _Recorder({"success": False})
        self.assertFalse(verify(enabled=True, token="tok", secret="sek", http_post=bad))

    def test_a_verifier_error_fails_closed(self):
        def boom(url, fields, timeout):
            raise OSError("cloudflare unreachable")

        self.assertFalse(verify(enabled=True, token="tok", secret="sek", http_post=boom))


def _config(**over):
    base = dict(
        published_base="https://example.github.io/records", corpus_digest="latest",
        rules_repo="example/rules", rules_tag="latest", daily_cap=500,
        allowed_origin="*", cap_table="caps", ssm_key_path="/x/key",
        turnstile_enabled=True, turnstile_secret_ssm_path="/x/ts", refresh_seconds=300,
        maintenance_message="Paused.", obligations_url="", aws_region="us-east-1",
    )
    base.update(over)
    return Config(**base)


class TurnstileHandlerTest(unittest.TestCase):
    """At the handler level, a failed check is a 403 before the engine is touched."""

    def _deps(self, verify_result, cfg):
        policy = Policy(enabled=True, model="claude-sonnet-5-5", daily_cap=None, disclaimer="")
        deployment = support.FakeDeployment(policy=policy, engine=object())
        self.answered = False

        def answer(engine, q):
            self.answered = True
            return support.FakeAnswer("answered", "hi")

        return Deps(
            config=cfg,
            get_deployment=lambda now: deployment,
            cap_reserve=lambda d, c, n: (True, 1),
            verify_turnstile=lambda t, ip: verify_result,
            answer_question=answer,
            clock=lambda: 1000.0,
            log=lambda r: None,
        )

    def test_failed_turnstile_is_403_and_skips_the_engine(self):
        deps = self._deps(verify_result=False, cfg=_config())
        resp = handle(support.make_event(body={"question": "trails?"}), deps)
        self.assertEqual(resp["statusCode"], 403)
        self.assertFalse(self.answered)

    def test_bypass_lets_the_question_through(self):
        deps = self._deps(verify_result=True, cfg=_config(turnstile_enabled=False))
        resp = handle(support.make_event(body={"question": "trails?"}), deps)
        self.assertEqual(resp["statusCode"], 200)
        self.assertTrue(self.answered)


if __name__ == "__main__":
    unittest.main()
