import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tuxutil import __version__
from tuxutil import flash, system
from tuxutil.cli import main

ROOT = Path(__file__).resolve().parents[1]


def disk(name, path, size, tran, rm, children=None, model="Disk", fstype=None, mountpoints=None):
    node = {
        "name": name, "path": path, "pkname": None, "type": "disk", "size": size,
        "tran": tran, "rm": rm, "model": model, "fstype": fstype,
        "mountpoints": mountpoints or [],
    }
    if children:
        node["children"] = children
    return node


def part(name, path, parent, size=1024, fstype=None, mountpoints=None, children=None):
    node = {
        "name": name, "path": path, "pkname": parent, "type": "part", "size": size,
        "tran": None, "rm": False, "model": None, "fstype": fstype,
        "mountpoints": mountpoints or [],
    }
    if children:
        node["children"] = children
    return node


def snap(devices, mounts=None, swaps=""):
    return {"blockdevices": devices}, {"blockdevices": []}, {"filesystems": mounts or []}, swaps


def held(reason):
    if not reason.startswith("holds "):
        return []
    return reason.removeprefix("holds ").split(", ")


class SafetyTest(unittest.TestCase):
    def test_nvme_system_disk_refused(self):
        normal, inverse, mounts, swaps = snap([
            disk("nvme0n1", "/dev/nvme0n1", 4000787030016, "nvme", False, model="Samsung SSD 990", children=[
                part("nvme0n1p1", "/dev/nvme0n1p1", "nvme0n1", fstype="vfat", mountpoints=["/boot"]),
                part("nvme0n1p2", "/dev/nvme0n1p2", "nvme0n1", fstype="ext4", mountpoints=["/", "/home", "/var"]),
            ]),
        ])
        rows = {row["path"]: row for row in flash.list_disks(normal, inverse, mounts, swaps)}
        self.assertFalse(rows["/dev/nvme0n1"]["safe"])
        self.assertEqual(held(rows["/dev/nvme0n1"]["reason"]), ["/", "/boot", "/home", "/var"])
        self.assertFalse(rows["/dev/nvme0n1"]["removable"])
        self.assertEqual(rows["/dev/nvme0n1"]["transport"], "nvme")

    def test_btrfs_subvolumes_refused(self):
        # Mounts come only from findmnt, with the btrfs [subvol] suffix. The
        # partition nodes have no mountpoints, so string-matching the disk is not enough.
        normal, inverse, _mounts, swaps = snap([
            disk("nvme0n1", "/dev/nvme0n1", 4000, "nvme", False, children=[
                part("nvme0n1p1", "/dev/nvme0n1p1", "nvme0n1", fstype="vfat"),
                part("nvme0n1p2", "/dev/nvme0n1p2", "nvme0n1", fstype="btrfs"),
            ]),
        ])
        mounts = {"filesystems": [{
            "target": "/", "source": "/dev/nvme0n1p2[/@]", "fstype": "btrfs",
            "children": [
                {"target": "/home", "source": "/dev/nvme0n1p2[/@home]", "fstype": "btrfs"},
                {"target": "/var", "source": "/dev/nvme0n1p2[/@var]", "fstype": "btrfs"},
                {"target": "/boot", "source": "/dev/nvme0n1p1", "fstype": "vfat"},
            ],
        }]}
        reason = flash.reason_for("/dev/nvme0n1", normal, inverse, mounts, swaps)
        self.assertCountEqual(held(reason), ["/", "/boot", "/home", "/var"])

    def test_luks_lvm_root_on_usb_refused(self):
        # Normal lsblk shows a bare USB disk. The root mount is on a LUKS/LVM
        # child that only `lsblk -s` connects back to that disk.
        normal = {"blockdevices": [
            disk("sdb", "/dev/sdb", 32_000_000_000, "usb", True, model="Cruzer"),
        ]}
        inverse = {"blockdevices": [{
            "name": "vg-root", "path": "/dev/mapper/vg-root", "type": "lvm", "size": 1000,
            "children": [{
                "name": "luks", "path": "/dev/mapper/luks", "type": "crypt",
                "children": [{
                    "name": "sdb1", "path": "/dev/sdb1", "type": "part",
                    "children": [{
                        "name": "sdb", "path": "/dev/sdb", "type": "disk",
                        "size": 32_000_000_000, "tran": "usb", "rm": True,
                    }],
                }],
            }],
        }]}
        mounts = {"filesystems": [{"target": "/", "source": "/dev/mapper/vg-root", "fstype": "ext4"}]}
        reason = flash.reason_for("/dev/sdb", normal, inverse, mounts, "")
        self.assertEqual(held(reason), ["/"])
        rows = flash.list_disks(normal, inverse, mounts, "")
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["safe"])

    def test_empty_card_reader_refused(self):
        normal, inverse, mounts, swaps = snap([
            disk("sda", "/dev/sda", 0, "usb", True, model="Mass-Storage"),
        ])
        row = flash.list_disks(normal, inverse, mounts, swaps)[0]
        self.assertFalse(row["safe"])
        self.assertIn("zero-size", row["reason"])
        self.assertEqual(row["size_bytes"], 0)

    def test_usb_stick_allowed_with_data_mount(self):
        normal, inverse, _mounts, swaps = snap([
            disk("sdc", "/dev/sdc", 16_000_000_000, "usb", True, model="Ultra", children=[
                part("sdc1", "/dev/sdc1", "sdc", fstype="vfat", mountpoints=["/run/media/user/STICK"]),
            ]),
        ])
        mounts = {"filesystems": [
            {"target": "/run/media/user/STICK", "source": "/dev/sdc1", "fstype": "vfat"},
        ]}
        row = flash.list_disks(normal, inverse, mounts, swaps)[0]
        self.assertTrue(row["safe"], row)
        self.assertEqual(row["reason"], "")
        self.assertTrue(row["removable"])
        self.assertEqual(row["model"], "Ultra")
        required = {"path", "model", "size_bytes", "transport", "removable", "safe", "reason"}
        self.assertTrue(required <= set(row))
        self.assertIsInstance(row["size_bytes"], int)

    def test_usb_transport_without_removable_flag_allowed(self):
        normal, inverse, mounts, swaps = snap([
            disk("sdd", "/dev/sdd", 8_000_000_000, "usb", False, model="Enclosure"),
        ])
        row = flash.list_disks(normal, inverse, mounts, swaps)[0]
        self.assertTrue(row["safe"], row)
        self.assertFalse(row["removable"])

    def test_internal_disk_at_run_media_refused(self):
        normal, inverse, mounts, swaps = snap([
            disk("nvme1n1", "/dev/nvme1n1", 4000, "nvme", False, children=[
                part("nvme1n1p1", "/dev/nvme1n1p1", "nvme1n1", mountpoints=["/run/media/user/storage"]),
            ]),
        ])
        row = flash.list_disks(normal, inverse, mounts, swaps)[0]
        self.assertFalse(row["safe"])
        self.assertIn("not removable", row["reason"])

    def test_usb_mounted_at_opt_refused(self):
        normal, inverse, mounts, swaps = snap([
            disk("sde", "/dev/sde", 8000, "usb", True, children=[
                part("sde1", "/dev/sde1", "sde", mountpoints=["/opt"]),
            ]),
        ])
        self.assertEqual(
            flash.reason_for("/dev/sde", normal, inverse, mounts, swaps),
            "holds /opt",
        )

    def test_swap_on_usb_refused(self):
        normal, inverse, mounts, swaps = snap(
            [disk("sdf", "/dev/sdf", 8000, "usb", True, children=[
                part("sdf1", "/dev/sdf1", "sdf", fstype="swap"),
            ])],
            swaps="Filename Type Size Used Priority\n/dev/sdf1 partition 100 0 -2\n",
        )
        self.assertIn("swap", held(flash.reason_for("/dev/sdf", normal, inverse, mounts, swaps)))

    def test_partition_and_rom_are_not_targets(self):
        normal, inverse, mounts, swaps = snap([
            disk("sdc", "/dev/sdc", 16_000_000_000, "usb", True, children=[
                part("sdc1", "/dev/sdc1", "sdc"),
            ]),
            {"name": "sr0", "path": "/dev/sr0", "type": "rom", "size": 100, "tran": "usb", "rm": True,
             "model": "DVD", "mountpoints": []},
        ])
        rows = flash.list_disks(normal, inverse, mounts, swaps)
        self.assertEqual([row["path"] for row in rows], ["/dev/sdc"])
        self.assertEqual(flash.reason_for("/dev/sdc1", normal, inverse, mounts, swaps), "not a whole disk")

    def test_iso_on_target_refused(self):
        normal, inverse, _mounts, swaps = snap([
            disk("sdc", "/dev/sdc", 16_000_000_000, "usb", True, children=[
                part("sdc1", "/dev/sdc1", "sdc"),
            ]),
            disk("nvme0n1", "/dev/nvme0n1", 4000, "nvme", False, children=[
                part("nvme0n1p2", "/dev/nvme0n1p2", "nvme0n1"),
            ]),
        ])
        mounts = {"filesystems": [
            {"target": "/", "source": "/dev/nvme0n1p2", "fstype": "ext4", "children": [
                {"target": "/run/media/user/STICK", "source": "/dev/sdc1", "fstype": "vfat"},
            ]},
        ]}
        on_stick = flash.reason_for(
            "/dev/sdc", normal, inverse, mounts, swaps, iso="/run/media/user/STICK/os.iso",
        )
        self.assertIn("ISO file resides", on_stick)
        elsewhere = flash.reason_for(
            "/dev/sdc", normal, inverse, mounts, swaps, iso="/home/user/os.iso",
        )
        self.assertEqual(elsewhere, "")

    def test_raid_across_two_disks_blocks_both(self):
        inverse = {"blockdevices": [{
            "name": "md0", "path": "/dev/md0", "type": "raid1", "size": 100,
            "children": [
                {"name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk", "size": 100, "tran": "nvme", "rm": False},
                {"name": "sdb", "path": "/dev/sdb", "type": "disk", "size": 100, "tran": "usb", "rm": True},
            ],
        }]}
        normal = {"blockdevices": [
            disk("nvme0n1", "/dev/nvme0n1", 100, "nvme", False),
            disk("sdb", "/dev/sdb", 100, "usb", True),
        ]}
        mounts = {"filesystems": [{"target": "/", "source": "/dev/md0", "fstype": "ext4"}]}
        self.assertEqual(flash.reason_for("/dev/sdb", normal, inverse, mounts, ""), "holds /")

    def test_unplaceable_root_mount_blocks_every_disk(self):
        normal, inverse, _mounts, swaps = snap([disk("sdb", "/dev/sdb", 100, "usb", True)])
        mounts = {"filesystems": [{"target": "/", "source": "/dev/root", "fstype": "ext4"}]}
        self.assertFalse(flash.list_disks(normal, inverse, mounts, swaps)[0]["safe"])

    def test_loop_mount_does_not_block(self):
        normal, inverse, _mounts, swaps = snap([
            disk("sdb", "/dev/sdb", 100, "usb", True),
            {"name": "loop0", "path": "/dev/loop0", "type": "loop", "size": 10},
        ])
        mounts = {"filesystems": [{"target": "/opt/iso", "source": "/dev/loop0", "fstype": "iso9660"}]}
        self.assertTrue(flash.list_disks(normal, inverse, mounts, swaps)[0]["safe"])

    def test_cli_list_json(self):
        normal, inverse, mounts, swaps = snap([
            disk("sdc", "/dev/sdc", 100, "usb", True, model="Ultra"),
        ])
        buf = io.StringIO()
        with mock.patch.object(flash, "load_snapshot", return_value=(normal, inverse, mounts, swaps)):
            with redirect_stdout(buf):
                code = main(["flash", "list", "--json"])
        self.assertEqual(code, 0)
        rows = json.loads(buf.getvalue())
        self.assertEqual(rows[0]["path"], "/dev/sdc")
        self.assertTrue(rows[0]["safe"])


