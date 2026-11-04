"""PySide6 desktop GUI for TuxUtil.

A frontend over the CLI: runs `python -m tuxutil ...` via QProcess and reads stdout.
"""
import json
import os
import shlex
import sys
from pathlib import Path

# PySide6 is imported lazily so tuxutil can run headless in CI without GUI dependencies.
try:
    from PySide6.QtCore import QProcess, Qt, Signal
    from PySide6.QtGui import QColor, QFont
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFormLayout,
        QFrame,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QListWidget,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSplitter,
        QTabWidget,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False


def get_cli_base_cmd():
    """Return the base command list for invoking the CLI."""
    override = os.environ.get("TUXUTIL_CLI")
    if override:
        return shlex.split(override)
    from . import system
    return system.self_cmd()


def mask_command_for_log(cmd):
    """Mask password and secret argument values when echoing command to log."""
    masked = []
    skip_next = False
    for i, arg in enumerate(cmd):
        if skip_next:
            skip_next = False
            continue
        arg_str = str(arg)
        lower = arg_str.lower()
        if any(kw in lower for kw in ("password", "secret", "token")):
            if "=" in arg_str:
                flag, _ = arg_str.split("=", 1)
                masked.append(f"{flag}=******")
            elif i + 1 < len(cmd):
                masked.append(arg_str)
                masked.append("******")
                skip_next = True
            else:
                masked.append(arg_str)
        else:
            masked.append(arg_str)
    return shlex.join(masked)


def format_size(size_bytes):
    """Format bytes into human-readable string (e.g. 29.8 GB)."""
    if not size_bytes or size_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(size_bytes)
    for unit in units:
        if size < 1024.0 or unit == "PB":
            return f"{int(size)} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size_bytes} B"


