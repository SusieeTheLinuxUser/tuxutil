"""Tests for tuxutil.winiso — unit tests that run in CI with no root, ISOs, or network."""

import io
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch, MagicMock

from tuxutil import winiso


class TestValidateIso(unittest.TestCase):
    def test_validate_iso_passes_in_dry_run(self):
        """--dry-run must not stub out the read-only 7z probe: a real ISO still validates."""
        with tempfile.NamedTemporaryFile(suffix=".iso") as iso:
            listing = subprocess.CompletedProcess([], 0, "sources/install.wim\n", "")
            with patch.object(winiso.system, "DRY_RUN", True), \
                    patch.object(winiso.system, "query", return_value=listing) as query:
                winiso._validate_iso(iso.name)  # must not raise
            self.assertEqual(query.call_args.args[0][0], "7z")
            self.assertIn("sources/install.wim", query.call_args.args[0])

    def test_a_failed_listing_names_the_7z_error(self):
        with tempfile.NamedTemporaryFile(suffix=".iso") as iso:
            failed = subprocess.CompletedProcess([], 2, "", "E_FAIL boom")
            with patch.object(winiso.system, "query", return_value=failed):
                with self.assertRaises(SystemExit) as ctx:
                    winiso._validate_iso(iso.name)
            self.assertIn("boom", str(ctx.exception))
            self.assertIn(iso.name, str(ctx.exception))


class TestXmlEscape(unittest.TestCase):
    """Test XML escaping for autounattend.xml generation."""

    def test_plain_text(self):
        self.assertEqual(winiso._xml_escape("hello"), "hello")

    def test_ampersand(self):
        self.assertEqual(winiso._xml_escape("A&B"), "A&amp;B")

    def test_angle_brackets(self):
        self.assertEqual(winiso._xml_escape("<script>"), "&lt;script&gt;")

    def test_quotes(self):
        self.assertEqual(winiso._xml_escape('say "hi"'), "say &quot;hi&quot;")

    def test_special_username(self):
        # A username with special chars must be safely escaped
        escaped = winiso._xml_escape('Admin<>&"User')
        self.assertNotIn("<", escaped)
        self.assertNotIn("&", escaped.replace("&amp;", "").replace("&lt;", "")
                         .replace("&gt;", "").replace("&quot;", ""))

    def test_password_with_xml_chars(self):
        escaped = winiso._xml_escape('P@ss<w>&rd"!')
        self.assertNotIn("<", escaped)
        # Must contain escaped versions
        self.assertIn("&lt;", escaped)
        self.assertIn("&amp;", escaped)
        self.assertIn("&quot;", escaped)


class TestParseWimInfo(unittest.TestCase):
    """Test parsing of wimlib-imagex info output."""

    SAMPLE_OUTPUT = textwrap.dedent("""\
        WIM Information:
        ----------------
        Path:           /tmp/install.wim
        Image Count:    3
        Compression:    LZX

        Available Images:
        -----------------
        Index:                  1
        Name:                   Windows 11 Home
        Description:            Windows 11 Home
        Architecture:           x86_64
        Total Bytes:            25739214235
        Build:                  26100
        Service Pack Build:     1742
        Edition ID:             Core
        Default Language:       en-US

        Index:                  2
        Name:                   Windows 11 Pro
        Description:            Windows 11 Pro
        Architecture:           x86_64
        Total Bytes:            26779377698
        Build:                  26100
        Service Pack Build:     1742
        Edition ID:             Professional
        Default Language:       en-US

        Index:                  3
        Name:                   Windows 11 Pro N
        Description:            Windows 11 Pro N
        Architecture:           x86_64
        Total Bytes:            26046762311
        Build:                  26100
        Service Pack Build:     0
        Edition ID:             ProfessionalN
        Default Language:       en-US
    """)

    def test_parse_count(self):
        editions = winiso._parse_wiminfo(self.SAMPLE_OUTPUT)
        self.assertEqual(len(editions), 3)

    def test_parse_names(self):
        editions = winiso._parse_wiminfo(self.SAMPLE_OUTPUT)
        names = [e.name for e in editions]
        self.assertEqual(names, ["Windows 11 Home", "Windows 11 Pro", "Windows 11 Pro N"])

    def test_parse_indices(self):
        editions = winiso._parse_wiminfo(self.SAMPLE_OUTPUT)
        indices = [e.index for e in editions]
        self.assertEqual(indices, [1, 2, 3])

    def test_parse_arch(self):
        editions = winiso._parse_wiminfo(self.SAMPLE_OUTPUT)
        for e in editions:
            self.assertEqual(e.arch, "x86_64")

    def test_parse_size(self):
        editions = winiso._parse_wiminfo(self.SAMPLE_OUTPUT)
        self.assertEqual(editions[0].size_bytes, 25739214235)

    def test_parse_build(self):
        editions = winiso._parse_wiminfo(self.SAMPLE_OUTPUT)
        self.assertEqual(editions[0].build, "26100.1742")
        # SP build 0 should just show the base build
        self.assertEqual(editions[2].build, "26100")

    def test_parse_edition_id(self):
        editions = winiso._parse_wiminfo(self.SAMPLE_OUTPUT)
        self.assertEqual(editions[0].edition_id, "Core")
        self.assertEqual(editions[1].edition_id, "Professional")

    def test_parse_empty(self):
        editions = winiso._parse_wiminfo("")
        self.assertEqual(editions, [])


