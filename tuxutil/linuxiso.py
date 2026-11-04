"""Remaster a Linux live ISO into a custom one, rootless.

The ISO is rebuilt with ``xorriso -boot_image any replay`` so hybrid MBR, GPT
and El Torito stay as they were. Root filesystem edits run inside
``unshare --map-auto --map-root-user`` so ownership from the source image is
kept. ``unshare -Ur`` is not used: it maps a single uid and would flatten
every owner to the calling user.
"""

import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from . import system

SCRATCH = Path("/home/susiee/Projects/tuxutil/.work/linuxiso")
COMPRESSIONS = ("gzip", "xz", "zstd", "lzo")
# erofs has no lzo. gzip/xz are the squashfs names; erofs calls them deflate/lzma.
EROFS_COMP = {"gzip": "deflate", "xz": "lzma", "zstd": "zstd"}
SQUASH_MAGIC = b"hsqs"
EROFS_MAGIC = bytes.fromhex("e2e1f5e0")  # superblock magic at offset 1024
EXT4_MAGIC_OFF = 0x438

# subuid map on this machine covers namespace uids 1..65536. Higher owners in a
# source image cannot be represented and are left as the overflow uid.
# ponytail: 65536 is the host's /etc/subuid width, not a property of squashfs.


def option_specs():
    """Every build option, in the shape the GUI turns into a form."""
    return [
        {"flag": "-o", "type": "path", "label": "Output ISO",
         "help": "Where to write the remastered ISO. Must not be the input file.",
         "default": "", "choices": []},
        {"flag": "--add-pkg", "type": "list", "label": "Add packages",
         "help": "Packages to install with the live system's own package manager. Repeat the flag.",
         "default": [], "choices": []},
        {"flag": "--remove-pkg", "type": "list", "label": "Remove packages",
         "help": "Packages to remove with the live system's own package manager. Repeat the flag.",
         "default": [], "choices": []},
        {"flag": "--overlay", "type": "path", "label": "Root overlay",
         "help": "Directory copied onto the live root filesystem.",
         "default": "", "choices": []},
        {"flag": "--first-boot", "type": "path", "label": "First-boot script",
         "help": "Script run once on the live system's first boot, via a systemd oneshot and a stamp file.",
         "default": "", "choices": []},
        {"flag": "--hostname", "type": "str", "label": "Hostname",
         "help": "Hostname written to /etc/hostname.",
         "default": "", "choices": []},
        {"flag": "--label", "type": "str", "label": "Volume label",
         "help": "ISO volume label. Also rewritten in boot entries that name the old label (32 bytes max).",
         "default": "", "choices": []},
        {"flag": "--kernel-arg", "type": "list", "label": "Kernel argument",
         "help": "Added to every boot entry. Repeat the flag.",
         "default": [], "choices": []},
        {"flag": "--default-timeout", "type": "str", "label": "Boot menu timeout",
         "help": "Boot menu timeout in seconds (syslinux stores tenths). Empty keeps the source value.",
         "default": "", "choices": []},
        {"flag": "--compression", "type": "choice", "label": "Compression",
         "help": "Compression for the repacked root image. Empty keeps the source algorithm.",
         "default": "", "choices": list(COMPRESSIONS)},
    ]


def register(sub):
    p = sub.add_parser("linuxiso", help="remaster a Linux live ISO")
    commands = p.add_subparsers(dest="linuxiso_cmd", required=True)

    info = commands.add_parser("info", help="describe a live ISO")
    info.add_argument("iso")
    info.add_argument("--json", action="store_true")
    info.set_defaults(func=cmd_info)

    opt = commands.add_parser("options", help="list build options as JSON for the GUI")
    opt.add_argument("--json", action="store_true")
    opt.set_defaults(func=cmd_options)

    build = commands.add_parser("build", help="build a customized ISO")
    build.add_argument("iso")
    build.add_argument("-o", "--output", required=True)
    build.add_argument("--add-pkg", action="append", default=None)
    build.add_argument("--remove-pkg", action="append", default=None)
    build.add_argument("--overlay")
    build.add_argument("--first-boot")
    build.add_argument("--hostname", default="")
    build.add_argument("--label", default="")
    build.add_argument("--kernel-arg", action="append", default=None)
    build.add_argument("--default-timeout", type=int, default=None)
    build.add_argument("--compression", choices=list(COMPRESSIONS), default="")
    build.set_defaults(func=cmd_build)


def cmd_options(args):
    specs = option_specs()
    if args.json:
        import json
        print(json.dumps(specs))
        return 0
    for spec in specs:
        choices = f" ({', '.join(spec['choices'])})" if spec["choices"] else ""
        print(f"{spec['flag']}: {spec['help']}{choices}")
    return 0


def cmd_info(args):
    import json
    iso = Path(args.iso)
    if not iso.is_file():
        raise SystemExit(f"ISO not found: {iso}")
    system.require_tools(["xorriso"])
    paths = list_iso_files(iso)
    info = describe(
        paths,
        label=volume_label(iso),
        disk_info=_maybe_text(iso, paths, "/.disk/info"),
        treeinfo=_maybe_text(iso, paths, "/.treeinfo"),
        boot_text=_boot_blob(iso, paths),
        image_kind=_primary_kind(iso, paths),
    )
    if args.json:
        print(json.dumps(info))
        return 0
    print(f"family: {info['family']}")
    print(f"squashfs: {info['squashfs']}")
    print(f"label: {info['label']}")
    print(f"arch: {info['arch']}")
    print(f"supported: {str(info['supported']).lower()}")
    print(f"notes: {info['notes']}")
    return 0


def cmd_build(args):
    iso = Path(args.iso)
    out = Path(args.output)
    if not iso.is_file():
        raise SystemExit(f"ISO not found: {iso}")
    if not out.parent.is_dir():
        raise SystemExit(f"Output directory does not exist: {out.parent}")
    if same_path(iso, out):
        raise SystemExit(f"Refusing to overwrite the input ISO ({iso}).")
    if args.label and len(args.label.encode()) > 32:
        raise SystemExit("Volume label must be at most 32 bytes (ISO 9660).")
    if args.hostname and not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?", args.hostname):
        raise SystemExit("Hostname must be a single DNS label (letters, digits, hyphen).")
    if args.default_timeout is not None and args.default_timeout < 0:
        raise SystemExit("--default-timeout must be zero or a positive number of seconds.")
    if args.overlay and not Path(args.overlay).is_dir():
        raise SystemExit(f"Overlay directory not found: {args.overlay}")
    if args.first_boot and not Path(args.first_boot).is_file():
        raise SystemExit(f"First-boot script not found: {args.first_boot}")
    system.require_tools(["xorriso", "unsquashfs", "mksquashfs", "unshare", "newuidmap"])
    SCRATCH.mkdir(parents=True, exist_ok=True)
    need = bytes_needed(iso.stat().st_size)
    err = space_error(shutil.disk_usage(SCRATCH).free, need)
    if err:
        raise SystemExit(err + f" ({SCRATCH})")
    out_free = shutil.disk_usage(out.parent).free
    # Output may share the scratch filesystem; only complain when it is clearly short on its own.
    if out.parent.resolve() != SCRATCH.resolve() and space_error(out_free, iso.stat().st_size + 1024**3):
        raise SystemExit(space_error(out_free, iso.stat().st_size + 1024**3) + f" ({out.parent})")

    if os.environ.get("TUXUTIL_LINUXISO_INNER") != "1":
        scratch = SCRATCH / f"build-{os.getpid()}"
        scrub(scratch)
        scratch.mkdir(parents=True)
        env = os.environ.copy()
        env["TUXUTIL_LINUXISO_INNER"] = "1"
        env["TUXUTIL_LINUXISO_SCRATCH"] = str(scratch)
        cmd = [*unshare_prefix(), sys.executable, "-m", "tuxutil"]
        if system.DRY_RUN:
            cmd.append("--dry-run")
        cmd += _build_argv(args)
        try:
            system.run(cmd, env=env)
            return 0
        finally:
            if os.environ.get("TUXUTIL_LINUXISO_KEEP") != "1":
                scrub(scratch)
    if system.DRY_RUN:
        return 0
    inner_build(args, Path(os.environ["TUXUTIL_LINUXISO_SCRATCH"]))
    return 0


