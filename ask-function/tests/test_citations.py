"""The reply carries only cited ids that are real records, and an ``answered``
reply with none of them left is never sent as an answer."""

import json
import unittest

import support
from config import Config
from handler import UNSOURCED_REPLY, Deps, handle
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
DEGRADED = ["uvularia-standing: no data for ma-oml-minutes-timely"]


def answer_citing(*model_proposed, kind="answered", text="Trails are open dawn to dusk."):
    """An engine stand-in: the model proposes ids, and the gate keeps only the
    ones in the corpus, as mitchella does."""
    def answer(engine, question):
        found = [sid for sid in model_proposed if sid in CORPUS_IDS]
        return support.FakeAnswer(
            kind, text,
            sources=[support.FakeSource(sid, sid, f"records/{sid}.md") for sid in found],
            degraded=DEGRADED,
        )
    return answer


gated_answer = answer_citing("2026-02-01-trails", "2099-12-31-hallucinated")


def _deps(answer_fn, logs, emitted=None):
    policy = Policy(enabled=True, model="claude-sonnet-5-5", daily_cap=None, disclaimer="")
    deployment = support.FakeDeployment(policy=policy, engine=object())
    emitted = [] if emitted is None else emitted
    return Deps(
        config=_config(),
        get_deployment=lambda now: deployment,
        cap_reserve=lambda d, c, n: (True, 1),
        verify_turnstile=lambda t, ip: True,
        answer_question=answer_fn,
        clock=lambda: 1000.0,
        log=logs.append,
        emit=lambda stage, payload: emitted.append((stage, payload)) or True,
    )


def _ask(answer_fn, question="are the trails open?"):
    logs, emitted = [], []
    resp = handle(support.make_event(body={"question": question}),
                  _deps(answer_fn, logs, emitted))
    turn = next(r for r in logs if r["event"] == "turn")
    asked = next(p for stage, p in emitted if stage == "asked")
    return resp, json.loads(resp["body"]), turn, asked


class CitationTest(unittest.TestCase):
    def test_only_ids_in_the_corpus_are_returned(self):
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
        self.assertEqual(payload["degraded_signals"], DEGRADED)

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


class AnAnswerNeedsASource(unittest.TestCase):
    """An ``answered`` reply must cite at least one real record (#117)."""

    def assert_fallback(self, resp, payload, turn, asked):
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(payload["kind"], "escalated")
        self.assertEqual(payload["reply"], UNSOURCED_REPLY)
        self.assertEqual(payload["source_ids"], [])
        # The model's unsourced text never reaches the reader.
        self.assertNotIn("dawn to dusk", resp["body"])
        # What still holds about the turn is still said.
        self.assertEqual(payload["degraded_signals"], DEGRADED)
        # Logged under its own name, like a capped or unconfigured turn.
        self.assertEqual(turn["kind"], "unsourced")
        self.assertEqual(asked["kind"], "unsourced")

    def test_answered_with_no_sources_falls_back(self):
        self.assert_fallback(*_ask(answer_citing()))

    def test_answered_citing_only_fabricated_ids_falls_back(self):
        self.assert_fallback(*_ask(answer_citing("2099-12-31-hallucinated",
                                                 "1999-01-01-also-invented")))

    def test_answered_with_one_real_source_is_unchanged(self):
        resp, payload, turn, asked = _ask(answer_citing("2026-02-01-trails"))
        self.assertEqual(payload, {
            "kind": "answered",
            "reply": "Trails are open dawn to dusk.",
            "source_ids": ["2026-02-01-trails"],
            "degraded_signals": DEGRADED,
        })
        self.assertEqual(turn["kind"], "answered")
        self.assertEqual(asked["kind"], "answered")

    def test_an_incident_needs_no_record_source(self):
        # An incident leads with live state (the board, an announcement), which
        # is not a record id. Only an ``answered`` reply is held to a source.
        incident = answer_citing(kind="incident", text="Minutes are overdue.")
        resp, payload, turn, asked = _ask(incident)
        self.assertEqual(payload["kind"], "incident")
        self.assertEqual(payload["reply"], "Minutes are overdue.")
        self.assertEqual(turn["kind"], "incident")

    def test_the_fallback_logs_the_question_like_any_other_turn(self):
        resp, payload, turn, asked = _ask(answer_citing(), question="q" * 900)
        self.assertEqual(len(turn["question"]), 500)
        self.assertEqual(len(asked["question"]), 500)


if __name__ == "__main__":
    unittest.main()
