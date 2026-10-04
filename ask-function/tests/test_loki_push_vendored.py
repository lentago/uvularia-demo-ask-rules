"""src/loki_push.py is drosera's clients/loki_push.py, vendored unchanged.

The file carries a four-line header naming the upstream commit and the sha256 of
the upstream file. Offline, this test holds the body to that hash, so a hand edit
fails the build. With ``UVULARIA_CHECK_UPSTREAM=1`` (uvularia's own CI sets it) it
also fetches the file at the named commit and compares; an unreachable GitHub is
a skip with a notice, never a pass.

To take a newer drosera: copy the new file, update the commit and the hash in the
header, and run these tests.
"""

import hashlib
import os
import re
import unittest
import urllib.error
import urllib.request

import support

VENDORED = support.SRC / "loki_push.py"
HEADER_LINES = 4


def _split():
    text = VENDORED.read_bytes()
    lines = text.splitlines(keepends=True)
    header = b"".join(lines[:HEADER_LINES]).decode("utf-8")
    body = b"".join(lines[HEADER_LINES:])
    commit = re.search(r"\b([0-9a-f]{40})\b", header)
    digest = re.search(r"#\s*([0-9a-f]{64})\s*$", header, re.MULTILINE)
    return header, body, commit and commit.group(1), digest and digest.group(1)


class VendoredLokiPushIsUnchanged(unittest.TestCase):
    def test_header_names_source_commit_and_hash(self):
        header, _, commit, digest = _split()
        self.assertIn("lentago/drosera clients/loki_push.py", header)
        self.assertIsNotNone(commit, "header must name the upstream commit")
        self.assertIsNotNone(digest, "header must carry the upstream sha256")

    def test_body_matches_the_recorded_hash(self):
        _, body, _, digest = _split()
        self.assertEqual(hashlib.sha256(body).hexdigest(), digest,
                         "src/loki_push.py was edited after vendoring; re-copy it from drosera")

    @unittest.skipUnless(os.environ.get("UVULARIA_CHECK_UPSTREAM") == "1",
                         "set UVULARIA_CHECK_UPSTREAM=1 to compare against drosera")
    def test_body_matches_upstream_at_the_named_commit(self):
        _, body, commit, _ = _split()
        url = f"https://raw.githubusercontent.com/lentago/drosera/{commit}/clients/loki_push.py"
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310
                upstream = resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            self.skipTest(f"notice: could not reach {url} ({exc})")
        self.assertEqual(body, upstream, f"src/loki_push.py differs from drosera at {commit}")


if __name__ == "__main__":
    unittest.main()