def _build_argv(args):
    cmd = ["linuxiso", "build", args.iso, "-o", args.output]
    for pkg in args.add_pkg or []:
        cmd += ["--add-pkg", pkg]
    for pkg in args.remove_pkg or []:
        cmd += ["--remove-pkg", pkg]
    if args.overlay:
        cmd += ["--overlay", args.overlay]
    if args.first_boot:
        cmd += ["--first-boot", args.first_boot]
    if args.hostname:
        cmd += ["--hostname", args.hostname]
    if args.label:
        cmd += ["--label", args.label]
    for arg in args.kernel_arg or []:
        cmd += ["--kernel-arg", arg]
    if args.default_timeout is not None:
        cmd += ["--default-timeout", str(args.default_timeout)]
    if args.compression:
        cmd += ["--compression", args.compression]
    return cmd


# --- detection (pure) -------------------------------------------------------

def detect_layout(paths):
    """Map an ISO file list to a live-root layout. Paths are absolute ('/casper/...')."""
    files = [_norm(p) for p in paths]
    have = set(files)
    arch = guess_arch(files)
    windows = [p for p in files if p.lower().endswith(("/sources/install.wim", "/sources/install.esd"))]
    if windows:
        return _layout("unknown", "", [], arch, False,
                       "Windows image, not a Linux live ISO.")
    nix = [p for p in files if p.endswith("/nix-store.squashfs") or p.endswith("nix-store.squashfs")]
    if nix:
        return _layout("nixos", nix[0], [], arch, False,
                       "NixOS stores the system in a Nix store squashfs, which is not a mutable live root.")

    arch_roots = []
    for p in files:
        m = re.match(r"^/arch/([^/]+)/airootfs\.(sfs|erofs)$", p)
        if m:
            arch_roots.append((0 if m.group(1) in ("x86_64", "amd64") else 1, p))
    if arch_roots:
        primary = sorted(arch_roots)[0][1]
        return _layout("arch", primary, [primary], arch, True, "")

    if "/casper/filesystem.squashfs" in have:
        return _layout("ubuntu", "/casper/filesystem.squashfs", ["/casper/filesystem.squashfs"], arch, True, "")
    layers = casper_layers(files)
    if layers:
        return _layout("ubuntu", layers[-1], layers, arch, True,
                       "Layered casper image. The live session stacks "
                       + ", ".join(Path(p).name for p in layers)
                       + ". Changes are written into the top layer.")
    casper_single = sorted(p for p in files if p.endswith("/filesystem.squashfs") and "casper" in p.lower())
    if casper_single:
        return _layout("ubuntu", casper_single[0], [casper_single[0]], arch, True, "")

    if "/live/filesystem.squashfs" in have:
        return _layout("debian", "/live/filesystem.squashfs", ["/live/filesystem.squashfs"], arch, True, "")
    if "/live/filesystem.erofs" in have:
        return _layout("unknown", "/live/filesystem.erofs", [], arch, False,
                       "erofs image at live/filesystem.erofs is not a supported layout "
                       "(supported: casper, debian-live squashfs, archiso, Fedora LiveOS).")

    if "/LiveOS/squashfs.img" in have:
        return _layout("fedora", "/LiveOS/squashfs.img", ["/LiveOS/squashfs.img"], arch, True, "")
    if "/LiveOS/rootfs.squashfs" in have:
        return _layout("fedora", "/LiveOS/rootfs.squashfs", ["/LiveOS/rootfs.squashfs"], arch, True,
                       "Dracut live squashfs at LiveOS/rootfs.squashfs.")

    stacked = sorted(p for p in files if p.endswith("/rootfs.sfs"))
    if stacked:
        return _layout("arch", stacked[0], [stacked[0]], arch, True,
                       "Stacked arch-style live image. Changes apply to rootfs.sfs; "
                       "upper layers such as desktopfs.sfs can hide paths they also ship.")

    return _layout("unknown", "", [], arch, False, "No supported live root filesystem found.")


def describe(paths, *, label="", disk_info="", treeinfo="", boot_text="", image_kind=""):
    """Full info record. Extra text arguments are the contents of small ISO files."""
    layout = detect_layout(paths)
    notes = [layout["notes"]] if layout["notes"] else []
    supported = layout["supported"]
    family = layout["family"]
    if layout["squashfs"]:
        if image_kind == "erofs":
            notes.append("Live root image is erofs.")
        elif image_kind == "ext4":
            supported = False
            notes.append("Live root is an ext4 image. This host has no rootless ext4 writer.")
        if family == "fedora" and _immutable_text(boot_text):
            supported = False
            notes.append("Immutable ostree image (Bazzite, Silverblue, and similar). Not a writable live root.")
        if label.upper().startswith("TAILS") or "tails" in boot_text.lower():
            notes.append("Tails verifies the live filesystem and may refuse to boot a modified image.")
    else:
        disk = (disk_info or "").strip().splitlines()
        tree_family, tree_name = _treeinfo_names(treeinfo or "")
        if disk and not tree_name:
            family = "debian"
            supported = False
            notes.append(disk[0] + " Installer image; no live root filesystem.")
        elif tree_name:
            family = tree_family or "unknown"
            supported = False
            notes.append(f"{tree_name} installer image; no live root filesystem.")
        elif "No supported" not in " ".join(notes):
            notes.append("Not a Linux live ISO.")
    return {
        "family": family,
        "squashfs": layout["squashfs"],
        "label": label,
        "arch": layout["arch"],
        "supported": supported,
        "notes": " ".join(n.strip() for n in notes if n.strip()),
        "layers": layout["layers"],
    }


