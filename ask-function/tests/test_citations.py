"""The reply carries only the citations the engine's gate verified — never more."""

import json
import unittest

import support
from config import Config
from handler import Deps, handle
from policy import Policy


def _config():
    return Config(
        published_base="https://example.github.io/records", corpus_digest="latest",
        rules_repo="example/rules", rules_tag="latest", daily_cap=500,
        allowed_origin="*", cap_table="caps", ssm_key_path="/x/key",
        turnstile_enabled=False, turnstile_secret_ssm_path="", refresh_seconds=300,
        maintenance_message="Paused.", obligations_url="", aws_region="us-east-1",
    )


# A corpus of the ids that actually exist. mitchella's gate keeps a cited id only
# if corpus.by_id finds it; this mirrors that so the test is about the function's
# surface, not a re-test of the engine.
CORPUS_IDS = {"2026-01-01-bylaws", "2026-02-01-trails"}


def gated_answer(engine, question):
    """Model proposes ids; the gate keeps only the real ones, as mitchella does."""
    model_proposed = ["2026-02-01-trails", "2099-12-31-hallucinated"]
    verified = [sid for sid in model_proposed if sid in CORPUS_IDS]
    return support.FakeAnswer(
        "answered", "Trails are open dawn to dusk.",
        sources=[support.FakeSource(sid, sid, f"records/{sid}.md") for sid in verified],
        degraded=["uvularia-standing: no data for ma-oml-minutes-timely"],
    )


def _deps(answer_fn, logs):
    policy = Policy(enabled=True, model="claude-sonnet-5-5", daily_cap=None, disclaimer="")
    deployment = support.FakeDeployment(policy=policy, engine=object())
    return Deps(
        config=_config(),
        get_deployment=lambda now: deployment,
        cap_reserve=lambda d, c, n: (True, 1),
        verify_turnstile=lambda t, ip: True,
        answer_question=answer_fn,
        clock=lambda: 1000.0,
        log=logs.append,
    )


class CitationTest(unittest.TestCase):
    def test_only_verified_ids_are_returned(self):
        logs = []
        resp = handle(support.make_event(body={"question": "trails?"}),
                      _deps(gated_answer, logs))
        payload = json.loads(resp["body"])
        self.assertEqual(payload["source_ids"], ["2026-02-01-trails"])
        self.assertNotIn("2099-12-31-hallucinated", payload["source_ids"])

    def test_degraded_signals_are_surfaced(self):
        logs = []
        resp = handle(support.make_event(body={"question": "trails?"}),
                      _deps(gated_answer, logs))
        payload = json.loads(resp["body"])
        self.assertEqual(payload["degraded_signals"],
                         ["uvularia-standing: no data for ma-oml-minutes-timely"])

    def test_a_declined_answer_cites_nothing(self):
        logs = []
        declined = lambda e, q: support.FakeAnswer("declined", "That is out of scope.")
        resp = handle(support.make_event(body={"question": "legal advice?"}),
                      _deps(declined, logs))
        payload = json.loads(resp["body"])
        self.assertEqual(payload["kind"], "declined")
        self.assertEqual(payload["source_ids"], [])

    def test_the_log_truncates_the_question_to_500_chars(self):
        logs = []
        long_q = "x" * 900
        handle(support.make_event(body={"question": long_q}), _deps(gated_answer, logs))
        turn = next(r for r in logs if r["event"] == "turn")
        self.assertEqual(len(turn["question"]), 500)
        # No identifying fields in the log line.
        self.assertNotIn("sourceIp", turn)
        self.assertNotIn("origin", turn)


if __name__ == "__main__":
    unittest.main()