class TestResolveEdition(unittest.TestCase):
    """Test edition resolution logic."""

    def setUp(self):
        self.editions = [
            winiso.Edition(1, "Windows 11 Home", "x86_64", 25_000_000_000, "26100"),
            winiso.Edition(2, "Windows 11 Pro", "x86_64", 26_000_000_000, "26100"),
            winiso.Edition(3, "Windows 11 Pro N", "x86_64", 26_000_000_000, "26100"),
            winiso.Edition(4, "Windows 11 Education", "x86_64", 26_000_000_000, "26100"),
            winiso.Edition(5, "Windows 11 Pro Education", "x86_64", 26_000_000_000, "26100"),
            winiso.Edition(6, "Windows 11 Pro for Workstations", "x86_64", 26_000_000_000, "26100"),
        ]

    def test_default_selects_pro(self):
        """Default (empty string) should select plain Pro, not Pro N or Pro Education."""
        ed = winiso._resolve_edition(self.editions, "")
        self.assertEqual(ed.name, "Windows 11 Pro")

    def test_default_without_pro(self):
        """Without Pro, default should be the first edition."""
        editions = [self.editions[0], self.editions[3]]  # Home, Education
        ed = winiso._resolve_edition(editions, "")
        self.assertEqual(ed.name, "Windows 11 Home")

    def test_by_index(self):
        ed = winiso._resolve_edition(self.editions, "4")
        self.assertEqual(ed.name, "Windows 11 Education")

    def test_by_exact_name(self):
        ed = winiso._resolve_edition(self.editions, "Windows 11 Home")
        self.assertEqual(ed.name, "Windows 11 Home")

    def test_by_partial_name(self):
        ed = winiso._resolve_edition(self.editions, "Education")
        self.assertEqual(ed.name, "Windows 11 Education")

    def test_bad_index(self):
        with self.assertRaises(SystemExit):
            winiso._resolve_edition(self.editions, "99")

    def test_bad_name(self):
        with self.assertRaises(SystemExit):
            winiso._resolve_edition(self.editions, "Enterprise")


class TestGetAppsToRemove(unittest.TestCase):
    """Test app removal list generation."""

    def test_default_removals(self):
        opts = winiso.BuildOptions()
        apps = winiso._get_apps_to_remove(opts)
        # Should include common bloat
        self.assertIn("Microsoft.BingNews", apps)
        self.assertIn("Clipchamp.Clipchamp", apps)
        self.assertIn("Microsoft.MicrosoftTeams", apps)
        # Should not include non-default removals
        self.assertNotIn("Microsoft.WindowsCalculator", apps)
        self.assertNotIn("Microsoft.Paint", apps)

    def test_explicit_removal_list(self):
        opts = winiso.BuildOptions(remove_apps=["MyApp1", "MyApp2"])
        apps = winiso._get_apps_to_remove(opts)
        self.assertEqual(apps, ["MyApp1", "MyApp2"])

    def test_keep_apps(self):
        opts = winiso.BuildOptions(keep_apps=["Microsoft.BingNews"])
        apps = winiso._get_apps_to_remove(opts)
        self.assertNotIn("Microsoft.BingNews", apps)
        # Other defaults should still be removed
        self.assertIn("Clipchamp.Clipchamp", apps)


