import io
import json
import re
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
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


def query_returning(code):
    return mock.patch(
        "tuxutil.system.query",
        return_value=subprocess.CompletedProcess([], code, "", ""),
    )


def expected_commands(tweak, key):
    cmds = tweak[key]
    if tweak.get("root", True):
        script = " && ".join(shlex.join(cmd) for cmd in cmds)
        return [["sh", "-c", script]]
    return cmds


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
                with as_family(family), query_returning(1):
                    rc, out, err = invoke(["--dry-run", "tweaks", "apply", tweak["id"]])
                self.assertEqual(rc, 0, f"{tweak['id']} {family} {err}")
                self.assertEqual(commands(out), expected_commands(tweak, "apply"), tweak["id"])
                with as_family(family), query_returning(0):
                    rc, out, err = invoke(["--dry-run", "tweaks", "undo", tweak["id"]])
                self.assertEqual(rc, 0, err)
                self.assertEqual(commands(out), expected_commands(tweak, "undo"), tweak["id"])

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
        with as_family("debian"), query_returning(1):
            rc, out, _ = invoke(["--dry-run", "tweaks", "apply", "disable-ubuntu-report", "disable-whoopsie"])
        self.assertEqual(rc, 0)
        self.assertIn("$ ubuntu-report -f send no\n", out)
        self.assertNotIn("sudo ubuntu-report", out)
        self.assertNotIn("pkexec ubuntu-report", out)
        got = commands(out)
        self.assertEqual(got[0], ["ubuntu-report", "-f", "send", "no"])
        self.assertEqual(got[1][0], "sh")
        self.assertIn("whoopsie.service", got[1][2])

    def test_root_tweaks_share_one_shell(self):
        with as_family("arch"), query_returning(1):
            rc, out, err = invoke(["--dry-run", "tweaks", "apply", "enable-ufw", "fstrim-timer"])
        self.assertEqual(rc, 0, err)
        got = commands(out)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], "sh")
        script = got[0][2]
        self.assertEqual(script.count(" && "), 4)
        self.assertIn("ufw allow 22/tcp", script)
        self.assertNotIn("OpenSSH", script)
        self.assertIn("fstrim.timer", script)

    def test_skip_apply_and_undo_from_the_check(self):
        with as_family("arch"), query_returning(0):
            rc, out, err = invoke(["--dry-run", "tweaks", "apply", "swappiness"])
        self.assertEqual(rc, 0, err)
        self.assertIn("skipping swappiness: already applied", out)
        self.assertEqual(commands(out), [])
        with as_family("arch"), query_returning(1):
            rc, out, err = invoke(["--dry-run", "tweaks", "undo", "swappiness"])
        self.assertEqual(rc, 0, err)
        self.assertIn("skipping swappiness: not applied", out)
        self.assertNotIn("sysctl", out)
        self.assertNotIn("vm.swappiness=60", out)
        self.assertEqual(commands(out), [])

    def test_unknown_check_still_runs(self):
        with as_family("arch"), query_returning(127):
            rc, out, err = invoke(["--dry-run", "tweaks", "undo", "swappiness"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("skipping", out)
        self.assertIn("sysctl -w", commands(out)[0][2])

    def test_dry_run_does_not_execute(self):
        with as_family("arch"), query_returning(1), mock.patch("tuxutil.system.subprocess.run") as run:
            rc, _, _ = invoke(["--dry-run", "tweaks", "apply", "swappiness"])
        self.assertEqual(rc, 0)
        run.assert_not_called()

    def test_ufw_allows_port_22_and_checks_the_conf(self):
        tweak = next(item for item in tweaks.load() if item["id"] == "enable-ufw")
        self.assertIn(["ufw", "allow", "22/tcp"], tweak["apply"])
        self.assertNotIn("OpenSSH", json.dumps(tweak["apply"]))
        self.assertEqual(tweak["check"], ["grep", "-qxF", "ENABLED=yes", "/etc/ufw/ufw.conf"])
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "ufw.conf"
            conf.write_text("ENABLED=no\n")
            check = ["grep", "-qxF", "ENABLED=yes", str(conf)]
            self.assertNotEqual(subprocess.run(check).returncode, 0)
            conf.write_text("ENABLED=yes\n")
            subprocess.run(check, check=True)

    def test_timesyncd_skips_fedora_and_suse(self):
        tweak = next(item for item in tweaks.load() if item["id"] == "systemd-timesyncd")
        self.assertEqual(tweak["families"], ["arch", "debian"])
        with as_family("fedora"):
            rc, out, err = invoke(["--dry-run", "tweaks", "apply", "systemd-timesyncd"])
        self.assertNotEqual(rc, 0)
        self.assertIn("Not available on fedora", err + out)
        with as_family("suse"):
            rc, out, err = invoke(["--dry-run", "tweaks", "apply", "systemd-timesyncd"])
        self.assertNotEqual(rc, 0)
        self.assertIn("Not available on suse", err + out)

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


def _run_relocated(tweak, key, etc):
    script = tweak[key][0][2].replace("/etc", str(etc))
    subprocess.run(["sh", "-c", script], check=True)


class LineUndoTest(unittest.TestCase):
    """Run the real shell against a temp directory. Nothing here touches the host /etc."""

    def test_pacman_tweaks_undo_only_their_own_lines(self):
        catalog = {item["id"]: item for item in tweaks.load()}
        stock = (
            "# user note\n"
            "#ParallelDownloads = 5\n"
            "#[multilib-testing]\n"
            "#Include = /etc/pacman.d/mirrorlist\n"
            "#[multilib]\n"
            "#Include = /etc/pacman.d/mirrorlist\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            etc = Path(tmp) / "etc"
            etc.mkdir()
            conf = etc / "pacman.conf"
            conf.write_text(stock)
            _run_relocated(catalog["pacman-parallel"], "apply", etc)
            _run_relocated(catalog["pacman-multilib"], "apply", etc)
            text = conf.read_text()
            self.assertIn("ParallelDownloads = 5\n", text)
            self.assertIn("\n[multilib]\n", text)
            self.assertIn("#[multilib-testing]\n", text)
            self.assertNotIn("#[multilib]\n", text)
            conf.write_text(text + "USER_EDIT=1\n")
            _run_relocated(catalog["pacman-parallel"], "undo", etc)
            text = conf.read_text()
            self.assertIn("#ParallelDownloads = 5\n", text)
            self.assertIn("\n[multilib]\n", text)
            self.assertIn("USER_EDIT=1\n", text)
            self.assertIn("#[multilib-testing]\n", text)
            _run_relocated(catalog["pacman-multilib"], "undo", etc)
            text = conf.read_text()
            self.assertIn("#[multilib]\n", text)
            self.assertIn("#[multilib-testing]\n", text)
            self.assertIn("USER_EDIT=1\n", text)
            self.assertNotIn("\n[multilib]\n", text)
            conf.write_text("[multilib]\nInclude = /etc/pacman.d/mirrorlist\n")
            _run_relocated(catalog["pacman-multilib"], "undo", etc)
            self.assertEqual(conf.read_text(), "[multilib]\nInclude = /etc/pacman.d/mirrorlist\n")

    def test_dnf_undo_keeps_a_later_edit(self):
        tweak = next(item for item in tweaks.load() if item["id"] == "dnf-parallel")
        with tempfile.TemporaryDirectory() as tmp:
            etc = Path(tmp) / "etc"
            (etc / "dnf").mkdir(parents=True)
            conf = etc / "dnf" / "dnf.conf"
            conf.write_text("[main]\ngpgcheck=1\n")
            _run_relocated(tweak, "apply", etc)
            text = conf.read_text().replace("gpgcheck=1", "gpgcheck=0")
            conf.write_text(text)
            _run_relocated(tweak, "undo", etc)
            text = conf.read_text()
            self.assertNotIn("max_parallel_downloads", text)
            self.assertIn("gpgcheck=0\n", text)
            self.assertIn("[main]\n", text)

    def test_makepkg_undo_keeps_the_literal_nproc(self):
        tweak = next(item for item in tweaks.load() if item["id"] == "makepkg-jobs")
        with tempfile.TemporaryDirectory() as tmp:
            etc = Path(tmp) / "etc"
            etc.mkdir()
            conf = etc / "makepkg.conf"
            original = '#MAKEFLAGS="-j2"\nOTHER=1\n'
            conf.write_text(original)
            _run_relocated(tweak, "apply", etc)
            text = conf.read_text()
            self.assertIn('MAKEFLAGS="-j$(nproc)"\n', text)
            self.assertIn("OTHER=1\n", text)
            _run_relocated(tweak, "undo", etc)
            self.assertEqual(conf.read_text(), original)

    def test_popularity_contest_undo_keeps_other_lines(self):
        tweak = next(item for item in tweaks.load() if item["id"] == "disable-popularity-contest")
        with tempfile.TemporaryDirectory() as tmp:
            etc = Path(tmp) / "etc"
            etc.mkdir()
            conf = etc / "popularity-contest.conf"
            conf.write_text('MAILTO=root\nPARTICIPATE="yes"\n')
            _run_relocated(tweak, "apply", etc)
            conf.write_text(conf.read_text() + "EXTRA=1\n")
            _run_relocated(tweak, "undo", etc)
            text = conf.read_text()
            self.assertIn('PARTICIPATE="yes"\n', text)
            self.assertIn("MAILTO=root\n", text)
            self.assertIn("EXTRA=1\n", text)
            conf.unlink()
            _run_relocated(tweak, "apply", etc)
            self.assertTrue(conf.is_file())
            _run_relocated(tweak, "undo", etc)
            self.assertFalse(conf.exists())


if __name__ == "__main__":
    unittest.main()
