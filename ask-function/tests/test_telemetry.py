"""Pipeline events: the right fields, no identity, a no-op when unset, never fatal.

No network beyond a loopback mock Loki, no key, no SSM, no mitchella.
"""

import http.server
import json
import threading
import unittest
import urllib.error

import support
from config import Config
from engine_build import _STATE, HEARTBEAT_SECONDS, get_deployment
from handler import Deps, handle
from policy import Policy, parse_policy
from telemetry import (ASKED_FIELDS, Pusher, asked_payload, cluster_slug, first_subject,
                       make_pusher, served_payload)

def _rules_policy():
    """The rules' policy.yaml: beside the vendored ``ask-function/`` in a rules
    repo, or the template's copy in the uvularia source tree. None if neither
    layout holds one (the test that reads it then skips rather than guesses)."""
    for candidate in (support.SRC.parents[1] / "policy.yaml",
                      support.SRC.parents[1] / "ask-rules" / "policy.yaml"):
        if candidate.is_file():
            return candidate
    return None


RULES_POLICY = _rules_policy()

# The fields issues #52 and #59 name for an `asked` event — and nothing else.
SPEC_ASKED = {"at", "kind", "latency_ms", "cap_used", "cap_remaining", "digest", "subject",
              "degraded_signals", "question"}
IDENTITY_KEYS = {"ip", "source_ip", "sourceIp", "origin", "remote_ip", "user", "identity",
                 "token", "turnstile_token", "headers", "user_agent"}
CALLER_IP = "203.0.113.7"   # support.make_event's sourceIp
CALLER_ORIGIN = "https://caller.example"


def _config(**over):
    base = dict(
        published_base="https://example.github.io/records", corpus_digest="latest",
        rules_repo="Example-Org/example-rules", rules_tag="latest", daily_cap=10,
        allowed_origin="*", cap_table="caps", ssm_key_path="/x/key",
        turnstile_enabled=False, turnstile_secret_ssm_path="", refresh_seconds=300,
        maintenance_message="Paused.", obligations_url="", aws_region="us-east-1",
    )
    base.update(over)
    return Config(**base)


def _deps(emitted, *, enabled=True, emit=None, answer=None, subjects=("trails", "meetings")):
    policy = Policy(enabled=enabled, model="claude-sonnet-5-5", daily_cap=None, disclaimer="",
                    allowed_subjects=subjects)
    deployment = support.FakeDeployment(policy=policy)
    answer = answer or support.FakeAnswer(
        "answered", "Yes.", sources=[support.FakeSource("2026-01-01-trails", "Trails", "p")],
        degraded=["standing"])
    ticks = iter([1000.0, 1002.5, 1002.5, 1002.5])
    return Deps(
        config=_config(),
        get_deployment=lambda now: deployment,
        cap_reserve=lambda day, cap, now: (True, 3),
        verify_turnstile=lambda token, ip: True,
        answer_question=lambda engine, q: answer,
        clock=lambda: next(ticks),
        log=lambda rec: None,
        emit=emit or (lambda stage, payload: emitted.append((stage, payload)) or True),
    )


def _payload(**over):
    base = dict(at=1000.9, kind="answered", latency_ms=12, cap_used=1, cap_remaining=9,
                digest="d" * 64, subject="trails", degraded_signals=("feed",), question="q")
    base.update(over)
    return asked_payload(**base)


class AskedEventBuilder(unittest.TestCase):
    def test_fields_are_exactly_the_issues_list(self):
        self.assertEqual(set(ASKED_FIELDS), SPEC_ASKED)
        p = _payload()
        self.assertEqual(set(p), SPEC_ASKED)
        self.assertEqual(p["degraded_signals"], ["feed"])

    def test_at_is_whole_epoch_seconds(self):
        self.assertEqual(_payload()["at"], 1000)
        self.assertIsInstance(_payload()["at"], int)

    def test_unknown_subject_is_left_out_not_guessed(self):
        p = _payload(subject=None)
        self.assertNotIn("subject", p)
        self.assertEqual(set(p), SPEC_ASKED - {"subject"})

    def test_question_is_truncated_to_500(self):
        self.assertEqual(len(_payload(question="x" * 2000)["question"]), 500)

    def test_builder_refuses_anything_extra(self):
        with self.assertRaises(TypeError):
            _payload(origin=CALLER_ORIGIN)


