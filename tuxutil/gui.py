"""PySide6 desktop GUI for TuxUtil.

A frontend over the CLI: runs tuxutil's CLI via QProcess and reads stdout.
"""
import json
import os
import shlex
import sys
import urllib.parse
from pathlib import Path

# PySide6 is imported lazily so tuxutil can run headless in CI without GUI dependencies.
try:
    from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTemporaryDir, QTimer, QUrl, Signal
    from PySide6.QtGui import QColor, QDesktopServices, QFont, QPalette
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QFormLayout,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
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
    # GUI actions run as new CLI processes, so `tuxutil --dry-run gui` must pass the flag on.
    return [*system.self_cmd(), *(["--dry-run"] if system.DRY_RUN else [])]


def split_password(args):
    """Take --password's value out of argv, where other processes can read it in /proc."""
    if "--password" not in args:
        return args, ""
    i = args.index("--password")
    return args[:i] + args[i + 2:], args[i + 1]


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
        if lower.startswith(("http://", "https://")):
            # A signed link's query string is its password, and a link can also carry user:password@.
            try:
                url = urllib.parse.urlsplit(arg_str)
            except ValueError:
                masked.append("******")
                continue
            masked.append(url._replace(
                netloc=url.netloc.rsplit("@", 1)[-1], query="******" if url.query else "", fragment="",
            ).geturl())
            continue
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


THEMES = {
    "dark": {
        "bg": "#16181d", "surface": "#1e2127", "raised": "#2a2e37", "field": "#121419", "border": "#363b46",
        "text": "#e6e8ec", "dim": "#9aa1ad", "accent": "#3f74e0", "accent_hover": "#5587ea", "on_accent": "#ffffff",
        "ok": "#5fd38d", "warn": "#f2b84b", "err": "#ff7b7b", "danger": "#c93c3c", "danger_hover": "#dc5050",
        "danger_tint": "#2b1c20",
    },
    "light": {
        "bg": "#f3f4f6", "surface": "#ffffff", "raised": "#eceef1", "field": "#ffffff", "border": "#d0d4db",
        "text": "#1c1f24", "dim": "#5d6470", "accent": "#2f6fdb", "accent_hover": "#245cc0", "on_accent": "#ffffff",
        "ok": "#1a7f3c", "warn": "#8f5b00", "err": "#c62828", "danger": "#c62828", "danger_hover": "#a92020",
        "danger_tint": "#fdf0f0",
    },
}
C = dict(THEMES["dark"])  # The active theme's colors. apply_theme fills this in.

STYLESHEET = """
QWidget {{ color: {text}; }}
QToolTip {{ background: {raised}; color: {text}; border: 1px solid {border}; padding: 4px; }}
QFrame#sidebar {{ background: {surface}; border-right: 1px solid {border}; }}
QLabel#brand {{ font-size: 16pt; font-weight: 700; padding: 16px 16px 8px 16px; }}
QLabel#sidebarFoot {{ color: {dim}; padding: 8px 16px 14px 16px; }}
QListWidget#nav {{ background: transparent; border: none; outline: 0; }}
QListWidget#nav::item {{ padding: 7px 10px; margin: 1px 8px; border-radius: 6px; }}
QListWidget#nav::item:disabled {{ color: {dim}; padding-top: 14px; }}
QListWidget#nav::item:hover:enabled {{ background: {raised}; }}
QListWidget#nav::item:selected {{ background: {accent}; color: {on_accent}; font-weight: 600; }}
QTabWidget::pane {{ border: none; }}
QScrollArea {{ background: transparent; border: none; }}
QLabel#pageTitle {{ font-size: 16pt; font-weight: 700; }}
QLabel#dim, QLabel#hint {{ color: {dim}; }}
QLabel#hint {{ font-size: 9pt; }}
QLabel#hintDanger {{ color: {err}; font-size: 9pt; font-weight: 600; }}
QGroupBox {{ background: {surface}; border: 1px solid {border}; border-radius: 8px; font-weight: 600;
             margin-top: 26px; padding: 10px 6px 6px 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 2px; top: 2px; }}
QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QLineEdit, QGroupBox QComboBox, QGroupBox QPushButton,
QGroupBox QListWidget, QGroupBox QTreeWidget {{ font-weight: normal; }}
QGroupBox#dangerCard {{ background: {danger_tint}; border-color: {danger}; }}
QGroupBox#dangerCard::title {{ color: {err}; }}
QFrame#errorBanner {{ background: {danger_tint}; border: 1px solid {danger}; border-radius: 8px; }}
QFrame#footer, QFrame#jobPanel {{ background: {surface}; border: none; border-top: 1px solid {border}; }}
QPushButton {{ background: {raised}; border: 1px solid {border}; border-radius: 6px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: {accent_hover}; }}
QPushButton:pressed {{ background: {border}; }}
QPushButton:disabled {{ background: transparent; color: {dim}; border-color: {border}; }}
QPushButton#primaryAction {{ background: {accent}; border-color: {accent}; color: {on_accent}; font-weight: 600; }}
QPushButton#primaryAction:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
QPushButton#dangerAction {{ background: {danger}; border-color: {danger}; color: #ffffff; font-weight: 600; }}
QPushButton#dangerAction:hover {{ background: {danger_hover}; border-color: {danger_hover}; }}
QPushButton#primaryAction:disabled, QPushButton#dangerAction:disabled {{
    background: transparent; color: {dim}; border-color: {border}; font-weight: normal; }}
QLineEdit, QComboBox {{ background: {field}; border: 1px solid {border}; border-radius: 6px; padding: 5px 8px; }}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {accent}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {dim}; background: transparent; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url("{arrow}"); width: 12px; height: 12px; margin-right: 8px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator, QTreeWidget::indicator {{ width: 14px; height: 14px; border: 1px solid {dim}; border-radius: 4px; background: {field}; }}
QCheckBox::indicator:checked, QTreeWidget::indicator:checked {{ image: url("{check}"); background: {accent}; border-color: {accent}; }}
QCheckBox::indicator:disabled, QTreeWidget::indicator:disabled {{ background: transparent; border-color: {border}; }}
QComboBox QAbstractItemView {{ background: {raised}; border: 1px solid {border}; outline: 0;
                               selection-background-color: {accent}; selection-color: {on_accent}; }}
QTreeWidget, QListWidget, QPlainTextEdit {{ background: {field}; border: 1px solid {border}; border-radius: 6px; outline: 0; }}
QTreeWidget::item {{ padding: 3px 0; }}
QTreeWidget::item:selected, QListWidget::item:selected {{ background: {accent}; color: {on_accent}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {surface}; color: {dim}; border: none; border-bottom: 1px solid {border};
                        padding: 6px 8px; font-weight: 600; }}
QProgressBar {{ background: {field}; border: 1px solid {border}; border-radius: 6px; text-align: center; min-height: 20px; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 5px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {border}; border-radius: 3px; }}
QScrollBar::handle:vertical {{ min-height: 28px; }}
QScrollBar::handle:horizontal {{ min-width: 28px; }}
QScrollBar::handle:hover {{ background: {dim}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent; border: none; width: 0; height: 0; }}
QSplitter::handle {{ background: {border}; }}
"""

