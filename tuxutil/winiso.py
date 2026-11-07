"""Build a custom Windows 11 ISO (the MicroWin equivalent), rootless.

Uses 7z to extract ISO contents (UDF), wimlib-imagex to export/update the WIM,
and xorriso to write an El Torito ISO. No root, no FUSE, no loop mounts.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from html import escape as html_escape
from typing import List, Optional

from . import system

REQUIRED_TOOLS = ["7z", "wimlib-imagex", "xorriso"]

# Stay under the 4 GiB ISO9660/Joliet file limit Windows Setup can read.
# ponytail: xorriso here is not used to write UDF, so every file must fit.
ISO_FILE_LIMIT = 4 * 1024 ** 3 - 1024 * 1024
SPLIT_MIB = "3800"

# (package name, removed by default). Online removal only — deleting the
# WindowsApps payload while the package is still registered breaks Setup.
BLOAT_APPS = [
    ("Microsoft.BingNews", True),
    ("Microsoft.BingWeather", True),
    ("Microsoft.GetHelp", True),
    ("Microsoft.Getstarted", True),
    ("Microsoft.MicrosoftOfficeHub", True),
    ("Microsoft.MicrosoftSolitaireCollection", True),
    ("Microsoft.MicrosoftStickyNotes", False),
    ("Microsoft.People", True),
    ("Microsoft.PowerAutomateDesktop", True),
    ("Microsoft.Todos", False),
    ("Microsoft.WindowsAlarms", False),
    ("Microsoft.WindowsCommunicationsApps", True),
    ("Microsoft.WindowsFeedbackHub", True),
    ("Microsoft.WindowsMaps", True),
    ("Microsoft.WindowsSoundRecorder", False),
    ("Microsoft.Xbox.TCUI", True),
    ("Microsoft.XboxGameOverlay", True),
    ("Microsoft.XboxGamingOverlay", True),
    ("Microsoft.XboxIdentityProvider", True),
    ("Microsoft.XboxSpeechToTextOverlay", True),
    ("Microsoft.XboxApp", True),
    ("Microsoft.GamingApp", True),
    ("Microsoft.YourPhone", True),
    ("Microsoft.ZuneMusic", True),
    ("Microsoft.ZuneVideo", True),
    ("Clipchamp.Clipchamp", True),
    ("Microsoft.549981C3F5F10", True),  # Cortana
    ("Microsoft.Windows.DevHome", True),
    ("Microsoft.WindowsCamera", False),
    ("Microsoft.ScreenSketch", False),
    ("Microsoft.Windows.Photos", False),
    ("Microsoft.WindowsCalculator", False),
    ("Microsoft.WindowsStore", False),
    ("Microsoft.WindowsTerminal", False),
    ("Microsoft.WindowsNotepad", False),
    ("Microsoft.Paint", False),
    ("Microsoft.OutlookForWindows", True),
    ("Microsoft.Copilot", True),
    ("Microsoft.MicrosoftTeams", True),
    ("MicrosoftTeams", True),
    ("MSTeams", True),
]

# Files that are not registered provisioned packages, so deleting them is safe.
ONEDRIVE_SETUP = [
    "/Windows/System32/OneDriveSetup.exe",
    "/Windows/SysWOW64/OneDriveSetup.exe",
]

# Public setup-only keys (they select an edition; they do not activate Windows).
_PRODUCT_KEYS = {
    "Core": "YTMG3-N6DKC-DKB77-7M9GH-8HVX7",
    "CoreN": "4CPRK-NM3K3-X6XXQ-RXX86-WXCHW",
    "CoreSingleLanguage": "BT79Q-G7N6G-PGBYW-4YWX6-6F4BT",
    "Professional": "VK7JG-NPHTM-C97JM-9MPGT-3V66T",
    "ProfessionalN": "2B87N-8KFHP-DKV6R-Y2C8J-PKCKT",
    "ProfessionalEducation": "8PTT6-RNW4C-6V7J2-C2D3X-MHBPB",
    "ProfessionalEducationN": "GJTYN-HDMQY-FRR76-HVGC7-QPF8P",
    "ProfessionalWorkstation": "DXG7C-N36C4-C4HTG-X4T3X-2YV77",
    "ProfessionalWorkstationN": "WYPNQ-8C467-V2W6J-TX4WX-WT2RQ",
    "Education": "YNMGQ-8RYV3-4PGQ3-C8XTP-7CFBY",
    "EducationN": "84NGF-MHBT6-FXBX8-QWJK7-DRR8H",
    "Enterprise": "NPPR9-FWDCX-D2C8J-H872K-2YT43",
    "EnterpriseN": "DPH2V-TTNVB-4X9Q3-TJR4H-KHJW4",
}

# ponytail: the WIM has a default language but no timezone. Map the language.
_LOCALE_DEFAULTS = {
    "en-US": ("0409:00000409", "Pacific Standard Time"),
    "en-GB": ("0809:00000809", "GMT Standard Time"),
    "de-DE": ("0407:00000407", "W. Europe Standard Time"),
    "fr-FR": ("040c:0000040c", "Romance Standard Time"),
    "es-ES": ("0c0a:0000040a", "Romance Standard Time"),
    "it-IT": ("0410:00000410", "W. Europe Standard Time"),
    "pt-BR": ("0416:00000416", "E. South America Standard Time"),
    "pt-PT": ("0816:00000816", "GMT Standard Time"),
    "nl-NL": ("0413:00000413", "W. Europe Standard Time"),
    "pl-PL": ("0415:00000415", "Central European Standard Time"),
    "ru-RU": ("0419:00000419", "Russian Standard Time"),
    "ja-JP": ("0411:00000411", "Tokyo Standard Time"),
    "ko-KR": ("0412:00000412", "Korea Standard Time"),
    "zh-CN": ("0804:00000804", "China Standard Time"),
    "zh-TW": ("0404:00000404", "Taipei Standard Time"),
    "sv-SE": ("041d:0000041d", "W. Europe Standard Time"),
    "da-DK": ("0406:00000406", "Romance Standard Time"),
    "fi-FI": ("040b:0000040b", "FLE Standard Time"),
    "nb-NO": ("0414:00000414", "W. Europe Standard Time"),
    "cs-CZ": ("0405:00000405", "Central Europe Standard Time"),
    "hu-HU": ("040e:0000040e", "Central Europe Standard Time"),
    "tr-TR": ("041f:0000041f", "Turkey Standard Time"),
}

REGISTRY_TWEAKS = [
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection",
     "AllowTelemetry", "REG_DWORD", "0"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot",
     "TurnOffWindowsCopilot", "REG_DWORD", "1"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsAI",
     "DisableAIDataAnalysis", "REG_DWORD", "1"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsAI",
     "AllowRecallEnablement", "REG_DWORD", "0"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\CloudContent",
     "DisableWindowsConsumerFeatures", "REG_DWORD", "1"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\CloudContent",
     "DisableSoftLanding", "REG_DWORD", "1"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\CloudContent",
     "DisableTailoredExperiencesWithDiagnosticData", "REG_DWORD", "1"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System",
     "EnableActivityFeed", "REG_DWORD", "0"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System",
     "PublishUserActivities", "REG_DWORD", "0"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\LocationAndSensors",
     "DisableLocation", "REG_DWORD", "1"),
]

# Per-user defaults, applied by loading the Default user hive.
_USER_HIVE_TWEAKS = [
    (r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
     "SilentInstalledAppsEnabled", "0"),
    (r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
     "SystemPaneSuggestionsEnabled", "0"),
    (r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
     "SubscribedContent-338388Enabled", "0"),
    (r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
     "SubscribedContent-338389Enabled", "0"),
    (r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
     "SubscribedContent-353694Enabled", "0"),
    (r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
     "SubscribedContent-353696Enabled", "0"),
    (r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
     "SubscribedContent-310093Enabled", "0"),
    (r"Software\Policies\Microsoft\Windows\WindowsCopilot",
     "TurnOffWindowsCopilot", "1"),
    (r"Software\Policies\Microsoft\Windows\WindowsAI",
     "DisableAIDataAnalysis", "1"),
]

_WCM_NS = "http://schemas.microsoft.com/WMIConfig/2002/State"
_UNATTEND_NS = "urn:schemas-microsoft-com:unattend"
_ARCH_MAP = {"x86_64": "amd64", "amd64": "amd64", "x86": "x86", "i386": "x86",
             "arm64": "arm64", "aarch64": "arm64"}


# Erases disk 0 with no prompt, so it is opt-in: the default leaves disk choice to the
# person at Setup's disk screen. Booting this media on the wrong PC would destroy data.
# ponytail: GPT layout, UEFI boot only. Legacy BIOS installs use Setup's disk screen.
_WIPE_DISK0 = [
        '      <DiskConfiguration>',
        '        <Disk wcm:action="add">',
        "          <DiskID>0</DiskID>",
        "          <WillWipeDisk>true</WillWipeDisk>",
        "          <CreatePartitions>",
        '            <CreatePartition wcm:action="add">',
        "              <Order>1</Order>",
        "              <Type>EFI</Type>",
        "              <Size>260</Size>",
        "            </CreatePartition>",
        '            <CreatePartition wcm:action="add">',
        "              <Order>2</Order>",
        "              <Type>MSR</Type>",
        "              <Size>128</Size>",
        "            </CreatePartition>",
        '            <CreatePartition wcm:action="add">',
        "              <Order>3</Order>",
        "              <Type>Primary</Type>",
        "              <Extend>true</Extend>",
        "            </CreatePartition>",
        "          </CreatePartitions>",
        "          <ModifyPartitions>",
        '            <ModifyPartition wcm:action="add">',
        "              <Order>1</Order>",
        "              <PartitionID>1</PartitionID>",
        "              <Format>FAT32</Format>",
        "              <Label>System</Label>",
        "            </ModifyPartition>",
        '            <ModifyPartition wcm:action="add">',
        "              <Order>2</Order>",
        "              <PartitionID>2</PartitionID>",
        "            </ModifyPartition>",
        '            <ModifyPartition wcm:action="add">',
        "              <Order>3</Order>",
        "              <PartitionID>3</PartitionID>",
        "              <Format>NTFS</Format>",
        "              <Label>Windows</Label>",
        "              <Letter>C</Letter>",
        "            </ModifyPartition>",
        "          </ModifyPartitions>",
        "        </Disk>",
        "        <WillShowUI>OnError</WillShowUI>",
        "      </DiskConfiguration>",
]
_INSTALL_TO = [
    "          <InstallTo>",
    "            <DiskID>0</DiskID>",
    "            <PartitionID>3</PartitionID>",
    "          </InstallTo>",
    "          <InstallToAvailablePartition>false</InstallToAvailablePartition>",
]


@dataclass
class Edition:
    index: int
    name: str
    arch: str
    size_bytes: int
    build: str
    edition_id: str = ""
    lang: str = ""


@dataclass
class BuildOptions:
    edition: str = ""
    remove_apps: Optional[List[str]] = None
    keep_apps: Optional[List[str]] = None
    remove_onedrive: bool = True
    remove_edge: bool = False
    apply_tweaks: bool = True
    bypass_requirements: bool = True
    local_username: str = ""
    local_password: str = field(default="", repr=False)
    locale: str = ""
    keyboard: str = ""
    timezone: str = ""
    driver_dirs: Optional[List[str]] = None
    disable_hibernation: bool = True
    compact_os: bool = False
    compression: str = "solid"
    wipe_disk: bool = False


def _xml_escape(s: str) -> str:
    return html_escape(s, quote=True)


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _parse_wiminfo(text: str) -> list:
    editions = []
    current = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Index:"):
            if current:
                editions.append(current)
            current = {"Index": line.split(":", 1)[1].strip()}
        elif ":" in line and current:
            key, val = line.split(":", 1)
            current[key.strip()] = val.strip()
    if current:
        editions.append(current)

    result = []
    for e in editions:
        idx = int(e.get("Index", 0) or 0)
        if idx == 0:
            continue
        name = e.get("Name", e.get("Display Name", f"Image {idx}"))
        arch = e.get("Architecture", "unknown")
        total = int(e.get("Total Bytes", 0) or 0)
        build_num = e.get("Build", "0")
        sp_build = e.get("Service Pack Build", "0")
        build_str = f"{build_num}.{sp_build}" if sp_build not in ("", "0") else build_num
        result.append(Edition(
            idx, name, arch, total, build_str,
            e.get("Edition ID", ""), e.get("Default Language", ""),
        ))
    return result


def _run(cmd, **kwargs):
    """Run a command. Failures become SystemExit and never include file contents."""
    try:
        return system.run(cmd, **kwargs)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Command failed ({exc.returncode}): {shlex.join(map(str, cmd))}") from exc


def _validate_iso(iso_path: str):
    if not os.path.isfile(iso_path):
        raise SystemExit(f"ISO not found: {iso_path}")
    # query, not run: a read-only probe, so it still runs in --dry-run mode
    # (run would return a stub with no listing and reject every ISO).
    result = system.query(["7z", "l", iso_path, "sources/install.wim", "sources/install.esd"],
                          timeout=120)
    if result.returncode != 0:
        detail = f"\n{result.stderr.strip()}" if result.stderr and result.stderr.strip() else ""
        raise SystemExit(f"Cannot read ISO: {iso_path}{detail}")
    listing = (result.stdout or "").lower()
    if "install.wim" not in listing and "install.esd" not in listing:
        raise SystemExit(
            f"Not a Windows installation ISO (no sources/install.wim or install.esd): {iso_path}"
        )


def _resolve_edition(editions: list, spec: str) -> Edition:
    if not editions:
        raise SystemExit("No Windows editions found in the install image.")
    if not spec:
        for ed in editions:
            folded = ed.name.lower()
            if ("pro" in folded and "education" not in folded
                    and " n" not in folded and "workstation" not in folded):
                return ed
        return editions[0]

    try:
        idx = int(spec)
    except ValueError:
        idx = None
    if idx is not None:
        for ed in editions:
            if ed.index == idx:
                return ed
        raise SystemExit(
            f"No edition with index {idx}. "
            f"Available: {', '.join(f'{e.index}: {e.name}' for e in editions)}"
        )

    spec_lower = spec.lower()
    for ed in editions:
        if spec_lower == ed.name.lower():
            return ed
    for ed in editions:
        if spec_lower in ed.name.lower():
            return ed
    raise SystemExit(
        f"No edition matching '{spec}'. "
        f"Available: {', '.join(f'{e.index}: {e.name}' for e in editions)}"
    )


def _validate_username(name: str):
    """Windows rejects these in account names, and Setup fails late if they slip through."""
    if len(name) > 20 or not name.strip() or name.endswith(".") or set(name) & set('/\\[]:;|=,+*?<>"@'):
        raise SystemExit(f"Invalid Windows username: {name!r}. Use up to 20 characters without "
                         '/ \\ [ ] : ; | = , + * ? < > " @')
    if name.lower() in ("administrator", "guest", "defaultaccount", "system", "wdagutilityaccount"):
        raise SystemExit(f"{name!r} is reserved by Windows. Pick another username.")


def _get_apps_to_remove(opts: BuildOptions) -> list:
    if opts.remove_apps is not None:
        return list(opts.remove_apps)
    keep = {a.lower() for a in (opts.keep_apps or [])}
    return [name for name, default in BLOAT_APPS if default and name.lower() not in keep]


def _check_disk_space(work_dir: str, iso_path: str):
    iso_size = os.path.getsize(iso_path)
    needed = iso_size * 3
    stat = os.statvfs(work_dir)
    available = stat.f_bavail * stat.f_frsize
    if available < needed:
        raise SystemExit(
            f"Not enough disk space. Need ~{needed // (1024 ** 3)} GB, "
            f"have {available // (1024 ** 3)} GB free in {work_dir}"
        )


def _locale_defaults(locale: str):
    key = (locale or "en-US").replace("_", "-")
    for name, pair in _LOCALE_DEFAULTS.items():
        if name.lower() == key.lower():
            return pair
    return (key, "UTC")


def _arch(edition: Edition) -> str:
    return _ARCH_MAP.get((edition.arch or "").lower(), "amd64")


def _product_key(edition: Edition) -> str:
    return _PRODUCT_KEYS.get(edition.edition_id or "", "")


def _component_open(name: str, arch: str) -> str:
    return (
        f'    <component name="{name}" processorArchitecture="{arch}"'
        f' publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"'
        f' xmlns:wcm="{_WCM_NS}">'
    )


def _run_sync(order: int, path: str, description: str) -> list:
    return [
        '        <RunSynchronousCommand wcm:action="add">',
        f"          <Order>{order}</Order>",
        f"          <Path>{_xml_escape(path)}</Path>",
        f"          <Description>{_xml_escape(description)}</Description>",
        "          <WillReboot>Never</WillReboot>",
        "        </RunSynchronousCommand>",
    ]


def generate_autounattend(edition: Edition, opts: BuildOptions) -> str:
    """Answer file for the single exported edition. App removal lives in the setup script."""
    locale = opts.locale or edition.lang or "en-US"
    keyboard_default, timezone_default = _locale_defaults(locale)
    keyboard = opts.keyboard or keyboard_default
    timezone = opts.timezone or timezone_default
    username = opts.local_username or "User"
    arch = _arch(edition)
    disk_config = _WIPE_DISK0 if opts.wipe_disk else []
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<unattend xmlns="{_UNATTEND_NS}" xmlns:wcm="{_WCM_NS}">',
        '  <settings pass="windowsPE">',
        _component_open("Microsoft-Windows-International-Core-WinPE", arch),
        "      <SetupUILanguage>",
        f"        <UILanguage>{_xml_escape(locale)}</UILanguage>",
        "      </SetupUILanguage>",
        f"      <InputLocale>{_xml_escape(keyboard)}</InputLocale>",
        f"      <SystemLocale>{_xml_escape(locale)}</SystemLocale>",
        f"      <UILanguage>{_xml_escape(locale)}</UILanguage>",
        f"      <UserLocale>{_xml_escape(locale)}</UserLocale>",
        "    </component>",
        _component_open("Microsoft-Windows-Setup", arch),
        *disk_config,
        "      <ImageInstall>",
        "        <OSImage>",
        "          <InstallFrom>",
        '            <MetaData wcm:action="add">',
        "              <Key>/IMAGE/NAME</Key>",
        f"              <Value>{_xml_escape(edition.name)}</Value>",
        "            </MetaData>",
        "          </InstallFrom>",
        *_INSTALL_TO,
        "          <WillShowUI>OnError</WillShowUI>",
        "        </OSImage>",
        "      </ImageInstall>",
        "      <UserData>",
    ]
    if not opts.wipe_disk:
        lines = [line for line in lines if line not in _INSTALL_TO]
    key = _product_key(edition)
    if key:
        lines += [
            "        <ProductKey>",
            f"          <Key>{key}</Key>",
            "          <WillShowUI>OnError</WillShowUI>",
            "        </ProductKey>",
        ]
    lines += [
        "        <AcceptEula>true</AcceptEula>",
        "      </UserData>",
    ]
    if opts.bypass_requirements:
        lines.append("      <RunSynchronous>")
        for order, (value, desc) in enumerate((
            ("BypassTPMCheck", "Bypass TPM check"),
            ("BypassSecureBootCheck", "Bypass Secure Boot check"),
            ("BypassRAMCheck", "Bypass RAM check"),
            ("BypassCPUCheck", "Bypass CPU check"),
            ("BypassStorageCheck", "Bypass storage check"),
        ), start=1):
            cmd = (r'reg add "HKLM\SYSTEM\Setup\LabConfig" /v '
                   + value + r" /t REG_DWORD /d 1 /f")
            lines += _run_sync(order, cmd, desc)
        lines.append("      </RunSynchronous>")
    lines += [
        "      <DynamicUpdate>",
        "        <Enable>false</Enable>",
        "        <WillShowUI>Never</WillShowUI>",
        "      </DynamicUpdate>",
        "    </component>",
        "  </settings>",
        '  <settings pass="specialize">',
        _component_open("Microsoft-Windows-International-Core", arch),
        f"      <InputLocale>{_xml_escape(keyboard)}</InputLocale>",
        f"      <SystemLocale>{_xml_escape(locale)}</SystemLocale>",
        f"      <UILanguage>{_xml_escape(locale)}</UILanguage>",
        f"      <UserLocale>{_xml_escape(locale)}</UserLocale>",
        "    </component>",
        _component_open("Microsoft-Windows-Shell-Setup", arch),
        "      <ComputerName>*</ComputerName>",
        f"      <TimeZone>{_xml_escape(timezone)}</TimeZone>",
        "    </component>",
        _component_open("Microsoft-Windows-Deployment", arch),
        "      <RunSynchronous>",
    ]
    spec = 0
    # Offline OOBE: local account in oobeSystem, plus the network-requirement bypass
    # set before OOBE starts. No network is required to finish setup.
    for cmd, desc in (
        (r'reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE" '
         r"/v BypassNRO /t REG_DWORD /d 1 /f", "Allow offline local account"),
        (r'reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE" '
         r"/v DisablePrivacyExperience /t REG_DWORD /d 1 /f", "Skip privacy experience"),
    ):
        spec += 1
        lines += _run_sync(spec, cmd, desc)
    if opts.apply_tweaks:
        for key_path, vname, vtype, data in REGISTRY_TWEAKS:
            spec += 1
            cmd = f'reg add "{key_path}" /v {vname} /t {vtype} /d {data} /f'
            lines += _run_sync(spec, cmd, vname)
    if opts.disable_hibernation:
        spec += 1
        lines += _run_sync(spec, "powercfg /h off", "Disable hibernation")
    if opts.compact_os:
        spec += 1
        lines += _run_sync(spec, "compact /compactos:always", "Compact the OS")
    spec += 1
    lines += _run_sync(
        spec,
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
        r"C:\Windows\Setup\Scripts\tuxutil-setup.ps1 -Phase Specialize",
        "TuxUtil specialize setup",
    )
    lines += [
        "      </RunSynchronous>",
        "    </component>",
        "  </settings>",
        '  <settings pass="oobeSystem">',
        _component_open("Microsoft-Windows-Shell-Setup", arch),
        "      <OOBE>",
        "        <HideEULAPage>true</HideEULAPage>",
        "        <HideLocalAccountScreen>true</HideLocalAccountScreen>",
        "        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>",
        "        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>",
        "        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>",
        "        <NetworkLocation>Home</NetworkLocation>",
        "        <ProtectYourPC>3</ProtectYourPC>",
        "        <SkipMachineOOBE>true</SkipMachineOOBE>",
        "        <SkipUserOOBE>true</SkipUserOOBE>",
        "      </OOBE>",
        "      <UserAccounts>",
        "        <LocalAccounts>",
        '          <LocalAccount wcm:action="add">',
        f"            <Name>{_xml_escape(username)}</Name>",
        f"            <DisplayName>{_xml_escape(username)}</DisplayName>",
        "            <Group>Administrators</Group>",
    ]
    lines += [
        "            <Password>",
        f"              <Value>{_xml_escape(opts.local_password)}</Value>",
        "              <PlainText>true</PlainText>",
        "            </Password>",
        "          </LocalAccount>",
        "        </LocalAccounts>",
        "      </UserAccounts>",
        "      <AutoLogon>",
        "        <Enabled>true</Enabled>",
        f"        <Username>{_xml_escape(username)}</Username>",
        "        <LogonCount>1</LogonCount>",
    ]
    lines += [
        "        <Password>",
        f"          <Value>{_xml_escape(opts.local_password)}</Value>",
        "          <PlainText>true</PlainText>",
        "        </Password>",
        "      </AutoLogon>",
        "      <FirstLogonCommands>",
        '        <SynchronousCommand wcm:action="add">',
        "          <Order>1</Order>",
        "          <CommandLine>powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
        r"C:\Windows\Setup\Scripts\tuxutil-setup.ps1 -Phase FirstLogon</CommandLine>",
        "          <Description>TuxUtil post-install setup</Description>",
        "          <RequiresUserInput>false</RequiresUserInput>",
        "        </SynchronousCommand>",
        "      </FirstLogonCommands>",
        "    </component>",
        "  </settings>",
        "</unattend>",
    ]
    return "\n".join(lines) + "\n"


def generate_setup_script(apps_to_remove: list, remove_onedrive: bool, remove_edge: bool,
                          apply_tweaks: bool = True, install_drivers: bool = False) -> str:
    """First-logon / specialize script. Does not contain the account password."""
    lines = [
        "param([string]$Phase = 'FirstLogon')",
        "$ErrorActionPreference = 'Continue'",
        "$log = New-Object System.Collections.Generic.List[string]",
        "function Log([string]$m) { $script:log.Add($m) }",
        "Log 'TuxUtil Post-Install Report'",
        "Log '=========================='",
        "Log ('Date: ' + (Get-Date -Format o))",
        "Log ('PHASE_BEGIN ' + $Phase)",
        "Log ('USER ' + [Environment]::UserName)",
        "Log ('WHOAMI ' + (& whoami 2>&1 | Out-String).Trim())",
        "",
        "$appsToRemove = @(",
    ]
    for app in apps_to_remove:
        lines.append("    " + _ps_quote(app))
    lines += [
        ")",
        "",
        "Log 'App Removal Results:'",
        "foreach ($appPattern in $appsToRemove) {",
        "    $packages = @(Get-AppxProvisionedPackage -Online | Where-Object {",
        "        $_.PackageName -like ('*' + $appPattern + '*') -or $_.DisplayName -like ('*' + $appPattern + '*') })",
        "    if (-not $packages) { Log ('  ABSENT: ' + $appPattern); continue }",
        "    foreach ($pkg in $packages) {",
        "        try {",
        "            Remove-AppxProvisionedPackage -Online -PackageName $pkg.PackageName -ErrorAction Stop",
        "            Log ('  REMOVED: ' + $pkg.PackageName)",
        "        } catch {",
        "            Log ('  FAILED: ' + $pkg.PackageName + ' - ' + $_.Exception.Message)",
        "        }",
        "    }",
        "    Get-AppxPackage -AllUsers -Name ('*' + $appPattern + '*') | Remove-AppxPackage -AllUsers -ErrorAction SilentlyContinue",
        "}",
        "",
    ]
    if remove_onedrive:
        lines += [
            "Log 'OneDrive Removal:'",
            "$onedrive = \"$env:SystemRoot\\System32\\OneDriveSetup.exe\"",
            "$onedrive32 = \"$env:SystemRoot\\SysWOW64\\OneDriveSetup.exe\"",
            "if (Test-Path $onedrive) {",
            "    Start-Process $onedrive -ArgumentList '/uninstall' -Wait -ErrorAction SilentlyContinue",
            "    Log '  OneDrive uninstalled (64-bit)'",
            "} elseif (Test-Path $onedrive32) {",
            "    Start-Process $onedrive32 -ArgumentList '/uninstall' -Wait -ErrorAction SilentlyContinue",
            "    Log '  OneDrive uninstalled (32-bit)'",
            "} else {",
            "    Log '  OneDrive setup not found (already removed from image)'",
            "}",
            "Log ('ONEDRIVE_SETUP ' + (Test-Path $onedrive))",
            "",
        ]
    if remove_edge:
        lines += [
            "Log 'Edge Removal:'",
            "$edgeDir = \"${env:ProgramFiles(x86)}\\Microsoft\\Edge\\Application\"",
            "if (Test-Path $edgeDir) {",
            "    $edgeVer = Get-ChildItem $edgeDir -Directory | Where-Object { $_.Name -match '^\\d' } | Select-Object -First 1",
            "    if ($edgeVer) {",
            "        $installer = Join-Path $edgeVer.FullName 'Installer\\setup.exe'",
            "        if (Test-Path $installer) {",
            "            Start-Process $installer -ArgumentList '--uninstall','--system-level','--force-uninstall' -Wait -ErrorAction SilentlyContinue",
            "            Log '  Edge uninstall attempted'",
            "        }",
            "    }",
            "} else {",
            "    Log '  Edge not found'",
            "}",
            "",
        ]
    if apply_tweaks:
        lines += [
            "if ($Phase -eq 'Specialize') {",
            "    $hive = Join-Path $env:SystemDrive 'Users\\Default\\NTUSER.DAT'",
            "    if (Test-Path $hive) {",
            "        & reg.exe load 'HKU\\TuxDefault' $hive | Out-Null",
        ]
        if remove_onedrive:
            lines.append("        & reg.exe delete 'HKU\\TuxDefault\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' /v OneDriveSetup /f 2>$null | Out-Null")
        for subkey, name, data in _USER_HIVE_TWEAKS:
            lines.append(
                "        & reg.exe add " + _ps_quote("HKU\\TuxDefault\\" + subkey)
                + " /v " + _ps_quote(name) + " /t REG_DWORD /d " + data + " /f | Out-Null"
            )
        lines += [
            "        & reg.exe unload 'HKU\\TuxDefault' | Out-Null",
            "        Log 'reg.exe load default user hive: done'",
            "    } else {",
            "        Log 'reg.exe load default user hive: NTUSER.DAT missing'",
            "    }",
            "}",
            "",
        ]
    if install_drivers:
        lines += [
            "if (Test-Path 'C:\\Drivers') {",
            "    Get-ChildItem -Path 'C:\\Drivers' -Recurse -Filter *.inf -ErrorAction SilentlyContinue | ForEach-Object {",
            "        Log ('PNPUTIL ' + $_.FullName)",
            "        $out = & pnputil.exe /add-driver $_.FullName /install 2>&1 | Out-String",
            "        Log $out.Trim()",
            "    }",
            "}",
            "",
        ]
    lines += [
        "Log ''",
        "Log 'Remaining Provisioned Apps:'",
        "$remaining = @(Get-AppxProvisionedPackage -Online | Select-Object -ExpandProperty DisplayName | Sort-Object)",
        "foreach ($app in $remaining) { Log ('  REMAINING: ' + $app) }",
        "Log ('REMAINING_COUNT ' + $remaining.Count)",
        "if (Test-Path 'C:\\Drivers') {",
        "    Get-ChildItem 'C:\\Drivers' -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { Log ('DRIVER_FILE ' + $_.FullName) }",
        "} else { Log 'DRIVER_FILE none' }",
        "Log ('PHASE_END ' + $Phase)",
        "$log -join \"`r`n\" | Out-File -FilePath (Join-Path $env:SystemRoot 'Setup\\Scripts\\tuxutil-log.txt') -Encoding UTF8 -Append",
        # The answer file holds the account password in plain text.
        "if ($Phase -eq 'FirstLogon') {",
        "    Remove-Item -Force -ErrorAction SilentlyContinue \"$env:SystemRoot\\Panther\\unattend*.xml\", \"$env:SystemRoot\\Panther\\Unattend\\*.xml\"",
        "}",
        "exit 0",
    ]
    return "\n".join(lines) + "\n"


def _wim_export_argv(src: str, index, dest: str, compression: str) -> list:
    if compression not in ("solid", "lzx", "none"):
        raise SystemExit(f"Unknown compression: {compression}")
    cmd = ["wimlib-imagex", "export", src, str(index), dest]
    if compression == "solid":
        cmd += ["--solid", "--compress=LZMS"]
    elif compression == "lzx":
        cmd += ["--compress=LZX"]
    else:
        cmd += ["--compress=none"]
    return cmd


def _wim_quote(path: str) -> str:
    if any(ch in path for ch in ' \t"'):
        return '"' + path.replace('"', '') + '"'
    return path


def _wim_update_lines(script_path: str, driver_dirs: Optional[list], remove_onedrive: bool) -> list:
    """Update commands. Never deletes provisioned-app files under WindowsApps."""
    lines = []
    if remove_onedrive:
        for path in ONEDRIVE_SETUP:
            lines.append(f"delete --force {path}")
    lines.append(f"add {_wim_quote(script_path)} /Windows/Setup/Scripts/tuxutil-setup.ps1")
    for name, directory in _driver_names(driver_dirs or []):
        lines.append(f"add {_wim_quote(directory)} {_wim_quote('/Drivers/' + name)}")
    return lines


def _driver_names(driver_dirs: list):
    used = set()
    for directory in driver_dirs:
        base = os.path.basename(os.path.normpath(directory)) or "drivers"
        name = base
        n = 2
        while name.lower() in used:
            name = f"{base}-{n}"
            n += 1
        used.add(name.lower())
        yield name, os.path.abspath(directory)


def choose_boot_files(root: str):
    """Return (bios_path, uefi_path, uefi path relative to root, slash-separated)."""
    bios = os.path.join(root, "boot", "etfsboot.com")
    noprompt = os.path.join(root, "efi", "microsoft", "boot", "efisys_noprompt.bin")
    regular = os.path.join(root, "efi", "microsoft", "boot", "efisys.bin")
    if not os.path.isfile(bios):
        raise SystemExit(f"BIOS boot record not found: {bios}")
    # The prompting image on purpose. No-prompt reboots back into Setup when the
    # disc is still first in the boot order, which is the normal case in a VM.
    uefi = regular if os.path.isfile(regular) else noprompt
    if not os.path.isfile(uefi):
        raise SystemExit(f"UEFI boot record not found: tried {noprompt} and {regular}")
    rel = os.path.relpath(uefi, root).replace(os.sep, "/")
    return bios, uefi, rel


def _xorriso_argv(extract_dir: str, output_path: str, label: str, uefi_rel: str) -> list:
    # No -boot-load-size on the UEFI image: a short load size truncates efisys.bin.
    return [
        "xorriso", "-as", "mkisofs",
        "-iso-level", "3",
        # The BIOS loader looks for a file named exactly BOOTMGR, not BOOTMGR.;1
        "-omit-version-number", "-omit-period",
        "-joliet", "-joliet-long",
        "-rational-rock",
        "-volid", label,
        "-b", "boot/etfsboot.com",
        "-no-emul-boot",
        "-boot-load-size", "8",
        "-eltorito-alt-boot",
        "-e", uefi_rel,
        "-no-emul-boot",
        "-o", output_path,
        extract_dir,
    ]


def _iso_label(iso_path: str) -> str:
    try:
        with open(iso_path, "rb") as handle:
            handle.seek(32768)
            pvd = handle.read(2048)
    except OSError:
        return "TUXUTIL_WIN"
    if len(pvd) < 72 or pvd[1:6] != b"CD001":
        return "TUXUTIL_WIN"
    label = pvd[40:72].decode("ascii", "replace").strip().strip("\x00").strip()
    label = "".join(ch if 32 < ord(ch) < 127 else "_" for ch in label)[:32]
    return label or "TUXUTIL_WIN"


def _wim_env():
    env = os.environ.copy()
    # WIM paths are case-insensitive on Windows. Match that when deleting.
    env["WIMLIB_IMAGEX_IGNORE_CASE"] = "1"
    return env


def _find_install_image(extract_dir: str) -> str:
    sources = os.path.join(extract_dir, "sources")
    for name in ("install.wim", "install.esd"):
        path = os.path.join(sources, name)
        if os.path.isfile(path) and os.path.getsize(path) > 1024 * 1024:
            return path
    raise SystemExit("No install.wim or install.esd found in extracted ISO.")


def _clear_install_images(sources: str):
    if not os.path.isdir(sources):
        return
    for name in os.listdir(sources):
        lower = name.lower()
        if lower == "install.wim" or lower == "install.esd" or lower == "install.swm":
            os.remove(os.path.join(sources, name))
        elif re.fullmatch(r"install\d+\.swm", lower):
            os.remove(os.path.join(sources, name))


def _assert_under_limit(root: str):
    too_big = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > ISO_FILE_LIMIT:
                too_big.append(f"{path} ({size} bytes)")
    if too_big:
        raise SystemExit(
            "These files are over 4GB and Windows Setup cannot read them from this ISO: "
            + "; ".join(too_big)
        )


def _stage_winpe_drivers(extract_dir: str, driver_dirs: list):
    if not driver_dirs:
        return
    dest_root = os.path.join(extract_dir, "$WinPEDriver$")
    os.makedirs(dest_root, exist_ok=True)
    for name, directory in _driver_names(driver_dirs):
        shutil.copytree(directory, os.path.join(dest_root, name), dirs_exist_ok=True)


def _publish_or_split(src: str, sources: str, single_name: str):
    """Move src into sources under single_name, or split it into install*.swm."""
    size = os.path.getsize(src)
    if size <= ISO_FILE_LIMIT:
        os.replace(src, os.path.join(sources, single_name))
        return single_name, size
    # ponytail: Windows Setup does not install a split solid ESD. Caller must
    # pass a non-solid WIM when the solid image does not fit.
    swm = os.path.join(sources, "install.swm")
    _run(["wimlib-imagex", "split", src, swm, SPLIT_MIB])
    os.remove(src)
    parts = sorted(
        name for name in os.listdir(sources)
        if name.lower() == "install.swm" or re.fullmatch(r"install\d+\.swm", name.lower())
    )
    return " ".join(parts), size


def _get_editions(iso_path: str) -> list:
    """Extract only the install image onto scratch (never /tmp; that is RAM)."""
    cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "tuxutil")
    os.makedirs(cache, exist_ok=True)
    dest = tempfile.mkdtemp(prefix="winiso-info-", dir=cache)  # not /tmp: often RAM, and this is ~8 GB
    try:
        for name in ("sources/install.wim", "sources/install.esd"):
            result = system.run(
                ["7z", "e", iso_path, "-o" + dest, name, "-y"],
                capture=True, check=False,
            )
            path = os.path.join(dest, os.path.basename(name))
            if result.returncode == 0 and os.path.isfile(path) and os.path.getsize(path) > 1024 * 1024:
                info = _run(["wimlib-imagex", "info", path], capture=True)
                return _parse_wiminfo(info.stdout)
        raise SystemExit(
            f"No install.wim or install.esd found in {iso_path}. Is this a Windows installation ISO?"
        )
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def _own_work_dir(work_dir: Optional[str], output_path: str) -> str:
    """A new folder the build may delete, so cleanup can't remove anything already there."""
    parent = os.path.abspath(work_dir) if work_dir else os.path.dirname(output_path)
    os.makedirs(parent, exist_ok=True)
    return tempfile.mkdtemp(prefix="tuxutil-winiso-", dir=parent)


