import io
import re
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tuxutil import cli, selfupdate


class ParseVersionTest(unittest.TestCase):
    def test_tags_and_bare_versions(self):
        self.assertEqual(selfupdate.parse_version("v0.2.3"), (0, 2, 3))
        self.assertEqual(selfupdate.parse_version("0.2.3"), (0, 2, 3))
        self.assertEqual(selfupdate.parse_version("v1.0"), (1, 0))

    def test_garbage_is_none(self):
        for bad in ("", "  ", "garbage", "v1.2.x", "vv1.2", "v1.2.3.4-beta", None):
            self.assertIsNone(selfupdate.parse_version(bad), repr(bad))


class CompareTest(unittest.TestCase):
    def test_010_beats_09(self):
        self.assertGreater(selfupdate.parse_version("v0.2.10"), selfupdate.parse_version("v0.2.9"))

    def test_same_older(self):
        self.assertEqual(selfupdate.parse_version("v0.2.3"), selfupdate.parse_version("0.2.3"))
        self.assertLess(selfupdate.parse_version("v0.2.9"), selfupdate.parse_version("v0.3"))


class LocationTest(unittest.TestCase):
    def test_redirect_is_not_followed(self):
        # urllib calls this with 7 arguments; a wrong signature made every real check fail.
        self.assertIsNone(selfupdate.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://x"))

    def test_latest_version_reads_the_302_it_stops_at(self):
        # The whole request path, no network: a HEAD, redirects off, and the 302 that urllib then
        # raises as HTTPError carries the tag.
        tag_url = "https://github.com/SusieeTheLinuxUser/tuxutil/releases/tag/v0.2.4"
        found = urllib.error.HTTPError(selfupdate.RELEASES_URL, 302, "Found", {"Location": tag_url}, io.BytesIO())
        opener = mock.Mock()
        opener.open.side_effect = found
        with mock.patch.object(selfupdate.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(selfupdate.latest_version(), (0, 2, 4))
        build.assert_called_once_with(selfupdate.NoRedirect)
        request = opener.open.call_args.args[0]
        self.assertEqual((request.full_url, request.get_method()), (selfupdate.RELEASES_URL, "HEAD"))
        self.assertTrue(found.fp.closed)

    def test_latest_version_is_none_when_offline(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("offline")
        with mock.patch.object(selfupdate.urllib.request, "build_opener", return_value=opener):
            self.assertIsNone(selfupdate.latest_version())

    def test_tag_from_redirect(self):
        url = "https://github.com/SusieeTheLinuxUser/tuxutil/releases/tag/v0.2.4"
        self.assertEqual(selfupdate.version_from_location(url), (0, 2, 4))
        self.assertEqual(selfupdate.version_from_location(url + "/"), (0, 2, 4))

    def test_garbage_and_missing_are_none(self):
        for bad in ("", None, "not a url",
                    "https://github.com/SusieeTheLinuxUser/tuxutil/releases/tag/latest"):
            self.assertIsNone(selfupdate.version_from_location(bad), repr(bad))


class CurrentVersionTest(unittest.TestCase):
    def test_source_checkout_wins_over_an_older_installed_copy(self):
        # run.sh runs from source; an older pip-installed tuxutil must not decide the version.
        pyproject = (Path(selfupdate.__file__).parent.parent / "pyproject.toml").read_text()
        want = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
        with mock.patch.object(selfupdate.metadata, "version", return_value="0.0.1"):
            self.assertEqual(selfupdate.current_version(), want)

    def test_init_version_matches_pyproject(self):
        self.assertEqual(selfupdate.__version__, selfupdate.current_version())


class StatusTest(unittest.TestCase):
    def test_no_check_says_couldnt(self):
        with mock.patch("tuxutil.selfupdate.latest_version", return_value=None):
            st = selfupdate.status()
        self.assertIsNone(st["latest"])
        self.assertFalse(st["update_available"])
        self.assertIn("couldn't check for updates", selfupdate.describe(st))

    def test_newer_says_out(self):
        with mock.patch("tuxutil.selfupdate.latest_version", return_value=(0, 2, 10)), \
                mock.patch("tuxutil.selfupdate.current_version", return_value="0.2.9"):
            st = selfupdate.status()
        self.assertTrue(st["update_available"])
        line = selfupdate.describe(st)
        self.assertIn("0.2.9", line)
        self.assertIn("0.2.10 is out", line)
        self.assertIn(selfupdate.RELEASES_URL, line)

    def test_same_says_latest(self):
        with mock.patch("tuxutil.selfupdate.latest_version", return_value=(0, 2, 9)), \
                mock.patch("tuxutil.selfupdate.current_version", return_value="0.2.9"):
            st = selfupdate.status()
        self.assertFalse(st["update_available"])
        self.assertIn("(latest)", selfupdate.describe(st))

    def test_garbage_latest_is_no_update(self):
        with mock.patch("tuxutil.selfupdate.latest_version", return_value=None):
            self.assertFalse(selfupdate.status()["update_available"])


class DoctorLineTest(unittest.TestCase):
    def test_doctor_prints_one_selfupdate_line(self):
        out = io.StringIO()
        with mock.patch("tuxutil.selfupdate.latest_version", return_value=None), redirect_stdout(out):
            rc = cli.main(["doctor"])
        self.assertEqual(rc, 0)
        lines = [l for l in out.getvalue().splitlines() if l.startswith("tuxutil ")]
        self.assertEqual(len(lines), 1, out.getvalue())
        self.assertIn("couldn't check for updates", lines[0])


if __name__ == "__main__":
    unittest.main()