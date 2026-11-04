"""Build a custom Windows 11 ISO (the MicroWin equivalent), rootless.

Uses 7z to extract ISO contents (UDF support), wimlib-imagex to export/modify
the WIM image, and xorriso to create the final ISO with El Torito boot records.
No root, no FUSE, no loop mounts, no DISM, no hivex.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import escape as html_escape
from pathlib import Path
from typing import List, Optional

from . import system

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUIRED_TOOLS = ["7z", "wimlib-imagex", "xorriso"]

# Provisioned apps that are safe to remove.  Each entry: (package-name-pattern, default-remove).
# These match the AppxProvisionedPackage names in Windows 11.
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
]

# WIM paths that can be deleted to reduce image size.
# These are safe because they're cached installers or telemetry components.
BLOAT_WIM_PATHS = [
    # OneDrive setup (always safe, reinstallable)
    r"\Windows\System32\OneDriveSetup.exe",
    # Cortana/Search (the app package handles it)
    r"\Windows\SystemApps\Microsoft.Windows.Search_cw5n1h2txyewy",
    # Edge installer cache (not Edge itself — the browser remains functional)
    r"\Program Files (x86)\Microsoft\EdgeUpdate",
    # Copilot/Recall components (optional, won't break core OS)
    r"\Windows\SystemApps\MicrosoftWindows.Client.AIX_cw5n1h2txyewy",
    r"\Windows\SystemApps\MicrosoftWindows.Client.Photon_cw5n1h2txyewy",
]

# Registry tweaks applied via firstlogon script.  (key, value_name, type, data)
REGISTRY_TWEAKS = [
    # Disable telemetry
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection",
     "AllowTelemetry", "REG_DWORD", "0"),
    # Disable Copilot
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot",
     "TurnOffWindowsCopilot", "REG_DWORD", "1"),
    # Disable Recall
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsAI",
     "DisableAIDataAnalysis", "REG_DWORD", "1"),
    # Disable ads in Start and Explorer
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\CloudContent",
     "DisableWindowsConsumerFeatures", "REG_DWORD", "1"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\CloudContent",
     "DisableSoftLanding", "REG_DWORD", "1"),
    # Disable tips and suggestions
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\CloudContent",
     "DisableTailoredExperiencesWithDiagnosticData", "REG_DWORD", "1"),
    # Disable Activity History
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System",
     "EnableActivityFeed", "REG_DWORD", "0"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\System",
     "PublishUserActivities", "REG_DWORD", "0"),
    # Disable location tracking
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\LocationAndSensors",
     "DisableLocation", "REG_DWORD", "1"),
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

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
    """All build options with their defaults."""
    edition: str = ""  # name or index; default: "Pro" if present, else first
    remove_apps: Optional[List[str]] = None  # list of app patterns to remove; None = defaults
    keep_apps: Optional[List[str]] = None  # apps to keep even if default-remove
    remove_onedrive: bool = True
    remove_edge: bool = False  # risky, default off
    apply_tweaks: bool = True  # telemetry, copilot, recall, ads
    bypass_requirements: bool = True  # TPM, SecureBoot, RAM, CPU
    local_username: str = ""
    local_password: str = ""
    locale: str = ""  # e.g. en-US; defaults from source image
    keyboard: str = ""  # e.g. 0409:00000409
    timezone: str = ""  # e.g. "Pacific Standard Time"
    driver_dirs: Optional[List[str]] = None  # directories with .inf drivers
    disable_hibernation: bool = True
    compact_os: bool = False  # compact:on during install
    compression: str = "solid"  # "solid" (ESD/LZMS), "lzx", "none"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _xml_escape(s: str) -> str:
    """Escape a string for use in XML text/attribute values."""
    return html_escape(s, quote=True)


def _parse_wiminfo(text: str) -> list:
    """Parse wimlib-imagex info output into a list of Edition objects."""
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
        idx = int(e.get("Index", 0))
        if idx == 0:
            continue
        name = e.get("Name", e.get("Display Name", f"Image {idx}"))
        arch = e.get("Architecture", "unknown")
        total = int(e.get("Total Bytes", 0))
        build_num = e.get("Build", "0")
        sp_build = e.get("Service Pack Build", "0")
        build_str = f"{build_num}.{sp_build}" if sp_build != "0" else build_num
        edition_id = e.get("Edition ID", "")
        lang = e.get("Default Language", "")
        result.append(Edition(idx, name, arch, total, build_str, edition_id, lang))
    return result


def _get_editions(iso_path: str) -> list:
    """Extract install.wim from ISO to a temp location, get editions, clean up."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Extract just install.wim (or install.esd) to get edition info
        for wim_name in ("sources/install.wim", "sources/install.esd"):
            r = subprocess.run(
                ["7z", "e", iso_path, "-o" + tmpdir, wim_name, "-y"],
                capture_output=True, text=True
            )
            basename = os.path.basename(wim_name)
            wim_path = os.path.join(tmpdir, basename)
            if r.returncode == 0 and os.path.exists(wim_path):
                break
        else:
            raise ValueError(f"No install.wim or install.esd found in {iso_path}. "
                             "Is this a Windows installation ISO?")

        r = subprocess.run(
            ["wimlib-imagex", "info", wim_path],
            capture_output=True, text=True, check=True
        )
        return _parse_wiminfo(r.stdout)