def casper_layers(paths):
    """Bottom-to-top casper layers for names like minimal.standard.live.squashfs."""
    names = {}
    for p in paths:
        if p.startswith("/casper/") and p.endswith(".squashfs"):
            names[p.rsplit("/", 1)[-1]] = p
    for top in ("minimal.standard.live.squashfs", "minimal.standard.squashfs",
                "minimal.live.squashfs", "minimal.squashfs"):
        if top not in names:
            continue
        stem = top[: -len(".squashfs")]
        parts = stem.split(".")
        layers = []
        ok = True
        for i in range(1, len(parts) + 1):
            name = ".".join(parts[:i]) + ".squashfs"
            if name not in names:
                ok = False
                break
            layers.append(names[name])
        if ok and layers:
            return layers
    return []


def guess_arch(paths):
    blob = " ".join(paths).lower()
    for token, name in (("x86_64", "x86_64"), ("amd64", "x86_64"), ("aarch64", "aarch64"),
                        ("arm64", "aarch64"), ("i686", "i386"), ("i386", "i386")):
        if token in blob:
            return name
    if "bootx64.efi" in blob or "bootx64.efi" in blob:
        return "x86_64"
    return "unknown"


def sniff_image(head):
    """Identify a root image from its first bytes."""
    if not head:
        return "unknown"
    if head[:4] == SQUASH_MAGIC:
        return "squashfs"
    if len(head) >= 1028 and head[1024:1028] == EROFS_MAGIC:
        return "erofs"
    if len(head) >= EXT4_MAGIC_OFF + 2 and head[EXT4_MAGIC_OFF:EXT4_MAGIC_OFF + 2] == b"\x53\xef":
        return "ext4"
    return "unknown"


def _immutable_text(text):
    low = (text or "").lower()
    return any(token in low for token in ("ostree=", "bazzite", "silverblue", "kinoite"))


def _treeinfo_names(text):
    family = ""
    name = ""
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, val = (part.strip() for part in line.split("=", 1))
        if key == "family" and not family:
            family = _family_token(val)
            name = name or val
        if key == "name" and not name:
            name = val
    return family, name


def _family_token(val):
    low = val.lower()
    for token, family in (("opensuse", "suse"), ("suse", "suse"), ("fedora", "fedora"),
                          ("debian", "debian"), ("ubuntu", "ubuntu"), ("arch", "arch")):
        if token in low:
            return family
    return ""


def _layout(family, squashfs, layers, arch, supported, notes):
    return {"family": family, "squashfs": squashfs, "layers": list(layers),
            "arch": arch, "supported": supported, "notes": notes}


def _norm(path):
    path = path.strip().strip("'").strip('"')
    if not path.startswith("/"):
        path = "/" + path
    return path


# --- boot config and metadata (pure) ----------------------------------------

def boot_kind(path, text=""):
    low = path.lower()
    if low.endswith("/loader.conf"):
        return "loader"
    if "/loader/entries/" in low:
        return "systemd-boot"
    if "/syslinux/" in low or "/isolinux/" in low:
        return "syslinux"
    return "grub"


def is_boot_config(path):
    low = path.lower()
    name = low.rsplit("/", 1)[-1]
    if any(part in low for part in ("/i386-pc/", "/x86_64-efi/", "/i386-efi/", "/arm64-efi/")):
        return name == "grub.cfg"
    if "/loader/entries/" in low and name.endswith(".conf"):
        return True
    if name in {"grub.cfg", "loopback.cfg", "isolinux.cfg", "syslinux.cfg", "txt.cfg",
                "menu.cfg", "loader.conf", "grub.conf"}:
        return True
    if ("/syslinux/" in low or "/isolinux/" in low) and name.endswith(".cfg"):
        return True
    if name.endswith("grub.cfg"):
        return True
    return False


def is_label_carrier(path):
    """Files that may mention the volume label and are small enough to rewrite."""
    low = path.lower()
    base = low.rsplit("/", 1)[-1]
    if base.startswith(("vmlinuz", "initr")) or base.endswith((".sfs", ".squashfs", ".erofs", ".ko", ".mod")):
        return False
    if "squashfs" in base:
        return False
    if is_boot_config(path):
        return True
    if low.endswith((".efi", ".img")) and ("/efi/" in low or "/boot/" in low or base in {"efi.img", "eltorito.img"}):
        return True
    return False


def patch_boot_text(text, *, old_label="", new_label="", kernel_args=(), timeout=None, kind="grub"):
    if old_label and new_label and old_label != new_label and old_label in text:
        text = text.replace(old_label, new_label)
    if kernel_args:
        text = _add_kernel_args(text, list(kernel_args), kind)
    if timeout is not None and kind in {"grub", "syslinux", "loader"}:
        text = _set_timeout(text, int(timeout), kind)
    return text


def _add_kernel_args(text, args, kind):
    if kind == "syslinux":
        pattern = r"(?im)^(\s*append\s+)(.*)$"
    elif kind == "systemd-boot":
        pattern = r"(?m)^(\s*options\s+)(.*)$"
    else:
        pattern = r"(?m)^(\s*linux(?:efi|16)?\s+\S+)(?:[ \t]+(.*))?$"

    def repl(match):
        body = match.group(2) or ""
        prefix = match.group(1)
        if "memtest" in (prefix + body).lower():
            return match.group(0)
        updated = _append_cmdline(body, args)
        spacer = "" if kind == "grub" and match.group(2) is None else ""
        if kind == "grub":
            return prefix + (" " + updated if updated else "")
        return prefix + updated

    return re.sub(pattern, repl, text)


def _append_cmdline(body, args):
    present = set(body.split())
    extra = [arg for arg in args if arg not in present]
    if not extra:
        return body
    addition = " ".join(extra)
    if " ---" in body:
        pre, post = body.split(" ---", 1)
        return (pre.rstrip() + " " + addition).rstrip() + " ---" + post
    if body.strip() == "":
        return addition
    return body.rstrip() + " " + addition


def _set_timeout(text, seconds, kind):
    if kind == "syslinux":
        value = str(seconds * 10)  # syslinux TIMEOUT is in tenths of a second
        if re.search(r"(?im)^\s*timeout\s+\S+", text):
            return re.sub(r"(?im)^(\s*timeout\s+)\S+", rf"\g<1>{value}", text)
        return f"TIMEOUT {value}\n" + text
    if kind == "loader":
        if re.search(r"(?m)^\s*timeout\s+\S+", text):
            return re.sub(r"(?m)^(\s*timeout\s+)\S+", rf"\g<1>{seconds}", text)
        return f"timeout {seconds}\n" + text
    if re.search(r"(?m)^\s*set\s+timeout=", text):
        return re.sub(r"(?m)^(\s*set\s+timeout=)\S+", rf"\g<1>{seconds}", text)
    return f"set timeout={seconds}\n" + text


