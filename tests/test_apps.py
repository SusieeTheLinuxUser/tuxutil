import io
import json
import re
import shlex
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tuxutil import apps, cli, system

FAMILIES = ("arch", "debian", "fedora", "suse")
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


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
        catalog = apps.load()
        self.assertGreaterEqual(len(catalog), 80)
        ids = [app["id"] for app in catalog]
        self.assertEqual(len(ids), len(set(ids)))
        categories = set()
        for app in catalog:
            self.assertRegex(app["id"], ID_RE)
            for key in ("name", "category", "description"):
                self.assertIsInstance(app[key], str)
                self.assertTrue(app[key].strip())
            categories.add(app["category"])
            packages = app["packages"]
            self.assertIsInstance(packages, dict)
            for family, names in packages.items():
                self.assertIn(family, system.INSTALL_CMD)
                self.assertIsInstance(names, list)
                self.assertTrue(names)
                self.assertTrue(all(isinstance(name, str) and name for name in names))
            flatpak = app.get("flatpak") or ""
            self.assertIsInstance(flatpak, str)
            for family in FAMILIES:
                self.assertTrue(packages.get(family) or flatpak, app["id"])
        for category in ("browsers", "dev", "gaming", "media", "office", "chat", "utilities", "security", "system"):
            self.assertIn(category, categories)

    def test_view_availability_and_installed(self):
        bare = {"id": "x", "name": "X", "category": "dev", "description": "d", "packages": {}}
        self.assertFalse(apps.view(bare, "arch", set(), set())["available"])
        native = {"id": "git", "name": "Git", "category": "dev", "description": "d",
                  "packages": {"arch": ["git"]}}
        self.assertTrue(apps.view(native, "arch", {"git"}, set())["installed"])
        self.assertFalse(apps.view(native, "arch", set(), set())["installed"])
        self.assertFalse(apps.view(native, "debian", set(), set())["available"])
        both = {"id": "firefox", "name": "Firefox", "category": "browsers", "description": "d",
                "packages": {"arch": ["firefox"]}, "flatpak": "org.mozilla.firefox"}
        self.assertTrue(apps.view(both, "debian", set(), {"org.mozilla.firefox"})["installed"])


class DryRunTest(unittest.TestCase):
    def test_native_package_per_family(self):
        expected = {
            "arch": ["pacman", "-S", "--needed", "--noconfirm", "firefox"],
            "debian": ["apt-get", "install", "-y", "firefox-esr"],
            "fedora": ["dnf", "install", "-y", "firefox"],
            "suse": ["zypper", "-n", "install", "MozillaFirefox"],
        }
        for family in system.INSTALL_CMD:
            with as_family(family):
                rc, out, err = invoke(["--dry-run", "apps", "install", "firefox"])
            self.assertEqual(rc, 0, err)
            got = commands(out)
            if family in expected:
                self.assertEqual(got, [expected[family]])
            else:
                self.assertEqual(got, [["flatpak", "install", "-y", "flathub", "org.mozilla.firefox"]])

    def test_native_apps_share_one_command(self):
        with as_family("arch"):
            rc, out, _ = invoke(["--dry-run", "apps", "install", "firefox", "git"])
        self.assertEqual(rc, 0)
        self.assertEqual(commands(out), [["pacman", "-S", "--needed", "--noconfirm", "firefox", "git"]])

    def test_duplicate_ids_install_once(self):
        with as_family("debian"):
            rc, out, _ = invoke(["--dry-run", "apps", "install", "git", "git"])
        self.assertEqual(rc, 0)
        self.assertEqual(commands(out), [["apt-get", "install", "-y", "git"]])

    def test_native_and_flatpak_are_two_commands(self):
        with as_family("arch"):
            rc, out, _ = invoke(["--dry-run", "apps", "install", "firefox", "spotify"])
        self.assertEqual(rc, 0)
        self.assertEqual(commands(out), [
            ["pacman", "-S", "--needed", "--noconfirm", "firefox"],
            ["flatpak", "install", "-y", "flathub", "com.spotify.Client"],
        ])

    def test_flatpak_only_skips_the_package_manager(self):
        with as_family("fedora"):
            rc, out, _ = invoke(["--dry-run", "apps", "install", "spotify", "brave"])
        self.assertEqual(rc, 0)
        self.assertEqual(commands(out), [[
            "flatpak", "install", "-y", "flathub", "com.spotify.Client", "com.brave.Browser",
        ]])

    def test_remove_uses_the_remove_command(self):
        with as_family("fedora"):
            rc, out, _ = invoke(["--dry-run", "apps", "remove", "docker"])
        self.assertEqual(rc, 0)
        self.assertEqual(commands(out), [["dnf", "remove", "-y", "moby-engine", "docker-cli"]])

    def test_unknown_id_is_an_error(self):
        with as_family("arch"):
            rc, out, err = invoke(["--dry-run", "apps", "install", "not-a-real-app"])
        self.assertNotEqual(rc, 0)
        self.assertIn("Unknown", err + out)
        self.assertEqual(commands(out), [])

    def test_unavailable_without_flatpak_is_an_error(self):
        with as_family("nixos"):
            rc, out, err = invoke(["--dry-run", "apps", "install", "git"])
        self.assertNotEqual(rc, 0)
        self.assertIn("Not available on nixos", err + out)

    def test_dry_run_does_not_execute(self):
        with as_family("arch"), mock.patch("tuxutil.system.subprocess.run") as run:
            rc, _, _ = invoke(["--dry-run", "apps", "install", "firefox", "spotify"])
        self.assertEqual(rc, 0)
        run.assert_not_called()

    def test_list_json_shape_and_one_batch_query(self):
        calls = []

        def fake_query(cmd, timeout=15):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(list(cmd), 0, "firefox\norg.mozilla.firefox\n", "")

        with as_family("arch"), mock.patch("tuxutil.system.query", side_effect=fake_query):
            rc, out, err = invoke(["apps", "list", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls, [
            ["pacman", "-Qq"],
            ["flatpak", "list", "--app", "--columns=application"],
        ])
        rows = json.loads(out)
        self.assertGreaterEqual(len(rows), 80)
        by_id = {}
        for row in rows:
            for key in ("id", "name", "category", "description"):
                self.assertIsInstance(row[key], str)
            self.assertIsInstance(row["installed"], bool)
            self.assertIsInstance(row["available"], bool)
            by_id[row["id"]] = row
        self.assertTrue(by_id["firefox"]["installed"])
        self.assertTrue(by_id["firefox"]["available"])
        self.assertFalse(by_id["git"]["installed"])


if __name__ == "__main__":
    unittest.main()