def _validate_iso(iso_path: str):
    """Validate that the given path is a Windows ISO."""
    if not os.path.isfile(iso_path):
        raise SystemExit(f"ISO not found: {iso_path}")

    # Check it's a valid ISO by looking for key Windows files
    r = subprocess.run(
        ["7z", "l", iso_path],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        raise SystemExit(f"Cannot read ISO: {iso_path}")

    output = r.stdout.lower()
    if "sources/install.wim" not in output and "sources/install.esd" not in output:
        raise SystemExit(
            f"Not a Windows installation ISO (no sources/install.wim or install.esd): {iso_path}"
        )


def _resolve_edition(editions: list, spec: str) -> Edition:
    """Resolve an edition spec (name, index, or empty for default) to an Edition."""
    if not spec:
        # Default: Pro if present, else first
        for ed in editions:
            if "pro" in ed.name.lower() and "education" not in ed.name.lower() \
                    and " n" not in ed.name.lower() and "workstation" not in ed.name.lower():
                return ed
        return editions[0]

    # Try as index
    try:
        idx = int(spec)
        for ed in editions:
            if ed.index == idx:
                return ed
        raise SystemExit(f"No edition with index {idx}. "
                         f"Available: {', '.join(f'{e.index}: {e.name}' for e in editions)}")
    except ValueError:
        pass

    # Try as name (case-insensitive partial match)
    spec_lower = spec.lower()
    for ed in editions:
        if spec_lower == ed.name.lower():
            return ed
    for ed in editions:
        if spec_lower in ed.name.lower():
            return ed
    raise SystemExit(f"No edition matching '{spec}'. "
                     f"Available: {', '.join(f'{e.index}: {e.name}' for e in editions)}")


def _get_apps_to_remove(opts: BuildOptions) -> list:
    """Return list of app name patterns to remove."""
    if opts.remove_apps is not None:
        return opts.remove_apps

    keep = set(a.lower() for a in (opts.keep_apps or []))
    result = []
    for name, default_remove in BLOAT_APPS:
        if default_remove and name.lower() not in keep:
            result.append(name)
    return result


def _check_disk_space(work_dir: str, iso_path: str):
    """Rough check: we need ~2x the ISO size for working space."""
    iso_size = os.path.getsize(iso_path)
    needed = iso_size * 2  # conservative estimate
    stat = os.statvfs(work_dir)
    available = stat.f_bavail * stat.f_frsize
    if available < needed:
        raise SystemExit(
            f"Not enough disk space. Need ~{needed // (1024**3)} GB, "
            f"have {available // (1024**3)} GB free in {work_dir}"
        )


# ---------------------------------------------------------------------------
# Autounattend.xml generation
# ---------------------------------------------------------------------------

_WCM_NS = "http://schemas.microsoft.com/WMIConfig/2002/State"
_UNATTEND_NS = "urn:schemas-microsoft-com:unattend"


def generate_autounattend(edition: Edition, opts: BuildOptions,
                          apps_to_remove: list) -> str:
    """Generate autounattend.xml for unattended Windows 11 install.

    Uses:
    - windowsPE pass: LabConfig bypass keys for TPM/SecureBoot/RAM/CPU
    - specialize pass: registry tweaks, driver install
    - oobeSystem pass: local account, OOBE skip, app removal, report generation
    """
    locale = opts.locale or edition.lang or "en-US"
    keyboard = opts.keyboard or locale
    timezone = opts.timezone or "UTC"
    username = opts.local_username or "User"

    lines = []
    lines.append('<?xml version="1.0" encoding="utf-8"?>')
    lines.append(f'<unattend xmlns="{_UNATTEND_NS}">')

    # --- windowsPE pass: bypass requirements ---
    if opts.bypass_requirements:
        lines.append(f'  <settings pass="windowsPE">')
        lines.append(f'    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64"'
                     f' publicKeyToken="31bf3856ad364e35" language="neutral"'
                     f' versionScope="nonSxS"'
                     f' xmlns:wcm="{_WCM_NS}">')
        lines.append(f'      <RunSynchronous>')
        bypass_cmds = [
            ('reg add "HKLM\\SYSTEM\\Setup\\LabConfig" /v BypassTPMCheck /t REG_DWORD /d 1 /f', 1),
            ('reg add "HKLM\\SYSTEM\\Setup\\LabConfig" /v BypassSecureBootCheck /t REG_DWORD /d 1 /f', 2),
            ('reg add "HKLM\\SYSTEM\\Setup\\LabConfig" /v BypassRAMCheck /t REG_DWORD /d 1 /f', 3),
            ('reg add "HKLM\\SYSTEM\\Setup\\LabConfig" /v BypassCPUCheck /t REG_DWORD /d 1 /f', 4),
            ('reg add "HKLM\\SYSTEM\\Setup\\LabConfig" /v BypassStorageCheck /t REG_DWORD /d 1 /f', 5),
        ]
        for cmd, order in bypass_cmds:
            lines.append(f'        <RunSynchronousCommand wcm:action="add">')
            lines.append(f'          <Order>{order}</Order>')
            lines.append(f'          <Path>{_xml_escape(cmd)}</Path>')
            lines.append(f'        </RunSynchronousCommand>')
        lines.append(f'      </RunSynchronous>')

        # UserData for auto-accepting license and selecting edition
        lines.append(f'      <UserData>')
        lines.append(f'        <AcceptEula>true</AcceptEula>')
        lines.append(f'      </UserData>')

        # ImageInstall: install to disk 0 partition 1 (or auto)
        lines.append(f'      <ImageInstall>')
        lines.append(f'        <OSImage>')
        lines.append(f'          <InstallTo>')
        lines.append(f'            <DiskID>0</DiskID>')
        lines.append(f'            <PartitionID>3</PartitionID>')
        lines.append(f'          </InstallTo>')
        lines.append(f'        </OSImage>')
        lines.append(f'      </ImageInstall>')

        # DiskConfiguration for UEFI (GPT with ESP, MSR, Windows partition)
        lines.append(f'      <DiskConfiguration>')
        lines.append(f'        <Disk wcm:action="add">')
        lines.append(f'          <DiskID>0</DiskID>')
        lines.append(f'          <WillWipeDisk>true</WillWipeDisk>')
        lines.append(f'          <CreatePartitions>')
        # EFI System Partition
        lines.append(f'            <CreatePartition wcm:action="add">')
        lines.append(f'              <Order>1</Order>')
        lines.append(f'              <Type>EFI</Type>')
        lines.append(f'              <Size>260</Size>')
        lines.append(f'            </CreatePartition>')
        # MSR
        lines.append(f'            <CreatePartition wcm:action="add">')
        lines.append(f'              <Order>2</Order>')
        lines.append(f'              <Type>MSR</Type>')
        lines.append(f'              <Size>128</Size>')
        lines.append(f'            </CreatePartition>')
        # Windows
        lines.append(f'            <CreatePartition wcm:action="add">')
        lines.append(f'              <Order>3</Order>')
        lines.append(f'              <Type>Primary</Type>')
        lines.append(f'              <Extend>true</Extend>')
        lines.append(f'            </CreatePartition>')
        lines.append(f'          </CreatePartitions>')
        lines.append(f'          <ModifyPartitions>')
        lines.append(f'            <ModifyPartition wcm:action="add">')
        lines.append(f'              <Order>1</Order>')
        lines.append(f'              <PartitionID>1</PartitionID>')
        lines.append(f'              <Format>FAT32</Format>')
        lines.append(f'              <Label>System</Label>')
        lines.append(f'            </ModifyPartition>')
        lines.append(f'            <ModifyPartition wcm:action="add">')
        lines.append(f'              <Order>2</Order>')
        lines.append(f'              <PartitionID>2</PartitionID>')
        lines.append(f'            </ModifyPartition>')
        lines.append(f'            <ModifyPartition wcm:action="add">')
        lines.append(f'              <Order>3</Order>')
        lines.append(f'              <PartitionID>3</PartitionID>')
        lines.append(f'              <Format>NTFS</Format>')
        lines.append(f'              <Label>Windows</Label>')
        lines.append(f'            </ModifyPartition>')
        lines.append(f'          </ModifyPartitions>')
        lines.append(f'        </Disk>')
        lines.append(f'      </DiskConfiguration>')

        lines.append(f'    </component>')

        # International settings for PE
        lines.append(f'    <component name="Microsoft-Windows-International-Core-WinPE"'
                     f' processorArchitecture="amd64"'
                     f' publicKeyToken="31bf3856ad364e35" language="neutral"'
                     f' versionScope="nonSxS"'
                     f' xmlns:wcm="{_WCM_NS}">')
        lines.append(f'      <SetupUILanguage>')
        lines.append(f'        <UILanguage>{_xml_escape(locale)}</UILanguage>')
        lines.append(f'      </SetupUILanguage>')
        lines.append(f'      <InputLocale>{_xml_escape(keyboard)}</InputLocale>')
        lines.append(f'      <SystemLocale>{_xml_escape(locale)}</SystemLocale>')
        lines.append(f'      <UILanguage>{_xml_escape(locale)}</UILanguage>')
        lines.append(f'      <UserLocale>{_xml_escape(locale)}</UserLocale>')
        lines.append(f'    </component>')

        lines.append(f'  </settings>')

    # --- specialize pass: registry tweaks, locale, hostname, drivers ---
    lines.append(f'  <settings pass="specialize">')

    # International settings
    lines.append(f'    <component name="Microsoft-Windows-International-Core"'
                 f' processorArchitecture="amd64"'
                 f' publicKeyToken="31bf3856ad364e35" language="neutral"'
                 f' versionScope="nonSxS"'
                 f' xmlns:wcm="{_WCM_NS}">')
    lines.append(f'      <InputLocale>{_xml_escape(keyboard)}</InputLocale>')
    lines.append(f'      <SystemLocale>{_xml_escape(locale)}</SystemLocale>')
    lines.append(f'      <UILanguage>{_xml_escape(locale)}</UILanguage>')
    lines.append(f'      <UserLocale>{_xml_escape(locale)}</UserLocale>')
    lines.append(f'    </component>')

    # Shell setup — computer name
    lines.append(f'    <component name="Microsoft-Windows-Shell-Setup"'
                 f' processorArchitecture="amd64"'
                 f' publicKeyToken="31bf3856ad364e35" language="neutral"'
                 f' versionScope="nonSxS"'
                 f' xmlns:wcm="{_WCM_NS}">')
    lines.append(f'      <ComputerName>*</ComputerName>')
    lines.append(f'      <TimeZone>{_xml_escape(timezone)}</TimeZone>')
    lines.append(f'    </component>')

    # Deployment — skip EULA
    lines.append(f'    <component name="Microsoft-Windows-Deployment"'
                 f' processorArchitecture="amd64"'
                 f' publicKeyToken="31bf3856ad364e35" language="neutral"'
                 f' versionScope="nonSxS"'
                 f' xmlns:wcm="{_WCM_NS}">')
    lines.append(f'      <RunSynchronous>')
    spec_order = 0

    # Registry tweaks
    if opts.apply_tweaks:
        for key, vname, vtype, data in REGISTRY_TWEAKS:
            spec_order += 1
            cmd = f'reg add "{key}" /v {vname} /t {vtype} /d {data} /f'
            lines.append(f'        <RunSynchronousCommand wcm:action="add">')
            lines.append(f'          <Order>{spec_order}</Order>')
            lines.append(f'          <Path>{_xml_escape(cmd)}</Path>')
            lines.append(f'        </RunSynchronousCommand>')

    # Disable hibernation
    if opts.disable_hibernation:
        spec_order += 1
        lines.append(f'        <RunSynchronousCommand wcm:action="add">')
        lines.append(f'          <Order>{spec_order}</Order>')
        lines.append(f'          <Path>powercfg /h off</Path>')
        lines.append(f'        </RunSynchronousCommand>')

    # Driver injection via pnputil
    if opts.driver_dirs:
        spec_order += 1
        lines.append(f'        <RunSynchronousCommand wcm:action="add">')
        lines.append(f'          <Order>{spec_order}</Order>')
        lines.append(f'          <Path>cmd /c "for %d in (C:\\Drivers\\*.inf) do pnputil /add-driver %d /install"</Path>')
        lines.append(f'        </RunSynchronousCommand>')

    # Compact OS
    if opts.compact_os:
        spec_order += 1
        lines.append(f'        <RunSynchronousCommand wcm:action="add">')
        lines.append(f'          <Order>{spec_order}</Order>')
        lines.append(f'          <Path>compact /compactos:always</Path>')
        lines.append(f'        </RunSynchronousCommand>')

    lines.append(f'      </RunSynchronous>')
    lines.append(f'    </component>')

    lines.append(f'  </settings>')

    # --- oobeSystem pass: local account, OOBE skip, app removal ---
    lines.append(f'  <settings pass="oobeSystem">')
    lines.append(f'    <component name="Microsoft-Windows-Shell-Setup"'
                 f' processorArchitecture="amd64"'
                 f' publicKeyToken="31bf3856ad364e35" language="neutral"'
                 f' versionScope="nonSxS"'
                 f' xmlns:wcm="{_WCM_NS}">')

    # OOBE settings
    lines.append(f'      <OOBE>')
    lines.append(f'        <HideEULAPage>true</HideEULAPage>')
    lines.append(f'        <HideLocalAccountScreen>true</HideLocalAccountScreen>')
    lines.append(f'        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>')
    lines.append(f'        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>')
    lines.append(f'        <NetworkLocation>Home</NetworkLocation>')
    lines.append(f'        <ProtectYourPC>1</ProtectYourPC>')
    lines.append(f'        <SkipMachineOOBE>true</SkipMachineOOBE>')
    lines.append(f'        <SkipUserOOBE>true</SkipUserOOBE>')
    lines.append(f'      </OOBE>')

    # User accounts — local account
    lines.append(f'      <UserAccounts>')
    lines.append(f'        <LocalAccounts>')
    lines.append(f'          <LocalAccount wcm:action="add">')
    lines.append(f'            <Name>{_xml_escape(username)}</Name>')
    lines.append(f'            <DisplayName>{_xml_escape(username)}</DisplayName>')
    lines.append(f'            <Group>Administrators</Group>')
    if opts.local_password:
        lines.append(f'            <Password>')
        lines.append(f'              <Value>{_xml_escape(opts.local_password)}</Value>')
        lines.append(f'              <PlainText>true</PlainText>')
        lines.append(f'            </Password>')
    lines.append(f'          </LocalAccount>')
    lines.append(f'        </LocalAccounts>')
    lines.append(f'      </UserAccounts>')

    # AutoLogon
    lines.append(f'      <AutoLogon>')
    lines.append(f'        <Enabled>true</Enabled>')
    lines.append(f'        <Username>{_xml_escape(username)}</Username>')
    if opts.local_password:
        lines.append(f'        <Password>')
        lines.append(f'          <Value>{_xml_escape(opts.local_password)}</Value>')
        lines.append(f'          <PlainText>true</PlainText>')
        lines.append(f'        </Password>')
    lines.append(f'        <LogonCount>1</LogonCount>')
    lines.append(f'      </AutoLogon>')

    # FirstLogonCommands: remove apps and write report
    lines.append(f'      <FirstLogonCommands>')
    logon_order = 0

    # Run the setup script (placed by WIM update) that handles app removal + report
    logon_order += 1
    lines.append(f'        <SynchronousCommand wcm:action="add">')
    lines.append(f'          <Order>{logon_order}</Order>')
    lines.append(f'          <CommandLine>powershell.exe -ExecutionPolicy Bypass -File C:\\Windows\\Setup\\Scripts\\tuxutil-setup.ps1</CommandLine>')
    lines.append(f'          <Description>TuxUtil post-install setup</Description>')
    lines.append(f'        </SynchronousCommand>')

    lines.append(f'      </FirstLogonCommands>')

    lines.append(f'    </component>')
    lines.append(f'  </settings>')
    lines.append(f'</unattend>')

    return "\n".join(lines) + "\n"


def generate_setup_script(apps_to_remove: list, remove_onedrive: bool,
                          remove_edge: bool) -> str:
    """Generate the PowerShell setup script that runs at first logon.

    Removes provisioned apps, writes a report, and cleans up.
    """
    lines = []
    lines.append('# TuxUtil post-install setup script')
    lines.append('# Runs at first logon to remove bloat and generate report')
    lines.append('$ErrorActionPreference = "Continue"')
    lines.append('$report = @()')
    lines.append('$report += "TuxUtil Post-Install Report"')
    lines.append('$report += "=========================="')
    lines.append('$report += "Date: $(Get-Date -Format o)"')
    lines.append('$report += ""')
    lines.append('')

    # Remove provisioned apps
    lines.append('# Remove provisioned apps')
    lines.append('$appsToRemove = @(')
    for app in apps_to_remove:
        lines.append(f'    "{app}"')
    lines.append(')')
    lines.append('')
    lines.append('$report += "App Removal Results:"')
    lines.append('$report += "--------------------"')
    lines.append('foreach ($appPattern in $appsToRemove) {')
    lines.append('    $packages = Get-AppxProvisionedPackage -Online | Where-Object { $_.PackageName -like "*$appPattern*" }')
    lines.append('    foreach ($pkg in $packages) {')
    lines.append('        try {')
    lines.append('            Remove-AppxProvisionedPackage -Online -PackageName $pkg.PackageName -ErrorAction Stop')
    lines.append('            $report += "  REMOVED: $($pkg.PackageName)"')
    lines.append('        } catch {')
    lines.append('            $report += "  FAILED:  $($pkg.PackageName) - $($_.Exception.Message)"')
    lines.append('        }')
    lines.append('    }')
    lines.append('    # Also remove installed instances for current user')
    lines.append('    Get-AppxPackage -Name "*$appPattern*" | Remove-AppxPackage -ErrorAction SilentlyContinue')
    lines.append('}')
    lines.append('')

    # Remove OneDrive
    if remove_onedrive:
        lines.append('# Remove OneDrive')
        lines.append('$report += ""')
        lines.append('$report += "OneDrive Removal:"')
        lines.append('$onedrive = "$env:SystemRoot\\System32\\OneDriveSetup.exe"')
        lines.append('$onedrive32 = "$env:SystemRoot\\SysWOW64\\OneDriveSetup.exe"')
        lines.append('if (Test-Path $onedrive) {')
        lines.append('    Start-Process $onedrive -ArgumentList "/uninstall" -Wait -ErrorAction SilentlyContinue')
        lines.append('    $report += "  OneDrive uninstalled (64-bit)"')
        lines.append('} elseif (Test-Path $onedrive32) {')
        lines.append('    Start-Process $onedrive32 -ArgumentList "/uninstall" -Wait -ErrorAction SilentlyContinue')
        lines.append('    $report += "  OneDrive uninstalled (32-bit)"')
        lines.append('} else {')
        lines.append('    $report += "  OneDrive setup not found (already removed from image)"')
        lines.append('}')
        lines.append('')

    # Remove Edge (only if requested)
    if remove_edge:
        lines.append('# Remove Edge (requested)')
        lines.append('$report += ""')
        lines.append('$report += "Edge Removal:"')
        lines.append('$edgeDir = "${env:ProgramFiles(x86)}\\Microsoft\\Edge\\Application"')
        lines.append('if (Test-Path $edgeDir) {')
        lines.append('    $edgeVer = Get-ChildItem $edgeDir -Directory | Where-Object { $_.Name -match "^\\d" } | Select-Object -First 1')
        lines.append('    if ($edgeVer) {')
        lines.append('        $installer = Join-Path $edgeVer.FullName "Installer\\setup.exe"')
        lines.append('        if (Test-Path $installer) {')
        lines.append('            Start-Process $installer -ArgumentList "--uninstall","--system-level","--force-uninstall" -Wait -ErrorAction SilentlyContinue')
        lines.append('            $report += "  Edge uninstall attempted"')
        lines.append('        }')
        lines.append('    }')
        lines.append('} else {')
        lines.append('    $report += "  Edge not found"')
        lines.append('}')
        lines.append('')

    # List remaining provisioned apps
    lines.append('# List remaining apps for the report')
    lines.append('$report += ""')
    lines.append('$report += "Remaining Provisioned Apps:"')
    lines.append('$report += "--------------------------"')
    lines.append('$remaining = Get-AppxProvisionedPackage -Online | Select-Object -ExpandProperty DisplayName | Sort-Object')
    lines.append('foreach ($app in $remaining) {')
    lines.append('    $report += "  $app"')
    lines.append('}')
    lines.append('')

    # Save report to multiple locations
    lines.append('# Save report')
    lines.append('$reportText = $report -join "`r`n"')
    lines.append('$reportText | Out-File -FilePath "$env:SystemDrive\\tuxutil-report.txt" -Encoding UTF8')
    lines.append('')

    # Also try to write to any attached FAT disk (for QEMU test extraction)
    lines.append('# Try writing report to secondary drives for extraction')
    lines.append('$drives = Get-Volume | Where-Object { $_.DriveLetter -and $_.DriveLetter -ne "C" -and $_.DriveType -eq "Fixed" }')
    lines.append('foreach ($drv in $drives) {')
    lines.append('    $letter = $drv.DriveLetter')
    lines.append('    try {')
    lines.append('        $reportText | Out-File -FilePath "${letter}:\\tuxutil-report.txt" -Encoding UTF8 -ErrorAction Stop')
    lines.append('    } catch {}')
    lines.append('}')
    lines.append('')

    lines.append('# Done')
    lines.append('$report += ""')
    lines.append('$report += "Setup complete."')

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# ISO build pipeline
# ---------------------------------------------------------------------------

def build_iso(iso_path: str, output_path: str, opts: BuildOptions,
              work_dir: Optional[str] = None):
    """Build a custom Windows ISO.

    Steps:
    1. Validate input
    2. Extract ISO contents with 7z
    3. Get editions, resolve the selected one
    4. Export selected edition to a new WIM with wimlib-imagex
    5. Modify the WIM: delete bloat paths, add scripts
    6. Compress to ESD (solid LZMS) or split if needed
    7. Generate autounattend.xml
    8. Handle drivers ($WinPEDriver$ + Drivers dir in image)
    9. Create ISO with xorriso (El Torito BIOS+UEFI boot)
    """
    iso_path = os.path.abspath(iso_path)
    output_path = os.path.abspath(output_path)

    if iso_path == output_path:
        raise SystemExit("Output path must be different from input ISO path.")

    system.require_tools(REQUIRED_TOOLS)
    _validate_iso(iso_path)

    if work_dir is None:
        work_dir = os.path.join(
            os.path.expanduser("~"), "Projects", "tuxutil", ".work", "winiso"
        )
    os.makedirs(work_dir, exist_ok=True)

    _check_disk_space(work_dir, iso_path)

    extract_dir = os.path.join(work_dir, "extract")
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir)

    try:
        _do_build(iso_path, output_path, opts, work_dir, extract_dir)
    except BaseException:
        # Clean up on failure but keep the work dir itself
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        raise


