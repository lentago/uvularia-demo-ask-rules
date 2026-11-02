"""How much of a question is kept: ``question_log_chars``, at all four log sites.

The turn log (CloudWatch) and the ``asked`` event (Loki) keep the question cut
to ``question_log_chars``, never more than 500; 0 keeps none, and the field is
left out rather than sent empty. The subject is still matched on the whole
question. The setting arrives as ``UVULARIA_QUESTION_LOG_CHARS``, which the
Terraform module sets from ``var.question_log_chars``.
"""

import dataclasses
import json
import io
import os
import re
import unittest
from unittest import mock

import support
from config import Config
from handler import Deps, handle
from policy import Policy

TERRAFORM = support.SRC.parent / "terraform"
QUESTION = "Are the trails open? My name is Pat Example and I live on Elm Street."


def _config(**over):
    base = dict(
        published_base="https://example.github.io/records", corpus_digest="latest",
        rules_repo="example/rules", rules_tag="latest", daily_cap=10,
        allowed_origin="*", cap_table="caps", ssm_key_path="/x/key",
        turnstile_enabled=False, turnstile_secret_ssm_path="", refresh_seconds=300,
        maintenance_message="Paused.", obligations_url="", aws_region="us-east-1",
    )
    base.update(over)
    return Config(**base)


def _answered(engine, question):
    return support.FakeAnswer(
        "answered", "Yes.", sources=[support.FakeSource("2026-02-01-trails", "Trails", "p")])


def _no_key(engine, question):
    from secrets import SecretUnavailable
    raise SecretUnavailable("ParameterNotFound reading SSM parameter /x/key")


def _turn(site, chars):
    """Run one turn down one of the four log sites; return (log line, event)."""
    logs, emitted = [], []
    policy = Policy(enabled=(site != "maintenance"), model="claude-sonnet-5-5",
                    daily_cap=None, disclaimer="", allowed_subjects=("trails",))
    deployment = support.FakeDeployment(policy=policy)
    deps = Deps(
        config=_config(question_log_chars=chars),
        get_deployment=lambda now: deployment,
        cap_reserve=lambda day, cap, now: (site != "capped", 1),
        verify_turnstile=lambda token, ip: True,
        answer_question=_no_key if site == "unconfigured" else _answered,
        clock=lambda: 1000.0,
        log=logs.append,
        emit=lambda stage, payload: emitted.append((stage, payload)) or True,
    )
    handle(support.make_event(body={"question": QUESTION}), deps)
    turn = next(r for r in logs if r["event"] == "turn")
    asked = next(p for stage, p in emitted if stage == "asked")
    return turn, asked


SITES = ("maintenance", "capped", "unconfigured", "answered")


class QuestionLogLength(unittest.TestCase):
    def test_default_keeps_500(self):
        self.assertEqual(_config().question_log_chars, 500)

    def test_a_shorter_limit_applies_at_every_site(self):
        for site in SITES:
            with self.subTest(site=site):
                turn, asked = _turn(site, 12)
                self.assertEqual(turn["kind"], site)
                self.assertEqual(turn["question"], QUESTION[:12])
                self.assertEqual(asked["question"], QUESTION[:12])

    def test_zero_keeps_no_question_text_at_any_site(self):
        for site in SITES:
            with self.subTest(site=site):
                turn, asked = _turn(site, 0)
                self.assertNotIn("question", turn)
                self.assertNotIn("question", asked)
                self.assertNotIn("Pat Example", json.dumps(turn) + json.dumps(asked))
                # The rest of the turn is still reported.
                self.assertEqual(turn["kind"], site)
                self.assertEqual(asked["kind"], site)

    def test_zero_still_reports_the_subject(self):
        turn, asked = _turn("answered", 0)
        self.assertEqual(asked["subject"], "trails")

    def test_a_negative_value_keeps_none(self):
        turn, asked = _turn("answered", -5)
        self.assertNotIn("question", turn)
        self.assertNotIn("question", asked)

    def test_500_is_the_ceiling(self):
        logs = []
        deps = Deps(
            config=_config(question_log_chars=5000),
            get_deployment=lambda now: support.FakeDeployment(
                policy=Policy(enabled=True, model="m", daily_cap=None, disclaimer="")),
            cap_reserve=lambda day, cap, now: (True, 1),
            verify_turnstile=lambda token, ip: True,
            answer_question=_answered,
            clock=lambda: 1000.0,
            log=logs.append,
        )
        handle(support.make_event(body={"question": "x" * 900}), deps)
        self.assertEqual(len(next(r for r in logs if r["event"] == "turn")["question"]), 500)


class QuestionLogConfig(unittest.TestCase):
    def _from_env(self, **env):
        with mock.patch.dict(os.environ, env, clear=True):
            return Config.from_env()

    def test_unset_is_500(self):
        self.assertEqual(self._from_env().question_log_chars, 500)

    def test_read_from_the_environment(self):
        self.assertEqual(self._from_env(UVULARIA_QUESTION_LOG_CHARS="0").question_log_chars, 0)
        self.assertEqual(self._from_env(UVULARIA_QUESTION_LOG_CHARS="80").question_log_chars, 80)

    def test_a_value_that_will_not_parse_keeps_none_and_says_so(self):
        # A privacy setting fails closed: "off" or a typo must not restore 500.
        for raw in ("off", "zero", "5o"):
            with self.subTest(raw=raw), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(self._from_env(UVULARIA_QUESTION_LOG_CHARS=raw).question_log_chars, 0)
                self.assertIn("UVULARIA_QUESTION_LOG_CHARS", err.getvalue())

    def test_the_field_has_a_default_so_older_callers_still_build(self):
        field = {f.name: f for f in dataclasses.fields(Config)}["question_log_chars"]
        self.assertEqual(field.default, 500)

    @unittest.skipUnless((TERRAFORM / "main.tf").is_file(), "no terraform module beside src/")
    def test_terraform_sets_the_variable_the_function_reads(self):
        # Only live code counts: a commented-out setting must not pass, whether it
        # is a `/* … */` block or a `#` / `//` line.
        text = re.sub(r"/\*.*?\*/", "", (TERRAFORM / "main.tf").read_text(encoding="utf-8"), flags=re.S)
        main = "\n".join(line for line in text.splitlines()
                         if not line.lstrip().startswith(("#", "//")))
        variables = (TERRAFORM / "variables.tf").read_text(encoding="utf-8")
        self.assertRegex(
            main, r"UVULARIA_QUESTION_LOG_CHARS\s*=\s*tostring\(var\.question_log_chars\)")
        block = re.search(r'variable "question_log_chars" \{.*?\n\}', variables, re.S)
        self.assertIsNotNone(block, "variables.tf declares question_log_chars")
        self.assertIn("default     = 500", block.group(0))


if __name__ == "__main__":
    unittest.main()
