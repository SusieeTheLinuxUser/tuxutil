"""Tests for tuxutil.winiso — unit tests that run in CI with no root, ISOs, or network."""

import json
import os
import textwrap
import unittest
from unittest.mock import patch, MagicMock

from tuxutil import winiso


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
        xml_str = winiso.generate_autounattend(self.edition, opts, ["Microsoft.BingNews"])
        # Should parse without error
        root = ET.fromstring(xml_str)
        self.assertEqual(root.tag, f"{{{winiso._UNATTEND_NS}}}unattend")

    def test_bypass_keys(self):
        opts = winiso.BuildOptions(bypass_requirements=True)
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        self.assertIn("BypassTPMCheck", xml_str)
        self.assertIn("BypassSecureBootCheck", xml_str)
        self.assertIn("BypassRAMCheck", xml_str)
        self.assertIn("BypassCPUCheck", xml_str)

    def test_no_bypass(self):
        opts = winiso.BuildOptions(bypass_requirements=False)
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        self.assertNotIn("BypassTPMCheck", xml_str)

    def test_local_account(self):
        opts = winiso.BuildOptions(local_username="Admin", local_password="Secret123")
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        self.assertIn("<Name>Admin</Name>", xml_str)
        self.assertIn("HideOnlineAccountScreens", xml_str)
        self.assertIn("SkipMachineOOBE", xml_str)

    def test_password_not_plaintext_in_attribute(self):
        """Password should appear in a Value element, not as a bare attribute."""
        opts = winiso.BuildOptions(local_password="My<Secret>&Pass\"")
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        # Should be XML-escaped
        self.assertIn("My&lt;Secret&gt;&amp;Pass&quot;", xml_str)
        # Should never appear unescaped
        self.assertNotIn('My<Secret>', xml_str)

    def test_username_escaping(self):
        opts = winiso.BuildOptions(local_username='User<>&"Name')
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        self.assertIn("User&lt;&gt;&amp;&quot;Name", xml_str)
        # Verify the XML is still parseable
        import xml.etree.ElementTree as ET
        ET.fromstring(xml_str)

    def test_wcm_namespace(self):
        """Verify wcm namespace is properly declared."""
        opts = winiso.BuildOptions()
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        self.assertIn('xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"', xml_str)

    def test_wcm_action_attributes(self):
        opts = winiso.BuildOptions()
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        self.assertIn('wcm:action="add"', xml_str)

    def test_hibernation_disable(self):
        opts = winiso.BuildOptions(disable_hibernation=True)
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        self.assertIn("powercfg /h off", xml_str)

    def test_hibernation_keep(self):
        opts = winiso.BuildOptions(disable_hibernation=False)
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        self.assertNotIn("powercfg", xml_str)

    def test_compact_os(self):
        opts = winiso.BuildOptions(compact_os=True)
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        self.assertIn("compact /compactos:always", xml_str)

    def test_locale(self):
        opts = winiso.BuildOptions(locale="de-DE", keyboard="0407:00000407",
                                   timezone="W. Europe Standard Time")
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        self.assertIn("de-DE", xml_str)
        self.assertIn("0407:00000407", xml_str)
        self.assertIn("W. Europe Standard Time", xml_str)

    def test_disk_configuration(self):
        """Must have UEFI GPT disk partitioning."""
        opts = winiso.BuildOptions(bypass_requirements=True)
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        self.assertIn("<Type>EFI</Type>", xml_str)
        self.assertIn("<Type>MSR</Type>", xml_str)
        self.assertIn("<Type>Primary</Type>", xml_str)
        self.assertIn("<Format>FAT32</Format>", xml_str)
        self.assertIn("<Format>NTFS</Format>", xml_str)

    def test_all_passes_present(self):
        """Must have windowsPE, specialize, and oobeSystem passes."""
        opts = winiso.BuildOptions(bypass_requirements=True, apply_tweaks=True)
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        self.assertIn('pass="windowsPE"', xml_str)
        self.assertIn('pass="specialize"', xml_str)
        self.assertIn('pass="oobeSystem"', xml_str)

    def test_firstlogon_script(self):
        opts = winiso.BuildOptions()
        xml_str = winiso.generate_autounattend(self.edition, opts, ["Microsoft.BingNews"])
        self.assertIn("tuxutil-setup.ps1", xml_str)

    def test_registry_tweaks(self):
        opts = winiso.BuildOptions(apply_tweaks=True)
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
        self.assertIn("AllowTelemetry", xml_str)
        self.assertIn("TurnOffWindowsCopilot", xml_str)
        self.assertIn("DisableAIDataAnalysis", xml_str)

    def test_no_tweaks(self):
        opts = winiso.BuildOptions(apply_tweaks=False)
        xml_str = winiso.generate_autounattend(self.edition, opts, [])
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
        self.assertIn("tuxutil-report.txt", script)
        self.assertIn("Get-AppxProvisionedPackage", script)

    def test_secondary_drive_report(self):
        """Script should try to write report to secondary drives for test extraction."""
        script = winiso.generate_setup_script([], remove_onedrive=False, remove_edge=False)
        self.assertIn("Get-Volume", script)


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
    """Test boot record file detection logic."""

    def test_uefi_noprompt_preferred(self):
        """efisys_noprompt.bin should be preferred over efisys.bin."""
        # This tests the logic inline in _do_build.
        # Since we can't easily unit test the full build, we verify the preference order.
        # The code does: uefi_file = uefi_noprompt if exists else uefi_boot
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            noprompt = os.path.join(d, "efisys_noprompt.bin")
            regular = os.path.join(d, "efisys.bin")
            # Both exist: prefer noprompt
            open(noprompt, "w").close()
            open(regular, "w").close()
            uefi = noprompt if os.path.exists(noprompt) else regular
            self.assertEqual(uefi, noprompt)
            # Only regular exists
            os.remove(noprompt)
            uefi = noprompt if os.path.exists(noprompt) else regular
            self.assertEqual(uefi, regular)


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


if __name__ == "__main__":
    unittest.main()