def _do_build(iso_path: str, output_path: str, opts: BuildOptions,
              work_dir: str, extract_dir: str):
    """Internal build logic."""

    # Step 1: Extract ISO
    system.progress(0, "Extracting ISO contents...")
    system.run(["7z", "x", iso_path, f"-o{extract_dir}", "-y"])

    # Step 2: Get editions
    system.progress(5, "Reading edition information...")
    install_wim = os.path.join(extract_dir, "sources", "install.wim")
    install_esd = os.path.join(extract_dir, "sources", "install.esd")
    if os.path.exists(install_wim):
        wim_path = install_wim
    elif os.path.exists(install_esd):
        wim_path = install_esd
    else:
        raise SystemExit("No install.wim or install.esd found in extracted ISO.")

    r = system.run(["wimlib-imagex", "info", wim_path], capture=True)
    editions = _parse_wiminfo(r.stdout)
    if not editions:
        raise SystemExit("No Windows editions found in the install image.")

    edition = _resolve_edition(editions, opts.edition)
    print(f"Selected edition: {edition.name} (index {edition.index})", flush=True)

    # Step 3: Export selected edition to a new WIM
    system.progress(10, f"Exporting {edition.name}...")
    new_wim = os.path.join(work_dir, "install_new.wim")
    if os.path.exists(new_wim):
        os.remove(new_wim)

    system.run([
        "wimlib-imagex", "export", wim_path, str(edition.index),
        new_wim, "--compress=LZX"
    ])

    # Step 4: Modify the WIM — delete bloat, add scripts
    system.progress(30, "Removing bloat from image...")
    apps_to_remove = _get_apps_to_remove(opts)

    # Delete bloat paths from the WIM
    delete_cmds = []
    for path in BLOAT_WIM_PATHS:
        if not opts.remove_onedrive and "OneDrive" in path:
            continue
        if not opts.remove_edge and "Edge" in path:
            continue
        delete_cmds.append(f"delete --force {path}")

    # Also delete provisioned app directories from the WIM
    # We list the WIM contents to find matching app directories
    r = subprocess.run(
        ["wimlib-imagex", "dir", new_wim, "1", "--one-file-only"],
        capture_output=True, text=True
    )
    if r.returncode == 0:
        # Look for app directories under Program Files/WindowsApps
        for line in r.stdout.splitlines():
            line = line.strip()
            # Match provisioned app directories
            for app in apps_to_remove:
                app_lower = app.lower().replace(".", ".")
                if app_lower in line.lower() and "WindowsApps" in line:
                    delete_cmds.append(f"delete --force {line}")

    if delete_cmds:
        cmd_text = "\n".join(delete_cmds)
        update_file = os.path.join(work_dir, "wim_updates.txt")
        with open(update_file, "w") as f:
            f.write(cmd_text + "\n")
        # wimlib-imagex update ignores errors for missing paths with --force
        subprocess.run(
            ["wimlib-imagex", "update", new_wim, "1"],
            input=cmd_text, capture_output=True, text=True
        )

    # Step 5: Add setup scripts to the WIM
    system.progress(35, "Adding setup scripts...")
    scripts_dir = os.path.join(work_dir, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)

    # Create the PowerShell setup script
    ps_script = generate_setup_script(apps_to_remove, opts.remove_onedrive, opts.remove_edge)
    ps_path = os.path.join(scripts_dir, "tuxutil-setup.ps1")
    with open(ps_path, "w", newline="\r\n") as f:
        f.write(ps_script)

    # Add scripts to the WIM under Windows\Setup\Scripts
    add_cmds = [
        f"add {ps_path} /Windows/Setup/Scripts/tuxutil-setup.ps1"
    ]
    add_text = "\n".join(add_cmds)
    subprocess.run(
        ["wimlib-imagex", "update", new_wim, "1"],
        input=add_text, capture_output=True, text=True
    )

    # Step 5b: Add drivers to the WIM if specified
    if opts.driver_dirs:
        system.progress(37, "Injecting drivers into image...")
        driver_add_cmds = []
        for ddir in opts.driver_dirs:
            ddir = os.path.abspath(ddir)
            if not os.path.isdir(ddir):
                print(f"Warning: driver directory not found: {ddir}", flush=True)
                continue
            # Add to Drivers dir inside Windows for pnputil
            driver_add_cmds.append(f"add {ddir} /Drivers --no-acls")
        if driver_add_cmds:
            drv_text = "\n".join(driver_add_cmds)
            subprocess.run(
                ["wimlib-imagex", "update", new_wim, "1"],
                input=drv_text, capture_output=True, text=True
            )

    # Step 6: Compress WIM — export to ESD (solid LZMS) for <4GB file size
    system.progress(40, "Compressing image (this may take a while)...")
    final_image = os.path.join(extract_dir, "sources", "install.esd")
    # Remove original install.wim/esd
    for f in (install_wim, install_esd):
        if os.path.exists(f):
            os.remove(f)

    if opts.compression == "solid":
        system.run([
            "wimlib-imagex", "export", new_wim, "1",
            final_image, "--solid", "--compress=LZMS"
        ])
    elif opts.compression == "lzx":
        final_image = os.path.join(extract_dir, "sources", "install.wim")
        system.run([
            "wimlib-imagex", "export", new_wim, "1",
            final_image, "--compress=LZX"
        ])
    else:
        final_image = os.path.join(extract_dir, "sources", "install.wim")
        system.run([
            "wimlib-imagex", "export", new_wim, "1",
            final_image, "--compress=none"
        ])

    # Clean up intermediate WIM
    os.remove(new_wim)

    # Check if the file is over 4GB (ISO9660 limit) and split if needed
    final_size = os.path.getsize(final_image)
    print(f"Install image size: {final_size / (1024**3):.2f} GB", flush=True)
    if final_size > 4 * 1024**3:
        system.progress(60, "Splitting image (over 4GB)...")
        # Split into .swm files, 3800 MB each to stay well under 4GB
        base_name = os.path.splitext(final_image)[0]
        system.run([
            "wimlib-imagex", "split", final_image,
            base_name + ".swm", "3800"
        ])
        os.remove(final_image)

    # Step 7: Generate autounattend.xml
    system.progress(65, "Generating autounattend.xml...")
    xml_content = generate_autounattend(edition, opts, apps_to_remove)
    xml_path = os.path.join(extract_dir, "autounattend.xml")
    with open(xml_path, "w", newline="\r\n") as f:
        f.write(xml_content)

    # Step 7b: Add $WinPEDriver$ for driver injection during Setup
    if opts.driver_dirs:
        system.progress(67, "Copying drivers to ISO root...")
        pe_driver_dir = os.path.join(extract_dir, "$WinPEDriver$")
        os.makedirs(pe_driver_dir, exist_ok=True)
        for ddir in opts.driver_dirs:
            ddir = os.path.abspath(ddir)
            if os.path.isdir(ddir):
                dest = os.path.join(pe_driver_dir, os.path.basename(ddir))
                shutil.copytree(ddir, dest, dirs_exist_ok=True)

    # Step 8: Build ISO with xorriso
    system.progress(70, "Creating ISO...")

    # Identify boot record files
    bios_boot = os.path.join(extract_dir, "boot", "etfsboot.com")
    uefi_noprompt = os.path.join(extract_dir, "efi", "microsoft", "boot", "efisys_noprompt.bin")
    uefi_boot = os.path.join(extract_dir, "efi", "microsoft", "boot", "efisys.bin")

    if not os.path.exists(bios_boot):
        raise SystemExit(f"BIOS boot record not found: {bios_boot}")

    uefi_file = uefi_noprompt if os.path.exists(uefi_noprompt) else uefi_boot
    if not os.path.exists(uefi_file):
        raise SystemExit(f"UEFI boot record not found: tried {uefi_noprompt} and {uefi_boot}")

    # Get UEFI boot file size in 512-byte sectors for boot-load-size
    uefi_size = os.path.getsize(uefi_file)
    uefi_sectors = (uefi_size + 511) // 512

    # Volume label from source ISO
    vol_label = "CCCOMA_X64FRE_EN-GB_DV9"
    # Try to extract from the 7z listing
    r = subprocess.run(
        ["7z", "l", iso_path], capture_output=True, text=True
    )
    for line in r.stdout.splitlines():
        if "LogicalVolumeId:" in line or "VolumeId:" in line:
            parts = line.split(":", 1)
            if len(parts) == 2:
                label = parts[1].strip()
                if label:
                    vol_label = label
                    break

    # Build xorriso command
    # ponytail: we use -iso-level 4 (ISO 9660:1999) for long filenames and >4GB support,
    # but all files must be under 4GB individually for Windows Setup compatibility.
    xorriso_cmd = [
        "xorriso", "-as", "mkisofs",
        "-iso-level", "4",
        "-joliet",
        "-joliet-long",
        "-rational-rock",
        "-volid", vol_label,
        # BIOS boot
        "-b", "boot/etfsboot.com",
        "-no-emul-boot",
        "-boot-load-size", "8",
        "-boot-info-table",
        # UEFI boot (alt boot entry)
        "-eltorito-alt-boot",
        "-e", os.path.relpath(uefi_file, extract_dir),
        "-no-emul-boot",
        "-boot-load-size", str(uefi_sectors),
        # Output
        "-o", output_path,
        extract_dir
    ]

    system.run(xorriso_cmd)

    system.progress(95, "Cleaning up...")
    shutil.rmtree(extract_dir)

    output_size = os.path.getsize(output_path)
    system.progress(100, f"Done! Output: {output_path} ({output_size / (1024**3):.2f} GB)")
    print(f"Build complete: {output_path}", flush=True)
    print(f"  Size: {output_size / (1024**3):.2f} GB "
          f"(original: {os.path.getsize(iso_path) / (1024**3):.2f} GB)", flush=True)


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------

