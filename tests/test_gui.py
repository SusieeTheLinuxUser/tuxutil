"""Unit and integration tests for PySide6 desktop GUI."""
import io
import json
import os
import shlex
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tuxutil import gui

try:
    import PySide6
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication, QLineEdit, QStyle, QStyleOptionGroupBox, QWidget
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

FAKE_CLI_PATH = Path(__file__).parent / "fake_cli.py"


def get_qapp():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication(["tuxutil-test"])
    return app


def pump_events(ms=80):
    """Pump Qt event loop to let asynchronous QProcess and signals resolve."""
    app = get_qapp()
    timer = QTimer()
    timer.setSingleShot(True)
    done = False

    def on_timeout():
        nonlocal done
        done = True

    timer.timeout.connect(on_timeout)
    timer.start(ms)
    while not done:
        app.processEvents()


def wait_until(condition_fn, timeout_ms=15000, step_ms=25):
    """Poll a condition function while pumping Qt events."""
    elapsed = 0
    while elapsed < timeout_ms:
        pump_events(step_ms)
        elapsed += step_ms
        try:
            if condition_fn():
                return True
        except Exception:
            pass
    return False


class TestGuiNoPySide6(unittest.TestCase):
    """Test behavior when PySide6 is missing."""

    def test_pyside6_absent_fallback(self):
        with patch.object(gui, "HAS_PYSIDE6", False):
            stderr_capture = io.StringIO()
            with patch("sys.stderr", stderr_capture):
                exit_code = gui.cmd_gui()
            self.assertEqual(exit_code, 1)
            self.assertIn("PySide6 is required", stderr_capture.getvalue())
            self.assertIn("pip install PySide6", stderr_capture.getvalue())

    def test_mask_command_for_log(self):
        cmd = [
            "python3", "-m", "tuxutil", "winiso", "build", "src.iso",
            "-o", "out.iso", "--unattend-password", "SuperSecret123",
            "--root-password=TopSecret456",
        ]
        masked = gui.mask_command_for_log(cmd)
        self.assertNotIn("SuperSecret123", masked)
        self.assertNotIn("TopSecret456", masked)
        self.assertIn("******", masked)

    def test_signed_link_is_masked_in_the_log(self):
        masked = gui.mask_command_for_log(["tuxutil", "iso", "download", "https://example.test/a.iso?sig=abc&t=1"])
        self.assertNotIn("abc", masked)
        self.assertIn("https://example.test/a.iso", masked)
        masked = gui.mask_command_for_log(["iso", "download", "https://alice:hunter2@example.test:8080/a.iso"])
        self.assertNotIn("hunter2", masked)
        self.assertNotIn("alice", masked)
        self.assertIn("https://example.test:8080/a.iso", masked)
        self.assertNotIn("sig", gui.mask_command_for_log(["https://[bad/a.iso?sig=abc"]))

    def test_format_size(self):
        self.assertEqual(gui.format_size(0), "0 B")
        self.assertEqual(gui.format_size(1024), "1 KB")
        self.assertIn("MB", gui.format_size(1024 * 1024 * 5))
        self.assertIn("GB", gui.format_size(32010928128))


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 not installed; skipping GUI tests")
class TestTuxUtilGui(unittest.TestCase):
    """Test the complete TuxUtil PySide6 desktop GUI offscreen."""

    @classmethod
    def setUpClass(cls):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["TUXUTIL_CLI"] = f"{sys.executable} {FAKE_CLI_PATH}"
        cls.app = get_qapp()

    def setUp(self):
        self.win = gui.TuxUtilWindow()
        self.win.show()
        pump_events(80)

    def tearDown(self):
        if hasattr(self, "win") and self.win:
            with patch.object(gui.QMessageBox, "warning", return_value=gui.QMessageBox.Yes):
                self.win.close()
            pump_events(80)
            self.win = None

    def test_sidebar_and_pages_stay_in_step(self):
        self.win.nav.setCurrentItem(self.win.nav_items[4])
        self.assertEqual(self.win.tabs.currentIndex(), 4)
        self.win.tabs.setCurrentIndex(2)
        self.assertEqual(self.win.nav.currentItem().text(), "Tweaks")

    def test_group_titles_clear_their_contents(self):
        """Inputs must not sit on a card's title. This misses the Breeze overlap the owner hit, which Fusion replaces."""
        for i in range(self.win.tabs.count()):
            self.win.tabs.setCurrentIndex(i)
            pump_events(50)
            for box in self.win.tabs.currentWidget().findChildren(gui.QGroupBox):
                if not box.isVisible():
                    continue
                opt = QStyleOptionGroupBox()
                opt.initFrom(box)
                opt.text = box.title()
                opt.subControls = QStyle.SC_All
                title = box.style().subControlRect(QStyle.CC_GroupBox, opt, QStyle.SC_GroupBoxLabel, box)
                self.assertTrue(title.isValid())
                for child in box.findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
                    if child.isVisibleTo(box):
                        self.assertLess(title.bottom(), child.geometry().top(), box.title())

    def test_log_stays_out_of_the_way_until_a_job_runs(self):
        self.assertFalse(self.win.log_text.isVisible())
        self.assertFalse(self.win.progress_bar.isVisible())
        self.assertFalse(self.win.btn_cancel_job.isVisible())
        self.win.run_cli_job(["apps", "install", "x"], "Job")
        self.assertTrue(self.win.log_text.isVisible())
        self.assertTrue(self.win.progress_bar.isVisible())
        self.assertTrue(self.win.btn_cancel_job.isVisible())
        self.assertTrue(wait_until(lambda: self.win.active_process is None))
        self.assertFalse(self.win.progress_bar.isVisible())
        self.assertFalse(self.win.btn_cancel_job.isVisible())
        self.assertTrue(self.win.log_text.isVisible())

    def test_optional_choice_sends_nothing_until_picked(self):
        form = gui.DynamicFormWidget([
            {"flag": "--compression", "type": "choice", "label": "C", "default": [], "choices": ["gzip", "xz"]},
            {"flag": "--mode", "type": "choice", "label": "M", "default": "b", "choices": ["a", "b"]},
        ])
        self.assertEqual(form.collect_args(), ["--mode", "b"])
        form.fields["--compression"][1].setCurrentText("xz")
        self.assertEqual(form.collect_args(), ["--compression", "xz", "--mode", "b"])

    def test_clicking_an_edition_fills_the_edition_option(self):
        tab = self.win.tab_winiso
        tab.dynamic_form = gui.DynamicFormWidget([{"flag": "--edition", "type": "str", "label": "E"}])
        tab.editions_tree.addTopLevelItem(gui.QTreeWidgetItem(["6", "Windows 11 Pro"]))
        tab.editions_tree.setCurrentItem(tab.editions_tree.topLevelItem(0))
        self.assertEqual(tab.dynamic_form.collect_args(), ["--edition", "6"])

    def test_download_catalog_is_split_by_kind(self):
        self.win.tabs.setCurrentIndex(5)
        tab = self.win.tab_linuxiso
        self.assertTrue(wait_until(lambda: tab.download_combo.count() == 1))
        self.assertEqual(tab.download_combo.itemText(0), "Arch Linux")
        self.assertTrue(tab.download_row.isVisible())

    def test_download_fills_the_source_iso(self):
        self.win.tabs.setCurrentIndex(5)
        tab = self.win.tab_linuxiso
        self.assertTrue(wait_until(lambda: tab.download_combo.count() == 1))
        tab.download_combo.setCurrentIndex(0)
        self.assertEqual(tab.btn_download.text(), "Download")
        tab.on_download_clicked()
        self.assertTrue(wait_until(lambda: tab.iso_path_edit.text() == "/fake/path/archlinux-live.iso"))
        self.assertIn("iso download arch", self.win.log_text.toPlainText())

    def test_page_only_entry_opens_the_browser_and_shows_its_note(self):
        self.win.tabs.setCurrentIndex(4)
        tab = self.win.tab_winiso
        self.assertTrue(wait_until(lambda: tab.download_combo.count() == 1))
        tab.download_combo.setCurrentIndex(0)
        self.assertEqual(tab.btn_download.text(), "Open Download Page")
        self.assertEqual(tab.lbl_download_note.text(), "Links expire.")
        # Without Qt WebEngine the page goes to the system browser.
        with patch.dict(sys.modules, {"PySide6.QtWebEngineWidgets": None}), \
                patch.object(gui.QDesktopServices, "openUrl") as open_url, patch.object(self.win, "run_cli_job") as job:
            tab.on_download_clicked()
        self.assertEqual(open_url.call_args[0][0].toString(), "https://example.test/win")
        job.assert_not_called()

    def test_a_caught_iso_goes_to_the_cli_outside_argv_and_anything_else_does_not(self):
        tab = self.win.tab_winiso
        dialog, item = MagicMock(), MagicMock()
        item.url.return_value = gui.QUrl("https://dl.example.test/Win11.iso?sig=a%2Bb")
        with patch.object(self.win, "run_cli_job") as job, patch.object(gui.QMessageBox, "warning") as warn:
            tab.catch_download(item, "Windows 11", dialog)
            item.cancel.assert_called_once()
            dialog.close.assert_called_once()
            self.assertEqual(job.call_args[0][:2], (["iso", "download"], "Downloading Windows 11"))
            self.assertEqual(job.call_args[1]["env"], {"TUXUTIL_DOWNLOAD_URL": "https://dl.example.test/Win11.iso?sig=a%2Bb"})
            job.reset_mock()
            item.url.return_value = gui.QUrl("https://dl.example.test/setup.exe")
            tab.catch_download(item, "Windows 11", dialog)
        job.assert_not_called()
        warn.assert_called_once()

    def test_a_caught_iso_keeps_the_page_open_while_another_job_runs(self):
        tab = self.win.tab_winiso
        dialog, item = MagicMock(), MagicMock()
        item.url.return_value = gui.QUrl("https://dl.example.test/Win11.iso")
        with patch.object(self.win, "active_process", MagicMock()), patch.object(self.win, "run_cli_job") as job, \
                patch.object(gui.QMessageBox, "warning") as warn:
            tab.catch_download(item, "Windows 11", dialog)
        job.assert_not_called()
        dialog.close.assert_not_called()
        warn.assert_called_once()

    def test_pasted_link_goes_to_the_cli_and_junk_does_not(self):
        tab = self.win.tab_linuxiso
        with patch.object(self.win, "run_cli_job") as job, patch.object(gui.QMessageBox, "warning") as warn:
            tab.download_combo.setEditText("file:///etc/passwd")
            tab.on_download_clicked()
            job.assert_not_called()
            warn.assert_called_once()
            tab.download_combo.setEditText("https://example.test/a.iso?sig=abc")
            tab.on_download_clicked()
        self.assertEqual(job.call_args[0][0], ["iso", "download"])
        self.assertEqual(job.call_args[1]["env"], {"TUXUTIL_DOWNLOAD_URL": "https://example.test/a.iso?sig=abc"})
        self.assertEqual(job.call_args[0][1], "Downloading ISO")

    def test_cancel_says_cancelled_not_failed(self):
        self.win.run_cli_job(["test-long"], "Long job")
        self.assertTrue(wait_until(lambda: self.win.active_process and self.win.active_process.state() == gui.QProcess.Running))
        self.win.on_cancel_active_job()
        self.assertTrue(wait_until(lambda: self.win.active_process is None))
        self.assertEqual(self.win.lbl_job_status.text(), "Long job cancelled")
        self.assertFalse(self.win.progress_bar.isVisible())

    def test_window_properties_and_tabs(self):
        self.assertEqual(self.win.windowTitle(), "TuxUtil")
        self.assertGreaterEqual(self.win.minimumWidth(), 900)
        self.assertGreaterEqual(self.win.minimumHeight(), 600)
        # Cancel button must be disabled when idle
        self.assertFalse(self.win.btn_cancel_job.isEnabled())

        tab_names = [self.win.tabs.tabText(i) for i in range(self.win.tabs.count())]
        expected_tabs = ["Doctor", "Apps", "Tweaks", "Updates", "Windows ISO", "Linux ISO", "Flash USB"]
        self.assertEqual(tab_names, expected_tabs)

    def test_doctor_tab(self):
        self.assertTrue(wait_until(lambda: self.win.lbl_distro_name.text() != "Detecting..."))
        self.assertEqual(self.win.lbl_distro_name.text(), "Arch Linux")
        self.assertEqual(self.win.lbl_distro_family.text(), "arch")
        self.assertEqual(self.win.tools_list.count(), 2)
        self.assertTrue(self.win.btn_install_missing.isEnabled())

    def test_apps_tab(self):
        self.win.tabs.setCurrentIndex(1)
        self.assertTrue(wait_until(lambda: self.win.tab_apps.tree.topLevelItemCount() > 0))

        # Check catalog items
        self.assertGreater(self.win.tab_apps.tree.topLevelItemCount(), 0)
        self.assertGreaterEqual(len(self.win.tab_apps.raw_data), 5)

        # Search filter
        self.win.tab_apps.search_edit.setText("firefox")
        pump_events(50)
        found_firefox = False
        for i in range(self.win.tab_apps.tree.topLevelItemCount()):
            cat = self.win.tab_apps.tree.topLevelItem(i)
            if not cat.isHidden():
                for j in range(cat.childCount()):
                    c = cat.child(j)
                    if not c.isHidden() and "firefox" in c.text(0).lower():
                        found_firefox = True
        self.assertTrue(found_firefox)

        self.win.tab_apps.search_edit.clear()
        pump_events(50)

        # Selecting items
        self.win.tab_apps.set_all_checked(True)
        sel = self.win.tab_apps.get_selected_ids()
        self.assertIn("firefox", sel)
        self.assertIn("vlc", sel)

    def test_tweaks_tab(self):
        self.win.tabs.setCurrentIndex(2)
        self.assertTrue(wait_until(lambda: self.win.tab_tweaks.tree.topLevelItemCount() > 0))

        risks = set()
        for i in range(self.win.tab_tweaks.tree.topLevelItemCount()):
            cat = self.win.tab_tweaks.tree.topLevelItem(i)
            for j in range(cat.childCount()):
                risks.add(cat.child(j).text(1))
        self.assertTrue({"LOW", "MEDIUM", "HIGH"}.issubset(risks))

        self.win.tab_tweaks.set_all_checked(True)
        sel = self.win.tab_tweaks.get_selected_ids()
        self.assertIn("bbr", sel)

    def test_updates_tab(self):
        self.win.tabs.setCurrentIndex(3)
        self.assertTrue(self.win.btn_run_update.isEnabled())
        self.win.btn_run_update.click()
        self.assertTrue(wait_until(lambda: "Update complete" in self.win.log_text.toPlainText()))
        # The log line arrives before the process-finished signal sets the status.
        self.assertTrue(wait_until(lambda: "completed successfully" in self.win.lbl_job_status.text()))

    def test_winiso_tab_form_generation_and_build(self):
        self.win.tabs.setCurrentIndex(4)
        self.assertTrue(wait_until(lambda: self.win.tab_winiso.dynamic_form is not None))
        form = self.win.tab_winiso.dynamic_form

        self.assertIn("--bypass-tpm", form.fields)
        self.assertIn("--unattend-password", form.fields)
        self.assertIn("--drivers", form.fields)

        _, pwd_w = form.fields["--unattend-password"]
        self.assertEqual(pwd_w.echoMode(), QLineEdit.Password)

        self.win.tab_winiso.iso_path_edit.setText("/fake/path/win11.iso")
        self.assertTrue(wait_until(lambda: self.win.tab_winiso.editions_tree.topLevelItemCount() > 0))
        self.assertEqual(self.win.tab_winiso.editions_tree.topLevelItemCount(), 4)

        self.win.tab_winiso.btn_build.click()
        self.assertTrue(wait_until(lambda: "Windows ISO build complete" in self.win.log_text.toPlainText()))
        out_path = self.win.tab_winiso.out_path_edit.text()
        self.assertTrue(wait_until(lambda: self.win.flash_iso_edit.text() == out_path))

    def test_linuxiso_tab_supported_and_unsupported(self):
        self.win.tabs.setCurrentIndex(5)
        self.assertTrue(wait_until(lambda: self.win.tab_linuxiso.dynamic_form is not None))

        # Supported ISO
        self.win.tab_linuxiso.iso_path_edit.setText("/fake/path/archlinux-live.iso")
        self.assertTrue(wait_until(lambda: "Supported" in self.win.tab_linuxiso.lbl_supported.text()))
        self.assertTrue(self.win.tab_linuxiso.btn_build.isEnabled())
        self.assertIn("Arch Linux", self.win.tab_linuxiso.lbl_notes.text())

        # Unsupported ISO: must block Build and show notes
        self.win.tab_linuxiso.iso_path_edit.setText("/fake/path/unsupported-live.iso")
        self.assertTrue(wait_until(lambda: "Unsupported" in self.win.tab_linuxiso.lbl_supported.text()))
        self.assertFalse(self.win.tab_linuxiso.btn_build.isEnabled())
        self.assertIn("Unsupported ISO layout", self.win.tab_linuxiso.lbl_notes.text())

    def test_flash_tab_safety_and_destructive_confirm(self):
        self.win.tabs.setCurrentIndex(6)
        self.assertTrue(wait_until(lambda: len(self.win.raw_devices) > 0))

        # 1. Flash button must be disabled with no ISO chosen
        self.assertFalse(self.win.btn_flash_write.isEnabled())

        # Select safe device /dev/sdb
        sdb_idx = next(i for i, d in enumerate(self.win.raw_devices) if d["path"] == "/dev/sdb")
        self.win.device_combo.setCurrentIndex(sdb_idx)
        pump_events(50)

        # 2. Confirm text must state device model and size
        prompt = self.win.lbl_confirm_prompt.text()
        self.assertIn("SanDisk Ultra", prompt)
        self.assertIn("29.8 GB", prompt)
        self.assertIn("/dev/sdb", prompt)

        # 3. Flash button must remain disabled if exact path typed but no ISO exists
        self.win.confirm_input.setText("/dev/sdb")
        pump_events(20)
        self.assertFalse(self.win.btn_flash_write.isEnabled())

        # 4. Confirm text must reset whenever device selection changes
        sdc_idx = next(i for i, d in enumerate(self.win.raw_devices) if d["path"] == "/dev/sdc")
        self.win.device_combo.setCurrentIndex(sdc_idx)
        pump_events(20)
        self.assertEqual(self.win.confirm_input.text(), "")
        self.assertFalse(self.win.btn_flash_write.isEnabled())

        # 5. Confirm text must reset after Refresh
        self.win.device_combo.setCurrentIndex(sdb_idx)
        self.win.confirm_input.setText("/dev/sdb")
        self.win.load_flash_devices()
        self.assertTrue(wait_until(lambda: self.win.confirm_input.text() == ""))
        self.assertFalse(self.win.btn_flash_write.isEnabled())
        pump_events(200)

        # 6. Unsafe device (/dev/sda) cannot be enabled for flashing
        sda_idx = next(i for i, d in enumerate(self.win.raw_devices) if d["path"] == "/dev/sda")
        self.win.device_combo.setCurrentIndex(sda_idx)
        self.assertTrue(wait_until(lambda: not self.win.confirm_input.isEnabled()))
        self.assertFalse(self.win.btn_flash_write.isEnabled())

        # 7. With safe device + valid ISO + exact path -> Flash button enables
        self.win.device_combo.setCurrentIndex(sdb_idx)
        pump_events(20)
        self.win.flash_iso_edit.setText("/fake/path/live.iso")
        self.win.confirm_input.setText("/dev/sdb")
        pump_events(20)
        self.assertTrue(self.win.btn_flash_write.isEnabled())

        # Test flash execution
        self.win.btn_flash_write.click()
        self.assertTrue(wait_until(lambda: "Flash completed" in self.win.log_text.toPlainText()))

    def test_long_job_progress_and_cancel(self):
        self.win.run_cli_job(["test-long", "0.05"], "Slow Job")
        pump_events(80)
        self.assertTrue(self.win.btn_cancel_job.isEnabled())
        self.assertFalse(self.win.btn_run_update.isEnabled())

        self.win.btn_cancel_job.click()
        self.assertTrue(wait_until(lambda: self.win.active_process is None, timeout_ms=3000))
        self.assertIn("Terminating job", self.win.log_text.toPlainText())
        self.assertTrue(self.win.btn_run_update.isEnabled())
        self.assertFalse(self.win.btn_cancel_job.isEnabled())

    def test_closing_during_job_asks_first(self):
        self.win.run_cli_job(["test-long", "0.05"], "Slow Job")
        pump_events(80)
        with patch.object(gui.QMessageBox, "warning", return_value=gui.QMessageBox.No) as ask:
            self.win.show()
            self.assertFalse(self.win.close())
        ask.assert_called_once()
        self.assertIsNotNone(self.win.active_process)
        self.assertTrue(self.win.isVisible())
        with patch.object(gui.QMessageBox, "warning", return_value=gui.QMessageBox.Yes):
            self.assertTrue(self.win.close())
        self.assertIsNone(self.win.active_process)

    def test_failing_job(self):
        self.win.run_cli_job(["test-fail"], "Failing Job")
        self.assertTrue(wait_until(lambda: "failed with exit code 42" in self.win.lbl_job_status.text()))

    def test_shutdown_during_running_job(self):
        # Verify that closing the window during a running job does not crash or dump core
        self.win.run_cli_job(["test-long", "0.2"], "Background Job")
        pump_events(50)
        with patch.object(gui.QMessageBox, "warning", return_value=gui.QMessageBox.Yes):
            self.win.close()
        pump_events(100)
        self.assertIsNone(self.win.active_process)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 not installed; skipping screenshot generation")
