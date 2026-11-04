import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from tuxutil import linuxiso
from tuxutil.cli import main


class DetectTest(unittest.TestCase):
    def layout(self, *paths):
        return linuxiso.detect_layout(list(paths))

    def test_families(self):
        for path, family in (
            ("/arch/x86_64/airootfs.sfs", "arch"),
            ("/casper/filesystem.squashfs", "ubuntu"),
            ("/live/filesystem.squashfs", "debian"),
            ("/LiveOS/squashfs.img", "fedora"),
        ):
            got = self.layout(path, "/EFI/BOOT/BOOTX64.EFI")
            self.assertEqual((got["family"], got["squashfs"], got["supported"]), (family, path, True))

    def test_layered_ubuntu_edits_the_top_layer(self):
        got = self.layout("/casper/minimal.squashfs", "/casper/minimal.standard.squashfs",
                          "/casper/minimal.standard.live.squashfs")
        self.assertEqual(got["squashfs"], "/casper/minimal.standard.live.squashfs")
        self.assertEqual(len(got["layers"]), 3)

    def test_unsupported(self):
        self.assertFalse(self.layout("/nix-store.squashfs")["supported"])
        self.assertFalse(self.layout("/sources/install.wim")["supported"])
        self.assertFalse(self.layout("/install.amd/vmlinuz")["supported"])

    def test_netinst_is_not_a_live_iso(self):
        info = linuxiso.describe(["/install.amd/vmlinuz"], disk_info="Debian 13 netinst\n")
        self.assertEqual(info["family"], "debian")
        self.assertFalse(info["supported"])

    def test_debian_installer_with_a_stray_squashfs_refused(self):
        paths = ["/live/filesystem.squashfs"]
        self.assertFalse(linuxiso.describe(paths, boot_text="linux /install.amd/vmlinuz")["supported"])
        self.assertTrue(linuxiso.describe(paths, boot_text="linux /live/vmlinuz boot=live")["supported"])

    def test_immutable_fedora_refused(self):
        info = linuxiso.describe(["/LiveOS/squashfs.img"], boot_text="linux /vmlinuz ostree=/x")
        self.assertFalse(info["supported"])


class BootTextTest(unittest.TestCase):
    def test_grub_args_go_before_the_separator(self):
        out = linuxiso.patch_boot_text("linux /casper/vmlinuz boot=casper quiet --- splash\n",
                                       kernel_args=["console=ttyS0"], kind="grub")
        self.assertEqual(out, "linux /casper/vmlinuz boot=casper quiet console=ttyS0 --- splash\n")

    def test_grub_args_go_before_mints_double_dash(self):
        out = linuxiso.patch_boot_text("linux /casper/vmlinuz quiet splash --\n",
                                       kernel_args=["console=ttyS0"], kind="grub")
        self.assertEqual(out, "linux /casper/vmlinuz quiet splash console=ttyS0 --\n")

    def test_media_check_removed_and_hostname_param_follows(self):
        out = linuxiso.patch_boot_text("linux /v root=live:CDLABEL=F rd.live.check quiet hostname=mint\n",
                                       kind="grub", hostname="tux")
        self.assertEqual(out, "linux /v root=live:CDLABEL=F quiet hostname=tux\n")

    def test_syslinux_and_systemd_boot(self):
        self.assertIn("APPEND a b", linuxiso.patch_boot_text("  APPEND a\n", kernel_args=["b"], kind="syslinux"))
        self.assertIn("options a b", linuxiso.patch_boot_text("options a\n", kernel_args=["b"], kind="systemd-boot"))

    def test_memtest_left_alone(self):
        text = "linux /boot/memtest86+x64.efi\n"
        self.assertEqual(linuxiso.patch_boot_text(text, kernel_args=["b"], kind="grub"), text)

    def test_arg_not_added_twice(self):
        text = "linux /vmlinuz quiet\n"
        self.assertEqual(linuxiso.patch_boot_text(text, kernel_args=["quiet"], kind="grub"), text)

    def test_timeout_units(self):
        self.assertIn("TIMEOUT 50", linuxiso.patch_boot_text("TIMEOUT 1\n", timeout=5, kind="syslinux"))
        self.assertIn("set timeout=5", linuxiso.patch_boot_text("set timeout=30\n", timeout=5, kind="grub"))
        self.assertIn("timeout 5", linuxiso.patch_boot_text("timeout 15\n", timeout=5, kind="loader"))

    def test_label_rewrite(self):
        out = linuxiso.patch_boot_text("root=live:CDLABEL=OLD\n", old_label="OLD", new_label="NEW")
        self.assertEqual(out, "root=live:CDLABEL=NEW\n")


