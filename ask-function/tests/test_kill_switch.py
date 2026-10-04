"""`enabled: false` returns a maintenance line and spends nothing."""

import json
import unittest

import support
from config import Config
from handler import Deps, handle
from policy import Policy


def _config(**over):
    base = dict(
        published_base="https://example.github.io/records", corpus_digest="latest",
        rules_repo="example/rules", rules_tag="latest", daily_cap=500,
        allowed_origin="*", cap_table="caps", ssm_key_path="/x/key",
        turnstile_enabled=False, turnstile_secret_ssm_path="", refresh_seconds=300,
        maintenance_message="Paused for maintenance.", obligations_url="",
        aws_region="us-east-1",
    )
    base.update(over)
    return Config(**base)


class _Tripwire:
    """Fails the test if anything past the kill switch is touched."""

    def __init__(self, test):
        self.test = test
        self.cap_calls = 0
        self.turnstile_calls = 0
        self.answer_calls = 0

    def cap_reserve(self, day, cap, now):
        self.cap_calls += 1
        return True, 1

    def verify_turnstile(self, token, ip):
        self.turnstile_calls += 1
        return True

    def answer_question(self, engine, question):
        self.answer_calls += 1
        raise AssertionError("the engine must not be called while paused")


class KillSwitchTest(unittest.TestCase):
    def _deps(self, enabled, tripwire, logs):
        policy = Policy(enabled=enabled, model="claude-sonnet-5-5",
                        daily_cap=None, disclaimer="")
        deployment = support.FakeDeployment(policy=policy, engine=None)
        return Deps(
            config=_config(),
            get_deployment=lambda now: deployment,
            cap_reserve=tripwire.cap_reserve,
            verify_turnstile=tripwire.verify_turnstile,
            answer_question=tripwire.answer_question,
            clock=lambda: 1000.0,
            log=logs.append,
        )

    def test_paused_box_returns_maintenance_and_spends_nothing(self):
        tripwire, logs = _Tripwire(self), []
        deps = self._deps(enabled=False, tripwire=tripwire, logs=logs)
        resp = handle(support.make_event(body={"question": "are the trails open?"}), deps)

        self.assertEqual(resp["statusCode"], 200)
        payload = json.loads(resp["body"])
        self.assertEqual(payload["kind"], "maintenance")
        self.assertEqual(payload["reply"], "Paused for maintenance.")
        self.assertEqual(payload["source_ids"], [])
        # Nothing past the switch ran.
        self.assertEqual((tripwire.cap_calls, tripwire.turnstile_calls,
                          tripwire.answer_calls), (0, 0, 0))
        self.assertEqual(logs[0]["kind"], "maintenance")

    def test_live_box_reaches_the_engine(self):
        tripwire, logs = _Tripwire(self), []
        # Swap in an engine that answers so the live path is exercised.
        policy = Policy(enabled=True, model="claude-sonnet-5-5",
                        daily_cap=None, disclaimer="")
        deployment = support.FakeDeployment(policy=policy, engine=object())
        answered = support.FakeAnswer("answered", "Yes.", sources=[
            support.FakeSource("2026-01-01-trails", "Trails", "records/faq/trails.md")])
        deps = Deps(
            config=_config(),
            get_deployment=lambda now: deployment,
            cap_reserve=lambda d, c, n: (True, 1),
            verify_turnstile=lambda t, ip: True,
            answer_question=lambda e, q: answered,
            clock=lambda: 1000.0,
            log=logs.append,
        )
        resp = handle(support.make_event(body={"question": "trails?"}), deps)
        self.assertEqual(json.loads(resp["body"])["kind"], "answered")


if __name__ == "__main__":
    unittest.main()



class MissingKeyIsMaintenanceNotACrash(unittest.TestCase):
    """A live box whose API key has not been written yet answers with a 503
    maintenance-style reply and logs it; it never surfaces as a 500."""

    def test_unreadable_key_returns_503_maintenance(self):
        from secrets import SecretUnavailable

        def no_key(engine, question):
            raise SecretUnavailable("ParameterNotFound reading SSM parameter /x/key")

        logs = []
        policy = Policy(enabled=True, model="claude-sonnet-5-5", daily_cap=None, disclaimer="")
        deployment = support.FakeDeployment(policy=policy, engine=None)
        deps = Deps(
            config=_config(),
            get_deployment=lambda now: deployment,
            cap_reserve=lambda day, cap, now: (True, 1),
            verify_turnstile=lambda token, ip: True,
            answer_question=no_key,
            clock=lambda: 1000.0,
            log=logs.append,
        )
        resp = handle(support.make_event(body={"question": "When is the next meeting?"}), deps)
        self.assertEqual(resp["statusCode"], 503)
        payload = json.loads(resp["body"])
        self.assertEqual(payload["kind"], "maintenance")
        self.assertIn("not set up yet", payload["reply"])
        self.assertEqual(payload["source_ids"], [])
        self.assertEqual(logs[-1]["kind"], "unconfigured")
