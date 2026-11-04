# TuxUtil

[winutil](https://github.com/ChrisTitusTech/winutil), but for Linux. It detects your distro and gives you one place to install apps, apply tweaks, update the system, build custom Windows 11 and Linux ISOs, and flash them to a USB stick. Everything works from a GUI or the command line.

> **Early release.** The ISO builders are tested in virtual machines, but several features have never run on real hardware. See [What has been tested](#what-has-been-tested) before you rely on it. Flashing erases the target drive.

## Install

```sh
git clone https://github.com/SusieeTheLinuxUser/tuxutil.git
cd tuxutil
sh install.sh --from-local .
```

This puts `tuxutil` in `~/.local/bin`. Add that folder to your `PATH` if it isn't there already. You need Python 3.10 or newer. The GUI also needs PySide6. Run `tuxutil doctor` to see which tools are missing, or `tuxutil doctor --install` to install them.

## Use it

Run `tuxutil` with no arguments to open the GUI. Add `--dry-run` before any command to print what it would change without changing it. Read-only checks, such as listing drives, still run.

```sh
tuxutil doctor                 # distro, tools, and what is missing
tuxutil update                 # update the system
tuxutil apps list --json
tuxutil apps install ID...
tuxutil tweaks list --json
tuxutil tweaks apply ID...     # tweaks undo ID... reverses them
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

A flash can't be cancelled from the GUI yet, because the writer runs as root. If you close the window it keeps writing, so don't remove the drive until its activity stops.

## What has been tested

Tests ran in QEMU/KVM on image files. **Nothing has been tested on real hardware yet.**

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
- Applying tweaks or installing apps
- Anything that needs root

Please open an issue if you try any of these, whichever way it goes.

## License

MIT
