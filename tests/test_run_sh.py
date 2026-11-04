import os
import json
import pathlib
import subprocess
import tarfile
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class RunScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temp.name) / "home with spaces"
        self.home.mkdir()
        self.tarball = pathlib.Path(self.temp.name) / "source.tar.gz"
        with tarfile.open(self.tarball, "w:gz") as archive:
            for path in ROOT.iterdir():
                if path.name not in {".git", ".work", "__pycache__"}:
                    archive.add(path, arcname=f"tuxutil-main/{path.name}")

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args, script=None, url=None):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["XDG_CACHE_HOME"] = str(self.home / "cache with spaces")
        env["TUXUTIL_TARBALL"] = url or self.tarball.as_uri()
        return subprocess.run(
            ["sh", "-s", "--", *args], input=(script or (ROOT / "run.sh").read_text()),
            text=True, capture_output=True, cwd=ROOT, env=env, timeout=30,
        )

    def test_cli_runs_from_cached_tarball(self):
        result = self.invoke("doctor", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("family", json.loads(result.stdout))

    def test_failed_download_keeps_and_runs_last_good_copy(self):
        first = self.invoke("doctor", "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        failed = self.invoke("doctor", "--json", url="file:///does/not/exist.tar.gz")
        self.assertEqual(failed.returncode, 0, failed.stderr)
        self.assertIn("cached copy", failed.stderr)

    def test_truncated_script_does_nothing(self):
        result = self.invoke("doctor", script="#!/bin/sh\nmain() {\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / "cache with spaces").exists())


if __name__ == "__main__":
    unittest.main()
