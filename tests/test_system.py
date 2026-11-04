import tempfile
import unittest
from pathlib import Path

from tuxutil import system


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


if __name__ == "__main__":
    unittest.main()