class WriteGuardTest(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.copy()
        self._dry = system.DRY_RUN
        system.DRY_RUN = False
        os.environ.pop(flash.TEST_ENV, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        system.DRY_RUN = self._dry

    def test_a_stick_swapped_on_the_same_path_is_refused(self):
        # Picked stick A at /dev/sdb, then B was plugged in and got /dev/sdb.
        picked = flash.list_disks(*snap([dict(disk("sdb", "/dev/sdb", 8 << 30, "usb", True, model="A"), serial="1")])[:2],
                                  {"filesystems": []}, "")[0]["id"]
        swapped = snap([dict(disk("sdb", "/dev/sdb", 8 << 30, "usb", True, model="B"), serial="2")])
        real_stat = os.stat
        with tempfile.TemporaryDirectory() as tmp:
            iso = Path(tmp, "x.iso")
            iso.write_bytes(b"x" * 1024)
            fake = lambda p, *a, **k: os.stat_result((stat.S_IFBLK,) + (0,) * 9) if p == "/dev/sdb" else real_stat(p, *a, **k)
            with mock.patch.object(flash, "canon", side_effect=lambda p: p), mock.patch.object(flash.os, "stat", side_effect=fake):
                with self.assertRaises(SystemExit) as caught:
                    flash.prepare(str(iso), "/dev/sdb", "/dev/sdb", snapshot=swapped, expect_id=picked)
                self.assertIn("no longer the disk you picked", str(caught.exception))
                # The same stick still passes.
                same = snap([dict(disk("sdb", "/dev/sdb", 8 << 30, "usb", True, model="A"), serial="1")])
                self.assertEqual(flash.prepare(str(iso), "/dev/sdb", "/dev/sdb", snapshot=same, expect_id=picked)["device"], "/dev/sdb")

    def test_same_disk_ignores_a_serial_only_one_side_could_read(self):
        self.assertTrue(flash.same_disk("8:A:", "8:A:123"))
        self.assertFalse(flash.same_disk("8:A:1", "8:A:2"))
        self.assertFalse(flash.same_disk("8:A:", "8:B:"))
        self.assertFalse(flash.same_disk("8:A:", "16:A:"))

    def test_root_argv_passes_the_picked_disk(self):
        argv = flash.root_argv("/tmp/x.iso", "/dev/sdb", "/dev/sdb", expect_id="8:A:1")
        self.assertEqual(argv[-2:], ["--expect-id", "8:A:1"])

    def test_confirm_must_match_exactly(self):
        with self.assertRaises(SystemExit) as caught:
            flash.prepare("missing.iso", "/dev/sdb", "/dev/sdc")
        self.assertIn("confirm", str(caught.exception))
        with mock.patch.object(system, "as_root", side_effect=AssertionError("escalated")):
            with self.assertRaises(SystemExit):
                flash.cmd_write(argparse_ns("missing.iso", "/dev/sdb", "/dev/sdc"))

    def test_test_mode_refuses_dev_and_does_not_escalate(self):
        os.environ[flash.TEST_ENV] = "1"
        with mock.patch.object(system, "as_root", side_effect=AssertionError("escalated")):
            with self.assertRaises(SystemExit) as caught:
                flash.cmd_write(argparse_ns("missing.iso", "/dev/sda", "/dev/sda"))
        self.assertIn("/dev", str(caught.exception))
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / ".work" / "stick"
            link.parent.mkdir()
            link.symlink_to("/dev/sda")
            iso = link.parent / "tiny.iso"
            iso.write_bytes(b"iso")
            with self.assertRaises(SystemExit) as caught:
                flash.prepare(str(iso), str(link), str(link))
            self.assertIn("/dev", str(caught.exception))

    def test_test_mode_refuses_file_outside_work_and_zero_size(self):
        os.environ[flash.TEST_ENV] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "tiny.iso"
            iso.write_bytes(b"iso-bytes")
            outside = root / "disk.img"
            outside.write_bytes(b"\0" * 64)
            with self.assertRaises(SystemExit) as caught:
                flash.prepare(str(iso), str(outside), str(outside))
            self.assertIn(".work", str(caught.exception))
            target = root / ".work" / "empty.img"
            target.parent.mkdir()
            target.write_bytes(b"")
            with self.assertRaises(SystemExit) as caught:
                flash.prepare(str(iso), str(target), str(target))
            self.assertIn("zero-size", str(caught.exception))

    def test_oversized_image_refused(self):
        os.environ[flash.TEST_ENV] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / ".work"
            work.mkdir()
            iso = work / "big.iso"
            iso.write_bytes(b"x" * 200)
            target = work / "small.img"
            target.write_bytes(b"\0" * 100)
            with self.assertRaises(SystemExit) as caught:
                flash.prepare(str(iso), str(target), str(target))
            self.assertIn("200", str(caught.exception))

    def test_file_target_does_not_escalate(self):
        os.environ[flash.TEST_ENV] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / ".work"
            work.mkdir()
            iso = work / "tiny.iso"
            iso.write_bytes(b"x" * 32)
            target = work / "disk.img"
            target.write_bytes(b"\0" * 64)
            with mock.patch.object(system, "as_root", side_effect=AssertionError("escalated")):
                with mock.patch.object(flash, "detect_mode", return_value=("raw", [])):
                    with mock.patch.object(flash, "write_raw") as write_raw:
                        code = flash.cmd_write(argparse_ns(str(iso), str(target), str(target)))
            self.assertEqual(code, 0)
            write_raw.assert_called_once()

    def run_real_writer(self, stdin, *flags):
        """Start the real CLI on a large sparse ISO and a regular file. Returns its process and the file."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        work = Path(tmp.name) / ".work"
        work.mkdir()
        iso, target = work / "big.iso", work / "disk.img"
        size = 64 * 1024 * 1024
        with open(iso, "wb") as handle:
            handle.truncate(size - 3)
            handle.seek(0, os.SEEK_END)
            handle.write(b"END")
        with open(target, "wb") as handle:
            handle.truncate(size)
        # 7z may be missing in CI and can't read this file anyway, so only the mode check is faked.
        code = ("import sys; from unittest import mock; from tuxutil import flash; from tuxutil.cli import main;"
                "mock.patch.object(flash, 'detect_mode', return_value=('raw', [])).start(); sys.exit(main(sys.argv[1:]))")
        proc = subprocess.Popen(
            [sys.executable, "-c", code, "flash", "write", str(iso), str(target), "--confirm", str(target), "--no-verify", *flags],
            stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT,
            env={**os.environ, flash.TEST_ENV: "1"},
        )
        self.addCleanup(proc.stdout.close)
        return proc, target

    def test_closing_stdin_stops_the_real_writer(self):
        proc, target = self.run_real_writer(subprocess.PIPE, "--cancel-on-eof")
        output, _ = proc.communicate(timeout=60)  # Closes stdin.
        self.assertEqual(proc.returncode, 143, output)
        self.assertIn("cancelled", output)
        self.assertFalse(target.read_bytes().endswith(b"END"))

    def test_cancel_also_stops_a_child_nobody_is_waiting_for(self):
        # What a cancel leaves behind if it lands while subprocess is still starting 7z.
        code = ("import subprocess, time; from tuxutil import flash; flash.cancel_on_stdin_eof();"
                "print(subprocess.Popen(['sleep', '60']).pid, flush=True); time.sleep(60)")
        proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=ROOT)
        self.addCleanup(proc.stdout.close)
        child = int(proc.stdout.readline())
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=20), 143)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_cancel_gives_up_on_a_child_that_ignores_sigterm(self):
        code = ("import subprocess, time; from tuxutil import flash; flash.STOP_WAIT = 0.3; flash.cancel_on_stdin_eof();"
                "subprocess.Popen(['sh', '-c', 'trap \"\" TERM; echo up; exec sleep 60']); time.sleep(60)")
        proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=ROOT)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(os.killpg, proc.pid, signal.SIGKILL)
        self.assertEqual(proc.stdout.readline().strip(), "up")
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=20), 143)

    def test_closed_stdin_cannot_stop_a_flash_unless_asked(self):
        proc, target = self.run_real_writer(subprocess.DEVNULL)
        output, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, output)
        self.assertNotIn("cancelled", output)
        self.assertTrue(target.read_bytes().endswith(b"END"))

    def test_raw_write_round_trip(self):
        os.environ[flash.TEST_ENV] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / ".work"
            work.mkdir()
            payload = os.urandom(64 * 1024)
            iso = work / "payload.iso"
            iso.write_bytes(payload)
            target = work / "disk.img"
            target.write_bytes(b"\0" * len(payload))
            flash.write_raw(str(iso), str(target), False, verify=True)
            self.assertEqual(target.read_bytes(), payload)

    def test_dry_run_root_side_never_unmounts_or_writes(self):
        system.DRY_RUN = True
        plan = {"device": "/dev/sdz", "block": True}
        with mock.patch.object(flash, "prepare", return_value=plan), \
                mock.patch.object(flash, "unmount_target", side_effect=AssertionError("unmounted")), \
                mock.patch.object(flash, "write_raw", side_effect=AssertionError("wrote")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(flash.cmd_write_root(argparse_ns("a.iso", "/dev/sdz", "/dev/sdz")), 0)

    def test_dry_run_list_still_reads_disks(self):
        system.DRY_RUN = True
        with mock.patch.object(system, "query") as query:
            query.return_value = subprocess.CompletedProcess([], 0, '{"blockdevices": []}', "")
            self.assertEqual(flash.capture(["lsblk"]), '{"blockdevices": []}')

    def test_too_small_target_refused_before_it_is_wiped(self):
        os.environ[flash.TEST_ENV] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / ".work"
            work.mkdir()
            os.environ[flash.WORK_ENV] = str(work)
            iso = work / "win.iso"
            iso.write_bytes(b"x" * 8)
            target = work / "stick.img"
            target.write_bytes(b"\0" * 1024)

            def fake_extract(_iso, dest):
                (dest / "sources").mkdir(parents=True)
                (dest / "sources" / "boot.wim").write_bytes(b"x" * 4096)

            with mock.patch.object(flash, "extract_iso", fake_extract), \
                    mock.patch.object(flash, "partition_and_format", side_effect=AssertionError("wiped")), \
                    mock.patch.object(system, "require_tools"), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    flash.write_windows(str(iso), str(target), str(target))
            self.assertIn("4096", str(caught.exception))
            self.assertEqual([p.name for p in work.glob("tuxutil-flash-*")], [])

    def test_root_argv_embeds_package_path_and_imports(self):
        argv = flash.root_argv("/tmp/x.iso", "/dev/sdb", "/dev/sdb", no_verify=True)
        parent = str(Path(flash.__file__).resolve().parent.parent)
        self.assertEqual(argv[0], system.self_cmd()[0])
        self.assertEqual(argv[1:3], ["-I", "-c"])
        self.assertIn(parent, argv[3])
        self.assertNotIn("PYTHONPATH", " ".join(argv))
        self.assertNotIn(flash.TEST_ENV, " ".join(argv))
        self.assertEqual(argv[4:7], ["flash", "_write", str(Path("/tmp/x.iso").resolve())])
        self.assertIn("--confirm", argv)
        self.assertIn("--no-verify", argv)
        self.assertEqual(argv.count("/dev/sdb"), 2)
        wrapped = system.as_root(argv)
        self.assertIsInstance(wrapped, list)
        self.assertNotIn("sh", wrapped)
        self.assertNotIn("-c", [wrapped[0]])
        self.assertTrue(all(isinstance(part, str) for part in wrapped))
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        proc = subprocess.run(
            [*argv[:4], "--version"], cwd="/", env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(__version__, proc.stdout)

    def test_root_argv_ignores_hostile_files_in_the_current_folder(self):
        """sudo and doas keep the caller's folder, so a json.py there would otherwise run as root."""
        argv = flash.root_argv("/tmp/x.iso", "/dev/sdb", "/dev/sdb")
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "ran"
            (Path(tmp) / "json.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
            (Path(tmp) / "argparse.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
            # PYTHONPATH=. reaches the child under sudo -E or env_keep, and comes back as an absolute path.
            for pythonpath in (None, ".", tmp):
                env = os.environ.copy()
                env.pop("PYTHONPATH", None)
                if pythonpath:
                    env["PYTHONPATH"] = pythonpath
                proc = subprocess.run([*argv[:4], "--version"], cwd=tmp, env=env, capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(__version__, proc.stdout)
                self.assertFalse(marker.exists(), pythonpath)

    def test_mode_prefers_hybrid_raw(self):
        self.assertEqual(flash.mode_from_flags(True, True), "raw")
        self.assertEqual(flash.mode_from_flags(True, False), "raw")
        self.assertEqual(flash.mode_from_flags(False, True), "windows")
        self.assertEqual(flash.mode_from_flags(False, False), "raw")

    def test_windows_install_names(self):
        records = [
            {"Path": "README.TXT", "Size": "10"},
            {"Path": "sources/install.wim", "Size": "8158846937"},
            {"Path": "sources/boot.wim", "Size": "100"},
            {"Path": "sources/install.swm", "Size": "20", "Folder": "-"},
        ]
        found = flash.windows_install_files(records)
        self.assertEqual([item[0] for item in found], ["sources/install.wim", "sources/install.swm"])
        self.assertGreater(found[0][1], flash.FAT32_MAX_FILE)


@unittest.skipUnless(
    all(shutil.which(tool) for tool in ("sfdisk", "mkfs.vfat", "mcopy")),
    "sfdisk, mkfs.vfat and mcopy are required",
)
class FatCopyTest(unittest.TestCase):
    def test_copy_and_verify_on_image_file(self):
        os.environ[flash.TEST_ENV] = "1"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                work = root / ".work"
                src = work / "tree"
                (src / "efi" / "boot").mkdir(parents=True)
                (src / "sources").mkdir()
                (src / "efi" / "boot" / "bootx64.efi").write_bytes(b"signed-bootloader")
                (src / "sources" / "boot.wim").write_bytes(b"boot-wim-bytes")
                (src / "setup.exe").write_bytes(b"setup")
                target = work / "disk.img"
                target.write_bytes(b"\0" * (64 * 1024 * 1024))
                with mock.patch.object(system, "run", wraps=system.run) as run:
                    _offset, length = flash.partition_and_format(str(target), False)
                # A stick with old partitions has child nodes, which image files never show.
                self.assertIn("-I", next(c.args[0] for c in run.call_args_list if c.args[0][0] == "mkfs.vfat"))
                self.assertGreater(length, 32 * 1024 * 1024)
                # Windows does not mount an EFI System Partition, so Setup would fail.
                table = json.loads(subprocess.check_output(["sfdisk", "--json", str(target)]))
                self.assertEqual(table["partitiontable"]["label"], "dos")
                self.assertEqual(table["partitiontable"]["partitions"][0]["type"], "c")
                flash.copy_tree(src, str(target), _offset)
                flash.verify_tree(src, str(target), _offset)
                self.assertTrue(stat.S_ISREG(target.stat().st_mode))
        finally:
            os.environ.pop(flash.TEST_ENV, None)


def argparse_ns(iso, device, confirm, no_verify=False, json=False):
    return mock.Mock(iso=iso, device=device, confirm=confirm, no_verify=no_verify, json=json, cancel_on_eof=False,
                     expect_id=None)


class HybridBootTest(unittest.TestCase):
    """Summaries are xorriso's real output for the ISO named."""

    def test_summaries(self):
        for text, want in (
            ("System area summary: MBR isohybrid cyl-align-off GPT\n", True),  # Arch
            ("System area summary: MBR protective-msdos-label grub2-mbr cyl-align-off GPT\n", True),  # Ubuntu
            ("System area summary: MBR grub2-mbr cyl-align-off\n", True),  # Manjaro
            ("xorriso : NOTE : No System Area was loaded\n", False),  # Windows 11, Tails
        ):
            done = subprocess.CompletedProcess([], 0, text, "")
            with mock.patch.object(flash.subprocess, "run", return_value=done):
                self.assertEqual(flash.hybrid_boot("x.iso"), want, text)


if __name__ == "__main__":
    unittest.main()
