"""Command line entry point. Each feature module adds its own subcommand via register()."""
import argparse
import json
import shlex
import shutil
import subprocess
import sys

from . import __version__, apps, flash, gui, linuxiso, system, tweaks, winiso


def cmd_doctor(args):
    distro = system.detect()
    missing = system.missing_tools(system.TOOL_PACKAGES)
    if args.json:
        print(json.dumps({"id": distro.id, "name": distro.name, "family": distro.family, "missing": missing}))
        return 0
    print(f"Distro: {distro.name} (family: {distro.family})")
    print("Missing tools: " + (", ".join(missing) or "none"))
    if args.install and missing:
        if distro.family not in system.INSTALL_CMD:
            raise SystemExit(f"Don't know how to install packages on family '{distro.family}'.")
        pkgs = sorted({system.tool_package(t, distro.family) for t in missing})
        system.run(system.as_root([*system.INSTALL_CMD[distro.family], *pkgs]))
    return 0


def cmd_update(args):
    distro = system.detect()
    cmds = system.UPDATE_CMDS.get(distro.family, [])
    if cmds:
        system.run(system.as_root_all(cmds))
    if shutil.which("flatpak"):
        system.run(["flatpak", "update", "-y"])
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tuxutil", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--dry-run", action="store_true", help="print commands instead of running them")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("doctor", help="show distro and missing external tools")
    p.add_argument("--json", action="store_true")
    p.add_argument("--install", action="store_true", help="install missing tools")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("update", help="update the system and flatpaks")
    p.set_defaults(func=cmd_update)

    for module in (apps, tweaks, winiso, linuxiso, flash, gui):
        module.register(sub)

    args = parser.parse_args(argv)
    system.DRY_RUN = args.dry_run
    if not args.command:  # like winutil: no arguments opens the GUI
        args = parser.parse_args(["gui"])
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except subprocess.CalledProcessError as exc:
        rendered = shlex.join(str(part) for part in exc.cmd) if exc.cmd else "command"
        code = exc.returncode if isinstance(exc.returncode, int) and exc.returncode > 0 else 1
        print(f"Command failed (exit {code}): {rendered}", file=sys.stderr)
        return code
    except FileNotFoundError as exc:
        name = exc.filename or "command"
        print(f"Command not found (exit 127): {name}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