class TestAutounattendXml(unittest.TestCase):
    """Test autounattend.xml generation."""

    def setUp(self):
        self.edition = winiso.Edition(
            index=6, name="Windows 11 Pro", arch="x86_64",
            size_bytes=26_000_000_000, build="26100.1742",
            edition_id="Professional", lang="en-US"
        )

    def test_valid_xml(self):
        """Generated XML must be parseable."""
        import xml.etree.ElementTree as ET
        opts = winiso.BuildOptions(local_username="TestUser")
        xml_str = winiso.generate_autounattend(self.edition, opts)
        # Should parse without error
        root = ET.fromstring(xml_str)
        self.assertEqual(root.tag, f"{{{winiso._UNATTEND_NS}}}unattend")

    def test_bypass_keys(self):
        opts = winiso.BuildOptions(bypass_requirements=True)
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertIn("BypassTPMCheck", xml_str)
        self.assertIn("BypassSecureBootCheck", xml_str)
        self.assertIn("BypassRAMCheck", xml_str)
        self.assertIn("BypassCPUCheck", xml_str)

    def test_no_bypass(self):
        opts = winiso.BuildOptions(bypass_requirements=False)
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertNotIn("BypassTPMCheck", xml_str)

    def test_local_account(self):
        opts = winiso.BuildOptions(local_username="Admin", local_password="Secret123")
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertIn("<Name>Admin</Name>", xml_str)
        self.assertIn("HideOnlineAccountScreens", xml_str)
        self.assertIn("SkipMachineOOBE", xml_str)

    def test_password_not_plaintext_in_attribute(self):
        """Password should appear in a Value element, not as a bare attribute."""
        opts = winiso.BuildOptions(local_password="My<Secret>&Pass\"")
        xml_str = winiso.generate_autounattend(self.edition, opts)
        # Should be XML-escaped
        self.assertIn("My&lt;Secret&gt;&amp;Pass&quot;", xml_str)
        # Should never appear unescaped
        self.assertNotIn('My<Secret>', xml_str)

    def test_username_escaping(self):
        opts = winiso.BuildOptions(local_username='User<>&"Name')
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertIn("User&lt;&gt;&amp;&quot;Name", xml_str)
        # Verify the XML is still parseable
        import xml.etree.ElementTree as ET
        ET.fromstring(xml_str)

    def test_wcm_namespace(self):
        """Verify wcm namespace is properly declared."""
        opts = winiso.BuildOptions()
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertIn('xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"', xml_str)

    def test_wcm_action_attributes(self):
        opts = winiso.BuildOptions()
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertIn('wcm:action="add"', xml_str)

    def test_hibernation_disable(self):
        opts = winiso.BuildOptions(disable_hibernation=True)
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertIn("powercfg /h off", xml_str)

    def test_hibernation_keep(self):
        opts = winiso.BuildOptions(disable_hibernation=False)
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertNotIn("powercfg", xml_str)

    def test_compact_os(self):
        opts = winiso.BuildOptions(compact_os=True)
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertIn("compact /compactos:always", xml_str)

    def test_locale(self):
        opts = winiso.BuildOptions(locale="de-DE", keyboard="0407:00000407",
                                   timezone="W. Europe Standard Time")
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertIn("de-DE", xml_str)
        self.assertIn("0407:00000407", xml_str)
        self.assertIn("W. Europe Standard Time", xml_str)

    def test_default_never_touches_a_disk(self):
        xml_str = winiso.generate_autounattend(self.edition, winiso.BuildOptions())
        for forbidden in ("DiskConfiguration", "WillWipeDisk", "<InstallTo>", "InstallToAvailablePartition"):
            self.assertNotIn(forbidden, xml_str)
        ET.fromstring(xml_str)

    def test_wipe_disk_is_opt_in(self):
        xml_str = winiso.generate_autounattend(self.edition, winiso.BuildOptions(wipe_disk=True))
        self.assertIn("<WillWipeDisk>true</WillWipeDisk>", xml_str)
        self.assertIn("<Type>EFI</Type>", xml_str)
        self.assertIn("<PartitionID>3</PartitionID>", xml_str)
        ET.fromstring(xml_str)

    def test_all_passes_present(self):
        """Must have windowsPE, specialize, and oobeSystem passes."""
        opts = winiso.BuildOptions(bypass_requirements=True, apply_tweaks=True)
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertIn('pass="windowsPE"', xml_str)
        self.assertIn('pass="specialize"', xml_str)
        self.assertIn('pass="oobeSystem"', xml_str)

    def test_firstlogon_script(self):
        opts = winiso.BuildOptions()
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertIn("tuxutil-setup.ps1", xml_str)

    def test_registry_tweaks(self):
        opts = winiso.BuildOptions(apply_tweaks=True)
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertIn("AllowTelemetry", xml_str)
        self.assertIn("TurnOffWindowsCopilot", xml_str)
        self.assertIn("DisableAIDataAnalysis", xml_str)

    def test_no_tweaks(self):
        opts = winiso.BuildOptions(apply_tweaks=False)
        xml_str = winiso.generate_autounattend(self.edition, opts)
        self.assertNotIn("AllowTelemetry", xml_str)