NAV = [("SYSTEM", ["Doctor", "Apps", "Tweaks", "Updates"]), ("IMAGES", ["Windows ISO", "Linux ISO", "Flash USB"])]


if HAS_PYSIDE6:

    _icon_dir = None

    def apply_theme(app):
        """Pick dark or light from the desktop, then draw everything ourselves so every desktop theme looks the same."""
        C.update(THEMES["dark" if app.palette().window().color().lightness() < 128 else "light"])
        app.setStyle("Fusion")
        pal = QPalette()
        for role, key in (
            (QPalette.Window, "bg"), (QPalette.WindowText, "text"), (QPalette.Base, "field"),
            (QPalette.AlternateBase, "surface"), (QPalette.Text, "text"), (QPalette.Button, "raised"),
            (QPalette.ButtonText, "text"), (QPalette.ToolTipBase, "raised"), (QPalette.ToolTipText, "text"),
            (QPalette.Highlight, "accent"), (QPalette.HighlightedText, "on_accent"),
            (QPalette.PlaceholderText, "dim"), (QPalette.Link, "accent_hover"),
        ):
            pal.setColor(role, QColor(C[key]))
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
            pal.setColor(QPalette.Disabled, role, QColor(C["dim"]))
        app.setPalette(pal)
        # Style sheets can only load pictures from files, so the two icons are written out here.
        global _icon_dir
        _icon_dir = _icon_dir or QTemporaryDir()
        icons = {
            "arrow": ("M3 6l5 5 5-5", C["dim"]),
            "check": ("M3.5 8.5l3 3 6-7", C["on_accent"]),
        }
        paths = {}
        for name, (shape, color) in icons.items():
            paths[name] = _icon_dir.filePath(f"{name}.svg")
            Path(paths[name]).write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                f'<path d="{shape}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
            )
        app.setStyleSheet(STYLESHEET.format(**C, **paths))

    def set_tone(label, tone):
        """Color a status label: ok, warn or err. An empty tone clears it."""
        label.setStyleSheet(f"color: {C[tone]}; font-weight: 600;" if tone else "")

    def page_header(layout, title, subtitle):
        """Add the title and one-line description every page starts with."""
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        blurb = QLabel(subtitle)
        blurb.setObjectName("dim")
        blurb.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(blurb)
        layout.addSpacing(2)

    def scroll_page(title, subtitle):
        """Return a scrolling page and the layout to fill it with."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        page_header(layout, title, subtitle)
        scroll.setWidget(content)
        return scroll, layout

    class ErrorBanner(QFrame):
        """Banner shown on a tab when a backend command fails or returns invalid JSON."""

        retry_requested = Signal()

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("errorBanner")
            layout = QHBoxLayout(self)
            layout.setContentsMargins(10, 8, 10, 8)

            text_layout = QVBoxLayout()
            self.title_lbl = QLabel()
            set_tone(self.title_lbl, "err")
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
            root = QVBoxLayout(self)
            root.setContentsMargins(4, 4, 4, 4)
            root.setSpacing(16)
            layout = QFormLayout()
            layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            layout.setHorizontalSpacing(16)
            layout.setVerticalSpacing(12)
            root.addLayout(layout)
            checks = QGridLayout()
            checks.setHorizontalSpacing(24)
            checks.setVerticalSpacing(12)
            checks.setColumnStretch(0, 1)
            checks.setColumnStretch(1, 1)
            root.addLayout(checks)
            bools = 0

            for opt in self.schema:
                flag = opt.get("flag", "")
                otype = opt.get("type", "str")
                label_txt = opt.get("label", flag)
                help_txt = opt.get("help", "")
                default_val = opt.get("default")
                choices = opt.get("choices", [])

                lbl = QLabel(label_txt)
                cell = QVBoxLayout()
                cell.setSpacing(3)

                if otype == "bool":
                    w = QCheckBox(label_txt)
                    w.setAccessibleName(label_txt)
                    w.setChecked(bool(default_val))
                    cell.addWidget(w)
                    checks.addLayout(cell, bools // 2, bools % 2, Qt.AlignTop)
                    bools += 1
                    self.fields[flag] = ("bool", w)
                elif otype == "choice":
                    w = QComboBox()
                    w.setAccessibleName(label_txt)
                    # No default means the flag is optional: blank sends nothing and keeps the source's value.
                    if str(default_val) not in [str(c) for c in choices]:
                        w.addItem("")
                    w.addItems([str(c) for c in choices])
                    if default_val and str(default_val) in choices:
                        w.setCurrentText(str(default_val))
                    cell.addWidget(w)
                    layout.addRow(lbl, cell)
                    self.fields[flag] = ("choice", w)
                elif otype == "path":
                    h = QHBoxLayout()
                    le = QLineEdit(str(default_val or ""))
                    le.setAccessibleName(label_txt)
                    btn = QPushButton("Browse...")

                    # ponytail: the schema has no file-or-folder key, so this reads it from the help text.
                    def make_browse(target=le, title=label_txt, wants_dir=help_txt.startswith("Directory")):
                        def on_browse():
                            if wants_dir:
                                fn = QFileDialog.getExistingDirectory(self, f"Select {title}")
                            else:
                                fn, _ = QFileDialog.getOpenFileName(self, f"Select {title}")
                            if fn:
                                target.setText(fn)
                        return on_browse

                    btn.clicked.connect(make_browse())
                    h.addWidget(le)
                    h.addWidget(btn)
                    cell.addLayout(h)
                    layout.addRow(lbl, cell)
                    self.fields[flag] = ("path", le)
                elif otype == "list":
                    lw = QListWidget()
                    lw.setFixedHeight(92)
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

                    add_btn.clicked.connect(lambda _=False, fn=add_entry: fn())
                    new_item_le.returnPressed.connect(add_entry)
                    del_btn.clicked.connect(lambda _=False, fn=remove_entry: fn())
                    row.addWidget(new_item_le)
                    row.addWidget(add_btn)
                    row.addWidget(del_btn)
                    cell.addWidget(lw)
                    cell.addLayout(row)
                    layout.addRow(lbl, cell)
                    self.fields[flag] = ("list", lw)
                else:  # "str"
                    le = QLineEdit(str(default_val or ""))
                    le.setAccessibleName(label_txt)
                    if "password" in flag.lower():
                        le.setEchoMode(QLineEdit.Password)
                    cell.addWidget(le)
                    layout.addRow(lbl, cell)
                    self.fields[flag] = ("str", le)

                if help_txt:
                    hint = QLabel(help_txt)
                    hint.setObjectName("hintDanger" if help_txt.startswith("DANGER") else "hint")
                    hint.setWordWrap(True)
                    cell.addWidget(hint)

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
            layout.setContentsMargins(24, 20, 24, 16)
            layout.setSpacing(10)
            page_header(
                layout,
                catalog_type.capitalize(),
                "Tick what you want, then use the buttons below. Uses this distro's package manager or Flatpak."
                if catalog_type == "apps"
                else "Small system changes you can undo. Each one shows its risk.",
            )

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
            self.tree.setRootIsDecorated(False)
            self.tree.setItemsExpandable(False)
            self.tree.setIndentation(10)
            self.tree.setSelectionMode(QTreeWidget.NoSelection)
            header = self.tree.header()
            for col in range(1, len(headers) - 1):
                header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(len(headers) - 1, QHeaderView.Stretch)
            header.resizeSection(0, 290)
            self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
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

            categories = sorted({it.get("category", "General").capitalize() for it in items})
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
                c_item.setFirstColumnSpanned(True)
                cat_nodes[cat] = c_item

            for it in items:
                parent = cat_nodes.get(it.get("category", "General").capitalize())
                if not parent:
                    continue

                node = QTreeWidgetItem()
                node.setText(0, f"{it.get('name', '')} ({it.get('id', '')})")
                node.setToolTip(0, node.text(0))
                node.setData(0, Qt.UserRole, it)

                available = it.get("available", True)

                if self.catalog_type == "apps":
                    inst = it.get("installed", False)
                    node.setText(1, "Yes" if inst else "No")
                    node.setText(2, "Yes" if available else "No (Distro)")
                    node.setText(3, it.get("description", ""))
                    if inst:
                        node.setForeground(1, QColor(C["ok"]))
                    node.setToolTip(3, it.get("description", ""))
                else:
                    risk = (it.get("risk") or "low").upper()
                    node.setText(1, risk)
                    applied = it.get("applied")
                    node.setText(2, "Yes" if applied is True else ("No" if applied is False else "Unknown"))
                    node.setText(3, "Yes" if available else f"No ({it.get('reason') or 'Unsupported'})")
                    node.setText(4, it.get("description", ""))

                    node.setToolTip(4, it.get("description", ""))
                    node.setForeground(1, QColor(C[{"HIGH": "err", "MEDIUM": "warn"}.get(risk, "ok")]))

                    if applied is True:
                        node.setForeground(2, QColor(C["ok"]))

                if available:
                    node.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                    node.setCheckState(0, Qt.Unchecked)
                else:
                    node.setFlags(Qt.ItemIsUserCheckable)
                    node.setCheckState(0, Qt.Unchecked)
                    node.setForeground(0, QColor(C["dim"]))

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
                f"{ {'install': 'Installing', 'remove': 'Removing', 'apply': 'Applying', 'undo': 'Undoing'}[action] } "
                f"{len(ids)} {self.catalog_type}",
                on_finished=self.load_catalog,
            )

    class IsoBuilderTab(QWidget):
        """Shared implementation for Windows ISO and Linux ISO customizer tabs."""

        def __init__(self, main_win, flavor="winiso", parent=None):
            super().__init__(parent)
            self.main_win = main_win
            self.flavor = flavor  # "winiso" or "linuxiso"
            self.dynamic_form = None

            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)
            scroll, layout = scroll_page(
                "Windows 11 ISO" if flavor == "winiso" else "Linux ISO",
                "Make a debloated Windows 11 installer from an official ISO. Your original ISO is never changed."
                if flavor == "winiso"
                else "Remaster a live Linux ISO: add or remove packages, copy in files, set boot options. "
                "Your original ISO is never changed.",
            )
            outer.addWidget(scroll, 1)

            self.banner = ErrorBanner()
            self.banner.retry_requested.connect(self.on_refresh_info)
            layout.addWidget(self.banner)

            # Source ISO Picker
            source_group = QGroupBox("Source ISO")
            source_box = QVBoxLayout(source_group)
            s_layout = QHBoxLayout()
            source_box.addLayout(s_layout)
            self.iso_path_edit = QLineEdit()
            self.iso_path_edit.setPlaceholderText("Choose an ISO file...")
            self.iso_path_edit.setAccessibleName(f"{flavor} Source ISO Path")
            self.iso_path_edit.textChanged.connect(self.on_iso_path_changed)

            btn_browse_iso = QPushButton("Browse...")
            btn_browse_iso.clicked.connect(self.browse_source_iso)
            s_layout.addWidget(self.iso_path_edit)
            s_layout.addWidget(btn_browse_iso)

            # Download: pick from the catalog or paste a link. Stays hidden until the catalog has loaded.
            self.download_row = QWidget()
            d_layout = QHBoxLayout(self.download_row)
            d_layout.setContentsMargins(0, 0, 0, 0)
            self.download_combo = QComboBox()
            self.download_combo.setEditable(True)
            self.download_combo.setInsertPolicy(QComboBox.NoInsert)
            self.download_combo.lineEdit().setPlaceholderText("Or pick an ISO to download, or paste a link...")
            self.download_combo.setAccessibleName(f"{flavor} ISO to download")
            self.download_combo.currentTextChanged.connect(self.on_download_choice_changed)
            self.btn_download = QPushButton("Download")
            self.btn_download.clicked.connect(self.on_download_clicked)
            d_layout.addWidget(self.download_combo, 1)
            d_layout.addWidget(self.btn_download)
            self.download_row.setVisible(False)
            source_box.addWidget(self.download_row)
            self.lbl_download_note = QLabel("")
            self.lbl_download_note.setObjectName("hint")
            self.lbl_download_note.setWordWrap(True)
            self.lbl_download_note.setVisible(False)
            source_box.addWidget(self.lbl_download_note)
            layout.addWidget(source_group)

            # Info Group: stays hidden until an ISO has been read.
            if flavor == "winiso":
                self.info_group = QGroupBox("Editions in this ISO (click one to build it)")
                ig_layout = QVBoxLayout(self.info_group)
                self.editions_tree = QTreeWidget()
                self.editions_tree.setHeaderLabels(["Index", "Edition Name", "Architecture", "Build", "Size"])
                self.editions_tree.setRootIsDecorated(False)
                header = self.editions_tree.header()
                for col in range(5):
                    header.setSectionResizeMode(col, QHeaderView.Stretch if col == 1 else QHeaderView.ResizeToContents)
                self.editions_tree.setFixedHeight(150)
                self.editions_tree.itemSelectionChanged.connect(self.on_edition_selected)
                ig_layout.addWidget(self.editions_tree)
            else:
                self.info_group = QGroupBox("Compatibility")
                f_layout = QFormLayout(self.info_group)
                f_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
                self.lbl_family = QLabel("—")
                self.lbl_arch = QLabel("—")
                self.lbl_supported = QLabel("—")
                self.lbl_notes = QLabel("Select an ISO to inspect compatibility.")
                self.lbl_notes.setWordWrap(True)
                f_layout.addRow("Distro Family:", self.lbl_family)
                f_layout.addRow("Architecture:", self.lbl_arch)
                f_layout.addRow("Supported:", self.lbl_supported)
                f_layout.addRow("Notes:", self.lbl_notes)
            self.info_group.setVisible(False)
            layout.addWidget(self.info_group)

            # Customization Options Form
            self.options_group = QGroupBox("Options")
            self.options_layout = QVBoxLayout(self.options_group)
            layout.addWidget(self.options_group)
            layout.addStretch()

            # Output and Build stay on screen however far the options scroll.
            footer = QFrame()
            footer.setObjectName("footer")
            o_layout = QHBoxLayout(footer)
            o_layout.setContentsMargins(24, 10, 24, 10)
            o_layout.addWidget(QLabel("Save to"))
            self.out_path_edit = QLineEdit()
            default_fn = "custom-win11.iso" if flavor == "winiso" else "custom-linux.iso"
            self.out_path_edit.setText(str(Path.home() / default_fn))
            self.out_path_edit.setAccessibleName(f"{flavor} Output ISO Path")
            btn_browse_out = QPushButton("Browse...")
            btn_browse_out.clicked.connect(self.browse_out_iso)
            o_layout.addWidget(self.out_path_edit, 1)
            o_layout.addWidget(btn_browse_out)

            btn_title = "Build ISO"
            self.btn_build = QPushButton(btn_title)
            self.btn_build.setObjectName("primaryAction")
            self.btn_build.setAccessibleName("Build Custom Windows ISO" if flavor == "winiso" else "Build Custom Linux ISO")
            if flavor == "linuxiso":
                self.btn_build.setEnabled(False)  # blocked until verified supported
            self.btn_build.clicked.connect(self.on_build_clicked)
            o_layout.addWidget(self.btn_build)
            outer.addWidget(footer)

        def browse_source_iso(self):
            fn, _ = QFileDialog.getOpenFileName(self, "Select ISO Image", "", "ISO Images (*.iso)")
            if fn:
                self.iso_path_edit.setText(fn)

        def browse_out_iso(self):
            fn, _ = QFileDialog.getSaveFileName(self, "Select Destination ISO", self.out_path_edit.text(), "ISO Images (*.iso)")
            if fn:
                self.out_path_edit.setText(fn)

        def load_download_catalog(self):
            def on_done(exit_code, stdout, stderr):
                try:
                    entries = json.loads(stdout) if exit_code == 0 else []
                except ValueError:
                    entries = []
                kind = "windows" if self.flavor == "winiso" else "linux"
                self.download_combo.clear()
                for entry in entries:
                    if entry.get("kind") == kind:
                        self.download_combo.addItem(entry.get("name", entry.get("id", "")), entry)
                self.download_combo.setCurrentIndex(-1)
                self.download_row.setVisible(True)

            self.main_win.run_cli_query(["iso", "list", "--json"], on_done)

        def selected_download(self):
            """The catalog entry the combo's text names, or None for a pasted link."""
            i = self.download_combo.findText(self.download_combo.currentText().strip())
            return self.download_combo.itemData(i) if i >= 0 else None

        def on_download_choice_changed(self):
            entry = self.selected_download()
            page_only = bool(entry) and not entry.get("direct")
            self.btn_download.setText("Open Download Page" if page_only else "Download")
            note = entry.get("notes", "") if entry else ""
            self.lbl_download_note.setText(note)
            self.lbl_download_note.setVisible(bool(note))

        def on_download_clicked(self):
            entry = self.selected_download()
            if entry and not entry.get("direct"):
                self.open_download_page(entry)
                return
            target = entry["id"] if entry else self.download_combo.currentText().strip()
            title = f"Downloading {self.download_combo.currentText().strip()}" if entry else "Downloading ISO"
            # A pasted link may be signed, and other local users can read process arguments.
            args, env = (["iso", "download", target], None) if entry else (["iso", "download"], {"TUXUTIL_DOWNLOAD_URL": target})
            if not entry and not target.startswith(("http://", "https://")):
                QMessageBox.warning(self, "Nothing to Download", "Pick an ISO from the list, or paste a link starting with http:// or https://.")
                return
            self.start_download(args, title, env)

        def start_download(self, args, title, env=None):
            def on_success():
                if self.main_win.last_saved_path:
                    self.iso_path_edit.setText(self.main_win.last_saved_path)

            self.main_win.run_cli_job(args, title, on_finished=on_success, env=env)

        def open_download_page(self, entry):
            """Show a vendor's own page in the app, and download the ISO its button offers."""
            page_url = QUrl(entry.get("page", ""))
            try:
                from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
                from PySide6.QtWebEngineWidgets import QWebEngineView
            except ImportError:
                # Some distros package Qt WebEngine separately. The catalog note tells the user to paste the link.
                QDesktopServices.openUrl(page_url)
                return

            name = entry.get("name", "ISO")
            dialog = QDialog(self)
            dialog.setWindowTitle(f"Download {name}")
            dialog.resize(1000, 720)
            # A profile with no name keeps no cookies or history after the window closes.
            profile = QWebEngineProfile(dialog)
            view = QWebEngineView(dialog)
            page = QWebEnginePage(profile, view)
            view.setPage(page)
            profile.downloadRequested.connect(lambda item: self.catch_download(item, name, dialog))
            hint = QLabel("Pick your options on this page and press its download button. TuxUtil takes the file from there.")
            hint.setWordWrap(True)
            btn_browser = QPushButton("Open in My Browser Instead")

            def open_in_browser():
                QDesktopServices.openUrl(page_url)
                dialog.close()

            btn_browser.clicked.connect(open_in_browser)
            layout = QVBoxLayout(dialog)
            layout.addWidget(hint)
            layout.addWidget(view, 1)
            layout.addWidget(btn_browser)

            def release():
                # The page must go before its profile, or Qt warns and can crash on exit.
                view.setPage(None)
                page.deleteLater()
                dialog.deleteLater()

            dialog.finished.connect(lambda _result: release())
            view.load(page_url)
            dialog.open()

        def catch_download(self, item, name, dialog):
            url = item.url()
            item.cancel()
            if not url.path().lower().endswith(".iso"):
                QMessageBox.warning(dialog, "Not an ISO", "That download isn't an ISO file, so TuxUtil ignored it.")
                return
            # Keep the page open: Microsoft's button still works once the running job is done.
            if self.main_win.active_process is not None:
                QMessageBox.warning(dialog, "Job In Progress", "Another job is running. When it finishes, press the download button on this page again.")
                return
            dialog.close()
            # The vendor's link is signed, and other local users can read process arguments.
            self.start_download(["iso", "download"], f"Downloading {name}", {"TUXUTIL_DOWNLOAD_URL": url.toString(QUrl.FullyEncoded)})

        def load_options(self):
            if not self.download_combo.count():
                self.load_download_catalog()

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

        def on_edition_selected(self):
            items = self.editions_tree.selectedItems()
            if items and self.dynamic_form and "--edition" in self.dynamic_form.fields:
                self.dynamic_form.fields["--edition"][1].setText(items[0].text(0))

        def on_iso_path_changed(self):
            path = self.iso_path_edit.text().strip()
            self.info_group.setVisible(False)
            if self.flavor == "linuxiso":
                # Build stays blocked until this path has been inspected.
                self.lbl_supported.setText("—")
                set_tone(self.lbl_supported, "")
                self.btn_build.setEnabled(False)
            if path and (Path(path).is_file() or os.environ.get("TUXUTIL_CLI")):
                self.on_refresh_info()

        def on_refresh_info(self):
            path = self.iso_path_edit.text().strip()
            if not path:
                return
            self.banner.setVisible(False)

            def on_done(exit_code, stdout, stderr):
                if path != self.iso_path_edit.text().strip():
                    return  # The field has moved on to another ISO.
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

                self.info_group.setVisible(True)
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
                        self.lbl_supported.setText("Supported")
                        set_tone(self.lbl_supported, "ok")
                        self.btn_build.setEnabled(self.main_win.active_process is None)
                    else:
                        self.lbl_supported.setText("Unsupported")
                        set_tone(self.lbl_supported, "err")
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

            title = "Building Windows ISO" if self.flavor == "winiso" else "Building Linux ISO"
            self.main_win.run_cli_job(cmd, title, on_finished=on_success)

    class TuxUtilWindow(QMainWindow):
        """Main application window for TuxUtil."""

        def __init__(self):
            super().__init__()
            self.active_process = None
            self.active_job_is_flash = False
            self.last_built_iso = ""
            self.last_saved_path = ""
            self._active_queries = set()
            self._closing = False

            self.setWindowTitle("TuxUtil")
            self.resize(1000, 720)
            self.setMinimumSize(900, 600)

            apply_theme(QApplication.instance())
            self._init_ui()
            self.load_doctor_info()

        def closeEvent(self, event):
            if self.active_process and self.active_job_is_flash:
                # The root writer shares this window's output pipe, so closing could kill it mid-write.
                QMessageBox.information(self, "USB Flash Running", "Wait for the USB flash to finish, or press Cancel and wait for it to stop. Then close this window.")
                event.ignore()
                return
            if self.active_process:
                text = "A job is still running. Closing will stop it."
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

        def _init_ui(self):
            central = QWidget(self)
            self.setCentralWidget(central)
            main_layout = QHBoxLayout(central)
            main_layout.setContentsMargins(0, 0, 0, 0)
            main_layout.setSpacing(0)

            sidebar = QFrame()
            sidebar.setObjectName("sidebar")
            sidebar.setFixedWidth(180)
            side = QVBoxLayout(sidebar)
            side.setContentsMargins(0, 0, 0, 0)
            side.setSpacing(0)
            brand = QLabel("TuxUtil")
            brand.setObjectName("brand")
            side.addWidget(brand)
            self.nav = QListWidget()
            self.nav.setObjectName("nav")
            self.nav.setAccessibleName("Sections")
            self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            side.addWidget(self.nav, 1)
            self.lbl_sidebar_distro = QLabel("")
            self.lbl_sidebar_distro.setObjectName("sidebarFoot")
            self.lbl_sidebar_distro.setWordWrap(True)
            side.addWidget(self.lbl_sidebar_distro)
            main_layout.addWidget(sidebar)

            self.splitter = QSplitter(Qt.Vertical)
            self.splitter.setChildrenCollapsible(False)
            self.splitter.setHandleWidth(1)
            main_layout.addWidget(self.splitter, 1)

            self.tabs = QTabWidget()
            self.tabs.setAccessibleName("TuxUtil Tabs")
            self.tabs.tabBar().hide()  # The sidebar switches pages.
            self.splitter.addWidget(self.tabs)

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

            names = [self.tabs.tabText(i) for i in range(self.tabs.count())]
            self.nav_items = {}
            for section, entries in NAV:
                head = QListWidgetItem(section)
                head.setFlags(Qt.NoItemFlags)
                self.nav.addItem(head)
                for name in entries:
                    item = QListWidgetItem(name)
                    item.setData(Qt.UserRole, names.index(name))
                    self.nav.addItem(item)
                    self.nav_items[names.index(name)] = item
            self.nav.currentItemChanged.connect(lambda item, _old: item and self.tabs.setCurrentIndex(item.data(Qt.UserRole)))
            self.tabs.currentChanged.connect(lambda i: self.nav.setCurrentItem(self.nav_items[i]))
            self.nav.setCurrentItem(self.nav_items[0])

            # Execution Panel
            self.exec_panel = self._create_execution_panel()
            self.splitter.addWidget(self.exec_panel)
            self.splitter.setStretchFactor(0, 1)
            self.splitter.setStretchFactor(1, 0)

        # Tab 1: Doctor
        def _create_doctor_tab(self):
            scroll, layout = scroll_page("Doctor", "What TuxUtil sees on this system, and whether the tools it uses are installed.")

            self.doctor_banner = ErrorBanner()
            self.doctor_banner.retry_requested.connect(self.load_doctor_info)
            layout.addWidget(self.doctor_banner)

            sys_group = QGroupBox("This System")
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
                self.lbl_sidebar_distro.setText(data.get("name", ""))

                missing = data.get("missing", [])
                self.tools_list.clear()
                if not missing:
                    self.lbl_tools_status.setText("All required tools are installed.")
                    set_tone(self.lbl_tools_status, "ok")
                    self.btn_install_missing.setEnabled(False)
                    self.btn_install_missing.setVisible(False)
                    self.tools_list.setVisible(False)
                else:
                    self.lbl_tools_status.setText(f"{len(missing)} tools are missing:")
                    set_tone(self.lbl_tools_status, "warn")
                    for t in missing:
                        self.tools_list.addItem(t)
                    self.tools_list.setVisible(True)
                    self.btn_install_missing.setVisible(True)
                    self.btn_install_missing.setEnabled(True)

            self.run_cli_query(["doctor", "--json"], on_done)

        # Tab 4: Updates
        def _create_updates_tab(self):
            scroll, layout = scroll_page("Updates", "Update this distro's packages and every Flatpak in one go.")

            row = QHBoxLayout()
            self.btn_run_update = QPushButton("Run Update")
            self.btn_run_update.setObjectName("primaryAction")
            self.btn_run_update.clicked.connect(lambda: self.run_cli_job(["update"], "System and Flatpak Update"))
            row.addWidget(self.btn_run_update)
            row.addStretch()
            layout.addLayout(row)
            layout.addStretch()
            return scroll

        # Tab 7: Flash USB
        def _create_flash_tab(self):
            scroll, layout = scroll_page("Flash USB", "Write an ISO to a USB drive and check the copy. System drives can't be picked.")

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
            self.confirm_group = QGroupBox("Confirm: this erases the drive")
            self.confirm_group.setObjectName("dangerCard")
            c_layout = QVBoxLayout(self.confirm_group)
            c_layout.setSpacing(10)

            warn_lbl = QLabel("Everything on the target drive will be permanently erased.")
            warn_lbl.setWordWrap(True)
            set_tone(warn_lbl, "err")
            c_layout.addWidget(warn_lbl)

            self.lbl_confirm_prompt = QLabel("Select a safe USB drive to proceed.")
            self.lbl_confirm_prompt.setWordWrap(True)
            c_layout.addWidget(self.lbl_confirm_prompt)

            self.confirm_input = QLineEdit()
            self.confirm_input.setPlaceholderText("Type exact device path here (e.g. /dev/sdb)...")
            self.confirm_input.setAccessibleName("Confirm Device Path Input")
            self.confirm_input.textChanged.connect(self.validate_flash_confirm)
            c_layout.addWidget(self.confirm_input)

            self.btn_flash_write = QPushButton("Flash USB Drive")
            self.btn_flash_write.setObjectName("dangerAction")
            self.btn_flash_write.setAccessibleName("Write Image to USB Device")
            self.btn_flash_write.setEnabled(False)  # Must stay disabled until ISO, safe dev, and exact path match
            self.btn_flash_write.clicked.connect(self.on_flash_write)
            c_layout.addWidget(self.btn_flash_write)

            layout.addWidget(self.confirm_group)
            layout.addStretch()

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
                        self.device_combo.addItem(f"Blocked: {model} — {path} ({size}): {reason or 'system drive'}", dev)
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
                self.lbl_selected_device.setText(f"{path} can't be used: {reason}.")
                self.lbl_confirm_prompt.setText("Plug in a USB drive and click Refresh Devices.")
                self.confirm_input.setEnabled(False)
                self.btn_flash_write.setEnabled(False)
            else:
                self.lbl_selected_device.setText(f"{model}, {path}, {size}")
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
            cmd = ["flash", "write", iso_path, device_path, "--confirm", device_path, "--cancel-on-eof"]
            self.run_cli_job(
                cmd,
                f"Flashing {dev.get('model', 'Drive')} ({device_path})",
                is_flash=True,
                on_finished=self.load_flash_devices,
            )

        # Bottom Execution & Live Output Panel
        def _create_execution_panel(self):
            panel = QFrame()
            panel.setObjectName("jobPanel")
            layout = QVBoxLayout(panel)
            layout.setContentsMargins(24, 8, 24, 8)
            layout.setSpacing(6)

            h_row = QHBoxLayout()
            self.lbl_job_status = QLabel("Ready")
            self.lbl_job_status.setObjectName("dim")
            self.lbl_job_status.setWordWrap(True)
            self.lbl_job_status.setAccessibleName("Current Job Status")
            h_row.addWidget(self.lbl_job_status, 1)

            self.btn_cancel_job = QPushButton("Cancel Job")
            self.btn_cancel_job.setObjectName("dangerAction")
            self.btn_cancel_job.setAccessibleName("Cancel Active Job")
            # Hard rule: disabled when no job runs
            self.btn_cancel_job.setEnabled(False)
            self.btn_cancel_job.setVisible(False)
            self.btn_cancel_job.clicked.connect(self.on_cancel_active_job)
            h_row.addWidget(self.btn_cancel_job)

            self.btn_toggle_log = QPushButton("Show Log")
            self.btn_toggle_log.clicked.connect(lambda: self._set_log_visible(not self.log_visible))
            h_row.addWidget(self.btn_toggle_log)

            self.btn_clear_log = QPushButton("Clear Log")
            self.btn_clear_log.clicked.connect(lambda: self.log_text.clear())
            self.btn_clear_log.setVisible(False)
            h_row.addWidget(self.btn_clear_log)
            layout.addLayout(h_row)

            self.progress_bar = QProgressBar()
            self.progress_bar.setAccessibleName("Job Progress Bar")
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.progress_bar.setVisible(False)
            layout.addWidget(self.progress_bar)

            self.log_text = QPlainTextEdit()
            self.log_text.setReadOnly(True)
            self.log_text.setAccessibleName("Job Output Log Pane")
            font = QFont("monospace")
            font.setStyleHint(QFont.Monospace)
            self.log_text.setFont(font)
            self.log_text.setMaximumBlockCount(3000)
            self.log_text.setMinimumHeight(100)
            self.log_text.setVisible(False)
            self.log_visible = False
            layout.addWidget(self.log_text, 1)

            return panel

        def _set_log_visible(self, visible):
            self.log_visible = visible
            self.log_text.setVisible(visible)
            self.btn_clear_log.setVisible(visible)
            self.btn_toggle_log.setText("Hide Log" if visible else "Show Log")
            # The splitter keeps the old sizes unless told, which would leave a gap when the log closes.
            total = sum(self.splitter.sizes())
            log_height = 240 if visible else 0
            self.splitter.setSizes([total - log_height, log_height])

        def _set_job_status(self, text, tone=""):
            self.lbl_job_status.setText(text)
            set_tone(self.lbl_job_status, tone)

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
            # Qt does not emit finished when the program can't start.
            def on_error(error):
                if error == QProcess.ProcessError.FailedToStart:
                    stderr_buf.append(proc.errorString())
                    on_finish(-1, None)

            proc.errorOccurred.connect(on_error)
            proc.start(full_cmd[0], full_cmd[1:])

        def run_cli_job(self, args, job_title="Job", is_flash=False, on_finished=None, env=None):
            """Run a long job via QProcess, streaming log output and parsing @progress lines."""
            if self.active_process is not None:
                QMessageBox.warning(self, "Job In Progress", "A job is already running. Please wait or cancel it first.")
                return

            self.active_job_is_flash = is_flash
            self.last_saved_path = ""
            self.job_cancelled = False
            base_cmd = get_cli_base_cmd()
            args, password = split_password(args)
            full_cmd = base_cmd + args

            # self_cmd's startup code would only be noise here.
            from . import system
            prefix = system.self_cmd()
            shown = ["tuxutil", *full_cmd[len(prefix):]] if full_cmd[:len(prefix)] == prefix else full_cmd
            masked_str = mask_command_for_log(shown)
            self.log_text.appendPlainText(f"\n$ {masked_str}")

            self._set_job_status(f"Running: {job_title}...")
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat(f"0% - Starting {job_title}...")
            self.progress_bar.setVisible(True)
            self.btn_cancel_job.setVisible(True)
            self._set_log_visible(True)
            self.btn_cancel_job.setEnabled(True)
            self.set_actions_enabled(False)

            proc = QProcess(self)
            self.active_process = proc
            env = dict(env or {})
            if password:
                env["TUXUTIL_PASSWORD"] = password
            if env:
                proc_env = QProcessEnvironment.systemEnvironment()
                for key, value in env.items():
                    proc_env.insert(key, value)
                proc.setProcessEnvironment(proc_env)
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
                        if line.startswith("@saved "):
                            self.last_saved_path = line[len("@saved "):]
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
                self.btn_cancel_job.setVisible(False)
                self.progress_bar.setVisible(False)
                self.set_actions_enabled(True)

                # A flash can finish before its cancel arrives, and then the stick is good.
                if self.job_cancelled and (exit_code != 0 or not self.active_job_is_flash):
                    self._set_job_status(f"{job_title} cancelled", "warn")
                elif exit_code == 0:
                    self._set_job_status(f"{job_title} completed successfully", "ok")
                    if on_finished:
                        on_finished()
                else:
                    self._set_job_status(f"{job_title} failed with exit code {exit_code}", "err")
                    if not self.log_visible:
                        self._set_log_visible(True)

                proc.deleteLater()

            proc.readyReadStandardOutput.connect(on_out)
            proc.readyReadStandardError.connect(on_err)
            proc.finished.connect(on_finish)
            # Qt does not emit finished when the program can't start.
            def on_error(error):
                if error == QProcess.ProcessError.FailedToStart:
                    self.log_text.appendPlainText(proc.errorString())
                    on_finish(-1, None)

            proc.errorOccurred.connect(on_error)
            proc.start(full_cmd[0], full_cmd[1:])

        def on_cancel_active_job(self):
            if not self.active_process:
                return

            self.log_text.appendPlainText("\nTerminating job at user request...")
            self.job_cancelled = True
            self._set_job_status("Terminating job...", "warn")
            proc = self.active_process
            if self.active_job_is_flash:
                # The writer runs as root, so a signal from here can't reach it. It watches for this.
                self.btn_cancel_job.setEnabled(False)
                proc.closeWriteChannel()
                return
            proc.terminate()

            # Only this job: another may have started by the time the timer fires.
            QTimer.singleShot(1000, lambda: self.active_process is proc and proc.kill())

        def set_actions_enabled(self, enabled):
            """Enable or disable trigger buttons during long job runs."""
            self.btn_install_missing.setEnabled(enabled and (self.tools_list.count() > 0))
            self.tab_apps.btn_act1.setEnabled(enabled)
            self.tab_apps.btn_act2.setEnabled(enabled)
            self.tab_tweaks.btn_act1.setEnabled(enabled)
            self.tab_tweaks.btn_act2.setEnabled(enabled)
            self.btn_run_update.setEnabled(enabled)
            self.tab_winiso.btn_download.setEnabled(enabled)
            self.tab_linuxiso.btn_download.setEnabled(enabled)
            self.tab_winiso.btn_build.setEnabled(enabled)
            self.tab_linuxiso.btn_build.setEnabled(enabled and (self.tab_linuxiso.lbl_supported.text() == "Supported"))
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

    # Qt WebEngine refuses to load later unless this is set before the application exists.
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    win = TuxUtilWindow()
    win.show()
    return app.exec()
