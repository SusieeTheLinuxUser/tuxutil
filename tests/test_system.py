import io
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tuxutil import cli, system


def distro_from(text):
    with tempfile.NamedTemporaryFile("w", suffix="os-release") as f:
        f.write(text)
        f.flush()
        return system.detect(f.name)


class DetectTest(unittest.TestCase):
    def test_quoted_values_and_comments(self):
        got = system.parse_os_release('# c\nNAME="Foo Bar"\nID=foo\nEMPTY=\n')
        self.assertEqual(got, {"NAME": "Foo Bar", "ID": "foo", "EMPTY": ""})

    def test_families(self):
        for text, family in [
            ('ID=cachyos\nID_LIKE=arch\n', "arch"),
            ('ID=ubuntu\nID_LIKE=debian\n', "debian"),
            ('ID=somederivative\nID_LIKE="ubuntu debian"\n', "debian"),
            ('ID=nobara\nID_LIKE="rhel fedora"\n', "fedora"),
            ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', "suse"),
            ('ID=nixos\n', "nixos"),
        ]:
            self.assertEqual(distro_from(text).family, family, text)

    def test_tool_package(self):
        self.assertEqual(system.tool_package("wimlib-imagex", "debian"), "wimtools")
        self.assertEqual(system.tool_package("mksquashfs", "arch"), "squashfs-tools")
        self.assertEqual(system.tool_package("xorriso", "fedora"), "xorriso")

    def test_every_family_can_update_install_remove(self):
        self.assertEqual(set(system.INSTALL_CMD), set(system.REMOVE_CMD))
        self.assertEqual(set(system.INSTALL_CMD), set(system.UPDATE_CMDS))


class RootAndErrorsTest(unittest.TestCase):
    def test_as_root_all_joins_commands(self):
        with mock.patch("tuxutil.system.as_root", side_effect=lambda cmd: ["sudo", *cmd]):
            got = system.as_root_all([["ufw", "allow", "22/tcp"], ["ufw", "--force", "enable"]])
        self.assertEqual(got, ["sudo", "sh", "-c", "ufw allow 22/tcp && ufw --force enable"])

    def test_update_is_one_root_command(self):
        distro = system.Distro("debian", "Debian", "debian")
        out = io.StringIO()
        try:
            with mock.patch("tuxutil.system.detect", return_value=distro), \
                    mock.patch("tuxutil.cli.shutil.which", return_value=None), \
                    redirect_stdout(out):
                rc = cli.main(["--dry-run", "update"])
        finally:
            system.DRY_RUN = False
        self.assertEqual(rc, 0)
        lines = [line[2:] for line in out.getvalue().splitlines() if line.startswith("$ ")]
        self.assertEqual(len(lines), 1)
        argv = shlex.split(lines[0])
        if argv[0] in {"sudo", "pkexec", "doas"}:
            argv = argv[1:]
        self.assertEqual(argv[0], "sh")
        self.assertIn("apt-get update && apt-get dist-upgrade -y", argv[2])

    def test_command_failure_is_one_line(self):
        def boom(args):
            raise subprocess.CalledProcessError(2, ["ufw", "allow", "22/tcp"])

        err = io.StringIO()
        try:
            with mock.patch("tuxutil.tweaks.cmd_apply", boom), redirect_stderr(err), redirect_stdout(io.StringIO()):
                rc = cli.main(["--dry-run", "tweaks", "apply", "enable-ufw"])
        finally:
            system.DRY_RUN = False
        self.assertEqual(rc, 2)
        text = err.getvalue()
        self.assertEqual(text.strip().count("\n"), 0)
        self.assertIn("Command failed (exit 2): ufw allow 22/tcp", text)
        self.assertNotIn("Traceback", text)

    def test_missing_command_is_one_line(self):
        def boom(args):
            raise FileNotFoundError(2, "No such file or directory", "ufw")

        err = io.StringIO()
        try:
            with mock.patch("tuxutil.tweaks.cmd_apply", boom), redirect_stderr(err), redirect_stdout(io.StringIO()):
                rc = cli.main(["--dry-run", "tweaks", "apply", "enable-ufw"])
        finally:
            system.DRY_RUN = False
        self.assertEqual(rc, 127)
        text = err.getvalue()
        self.assertEqual(text.strip().count("\n"), 0)
        self.assertIn("Command not found (exit 127): ufw", text)
        self.assertNotIn("Traceback", text)


class AtomicOutputTest(unittest.TestCase):
    def test_keeps_the_old_file_until_the_new_one_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.iso"
            out.write_text("old")
            with self.assertRaises(RuntimeError), system.atomic_output(out) as partial:
                Path(partial).write_text("half")
                self.assertEqual(out.read_text(), "old")
                raise RuntimeError
            self.assertEqual(out.read_text(), "old")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["out.iso"])
            with system.atomic_output(out) as partial:
                Path(partial).write_text("new")
            self.assertEqual(out.read_text(), "new")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["out.iso"])


if __name__ == "__main__":
    unittest.main()