class TestRenderScreenshots(unittest.TestCase):
    """Render all tabs to PNG under .work/gui/ and verify image artifacts."""

    @classmethod
    def setUpClass(cls):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["TUXUTIL_CLI"] = f"{sys.executable} {FAKE_CLI_PATH}"
        cls.app = get_qapp()

        cls.scratch_dirs = [
            Path(__file__).parent.parent / ".work" / "gui",
        ]
        for d in cls.scratch_dirs:
            d.mkdir(parents=True, exist_ok=True)

    def save_screenshot(self, widget, filename):
        for out_dir in self.scratch_dirs:
            target = out_dir / filename
            pixmap = widget.grab()
            saved = pixmap.save(str(target), "PNG")
            self.assertTrue(saved, f"Failed to save screenshot {target}")
            self.assertTrue(target.is_file(), f"File does not exist: {target}")
            self.assertGreater(target.stat().st_size, 1000, f"Screenshot file too small: {target}")

    def test_render_all_views(self):
        win = gui.TuxUtilWindow()
        win.show()
        pump_events(80)

        # 1. Doctor tab
        self.assertTrue(wait_until(lambda: win.lbl_distro_name.text() != "Detecting..."))
        win.tabs.setCurrentIndex(0)
        pump_events(80)
        self.save_screenshot(win, "01_doctor_tab.png")

        # 2. Apps tab
        win.tabs.setCurrentIndex(1)
        self.assertTrue(wait_until(lambda: win.tab_apps.tree.topLevelItemCount() > 0))
        pump_events(80)
        self.save_screenshot(win, "02_apps_tab.png")

        # 3. Tweaks tab
        win.tabs.setCurrentIndex(2)
        self.assertTrue(wait_until(lambda: win.tab_tweaks.tree.topLevelItemCount() > 0))
        pump_events(80)
        self.save_screenshot(win, "03_tweaks_tab.png")

        # 4. Updates tab
        win.tabs.setCurrentIndex(3)
        pump_events(80)
        self.save_screenshot(win, "04_updates_tab.png")

        # 5. Windows ISO tab
        win.tabs.setCurrentIndex(4)
        self.assertTrue(wait_until(lambda: win.tab_winiso.dynamic_form is not None))
        win.tab_winiso.iso_path_edit.setText("/fake/path/win11_23h2.iso")
        self.assertTrue(wait_until(lambda: win.tab_winiso.editions_tree.topLevelItemCount() > 0))
        pump_events(80)
        self.save_screenshot(win, "05_winiso_tab.png")

        # 6. Linux ISO tab
        win.tabs.setCurrentIndex(5)
        self.assertTrue(wait_until(lambda: win.tab_linuxiso.dynamic_form is not None))
        win.tab_linuxiso.iso_path_edit.setText("/fake/path/archlinux-live.iso")
        self.assertTrue(wait_until(lambda: "Supported" in win.tab_linuxiso.lbl_supported.text()))
        pump_events(80)
        self.save_screenshot(win, "06_linuxiso_tab.png")

        # 7. Flash USB tab (clean state: safe device selected, no confirm typed, button disabled, cancel disabled)
        win.tabs.setCurrentIndex(6)
        self.assertTrue(wait_until(lambda: len(win.raw_devices) > 0))
        sdb_idx = next(i for i, d in enumerate(win.raw_devices) if d["path"] == "/dev/sdb")
        win.device_combo.setCurrentIndex(sdb_idx)
        pump_events(80)
        self.save_screenshot(win, "07_flash_tab.png")

        # 8. Running long job view
        win.run_cli_job(["test-long", "0.03"], "Customizing Live Image")
        pump_events(120)
        self.save_screenshot(win, "08_job_running.png")

        # 9. Finished successful job view
        self.assertTrue(wait_until(lambda: win.active_process is None, timeout_ms=5000))
        pump_events(80)
        self.save_screenshot(win, "09_job_success.png")

        # 10. Failed job view
        win.run_cli_job(["test-fail"], "Failing Diagnostic Job")
        self.assertTrue(wait_until(lambda: win.active_process is None, timeout_ms=3000))
        pump_events(80)
        self.save_screenshot(win, "10_job_failed.png")

        win.close()
        pump_events(80)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 not installed")