def cmd_info(args):
    iso_path = args.iso
    _validate_iso(iso_path)
    editions = _get_editions(iso_path)
    data = {
        "editions": [
            {
                "index": e.index,
                "name": e.name,
                "arch": e.arch,
                "size_bytes": e.size_bytes,
                "build": e.build,
            }
            for e in editions
        ]
    }
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"Found {len(editions)} edition(s):")
        for e in editions:
            print(f"  [{e.index}] {e.name} ({e.arch}, {e.size_bytes / (1024**3):.1f} GB, build {e.build})")
    return 0


def cmd_options(args):
    options = [
        {"flag": "--edition", "type": "choice", "label": "Windows Edition",
         "help": "Edition to keep (name or index). Default: Pro if present, else first.",
         "default": "", "choices": []},
        {"flag": "--remove-apps", "type": "list", "label": "Apps to Remove",
         "help": "Comma-separated app package names to remove. Default: common bloatware.",
         "default": ",".join(a for a, d in BLOAT_APPS if d)},
        {"flag": "--keep-apps", "type": "list", "label": "Apps to Keep",
         "help": "Comma-separated apps to keep even if in default removal list.",
         "default": ""},
        {"flag": "--no-remove-onedrive", "type": "bool", "label": "Keep OneDrive",
         "help": "Don't remove OneDrive.", "default": False},
        {"flag": "--remove-edge", "type": "bool", "label": "Remove Edge",
         "help": "Remove Microsoft Edge (risky, may break some things).", "default": False},
        {"flag": "--no-tweaks", "type": "bool", "label": "Skip Tweaks",
         "help": "Don't apply telemetry/Copilot/Recall/ads tweaks.", "default": False},
        {"flag": "--no-bypass", "type": "bool", "label": "Skip Requirement Bypass",
         "help": "Don't bypass TPM/SecureBoot/RAM/CPU requirements.", "default": False},
        {"flag": "--username", "type": "str", "label": "Local Username",
         "help": "Local account username (default: User).", "default": "User"},
        {"flag": "--password", "type": "str", "label": "Local Password",
         "help": "Local account password (optional).", "default": ""},
        {"flag": "--locale", "type": "str", "label": "Locale",
         "help": "System locale (e.g. en-US). Default: from source image.", "default": ""},
        {"flag": "--keyboard", "type": "str", "label": "Keyboard Layout",
         "help": "Keyboard layout (e.g. 0409:00000409). Default: from locale.", "default": ""},
        {"flag": "--timezone", "type": "str", "label": "Timezone",
         "help": "Windows timezone name (e.g. 'Pacific Standard Time'). Default: UTC.", "default": ""},
        {"flag": "--drivers", "type": "path", "label": "Driver Directories",
         "help": "Comma-separated paths to directories containing .inf drivers.", "default": ""},
        {"flag": "--no-disable-hibernation", "type": "bool", "label": "Keep Hibernation",
         "help": "Don't disable hibernation.", "default": False},
        {"flag": "--compact-os", "type": "bool", "label": "Compact OS",
         "help": "Enable OS compaction to save disk space.", "default": False},
        {"flag": "--compression", "type": "choice", "label": "Image Compression",
         "help": "Compression method: solid (ESD/LZMS, smallest), lzx (standard WIM), none.",
         "default": "solid", "choices": ["solid", "lzx", "none"]},
    ]
    if args.json:
        print(json.dumps(options, indent=2))
    else:
        for opt in options:
            print(f"  {opt['flag']:30s} {opt['help']}")
    return 0