class FirstSubject(unittest.TestCase):
    SUBJECTS = ("meetings", "minutes", "trails")

    def test_earliest_mention_in_the_question_wins(self):
        self.assertEqual(first_subject("Are the TRAILS open after the meetings?", self.SUBJECTS),
                         "trails")

    def test_a_tie_goes_to_the_policy_order(self):
        self.assertEqual(first_subject("minutes", ("minutes", "minute")), "minutes")
        self.assertEqual(first_subject("minutes", ("minute", "minutes")), "minute")

    def test_a_singular_question_matches_a_plural_subject(self):
        self.assertEqual(first_subject("Was the 2026 Form PC filing posted, and when?",
                                       ("filings",)), "filings")
        self.assertEqual(first_subject("Where is the trail map?", self.SUBJECTS), "trails")

    def test_a_plural_question_matches_a_singular_subject(self):
        self.assertEqual(first_subject("Show me the notices", ("notice",)), "notice")

    def test_y_and_ies_go_both_ways(self):
        self.assertEqual(first_subject("What is the privacy policy?", ("policies",)), "policies")
        self.assertEqual(first_subject("Which policies changed?", ("policy",)), "policy")

    def test_es_is_stripped_and_added(self):
        self.assertEqual(first_subject("Is the easement recorded?", ("easements",)), "easements")
        self.assertEqual(first_subject("Any new bylaw?", ("bylaws",)), "bylaws")
        self.assertEqual(first_subject("List the boxes", ("box",)), "box")

    def test_the_subject_is_returned_as_listed_not_inflected(self):
        self.assertEqual(first_subject("the trail", ("Trails",)), "Trails")

    def test_an_inflected_form_inside_a_longer_word_does_not_match(self):
        self.assertEqual(first_subject("Is the trailer parked?", ("trails",)), "unmatched")
        self.assertEqual(first_subject("Any reporting?", ("reports",)), "unmatched")

    def test_earliest_wins_across_forms(self):
        self.assertEqual(first_subject("Is the trail near the meetings?", self.SUBJECTS), "trails")

    def test_no_match_is_the_literal_none(self):
        self.assertEqual(first_subject("Where do I park?", self.SUBJECTS), "unmatched")
        self.assertEqual(first_subject("", self.SUBJECTS), "unmatched")

    def test_no_subject_list_is_unknown(self):
        self.assertIsNone(first_subject("trails", ()))
        self.assertIsNone(first_subject("trails", None))

    def test_the_shipped_policy_lists_subjects(self):
        if RULES_POLICY is None:
            self.skipTest("no policy.yaml beside this ask-function")
        policy = parse_policy(RULES_POLICY.read_text(encoding="utf-8"))
        self.assertIn("trails", policy.allowed_subjects)
        self.assertEqual(first_subject("When were the bylaws last changed?",
                                       policy.allowed_subjects), "bylaws")

    def test_policy_list_forms(self):
        block = "enabled: true\nallowed_subjects:\n  - Trails  # comment\n\n  - 'meetings'\nmodel: m\n"
        self.assertEqual(parse_policy(block).allowed_subjects, ("Trails", "meetings"))
        flow = "allowed_subjects: [trails, \"meetings\"]\n"
        self.assertEqual(parse_policy(flow).allowed_subjects, ("trails", "meetings"))
        self.assertEqual(parse_policy("enabled: true\n").allowed_subjects, ())