def patch_embedded_label(data, old, new):
    """Replace a volume label inside a boot binary without shifting later bytes.

    A different length is applied only when the old label is a NUL-terminated
    string (or has enough trailing NULs). A label sitting in the middle of a
    kernel command line must keep its length, or the rest of the line would be
    cut or overwritten.
    """
    if not old or old == new:
        return data
    old_b, new_b = old.encode(), new.encode()
    if old_b not in data:
        return data
    if len(new_b) == len(old_b):
        return data.replace(old_b, new_b)
    out = bytearray()
    i = 0
    while True:
        j = data.find(old_b, i)
        if j < 0:
            out += data[i:]
            break
        after = j + len(old_b)
        if len(new_b) < len(old_b):
            if after >= len(data) or data[after] != 0:
                raise SystemExit(
                    f"Volume label {old!r} is embedded in a boot binary. "
                    f"The new label must be exactly {len(old_b)} bytes, or the command line after it would be damaged.")
            out += data[i:j] + new_b + b"\x00" * (len(old_b) - len(new_b))
            i = after
            continue
        extra = len(new_b) - len(old_b)
        if data[after:after + extra] == b"\x00" * extra:
            out += data[i:j] + new_b
            i = after + extra
            continue
        raise SystemExit(
            f"Volume label {old!r} is embedded in a boot binary with no room to grow. "
            f"Use a label of at most {len(old_b)} bytes.")
    return bytes(out)


def device_pseudo(pf_text):
    """Keep character and block device lines from ``unsquashfs -pf`` output.

    Device nodes cannot be created in a user namespace, so mksquashfs rebuilds
    them from these definitions instead of from the unpacked tree.
    """
    kept = []
    for line in pf_text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] in {"C", "B", "c", "b"}:
            kept.append(line)
    return ("\n".join(kept) + "\n") if kept else ""


def update_md5sum(text, updates):
    """Replace or add md5 lines. ``updates`` maps ISO paths ('/casper/x') to hex digests."""
    lines = []
    seen = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, rel = line.split(None, 1)
        key = _md5_key(rel)
        if key in updates:
            lines.append(f"{updates[key]}  {rel.strip()}")
            seen.add(key)
        else:
            lines.append(line)
    for key, digest in updates.items():
        if key not in seen:
            rel = "./" + key.lstrip("/")
            lines.append(f"{digest}  {rel}")
    return ("\n".join(lines) + "\n") if lines else ""


def update_manifest(old_text, before, after):
    """Apply a package-list delta to a dpkg manifest (name, tab, version)."""
    current = _parse_manifest(old_text)
    if not current and after:
        current = dict(after)
    else:
        for name, ver in after.items():
            if before.get(name) != ver:
                current[name] = ver
        for name in before:
            if name not in after:
                current.pop(name, None)
    return "".join(f"{name}\t{current[name]}\n" for name in sorted(current))


def format_sha512(digest, basename):
    # archiso checks `sha512sum` output: hex, two spaces, filename.
    return f"{digest}  {basename}\n"


def stale_signature_paths(paths, changed):
    stale = []
    for path in paths:
        for source in changed:
            if path == source + ".gpg" or path == source + ".sig" or path == source + ".cms.sig":
                stale.append(path)
            elif path.startswith(source) and path.endswith(".sig"):
                stale.append(path)
        if path.endswith("/md5sum.txt.gpg"):
            stale.append(path)
    return sorted(set(stale))


def _parse_manifest(text):
    out = {}
    for line in (text or "").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if "\t" in line:
            name, ver = line.split("\t", 1)
        elif " " in line:
            name, ver = line.split(None, 1)
        else:
            name, ver = line, ""
        out[name.strip()] = ver.strip()
    return out


def _md5_key(rel):
    rel = rel.strip()
    if rel.startswith("*"):
        rel = rel[1:]
    rel = rel.lstrip("./")
    return "/" + rel


def bytes_needed(iso_size):
    return iso_size * 6 + 2 * 1024**3


def space_error(free, need):
    if free < need:
        return f"Not enough disk space: need {need} bytes, have {free}."
    return None


def same_path(a, b):
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


def xorriso_rebuild_cmd(src, dst, *, label="", deletes=(), maps=()):
    """Replay the source boot setup after mapping changed files.

    ``-boot_image any replay`` is last so El Torito sees the replaced files.
    """
    cmd = ["xorriso", "-indev", str(src), "-outdev", str(dst), "-overwrite", "on"]
    if label:
        cmd += ["-volid", label]
    for path in deletes:
        cmd += ["-rm", path, "--"]
    for host, iso_path in maps:
        cmd += ["-map", str(host), iso_path]
    cmd += ["-boot_image", "any", "replay"]
    return cmd


def unshare_prefix():
    # --map-auto uses /etc/subuid so non-root owners survive. No --net: package
    # managers need the host network stack.
    return ["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private",
            "--fork", "--pid", "--mount-proc"]


def mksquashfs_cmd(src, dst, compression, pseudo_file=None, block=None):
    cmd = ["mksquashfs", str(src), str(dst), "-comp", compression, "-noappend", "-no-progress",
           "-processors", "4"]
    if block:
        cmd += ["-b", str(block)]
    if pseudo_file:
        cmd += ["-pf", str(pseudo_file)]
    return cmd


def mkfs_erofs_cmd(src, dst, algorithm):
    return ["mkfs.erofs", "-z", algorithm, "--workers=4", "--preserve-mtime", str(dst), str(src)]


# --- ISO reading ------------------------------------------------------------

