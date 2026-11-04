# TuxUtil: rules for every agent

TuxUtil is winutil for Linux. Priorities, in order: custom Windows 11 ISO, custom Linux ISO, USB flash, then apps/tweaks/updates.

## Architecture
- Python 3.10+, stdlib only outside `gui.py` (PySide6, lazily imported).
- `tuxutil/system.py`: distro detection, `run()`, `as_root()`, `require_tools()`, `progress()`. Reuse it. Never call `subprocess` with a shell.
- One module per feature, each exposing `register(subparsers)`, already wired in `cli.py`. Stay in your own module and its test file so parallel work doesn't conflict.
- The GUI is a frontend over the CLI: it runs `python -m tuxutil ...` as a subprocess and reads stdout. All logic lives behind the CLI.

## CLI contract (the GUI depends on it; change only with the owner's OK)
Global: `tuxutil --dry-run ...` prints commands instead of running them (handled by `system.run`).
Long jobs print `@progress <0-100> <message>` lines via `system.progress()`.
```
tuxutil doctor [--json] [--install]
tuxutil update
tuxutil apps list --json | apps install ID... | apps remove ID...
tuxutil tweaks list --json | tweaks apply ID... | tweaks undo ID...
tuxutil winiso info ISO --json
tuxutil winiso build ISO -o OUT.iso [options]
tuxutil linuxiso info ISO --json
tuxutil linuxiso build ISO -o OUT.iso [options]
tuxutil flash list --json
tuxutil flash write ISO DEVICE --confirm DEVICE [--no-verify]
tuxutil iso list --json
tuxutil iso download [TARGET] [-o FILE | --dir DIR] [--sha256 HEX]    (no TARGET: reads $TUXUTIL_DOWNLOAD_URL)
tuxutil gui
```
Listing commands print exactly this JSON (extra keys are fine):
```
apps list:     [{"id","name","category","description","installed":bool,"available":bool}]
tweaks list:   [{"id","name","category","description","risk":"low|medium|high","applied":bool|null,"available":bool}]
winiso info:   {"editions":[{"index":int,"name","arch","size_bytes":int,"build"}]}
linuxiso info: {"family","squashfs","label","arch","supported":bool,"notes"}
flash list:    [{"path","model","size_bytes":int,"transport","removable":bool,"safe":bool,"reason"}]
iso list:      [{"id","name","kind":"linux|windows","direct":bool,"page","notes"}]
winiso options --json, linuxiso options --json:
               [{"flag":"--name","type":"bool|str|path|list|choice","label","help","default","choices":[...]}]
```
`options --json` describes every `build` option, so the GUI generates its forms from data and new options never need GUI changes.

## Code style
Smallest thing that works. No unrequested abstractions, no new dependencies, no scaffolding for later. Mark deliberate shortcuts with a `ponytail:` comment naming the limit. Non-trivial logic gets a small `unittest` in `tests/test_<module>.py`. Tests must pass in CI with no root, no ISOs and no network.

## Machine safety: hard rules, no exceptions
Agents run on the owner's daily PC, often unattended.
- Never reboot, shut down, suspend, or log out. Never change system config or install system packages.
- Never run sudo, pkexec, su, or doas. They would prompt on the owner's desktop. If something truly needs root, write down what, and test it another way.
- Never write to any `/dev/*` block device, and never attach host disks or USB devices to a VM. `sda` and `sr0` here are real devices. Test flashing against regular image files only.
- Write only inside this repo or your worktree. All large scratch output goes in `.work/<your-task>/` in the main checkout (gitignored). Don't use `/tmp` for ISOs, it is RAM. Delete your scratch when finished, except anything the PR says to keep.
- ISOs in `~/Documents/ISOs/` and `~/Torrents/` are read-only inputs. Never modify, move, rename, or delete them.
- VMs: QEMU with KVM on image files, at most 4 vCPU and 8 GB RAM each, no host passthrough, always killed when done. Kill only processes you started.
- Never touch another project, dotfiles, or `~/.claude`, `~/.codex`, `~/.grok`, `~/.gemini`.

## Git workflow
- `main`: stable. `dev`: integration. Never push to either directly (a local pre-push hook blocks it; GitHub protection isn't available while the repo is private on the Free plan).
- Work on `feat/<name>` or `fix/<name>`, open a PR into `dev`. Don't merge; the orchestrator reviews and merges. Squash merges only, so one PR is one revertable commit.
- Never force-push a branch you don't own. Never commit ISOs, disk images, or secrets.
- To undo a merged change: `git revert <sha>` in a new PR. Every merge to `main` gets a `v0.x.y` tag as a known-good restore point.

## Definition of done
Run the real thing, not only unit tests. Report in the PR what you actually ran and what you saw, including what failed or wasn't tested. Never claim a boot or install you didn't watch happen.
