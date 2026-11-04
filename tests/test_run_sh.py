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
            text=True, capture_output=True, cwd=self.temp.name, env=env, timeout=30,
        )

    def test_cli_runs_from_cached_tarball(self):
        result = self.invoke("doctor", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("family", json.loads(result.stdout))
        self.assertTrue((self.home / "cache with spaces/tuxutil/app/tuxutil/__main__.py").is_file())
        self.assertEqual(sorted(p.name for p in (self.home / "cache with spaces/tuxutil").iterdir()), ["app"])

    def test_it_runs_the_cached_copy_not_the_repo(self):
        marker = pathlib.Path(self.temp.name) / "marked.tar.gz"
        with tarfile.open(marker, "w:gz") as archive:
            for path in ROOT.iterdir():
                if path.name not in {".git", ".work", "__pycache__", "tuxutil"}:
                    archive.add(path, arcname=f"tuxutil-main/{path.name}")
            archive.add(ROOT / "tuxutil", arcname="tuxutil-main/tuxutil", filter=lambda t: None if "__pycache__" in t.name or t.name.endswith("/__main__.py") else t)
            main = pathlib.Path(self.temp.name) / "__main__.py"
            main.write_text("print('CACHED COPY')\n")
            archive.add(main, arcname="tuxutil-main/tuxutil/__main__.py")
        result = self.invoke("doctor", url=marker.as_uri())
        self.assertIn("CACHED COPY", result.stdout, result.stderr)

    def test_bad_tarball_keeps_the_last_good_copy(self):
        self.assertEqual(self.invoke("doctor", "--json").returncode, 0)
        junk = pathlib.Path(self.temp.name) / "junk.tar.gz"
        junk.write_bytes(b"not a tarball")
        result = self.invoke("doctor", "--json", url=junk.as_uri())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cached copy", result.stderr)

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
