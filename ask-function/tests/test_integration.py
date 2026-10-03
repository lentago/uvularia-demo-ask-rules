"""Build the engine over the live demonstration vault — real published fixtures.

This is the only test that reaches the network and the only one that needs
mitchella installed, so it skips cleanly when either is absent (the unit tests
remain the gate — the repo's skip-with-a-notice precedent, never a fake pass).
It reads the demonstration vault's published ``corpus-latest.json``,
``standing.json``, and ``feed.xml`` — the fixtures named in issue #34 — loads them
exactly as the Lambda would, and wires mitchella's providers over them. It does
not call the model (no key), so it costs nothing: it proves the artifacts load,
the corpus is non-empty, and the signal plane reads standing and announcements
without faulting.

Override the source with ``UVULARIA_PUBLISHED_BASE`` (an http(s) or file:// base,
or a local directory) to run it against a fixture of your own.
"""

import os
import unittest
import urllib.error

import support  # noqa: F401 — sets up sys.path

DEMO_BASE = "https://lentago.github.io/uvularia-demo-records"

try:
    import mitchella  # noqa: F401
    from mitchella import (
        AnnouncementProvider,
        Config,
        Engine,
        SignalPlane,
        StandingProvider,
        load_corpus,
    )
    HAVE_MITCHELLA = True
except ImportError:
    HAVE_MITCHELLA = False


@unittest.skipUnless(HAVE_MITCHELLA, "mitchella is not installed (live mode only)")
class DemoIntegrationTest(unittest.TestCase):
    def setUp(self):
        from engine_build import corpus_url

        self.base = os.environ.get("UVULARIA_PUBLISHED_BASE", DEMO_BASE).rstrip("/")
        try:
            self.corpus = load_corpus(corpus_url(self.base, "latest"), "bundle", timeout=20.0)
        except (urllib.error.URLError, OSError) as exc:
            self.skipTest(f"demonstration vault unreachable: {exc}")

    def test_corpus_loads_from_the_published_bundle(self):
        self.assertGreater(len(self.corpus.entries), 0)
        self.assertTrue(self.corpus.fingerprint)

    def test_signal_plane_reads_standing_and_announcements(self):
        plane = SignalPlane([
            StandingProvider(f"{self.base}/standing.json", timeout=20.0),
            AnnouncementProvider(f"{self.base}/feed.xml", timeout=20.0),
        ])
        snapshot = plane.snapshot()  # must not raise; degraded sources are strings
        self.assertIsInstance(snapshot.incidents, tuple)
        self.assertIsInstance(snapshot.degraded, tuple)

    def test_engine_constructs_over_the_demo(self):
        plane = SignalPlane([StandingProvider(f"{self.base}/standing.json", timeout=20.0)])
        engine = Engine(corpus=self.corpus, signal_plane=plane,
                        client=object(), config=Config(model="claude-sonnet-5-5"))
        self.assertIs(engine.corpus, self.corpus)


if __name__ == "__main__":
    unittest.main()