class HandlerEmitsOneAskedEventPerTurn(unittest.TestCase):
    def _ask(self, deps, question="are the trails open?"):
        event = support.make_event(body={"question": question, "turnstile_token": "secret-tok"},
                                   origin=CALLER_ORIGIN)
        return handle(event, deps)

    def test_answered_turn(self):
        emitted = []
        resp = self._ask(_deps(emitted), question="q" * 900)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(len(emitted), 1)
        stage, payload = emitted[0]
        self.assertEqual(stage, "asked")
        self.assertEqual(set(payload), SPEC_ASKED)
        self.assertEqual(payload["kind"], "answered")
        self.assertEqual(payload["latency_ms"], 2500)
        self.assertEqual((payload["cap_used"], payload["cap_remaining"]), (3, 7))
        self.assertEqual(payload["degraded_signals"], ["standing"])
        self.assertEqual(len(payload["question"]), 500)
        self.assertEqual(payload["at"], 1000)
        self.assertEqual(payload["digest"], "deadbeef")
        self.assertEqual(payload["subject"], "unmatched")

    def test_subject_is_the_first_allowed_subject_never_the_question(self):
        emitted = []
        self._ask(_deps(emitted), question="Are the meetings or the trails open on Sunday?")
        _, payload = emitted[0]
        self.assertEqual(payload["subject"], "meetings")
        emitted = []
        self._ask(_deps(emitted, subjects=()), question="Are the trails open?")
        self.assertNotIn("subject", emitted[0][1])

    def test_no_identity_anywhere_in_the_event(self):
        emitted = []
        self._ask(_deps(emitted))
        _, payload = emitted[0]
        self.assertFalse(IDENTITY_KEYS & set(payload))
        line = json.dumps(payload)
        for leak in (CALLER_IP, CALLER_ORIGIN, "secret-tok"):
            self.assertNotIn(leak, line)

    def test_paused_box_still_reports_the_turn(self):
        emitted = []
        self._ask(_deps(emitted, enabled=False))
        self.assertEqual([(s, p["kind"]) for s, p in emitted], [("asked", "maintenance")])
        self.assertEqual((emitted[0][1]["at"], emitted[0][1]["subject"]), (1000, "trails"))

    def test_a_failing_pusher_never_changes_the_answer(self):
        def outage(*a, **kw):
            raise urllib.error.URLError("loki is down")
        warnings = []
        pusher = Pusher("https://logs.example", "org", "org/rules", lambda: "1:tok",
                        push=outage, log=warnings.append)
        resp = self._ask(_deps([], emit=pusher.emit))
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"])["kind"], "answered")
        self.assertEqual(warnings[0]["event"], "telemetry_warning")


class NoOpWithoutConfiguration(unittest.TestCase):
    def test_unset_url_sends_nothing_and_reads_no_secret(self):
        reads, pushes = [], []
        pusher = make_pusher(_config(), lambda path, region: reads.append(path) or "1:t",
                             push=lambda *a, **kw: pushes.append(a))
        self.assertFalse(pusher.enabled)
        self.assertFalse(pusher.emit("asked", {"kind": "answered"}))
        self.assertFalse(pusher.emit("served", {}))
        self.assertEqual((reads, pushes), ([], []))

    def test_default_deps_emit_is_a_no_op(self):
        policy = Policy(enabled=False, model="m", daily_cap=None, disclaimer="")
        deps = Deps(config=_config(), get_deployment=lambda now: support.FakeDeployment(policy),
                    cap_reserve=None, verify_turnstile=None, answer_question=None,
                    clock=lambda: 1.0, log=lambda rec: None)
        resp = handle(support.make_event(body={"question": "q"}), deps)
        self.assertEqual(resp["statusCode"], 200)

    def test_unreadable_token_warns_once_then_stops_trying(self):
        reads, warnings = [], []

        def no_token():
            reads.append(1)
            raise RuntimeError("ParameterNotFound")
        pusher = Pusher("https://logs.example", "org", "org/rules", no_token,
                        push=lambda *a, **kw: 204, log=warnings.append)
        self.assertFalse(pusher.emit("asked", {}))
        self.assertFalse(pusher.emit("asked", {}))
        self.assertEqual((len(reads), len(warnings)), (1, 1))
        self.assertFalse(pusher.enabled)


