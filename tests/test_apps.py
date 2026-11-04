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
                self.assertIsInstance(names, list)
                self.assertTrue(all(isinstance(name, str) and name for name in names))
                if family == "ubuntu":
                    continue
                self.assertIn(family, system.INSTALL_CMD)
                self.assertTrue(names)
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


FLATHUB_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"


def flatpak_install(*ids):
    return [
        ["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", FLATHUB_URL],
        ["flatpak", "install", "--user", "-y", "flathub", *ids],
    ]


def assert_flatpak_unprivileged(testcase, text):
    for line in text.splitlines():
        if line.startswith("$ ") and "flatpak" in line:
            testcase.assertFalse(line.startswith(("$ pkexec ", "$ sudo ", "$ doas ")), line)


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
                self.assertEqual(got, flatpak_install("org.mozilla.firefox"))
                assert_flatpak_unprivileged(self, out)

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
            *flatpak_install("com.spotify.Client"),
        ])
        assert_flatpak_unprivileged(self, out)

    def test_flatpak_only_skips_the_package_manager(self):
        with as_family("fedora"):
            rc, out, _ = invoke(["--dry-run", "apps", "install", "spotify", "brave"])
        self.assertEqual(rc, 0)
        self.assertEqual(commands(out), flatpak_install("com.spotify.Client", "com.brave.Browser"))
        assert_flatpak_unprivileged(self, out)

    def test_remove_flatpak_is_per_user(self):
        with as_family("arch"):
            rc, out, _ = invoke(["--dry-run", "apps", "remove", "spotify"])
        self.assertEqual(rc, 0)
        self.assertEqual(commands(out), [["flatpak", "uninstall", "--user", "-y", "com.spotify.Client"]])
        assert_flatpak_unprivileged(self, out)

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


class UbuntuPackagesTest(unittest.TestCase):
    def test_ubuntu_overrides_missing_and_snap_debs(self):
        catalog = {app["id"]: app for app in apps.load()}
        ubuntu = system.Distro("ubuntu", "Ubuntu", "debian")
        mint = system.Distro("linuxmint", "Linux Mint", "debian")
        debian = system.Distro("debian", "Debian", "debian")
        for app_id in ("firefox", "chromium", "thunderbird", "prism-launcher", "telegram", "fastfetch", "gamescope"):
            for distro in (ubuntu, mint):
                self.assertEqual(apps.packages_for(catalog[app_id], distro), [], app_id)
        self.assertEqual(apps.packages_for(catalog["firefox"], debian), ["firefox-esr"])
        self.assertEqual(apps.packages_for(catalog["chromium"], debian), ["chromium"])
        self.assertEqual(apps.packages_for(catalog["thunderbird"], debian), ["thunderbird"])
        self.assertEqual(apps.packages_for(catalog["git"], ubuntu), ["git"])

    def test_id_like_ubuntu_uses_the_override(self):
        firefox = next(app for app in apps.load() if app["id"] == "firefox")
        distro = system.Distro("myubuntu", "My Ubuntu", "debian")
        release = 'ID=myubuntu\nID_LIKE="ubuntu debian"\n'
        with mock.patch("tuxutil.apps.Path.read_text", return_value=release):
            self.assertEqual(apps.packages_for(firefox, distro), [])
        release = "ID=myubuntu\nID_LIKE=debian\n"
        with mock.patch("tuxutil.apps.Path.read_text", return_value=release):
            self.assertEqual(apps.packages_for(firefox, distro), ["firefox-esr"])

    def test_ubuntu_firefox_installs_flatpak_not_esr(self):
        distro = system.Distro("ubuntu", "Ubuntu", "debian")
        with mock.patch("tuxutil.system.detect", return_value=distro):
            rc, out, err = invoke(["--dry-run", "apps", "install", "firefox"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(commands(out), flatpak_install("org.mozilla.firefox"))
        self.assertNotIn("firefox-esr", out)
        self.assertNotIn("apt-get", out)
        assert_flatpak_unprivileged(self, out)

    def test_ubuntu_fastfetch_is_not_available(self):
        distro = system.Distro("linuxmint", "Linux Mint", "debian")
        with mock.patch("tuxutil.system.detect", return_value=distro):
            rc, out, err = invoke(["--dry-run", "apps", "install", "fastfetch"])
        self.assertNotEqual(rc, 0)
        self.assertIn("Not available", err + out)
        self.assertEqual(commands(out), [])


class RootBatchTest(unittest.TestCase):
    def test_one_root_step_is_not_wrapped(self):
        steps = [(True, ["apt-get", "install", "-y", "git"]), (False, ["flatpak", "install", "--user", "-y", "x"])]
        with mock.patch("tuxutil.system.run") as run, \
                mock.patch("tuxutil.system.as_root", side_effect=lambda cmd: ["sudo", *cmd]) as one, \
                mock.patch("tuxutil.system.as_root_all") as batch, \
                redirect_stdout(io.StringIO()):
            apps._run_steps(steps)
        one.assert_called_once_with(["apt-get", "install", "-y", "git"])
        batch.assert_not_called()
        self.assertEqual(run.call_count, 2)

    def test_two_root_steps_share_one_prompt(self):
        steps = [(True, ["pacman", "-Sy"]), (True, ["pacman", "-Su"])]
        with mock.patch("tuxutil.system.run") as run, \
                mock.patch("tuxutil.system.as_root") as one, \
                mock.patch("tuxutil.system.as_root_all", return_value=["sudo", "sh", "-c", "joined"]) as batch, \
                redirect_stdout(io.StringIO()):
            apps._run_steps(steps)
        one.assert_not_called()
        batch.assert_called_once_with([["pacman", "-Sy"], ["pacman", "-Su"]])
        run.assert_called_once_with(["sudo", "sh", "-c", "joined"])


if __name__ == "__main__":
    unittest.main()