class TestSetupScript(unittest.TestCase):
    """Test PowerShell setup script generation."""

    def test_contains_app_removal(self):
        script = winiso.generate_setup_script(
            ["Microsoft.BingNews", "Clipchamp.Clipchamp"],
            remove_onedrive=True, remove_edge=False
        )
        self.assertIn("Microsoft.BingNews", script)
        self.assertIn("Clipchamp.Clipchamp", script)
        self.assertIn("Remove-AppxProvisionedPackage", script)

    def test_onedrive_removal(self):
        script = winiso.generate_setup_script([], remove_onedrive=True, remove_edge=False)
        self.assertIn("OneDriveSetup.exe", script)
        self.assertIn("/uninstall", script)

    def test_no_onedrive_removal(self):
        script = winiso.generate_setup_script([], remove_onedrive=False, remove_edge=False)
        self.assertNotIn("OneDriveSetup", script)

    def test_edge_removal(self):
        script = winiso.generate_setup_script([], remove_onedrive=False, remove_edge=True)
        self.assertIn("--force-uninstall", script)

    def test_no_edge_removal(self):
        script = winiso.generate_setup_script([], remove_onedrive=False, remove_edge=False)
        self.assertNotIn("force-uninstall", script)

    def test_report_generation(self):
        script = winiso.generate_setup_script(["X"], remove_onedrive=False, remove_edge=False)
        self.assertIn("tuxutil-log.txt", script)
        self.assertIn("Get-AppxProvisionedPackage", script)

    def test_report_never_written_to_other_drives(self):
        script = winiso.generate_setup_script(["Microsoft.BingNews"], True, False)
        self.assertNotIn("Get-Volume", script)
        self.assertNotIn("[char[]]", script)
        self.assertIn("tuxutil-log.txt", script)

    def test_first_logon_removes_answer_file(self):
        script = winiso.generate_setup_script([], False, False)
        self.assertIn("Panther", script)
        self.assertIn("unattend*.xml", script)


class TestBuildOptions(unittest.TestCase):
    """Test BuildOptions defaults."""

    def test_defaults(self):
        opts = winiso.BuildOptions()
        self.assertEqual(opts.edition, "")
        self.assertTrue(opts.bypass_requirements)
        self.assertTrue(opts.remove_onedrive)
        self.assertFalse(opts.remove_edge)
        self.assertTrue(opts.apply_tweaks)
        self.assertTrue(opts.disable_hibernation)
        self.assertFalse(opts.compact_os)
        self.assertEqual(opts.compression, "solid")
        self.assertEqual(opts.local_username, "")
        self.assertEqual(opts.local_password, "")


