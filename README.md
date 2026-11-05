# TuxUtil

[winutil](https://github.com/ChrisTitusTech/winutil), but for Linux. It detects your distro and gives you one place to install apps, apply tweaks, update the system, build custom Windows 11 and Linux ISOs, and flash them to a USB stick. Everything works from a GUI or the command line.

> **Early release.** The ISO builders are tested in virtual machines, but several features have never run on real hardware, including USB flashing. See [What has been tested](#what-has-been-tested) before you rely on it. Flashing erases the target drive.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/SusieeTheLinuxUser/tuxutil/main/run.sh | sh
```

This runs the latest code from the `main` branch in your cache; you can read [`run.sh`](run.sh) first. It needs Python 3.10 or newer, plus `curl` or `wget` and `tar`. The GUI downloads PySide6 to the cache if needed (about 260 MB). Run `tuxutil doctor` to see which tools are missing, or `tuxutil doctor --install` to install them.

For a permanent install in `~/.local/bin`:

```sh
git clone https://github.com/SusieeTheLinuxUser/tuxutil.git
cd tuxutil
sh install.sh --from-local .
```

## Use it

Run `tuxutil` with no arguments to open the GUI. Add `--dry-run` before any command to print what it would change without changing it. Read-only checks, such as listing drives, still run.

```sh
tuxutil doctor                 # distro, tools, and what is missing
tuxutil update                 # update the system
tuxutil apps list --json
tuxutil apps install ID...
tuxutil tweaks list --json
tuxutil tweaks apply ID...     # tweaks undo ID... reverses them
tuxutil iso list --json
tuxutil iso download arch --dir ~/Downloads
```

### Custom Windows 11 ISO

No root needed. It skips the TPM, Secure Boot, RAM, network and Microsoft account checks, removes preinstalled apps such as Teams and OneDrive, and creates a local account.

```sh
tuxutil winiso info Win11.iso --json
tuxutil winiso build Win11.iso -o custom.iso --username tux
tuxutil winiso options --json  # every build option
```

By default, Setup stops at the disk screen so you choose the disk. `--wipe-disk` (UEFI only) makes the install unattended and **erases disk 0, with no prompt, on any PC that boots the media**. Label that stick and keep it away from other machines.

`--password` is stored in plain text in the ISO, and it stays there. First logon only removes it from the installed Windows, so keep that ISO private or delete it. Other processes can read command-line flags while a build runs, so on a shared machine set the `TUXUTIL_PASSWORD` environment variable instead. The GUI already does this.

If the stick stays first in the boot order, the PC goes back into Setup after the first reboot. Remove it at that point, or set the internal disk first.

### Download an ISO

The GUI has this in the Source section of both ISO pages. Pick an ISO and press Download, and the finished file fills in the source field. `tuxutil iso list --json` shows the catalog. `tuxutil iso download ID` downloads any Linux entry (Arch, CachyOS, Linux Mint, Ubuntu, Fedora KDE, Pop!_OS, Zorin, Manjaro, Garuda) and checks the official checksum.

Windows 11 is different. Microsoft has no permanent link and blocks scripts from its download service, so TuxUtil doesn't script it. The GUI shows Microsoft's own page in a window: pick your language, press its download button, and TuxUtil takes the file from there. That window needs Qt WebEngine (a separate package on some distros, such as `qt6-webengine` on Arch). Without it the page opens in your browser and you paste the link. A Windows download has no checksum to compare, so TuxUtil prints the file's hash instead. On the command line, pass a copied link to `tuxutil iso download URL`. A pasted link can't be checked unless you also pass `--sha256`, so TuxUtil prints the file's hash for you to compare. Downloads resume from a `.part` file and are saved in `~/Downloads` unless you pass `-o FILE` or `--dir DIR`.

### Custom Linux ISO

No root needed, as long as user namespaces, `/etc/subuid`, `/etc/subgid`, `newuidmap` and `newgidmap` are set up. It edits the live system of an existing ISO.

`--add-pkg` and `--remove-pkg` run the ISO's own package manager and scripts with access to your user's files, so only use them on ISOs you trust.

```sh
tuxutil linuxiso info distro.iso --json
tuxutil linuxiso build distro.iso -o custom.iso \
    --overlay ./my-files --first-boot ./setup.sh \
    --hostname mybox --add-pkg htop --kernel-arg quiet
tuxutil linuxiso options --json  # every build option
```

### Flash a USB stick

```sh
tuxutil flash list --json
tuxutil flash write custom.iso /dev/sdX --confirm /dev/sdX
```

This needs root and asks for it. It refuses any drive that holds your running system or swap, or has a filesystem mounted outside `/media`, `/run/media` and `/mnt`. Filesystems mounted under those folders, such as a USB stick your desktop opened, **are unmounted and erased**, so check the device name. **Everything on the target drive is erased.** Windows ISOs get a FAT32 stick, with the install image split if it is over the FAT32 file size limit. Other ISOs are written as they are. Writes are verified unless you pass `--no-verify`.

Cancel in the GUI stops a flash, and the stick then needs flashing again before use. The window stays open until the flash has stopped.

## What has been tested

ISO tests ran in QEMU/KVM on image files. **No custom ISO or USB stick has been tested on real hardware yet.**

**Real PC:** on one CachyOS desktop, `install.sh` worked, the GUI opened, and the Apps page installed and removed GIMP.

**Downloads:** a full Arch download ran for real, including interrupting and resuming it, and the checksum matched. Ubuntu, Mint and Garuda were only checked for a working link and checksum list. The ISOs they lead to may be newer than the ones in the table below.

**Windows 11:** installed to the desktop from one Windows 11 ISO under UEFI and BIOS, with no TPM and no network, and from a flashed stick image. Other editions, languages and Secure Boot are untested.

**Linux:** "Boots" means the custom ISO booted with the overlay and first-boot script working, and the kernel arguments applied. Installing to disk from a custom ISO is untested for every distro.

| ISO | Result |
|---|---|
| Arch, CachyOS, Nyarch, Mint, Ubuntu, Zorin, Pop!_OS | Boots under UEFI, with `--hostname` working. Arch and Mint also boot under BIOS. |
| Fedora KDE | Boots under UEFI and BIOS, but **with SELinux off**, because labels can't be kept without root. The live system also resets the hostname. |
| Garuda, Manjaro, AnduinOS | Boots under UEFI, but the live system overrides `--hostname`. On Garuda and Manjaro, files in upper image layers also override your overlay. |
| Tails | Boots under BIOS only, the same as the stock ISO. Tails may refuse a modified system, and changing it undermines what Tails is for. |
| Kali, Debian netinst, openSUSE, OpenMediaVault | Refused: these boot an installer, not a live system |
| Bazzite, NixOS, Chimera, TUXEDO | Refused: unsupported layout |

`--add-pkg` ran for real on Arch, Mint and Fedora. It is refused on Ubuntu's layered images. `--remove-pkg` and `--label` have not been boot-tested.

**Never run for real:**

- Writing to a real USB stick (only image files)
- Applying tweaks

Please open an issue if you try any of these, whichever way it goes.

## License

MIT
