#!/usr/bin/env python3
"""Fake tuxutil CLI script for testing the PySide6 desktop GUI."""
import json
import sys
import time


def emit_progress(steps, delay=0.02):
    for percent, msg in steps:
        print(f"@progress {percent} {msg}", flush=True)
        if delay > 0:
            time.sleep(delay)


def main():
    args = sys.argv[1:]
    if not args:
        print("fake_cli: no arguments", file=sys.stderr)
        return 1

    cmd = args[0]

    if cmd == "doctor":
        if "--json" in args:
            data = {
                "id": "arch",
                "name": "Arch Linux",
                "family": "arch",
                "missing": ["xorriso", "wimlib-imagex"],
                "tuxutil": {
                    "version": "0.2.3",
                    "latest": "0.2.4",
                    "update_available": True,
                    "update_url": "https://example.test/tuxutil/releases/latest",
                },
            }
            print(json.dumps(data))
            return 0
        if "--install" in args:
            print("Starting installation of missing tools...", flush=True)
            emit_progress([
                (20, "Resolving package dependencies"),
                (50, "Installing xorriso..."),
                (80, "Installing wimlib..."),
                (100, "All tools installed successfully"),
            ])
            print("Tools installation complete.", flush=True)
            return 0
        print("Distro: Arch Linux (family: arch)\nMissing tools: xorriso, wimlib-imagex")
        return 0

    if cmd == "update":
        print("Starting system update...", flush=True)
        emit_progress([
            (15, "Synchronizing package databases..."),
            (45, "Upgrading core packages..."),
            (75, "Updating Flatpaks..."),
            (100, "System and Flatpaks updated successfully"),
        ])
        print("Update complete.", flush=True)
        return 0

    if cmd == "apps":
        sub = args[1] if len(args) > 1 else ""
        if sub == "list" and "--json" in args:
            apps = [
                {
                    "id": "firefox",
                    "name": "Firefox",
                    "category": "Browsers",
                    "description": "Fast, private web browser from Mozilla",
                    "installed": True,
                    "available": True,
                },
                {
                    "id": "chromium",
                    "name": "Chromium",
                    "category": "Browsers",
                    "description": "Open-source web browser developed by Google",
                    "installed": False,
                    "available": True,
                },
                {
                    "id": "vlc",
                    "name": "VLC Media Player",
                    "category": "Multimedia",
                    "description": "Cross-platform multimedia player and framework",
                    "installed": False,
                    "available": True,
                },
                {
                    "id": "obs-studio",
                    "name": "OBS Studio",
                    "category": "Multimedia",
                    "description": "Software for video recording and live streaming",
                    "installed": False,
                    "available": True,
                },
                {
                    "id": "gimp",
                    "name": "GIMP",
                    "category": "Graphics",
                    "description": "GNU Image Manipulation Program",
                    "installed": True,
                    "available": True,
                },
                {
                    "id": "inkscape",
                    "name": "Inkscape",
                    "category": "Graphics",
                    "description": "Vector graphics editor using SVG format",
                    "installed": False,
                    "available": True,
                },
                {
                    "id": "steam",
                    "name": "Steam",
                    "category": "Gaming",
                    "description": "Digital storefront and launcher for games",
                    "installed": False,
                    "available": True,
                },
                {
                    "id": "htop",
                    "name": "htop",
                    "category": "Utilities",
                    "description": "Interactive process viewer and system monitor",
                    "installed": True,
                    "available": True,
                },
                {
                    "id": "unavailable-tool",
                    "name": "Unavailable Tool",
                    "category": "Utilities",
                    "description": "Tool not available for this distro family",
                    "installed": False,
                    "available": False,
                },
            ]
            print(json.dumps(apps))
            return 0
        if sub in ("install", "remove"):
            target_ids = [a for a in args[2:] if not a.startswith("-")]
            action = "Installing" if sub == "install" else "Removing"
            print(f"{action} apps: {', '.join(target_ids)}", flush=True)
            emit_progress([
                (25, f"{action} dependencies..."),
                (60, f"Processing {len(target_ids)} packages..."),
                (90, "Cleaning caches and updating desktop database..."),
                (100, f"Apps {sub}ed successfully"),
            ])
            print(f"Finished {sub}.", flush=True)
            return 0

    if cmd == "tweaks":
        sub = args[1] if len(args) > 1 else ""
        if sub == "list" and "--json" in args:
            tweaks = [
                {
                    "id": "bbr",
                    "name": "TCP BBR Congestion Control",
                    "category": "Network",
                    "description": "Enables Google BBR algorithm for improved throughput and latency",
                    "risk": "low",
                    "applied": True,
                    "available": True,
                },
                {
                    "id": "swappiness",
                    "name": "Optimize Swappiness (vm.swappiness=10)",
                    "category": "Performance",
                    "description": "Reduces aggressiveness of swapping to preserve RAM responsiveness",
                    "risk": "low",
                    "applied": False,
                    "available": True,
                },
                {
                    "id": "trim-timer",
                    "name": "Enable Weekly SSD TRIM",
                    "category": "Storage",
                    "description": "Activates fstrim.timer to maintain SSD performance and longevity",
                    "risk": "low",
                    "applied": True,
                    "available": True,
                },
                {
                    "id": "dns-doh",
                    "name": "DNS-over-HTTPS (Quad9 / Cloudflare)",
                    "category": "Network",
                    "description": "Encrypts DNS queries via systemd-resolved",
                    "risk": "medium",
                    "applied": False,
                    "available": True,
                },
                {
                    "id": "disable-mitigations",
                    "name": "Disable CPU Speculative Execution Mitigations",
                    "category": "Performance",
                    "description": "Adds mitigations=off to kernel cmdline. Increases performance at expense of security.",
                    "risk": "high",
                    "applied": False,
                    "available": True,
                },
                {
                    "id": "custom-kernel-tuning",
                    "name": "Custom Kernel Sysctl Parameters",
                    "category": "Kernel",
                    "description": "Kernel parameters not applicable to this kernel version",
                    "risk": "medium",
                    "applied": None,
                    "available": False,
                },
            ]
            print(json.dumps(tweaks))
            return 0
        if sub in ("apply", "undo"):
            target_ids = [a for a in args[2:] if not a.startswith("-")]
            action = "Applying" if sub == "apply" else "Reverting"
            print(f"{action} tweaks: {', '.join(target_ids)}", flush=True)
            emit_progress([
                (30, f"{action} configuration files..."),
                (70, "Reloading sysctl and systemd units..."),
                (100, f"Tweaks {sub}ed successfully"),
            ])
            print(f"Finished {sub}.", flush=True)
            return 0

    if cmd == "winiso":
        sub = args[1] if len(args) > 1 else ""
        if sub == "options" and "--json" in args:
            options = [
                {
                    "flag": "--bypass-tpm",
                    "type": "bool",
                    "label": "Bypass TPM 2.0 & Secure Boot",
                    "help": "Bypasses Windows 11 hardware checks during setup",
                    "default": True,
                    "choices": [],
                },
                {
                    "flag": "--bypass-ram",
                    "type": "bool",
                    "label": "Bypass RAM Checks (4GB limit)",
                    "help": "Allows installation on systems with less than 4GB RAM",
                    "default": True,
                    "choices": [],
                },
                {
                    "flag": "--edition",
                    "type": "choice",
                    "label": "Target Windows Edition",
                    "help": "Windows edition to select during automated setup",
                    "default": "Windows 11 Pro",
                    "choices": ["Windows 11 Home", "Windows 11 Pro", "Windows 11 Enterprise", "Windows 11 IoT Enterprise"],
                },
                {
                    "flag": "--unattend-user",
                    "type": "str",
                    "label": "Local Admin Username",
                    "help": "Default local administrator account username",
                    "default": "User",
                    "choices": [],
                },
                {
                    "flag": "--unattend-password",
                    "type": "str",
                    "label": "Local Admin Password",
                    "help": "Password for the local administrator account",
                    "default": "",
                    "choices": [],
                },
                {
                    "flag": "--autounattend",
                    "type": "path",
                    "label": "Custom autounattend.xml File",
                    "help": "Optional path to custom answer XML file",
                    "default": "",
                    "choices": [],
                },
                {
                    "flag": "--drivers",
                    "type": "list",
                    "label": "Extra Driver Folders (INF)",
                    "help": "Directories containing extra INF drivers to integrate into boot.wim and install.wim",
                    "default": [],
                    "choices": [],
                },
            ]
            print(json.dumps(options))
            return 0
        if sub == "info":
            iso_path = args[2] if len(args) > 2 else ""
            if "--json" in args:
                data = {
                    "editions": [
                        {"index": 1, "name": "Windows 11 Home", "arch": "x64", "size_bytes": 4823449600, "build": "22631.2861"},
                        {"index": 2, "name": "Windows 11 Pro", "arch": "x64", "size_bytes": 4912384000, "build": "22631.2861"},
                        {"index": 3, "name": "Windows 11 Enterprise", "arch": "x64", "size_bytes": 4895010000, "build": "22631.2861"},
                        {"index": 4, "name": "Windows 11 Education", "arch": "x64", "size_bytes": 4872120000, "build": "22631.2861"},
                    ]
                }
                print(json.dumps(data))
                return 0
        if sub == "build":
            print(f"Building Windows ISO: {' '.join(args[2:])}", flush=True)
            emit_progress([
                (10, "Extracting ISO files..."),
                (35, "Mounting and modifying install.wim..."),
                (65, "Injecting unattended configurations & drivers..."),
                (85, "Creating bootable hybrid UDF/ISO..."),
                (100, "Windows ISO build completed successfully"),
            ])
            print("Windows ISO build complete.", flush=True)
            return 0

    if cmd == "linuxiso":
        sub = args[1] if len(args) > 1 else ""
        if sub == "options" and "--json" in args:
            options = [
                {
                    "flag": "--packages",
                    "type": "list",
                    "label": "Additional Packages to Install",
                    "help": "Packages to inject into the live squashfs filesystem",
                    "default": ["neovim", "htop"],
                    "choices": [],
                },
                {
                    "flag": "--hostname",
                    "type": "str",
                    "label": "Live Environment Hostname",
                    "help": "Hostname for the live environment",
                    "default": "tuxutil-live",
                    "choices": [],
                },
                {
                    "flag": "--root-password",
                    "type": "str",
                    "label": "Root Password",
                    "help": "Password for root in the live environment",
                    "default": "",
                    "choices": [],
                },
                {
                    "flag": "--overlay-dir",
                    "type": "path",
                    "label": "Filesystem Overlay Directory",
                    "help": "Directory whose contents will overlay the root filesystem",
                    "default": "",
                    "choices": [],
                },
                {
                    "flag": "--enable-sshd",
                    "type": "bool",
                    "label": "Enable OpenSSH Server",
                    "help": "Automatically start sshd service on boot",
                    "default": False,
                    "choices": [],
                },
            ]
            print(json.dumps(options))
            return 0
        if sub == "info":
            iso_path = args[2] if len(args) > 2 else ""
            if "--json" in args:
                if "unsupported" in iso_path.lower():
                    data = {
                        "family": "unknown",
                        "squashfs": "",
                        "label": "CUSTOM_UNKNOWN",
                        "arch": "x86_64",
                        "supported": False,
                        "notes": "Unsupported ISO layout: squashfs image not found. Only Arch, Debian/Ubuntu, and Fedora live ISOs are supported.",
                    }
                else:
                    data = {
                        "family": "arch",
                        "squashfs": "airootfs.sfs",
                        "label": "ARCH_202611",
                        "arch": "x86_64",
                        "supported": True,
                        "notes": "Arch Linux live media detected. Kernel: Linux 6.12. Squashfs size: ~850MB.",
                    }
                print(json.dumps(data))
                return 0
        if sub == "build":
            print(f"Customizing Linux ISO: {' '.join(args[2:])}", flush=True)
            emit_progress([
                (15, "Unpacking squashfs filesystem..."),
                (45, "Running chroot customizations..."),
                (75, "Recompressing squashfs..."),
                (90, "Regenerating initramfs and bootloader..."),
                (100, "Linux ISO build completed successfully"),
            ])
            print("Linux ISO customization complete.", flush=True)
            return 0

    if cmd == "flash":
        sub = args[1] if len(args) > 1 else ""
        if sub == "list" and "--json" in args:
            devices = [
                {
                    "path": "/dev/sda",
                    "model": "Samsung SSD 980 1TB",
                    "size_bytes": 1000204886016,
                    "transport": "nvme",
                    "removable": False,
                    "safe": False,
                    "reason": "System root drive (mounted on /)",
                },
                {
                    "path": "/dev/sdb",
                    "model": "SanDisk Ultra USB 3.0",
                    "size_bytes": 32010928128,
                    "transport": "usb",
                    "removable": True,
                    "safe": True,
                    "reason": "",
                },
                {
                    "path": "/dev/sdc",
                    "model": "Kingston DataTraveler 3.0",
                    "size_bytes": 16005464064,
                    "transport": "usb",
                    "removable": True,
                    "safe": True,
                    "reason": "",
                },
                {
                    "path": "/dev/sr0",
                    "model": "HL-DT-ST DVDRAM",
                    "size_bytes": 0,
                    "transport": "sata",
                    "removable": True,
                    "safe": False,
                    "reason": "Optical disc drive (read-only)",
                },
            ]
            print(json.dumps(devices))
            return 0
        if sub == "write":
            # Syntax: flash write ISO DEVICE --confirm DEVICE [--no-verify]
            # args: ["flash", "write", iso, device, "--confirm", confirm_device, ...]
            iso = args[2] if len(args) > 2 else ""
            device = args[3] if len(args) > 3 else ""
            confirm_val = None
            if "--confirm" in args:
                idx = args.index("--confirm")
                if idx + 1 < len(args):
                    confirm_val = args[idx + 1]

            if not confirm_val or confirm_val != device:
                print(f"Error: Confirmation mismatch or missing. Expected --confirm {device}, got {confirm_val}", file=sys.stderr)
                return 1

            print(f"Flashing {iso} to {device}...", flush=True)
            emit_progress([
                (10, "Unmounting existing partitions..."),
                (30, "Writing raw image data (1.2 GB / 4.8 GB)..."),
                (65, "Writing raw image data (3.5 GB / 4.8 GB)..."),
                (90, "Syncing disk buffers..."),
                (98, "Verifying checksum..."),
                (100, "Flash completed and verified successfully"),
            ])
            print("Flash completed and verified successfully. Write operation complete.", flush=True)
            return 0

    if cmd == "iso":
        if args[1] == "list":
            print(json.dumps([
                {"id": "arch", "name": "Arch Linux", "kind": "linux", "direct": True, "page": "https://example.test/arch", "notes": ""},
                {"id": "win11", "name": "Windows 11", "kind": "windows", "direct": False,
                 "page": "https://example.test/win", "notes": "Links expire."},
            ]))
            return 0
        emit_progress([(50, "Downloading")])
        print("@saved /fake/path/archlinux-live.iso", flush=True)
        return 0

    if cmd == "test-long":
        # Simulates a slow long job to test cancellation and progress updates
        delay = float(args[1]) if len(args) > 1 else 0.5
        print("Starting slow test job...", flush=True)
        for i in range(1, 101):
            emit_progress([(i, f"Step {i} of 100")], delay=delay)
        print("Finished test long job.", flush=True)
        return 0

    if cmd == "test-fail":
        print("Starting job that will fail...", flush=True)
        emit_progress([(25, "Doing first phase...")], delay=0.01)
        print("Fatal error: simulated failure encountered!", file=sys.stderr)
        return 42

    if cmd == "test-bad-json":
        print("This is not valid JSON at all: <<{{{")
        return 0

    print(f"fake_cli: unrecognized command {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