class TestCLIOptionsOutput(unittest.TestCase):
    """Test the CLI options subcommand."""

    def test_options_json(self):
        """The options command should produce valid JSON."""
        import io
        from unittest.mock import patch
        args = MagicMock()
        args.json = True
        with patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
            winiso.cmd_options(args)
            output = mock_stdout.getvalue()
        data = json.loads(output)
        self.assertIsInstance(data, list)
        # Each option must have required keys
        for opt in data:
            self.assertIn("flag", opt)
            self.assertIn("type", opt)
            self.assertIn("label", opt)
            self.assertIn("help", opt)
            self.assertIn("default", opt)

    def test_options_has_required_flags(self):
        import io
        args = MagicMock()
        args.json = True
        with patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
            winiso.cmd_options(args)
            output = mock_stdout.getvalue()
        data = json.loads(output)
        flags = [o["flag"] for o in data]
        required = ["--edition", "--username", "--compression", "--locale",
                     "--drivers", "--compact-os"]
        for flag in required:
            self.assertIn(flag, flags, f"Missing option: {flag}")


class TestBootRecordSelection(unittest.TestCase):
    """Boot catalog selection used by the ISO builder."""

    def _tree(self, root, noprompt=True, regular=True, bios=True):
        os.makedirs(os.path.join(root, "boot"), exist_ok=True)
        os.makedirs(os.path.join(root, "efi", "microsoft", "boot"), exist_ok=True)
        if bios:
            open(os.path.join(root, "boot", "etfsboot.com"), "w").close()
        if noprompt:
            open(os.path.join(root, "efi", "microsoft", "boot", "efisys_noprompt.bin"), "w").close()
        if regular:
            open(os.path.join(root, "efi", "microsoft", "boot", "efisys.bin"), "w").close()

    def test_uefi_prompting_image_preferred(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, noprompt=True, regular=True)
            _bios, uefi, rel = winiso.choose_boot_files(d)
            self.assertTrue(uefi.endswith(os.path.join("boot", "efisys.bin")))
            self.assertEqual(rel, "efi/microsoft/boot/efisys.bin")

    def test_uefi_falls_back_to_efisys(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, noprompt=False, regular=True)
            _bios, uefi, rel = winiso.choose_boot_files(d)
            self.assertTrue(uefi.endswith(os.path.join("efi", "microsoft", "boot", "efisys.bin")))
            self.assertEqual(rel, "efi/microsoft/boot/efisys.bin")

    def test_missing_bios_boot(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, bios=False)
            with self.assertRaises(SystemExit):
                winiso.choose_boot_files(d)

    def test_xorriso_uses_chosen_boot_record(self):
        argv = winiso._xorriso_argv(
            "/work/extract", "/work/out.iso", "CCCOMA_X64FRE_EN-GB_DV9",
            "efi/microsoft/boot/efisys_noprompt.bin",
        )
        self.assertEqual(argv[0], "xorriso")
        self.assertIn("boot/etfsboot.com", argv)
        self.assertIn("efi/microsoft/boot/efisys_noprompt.bin", argv)
        self.assertNotIn("-udf", [a.lower() for a in argv])
        # UEFI entry must not set a short boot-load-size (that truncates efisys).
        efi_at = argv.index("efi/microsoft/boot/efisys_noprompt.bin")
        self.assertEqual(argv[efi_at - 1], "-e")
        self.assertNotIn("-boot-load-size", argv[efi_at:])


class TestWimCommands(unittest.TestCase):
    def test_export_compression_flags(self):
        solid = winiso._wim_export_argv("src.wim", 6, "out.esd", "solid")
        self.assertEqual(solid[:5], ["wimlib-imagex", "export", "src.wim", "6", "out.esd"])
        self.assertIn("--solid", solid)
        self.assertIn("--compress=LZMS", solid)
        lzx = winiso._wim_export_argv("src.wim", 1, "out.wim", "lzx")
        self.assertIn("--compress=LZX", lzx)
        self.assertNotIn("--solid", lzx)
        none = winiso._wim_export_argv("src.wim", 1, "out.wim", "none")
        self.assertIn("--compress=none", none)

    def test_unknown_compression(self):
        with self.assertRaises(SystemExit):
            winiso._wim_export_argv("a", 1, "b", "xz")

    def test_update_deletes_onedrive_not_windowsapps(self):
        lines = winiso._wim_update_lines("/tmp/tuxutil-setup.ps1", None, remove_onedrive=True)
        text = "\n".join(lines)
        self.assertIn("delete --force /Windows/System32/OneDriveSetup.exe", text)
        self.assertIn("add /tmp/tuxutil-setup.ps1 /Windows/Setup/Scripts/tuxutil-setup.ps1", text)
        self.assertNotIn("WindowsApps", text)

    def test_update_keeps_onedrive_when_asked(self):
        lines = winiso._wim_update_lines("/tmp/tuxutil-setup.ps1", ["/drv/net"], remove_onedrive=False)
        text = "\n".join(lines)
        self.assertNotIn("OneDrive", text)
        self.assertIn("add /drv/net /Drivers/net", text)

    def test_commands_do_not_contain_password(self):
        secret = "p<q>&secret"
        argv = winiso._wim_export_argv("src.wim", 1, "out.esd", "solid")
        iso = winiso._xorriso_argv("/e", "/o.iso", "LABEL", "efi/microsoft/boot/efisys_noprompt.bin")
        self.assertIn("-omit-version-number", iso)
        self.assertIn("-omit-period", iso)
        self.assertNotIn(secret, " ".join(argv + iso))