def build_iso(iso_path: str, output_path: str, opts: BuildOptions, work_dir: Optional[str] = None):
    iso_path = os.path.abspath(iso_path)
    output_path = os.path.abspath(output_path)
    if iso_path == output_path:
        raise SystemExit("Output path must be different from input ISO path.")
    _validate_username(opts.local_username or "User")
    if system.DRY_RUN:
        # ponytail: later steps depend on real files, so dry-run only states the plan.
        print(f"Would build {output_path} from {iso_path}: extract with 7z, export one edition "
              "with wimlib-imagex, add autounattend.xml, write the ISO with xorriso.")
        return
    system.require_tools(REQUIRED_TOOLS)
    _validate_iso(iso_path)
    for directory in opts.driver_dirs or []:
        if not os.path.isdir(directory):
            raise SystemExit(f"Driver directory not found: {directory}")
    if opts.compression not in ("solid", "lzx", "none"):
        raise SystemExit(f"Unknown compression: {opts.compression}")

    parent = os.path.dirname(output_path)
    os.makedirs(parent, exist_ok=True)
    work_dir = _own_work_dir(work_dir, output_path)
    _check_disk_space(work_dir, iso_path)
    extract_dir = os.path.join(work_dir, "extract")

    try:
        _do_build(iso_path, output_path, opts, work_dir, extract_dir)
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def _do_build(iso_path, output_path, opts, work_dir, extract_dir):
    env = _wim_env()
    system.progress(0, "Extracting ISO contents...")
    _run(["7z", "x", iso_path, f"-o{extract_dir}", "-y"])

    system.progress(8, "Reading edition information...")
    wim_path = _find_install_image(extract_dir)
    info = _run(["wimlib-imagex", "info", wim_path], capture=True)
    editions = _parse_wiminfo(info.stdout)
    edition = _resolve_edition(editions, opts.edition)
    print(f"Selected edition: {edition.name} (index {edition.index}, build {edition.build})", flush=True)

    apps = _get_apps_to_remove(opts)
    system.progress(12, f"Exporting {edition.name}...")
    new_wim = os.path.join(work_dir, "install_new.wim")
    if os.path.exists(new_wim):
        os.remove(new_wim)
    _run(_wim_export_argv(wim_path, edition.index, new_wim, "lzx"))

    system.progress(35, "Updating image...")
    scripts_dir = os.path.join(work_dir, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    script_path = os.path.join(scripts_dir, "tuxutil-setup.ps1")
    script = generate_setup_script(
        apps, opts.remove_onedrive, opts.remove_edge,
        apply_tweaks=opts.apply_tweaks, install_drivers=bool(opts.driver_dirs),
    )
    with open(script_path, "w", encoding="utf-8-sig", newline="\r\n") as handle:
        handle.write(script)
    update_lines = _wim_update_lines(script_path, opts.driver_dirs, opts.remove_onedrive)
    _run(["wimlib-imagex", "update", new_wim], input="\n".join(update_lines) + "\n", text=True, env=env)
    check_dir = os.path.join(work_dir, "script-check")
    if os.path.exists(check_dir):
        shutil.rmtree(check_dir)
    os.makedirs(check_dir)
    _run([
        "wimlib-imagex", "extract", new_wim, "1",
        "/Windows/Setup/Scripts/tuxutil-setup.ps1", f"--dest-dir={check_dir}",
    ], env=env)

    sources = os.path.join(extract_dir, "sources")
    _clear_install_images(sources)
    system.progress(40, "Compressing image (this may take a while)...")
    if opts.compression == "solid":
        esd = os.path.join(work_dir, "install_solid.esd")
        if os.path.exists(esd):
            os.remove(esd)
        _run(_wim_export_argv(new_wim, 1, esd, "solid"))
        if os.path.getsize(esd) <= ISO_FILE_LIMIT:
            os.remove(new_wim)
            name, size = _publish_or_split(esd, sources, "install.esd")
        else:
            # Solid image does not fit. Split the non-solid WIM instead.
            os.remove(esd)
            print("Solid image is over 4GB; splitting a non-solid WIM for Setup.", flush=True)
            name, size = _publish_or_split(new_wim, sources, "install.wim")
    elif opts.compression == "lzx":
        name, size = _publish_or_split(new_wim, sources, "install.wim")
    else:
        plain = os.path.join(work_dir, "install_none.wim")
        if os.path.exists(plain):
            os.remove(plain)
        _run(_wim_export_argv(new_wim, 1, plain, "none"))
        os.remove(new_wim)
        name, size = _publish_or_split(plain, sources, "install.wim")
    print(f"Install image: {name} (source image bytes before split/move: {size})", flush=True)

    system.progress(68, "Writing autounattend.xml...")
    xml_path = os.path.join(extract_dir, "autounattend.xml")
    with open(xml_path, "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(generate_autounattend(edition, opts))
    if edition.edition_id:
        ei = os.path.join(sources, "ei.cfg")
        with open(ei, "w", encoding="ascii", newline="") as handle:
            handle.write(f"[EditionID]\r\n{edition.edition_id}\r\n[Channel]\r\nRetail\r\n[VL]\r\n0\r\n")

    if opts.driver_dirs:
        system.progress(70, "Copying drivers to $WinPEDriver$...")
        _stage_winpe_drivers(extract_dir, opts.driver_dirs)

    _assert_under_limit(extract_dir)
    bios, uefi, uefi_rel = choose_boot_files(extract_dir)
    label = _iso_label(iso_path)
    print(f"Boot records: BIOS {bios}, UEFI {uefi_rel}, label {label}", flush=True)

    system.progress(75, "Creating ISO...")
    with system.atomic_output(output_path) as partial:
        _run(_xorriso_argv(extract_dir, partial, label, uefi_rel))

    system.progress(95, "Cleaning up...")
    shutil.rmtree(work_dir, ignore_errors=True)
    output_size = os.path.getsize(output_path)
    system.progress(100, f"Done. {output_path} ({output_size / (1024 ** 3):.2f} GB)")
    print(
        f"Build complete: {output_path}\n"
        f"  Size: {output_size / (1024 ** 3):.2f} GB "
        f"(original: {os.path.getsize(iso_path) / (1024 ** 3):.2f} GB)",
        flush=True,
    )


def _flat(values):
    """Flatten repeated flags, each of which may itself be comma-separated."""
    parts = [part for value in values or [] for part in str(value).split(",") if part]
    return parts or None


def options_from_args(args) -> BuildOptions:
    # Always cleared, so child tools never inherit it.
    env_password = os.environ.pop("TUXUTIL_PASSWORD", "")
    return BuildOptions(
        edition=args.edition or "",
        remove_apps=_flat(args.remove_apps),
        keep_apps=_flat(args.keep_apps),
        remove_onedrive=not args.no_remove_onedrive,
        remove_edge=bool(args.remove_edge),
        apply_tweaks=not args.no_tweaks,
        bypass_requirements=not args.no_bypass,
        local_username=args.username or "",
        local_password=args.password or env_password,
        locale=args.locale or "",
        keyboard=args.keyboard or "",
        timezone=args.timezone or "",
        driver_dirs=_flat(args.drivers),
        disable_hibernation=not args.no_disable_hibernation,
        compact_os=bool(args.compact_os),
        compression=args.compression,
        wipe_disk=bool(args.wipe_disk),
    )


def _option_table():
    return [
        {"flag": "--edition", "type": "str", "label": "Windows Edition",
         "help": "Edition to keep (name or index). Default: Pro if present, else first.",
         "default": "", "choices": []},
        {"flag": "--remove-apps", "type": "list", "label": "Apps to Remove",
         "help": "App package names to remove. Replaces the default bloat list when set.",
         "default": [name for name, default in BLOAT_APPS if default]},
        {"flag": "--keep-apps", "type": "list", "label": "Apps to Keep",
         "help": "Apps to keep even if in the default removal list.",
         "default": []},
        {"flag": "--no-remove-onedrive", "type": "bool", "label": "Keep OneDrive",
         "help": "Don't remove OneDrive.", "default": False},
        {"flag": "--remove-edge", "type": "bool", "label": "Remove Edge",
         "help": "Remove Microsoft Edge (optional, off by default).", "default": False},
        {"flag": "--no-tweaks", "type": "bool", "label": "Skip Tweaks",
         "help": "Don't apply telemetry/Copilot/Recall/ads tweaks.", "default": False},
        {"flag": "--no-bypass", "type": "bool", "label": "Skip Requirement Bypass",
         "help": "Don't bypass TPM/Secure Boot/RAM/CPU checks.", "default": False},
        {"flag": "--username", "type": "str", "label": "Local Username",
         "help": "Local account username (default: User).", "default": "User"},
        {"flag": "--password", "type": "str", "label": "Local Password",
         "help": "Optional. Stored in plain text in the ISO, so keep the ISO private.",
         "default": ""},
        {"flag": "--locale", "type": "str", "label": "Locale",
         "help": "System locale (e.g. en-US). Default: from the source image.", "default": ""},
        {"flag": "--keyboard", "type": "str", "label": "Keyboard Layout",
         "help": "Keyboard id (e.g. 0809:00000809). Default: from the source locale.", "default": ""},
        {"flag": "--timezone", "type": "str", "label": "Timezone",
         "help": "Windows timezone name. Default: inferred from the source language.", "default": ""},
        {"flag": "--drivers", "type": "list", "label": "Driver Directories",
         "help": "Directories of .inf drivers.", "default": []},
        {"flag": "--no-disable-hibernation", "type": "bool", "label": "Keep Hibernation",
         "help": "Don't disable hibernation.", "default": False},
        {"flag": "--compact-os", "type": "bool", "label": "Compact OS",
         "help": "Enable Compact OS during install.", "default": False},
        {"flag": "--wipe-disk", "type": "bool", "label": "Fully Unattended: ERASE Disk 0",
         "help": "DANGER: erases disk 0 with no prompt on any PC that boots this media. UEFI only. "
                 "Off means Setup asks which disk to use.", "default": False},
        {"flag": "--compression", "type": "choice", "label": "Image Compression",
         "help": "solid (ESD/LZMS), lzx, or none. Files over 4GB are split into install*.swm.",
         "default": "solid", "choices": ["solid", "lzx", "none"]},
    ]


def cmd_info(args):
    system.require_tools(["7z", "wimlib-imagex"])
    _validate_iso(args.iso)
    editions = _get_editions(args.iso)
    data = {
        "editions": [
            {"index": e.index, "name": e.name, "arch": e.arch,
             "size_bytes": e.size_bytes, "build": e.build}
            for e in editions
        ]
    }
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"Found {len(editions)} edition(s):")
        for e in editions:
            print(f"  [{e.index}] {e.name} ({e.arch}, {e.size_bytes / (1024 ** 3):.1f} GB, build {e.build})")
    return 0


def cmd_options(args):
    options = _option_table()
    if args.json:
        print(json.dumps(options, indent=2))
    else:
        for opt in options:
            print(f"  {opt['flag']:30s} {opt['help']}")
    return 0


def cmd_build(args):
    build_iso(args.iso, args.output, options_from_args(args), work_dir=args.work_dir)
    return 0


def register(sub):
    parser = sub.add_parser("winiso", help="build a custom Windows 11 ISO")
    commands = parser.add_subparsers(dest="winiso_command")

    info = commands.add_parser("info", help="list editions in a Windows ISO")
    info.add_argument("iso", help="path to Windows ISO")
    info.add_argument("--json", action="store_true", help="output as JSON")
    info.set_defaults(func=cmd_info)

    options = commands.add_parser("options", help="describe build options")
    options.add_argument("--json", action="store_true", help="output as JSON")
    options.set_defaults(func=cmd_options)

    build = commands.add_parser("build", help="build a custom Windows ISO")
    build.add_argument("iso", help="path to source Windows ISO")
    build.add_argument("-o", "--output", required=True, help="output ISO path")
    build.add_argument("--edition", default="", help="edition name or index")
    build.add_argument("--remove-apps", action="append", help="app name to remove; repeatable")
    build.add_argument("--keep-apps", action="append", help="app name to keep; repeatable")
    build.add_argument("--no-remove-onedrive", action="store_true", help="keep OneDrive")
    build.add_argument("--remove-edge", action="store_true", help="remove Microsoft Edge")
    build.add_argument("--no-tweaks", action="store_true", help="skip registry tweaks")
    build.add_argument("--no-bypass", action="store_true", help="don't bypass TPM/Secure Boot/RAM/CPU checks")
    build.add_argument("--username", default="User", help="local account username")
    build.add_argument("--password", default="", help="local account password. Other processes can read this flag, so prefer the TUXUTIL_PASSWORD environment variable")
    build.add_argument("--locale", default="", help="system locale (e.g. en-US)")
    build.add_argument("--keyboard", default="", help="keyboard layout")
    build.add_argument("--timezone", default="", help="Windows timezone name")
    build.add_argument("--drivers", action="append", help="driver directory; repeatable")
    build.add_argument("--no-disable-hibernation", action="store_true", help="keep hibernation enabled")
    build.add_argument("--compact-os", action="store_true", help="enable OS compaction")
    build.add_argument("--wipe-disk", action="store_true",
                       help="DANGER: erase disk 0 with no prompt on any PC that boots this media")
    build.add_argument("--compression", default="solid", choices=["solid", "lzx", "none"],
                       help="image compression method")
    build.add_argument("--work-dir", default=None, help="working directory for temp files")
    build.set_defaults(func=cmd_build)

    parser.set_defaults(func=lambda args: parser.print_help() or 1)