if HAS_PYSIDE6:

    class ErrorBanner(QFrame):
        """Banner shown on a tab when a backend command fails or returns invalid JSON."""

        retry_requested = Signal()

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("errorBanner")
            self.setStyleSheet(
                "#errorBanner { background-color: rgba(220, 53, 69, 0.12); "
                "border: 1px solid rgba(220, 53, 69, 0.4); border-radius: 6px; padding: 8px; }"
            )
            layout = QHBoxLayout(self)
            layout.setContentsMargins(10, 8, 10, 8)

            icon_lbl = QLabel("⚠️")
            icon_lbl.setStyleSheet("font-size: 20px;")
            layout.addWidget(icon_lbl)

            text_layout = QVBoxLayout()
            self.title_lbl = QLabel()
            self.title_lbl.setStyleSheet("font-weight: bold; color: #dc3545;")
            self.msg_lbl = QLabel()
            self.msg_lbl.setWordWrap(True)
            text_layout.addWidget(self.title_lbl)
            text_layout.addWidget(self.msg_lbl)
            layout.addLayout(text_layout, 1)

            self.retry_btn = QPushButton("Retry")
            self.retry_btn.setAccessibleName("Retry request")
            self.retry_btn.clicked.connect(self.retry_requested.emit)
            layout.addWidget(self.retry_btn)
            self.setVisible(False)

        def show_error(self, title, message):
            self.title_lbl.setText(title)
            self.msg_lbl.setText(message)
            self.setVisible(True)

    class DynamicFormWidget(QWidget):
        """Builds a form dynamically from `options --json` schema."""

        def __init__(self, options_schema, parent=None):
            super().__init__(parent)
            self.schema = options_schema or []
            self.fields = {}
            self._init_form()

        def _init_form(self):
            layout = QFormLayout(self)
            layout.setContentsMargins(6, 6, 6, 6)
            layout.setSpacing(10)

            for opt in self.schema:
                flag = opt.get("flag", "")
                otype = opt.get("type", "str")
                label_txt = opt.get("label", flag)
                help_txt = opt.get("help", "")
                default_val = opt.get("default")
                choices = opt.get("choices", [])

                lbl = QLabel(label_txt)
                if help_txt:
                    lbl.setToolTip(help_txt)

                if otype == "bool":
                    w = QCheckBox()
                    w.setAccessibleName(label_txt)
                    w.setChecked(bool(default_val))
                    layout.addRow(lbl, w)
                    self.fields[flag] = ("bool", w)
                elif otype == "choice":
                    w = QComboBox()
                    w.setAccessibleName(label_txt)
                    w.addItems([str(c) for c in choices])
                    if default_val and str(default_val) in choices:
                        w.setCurrentText(str(default_val))
                    layout.addRow(lbl, w)
                    self.fields[flag] = ("choice", w)
                elif otype == "path":
                    box = QWidget()
                    h = QHBoxLayout(box)
                    h.setContentsMargins(0, 0, 0, 0)
                    le = QLineEdit(str(default_val or ""))
                    le.setAccessibleName(label_txt)
                    btn = QPushButton("Browse...")

                    def make_browse(target=le, title=label_txt):
                        def on_browse():
                            fn, _ = QFileDialog.getOpenFileName(self, f"Select {title}")
                            if fn:
                                target.setText(fn)
                        return on_browse

                    btn.clicked.connect(make_browse())
                    h.addWidget(le)
                    h.addWidget(btn)
                    layout.addRow(lbl, box)
                    self.fields[flag] = ("path", le)
                elif otype == "list":
                    box = QFrame()
                    box.setFrameShape(QFrame.StyledPanel)
                    vl = QVBoxLayout(box)
                    vl.setContentsMargins(4, 4, 4, 4)
                    lw = QListWidget()
                    lw.setFixedHeight(75)
                    lw.setAccessibleName(label_txt)
                    if isinstance(default_val, list):
                        for item in default_val:
                            lw.addItem(str(item))

                    row = QHBoxLayout()
                    new_item_le = QLineEdit()
                    new_item_le.setPlaceholderText("New item...")
                    add_btn = QPushButton("Add")
                    del_btn = QPushButton("Remove")

                    def add_entry(inp=new_item_le, lst=lw):
                        t = inp.text().strip()
                        if t:
                            lst.addItem(t)
                            inp.clear()

                    def remove_entry(lst=lw):
                        for it in lst.selectedItems():
                            lst.takeItem(lst.row(it))

                    add_btn.clicked.connect(add_entry)
                    new_item_le.returnPressed.connect(add_entry)
                    del_btn.clicked.connect(remove_entry)
                    row.addWidget(new_item_le)
                    row.addWidget(add_btn)
                    row.addWidget(del_btn)
                    vl.addWidget(lw)
                    vl.addLayout(row)
                    layout.addRow(lbl, box)
                    self.fields[flag] = ("list", lw)
                else:  # "str"
                    le = QLineEdit(str(default_val or ""))
                    le.setAccessibleName(label_txt)
                    if "password" in flag.lower():
                        le.setEchoMode(QLineEdit.Password)
                    layout.addRow(lbl, le)
                    self.fields[flag] = ("str", le)

        def collect_args(self):
            args = []
            for flag, (ftype, widget) in self.fields.items():
                if ftype == "bool":
                    if widget.isChecked():
                        args.append(flag)
                elif ftype == "choice":
                    val = widget.currentText().strip()
                    if val:
                        args.extend([flag, val])
                elif ftype in ("str", "path"):
                    val = widget.text().strip()
                    if val:
                        args.extend([flag, val])
                elif ftype == "list":
                    for i in range(widget.count()):
                        val = widget.item(i).text().strip()
                        if val:
                            args.extend([flag, val])
            return args

    class CatalogTab(QWidget):
        """Shared implementation for Apps and Tweaks tabs."""

        def __init__(self, main_win, catalog_type="apps", parent=None):
            super().__init__(parent)
            self.main_win = main_win
            self.catalog_type = catalog_type  # "apps" or "tweaks"
            self.raw_data = []

            layout = QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 14)
            layout.setSpacing(10)

            self.banner = ErrorBanner()
            self.banner.retry_requested.connect(self.load_catalog)
            layout.addWidget(self.banner)

            # Search bar & category filter
            top_bar = QHBoxLayout()
            self.search_edit = QLineEdit()
            self.search_edit.setPlaceholderText(f"Search {catalog_type}...")
            self.search_edit.setAccessibleName(f"Search {catalog_type}")
            self.search_edit.textChanged.connect(self.filter_tree)
            top_bar.addWidget(self.search_edit, 2)

            self.cat_combo = QComboBox()
            self.cat_combo.setAccessibleName(f"Filter {catalog_type} by Category")
            self.cat_combo.addItem("All Categories")
            self.cat_combo.currentIndexChanged.connect(self.filter_tree)
            top_bar.addWidget(self.cat_combo, 1)

            btn_refresh = QPushButton("Refresh")
            btn_refresh.setAccessibleName(f"Refresh {catalog_type}")
            btn_refresh.clicked.connect(self.load_catalog)
            top_bar.addWidget(btn_refresh)
            layout.addLayout(top_bar)

            # Tree widget
            self.tree = QTreeWidget()
            self.tree.setAccessibleName(f"{catalog_type.capitalize()} Catalog")
            headers = (
                ["Application", "Installed", "Available", "Description"]
                if catalog_type == "apps"
                else ["Tweak", "Risk", "Applied", "Available", "Description"]
            )
            self.tree.setHeaderLabels(headers)
            header = self.tree.header()
            for col in range(len(headers) - 1):
                header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(len(headers) - 1, QHeaderView.Stretch)
            layout.addWidget(self.tree, 1)

            # Action bar
            btn_row = QHBoxLayout()
            if catalog_type == "apps":
                self.btn_act1 = QPushButton("Install Selected")
                self.btn_act1.setObjectName("primaryAction")
                self.btn_act1.clicked.connect(lambda: self.run_batch_action("install"))
                self.btn_act2 = QPushButton("Remove Selected")
                self.btn_act2.setObjectName("dangerAction")
                self.btn_act2.clicked.connect(lambda: self.run_batch_action("remove"))
            else:
                self.btn_act1 = QPushButton("Apply Selected")
                self.btn_act1.setObjectName("primaryAction")
                self.btn_act1.clicked.connect(lambda: self.run_batch_action("apply"))
                self.btn_act2 = QPushButton("Undo Selected")
                self.btn_act2.clicked.connect(lambda: self.run_batch_action("undo"))

            btn_sel_all = QPushButton("Select All")
            btn_sel_all.clicked.connect(lambda: self.set_all_checked(True))
            btn_desel_all = QPushButton("Deselect All")
            btn_desel_all.clicked.connect(lambda: self.set_all_checked(False))

            btn_row.addWidget(self.btn_act1)
            btn_row.addWidget(self.btn_act2)
            btn_row.addSpacing(16)
            btn_row.addWidget(btn_sel_all)
            btn_row.addWidget(btn_desel_all)
            btn_row.addStretch()
            layout.addLayout(btn_row)

        def load_catalog(self):
            self.banner.setVisible(False)

            def on_done(exit_code, stdout, stderr):
                if exit_code != 0:
                    self.banner.show_error(
                        f"{self.catalog_type.capitalize()} Backend Error",
                        f"Command exited with code {exit_code}.\n{stderr or stdout or 'Backend not implemented.'}",
                    )
                    return
                try:
                    data = json.loads(stdout)
                except Exception as e:
                    self.banner.show_error(
                        "Invalid JSON from Backend",
                        f"Failed to parse backend output: {e}\nRaw output: {stdout[:250]}",
                    )
                    return
                self.raw_data = data
                self.populate_tree(data)

            self.main_win.run_cli_query([self.catalog_type, "list", "--json"], on_done)

        def populate_tree(self, items):
            self.tree.clear()
            self.cat_combo.blockSignals(True)
            current_cat = self.cat_combo.currentText()
            self.cat_combo.clear()
            self.cat_combo.addItem("All Categories")

            categories = sorted({it.get("category", "General") for it in items})
            for cat in categories:
                self.cat_combo.addItem(cat)
            if current_cat in categories:
                self.cat_combo.setCurrentText(current_cat)
            self.cat_combo.blockSignals(False)

            cat_nodes = {}
            for cat in categories:
                c_item = QTreeWidgetItem([cat, *[""] * (self.tree.columnCount() - 1)])
                c_item.setFlags(Qt.ItemIsEnabled)
                font = c_item.font(0)
                font.setBold(True)
                c_item.setFont(0, font)
                self.tree.addTopLevelItem(c_item)
                cat_nodes[cat] = c_item

            for it in items:
                cat = it.get("category", "General")
                parent = cat_nodes.get(cat)
                if not parent:
                    continue

                node = QTreeWidgetItem()
                node.setText(0, f"{it.get('name', '')} ({it.get('id', '')})")
                node.setData(0, Qt.UserRole, it)

                available = it.get("available", True)

                if self.catalog_type == "apps":
                    inst = it.get("installed", False)
                    node.setText(1, "Yes" if inst else "No")
                    node.setText(2, "Yes" if available else "No (Distro)")
                    node.setText(3, it.get("description", ""))
                    if inst:
                        node.setForeground(1, QColor("#2e7d32"))
                else:
                    risk = (it.get("risk") or "low").upper()
                    node.setText(1, risk)
                    applied = it.get("applied")
                    node.setText(2, "Yes" if applied is True else ("No" if applied is False else "Unknown"))
                    node.setText(3, "Yes" if available else "No (Unsupported)")
                    node.setText(4, it.get("description", ""))

                    if risk == "HIGH":
                        node.setForeground(1, QColor("#d32f2f"))
                    elif risk == "MEDIUM":
                        node.setForeground(1, QColor("#f57f17"))
                    else:
                        node.setForeground(1, QColor("#2e7d32"))

                    if applied is True:
                        node.setForeground(2, QColor("#2e7d32"))

                if available:
                    node.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                    node.setCheckState(0, Qt.Unchecked)
                else:
                    node.setFlags(Qt.ItemIsUserCheckable)
                    node.setCheckState(0, Qt.Unchecked)
                    node.setForeground(0, QColor("#888888"))

                parent.addChild(node)

            self.tree.expandAll()
            self.filter_tree()

        def filter_tree(self):
            search = self.search_edit.text().strip().lower()
            cat_choice = self.cat_combo.currentText()
            for i in range(self.tree.topLevelItemCount()):
                cat_item = self.tree.topLevelItem(i)
                cat_match = (cat_choice == "All Categories" or cat_choice == cat_item.text(0))
                visible_children = 0
                for j in range(cat_item.childCount()):
                    child = cat_item.child(j)
                    data = child.data(0, Qt.UserRole) or {}
                    text_blob = f"{data.get('name', '')} {data.get('id', '')} {data.get('description', '')}".lower()
                    match = cat_match and (not search or search in text_blob)
                    child.setHidden(not match)
                    if match:
                        visible_children += 1
                cat_item.setHidden(visible_children == 0)

        def set_all_checked(self, checked):
            state = Qt.Checked if checked else Qt.Unchecked
            for i in range(self.tree.topLevelItemCount()):
                cat_item = self.tree.topLevelItem(i)
                for j in range(cat_item.childCount()):
                    child = cat_item.child(j)
                    if not child.isHidden() and (child.flags() & Qt.ItemIsEnabled):
                        child.setCheckState(0, state)

        def get_selected_ids(self):
            ids = []
            for i in range(self.tree.topLevelItemCount()):
                cat_item = self.tree.topLevelItem(i)
                for j in range(cat_item.childCount()):
                    child = cat_item.child(j)
                    if child.checkState(0) == Qt.Checked:
                        d = child.data(0, Qt.UserRole)
                        if d and d.get("id"):
                            ids.append(d["id"])
            return ids

        def run_batch_action(self, action):
            ids = self.get_selected_ids()
            if not ids:
                QMessageBox.information(
                    self,
                    "No Items Selected",
                    f"Please select at least one {self.catalog_type[:-1]} to {action}.",
                )
                return
            self.main_win.run_cli_job(
                [self.catalog_type, action, *ids],
                f"{action.capitalize()}ing {len(ids)} {self.catalog_type}",
                on_finished=self.load_catalog,
            )

    class IsoBuilderTab(QScrollArea):
        """Shared implementation for Windows ISO and Linux ISO customizer tabs."""

        def __init__(self, main_win, flavor="winiso", parent=None):
            super().__init__(parent)
            self.main_win = main_win
            self.flavor = flavor  # "winiso" or "linuxiso"
            self.setWidgetResizable(True)
            self.dynamic_form = None

            content = QWidget()
            layout = QVBoxLayout(content)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(14)

            self.banner = ErrorBanner()
            self.banner.retry_requested.connect(self.on_refresh_info)
            layout.addWidget(self.banner)

            # Source ISO Picker
            iso_title = "Source Windows 11 ISO" if flavor == "winiso" else "Source Linux Live ISO"
            source_group = QGroupBox(iso_title)
            s_layout = QHBoxLayout(source_group)
            self.iso_path_edit = QLineEdit()
            self.iso_path_edit.setPlaceholderText("Select installation ISO image...")
            self.iso_path_edit.setAccessibleName(f"{flavor} Source ISO Path")
            self.iso_path_edit.textChanged.connect(self.on_iso_path_changed)

            btn_browse_iso = QPushButton("Browse...")
            btn_browse_iso.clicked.connect(self.browse_source_iso)
            s_layout.addWidget(self.iso_path_edit)
            s_layout.addWidget(btn_browse_iso)
            layout.addWidget(source_group)

            # Info Group
            if flavor == "winiso":
                self.info_group = QGroupBox("Detected Windows Editions")
                ig_layout = QVBoxLayout(self.info_group)
                self.editions_tree = QTreeWidget()
                self.editions_tree.setHeaderLabels(["Index", "Edition Name", "Architecture", "Build", "Size"])
                self.editions_tree.setFixedHeight(120)
                ig_layout.addWidget(self.editions_tree)
            else:
                self.info_group = QGroupBox("Linux Environment & Compatibility")
                f_layout = QFormLayout(self.info_group)
                self.lbl_family = QLabel("—")
                self.lbl_arch = QLabel("—")
                self.lbl_supported = QLabel("—")
                self.lbl_notes = QLabel("Select an ISO to inspect compatibility.")
                self.lbl_notes.setWordWrap(True)
                f_layout.addRow("Distro Family:", self.lbl_family)
                f_layout.addRow("Architecture:", self.lbl_arch)
                f_layout.addRow("Supported:", self.lbl_supported)
                f_layout.addRow("Notes:", self.lbl_notes)
            layout.addWidget(self.info_group)

            # Customization Options Form
            self.options_group = QGroupBox("Customization Options")
            self.options_layout = QVBoxLayout(self.options_group)
            layout.addWidget(self.options_group)

            # Destination Output ISO
            out_group = QGroupBox("Output ISO Destination")
            o_layout = QHBoxLayout(out_group)
            self.out_path_edit = QLineEdit()
            default_fn = "custom-win11.iso" if flavor == "winiso" else "custom-linux.iso"
            self.out_path_edit.setText(str(Path.home() / default_fn))
            self.out_path_edit.setAccessibleName(f"{flavor} Output ISO Path")
            btn_browse_out = QPushButton("Browse...")
            btn_browse_out.clicked.connect(self.browse_out_iso)
            o_layout.addWidget(self.out_path_edit)
            o_layout.addWidget(btn_browse_out)
            layout.addWidget(out_group)

            # Build Button
            btn_title = "🔨 Build Custom Windows ISO" if flavor == "winiso" else "🔨 Build Custom Linux ISO"
            self.btn_build = QPushButton(btn_title)
            self.btn_build.setObjectName("primaryAction")
            self.btn_build.setFixedHeight(38)
            self.btn_build.setAccessibleName(btn_title)
            if flavor == "linuxiso":
                self.btn_build.setEnabled(False)  # blocked until verified supported
            self.btn_build.clicked.connect(self.on_build_clicked)
            layout.addWidget(self.btn_build)

            layout.addStretch()
            self.setWidget(content)

        def browse_source_iso(self):
            fn, _ = QFileDialog.getOpenFileName(self, "Select ISO Image", "", "ISO Images (*.iso)")
            if fn:
                self.iso_path_edit.setText(fn)

        def browse_out_iso(self):
            fn, _ = QFileDialog.getSaveFileName(self, "Select Destination ISO", self.out_path_edit.text(), "ISO Images (*.iso)")
            if fn:
                self.out_path_edit.setText(fn)

        def load_options(self):
            def on_done(exit_code, stdout, stderr):
                if exit_code != 0:
                    self.banner.show_error(
                        f"{self.flavor} Options Error",
                        f"Failed to load options ({exit_code}):\n{stderr or stdout}",
                    )
                    return
                try:
                    options = json.loads(stdout)
                except Exception as e:
                    self.banner.show_error("Invalid Options JSON", f"{e}\n{stdout[:200]}")
                    return

                if self.dynamic_form:
                    self.options_layout.removeWidget(self.dynamic_form)
                    self.dynamic_form.deleteLater()

                self.dynamic_form = DynamicFormWidget(options)
                self.options_layout.addWidget(self.dynamic_form)

            self.main_win.run_cli_query([self.flavor, "options", "--json"], on_done)

        def on_iso_path_changed(self):
            path = self.iso_path_edit.text().strip()
            if path and (Path(path).is_file() or os.environ.get("TUXUTIL_CLI")):
                self.on_refresh_info()
            elif self.flavor == "linuxiso":
                self.btn_build.setEnabled(False)

        def on_refresh_info(self):
            path = self.iso_path_edit.text().strip()
            if not path:
                return
            self.banner.setVisible(False)

            def on_done(exit_code, stdout, stderr):
                if exit_code != 0:
                    self.banner.show_error(
                        f"{self.flavor} Info Inspection Failed",
                        f"Could not read ISO metadata ({exit_code}):\n{stderr or stdout}",
                    )
                    if self.flavor == "linuxiso":
                        self.btn_build.setEnabled(False)
                    return
                try:
                    data = json.loads(stdout)
                except Exception as e:
                    self.banner.show_error("Invalid ISO Info JSON", f"{e}\n{stdout[:200]}")
                    if self.flavor == "linuxiso":
                        self.btn_build.setEnabled(False)
                    return

                if self.flavor == "winiso":
                    self.editions_tree.clear()
                    for ed in data.get("editions", []):
                        item = QTreeWidgetItem([
                            str(ed.get("index", "")),
                            ed.get("name", "Unknown"),
                            ed.get("arch", ""),
                            ed.get("build", ""),
                            format_size(ed.get("size_bytes", 0)),
                        ])
                        self.editions_tree.addTopLevelItem(item)
                else:
                    self.lbl_family.setText(data.get("family", "Unknown"))
                    self.lbl_arch.setText(data.get("arch", "Unknown"))
                    notes = data.get("notes", "")
                    self.lbl_notes.setText(notes or "No notes provided.")
                    supported = bool(data.get("supported", False))

                    # Hard rule: Show notes and block Build when supported is false
                    if supported:
                        self.lbl_supported.setText("✅ Supported")
                        self.lbl_supported.setStyleSheet("color: #2e7d32; font-weight: bold;")
                        self.lbl_notes.setStyleSheet("color: #2e7d32;")
                        self.btn_build.setEnabled(True)
                    else:
                        self.lbl_supported.setText("❌ Unsupported")
                        self.lbl_supported.setStyleSheet("color: #d32f2f; font-weight: bold;")
                        self.lbl_notes.setStyleSheet("color: #d32f2f; font-weight: bold;")
                        self.btn_build.setEnabled(False)
                        self.banner.show_error("ISO Not Supported", f"This Linux ISO cannot be customized.\nNotes: {notes}")

            self.main_win.run_cli_query([self.flavor, "info", path, "--json"], on_done)

        def on_build_clicked(self):
            iso_path = self.iso_path_edit.text().strip()
            out_path = self.out_path_edit.text().strip()
            if not iso_path:
                QMessageBox.warning(self, "Missing ISO", "Please select a source ISO image.")
                return
            if not out_path:
                QMessageBox.warning(self, "Missing Output Path", "Please specify a destination output path.")
                return

            opts = self.dynamic_form.collect_args() if self.dynamic_form else []
            cmd = [self.flavor, "build", iso_path, "-o", out_path, *opts]

            def on_success():
                self.main_win.last_built_iso = out_path
                self.main_win.flash_iso_edit.setText(out_path)

            self.main_win.run_cli_job(cmd, f"Building {self.flavor.upper()} Image", on_finished=on_success)

    class TuxUtilWindow(QMainWindow):
        """Main application window for TuxUtil."""

        def __init__(self):
            super().__init__()
            self.active_process = None
            self.active_job_is_flash = False
            self.last_built_iso = ""
            self._active_queries = set()
            self._closing = False

            self.setWindowTitle("TuxUtil")
            self.resize(1000, 720)
            self.setMinimumSize(900, 600)

            self._init_ui()
            self._setup_styling()
            self.load_doctor_info()

        def closeEvent(self, event):
            if self.active_process:
                text = "A job is still running. Closing will stop it."
                if self.active_job_is_flash:
                    text += "\n\nStopping a USB flash may leave the drive corrupted or unbootable."
                answer = QMessageBox.warning(self, "Job Running", text + "\n\nClose anyway?",
                                             QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    event.ignore()
                    return
            self._closing = True
            if self.active_process:
                try:
                    self.active_process.readyReadStandardOutput.disconnect()
                    self.active_process.readyReadStandardError.disconnect()
                    self.active_process.finished.disconnect()
                except Exception:
                    pass
                self.active_process.kill()
                self.active_process.waitForFinished(500)
                self.active_process = None

            for q in list(self._active_queries):
                try:
                    q.readyReadStandardOutput.disconnect()
                    q.readyReadStandardError.disconnect()
                    q.finished.disconnect()
                except Exception:
                    pass
                q.kill()
                q.waitForFinished(200)
            self._active_queries.clear()
            super().closeEvent(event)

        def _setup_styling(self):
            pal = self.palette()
            is_dark = pal.window().color().lightness() < 128
            bg_panel = "#2d2d2d" if is_dark else "#f8f9fa"
            border_color = "rgba(255, 255, 255, 0.12)" if is_dark else "rgba(0, 0, 0, 0.15)"
            highlight = pal.highlight().color().name()

            self.setStyleSheet(f"""
                QTabWidget::pane {{ border: 1px solid {border_color}; }}
                QTabBar::tab {{ padding: 8px 16px; font-weight: 500; margin-right: 4px; }}
                QTabBar::tab:selected {{ background: {bg_panel}; border-bottom: 2px solid {highlight}; font-weight: bold; }}
                QGroupBox {{ font-weight: bold; border: 1px solid {border_color}; border-radius: 6px; margin-top: 12px; padding-top: 10px; }}
                QPushButton {{ padding: 6px 14px; border-radius: 4px; font-weight: 500; }}
                QPushButton:disabled {{ background-color: rgba(128, 128, 128, 0.2); color: rgba(128, 128, 128, 0.6); border: 1px solid rgba(128, 128, 128, 0.2); }}
                QPushButton#primaryAction {{ background-color: {highlight}; color: {pal.highlightedText().color().name()}; font-weight: bold; }}
                QPushButton#primaryAction:disabled {{ background-color: rgba(128, 128, 128, 0.2); color: rgba(128, 128, 128, 0.6); }}
                QPushButton#dangerAction {{ background-color: #d32f2f; color: white; font-weight: bold; }}
                QPushButton#dangerAction:disabled {{ background-color: rgba(128, 128, 128, 0.2); color: rgba(128, 128, 128, 0.6); }}
                QTreeWidget, QListWidget, QPlainTextEdit {{ border: 1px solid {border_color}; border-radius: 4px; }}
                QProgressBar {{ border: 1px solid {border_color}; border-radius: 4px; text-align: center; font-weight: bold; }}
                QProgressBar::chunk {{ background-color: {highlight}; border-radius: 3px; }}
            """)

        def _init_ui(self):
            central = QWidget(self)
            self.setCentralWidget(central)
            main_layout = QVBoxLayout(central)
            main_layout.setContentsMargins(12, 12, 12, 12)

            splitter = QSplitter(Qt.Vertical)
            main_layout.addWidget(splitter, 1)

            self.tabs = QTabWidget()
            self.tabs.setAccessibleName("TuxUtil Tabs")
            splitter.addWidget(self.tabs)

            # Tab 1: Doctor
            self.tab_doctor = self._create_doctor_tab()
            # Tab 2: Apps (CatalogTab)
            self.tab_apps = CatalogTab(self, "apps")
            # Tab 3: Tweaks (CatalogTab)
            self.tab_tweaks = CatalogTab(self, "tweaks")
            # Tab 4: Updates
            self.tab_updates = self._create_updates_tab()
            # Tab 5: Windows ISO (IsoBuilderTab)
            self.tab_winiso = IsoBuilderTab(self, "winiso")
            # Tab 6: Linux ISO (IsoBuilderTab)
            self.tab_linuxiso = IsoBuilderTab(self, "linuxiso")
            # Tab 7: Flash USB
            self.tab_flash = self._create_flash_tab()

            self.tabs.addTab(self.tab_doctor, "Doctor")
            self.tabs.addTab(self.tab_apps, "Apps")
            self.tabs.addTab(self.tab_tweaks, "Tweaks")
            self.tabs.addTab(self.tab_updates, "Updates")
            self.tabs.addTab(self.tab_winiso, "Windows ISO")
            self.tabs.addTab(self.tab_linuxiso, "Linux ISO")
            self.tabs.addTab(self.tab_flash, "Flash USB")
            self.tabs.currentChanged.connect(self._on_tab_changed)

            # Execution Panel
            self.exec_panel = self._create_execution_panel()
            splitter.addWidget(self.exec_panel)
            splitter.setStretchFactor(0, 7)
            splitter.setStretchFactor(1, 3)

        # Tab 1: Doctor
        def _create_doctor_tab(self):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            w = QWidget()
            layout = QVBoxLayout(w)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(14)

            self.doctor_banner = ErrorBanner()
            self.doctor_banner.retry_requested.connect(self.load_doctor_info)
            layout.addWidget(self.doctor_banner)

            sys_group = QGroupBox("System Distribution")
            f_layout = QFormLayout(sys_group)
            self.lbl_distro_name = QLabel("Detecting...")
            self.lbl_distro_family = QLabel("Detecting...")
            self.lbl_distro_id = QLabel("Detecting...")
            f_layout.addRow("Distribution:", self.lbl_distro_name)
            f_layout.addRow("Family:", self.lbl_distro_family)
            f_layout.addRow("ID:", self.lbl_distro_id)
            layout.addWidget(sys_group)

            tools_group = QGroupBox("External Tools")
            t_layout = QVBoxLayout(tools_group)
            self.lbl_tools_status = QLabel("Checking tools...")
            t_layout.addWidget(self.lbl_tools_status)
            self.tools_list = QListWidget()
            self.tools_list.setFixedHeight(100)
            t_layout.addWidget(self.tools_list)

            btn_row = QHBoxLayout()
            self.btn_install_missing = QPushButton("Install Missing Tools")
            self.btn_install_missing.setObjectName("primaryAction")
            self.btn_install_missing.setEnabled(False)
            self.btn_install_missing.clicked.connect(lambda: self.run_cli_job(["doctor", "--install"], "Installing Tools", on_finished=self.load_doctor_info))
            self.btn_refresh_doctor = QPushButton("Refresh Status")
            self.btn_refresh_doctor.clicked.connect(self.load_doctor_info)
            btn_row.addWidget(self.btn_install_missing)
            btn_row.addWidget(self.btn_refresh_doctor)
            btn_row.addStretch()
            t_layout.addLayout(btn_row)
            layout.addWidget(tools_group)

            layout.addStretch()
            scroll.setWidget(w)
            return scroll

        def load_doctor_info(self):
            self.doctor_banner.setVisible(False)

            def on_done(exit_code, stdout, stderr):
                if exit_code != 0:
                    self.doctor_banner.show_error(
                        "Doctor Command Failed",
                        f"Command exited with code {exit_code}.\n{stderr or stdout or 'Backend not implemented.'}",
                    )
                    return
                try:
                    data = json.loads(stdout)
                except Exception as e:
                    self.doctor_banner.show_error("Invalid JSON", f"{e}\n{stdout[:200]}")
                    return

                self.lbl_distro_name.setText(data.get("name", "Unknown"))
                self.lbl_distro_family.setText(data.get("family", "Unknown"))
                self.lbl_distro_id.setText(data.get("id", "Unknown"))

                missing = data.get("missing", [])
                self.tools_list.clear()
                if not missing:
                    self.lbl_tools_status.setText("✅ All required external tools are installed and ready!")
                    self.btn_install_missing.setEnabled(False)
                    self.tools_list.setVisible(False)
                else:
                    self.lbl_tools_status.setText(f"⚠️ {len(missing)} external tools are missing:")
                    for t in missing:
                        self.tools_list.addItem(t)
                    self.tools_list.setVisible(True)
                    self.btn_install_missing.setEnabled(True)

            self.run_cli_query(["doctor", "--json"], on_done)

        # Tab 4: Updates
        def _create_updates_tab(self):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            w = QWidget()
            layout = QVBoxLayout(w)
            layout.setContentsMargins(20, 20, 20, 20)
            layout.setSpacing(16)

            card = QGroupBox("System & Flatpak Upgrades")
            c_layout = QVBoxLayout(card)
            c_layout.setContentsMargins(16, 16, 16, 16)
            c_layout.setSpacing(12)

            info = QLabel(
                "Keep your distribution packages and installed Flatpaks up to date with a single click.\n\n"
                "• Synchronizes repositories and updates system packages\n"
                "• Upgrades all Flatpak runtimes and application packages\n"
                "• Live progress and terminal output are displayed below."
            )
            info.setWordWrap(True)
            c_layout.addWidget(info)

            self.btn_run_update = QPushButton("🚀 Run System & Flatpak Update")
            self.btn_run_update.setObjectName("primaryAction")
            self.btn_run_update.setFixedHeight(40)
            self.btn_run_update.clicked.connect(lambda: self.run_cli_job(["update"], "System and Flatpak Update"))
            c_layout.addWidget(self.btn_run_update)

            layout.addWidget(card)
            layout.addStretch()
            scroll.setWidget(w)
            return scroll

        # Tab 7: Flash USB
        def _create_flash_tab(self):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            w = QWidget()
            layout = QVBoxLayout(w)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(14)

            self.flash_banner = ErrorBanner()
            self.flash_banner.retry_requested.connect(self.load_flash_devices)
            layout.addWidget(self.flash_banner)

            # ISO input
            iso_group = QGroupBox("Image to Flash")
            i_layout = QHBoxLayout(iso_group)
            self.flash_iso_edit = QLineEdit()
            self.flash_iso_edit.setPlaceholderText("Select an ISO or disk image to write to USB...")
            self.flash_iso_edit.setAccessibleName("Flash Source ISO Path")
            self.flash_iso_edit.textChanged.connect(self.validate_flash_confirm)
            btn_browse = QPushButton("Browse...")
            btn_browse.clicked.connect(self._browse_flash_iso)
            i_layout.addWidget(self.flash_iso_edit)
            i_layout.addWidget(btn_browse)
            layout.addWidget(iso_group)

            # Target Device
            dev_group = QGroupBox("Target USB Drive")
            d_layout = QVBoxLayout(dev_group)
            row = QHBoxLayout()
            self.device_combo = QComboBox()
            self.device_combo.setAccessibleName("Target USB Device Selector")
            self.device_combo.currentIndexChanged.connect(self.on_device_selection_changed)
            row.addWidget(self.device_combo, 1)

            btn_refresh = QPushButton("Refresh Devices")
            btn_refresh.clicked.connect(self.load_flash_devices)
            row.addWidget(btn_refresh)
            d_layout.addLayout(row)

            self.lbl_selected_device = QLabel("No device selected.")
            self.lbl_selected_device.setWordWrap(True)
            d_layout.addWidget(self.lbl_selected_device)
            layout.addWidget(dev_group)

            # Destructive Confirmation Box
            self.confirm_group = QGroupBox("⚠️ Destructive Confirmation Required")
            self.confirm_group.setStyleSheet(
                "QGroupBox { border: 2px solid #d32f2f; background: rgba(211, 47, 47, 0.05); }"
            )
            c_layout = QVBoxLayout(self.confirm_group)
            c_layout.setSpacing(10)

            warn_lbl = QLabel("WARNING: All partitions and existing data on the target drive will be PERMANENTLY ERASED!")
            warn_lbl.setStyleSheet("font-weight: bold; color: #d32f2f;")
            c_layout.addWidget(warn_lbl)

            self.lbl_confirm_prompt = QLabel("Select a safe USB drive to proceed.")
            self.lbl_confirm_prompt.setWordWrap(True)
            c_layout.addWidget(self.lbl_confirm_prompt)

            self.confirm_input = QLineEdit()
            self.confirm_input.setPlaceholderText("Type exact device path here (e.g. /dev/sdb)...")
            self.confirm_input.setAccessibleName("Confirm Device Path Input")
            self.confirm_input.textChanged.connect(self.validate_flash_confirm)
            c_layout.addWidget(self.confirm_input)

            self.btn_flash_write = QPushButton("⚡ Flash USB Device")
            self.btn_flash_write.setObjectName("dangerAction")
            self.btn_flash_write.setFixedHeight(40)
            self.btn_flash_write.setAccessibleName("Write Image to USB Device")
            self.btn_flash_write.setEnabled(False)  # Must stay disabled until ISO, safe dev, and exact path match
            self.btn_flash_write.clicked.connect(self.on_flash_write)
            c_layout.addWidget(self.btn_flash_write)

            layout.addWidget(self.confirm_group)
            layout.addStretch()
            scroll.setWidget(w)

            self.raw_devices = []
            return scroll

        def _browse_flash_iso(self):
            fn, _ = QFileDialog.getOpenFileName(self, "Select Image to Flash", "", "Disk Images (*.iso *.img);;All Files (*)")
            if fn:
                self.flash_iso_edit.setText(fn)

        def load_flash_devices(self):
            self.flash_banner.setVisible(False)
            self.confirm_input.clear()

            def on_done(exit_code, stdout, stderr):
                if exit_code != 0:
                    self.flash_banner.show_error(
                        "Flash Device Enumeration Failed",
                        f"Command exited with code {exit_code}:\n{stderr or stdout or 'Backend not implemented.'}",
                    )
                    return
                try:
                    devices = json.loads(stdout)
                except Exception as e:
                    self.flash_banner.show_error("Invalid Device List JSON", f"{e}\n{stdout[:200]}")
                    return

                self.raw_devices = devices
                self.device_combo.blockSignals(True)
                self.device_combo.clear()

                first_safe = -1
                for idx, dev in enumerate(devices):
                    path = dev.get("path", "")
                    model = dev.get("model", "Drive")
                    size = format_size(dev.get("size_bytes", 0))
                    safe = dev.get("safe", False)
                    reason = dev.get("reason", "")

                    if safe:
                        self.device_combo.addItem(f"{model} — {path} ({size})", dev)
                        if first_safe == -1:
                            first_safe = idx
                    else:
                        self.device_combo.addItem(f"⛔ [UNSAFE: {reason or 'System Drive'}] {model} — {path} ({size})", dev)
                        # Mark unsafe items disabled in view
                        m_idx = self.device_combo.model().index(idx, 0)
                        self.device_combo.model().setData(m_idx, 0, Qt.UserRole - 1)

                self.device_combo.blockSignals(False)
                if first_safe != -1:
                    self.device_combo.setCurrentIndex(first_safe)
                self.on_device_selection_changed()

            self.run_cli_query(["flash", "list", "--json"], on_done)

        def on_device_selection_changed(self):
            # Hard rule: confirm text has to reset whenever device selection changes
            self.confirm_input.clear()
            idx = self.device_combo.currentIndex()
            if idx < 0 or idx >= len(self.raw_devices):
                self.lbl_selected_device.setText("No device selected.")
                self.lbl_confirm_prompt.setText("Select a safe drive to proceed.")
                self.confirm_input.setEnabled(False)
                self.btn_flash_write.setEnabled(False)
                return

            dev = self.raw_devices[idx]
            safe = dev.get("safe", False)
            path = dev.get("path", "")
            model = dev.get("model", "Drive")
            size = format_size(dev.get("size_bytes", 0))

            if not safe:
                reason = dev.get("reason", "Drive is protected or unsafe")
                self.lbl_selected_device.setText(f"⛔ Cannot select {path}: {reason}\nFlashing to this device is blocked for machine safety.")
                self.lbl_confirm_prompt.setText("Device is unsafe. Selection disabled.")
                self.confirm_input.setEnabled(False)
                self.btn_flash_write.setEnabled(False)
            else:
                self.lbl_selected_device.setText(f"Selected Target: {model} | Path: {path} | Size: {size}")
                # Hard rule: The confirm text must state the device model and size
                self.lbl_confirm_prompt.setText(f"To proceed, type the device path exactly ({path}) to confirm flashing {model} ({size}):")
                self.confirm_input.setEnabled(True)
                self.validate_flash_confirm()

        def validate_flash_confirm(self):
            iso_path = self.flash_iso_edit.text().strip()
            # Hard rule: ISO must exist on disk (or in fake CLI test mode)
            iso_valid = bool(iso_path) and (
                Path(iso_path).is_file()
                or (os.environ.get("TUXUTIL_CLI") and iso_path.endswith((".iso", ".img")))
            )

            idx = self.device_combo.currentIndex()
            if idx < 0 or idx >= len(self.raw_devices):
                self.btn_flash_write.setEnabled(False)
                return

            dev = self.raw_devices[idx]
            if not dev.get("safe", False):
                self.btn_flash_write.setEnabled(False)
                return

            expected_path = dev.get("path", "").strip()
            typed = self.confirm_input.text().strip()

            # Hard rule: Flash button MUST stay disabled until an ISO exists on disk,
            # a safe device is selected, and the exact path is typed.
            can_flash = bool(iso_valid and typed and (typed == expected_path))
            self.btn_flash_write.setEnabled(can_flash)

        def on_flash_write(self):
            idx = self.device_combo.currentIndex()
            if idx < 0 or idx >= len(self.raw_devices):
                return
            dev = self.raw_devices[idx]
            if not dev.get("safe", False):
                QMessageBox.critical(self, "Unsafe Device", "Cannot flash an unsafe drive.")
                return

            iso_path = self.flash_iso_edit.text().strip()
            device_path = dev.get("path", "").strip()
            typed = self.confirm_input.text().strip()
            if typed != device_path:
                QMessageBox.critical(self, "Confirmation Mismatch", f"Confirmation must exactly match {device_path}.")
                return

            # Hard rule: Passing `--confirm` is only ever done after that typed confirmation.
            cmd = ["flash", "write", iso_path, device_path, "--confirm", device_path]
            self.active_job_is_flash = True
            self.run_cli_job(
                cmd,
                f"Flashing {dev.get('model', 'Drive')} ({device_path})",
                is_flash=True,
                on_finished=self.load_flash_devices,
            )

        # Bottom Execution & Live Output Panel
        def _create_execution_panel(self):
            panel = QFrame()
            panel.setFrameShape(QFrame.StyledPanel)
            layout = QVBoxLayout(panel)
            layout.setContentsMargins(10, 8, 10, 8)
            layout.setSpacing(6)

            h_row = QHBoxLayout()
            self.lbl_job_status = QLabel("Ready")
            self.lbl_job_status.setStyleSheet("font-weight: bold;")
            self.lbl_job_status.setAccessibleName("Current Job Status")
            h_row.addWidget(self.lbl_job_status, 1)

            self.btn_cancel_job = QPushButton("Cancel Job")
            self.btn_cancel_job.setObjectName("dangerAction")
            self.btn_cancel_job.setAccessibleName("Cancel Active Job")
            # Hard rule: disabled when no job runs
            self.btn_cancel_job.setEnabled(False)
            self.btn_cancel_job.clicked.connect(self.on_cancel_active_job)
            h_row.addWidget(self.btn_cancel_job)

            self.btn_toggle_log = QPushButton("Hide Log")
            self.btn_toggle_log.clicked.connect(self._toggle_log)
            h_row.addWidget(self.btn_toggle_log)

            btn_clear = QPushButton("Clear Log")
            btn_clear.clicked.connect(lambda: self.log_text.clear())
            h_row.addWidget(btn_clear)
            layout.addLayout(h_row)

            self.progress_bar = QProgressBar()
            self.progress_bar.setAccessibleName("Job Progress Bar")
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("%p% - Ready")
            layout.addWidget(self.progress_bar)

            self.log_text = QPlainTextEdit()
            self.log_text.setReadOnly(True)
            self.log_text.setAccessibleName("Job Output Log Pane")
            font = QFont("monospace")
            font.setStyleHint(QFont.Monospace)
            self.log_text.setFont(font)
            self.log_text.setMaximumBlockCount(3000)
            layout.addWidget(self.log_text)

            return panel

        def _toggle_log(self):
            vis = not self.log_text.isVisible()
            self.log_text.setVisible(vis)
            self.btn_toggle_log.setText("Hide Log" if vis else "Show Log")

        def _on_tab_changed(self, index):
            tab_text = self.tabs.tabText(index)
            if tab_text == "Apps" and not self.tab_apps.raw_data:
                self.tab_apps.load_catalog()
            elif tab_text == "Tweaks" and not self.tab_tweaks.raw_data:
                self.tab_tweaks.load_catalog()
            elif tab_text == "Windows ISO" and not self.tab_winiso.dynamic_form:
                self.tab_winiso.load_options()
            elif tab_text == "Linux ISO" and not self.tab_linuxiso.dynamic_form:
                self.tab_linuxiso.load_options()
            elif tab_text == "Flash USB" and not self.raw_devices:
                self.load_flash_devices()

        # Process Execution Helpers
        def run_cli_query(self, args, on_finished_cb):
            """Execute a query command asynchronously via QProcess."""
            base_cmd = get_cli_base_cmd()
            full_cmd = base_cmd + args

            proc = QProcess(self)
            self._active_queries.add(proc)
            stdout_buf, stderr_buf = [], []

            def on_out():
                stdout_buf.append(proc.readAllStandardOutput().data().decode("utf-8", errors="replace"))

            def on_err():
                stderr_buf.append(proc.readAllStandardError().data().decode("utf-8", errors="replace"))

            def on_finish(exit_code, exit_status):
                if proc in self._active_queries:
                    self._active_queries.remove(proc)
                if self._closing:
                    proc.deleteLater()
                    return
                out = "".join(stdout_buf)
                err = "".join(stderr_buf)
                try:
                    on_finished_cb(exit_code, out, err)
                except Exception:
                    pass
                proc.deleteLater()

            proc.readyReadStandardOutput.connect(on_out)
            proc.readyReadStandardError.connect(on_err)
            proc.finished.connect(on_finish)
            proc.start(full_cmd[0], full_cmd[1:])

        def run_cli_job(self, args, job_title="Job", is_flash=False, on_finished=None):
            """Run a long job via QProcess, streaming log output and parsing @progress lines."""
            if self.active_process is not None:
                QMessageBox.warning(self, "Job In Progress", "A job is already running. Please wait or cancel it first.")
                return

            self.active_job_is_flash = is_flash
            base_cmd = get_cli_base_cmd()
            full_cmd = base_cmd + args

            masked_str = mask_command_for_log(full_cmd)
            self.log_text.appendPlainText(f"\n$ {masked_str}")

            self.lbl_job_status.setText(f"Running: {job_title}...")
            self.lbl_job_status.setStyleSheet("font-weight: bold; color: #0288d1;")
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat(f"0% - Starting {job_title}...")
            self.btn_cancel_job.setEnabled(True)
            self.set_actions_enabled(False)

            proc = QProcess(self)
            self.active_process = proc
            line_buf = ""

            def handle_stream(text):
                nonlocal line_buf
                line_buf += text
                while "\n" in line_buf:
                    line, line_buf = line_buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if line.startswith("@progress "):
                        parts = line[len("@progress "):].strip().split(" ", 1)
                        try:
                            pct = int(parts[0])
                            msg = parts[1] if len(parts) > 1 else ""
                            self.progress_bar.setValue(pct)
                            self.progress_bar.setFormat(f"{pct}% - {msg}")
                        except ValueError:
                            pass
                    else:
                        self.log_text.appendPlainText(line)

            def on_out():
                handle_stream(proc.readAllStandardOutput().data().decode("utf-8", errors="replace"))

            def on_err():
                handle_stream(proc.readAllStandardError().data().decode("utf-8", errors="replace"))

            def on_finish(exit_code, exit_status):
                if line_buf.strip() and not line_buf.startswith("@progress "):
                    self.log_text.appendPlainText(line_buf)

                self.active_process = None
                self.btn_cancel_job.setEnabled(False)
                self.set_actions_enabled(True)

                if exit_code == 0:
                    self.lbl_job_status.setText(f"✅ {job_title} completed successfully (exit code 0)")
                    self.lbl_job_status.setStyleSheet("font-weight: bold; color: #2e7d32;")
                    self.progress_bar.setValue(100)
                    self.progress_bar.setFormat("100% - Completed successfully")
                    if on_finished:
                        on_finished()
                else:
                    self.lbl_job_status.setText(f"❌ {job_title} failed with exit code {exit_code}")
                    self.lbl_job_status.setStyleSheet("font-weight: bold; color: #d32f2f;")
                    self.progress_bar.setFormat(f"Failed (exit code {exit_code})")

                proc.deleteLater()

            proc.readyReadStandardOutput.connect(on_out)
            proc.readyReadStandardError.connect(on_err)
            proc.finished.connect(on_finish)
            proc.start(full_cmd[0], full_cmd[1:])

        def on_cancel_active_job(self):
            if not self.active_process:
                return

            if self.active_job_is_flash:
                resp = QMessageBox.warning(
                    self,
                    "Cancel USB Flash",
                    "Cancelling a USB flash in progress may leave the drive corrupted, unpartitioned, or unbootable.\n\n"
                    "Are you sure you want to abort the flash operation?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if resp != QMessageBox.Yes:
                    return

            self.log_text.appendPlainText("\n⚠️ Terminating job at user request...")
            self.lbl_job_status.setText("Terminating job...")
            self.lbl_job_status.setStyleSheet("font-weight: bold; color: #e65100;")
            self.active_process.terminate()

            from PySide6.QtCore import QTimer
            QTimer.singleShot(1000, lambda: self.active_process and self.active_process.kill())

        def set_actions_enabled(self, enabled):
            """Enable or disable trigger buttons during long job runs."""
            self.btn_install_missing.setEnabled(enabled and (self.tools_list.count() > 0))
            self.tab_apps.btn_act1.setEnabled(enabled)
            self.tab_apps.btn_act2.setEnabled(enabled)
            self.tab_tweaks.btn_act1.setEnabled(enabled)
            self.tab_tweaks.btn_act2.setEnabled(enabled)
            self.btn_run_update.setEnabled(enabled)
            self.tab_winiso.btn_build.setEnabled(enabled)
            self.tab_linuxiso.btn_build.setEnabled(enabled and ("✅" in self.tab_linuxiso.lbl_supported.text()))
            self.validate_flash_confirm()


def register(sub):
    """Register the gui subcommand in tuxutil CLI."""
    p = sub.add_parser("gui", help="launch the PySide6 desktop GUI")
    p.set_defaults(func=cmd_gui)


def cmd_gui(args=None):
    """Launch the GUI desktop application."""
    if not HAS_PYSIDE6:
        print(
            "PySide6 is required to run the TuxUtil GUI.\n"
            "Install it with: pip install PySide6 (or via your system package manager)",
            file=sys.stderr,
        )
        return 1

    app = QApplication.instance() or QApplication(sys.argv[:1])
    win = TuxUtilWindow()
    win.show()
    return app.exec()
