"""Write an ISO to a USB stick, safely.

Whole disks only. The target must be removable or USB, and must not hold a
system mount or swap. `flash write` checks again immediately before writing.
The root re-exec (`flash _write`) repeats those checks, so they cannot be
skipped by calling the writer directly.
"""
import argparse
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

from . import system

TEST_ENV = "TUXUTIL_FLASH_TEST_FILE"
WORK_ENV = "TUXUTIL_FLASH_WORK"
# FAT32 cannot store a file of 4 GiB or larger (the on-disk limit is 2**32-1).
FAT32_MAX_FILE = 2**32 - 1
PROTECTED = ("/", "/boot", "/home", "/usr", "/var")
DATA_MOUNTS = ("/media", "/run/media", "/mnt")
INSTALL_NAMES = {"install.wim", "install.esd", "install.swm"}
LSBLK_COLS = "NAME,PATH,PKNAME,TYPE,SIZE,TRAN,RM,MODEL,FSTYPE,MOUNTPOINTS"
DEFAULT_WORK = Path("/home/susiee/Projects/tuxutil/.work/flash")


def refuse(message):
    raise SystemExit(f"refusing: {message}")


def canon(path):
    if not path:
        return path
    try:
        return os.path.realpath(path)
    except OSError:
        return os.path.normpath(path)


def is_under_dev(path):
    """True for /dev and anything that resolves into it, including symlinks."""
    for candidate in (path, os.path.normpath(path), canon(path)):
        if candidate == "/dev" or str(candidate).startswith("/dev/"):
            return True
    return False


def is_under_work(path):
    return ".work" in Path(canon(path)).parts


def as_bool(value):
    return value in (True, 1, "1", "true", "True")


def device_from_source(source):
    """Block device from a findmnt source. Strips the btrfs subvol suffix.

    findmnt prints btrfs as ``/dev/nvme0n1p2[/@home]``. The bracket is not part
    of the device path. Non-device sources (tmpfs, sysfs, …) return None.
    """
    if not source or not isinstance(source, str) or not source.startswith("/dev/"):
        return None
    return source.split("[", 1)[0]