class ServedOnRefresh(unittest.TestCase):
    POLICY = "enabled: false\nmodel: claude-sonnet-5-5\n"

    def setUp(self):
        _STATE.update(deployment=None, reported_at=None)

    def tearDown(self):
        _STATE.update(deployment=None, reported_at=None)

    def _calls(self, times, *, refresh_seconds=300, **kw):
        cfg = _config(rules_tag="rules-v4", refresh_seconds=refresh_seconds)
        seen, builds = [], []

        def http_get(url, **_):
            builds.append(url)
            return self.POLICY
        for now in times:
            get_deployment(cfg, None, now=now, http_get=http_get,
                           on_refresh=lambda d, now=now: seen.append(served_payload(d, at=now)),
                           **kw)
        return seen, builds

    def test_served_fires_once_per_fresh_deployment(self):
        seen, _ = self._calls((1000.0, 1100.0, 1400.0))     # build, cached, rebuilt
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], {"at": 1000, "digest": None, "rules_tag": "rules-v4",
                                   "enabled": False})
        self.assertEqual(seen[1]["at"], 1400)

    def test_a_cached_box_still_reports_once_the_heartbeat_is_due(self):
        # A long refresh window: only the first call rebuilds, but a call that
        # finds the last report HEARTBEAT_SECONDS old reports the cached one.
        t0 = 1000.0
        times = (t0, t0 + 60, t0 + HEARTBEAT_SECONDS, t0 + HEARTBEAT_SECONDS + 60,
                 t0 + 2 * HEARTBEAT_SECONDS)
        seen, builds = self._calls(times, refresh_seconds=3600)
        self.assertEqual(len(builds), 1)
        self.assertEqual([p["at"] for p in seen],
                         [1000, 1000 + HEARTBEAT_SECONDS, 1000 + 2 * HEARTBEAT_SECONDS])

    def test_heartbeat_is_under_the_fifteen_minute_ping(self):
        self.assertLess(HEARTBEAT_SECONDS, 15 * 60)

    def test_can_fail_without_a_heartbeat_a_quiet_cached_box_says_nothing(self):
        seen, _ = self._calls((1000.0, 1000.0 + HEARTBEAT_SECONDS), refresh_seconds=3600,
                              heartbeat_seconds=float("inf"))
        self.assertEqual(len(seen), 1, "the mutation should silence the second report")

    def test_a_health_ping_reports_served_and_claims_no_cap_slot(self):
        cfg = _config(rules_tag="rules-v4", refresh_seconds=3600)
        emitted, reserved = [], []
        clock = iter([1000.0, 1000.0 + HEARTBEAT_SECONDS])

        def deployment(now):
            return get_deployment(cfg, None, now=now, http_get=lambda url, **_: self.POLICY,
                                  on_refresh=lambda d: emitted.append(
                                      ("served", served_payload(d, at=now))))
        deps = Deps(config=cfg, get_deployment=deployment,
                    cap_reserve=lambda *a: reserved.append(a) or (True, 1),
                    verify_turnstile=None, answer_question=None, cap_used=lambda day: 4,
                    clock=lambda: next(clock), log=lambda rec: None)
        for _ in range(2):
            resp = handle(support.make_event(method="GET", path="/health"), deps)
            self.assertEqual(resp["statusCode"], 200)
        self.assertEqual([(s, p["at"], p["rules_tag"]) for s, p in emitted],
                         [("served", 1000, "rules-v4"),
                          ("served", 1000 + HEARTBEAT_SECONDS, "rules-v4")])
        self.assertEqual(reserved, [])


class _MockLoki(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((self.path, self.headers.get("Authorization"), json.loads(body)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


class WireFormatThroughTheVendoredClient(unittest.TestCase):
    def test_labels_and_line(self):
        _MockLoki.received = []
        server = http.server.HTTPServer(("127.0.0.1", 0), _MockLoki)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            cfg = _config(loki_push_url=f"http://127.0.0.1:{server.server_port}",
                          loki_token_ssm_path="/x/loki")
            pusher = make_pusher(cfg, lambda path, region: "123:glc_tok")
            payload = _payload()
            self.assertTrue(pusher.emit("asked", payload))
        finally:
            server.shutdown()
            server.server_close()
        path, auth, body = _MockLoki.received[0]
        self.assertEqual(path, "/loki/api/v1/push")
        self.assertTrue(auth.startswith("Basic "))
        stream = body["streams"][0]
        self.assertEqual(stream["stream"], {
            "log_source": "uvularia_asked", "cluster": "example-org", "source": "uvularia",
            "pipeline": "ask", "stage": "asked", "repo": "Example-Org/example-rules"})
        self.assertEqual(json.loads(stream["values"][0][1]), payload)

    def test_cluster_slug(self):
        self.assertEqual(cluster_slug("", "Example-Org/rules"), "example-org")
        self.assertEqual(cluster_slug("My Org", "x/y"), "my-org")


if __name__ == "__main__":
    unittest.main()
