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
from engine_build import _STATE, get_deployment
from handler import Deps, handle
from policy import Policy
from telemetry import ASKED_FIELDS, Pusher, asked_payload, cluster_slug, make_pusher, served_payload

# The fields issue #52 names for an `asked` event — and nothing else.
SPEC_ASKED = {"kind", "latency_ms", "cap_used", "cap_remaining", "degraded_signals", "question"}
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


def _deps(emitted, *, enabled=True, emit=None, answer=None):
    policy = Policy(enabled=enabled, model="claude-sonnet-5-5", daily_cap=None, disclaimer="")
    deployment = support.FakeDeployment(policy=policy)
    answer = answer or support.FakeAnswer(
        "answered", "Yes.", sources=[support.FakeSource("2026-01-01-trails", "Trails", "p")],
        degraded=["standing"])
    ticks = iter([1000.0, 1000.25, 1000.25, 1000.25])
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


class AskedEventBuilder(unittest.TestCase):
    def test_fields_are_exactly_the_issues_list(self):
        self.assertEqual(set(ASKED_FIELDS), SPEC_ASKED)
        p = asked_payload(kind="answered", latency_ms=12, cap_used=1, cap_remaining=9,
                          degraded_signals=("feed",), question="q")
        self.assertEqual(set(p), SPEC_ASKED)
        self.assertEqual(p["degraded_signals"], ["feed"])

    def test_question_is_truncated_to_500(self):
        p = asked_payload(kind="answered", latency_ms=0, cap_used=1, cap_remaining=0,
                          degraded_signals=(), question="x" * 2000)
        self.assertEqual(len(p["question"]), 500)

    def test_builder_refuses_anything_extra(self):
        with self.assertRaises(TypeError):
            asked_payload(kind="answered", latency_ms=0, cap_used=1, cap_remaining=0,
                          degraded_signals=(), question="q", origin=CALLER_ORIGIN)


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
        self.assertEqual(payload["latency_ms"], 250)
        self.assertEqual((payload["cap_used"], payload["cap_remaining"]), (3, 7))
        self.assertEqual(payload["degraded_signals"], ["standing"])
        self.assertEqual(len(payload["question"]), 500)

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
        _STATE["deployment"] = None

    def tearDown(self):
        _STATE["deployment"] = None

    def test_served_fires_once_per_fresh_deployment(self):
        cfg = _config(rules_tag="rules-v4", refresh_seconds=300)
        seen = []
        http_get = lambda url, **kw: self.POLICY  # noqa: E731
        for now in (1000.0, 1100.0, 1400.0):     # build, cached, rebuilt
            get_deployment(cfg, None, now=now, http_get=http_get,
                           on_refresh=lambda d: seen.append(served_payload(d)))
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], {"digest": None, "rules_tag": "rules-v4", "enabled": False})


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
            payload = asked_payload(kind="answered", latency_ms=5, cap_used=1,
                                    cap_remaining=9, degraded_signals=(), question="q")
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