class MisoTest(unittest.TestCase):
    def test_kernels_cfg_gets_args_and_bare_timeout_changes(self):
        self.assertTrue(linuxiso.is_boot_config("/boot/grub/kernels.cfg"))
        text = linuxiso.patch_boot_text("linux /boot/vmlinuz-$2 driver=free\n", kernel_args=["console=ttyS0"])
        self.assertIn("driver=free console=ttyS0", text)
        self.assertEqual(linuxiso.patch_boot_text("timeout=10\n", timeout=2), "timeout=2\n")


class CmdlineTest(unittest.TestCase):
    def test_args_go_before_separator_at_start_or_middle(self):
        self.assertEqual(linuxiso._append_cmdline("--- quiet", ["a=1"]), "a=1 --- quiet")
        self.assertEqual(linuxiso._append_cmdline("quiet --", ["a=1"]), "quiet a=1 --")
        self.assertEqual(linuxiso._append_cmdline("quiet", ["a=1"]), "quiet a=1")


class FirstBootTest(unittest.TestCase):
    def test_unit_is_enabled_and_preset(self):
        with tempfile.TemporaryDirectory() as folder:
            root, script = Path(folder, "root"), Path(folder, "s.sh")
            script.write_text("#!/bin/sh\n")
            linuxiso._install_first_boot(root, script)
            self.assertTrue((root / "etc/systemd/system/multi-user.target.wants/tuxutil-firstboot.service").is_symlink())
            self.assertIn("enable tuxutil-firstboot.service",
                          (root / "etc/systemd/system-preset/00-tuxutil.preset").read_text())


class SelinuxTest(unittest.TestCase):
    def test_only_enforcing_counts(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(linuxiso._selinux_enforcing(root))
            config = Path(root) / "etc/selinux/config"
            config.parent.mkdir(parents=True)
            config.write_text("# SELINUX=enforcing\nSELINUX=disabled\n")
            self.assertFalse(linuxiso._selinux_enforcing(root))
            config.write_text("SELINUX=enforcing\nSELINUXTYPE=targeted\n")
            self.assertTrue(linuxiso._selinux_enforcing(root))


class IsoMetadataTest(unittest.TestCase):
    def test_device_nodes_become_pseudo_lines(self):
        listing = ("crw-rw-rw- 0/5   1,  3 2024-01-01 00:00 squashfs-root/dev/null\n"
                   "brw-r----- 0/6 8,0 2024-01-01 00:00 squashfs-root/dev/sda\n"
                   "drwxr-xr-x 0/0 3 2024-01-01 00:00 squashfs-root/dev\n"
                   "-rw-r--r-- 0/0 3 2024-01-01 00:00 squashfs-root/etc/hostname\n")
        self.assertEqual(linuxiso.device_pseudo(listing), "dev/null c 666 0 5 1 3\ndev/sda b 640 0 6 8 0\n")

    def test_replay_after_maps_and_partitions_after_replay(self):
        cmd = linuxiso.xorriso_rebuild_cmd("in.iso", "out.iso", deletes=["/a.sig"], maps=[("/host/x", "/iso/x")],
                                           partitions=[(2, "0xef", "/host/efi.img")])
        self.assertLess(cmd.index("-map"), cmd.index("replay"))
        self.assertLess(cmd.index("replay"), cmd.index("-append_partition"))
        self.assertEqual(cmd[cmd.index("-map"):cmd.index("-map") + 3], ["-map", "/host/x", "/iso/x"])

    def test_appended_partition_report(self):
        report = ("-isohybrid-mbr --interval:local_fs:0s-15s:zero_mbrpt:'a.iso'\n"
                  "-append_partition 2 0xef --interval:local_fs:2048d-4095d::'a.iso'\n")
        self.assertEqual(linuxiso.appended_partitions(report), [(2, "0xef", 2048 * 512, 2048 * 512)])

    def test_md5sum_updates_only_changed_lines(self):
        old = "aaa  ./casper/filesystem.squashfs\nbbb  ./boot/grub/grub.cfg\n"
        new = linuxiso.update_md5sum(old, {"/casper/filesystem.squashfs": "ccc"})
        self.assertEqual(new, "ccc  ./casper/filesystem.squashfs\nbbb  ./boot/grub/grub.cfg\n")

    def test_signatures_of_changed_files_are_dropped(self):
        stale = linuxiso.stale_signature_paths(
            ["/arch/x86_64/airootfs.sfs.cms.sig", "/arch/x86_64/airootfs.sfs", "/other.sig"],
            {"/arch/x86_64/airootfs.sfs"})
        self.assertEqual(stale, ["/arch/x86_64/airootfs.sfs.cms.sig"])

    def test_options_json_shape(self):
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["linuxiso", "options", "--json"]), 0)
        specs = json.loads(out.getvalue())
        # The GUI has its own output field and passes -o itself.
        self.assertNotIn("-o", [spec["flag"] for spec in specs])
        for spec in specs:
            self.assertLessEqual({"flag", "type", "label", "help", "default", "choices"}, set(spec))


if __name__ == "__main__":
    unittest.main()