def capture(cmd):
    """Run a read-only query without echoing it.

    ``system.run`` prints the command on stdout, which would corrupt
    ``flash list --json``. Still no shell.
    """
    if system.DRY_RUN:
        system.run(cmd)
        return ""
    try:
        proc = subprocess.run([str(c) for c in cmd], check=False, capture_output=True, text=True)
    except FileNotFoundError:
        refuse(f"missing tool {cmd[0]}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        refuse(f"{cmd[0]} failed: {detail}")
    return proc.stdout


def load_snapshot():
    """Live lsblk (normal and inverse), findmnt, and swap devices.

    Parent links come from both trees. ``lsblk -s`` is what resolves a mount
    on a LUKS/LVM/md/btrfs child back to the disk that holds it.
    """
    normal = json.loads(capture(["lsblk", "--json", "--bytes", "-o", LSBLK_COLS]))
    inverse = json.loads(capture(["lsblk", "-s", "--json", "--bytes", "-o", LSBLK_COLS]))
    mounts = json.loads(capture(["findmnt", "-J", "-o", "TARGET,SOURCE,FSTYPE"]))
    try:
        swaps = Path("/proc/swaps").read_text()
    except OSError as err:
        refuse(f"cannot read /proc/swaps: {err}")
    return normal, inverse, mounts, swaps


def walk(node):
    if not isinstance(node, dict):
        return
    yield node
    for child in node.get("children") or []:
        yield from walk(child)


def walk_doc(doc):
    for top in (doc or {}).get("blockdevices") or []:
        yield from walk(top)


def build_topology(normal, inverse):
    """Return (devices, parents). parents maps a device path to its parent path."""
    devices = {}
    by_name = {}
    for doc in (normal, inverse):
        for node in walk_doc(doc):
            path = node.get("path")
            if not path:
                continue
            current = devices.setdefault(path, {"path": path})
            for key, value in node.items():
                if key == "children":
                    continue
                if value is not None and value != "" and value != []:
                    current[key] = value
                elif key not in current:
                    current[key] = value
            if node.get("name"):
                by_name.setdefault(node["name"], path)
    parents = {}
    for node in walk_doc(normal):
        # pkname in the normal tree is the real parent. Ignore it on the disk
        # itself. The inverse tree flips pkname, so it is not used here.
        if node.get("type") == "disk":
            continue
        parent_name = node.get("pkname")
        parent = by_name.get(parent_name) if parent_name else None
        if node.get("path") and parent:
            parents.setdefault(canon(node["path"]), canon(parent))
    for node in walk_doc(inverse):
        # In `lsblk -s`, the children of a node are its parents.
        if node.get("type") == "disk":
            continue
        kids = node.get("children") or []
        if node.get("path") and kids and kids[0].get("path"):
            parents.setdefault(canon(node["path"]), canon(kids[0]["path"]))
    return devices, parents


def node_for(path, devices):
    if path in devices:
        return devices[path]
    wanted = canon(path)
    for key, node in devices.items():
        if canon(key) == wanted:
            return node
    return None


def ancestor_disks(path, devices, parents):
    """Walk parents until a whole disk. Cycles return nothing (fail closed)."""
    seen = set()
    current = canon(device_from_source(path) or path)
    while current and current not in seen:
        seen.add(current)
        node = node_for(current, devices)
        if node and node.get("type") == "disk":
            return [canon(node.get("path") or current)]
        nxt = parents.get(current)
        if not nxt or canon(nxt) == current:
            return []
        current = canon(nxt)
    return []


def flatten_findmnt(doc):
    found = []

    def visit(node):
        if not isinstance(node, dict):
            return
        if node.get("target"):
            found.append(node)
        for child in node.get("children") or []:
            visit(child)

    for top in (doc or {}).get("filesystems") or []:
        visit(top)
    return found


def parse_swaps(text):
    devices = []
    for line in (text or "").splitlines()[1:]:
        parts = line.split()
        if parts and parts[0].startswith("/dev/"):
            devices.append(parts[0])
    return devices


def mount_labels(node):
    labels = []
    points = node.get("mountpoints")
    if points is None and node.get("mountpoint"):
        points = [node.get("mountpoint")]
    for point in points or []:
        if not point:
            continue
        labels.append("swap" if point == "[SWAP]" else point)
    if node.get("fstype") == "swap" and "swap" not in labels:
        labels.append("swap")
    return labels


def mounts_by_disk(devices, parents, findmnt_doc, swaps_text):
    """Map each whole disk to the mount targets that resolve onto it."""
    result = {}

    def add(dev, label):
        if not dev or not label:
            return
        for disk in ancestor_disks(dev, devices, parents):
            result.setdefault(disk, set()).add(label)

    for mount in flatten_findmnt(findmnt_doc):
        add(device_from_source(mount.get("source")), mount.get("target"))
    for node in devices.values():
        for label in mount_labels(node):
            add(node.get("path"), label)
    for dev in parse_swaps(swaps_text):
        add(dev, "swap")
    return result


def is_swap(label):
    return label in {"swap", "[SWAP]"}


def is_protected(label):
    if is_swap(label):
        return True
    for root in PROTECTED:
        if label == root or label.startswith(root + "/"):
            return True
    return False


def is_data_mount(label):
    # ponytail: a removable data mount is only /media, /run/media, or /mnt.
    # An internal disk mounted there still fails the removable/USB check.
    if is_swap(label):
        return False
    for root in DATA_MOUNTS:
        if label == root or label.startswith(root + "/"):
            return True
    return False


def bad_labels(labels):
    shown = []
    seen = set()
    for label in sorted(labels, key=lambda item: (item != "/", item)):
        if is_swap(label):
            text = "swap"
        elif is_protected(label):
            text = next(root for root in PROTECTED if label == root or label.startswith(root + "/"))
        elif is_data_mount(label):
            continue
        else:
            text = label
        if text not in seen:
            seen.add(text)
            shown.append(text)
    return shown


def disk_reason(node, labels):
    if node.get("type") != "disk":
        return "not a whole disk"
    try:
        size = int(node.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return "zero-size device (no media)"
    bad = bad_labels(labels)
    if bad:
        return "holds " + ", ".join(bad)
    transport = node.get("tran") or ""
    if not isinstance(transport, str):
        transport = ""
    if not as_bool(node.get("rm")) and transport.lower() != "usb":
        return f"not removable (transport {transport.lower() or 'unknown'})"
    return ""


def list_disks(normal, inverse, findmnt_doc, swaps_text):
    devices, parents = build_topology(normal, inverse)
    mounts = mounts_by_disk(devices, parents, findmnt_doc, swaps_text)
    rows = []
    seen = set()
    for node in walk_doc(normal):
        if node.get("type") != "disk":
            continue
        path = node.get("path")
        if not path or path in seen:
            continue
        seen.add(path)
        merged = devices.get(path, node)
        reason = disk_reason(merged, mounts.get(canon(path), set()))
        transport = merged.get("tran") or ""
        if not isinstance(transport, str):
            transport = ""
        try:
            size = int(merged.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        rows.append({
            "path": path,
            "model": str(merged.get("model") or "").strip(),
            "size_bytes": size,
            "transport": transport,
            "removable": as_bool(merged.get("rm")),
            "safe": reason == "",
            "reason": reason,
        })
    return rows


def disks_holding_path(path, devices, parents, findmnt_doc):
    real = canon(path)
    best = None
    for mount in flatten_findmnt(findmnt_doc):
        target = mount.get("target") or ""
        if real == target or real.startswith(target.rstrip("/") + "/"):
            if best is None or len(target) > len(best.get("target") or ""):
                best = mount
    if not best:
        return []
    return ancestor_disks(best.get("source"), devices, parents)


def reason_for(path, normal, inverse, findmnt_doc, swaps_text, iso=None):
    devices, parents = build_topology(normal, inverse)
    node = node_for(path, devices)
    if node is None:
        return "not a known disk"
    if node.get("type") != "disk":
        return "not a whole disk"
    reason = disk_reason(node, mounts_by_disk(devices, parents, findmnt_doc, swaps_text).get(canon(path), set()))
    if reason:
        return reason
    if iso and canon(path) in disks_holding_path(iso, devices, parents, findmnt_doc):
        return "ISO file resides on the target disk"
    return ""


def require_confirm(device, confirm):
    if confirm != device:
        refuse(f"--confirm {confirm!r} does not exactly match the device {device!r}")


def test_mode():
    return os.environ.get(TEST_ENV) == "1"


def prepare(iso, device, confirm, snapshot=None):
    """Every safety check, except the final unmount. Raises SystemExit."""
    require_confirm(device, confirm)
    iso_real = canon(iso)
    if not os.path.isfile(iso_real):
        refuse(f"ISO not found: {iso}")
    if test_mode() and (is_under_dev(device) or is_under_dev(canon(device))):
        # ponytail: the test-file switch never opens /dev, even if a check below
        # would have allowed that disk. Real sticks do not use this switch.
        refuse("test mode cannot target /dev")
    real = canon(device)
    try:
        st = os.stat(real)
    except OSError as err:
        refuse(f"cannot stat {device}: {err}")
    block = stat.S_ISBLK(st.st_mode)
    if block:
        if snapshot is None:
            snapshot = load_snapshot()
        reason = reason_for(real, *snapshot, iso=iso_real)
        if reason:
            refuse(f"{real}: {reason}")
        size = _block_size(real, snapshot)
    elif test_mode() and stat.S_ISREG(st.st_mode):
        if is_under_dev(real) or not is_under_work(real):
            refuse("test mode only allows a regular file under .work")
        size = st.st_size
    else:
        refuse(f"{real} is not a block device")
    if size <= 0:
        refuse(f"{real} is zero-size (no media)")
    iso_size = os.path.getsize(iso_real)
    if iso_size <= 0:
        refuse("ISO is empty")
    if iso_size > size:
        refuse(f"ISO is {iso_size} bytes but the target is {size} bytes")
    return {
        "iso": iso_real,
        "device": real,
        "device_arg": device,
        "confirm": confirm,
        "size": size,
        "iso_size": iso_size,
        "block": block,
    }


def _block_size(path, snapshot):
    devices, _parents = build_topology(snapshot[0], snapshot[1])
    node = node_for(path, devices)
    if not node:
        refuse(f"{path} disappeared from lsblk")
    try:
        return int(node.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def unmount_one(device, mountpoint):
    if device and shutil.which("udisksctl"):
        proc = subprocess.run(
            ["udisksctl", "unmount", "-b", device],
            check=False, capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return True
    for target in (mountpoint, device):
        if not target or target in {"swap", "[SWAP]"}:
            continue
        proc = subprocess.run(["umount", target], check=False, capture_output=True, text=True)
        if proc.returncode == 0:
            return True
    return False


def unmount_target(device):
    """Unmount filesystems that resolve onto this disk. Abort if any remain."""
    snapshot = load_snapshot()
    devices, parents = build_topology(snapshot[0], snapshot[1])
    wanted = canon(device)
    pending = []
    for mount in flatten_findmnt(snapshot[2]):
        disks = ancestor_disks(mount.get("source"), devices, parents)
        if wanted in disks:
            pending.append((device_from_source(mount.get("source")), mount.get("target")))
    for node in devices.values():
        if wanted not in ancestor_disks(node.get("path"), devices, parents):
            continue
        for label in mount_labels(node):
            if label in {"swap", "[SWAP]"}:
                continue
            pending.append((node.get("path"), label))
    seen = set()
    for dev, mountpoint in pending:
        key = (dev, mountpoint)
        if key in seen:
            continue
        seen.add(key)
        if not unmount_one(dev, mountpoint):
            refuse(f"could not unmount {mountpoint or dev} from {device}")
    if not seen:
        return
    again = load_snapshot()
    devices, parents = build_topology(again[0], again[1])
    for mount in flatten_findmnt(again[2]):
        if wanted in ancestor_disks(mount.get("source"), devices, parents):
            refuse(f"{mount.get('target')} is still mounted on {device}")


def prepare_for_write(iso, device, confirm):
    """Check, unmount, then check again immediately before any write."""
    plan = prepare(iso, device, confirm)
    if plan["block"]:
        unmount_target(plan["device"])
    return prepare(iso, device, confirm)


def root_argv(iso, device, confirm, no_verify=False):
    """Argv of the root-side writer.

    pkexec keeps argv and drops the environment, including PYTHONPATH, so a
    checkout is invisible to ``python -m tuxutil``. The ``-c`` script inserts
    the absolute directory that contains the tuxutil package. The test-file
    switch is intentionally not passed: the root process must not enter it.
    ponytail: this assumes pkexec preserves argv. A polkit rule that only
    allows a packaged console helper would need that helper instead.
    """
    exe = system.self_cmd()[0]
    parent = str(Path(__file__).resolve().parent.parent)
    code = (
        "import sys; "
        f"sys.path.insert(0, {parent!r}); "
        "sys.argv = ['tuxutil', *sys.argv[1:]]; "
        "from tuxutil.cli import main; "
        "raise SystemExit(main())"
    )
    argv = [exe, "-c", code, "flash", "_write", str(Path(iso).resolve()), device, "--confirm", confirm]
    if no_verify:
        argv.append("--no-verify")
    return argv


def mode_from_flags(hybrid, has_windows):
    # An image that is both hybrid and a Windows ISO is raw-copied.
    if hybrid:
        return "raw"
    if has_windows:
        return "windows"
    return "raw"


def hybrid_boot(iso):
    """True when xorriso reports an isohybrid system area.

    This does not list files. Microsoft ISOs have no hybrid system area;
    their files are read with 7z.
    """
    proc = subprocess.run(
        ["xorriso", "-osirrox", "on", "-indev", iso, "-report_system_area", "plain"],
        check=False, capture_output=True, text=True,
    )
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0 and "isohybrid" not in text.lower() and "System area" not in text and "No System Area" not in text:
        refuse(f"xorriso could not inspect {iso}: {text.strip()}")
    return "isohybrid" in text.lower()


def parse_7z_slt(text):
    records = []
    current = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if " = " in line:
            key, value = line.split(" = ", 1)
            current[key] = value
    if current:
        records.append(current)
    return records


def windows_install_files(records):
    found = []
    for record in records:
        if record.get("Folder") == "+":
            continue
        path = (record.get("Path") or "").replace("\\", "/").lstrip("./")
        parent, _, name = path.rpartition("/")
        if parent.lower() != "sources" and not parent.lower().endswith("/sources"):
            continue
        lower = name.lower()
        if lower not in INSTALL_NAMES and not (lower.startswith("install") and lower.endswith(".swm")):
            continue
        try:
            size = int(record.get("Size") or 0)
        except ValueError:
            size = 0
        found.append((path, size))
    return found


def list_iso(iso):
    system.require_tools(["7z"])
    proc = subprocess.run(["7z", "l", "-slt", "-ba", iso], check=False, capture_output=True, text=True)
    # 7z uses exit status 1 for warnings (for example a quirky El Torito image).
    if proc.returncode > 1:
        refuse(f"7z could not list {iso}: {(proc.stderr or '').strip()}")
    return parse_7z_slt(proc.stdout)


def detect_mode(iso):
    system.require_tools(["xorriso", "7z"])
    files = windows_install_files(list_iso(iso))
    hybrid = hybrid_boot(iso)
    mode = mode_from_flags(hybrid, bool(files))
    if mode == "windows":
        print("flash: mode windows (UEFI FAT32 copy; ISO has no hybrid boot)", flush=True)
    elif hybrid:
        print("flash: mode raw (hybrid ISO)", flush=True)
    else:
        print("flash: mode raw (no Windows install image)", flush=True)
    return mode, files


def drop_cache(fd, path):
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    if stat.S_ISBLK(os.fstat(fd).st_mode) and shutil.which("blockdev"):
        subprocess.run(["blockdev", "--flushbufs", path], check=False, capture_output=True)


def sha256_file(path, length=None, on_progress=None):
    digest = hashlib.sha256()
    remaining = length
    done = 0
    with open(path, "rb") as handle:
        while True:
            chunk_n = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            if chunk_n <= 0:
                break
            block = handle.read(chunk_n)
            if not block:
                break
            digest.update(block)
            done += len(block)
            if remaining is not None:
                remaining -= len(block)
            if on_progress:
                on_progress(done)
    if length is not None and done != length:
        refuse(f"short read of {path}: got {done} of {length} bytes")
    return digest.hexdigest()


def assert_writable_target(path, block):
    """Last check before a byte is written. Test mode never opens /dev or a block device."""
    real = canon(path)
    info = os.stat(real)
    under_dev = is_under_dev(real)
    if test_mode() and (block or under_dev or stat.S_ISBLK(info.st_mode)):
        refuse("test mode cannot target /dev")
    if block:
        if not stat.S_ISBLK(info.st_mode) or not under_dev:
            refuse(f"{real} is not a /dev block device")
        return real
    if not test_mode() or under_dev or not stat.S_ISREG(info.st_mode) or not is_under_work(real):
        refuse("test mode cannot target /dev" if under_dev else f"{real} is not a test file under .work")
    return real


def _open_target(path, block):
    return open(assert_writable_target(path, block), "r+b", buffering=1024 * 1024)


def write_raw(iso, target, block, verify=True):
    total = os.path.getsize(iso)
    written = 0
    source_hash = hashlib.sha256()
    with open(iso, "rb") as src, _open_target(target, block) as dst:
        while True:
            block_bytes = src.read(1024 * 1024)
            if not block_bytes:
                break
            dst.write(block_bytes)
            source_hash.update(block_bytes)
            previous = written
            written += len(block_bytes)
            step = 16 * 1024 * 1024
            if total and (written == total or written // step != previous // step):
                system.progress(min(70, int(written * 70 / total)), "writing")
        dst.flush()
        os.fsync(dst.fileno())
        if verify:
            drop_cache(dst.fileno(), target)
    if written != total:
        refuse(f"short write: {written} of {total} bytes")
    if not verify:
        system.progress(100, "written")
        return
    system.progress(75, "verifying")

    def on_progress(done):
        if total and (done == total or done % (32 * 1024 * 1024) < 1024 * 1024):
            system.progress(75 + int(25 * done / total), "verifying")

    readback = sha256_file(target, length=total, on_progress=on_progress)
    if readback != source_hash.hexdigest():
        refuse("SHA-256 of the target does not match the ISO")
    print(f"flash: verified raw write {readback}", flush=True)
    system.progress(100, "verified")


def scratch_dir():
    path = Path(os.environ[WORK_ENV]) if os.environ.get(WORK_ENV) else DEFAULT_WORK
    path.mkdir(parents=True, exist_ok=True)
    return path


def mtools_env():
    env = os.environ.copy()
    env["MTOOLS_SKIP_CHECK"] = "1"
    return env


def copy_sparse(src, dst, dst_offset):
    """Copy allocated bytes only, so a large FAT image stays sparse."""
    size = os.path.getsize(src)
    with open(src, "rb") as source, open(dst, "r+b") as target:
        pos = 0
        while pos < size:
            try:
                data_at = os.lseek(source.fileno(), pos, os.SEEK_DATA)
            except OSError:
                break
            try:
                hole = os.lseek(source.fileno(), data_at, os.SEEK_HOLE)
            except OSError:
                hole = size
            hole = min(hole, size)
            source.seek(data_at)
            target.seek(dst_offset + data_at)
            left = hole - data_at
            while left:
                chunk = source.read(min(1024 * 1024, left))
                if not chunk:
                    break
                target.write(chunk)
                left -= len(chunk)
            pos = hole
        target.flush()
        os.fsync(target.fileno())


def partition_and_format(target, work, block):
    """GPT with one EFI System partition, formatted FAT32, embedded at its offset.

    No loop device: those need root, and the same offset path works on a file.
    ponytail: UEFI only. The ISO's own bootx64.efi is the Secure Boot loader.
    There is no legacy BIOS boot sector and no third-party bootloader.
    """
    target = assert_writable_target(target, block)
    script = "label: gpt\nstart=2048, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B\n"
    system.run(
        ["sfdisk", "--wipe", "always", "--wipe-partitions", "always", target],
        input=script, text=True,
    )
    info = json.loads(capture(["sfdisk", "--json", target]))
    table = info["partitiontable"]
    parts = table.get("partitions") or []
    if len(parts) != 1:
        refuse(f"expected one partition, sfdisk produced {len(parts)}")
    sector = int(table.get("sectorsize") or 512)
    offset = int(parts[0]["start"]) * sector
    length = int(parts[0]["size"]) * sector
    fat = Path(work) / "fat.img"
    with open(fat, "wb") as handle:
        handle.truncate(length)
    system.run(["mkfs.vfat", "-F", "32", "-n", "TUXUTIL", str(fat)])
    copy_sparse(fat, target, offset)
    fat.unlink()
    return offset, length


def image_spec(target, offset):
    return f"{target}@@{offset}"


def copy_tree(src, target, offset):
    entries = sorted(path for path in Path(src).iterdir())
    files = [path for path in Path(src).rglob("*") if path.is_file()]
    if not files:
        refuse("ISO extract has no files")
    # -o: don't prompt when a name already exists. stdin is closed so mtools
    # cannot fall back to an interactive question.
    cmd = ["mcopy", "-b", "-s", "-n", "-o", "-i", image_spec(target, offset), *map(str, entries), "::/"]
    system.run(cmd, env=mtools_env(), stdin=subprocess.DEVNULL)


def verify_tree(src, target, offset):
    spec = image_spec(target, offset)
    files = [path for path in Path(src).rglob("*") if path.is_file()]
    total = sum(path.stat().st_size for path in files) or 1
    done = 0
    for path in files:
        rel = path.relative_to(src).as_posix()
        proc = subprocess.Popen(
            ["mcopy", "-n", "-i", spec, f"::/{rel}", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=mtools_env(),
        )
        digest = hashlib.sha256()
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        _out, err = proc.communicate()
        if proc.returncode != 0:
            refuse(f"could not read back {rel}: {err.decode(errors='replace').strip()}")
        want = sha256_file(path)
        if digest.hexdigest() != want:
            refuse(f"verify failed for {rel}")
        done += path.stat().st_size
        system.progress(80 + int(20 * done / total), "verifying")
    print(f"flash: verified {len(files)} files", flush=True)


def find_named(root, relative):
    current = Path(root)
    for part in relative.split("/"):
        if not current.is_dir():
            return None
        match = next((child for child in current.iterdir() if child.name.lower() == part.lower()), None)
        if match is None:
            return None
        current = match
    return current


def split_oversized_wims(tree):
    sources = find_named(tree, "sources")
    if sources is None:
        refuse("Windows ISO has no sources directory")
    wim = find_named(sources, "install.wim")
    if wim is None or not wim.is_file() or wim.stat().st_size <= FAT32_MAX_FILE:
        esd = find_named(sources, "install.esd")
        if esd is not None and esd.is_file() and esd.stat().st_size > FAT32_MAX_FILE:
            refuse(f"{esd.name} is {esd.stat().st_size} bytes, over the FAT32 limit, and is not a split WIM")
    else:
        system.require_tools(["wimlib-imagex"])
        dest = wim.parent / "install.swm"
        print(f"flash: splitting {wim.name} ({wim.stat().st_size} bytes) into 4000 MiB SWM parts", flush=True)
        system.run(["wimlib-imagex", "split", str(wim), str(dest), "4000"])
        wim.unlink()
        parts = sorted(wim.parent.glob("install*.swm"))
        if not parts:
            refuse("wimlib-imagex split produced no .swm files")
        for part in parts:
            if part.stat().st_size > FAT32_MAX_FILE:
                refuse(f"{part.name} is still over the FAT32 limit")
            print(f"flash: {part.name} {part.stat().st_size} bytes", flush=True)
        system.run(["wimlib-imagex", "info", str(parts[0])])
    for path in Path(tree).rglob("*"):
        if path.is_file() and path.stat().st_size > FAT32_MAX_FILE:
            refuse(f"{path} is over the FAT32 4 GiB limit")


def extract_iso(iso, dest):
    system.require_tools(["7z"])
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    system.progress(5, "extracting ISO")
    system.run(["7z", "x", "-y", f"-o{dest}", iso], stdin=subprocess.DEVNULL)


def extracted_root(dest):
    """7z usually extracts the ISO root directly. Sometimes it adds one directory."""
    if find_named(dest, "sources") is not None or find_named(dest, "efi") is not None:
        return dest
    dirs = [path for path in dest.iterdir() if path.is_dir()]
    if len(dirs) == 1 and (find_named(dirs[0], "sources") is not None or find_named(dirs[0], "efi") is not None):
        return dirs[0]
    return dest


def write_windows(iso, device, confirm, verify=True):
    system.require_tools(["7z", "sfdisk", "mkfs.vfat", "mcopy", "wimlib-imagex"])
    work = scratch_dir()
    tree = work / "extract"
    extract_iso(iso, tree)
    try:
        root = extracted_root(tree)
        system.progress(30, "preparing install images")
        split_oversized_wims(root)
        # Extract can take a long time. Unmount and check again before sfdisk.
        plan = prepare_for_write(iso, device, confirm)
        system.progress(45, "partitioning")
        offset, length = partition_and_format(plan["device"], work, plan["block"])
        payload = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
        if payload >= length:
            refuse(f"files are {payload} bytes and the FAT partition is {length} bytes")
        system.progress(55, "copying files")
        copy_tree(root, plan["device"], offset)
        fd = os.open(plan["device"], os.O_RDWR)
        try:
            os.fsync(fd)
            drop_cache(fd, plan["device"])
        finally:
            os.close(fd)
        if verify:
            system.progress(80, "verifying")
            verify_tree(root, plan["device"], offset)
        system.progress(100, "verified" if verify else "written")
    finally:
        if tree.exists():
            shutil.rmtree(tree, ignore_errors=True)


def _guard_test_dev(device):
    if test_mode() and (is_under_dev(device) or is_under_dev(canon(device))):
        refuse("test mode cannot target /dev")


def cmd_list(args):
    rows = list_disks(*load_snapshot())
    if getattr(args, "json", False):
        print(json.dumps(rows), flush=True)
    else:
        for row in rows:
            state = "safe" if row["safe"] else "unsafe"
            print(f"{row['path']}\t{row['size_bytes']}\t{row['transport']}\t{state}\t{row['reason']}")
    return 0


def cmd_write(args):
    _guard_test_dev(args.device)
    if test_mode():
        return cmd_write_root(args)
    # Fail closed before the root prompt when the target is obviously wrong.
    prepare(args.iso, args.device, args.confirm)
    system.run(system.as_root(root_argv(args.iso, args.device, args.confirm, args.no_verify)))
    return 0


def cmd_write_root(args):
    _guard_test_dev(args.device)
    plan = prepare_for_write(args.iso, args.device, args.confirm)
    if system.DRY_RUN:
        print(f"flash: dry-run; not writing {plan['device']}", flush=True)
        return 0
    try:
        mode, _files = detect_mode(plan["iso"])
        # Detection only reads the ISO. Check once more before opening the target.
        plan = prepare_for_write(args.iso, args.device, args.confirm)
        if mode == "windows":
            write_windows(args.iso, args.device, args.confirm, verify=not args.no_verify)
        else:
            write_raw(plan["iso"], plan["device"], plan["block"], verify=not args.no_verify)
    except subprocess.CalledProcessError as err:
        command = shlex.join(str(part) for part in err.cmd)
        refuse(f"command failed ({err.returncode}): {command}")
    return 0


def register(sub):
    parser = sub.add_parser("flash", help="write an ISO to a USB stick")
    commands = parser.add_subparsers(dest="flash_cmd", required=True)

    listing = commands.add_parser("list", help="list whole disks and whether they are safe to flash")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_list)

    write = commands.add_parser("write", help="write an ISO to a whole disk")
    write.add_argument("iso")
    write.add_argument("device")
    write.add_argument("--confirm", required=True, help="must exactly equal DEVICE")
    write.add_argument("--no-verify", action="store_true", help="skip the read-back check")
    write.set_defaults(func=cmd_write)

    internal = commands.add_parser("_write", help=argparse.SUPPRESS)
    internal.add_argument("iso")
    internal.add_argument("device")
    internal.add_argument("--confirm", required=True)
    internal.add_argument("--no-verify", action="store_true")
    internal.set_defaults(func=cmd_write_root)
