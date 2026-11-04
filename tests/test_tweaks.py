import io
import json
import re
import shlex
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tuxutil import cli, system, tweaks

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
KNOWN_FAMILIES = set(system.FAMILY_IDS.values())
RISKS = {"low", "medium", "high"}
FORBIDDEN = ("setenforce", "aa-teardown", "apparmor_parser", "grubby", "update-grub", "shutdown", "reboot")


def invoke(argv):
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code
                if not isinstance(rc, int):
                    print(rc, file=err)
                    rc = 1
    finally:
        system.DRY_RUN = False
    return rc, out.getvalue(), err.getvalue()


def commands(text):
    """Parse '$ cmd' lines. A quoted argument may span lines, so quotes are matched first."""
    found = []
    pending = None
    for line in text.splitlines(keepends=True):
        if pending is None:
            if not line.startswith("$ "):
                continue
            pending = line[2:]
        else:
            pending += line
        try:
            argv = shlex.split(pending)
        except ValueError:
            continue
        if argv and argv[0] in {"sudo", "pkexec", "doas"}:
            argv = argv[1:]
        found.append(argv)
        pending = None
    if pending is not None:
        raise AssertionError("unclosed command in output: " + pending[:200])
    return found


def as_family(name):
    return mock.patch("tuxutil.system.detect", return_value=system.Distro(name, name, name))


class CatalogTest(unittest.TestCase):
    def test_catalog_shape(self):
        catalog = tweaks.load()
        self.assertGreaterEqual(len(catalog), 30)
        ids = [tweak["id"] for tweak in catalog]
        self.assertEqual(len(ids), len(set(ids)))
        for tweak in catalog:
            self.assertRegex(tweak["id"], ID_RE)
            for key in ("name", "category", "description"):
                self.assertIsInstance(tweak[key], str)
                self.assertTrue(tweak[key].strip())
            self.assertIn(tweak["risk"], RISKS)
            self.assertIsInstance(tweak["families"], list)
            self.assertTrue(tweak["families"])
            for family in tweak["families"]:
                self.assertIn(family, KNOWN_FAMILIES)
            for key in ("apply", "undo"):
                self.assertIsInstance(tweak[key], list)
                self.assertTrue(tweak[key])
                for cmd in tweak[key]:
                    self._argv(cmd)
            self._argv(tweak["check"])

    def test_shell_snippets_parse(self):
        for tweak in tweaks.load():
            for cmd in [*tweak["apply"], *tweak["undo"], tweak["check"]]:
                joined = " ".join(cmd).lower()
                for token in FORBIDDEN:
                    self.assertNotIn(token, joined, tweak["id"])
                if cmd[0] == "sh":
                    self.assertEqual(cmd[:2], ["sh", "-c"])
                    subprocess.run(["sh", "-n", "-c", cmd[2]], check=True)

    def test_user_tweak_is_not_run_as_root(self):
        tweak = next(item for item in tweaks.load() if item["id"] == "disable-ubuntu-report")
        self.assertFalse(tweak["root"])
        self.assertEqual(tweak["families"], ["debian"])

    def _argv(self, cmd):
        self.assertIsInstance(cmd, list)
        self.assertTrue(cmd)
        self.assertTrue(all(isinstance(part, str) and part for part in cmd))


class DryRunTest(unittest.TestCase):
    def test_apply_and_undo_print_the_catalog_commands(self):
        for tweak in tweaks.load():
            for family in tweak["families"]:
                with as_family(family):
                    rc, out, err = invoke(["--dry-run", "tweaks", "apply", tweak["id"]])
                self.assertEqual(rc, 0, f"{tweak['id']} {family} {err}")
                self.assertEqual(commands(out), tweak["apply"], tweak["id"])
                with as_family(family):
                    rc, out, err = invoke(["--dry-run", "tweaks", "undo", tweak["id"]])
                self.assertEqual(rc, 0, err)
                self.assertEqual(commands(out), tweak["undo"], tweak["id"])

    def test_unavailable_family_is_an_error(self):
        with as_family("arch"):
            rc, out, err = invoke(["--dry-run", "tweaks", "apply", "disable-popularity-contest"])
        self.assertNotEqual(rc, 0)
        self.assertIn("Not available on arch", err + out)
        self.assertEqual(commands(out), [])

    def test_unknown_id_is_an_error(self):
        with as_family("debian"):
            rc, out, err = invoke(["--dry-run", "tweaks", "undo", "not-a-tweak"])
        self.assertNotEqual(rc, 0)
        self.assertIn("Unknown", err + out)

    def test_ubuntu_report_stays_unprivileged(self):
        with as_family("debian"):
            rc, out, _ = invoke(["--dry-run", "tweaks", "apply", "disable-ubuntu-report"])
        self.assertEqual(rc, 0)
        self.assertIn("$ ubuntu-report -f send no\n", out)
        self.assertNotIn("sudo ubuntu-report", out)
        self.assertNotIn("pkexec ubuntu-report", out)

    def test_dry_run_does_not_execute(self):
        with as_family("arch"), mock.patch("tuxutil.system.subprocess.run") as run:
            rc, _, _ = invoke(["--dry-run", "tweaks", "apply", "swappiness"])
        self.assertEqual(rc, 0)
        run.assert_not_called()

    def test_list_json_shape(self):
        with as_family("debian"), mock.patch(
            "tuxutil.system.query",
            return_value=subprocess.CompletedProcess([], 1, "", ""),
        ):
            rc, out, err = invoke(["tweaks", "list", "--json"])
        self.assertEqual(rc, 0, err)
        rows = json.loads(out)
        self.assertGreaterEqual(len(rows), 30)
        for row in rows:
            for key in ("id", "name", "category", "description"):
                self.assertIsInstance(row[key], str)
            self.assertIn(row["risk"], RISKS)
            self.assertIsInstance(row["available"], bool)
            self.assertIn(row["applied"], (True, False, None))
            if not row["available"]:
                self.assertIsNone(row["applied"])


if __name__ == "__main__":
    unittest.main()
