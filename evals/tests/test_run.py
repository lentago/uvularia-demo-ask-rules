"""Tests for the eval harness (evals/run.py). Stdlib only, offline.

Why here and not in core/tests: the Ask box's rules are a *client* of the core
(CLAUDE.md invariant 7 — the core owns schemas and formats; Ask runtimes and
their governance are clients). The harness lives in this template, so its test
lives beside it, and it never touches the core.

Every check the dry gate makes is proved to *fail* on a crafted golden fixture,
so "the golden set is green" means the gate can also go red — including the
deliberately-wrong expected outcome the issue asks us to catch. The network is
never touched: all fixtures are local and name-free (no demonstration-client
identity appears under templates/).

    python3 -m unittest discover -s templates/ask-rules/evals/tests -p "test_*.py"
"""

import sys
import unittest
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parents[1]      # templates/ask-rules/evals
FIXTURES = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(EVALS_DIR))

import run  # noqa: E402


def load(name):
    return run.load_golden(FIXTURES / name)


class DryChecks(unittest.TestCase):
    def setUp(self):
        self.bundle = run.load_bundle(str(FIXTURES / "corpus-latest.json"))

    def test_good_golden_passes(self):
        errors = run.check_dry(load("golden.good.jsonl"), self.bundle)
        self.assertEqual(errors, [], f"expected a clean golden set, got: {errors}")

    def test_missing_source_id_fails(self):
        errors = run.check_dry(load("golden.bad-missing-source.jsonl"), self.bundle)
        self.assertTrue(any("not in the published corpus" in e for e in errors), errors)

    def test_wrong_expected_outcome_fails(self):
        # expect=declined but the entry cites a real record the corpus answers from.
        errors = run.check_dry(load("golden.bad-outcome.jsonl"), self.bundle)
        self.assertTrue(any("can answer it" in e for e in errors), errors)

    def test_unknown_subject_fails(self):
        errors = run.check_dry(load("golden.bad-subject.jsonl"), self.bundle)
        self.assertTrue(any("maps to no published record" in e for e in errors), errors)

    def test_answered_without_sources_fails(self):
        entries = [{"id": "x", "question": "q", "expect": "answered", "source_ids": [], "subjects": []}]
        errors = run.check_dry(entries, self.bundle)
        self.assertTrue(any("lists no source_ids" in e for e in errors), errors)

    def test_bad_outcome_kind_fails(self):
        entries = [{"id": "x", "question": "q", "expect": "maybe", "source_ids": [], "subjects": []}]
        errors = run.check_dry(entries, self.bundle)
        self.assertTrue(any("'expect' must be one of" in e for e in errors), errors)


class SubjectMapping(unittest.TestCase):
    def setUp(self):
        self.bundle = run.load_bundle(str(FIXTURES / "corpus-latest.json"))
        self.haystacks = run.corpus_index(self.bundle)[1]

    def test_tag_match(self):
        self.assertTrue(run.subject_maps("bylaw", self.haystacks))

    def test_id_or_title_substring_match(self):
        self.assertTrue(run.subject_maps("trails", self.haystacks))

    def test_unknown_subject(self):
        self.assertFalse(run.subject_maps("helicopter", self.haystacks))


class Plumbing(unittest.TestCase):
    def test_resolve_dir_appends_corpus_latest(self):
        loc = run.resolve_bundle_location(str(FIXTURES))
        self.assertTrue(loc.endswith("corpus-latest.json"))

    def test_resolve_from_policy_published_base(self):
        loc = run.resolve_bundle_location(None)
        self.assertTrue(loc.endswith("/corpus-latest.json"))
        self.assertTrue(loc.startswith("http"))

    def test_scan_scalar_reads_nested_and_quoted(self):
        text = "model: claude-sonnet-5-5\neval:\n  threshold: 0.8\n"
        self.assertEqual(run._scan_scalar(text, "model"), "claude-sonnet-5-5")
        self.assertEqual(run._scan_scalar(text, "threshold"), "0.8")

    def test_main_dry_against_good_fixture_returns_zero(self):
        code = run.main([
            "--mode", "dry",
            "--golden", str(FIXTURES / "golden.good.jsonl"),
            "--bundle", str(FIXTURES / "corpus-latest.json"),
        ])
        self.assertEqual(code, 0)

    def test_main_dry_against_bad_fixture_returns_one(self):
        code = run.main([
            "--mode", "dry",
            "--golden", str(FIXTURES / "golden.bad-outcome.jsonl"),
            "--bundle", str(FIXTURES / "corpus-latest.json"),
        ])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()


class MissingCorpusIsANoticeNotAFailure(unittest.TestCase):
    """A fresh repo has no published corpus yet; the harness must say so with exit
    code 3 (distinct from a failing eval) so the workflow can treat it as a notice."""

    def test_unreadable_bundle_exits_3(self):
        with self.assertRaises(SystemExit) as cm:
            run.load_bundle("/nonexistent/corpus-latest.json")
        self.assertEqual(cm.exception.code, 3)


class SummaryCountsForTelemetry(unittest.TestCase):
    """--summary-json reports pass/fail counts per golden entry, and only counts."""

    def _run(self, golden):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "summary.json"
            rc = run.main(["--mode", "dry", "--golden", str(FIXTURES / golden),
                           "--bundle", str(FIXTURES / "corpus-latest.json"),
                           "--summary-json", str(out)])
            return rc, json.loads(out.read_text())

    def test_clean_set_counts_every_entry_passed(self):
        rc, summary = self._run("golden.good.jsonl")
        self.assertEqual(rc, 0)
        self.assertEqual(summary["mode"], "dry")
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["passed"], summary["total"])
        self.assertEqual(set(summary), {"mode", "total", "passed", "failed"})

    def test_bad_set_counts_the_failing_entries(self):
        rc, summary = self._run("golden.bad-outcome.jsonl")
        self.assertEqual(rc, 1)
        self.assertGreater(summary["failed"], 0)
        self.assertEqual(summary["passed"] + summary["failed"], summary["total"])