class TestOptionParsing(unittest.TestCase):
    def test_build_flags_map_onto_options(self):
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        winiso.register(sub)
        args = parser.parse_args([
            "winiso", "build", "in.iso", "-o", "out.iso",
            "--edition", "6",
            "--username", "A&B",
            "--password", "p<q",
            "--compression", "lzx",
            "--locale", "de-DE",
            "--keyboard", "0407:00000407",
            "--timezone", "W. Europe Standard Time",
            "--remove-edge",
            "--no-bypass",
            "--no-tweaks",
            "--no-remove-onedrive",
            "--compact-os",
            "--drivers", "/tmp/a,/tmp/b",
            "--keep-apps", "Microsoft.BingNews",
            "--remove-apps", "Only.This",
        ])
        opts = winiso.options_from_args(args)
        self.assertEqual(opts.edition, "6")
        self.assertEqual(opts.local_username, "A&B")
        self.assertEqual(opts.local_password, "p<q")
        args.password = ""
        with patch.dict(os.environ, {"TUXUTIL_PASSWORD": "from-env"}):
            self.assertEqual(winiso.options_from_args(args).local_password, "from-env")
            self.assertNotIn("TUXUTIL_PASSWORD", os.environ)  # Child tools must not inherit it.
        args.password = "from-flag"
        with patch.dict(os.environ, {"TUXUTIL_PASSWORD": "from-env"}):
            self.assertEqual(winiso.options_from_args(args).local_password, "from-flag")
            self.assertNotIn("TUXUTIL_PASSWORD", os.environ)
        self.assertFalse(opts.bypass_requirements)
        self.assertFalse(opts.apply_tweaks)
        self.assertFalse(opts.remove_onedrive)
        self.assertTrue(opts.remove_edge)
        self.assertTrue(opts.compact_os)
        self.assertEqual(opts.compression, "lzx")
        self.assertEqual(opts.driver_dirs, ["/tmp/a", "/tmp/b"])
        self.assertEqual(opts.remove_apps, ["Only.This"])
        self.assertNotIn("p<q", repr(opts))

    def test_7z_package_names(self):
        from tuxutil import system
        self.assertEqual(system.tool_package("7z", "arch"), "7zip")
        self.assertEqual(system.tool_package("7z", "debian"), "p7zip-full")
        self.assertEqual(system.tool_package("7z", "fedora"), "p7zip-plugins")
        self.assertIn(system.tool_package("7z", "suse"), ("p7zip", "7zip"))


class TestWorkDirAndQuoting(unittest.TestCase):
    def test_user_work_dir_is_not_the_build_dir(self):
        with tempfile.TemporaryDirectory() as folder:
            own = winiso._own_work_dir(folder, os.path.join(folder, "out.iso"))
            self.assertNotEqual(own, os.path.abspath(folder))
            self.assertEqual(os.path.dirname(own), os.path.abspath(folder))

    def test_default_work_dir_is_new_folder_next_to_output(self):
        with tempfile.TemporaryDirectory() as folder:
            existing = os.path.join(folder, "out.iso.work")
            os.mkdir(existing)
            own = winiso._own_work_dir(None, os.path.join(folder, "out.iso"))
            self.assertNotEqual(own, existing)
            self.assertEqual(os.path.dirname(own), folder)

    def test_driver_destination_with_space_is_quoted(self):
        lines = winiso._wim_update_lines("/s.ps1", ["/home/u/My Drivers"], False)
        self.assertIn('add "/home/u/My Drivers" "/Drivers/My Drivers"', lines)