class ProcessFailureTest(unittest.TestCase):
    def setUp(self):
        get_qapp()
        patcher = patch.dict(os.environ, {"TUXUTIL_CLI": f"{sys.executable} {FAKE_CLI_PATH}"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.win = gui.TuxUtilWindow()
        self.addCleanup(self.win.close)
        wait_until(lambda: not self.win._active_queries)

    def test_program_that_cannot_start_reports_and_recovers(self):
        results = []
        with patch.dict(os.environ, {"TUXUTIL_CLI": "/nonexistent/tuxutil"}):
            self.win.run_cli_query(["doctor"], lambda code, out, err: results.append((code, err)))
            self.assertTrue(wait_until(lambda: results))
            self.assertEqual(results[0][0], -1)
            self.assertTrue(results[0][1])
            self.assertFalse(self.win._active_queries)
            self.win.run_cli_job(["update"], "Update")
            self.assertTrue(wait_until(lambda: self.win.active_process is None))
        self.assertFalse(self.win.btn_cancel_job.isEnabled())
        self.assertIn("failed", self.win.lbl_job_status.text())

    def test_rejected_flash_does_not_mark_a_flash_as_running(self):
        with patch.dict(os.environ, {"TUXUTIL_CLI": f"{sys.executable} -c"}), \
                patch("PySide6.QtWidgets.QMessageBox.warning") as warned:
            self.win.run_cli_job(["import time; time.sleep(30)"], "Other")
            proc = self.win.active_process
            self.win.run_cli_job(["pass"], "Flash", is_flash=True)
            warned.assert_called_once()
            self.assertFalse(self.win.active_job_is_flash)
            proc.kill()
            self.assertTrue(wait_until(lambda: self.win.active_process is None))

    def test_flash_cancel_closes_stdin_and_never_signals(self):
        # This fake writer exits only on stdin EOF, and ignores SIGTERM as a root process effectively does.
        code = "import signal,sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); sys.stdin.read(); sys.exit(143)"
        with patch.dict(os.environ, {"TUXUTIL_CLI": f"{sys.executable} -c"}):
            self.win.run_cli_job([code], "Flash", is_flash=True)
            proc = self.win.active_process
            self.assertTrue(proc.waitForStarted(5000))
            self.assertTrue(self.win.btn_cancel_job.isEnabled())
            with patch.object(proc, "terminate") as terminate, patch.object(proc, "kill") as kill:
                self.win.on_cancel_active_job()
                self.assertFalse(self.win.btn_cancel_job.isEnabled())
                self.assertTrue(wait_until(lambda: self.win.active_process is None))
            terminate.assert_not_called()
            kill.assert_not_called()
        self.assertEqual(self.win.lbl_job_status.text(), "Flash cancelled")

    def test_flash_that_finishes_despite_cancel_reports_success(self):
        with patch.dict(os.environ, {"TUXUTIL_CLI": f"{sys.executable} -c"}):
            self.win.run_cli_job(["import sys; sys.stdin.read()"], "Flash", is_flash=True)
            self.assertTrue(self.win.active_process.waitForStarted(5000))
            self.win.on_cancel_active_job()
            self.assertTrue(wait_until(lambda: self.win.active_process is None))
        self.assertIn("completed successfully", self.win.lbl_job_status.text())

    def test_window_stays_open_during_a_flash(self):
        with patch.dict(os.environ, {"TUXUTIL_CLI": f"{sys.executable} -c"}), \
                patch("PySide6.QtWidgets.QMessageBox.information") as told:
            self.win.run_cli_job(["import time; time.sleep(30)"], "Flash", is_flash=True)
            proc = self.win.active_process
            self.assertTrue(proc.waitForStarted(5000))
            event = MagicMock()
            self.win.closeEvent(event)
            event.ignore.assert_called_once()
            told.assert_called_once()
            self.assertEqual(proc.state(), type(proc).ProcessState.Running)
            proc.kill()
            self.assertTrue(wait_until(lambda: self.win.active_process is None))

    def test_cancel_timer_does_not_kill_the_next_job(self):
        with patch.dict(os.environ, {"TUXUTIL_CLI": f"{sys.executable} -c"}):
            self.win.run_cli_job(["import time; time.sleep(30)"], "First")
            first = self.win.active_process
            self.assertTrue(first.waitForStarted(5000))
            self.win.on_cancel_active_job()
            self.assertTrue(wait_until(lambda: self.win.active_process is None))
            self.win.run_cli_job(["import time; time.sleep(30)"], "Second")
            second = self.win.active_process
            self.assertTrue(second.waitForStarted(5000))
            pump_events(1300)
            self.assertIs(self.win.active_process, second)
            self.assertEqual(second.state(), type(second).ProcessState.Running)
            second.kill()
            self.assertTrue(wait_until(lambda: self.win.active_process is None))

    def test_pasted_link_reaches_the_process_by_environment_only(self):
        code = "import os, sys; print('ARGV', sys.argv[1:], 'ENV', os.environ.get('TUXUTIL_DOWNLOAD_URL'))"
        with patch.dict(os.environ, {"TUXUTIL_CLI": shlex.join([sys.executable, "-c", code])}):
            self.win.run_cli_job(["iso", "download"], "Download", env={"TUXUTIL_DOWNLOAD_URL": "https://x.test/a?sig=1"})
            self.assertTrue(wait_until(lambda: self.win.active_process is None))
        self.assertIn("ARGV ['iso', 'download'] ENV https://x.test/a?sig=1", self.win.log_text.toPlainText())

    def test_password_goes_through_the_environment(self):
        code = "import os, sys; print('ARGV', sys.argv[1:], 'ENV', os.environ.get('TUXUTIL_PASSWORD'))"
        with patch.dict(os.environ, {"TUXUTIL_CLI": shlex.join([sys.executable, "-c", code])}):
            self.win.run_cli_job(["winiso", "build", "--password", "hunter2", "--username", "tux"], "Build")
            self.assertTrue(wait_until(lambda: self.win.active_process is None))
        log = self.win.log_text.toPlainText()
        self.assertIn("ARGV ['winiso', 'build', '--username', 'tux'] ENV hunter2", log)

    def test_stale_linux_info_cannot_enable_build(self):
        tab = self.win.tab_linuxiso
        tab.iso_path_edit.setText("/fake/path/archlinux-live.iso")
        tab.iso_path_edit.setText("/fake/path/unsupported-live.iso")
        self.assertTrue(wait_until(lambda: "Unsupported" in tab.lbl_supported.text()))
        wait_until(lambda: not self.win._active_queries)
        self.assertFalse(tab.btn_build.isEnabled())
        tab.iso_path_edit.setText("/fake/path/archlinux-live.iso")
        self.assertTrue(wait_until(lambda: "Supported" in tab.lbl_supported.text() and "Un" not in tab.lbl_supported.text()))
        self.assertTrue(tab.btn_build.isEnabled())
        tab.iso_path_edit.setText("")
        self.win.set_actions_enabled(True)
        self.assertFalse(tab.btn_build.isEnabled())


class DryRunTest(unittest.TestCase):
    def test_gui_passes_dry_run_to_the_cli(self):
        with patch.dict(os.environ) as env, patch("tuxutil.system.DRY_RUN", True):
            env.pop("TUXUTIL_CLI", None)
            self.assertIn("--dry-run", gui.get_cli_base_cmd())
        with patch.dict(os.environ) as env, patch("tuxutil.system.DRY_RUN", False):
            env.pop("TUXUTIL_CLI", None)
            self.assertNotIn("--dry-run", gui.get_cli_base_cmd())


if __name__ == "__main__":
    unittest.main()