def list_iso_files(iso):
    result = subprocess.run(["xorriso", "-indev", str(iso), "-find", "/", "-type", "f"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "xorriso failed").strip().splitlines()
        raise SystemExit(tail[-1] if tail else "xorriso failed to read the ISO")
    return [_norm(line) for line in result.stdout.splitlines() if line.strip()]


def volume_label(iso):
    result = subprocess.run(["xorriso", "-indev", str(iso), "-pvd_info"], capture_output=True, text=True)
    found = ""
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        if "volume id" not in line.lower() or ":" not in line:
            continue
        val = line.split(":", 1)[1].strip().strip("'")
        if val:
            found = val
    return found


def extract_file(iso, inner, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(["xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", inner, str(dest)])


def image_head(iso, inner, size=4096):
    result = subprocess.run(["xorriso", "-indev", str(iso), "-find", inner, "-exec", "report_lba"],
                            capture_output=True, text=True)
    match = re.search(r"File data lba:\s*\d+\s*,\s*(\d+)\s*,", result.stdout)
    if not match:
        return b""
    with open(iso, "rb") as handle:
        handle.seek(int(match.group(1)) * 2048)
        return handle.read(size)


def _maybe_text(iso, paths, inner):
    if inner not in paths and _norm(inner) not in {_norm(p) for p in paths}:
        return ""
    return _read_small(iso, inner)


def _boot_blob(iso, paths):
    chunks = []
    for path in paths:
        if not is_boot_config(path):
            continue
        if not path.lower().endswith(("grub.cfg", "isolinux.cfg", "syslinux.cfg", "loader.conf")):
            continue
        text = _read_small(iso, path)
        if text:
            chunks.append(text)
        if sum(len(c) for c in chunks) > 200_000:
            break
    return "\n".join(chunks)


def _primary_kind(iso, paths):
    layout = detect_layout(paths)
    if not layout["squashfs"]:
        return ""
    return sniff_image(image_head(iso, layout["squashfs"]))


def _read_small(iso, inner):
    with tempfile.TemporaryDirectory(dir=SCRATCH if SCRATCH.is_dir() else None) as tmp:
        dest = Path(tmp) / "f"
        result = subprocess.run(
            ["xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", inner, str(dest)],
            capture_output=True, text=True)
        if result.returncode != 0 or not dest.is_file():
            return ""
        data = dest.read_bytes()[:2_000_000]
        if b"\x00" in data[:512]:
            return ""
        return data.decode("utf-8", "replace")


# --- build ------------------------------------------------------------------

def inner_build(args, scratch):
    iso = Path(args.iso).resolve()
    out = Path(args.output).resolve()
    mounts = _Mounts()
    try:
        system.progress(2, "reading ISO layout")
        paths = list_iso_files(iso)
        label = volume_label(iso)
        info = describe(paths, label=label, disk_info=_maybe_text(iso, paths, "/.disk/info"),
                        treeinfo=_maybe_text(iso, paths, "/.treeinfo"),
                        boot_text=_boot_blob(iso, paths), image_kind=_primary_kind(iso, paths))
        if not info["supported"]:
            raise SystemExit(f"Unsupported ISO: {info['notes'] or info['family']}")
        if not info["squashfs"]:
            raise SystemExit("No live root filesystem to modify.")
        _warn(f"{info['family']} {info['squashfs']} label={label or '(none)'}")
        if info["notes"]:
            _warn(info["notes"])
        if args.first_boot:
            _warn("First boot is a systemd oneshot. Images without systemd fail when the root is unpacked.")

        system.progress(8, "extracting root filesystem")
        root, repack = _materialize(iso, info, scratch, mounts)
        if _is_immutable_root(root):
            raise SystemExit("This live image is an immutable ostree system. Refusing to modify it.")
        if args.first_boot and not _has_systemd(root):
            raise SystemExit("This live image has no systemd, so --first-boot cannot be installed. "
                             "info notes should mention that for non-systemd images; this one has no "
                             "/usr/lib/systemd/systemd.")

        system.progress(40, "applying overlay, hostname, first boot")
        _apply_identity(root, args)
        pkg_family = _package_family(root, scratch)
        before = _package_list(root, pkg_family) if (args.add_pkg or args.remove_pkg) else {}
        if args.add_pkg or args.remove_pkg:
            if pkg_family not in {"arch", "debian", "fedora", "suse", "alpine"}:
                raise SystemExit(f"Don't know how to install packages for distro family '{pkg_family}'.")
            system.progress(48, "installing packages inside the live root")
            _run_packages(root, pkg_family, args.add_pkg or [], args.remove_pkg or [], mounts)
        after = _package_list(root, pkg_family) if (args.add_pkg or args.remove_pkg) else {}

        if repack.get("delta") is not None:
            _copy_tree(repack["delta"], repack["dir"])
        _umount_children(root, mounts)

        system.progress(70, "repacking root filesystem")
        built = _repack(repack, scratch, args.compression)
        changed = {repack["iso_path"]: built}

        system.progress(82, "updating boot entries and metadata")
        deletes, maps = _metadata_and_boot(
            iso, paths, scratch, info, label, args, root, changed, before, after, pkg_family)
        if out.exists():
            out.unlink()
        system.progress(90, "writing ISO")
        run(xorriso_rebuild_cmd(iso, out, label=args.label or "", deletes=deletes, maps=list(maps.items())))
        if info["family"] == "fedora":
            if shutil.which("implantisomd5"):
                run(["implantisomd5", str(out)])
            else:
                _warn("implantisomd5 is not installed, so the Fedora media checksum was not embedded. "
                      "The 'Test this media' boot entry will report a checksum mismatch.")
        system.progress(100, "done")
        print(f"Wrote {out}")
    finally:
        mounts.umount_all()


def _materialize(iso, info, scratch, mounts):
    layers = info.get("layers") or [info["squashfs"]]
    if info["family"] == "ubuntu" and len(layers) > 1:
        return _materialize_layered(iso, layers, scratch, mounts)
    image = scratch / "image"
    extract_file(iso, info["squashfs"], image)
    kind = sniff_image(image.read_bytes()[:4096])
    root = scratch / "root"
    pseudo = None
    iso_path = info["squashfs"]
    comp = ""
    block = None
    if kind == "squashfs":
        pseudo, comp, block = _unpack_squash(image, root)
        inner = root / "LiveOS" / "rootfs.img"
        if inner.is_file() and not (root / "etc" / "os-release").exists() and not (root / "usr" / "lib" / "os-release").exists():
            inner_kind = sniff_image(inner.read_bytes()[:4096])
            if inner_kind == "ext4":
                raise SystemExit("LiveOS/rootfs.img is ext4. fuse2fs is not installed, so it cannot be "
                                 "edited without root. Refusing to build.")
            if inner_kind == "erofs":
                root2 = scratch / "root-inner"
                _unpack_erofs(inner, root2)
                return root2, {"dir": root2, "kind": "erofs-in-squash", "outer_root": root,
                               "outer_image": image, "inner_image": inner, "iso_path": iso_path,
                               "pseudo": pseudo, "compression": comp, "block": block, "delta": None}
        return root, {"dir": root, "kind": "squashfs", "iso_path": iso_path, "pseudo": pseudo,
                      "compression": comp, "block": block, "delta": None, "image": image}
    if kind == "erofs":
        _unpack_erofs(image, root)
        return root, {"dir": root, "kind": "erofs", "iso_path": iso_path, "pseudo": None,
                      "compression": _erofs_alg(image), "block": None, "delta": None, "image": image}
    raise SystemExit(f"Unrecognized root image format in {info['squashfs']} ({kind}).")


def _materialize_layered(iso, layers, scratch, mounts):
    """Edit the top casper layer through an overlay of the lower layers.

    The lower squashfs files stay compressed and are mounted with squashfuse.
    Package and file changes land in the top layer, which is the one casper
    already uses as LAYERFS_PATH (minimal.standard.live on current Ubuntu).
    """
    extracted = []
    for index, iso_path in enumerate(layers):
        dest = scratch / "layers" / f"{index}.squashfs"
        extract_file(iso, iso_path, dest)
        extracted.append(dest)
    top_image = extracted[-1]
    top_dir = scratch / "top"
    pseudo, comp, block = _unpack_squash(top_image, top_dir)
    mnt_paths = []
    for index, image in enumerate(extracted):
        mnt = scratch / "mnt" / str(index)
        mnt.mkdir(parents=True)
        run(["squashfuse", str(image), str(mnt)])
        mounts.add(mnt)
        mnt_paths.append(mnt)
    delta = scratch / "delta"
    work = scratch / "ovwork"
    merged = scratch / "merged"
    delta.mkdir()
    work.mkdir()
    merged.mkdir()
    # First lowerdir is the highest priority (the current top layer).
    lower = ":".join(str(p) for p in reversed(mnt_paths))
    run(["mount", "-t", "overlay", "overlay", "-o",
         f"lowerdir={lower},upperdir={delta},workdir={work}", str(merged)])
    mounts.add(merged)
    return merged, {"dir": top_dir, "kind": "squashfs", "iso_path": layers[-1], "pseudo": pseudo,
                    "compression": comp, "block": block, "delta": delta, "image": top_image}


def _unpack_squash(image, dest):
    if dest.exists():
        scrub(dest)
    dest.mkdir(parents=True)
    # Exit 2 is "non-fatal": device nodes and some xattrs cannot be created here.
    run(["unsquashfs", "-q", "-no-progress", "-f", "-d", str(dest), str(image)], ok=(0, 2))
    pf = run(["unsquashfs", "-pf", "-", "-q", str(image)], capture=True, ok=(0, 2))
    comp, block = _squash_info(image)
    return device_pseudo(pf.stdout or ""), comp, block


def _unpack_erofs(image, dest):
    system.require_tools(["fsck.erofs"])
    if dest.exists():
        scrub(dest)
    dest.mkdir(parents=True)
    # ponytail: fsck.erofs turns device nodes into regular files in a user
    # namespace. Live boot mounts devtmpfs on /dev, which hides them.
    run(["fsck.erofs", f"--extract={dest}", "--preserve", "--overwrite", str(image)], ok=(0, 1))
    if not (dest / "etc").exists() and not (dest / "usr").exists():
        raise SystemExit("fsck.erofs did not extract a root filesystem.")


def _squash_info(image):
    result = subprocess.run(["unsquashfs", "-s", str(image)], capture_output=True, text=True)
    comp, block = "xz", None
    for line in (result.stdout or "").splitlines():
        low = line.lower()
        if "compression" in low:
            comp = line.split()[-1].lower()
        if "block size" in low:
            token = line.split()[-1]
            if token.isdigit():
                block = token
    if comp not in COMPRESSIONS:
        comp = "xz"
    return comp, block


def _erofs_alg(image):
    if not shutil.which("dump.erofs"):
        return "lzma"
    result = subprocess.run(["dump.erofs", "-s", str(image)], capture_output=True, text=True)
    for line in (result.stdout or "").splitlines():
        if "compr_algs" in line and ":" in line:
            alg = line.split(":", 1)[1].strip().split()[0]
            if alg:
                return alg
    return "lzma"


def _repack(repack, scratch, compression):
    kind = repack["kind"]
    if kind == "erofs":
        system.require_tools(["mkfs.erofs"])
        alg = EROFS_COMP.get(compression) if compression else repack["compression"]
        if compression == "lzo":
            raise SystemExit("lzo compression is not available for erofs images. Choose gzip, xz, or zstd.")
        if not alg:
            alg = "lzma"
        dest = scratch / "new-root.img"
        run(mkfs_erofs_cmd(repack["dir"], dest, alg))
        return dest
    if kind == "erofs-in-squash":
        system.require_tools(["mkfs.erofs"])
        alg = EROFS_COMP.get(compression) if compression else "lzma"
        if compression == "lzo":
            raise SystemExit("lzo compression is not available for erofs images. Choose gzip, xz, or zstd.")
        new_inner = scratch / "rootfs.img"
        run(mkfs_erofs_cmd(repack["dir"], new_inner, alg or "lzma"))
        live = repack["outer_root"] / "LiveOS"
        live.mkdir(parents=True, exist_ok=True)
        shutil.move(str(new_inner), str(live / "rootfs.img"))
        dest = scratch / "new-squash.img"
        comp = compression or repack["compression"] or "xz"
        pseudo = scratch / "devices.pf"
        if repack.get("pseudo"):
            pseudo.write_text(repack["pseudo"])
        run(mksquashfs_cmd(repack["outer_root"], dest, comp, pseudo if repack.get("pseudo") else None, repack.get("block")))
        return dest
    dest = scratch / "new.squashfs"
    comp = compression or repack["compression"] or "xz"
    pseudo = None
    if repack.get("pseudo"):
        pseudo = scratch / "devices.pf"
        pseudo.write_text(repack["pseudo"])
    run(mksquashfs_cmd(repack["dir"], dest, comp, pseudo, repack.get("block")))
    return dest


def _apply_identity(root, args):
    if args.overlay:
        _copy_tree(Path(args.overlay), root)
    if args.hostname:
        etc = root / "etc"
        etc.mkdir(parents=True, exist_ok=True)
        (etc / "hostname").write_text(args.hostname + "\n")
        hosts = etc / "hosts"
        if hosts.is_file() and not hosts.is_symlink():
            rows = []
            replaced = False
            for line in hosts.read_text(errors="replace").splitlines():
                if line.startswith("127.0.1.1") or line.startswith("127.0.0.1 ") and args.hostname in line.split():
                    rows.append(f"127.0.1.1\t{args.hostname}")
                    replaced = True
                else:
                    rows.append(line)
            if not replaced:
                rows.append(f"127.0.1.1\t{args.hostname}")
            hosts.write_text("\n".join(rows) + "\n")
    if args.first_boot:
        _install_first_boot(root, Path(args.first_boot))


def _install_first_boot(root, script):
    lib = root / "usr/local/lib/tuxutil"
    lib.mkdir(parents=True, exist_ok=True)
    target = lib / "first-boot"
    shutil.copyfile(script, target)
    target.chmod(0o755)
    wrapper = root / "usr/local/sbin/tuxutil-firstboot"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("#!/bin/sh\nexec /bin/sh /usr/local/lib/tuxutil/first-boot\n")
    wrapper.chmod(0o755)
    unit_dir = root / "etc/systemd/system"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "tuxutil-firstboot.service").write_text(
        "[Unit]\n"
        "Description=TuxUtil first boot\n"
        "ConditionPathExists=!/var/lib/tuxutil-firstboot.stamp\n"
        "After=local-fs.target\n"
        "Before=display-manager.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/local/sbin/tuxutil-firstboot\n"
        "ExecStartPost=/bin/sh -c 'mkdir -p /var/lib && touch /var/lib/tuxutil-firstboot.stamp'\n"
        "RemainAfterExit=yes\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    wants = unit_dir / "multi-user.target.wants"
    wants.mkdir(parents=True, exist_ok=True)
    link = wants / "tuxutil-firstboot.service"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to("/etc/systemd/system/tuxutil-firstboot.service")


def _has_systemd(root):
    return any((root / rel).exists() for rel in (
        "usr/lib/systemd/systemd", "lib/systemd/systemd", "usr/bin/systemctl", "bin/systemctl"))


def _is_immutable_root(root):
    if (root / "ostree" / "repo").is_dir() or (root / "sysroot" / "ostree").is_dir():
        return True
    text = _read_os_release(root)
    low = text.lower()
    return any(token in low for token in ('id=bazzite', 'id="bazzite"', "variant_id=ostree",
                                          'id=silverblue', 'id="silverblue"', 'id=kinoite'))


def _read_os_release(root):
    for rel in ("usr/lib/os-release", "etc/os-release"):
        path = root / rel
        if path.is_file() and not path.is_symlink():
            return path.read_text(errors="replace")
    return ""


def _package_family(root, scratch):
    text = _read_os_release(root)
    if not text:
        return "unknown"
    copy = scratch / "os-release"
    copy.write_text(text)
    return system.detect(copy).family


def _package_list(root, family):
    if family != "debian":
        return {}
    result = run(["chroot", str(root), "dpkg-query", "-W"], capture=True, ok=(0, 1))
    return _parse_manifest(result.stdout or "")


def _run_packages(root, family, add, remove, mounts):
    _mount_api(root, mounts)
    saved = _inject_resolv(root)
    policy = None
    try:
        env = _chroot_env()
        if family == "debian":
            policy = root / "usr/sbin/policy-rc.d"
            policy.parent.mkdir(parents=True, exist_ok=True)
            policy.write_text("#!/bin/sh\nexit 101\n")
            policy.chmod(0o755)
            apt = ["chroot", str(root), "apt-get", "-y", "-o", "APT::Sandbox::User=root",
                   "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]
            run([*apt, "update"], env=env)
            if remove:
                run([*apt, "remove", *remove], env=env)
            if add:
                run([*apt, "install", *add], env=env)
        elif family == "arch":
            lock = root / "var/lib/pacman/db.lck"
            if lock.exists():
                lock.unlink()
            if remove:
                run(["chroot", str(root), "pacman", "-Rns", "--noconfirm", *remove], env=env)
            if add:
                run(["chroot", str(root), "pacman", "-Sy", "--needed", "--noconfirm", *add], env=env)
        elif family == "fedora":
            if remove:
                run(["chroot", str(root), "dnf", "remove", "-y", *remove], env=env)
            if add:
                run(["chroot", str(root), "dnf", "install", "-y", *add], env=env)
        elif family == "suse":
            if remove:
                run(["chroot", str(root), "zypper", "-n", "remove", *remove], env=env)
            if add:
                run(["chroot", str(root), "zypper", "-n", "install", *add], env=env)
        elif family == "alpine":
            if remove:
                run(["chroot", str(root), "apk", "del", *remove], env=env)
            if add:
                run(["chroot", str(root), "apk", "add", *add], env=env)
    finally:
        _restore_resolv(root, saved)
        if policy and policy.exists():
            policy.unlink()


def _mount_api(root, mounts):
    proc = root / "proc"
    proc.mkdir(parents=True, exist_ok=True)
    run(["mount", "-t", "proc", "proc", str(proc)])
    mounts.add(proc)
    dev = root / "dev"
    dev.mkdir(parents=True, exist_ok=True)
    for name in ("null", "zero", "full", "random", "urandom", "tty", "console", "ptmx", "fuse"):
        src = Path("/dev") / name
        if not src.exists():
            continue
        dest = dev / name
        if dest.is_symlink():
            dest.unlink()
        if not dest.exists():
            dest.touch()
        run(["mount", "--bind", str(src), str(dest)])
        mounts.add(dest)
    pts = dev / "pts"
    pts.mkdir(exist_ok=True)
    result = run(["mount", "-t", "devpts", "devpts", str(pts)], ok=(0, 32))
    if result.returncode == 0:
        mounts.add(pts)


def _inject_resolv(root):
    dest = root / "etc/resolv.conf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    saved = root / "etc/resolv.conf.tuxutil-saved"
    if dest.is_symlink():
        saved.write_text("symlink:" + os.readlink(dest))
        dest.unlink()
    elif dest.exists():
        shutil.copy2(dest, saved)
    else:
        saved.write_text("missing")
    shutil.copyfile("/etc/resolv.conf", dest)
    return saved


def _restore_resolv(root, saved):
    dest = root / "etc/resolv.conf"
    if not saved or not saved.exists():
        return
    marker = saved.read_text(errors="replace") if saved.is_file() else ""
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    if marker.startswith("symlink:"):
        dest.symlink_to(marker[len("symlink:"):])
        saved.unlink()
    elif marker == "missing":
        saved.unlink()
    else:
        shutil.move(str(saved), str(dest))


def _chroot_env():
    env = os.environ.copy()
    env.update({
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "DEBIAN_FRONTEND": "noninteractive",
        "LANG": "C",
        "LC_ALL": "C",
        "HOME": "/root",
    })
    return env


def _umount_children(root, mounts):
    """Drop API mounts under the root before it is packed. Overlay mounts stay until finally."""
    kept = []
    for point in mounts.points:
        try:
            inside = Path(point).resolve().is_relative_to(root.resolve())
        except (OSError, ValueError):
            inside = str(point).startswith(str(root) + os.sep) or Path(point) == root
        if inside and Path(point) != root:
            subprocess.run(["umount", "-l", str(point)], check=False)
        else:
            kept.append(point)
    mounts.points = kept


def _metadata_and_boot(iso, paths, scratch, info, label, args, root, changed, before, after, pkg_family):
    """Return (deletes, maps) where maps is {iso path: host file}."""
    maps = {}
    work = scratch / "meta"
    work.mkdir(parents=True, exist_ok=True)
    built = changed[info["layers"][-1] if info.get("layers") else info["squashfs"]]
    # `changed` was keyed by iso path already.
    iso_to_host = dict(changed)

    kernel_args = list(args.kernel_arg or [])
    new_label = args.label or ""
    for path in paths:
        if not is_label_carrier(path):
            continue
        dest = work / "boot" / path.lstrip("/")
        try:
            extract_file(iso, path, dest)
        except SystemExit:
            continue
        if not dest.is_file():
            continue
        data = dest.read_bytes()
        if _is_fat(data):
            patched = _patch_fat(dest, old_label=label, new_label=new_label,
                                 kernel_args=kernel_args, timeout=args.default_timeout)
            if patched:
                iso_to_host[path] = dest
            continue
        if is_boot_config(path) and b"\x00" not in data[:200]:
            text = data.decode("utf-8", "replace")
            updated = patch_boot_text(text, old_label=label, new_label=new_label,
                                      kernel_args=kernel_args, timeout=args.default_timeout,
                                      kind=boot_kind(path, text))
            if updated != text:
                dest.write_text(updated)
                iso_to_host[path] = dest
            continue
        if new_label and label and new_label != label and label.encode() in data:
            dest.write_bytes(patch_embedded_label(data, label, new_label))
            iso_to_host[path] = dest

    primary = info["squashfs"]
    if info["family"] == "arch":
        digest = _hash_file(built, "sha512")
        sha_name = Path(primary).name
        sha_iso = str(Path(primary).parent / (Path(primary).stem + ".sha512"))
        if not sha_iso.endswith(".sha512"):
            sha_iso = str(Path(primary).with_suffix(".sha512"))
        # airootfs.sfs -> airootfs.sha512 (stem of the double suffix is airootfs.sfs's stem)
        sha_iso = str(Path(primary).parent / (Path(primary).name.split(".")[0] + ".sha512"))
        existing = [p for p in paths if p.endswith("/airootfs.sha512") and p.rsplit("/", 1)[0] == primary.rsplit("/", 1)[0]]
        sha_iso = existing[0] if existing else sha_iso
        sha_host = work / "airootfs.sha512"
        sha_host.write_text(format_sha512(digest, sha_name))
        iso_to_host[sha_iso] = sha_host
    if info["family"] in {"ubuntu", "debian"} or primary.startswith("/casper") or "/filesystem.squashfs" in primary:
        _write_size_and_manifest(work, root, primary, paths, before, after, pkg_family, iso_to_host, built)
    if "/md5sum.txt" in paths:
        old = _read_small(iso, "/md5sum.txt")
        updates = {}
        for iso_path, host in iso_to_host.items():
            if Path(host).is_file():
                updates[iso_path] = _hash_file(host, "md5")
        rendered = update_md5sum(old, updates)
        md5_host = work / "md5sum.txt"
        md5_host.write_text(rendered)
        iso_to_host["/md5sum.txt"] = md5_host
        # The md5sum file itself is not usually listed inside itself. Re-hash if it is.
        if "/md5sum.txt" in {_md5_key(line.split(None, 1)[1]) for line in old.splitlines() if line.strip() and len(line.split(None, 1)) == 2}:
            updates["/md5sum.txt"] = _hash_file(md5_host, "md5")
            md5_host.write_text(update_md5sum(old, updates))

    deletes = stale_signature_paths(paths, set(iso_to_host) | {primary})
    # Don't delete a file we are also mapping.
    deletes = [path for path in deletes if path not in iso_to_host]
    return deletes, iso_to_host


def _write_size_and_manifest(work, root, primary, paths, before, after, pkg_family, iso_to_host, built):
    size_path = _sibling(paths, primary, ".size")
    if size_path or primary.endswith(".squashfs"):
        size_path = size_path or (str(Path(primary).with_suffix(".size")) if primary.endswith(".squashfs") else "")
    if size_path:
        # filesystem.size is the unpacked byte size casper shows while copying.
        result = run(["du", "-sx", "-B1", str(root)], capture=True)
        host = work / Path(size_path).name
        host.write_text(result.stdout.split()[0] + "\n")
        iso_to_host[size_path] = host
    if pkg_family == "debian":
        man_path = _sibling(paths, primary, ".manifest")
        if man_path or "/casper/filesystem.manifest" in paths or "/live/filesystem.packages" in paths:
            if not man_path and "/casper/filesystem.manifest" in paths and primary.endswith("filesystem.squashfs"):
                man_path = "/casper/filesystem.manifest"
            if man_path:
                old = _read_small(iso, man_path) if man_path in paths else ""
                host = work / Path(man_path).name
                host.write_text(update_manifest(old, before, after))
                iso_to_host[man_path] = host


def _sibling(paths, primary, suffix):
    """casper/minimal.standard.live.squashfs -> casper/minimal.standard.live.size."""
    if not primary.endswith(".squashfs") and not primary.endswith(".sfs"):
        return ""
    stem = primary[: primary.rfind(".")]
    candidate = stem + suffix
    if candidate in paths:
        return candidate
    return ""


def _patch_fat(image, *, old_label, new_label, kernel_args, timeout):
    env = os.environ.copy()
    env["MTOOLS_SKIP_CHECK"] = "1"
    listed = subprocess.run(["mdir", "-i", str(image), "-s", "-b", "::"], capture_output=True, text=True, env=env)
    if listed.returncode != 0:
        return False
    changed = False
    with tempfile.TemporaryDirectory() as tmp:
        for line in listed.stdout.splitlines():
            inner = line.strip()
            if not inner or inner.endswith("/"):
                continue
            low = inner.lower()
            if not any(low.endswith(ext) for ext in (".cfg", ".conf", ".txt")):
                continue
            local = Path(tmp) / Path(inner.replace("::", "").lstrip("/")).name
            got = subprocess.run(["mcopy", "-i", str(image), "-n", inner, str(local)],
                                 capture_output=True, text=True, env=env)
            if got.returncode != 0 or not local.is_file():
                continue
            raw = local.read_bytes()
            if b"\x00" in raw[:80]:
                continue
            text = raw.decode("utf-8", "replace")
            updated = patch_boot_text(text, old_label=old_label, new_label=new_label,
                                      kernel_args=kernel_args, timeout=timeout,
                                      kind=boot_kind(inner, text))
            if updated == text:
                continue
            local.write_text(updated)
            put = subprocess.run(["mcopy", "-i", str(image), "-o", str(local), inner],
                                 capture_output=True, text=True, env=env)
            if put.returncode != 0:
                raise SystemExit(f"mcopy failed to update {inner} inside {image}: {put.stderr.strip()}")
            changed = True
    return changed


def _is_fat(data):
    if len(data) < 0x60:
        return False
    return data[0x36:0x3B] in {b"FAT12", b"FAT16"} or data[0x52:0x58] == b"FAT32 " or data[0x36:0x3A] == b"FAT "


def _hash_file(path, kind):
    digest = hashlib.sha512() if kind == "sha512" else hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_tree(src, dst):
    dst.mkdir(parents=True, exist_ok=True)
    result = run(["cp", "-a", str(src) + "/.", str(dst) + "/"], ok=(0, 1))
    if result.returncode == 0:
        return
    for dirpath, dirnames, filenames in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        target = dst / rel
        target.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            source = Path(dirpath) / name
            if source.is_symlink():
                link = target / name
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(os.readlink(source))
                continue
            mode = source.lstat().st_mode
            if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
                continue
            shutil.copy2(source, target / name)


class _Mounts:
    def __init__(self):
        self.points = []

    def add(self, point):
        self.points.append(point)

    def umount_all(self):
        for point in reversed(self.points):
            subprocess.run(["umount", "-l", str(point)], check=False)
            subprocess.run(["fusermount", "-uz", str(point)], check=False)
        self.points.clear()


def run(cmd, *, ok=(0,), capture=False, env=None):
    result = system.run(cmd, check=False, capture=capture, env=env)
    if result.returncode not in ok:
        raise SystemExit(f"Command failed ({result.returncode}): {shlex.join(map(str, cmd))}")
    return result


def scrub(path):
    path = Path(path)
    if not path.exists():
        return
    # Files created as non-root inside the user namespace are owned by subuids.
    # The calling user cannot unlink those; root inside a fresh user namespace can.
    subprocess.run(["unshare", "--map-auto", "--map-root-user", "chmod", "-R", "u+w", str(path)],
                   check=False)
    subprocess.run(["unshare", "--map-auto", "--map-root-user", "rm", "-rf", "--", str(path)],
                   check=False)


def _warn(message):
    print(f"linuxiso: {message}", file=sys.stderr)