class TestLocaleDefaults(unittest.TestCase):
    def test_en_gb_inferred_from_image_language(self):
        edition = winiso.Edition(
            index=6, name="Windows 11 Pro", arch="x86_64",
            size_bytes=1, build="26300.9457", edition_id="Professional", lang="en-GB",
        )
        xml_str = winiso.generate_autounattend(edition, winiso.BuildOptions())
        self.assertIn("en-GB", xml_str)
        self.assertIn("0809:00000809", xml_str)
        self.assertIn("GMT Standard Time", xml_str)
        self.assertIn("VK7JG-NPHTM-C97JM-9MPGT-3V66T", xml_str)
        self.assertIn("/IMAGE/NAME", xml_str)
        self.assertIn("Windows 11 Pro", xml_str)


class TestSetupScriptHive(unittest.TestCase):
    def test_reg_load_default_user_hive(self):
        script = winiso.generate_setup_script(["Microsoft.BingNews"], True, False, apply_tweaks=True)
        self.assertIn("reg.exe load", script)
        self.assertIn("NTUSER.DAT", script)
        self.assertIn("reg.exe unload", script)

    def test_no_hive_without_tweaks(self):
        script = winiso.generate_setup_script([], False, False, apply_tweaks=False)
        self.assertNotIn("reg.exe load", script)
        self.assertNotIn("OneDriveSetup", script)


class TestValidation(unittest.TestCase):
    """Test validation helpers."""

    def test_validate_nonexistent_iso(self):
        with self.assertRaises(SystemExit):
            winiso._validate_iso("/nonexistent/file.iso")

    def test_same_input_output(self):
        with self.assertRaises(SystemExit):
            winiso.build_iso("/tmp/test.iso", "/tmp/test.iso", winiso.BuildOptions())

    def test_disk_space_check(self):
        """Disk space check should not raise on the current filesystem."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            # Create a tiny fake file
            fake = os.path.join(d, "fake.iso")
            with open(fake, "wb") as f:
                f.write(b"x" * 1024)
            # Should not raise (plenty of space for a 1KB file)
            winiso._check_disk_space(d, fake)

    def test_disk_space_failure_cleans_work_dir(self):
        """A failing disk-space check must not leave an empty work dir behind."""
        from tuxutil import system
        with tempfile.TemporaryDirectory() as d:
            iso = os.path.join(d, "in.iso")
            open(iso, "wb").close()
            out = os.path.join(d, "out.iso")
            with patch.object(winiso, "_check_disk_space", side_effect=SystemExit(1)), \
                    patch.object(system, "require_tools"), \
                    patch.object(winiso, "_validate_iso"):
                with self.assertRaises(SystemExit):
                    winiso.build_iso(iso, out, winiso.BuildOptions())
            leftovers = [n for n in os.listdir(d) if n.startswith("tuxutil-winiso-")]
            self.assertEqual(leftovers, [])

    def test_flat_strips_and_drops_empty_parts(self):
        self.assertEqual(winiso._flat(["a, b", " c ,"]), ["a", "b", "c"])
        self.assertIsNone(winiso._flat([""]))


class TestInfoUnderDryRun(unittest.TestCase):
    """`winiso info` reads the ISO, so it must inspect it even under --dry-run."""

    def test_info_disables_dry_run_before_inspecting(self):
        import argparse
        from tuxutil import system
        seen = []
        try:
            system.DRY_RUN = True
            with patch.object(system, "require_tools"), \
                    patch.object(winiso, "_validate_iso",
                                 side_effect=lambda *a, **k: seen.append(system.DRY_RUN)), \
                    patch.object(winiso, "_get_editions",
                                 side_effect=lambda *a, **k: seen.append(system.DRY_RUN) or []), \
                    patch("sys.stdout", new_callable=io.StringIO):
                rc = winiso.cmd_info(argparse.Namespace(iso="in.iso", json=True))
            self.assertEqual(rc, 0)
            self.assertTrue(system.DRY_RUN)  # restored for any later in-process caller
        finally:
            system.DRY_RUN = False
        self.assertEqual(seen, [False, False])


if __name__ == "__main__":
    unittest.main()