def cmd_build(args):
    opts = BuildOptions(
        edition=args.edition or "",
        remove_apps=args.remove_apps.split(",") if args.remove_apps else None,
        keep_apps=args.keep_apps.split(",") if args.keep_apps else None,
        remove_onedrive=not args.no_remove_onedrive,
        remove_edge=args.remove_edge,
        apply_tweaks=not args.no_tweaks,
        bypass_requirements=not args.no_bypass,
        local_username=args.username,
        local_password=args.password,
        locale=args.locale,
        keyboard=args.keyboard,
        timezone=args.timezone,
        driver_dirs=args.drivers.split(",") if args.drivers else None,
        disable_hibernation=not args.no_disable_hibernation,
        compact_os=args.compact_os,
        compression=args.compression,
    )
    build_iso(args.iso, args.output, opts, work_dir=args.work_dir)
    return 0


def register(sub):
    p = sub.add_parser("winiso", help="build a custom Windows 11 ISO")
    sp = p.add_subparsers(dest="winiso_command")

    # info
    info_p = sp.add_parser("info", help="list editions in a Windows ISO")
    info_p.add_argument("iso", help="path to Windows ISO")
    info_p.add_argument("--json", action="store_true", help="output as JSON")
    info_p.set_defaults(func=cmd_info)

    # options
    opts_p = sp.add_parser("options", help="describe build options")
    opts_p.add_argument("--json", action="store_true", help="output as JSON")
    opts_p.set_defaults(func=cmd_options)

    # build
    build_p = sp.add_parser("build", help="build a custom Windows ISO")
    build_p.add_argument("iso", help="path to source Windows ISO")
    build_p.add_argument("-o", "--output", required=True, help="output ISO path")
    build_p.add_argument("--edition", default="", help="edition name or index")
    build_p.add_argument("--remove-apps", default=None,
                         help="comma-separated app names to remove")
    build_p.add_argument("--keep-apps", default=None,
                         help="comma-separated apps to keep from default removal")
    build_p.add_argument("--no-remove-onedrive", action="store_true",
                         help="keep OneDrive")
    build_p.add_argument("--remove-edge", action="store_true",
                         help="remove Microsoft Edge")
    build_p.add_argument("--no-tweaks", action="store_true",
                         help="skip registry tweaks")
    build_p.add_argument("--no-bypass", action="store_true",
                         help="don't bypass TPM/SecureBoot/RAM/CPU checks")
    build_p.add_argument("--username", default="User",
                         help="local account username")
    build_p.add_argument("--password", default="",
                         help="local account password")
    build_p.add_argument("--locale", default="",
                         help="system locale (e.g. en-US)")
    build_p.add_argument("--keyboard", default="",
                         help="keyboard layout")
    build_p.add_argument("--timezone", default="",
                         help="Windows timezone name")
    build_p.add_argument("--drivers", default="",
                         help="comma-separated driver directories")
    build_p.add_argument("--no-disable-hibernation", action="store_true",
                         help="keep hibernation enabled")
    build_p.add_argument("--compact-os", action="store_true",
                         help="enable OS compaction")
    build_p.add_argument("--compression", default="solid",
                         choices=["solid", "lzx", "none"],
                         help="image compression method")
    build_p.add_argument("--work-dir", default=None,
                         help="working directory for temp files")
    build_p.set_defaults(func=cmd_build)

    # Default handler if no subcommand
    p.set_defaults(func=lambda args: p.print_help() or 1)
