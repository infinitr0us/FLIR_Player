# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fit the emissivity from thermocouples: the setup and results dialogs.

The work itself is ``tcmatch`` (Qt-free), run by the decoder thread on the
open recording. The setup dialog collects the TC file and the options; the
results dialog shows each TC's pixel and emissivity and offers to use them in
the player or in an Excel workbook.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .excel_dialog import _panel
from .export_dialogs import _path_row, confirm_replace
from .models import RoiShape, VideoMetadata
from .settings import app_settings
from .tcdata import TEXT_SUFFIXES, WORKBOOK_SUFFIXES, TcTable, parse_windows, read_tc_table, sheet_names, suggest_sheet
from .tcmatch import MEMORY_LIMIT, MatchOptions, RunOutput, roi_shapes
from .widgets import ICON_MUTED, ChevronComboBox, FramelessDialog, MessageDialog, awesome_icon

RESULT_FILES = ("result.json", "summary.txt", "overview.png")  # what a run writes besides the ROI set
LARGE_STATISTICS = 4e9  # bytes of frame statistics worth a hint to draw a smaller region
# The size is estimated from the average frame rate; dropped frames (superframing) can make the
# engine's bins finer, so held in memory it must fit with this margin.
MEMORY_MARGIN = 1.25
# Channels that measure air or gas have no surface pixel: unticked at first (the user decides).
_AIR_HINT = re.compile(r"(?<![a-z])(tree|air|gas|duct|ambient|room|plume|flame|exhaust)(?![a-z])", re.IGNORECASE)
_LAST_DIR_KEY = "tcmatch/last_dir"
_ALIVE: set[QThread] = set()  # readers outliving a closed dialog finish here


def _keep_until_done(reader: QThread) -> None:
    """Hold a reader until it ends; the application waits for it before quitting.

    A QThread destroyed while it runs aborts the process, as it would when the
    player closes during a read (a lab workbook takes a few seconds).
    """
    app = QApplication.instance()
    if app is not None and not app.property("tcReadersHooked"):
        app.aboutToQuit.connect(lambda: [thread.wait() for thread in tuple(_ALIVE)])
        app.setProperty("tcReadersHooked", True)
    _ALIVE.add(reader)
    reader.finished.connect(lambda: (_ALIVE.discard(reader), reader.deleteLater()))


def results_folder(recording: Path) -> Path:
    """Where a recording's TC results go by default: ``<name>_tcmatch`` next to it (as the CLI)."""
    return recording.with_name(recording.stem + "_tcmatch")


class _TableReader(QThread):
    """Reads a TC file's sheet names and one sheet off the UI thread (a lab workbook takes seconds)."""

    read = Signal(int, object, object, str)  # request, sheet names, table, error

    def __init__(self, request: int, path: Path, sheet: str | None) -> None:
        super().__init__()
        self.request, self.path, self.sheet = request, path, sheet

    def run(self) -> None:
        try:
            names = sheet_names(self.path)
            sheet = self.sheet if self.sheet is not None else suggest_sheet(names)
            table = read_tc_table(self.path, sheet=sheet)
            self.read.emit(self.request, names, table, "")
        except Exception as exc:  # shown in the dialog, which stays usable
            names = None
            try:
                names = sheet_names(self.path)
            except Exception:
                pass
            self.read.emit(self.request, names, None, str(exc) or type(exc).__name__)


def _box_region(shape: RoiShape, width: int, height: int) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1), end exclusive, of the pixels a box ROI touches."""
    (ax, ay), (bx, by) = shape.points
    x0, x1 = sorted((ax, bx))
    y0, y1 = sorted((ay, by))
    return (max(0, math.floor(x0)), max(0, math.floor(y0)), min(width, math.ceil(x1)), min(height, math.ceil(y1)))


class TcCalibrationDialog(FramelessDialog):
    """Choose the TC file and the options for fitting the emissivity of the open recording."""

    LAST_RESULTS = 2  # done() code: show the previous run's results instead

    def __init__(self, metadata: VideoMetadata, rois: tuple[RoiShape, ...], current_frame: int, *,
                 selected_roi_id: int | None = None, ignition_frame: int | None = None,
                 previous: dict | None = None, has_results: bool = False, parent=None) -> None:
        super().__init__("Fit Emissivity from TCs", parent)
        self.setMinimumWidth(660)
        self.metadata = metadata
        self.table: TcTable | None = None
        self._sheets: list[str] = []
        self._request = 0
        self._read_path = ""  # the TC file last read (or being read)
        self._confirmed: list[str] = []
        self._replace: list[str] = []  # existing results the user agreed to replace
        previous = dict(previous or {})
        self._pending = {name: (checked, text) for name, checked, text in previous.get("channel_rows", ())}

        scroll = QScrollArea()
        scroll.setObjectName("PickerScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # panels fit the width
        content = QWidget()
        content.setObjectName("PickerContent")
        panels = QVBoxLayout(content)
        panels.setContentsMargins(0, 0, 4, 0)
        panels.setSpacing(10)
        scroll.setWidget(content)

        intro = QLabel("Finds each thermocouple's pixel and the time offset between the logger and the "
                       "recording, then fits the emissivity where IR and TC can be compared (no flames, TC "
                       "working). Reads every frame once.")
        intro.setWordWrap(True)
        panels.addWidget(intro)

        # thermocouple data
        frame, box = _panel("Thermocouple data")
        row, self.file_edit, browse = _path_row("Choose the TC file (.xlsx or .csv)")
        self.file_edit.setPlaceholderText("Logger export: .xlsx, .csv or .txt, time in the first columns")
        self.file_edit.editingFinished.connect(self._file_edited)
        browse.clicked.connect(self._browse_tc)
        box.addLayout(row)
        sheet_row = QHBoxLayout()
        sheet_row.addWidget(QLabel("Sheet"))
        self.sheet_combo = ChevronComboBox()
        self.sheet_combo.setToolTip("The workbook sheet with the TC readings")
        self.sheet_combo.activated.connect(lambda _index: self._load(sheet=self.sheet_combo.currentText()))
        sheet_row.addWidget(self.sheet_combo, 1)
        box.addLayout(sheet_row)
        self.table_label = QLabel()
        self.table_label.setObjectName("FieldLabel")
        self.table_label.setWordWrap(True)
        box.addWidget(self.table_label)
        self.channel_table = QTableWidget(0, 3)
        self.channel_table.setHorizontalHeaderLabels(["Thermocouple", "Readings (°C)", "Use only (s)"])
        self.channel_table.verticalHeader().setVisible(False)
        self.channel_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.channel_table.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)  # one click edits
        header = self.channel_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.channel_table.horizontalHeaderItem(2).setToolTip(
            "Logger seconds a TC can be trusted, e.g. 0-1150 or 0-300, 500-900; blank = all of it")
        self.channel_table.setMinimumHeight(150)
        self.channel_table.itemChanged.connect(lambda _item: self._update())
        box.addWidget(self.channel_table)
        hint_row = QHBoxLayout()
        hint = QLabel("Tick the TCs on the surface the camera sees; TCs in air or gas have no pixel. "
                      "\"Use only\" keeps a TC to the seconds it worked (e.g. before it failed).")
        hint.setObjectName("FieldLabel")
        hint.setWordWrap(True)
        hint_row.addWidget(hint, 1)
        for text, checked in (("All", True), ("None", False)):
            button = QPushButton(text)
            button.setProperty("variant", "ghost")
            button.clicked.connect(lambda _=False, checked=checked: self._check_all(checked))
            hint_row.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        box.addLayout(hint_row)
        panels.addWidget(frame)

        # recording
        frame, box = _panel("Recording")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self.region_combo = ChevronComboBox()
        self.region_combo.addItem(f"Whole image ({metadata.width} × {metadata.height})", None)
        for shape in rois:
            if shape.kind != "rect":
                continue
            region = _box_region(shape, metadata.width, metadata.height)
            if region[2] > region[0] and region[3] > region[1]:
                x0, y0, x1, y1 = region
                self.region_combo.addItem(f"{shape.name}: columns {x0}-{x1 - 1}, rows {y0}-{y1 - 1}", region)
                if shape.id == selected_roi_id:
                    self.region_combo.setCurrentIndex(self.region_combo.count() - 1)
        self.region_combo.setToolTip("Where to look for the TCs. A box ROI around the battery makes the run "
                                     "faster and its saved statistics smaller.")
        grid.addWidget(QLabel("Search region"), 0, 0)
        grid.addWidget(self.region_combo, 0, 1, 1, 2)
        r = 1
        self.preset_combo = None
        if metadata.presets:
            self.preset_combo = ChevronComboBox()
            lowest = min(metadata.presets, key=lambda p: p.min_k if p.min_k is not None else math.inf)
            for preset in metadata.presets:
                span = (f"{preset.min_k - 273.15:.0f} to {preset.max_k - 273.15:.0f} °C"
                        if preset.min_k is not None and preset.max_k is not None else "not calibrated")
                self.preset_combo.addItem(f"Preset {preset.index}: {span}", preset.index)
                if preset is lowest:
                    self.preset_combo.setCurrentIndex(self.preset_combo.count() - 1)
            self.preset_combo.setToolTip("Superframing: the frames of one preset are used. The lowest range "
                                         "is closest to TC temperatures.")
            grid.addWidget(QLabel("Preset"), r, 0)
            grid.addWidget(self.preset_combo, r, 1, 1, 2)
            r += 1
        self.params_combo = ChevronComboBox()
        self.params_combo.addItem("As in the Measurement panel", "player")
        self.params_combo.addItem("The recording's own (camera values)", "file")
        self.params_combo.setToolTip("Distance, reflected temperature and atmosphere for the fit. The "
                                     "emissivity is what the fit finds.")
        grid.addWidget(QLabel("Object parameters"), r, 0)
        grid.addWidget(self.params_combo, r, 1, 1, 2)
        r += 1
        self.time_combo = ChevronComboBox()
        self.time_combo.addItem("Find automatically (recommended)", "auto")
        self.time_combo.addItem("Logger time 0 is at frame", "frame")
        self.time_combo.setToolTip("The logger's time 0 in the recording. Automatic matching uses the TC "
                                   "curves; give the frame when it is known (e.g. ignition) or the match "
                                   "is reported as uncertain.")
        self.frame_spin = QSpinBox()
        self.frame_spin.setRange(1, max(1, metadata.num_frames))
        self.frame_spin.setValue((ignition_frame if ignition_frame is not None else current_frame) + 1)
        self.frame_spin.setToolTip("Frame number as shown in the player")
        self.current_button = QPushButton("Current frame")
        self.current_button.setProperty("variant", "ghost")
        self.current_button.clicked.connect(lambda: self.frame_spin.setValue(current_frame + 1))
        time_row = QHBoxLayout()
        time_row.addWidget(self.time_combo, 1)
        time_row.addWidget(self.frame_spin)
        time_row.addWidget(self.current_button)
        grid.addWidget(QLabel("Time match"), r, 0)
        grid.addLayout(time_row, r, 1, 1, 2)
        grid.setColumnStretch(1, 1)
        box.addLayout(grid)
        panels.addWidget(frame)

        # advanced
        frame, box = _panel("Fit")
        self.advanced_button = QToolButton()
        self.advanced_button.setObjectName("DisclosureButton")
        self.advanced_button.setText("Advanced options")
        self.advanced_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_button.setIcon(awesome_icon("fa6s.chevron-right", ICON_MUTED))
        self.advanced_button.setIconSize(QSize(11, 11))
        self.advanced_button.setCheckable(True)
        self.advanced_button.toggled.connect(self._toggle_advanced)
        box.addWidget(self.advanced_button)
        self._advanced = QWidget()
        form = QFormLayout(self._advanced)
        form.setContentsMargins(0, 0, 0, 0)
        defaults = MatchOptions()
        self.spot_spin = QSpinBox()
        self.spot_spin.setRange(1, 15)
        self.spot_spin.setValue(defaults.spot)
        self.spot_spin.setSuffix(" px")
        self.spot_spin.setToolTip("Each TC's IR is the mean of an N × N block around its pixel")
        form.addRow("Spot size (N × N)", self.spot_spin)
        self.tc_min_spin = QDoubleSpinBox()
        self.tc_min_spin.setRange(-40.0, 1500.0)
        self.tc_min_spin.setDecimals(0)
        self.tc_min_spin.setValue(defaults.tc_min_c)
        self.tc_min_spin.setSuffix(" °C")
        self.tc_min_spin.setToolTip("Near room temperature IR and TC hardly depend on the emissivity")
        form.addRow("Fit only where the TC reads at least", self.tc_min_spin)
        self.jump_spin = QDoubleSpinBox()
        self.jump_spin.setRange(1.0, 1000.0)
        self.jump_spin.setDecimals(0)
        self.jump_spin.setValue(defaults.jump_k)
        self.jump_spin.setSuffix(" K")
        self.hold_spin = QDoubleSpinBox()
        self.hold_spin.setRange(0.0, 3600.0)
        self.hold_spin.setDecimals(0)
        self.hold_spin.setValue(defaults.jump_hold_s)
        self.hold_spin.setSuffix(" s")
        for widget in (self.jump_spin, self.hold_spin):
            widget.setToolTip("A flame or hot gas on a TC makes it jump faster than the surface can heat; "
                              "it then reads neither while both cool. 0 s turns this off.")
        form.addRow(f"Flame on a TC: a rise within {defaults.jump_window_s:g} s over", self.jump_spin)
        form.addRow("Skip after such a rise", self.hold_spin)
        self.below_check = QCheckBox("Also fit IR below the calibrated range")
        self.below_check.setToolTip("Below the camera range the temperatures are extrapolated; more events, "
                                    "but less reliable")
        form.addRow("", self.below_check)
        self.cache_check = QCheckBox("Keep the frame statistics in the results folder")
        self.cache_check.setToolTip("Later runs with the same search region skip reading the frames")
        self.cache_check.setChecked(True)
        self.cache_check.toggled.connect(self._update)
        form.addRow("", self.cache_check)
        self._advanced.setVisible(False)
        box.addWidget(self._advanced)
        panels.addWidget(frame)
        panels.addStretch(1)
        self.body.addWidget(scroll, 1)

        out = QFormLayout()
        row, self.out_edit, browse = _path_row("Choose the results folder")
        self.out_edit.setText(str(previous.get("out_dir") or results_folder(metadata.path)))
        browse.clicked.connect(self._browse_out)
        out.addRow("Results folder", row)
        self.body.addLayout(out)
        self.estimate_label = QLabel()
        self.estimate_label.setObjectName("FieldLabel")
        self.estimate_label.setWordWrap(True)
        self.body.addWidget(self.estimate_label)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                           | QDialogButtonBox.StandardButton.Cancel)
        run = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        run.setText("Run")
        run.setProperty("accent", True)
        if has_results:
            last = self.button_box.addButton("Show Last Results", QDialogButtonBox.ButtonRole.ActionRole)
            last.setToolTip("The results of the previous run on this recording")
            last.clicked.connect(lambda: self.done(self.LAST_RESULTS))
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.body.addWidget(self.button_box)
        self._add_size_grip()

        self.time_combo.currentIndexChanged.connect(self._update)
        self.region_combo.currentIndexChanged.connect(self._update)
        self._restore(previous)
        self._update()
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry().height() if screen is not None else 900
        fixed = sum(self.body.itemAt(i).sizeHint().height() for i in range(self.body.count())
                    if self.body.itemAt(i).widget() is not scroll)
        wanted = content.sizeHint().height() + fixed + 16 * self.body.count() + 40
        self.resize(max(self.minimumWidth(), content.sizeHint().width() + 48), min(wanted, int(available * 0.9)))

    def keyPressEvent(self, event) -> None:
        # Enter finishes typing (a path, a "Use only" cell) or does nothing on a check box; it never
        # starts a minutes-long run unless a push button has the focus
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not isinstance(
                self.focusWidget(), QPushButton):
            event.accept()
            return
        super().keyPressEvent(event)

    # --- TC file ---------------------------------------------------------------------------

    def _browse_tc(self) -> None:
        start = self.file_edit.text().strip() or str(app_settings().value(_LAST_DIR_KEY, "", type=str)
                                                     or self.metadata.path.parent)
        patterns = " ".join(f"*{s}" for s in WORKBOOK_SUFFIXES + TEXT_SUFFIXES)
        path, _ = QFileDialog.getOpenFileName(self, "Choose the TC file", start,
                                              f"TC logger data ({patterns});;All files (*.*)")
        if path:
            self.file_edit.setText(path)
            app_settings().setValue(_LAST_DIR_KEY, str(Path(path).parent))
            self._load(sheet=None)

    def _file_edited(self) -> None:
        # editingFinished also fires when the field merely loses focus (e.g. on a click on Run)
        if self.file_edit.text().strip() != self._read_path:
            self._load(sheet=None)

    def _load(self, sheet: str | None) -> None:
        """Read the TC file (in the background); ``sheet`` None = the one named like TC data."""
        text = self.file_edit.text().strip()
        self._read_path = text
        self._request += 1  # a read still under way for another path is now stale
        path = Path(text) if text else None
        if path is None or not path.is_file():
            self.table = None
            self._sheets = []
            self.sheet_combo.clear()
            self.channel_table.setRowCount(0)
            self.table_label.setText("Choose the logger's export of the TC readings." if path is None
                                     else "No such file.")
            self._update()
            return
        if self.table is not None and not self._pending:  # keep the user's choices for a re-read sheet
            self._pending = self._channel_rows()
        self.table = None
        self.table_label.setText("Reading the TC file…")
        self._update()
        reader = _TableReader(self._request, path, sheet)
        reader.read.connect(self._loaded)
        _keep_until_done(reader)
        reader.start()

    def _loaded(self, request: int, names, table, error: str) -> None:
        if request != self._request:
            return  # a newer read is on its way
        self._sheets = list(names or [])
        self.sheet_combo.blockSignals(True)
        self.sheet_combo.clear()
        self.sheet_combo.addItems(self._sheets)
        if table is not None and table.sheet in self._sheets:
            self.sheet_combo.setCurrentIndex(self._sheets.index(table.sheet))
        self.sheet_combo.setEnabled(len(self._sheets) > 1)
        self.sheet_combo.blockSignals(False)
        self.table = table
        self.channel_table.blockSignals(True)
        self.channel_table.setRowCount(0)
        if table is None:
            self.table_label.setText(f"Could not read TC data: {error}")
        else:
            t0, t1 = float(table.times[0]), float(table.times[-1])
            self.table_label.setText(f"{len(table.names)} channel(s), {table.times.size:,} readings every "
                                     f"{table.interval:g} s, logger time {t0:g} to {t1:g} s.")
            for k, name in enumerate(table.names):
                checked, windows = self._pending.get(name, (not _AIR_HINT.search(name), ""))
                self._add_channel_row(name, table.values[:, k], checked, windows)
        self.channel_table.blockSignals(False)
        self._pending = {}
        self._update()

    def _add_channel_row(self, name: str, values, checked: bool, windows: str) -> None:
        row = self.channel_table.rowCount()
        self.channel_table.insertRow(row)
        self.channel_table.setRowHeight(row, 30)
        item = QTableWidgetItem(name)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.channel_table.setItem(row, 0, item)
        finite = values[np.isfinite(values)]
        span = f"{finite.min():.0f} to {finite.max():.0f}" if finite.size else "no readings"
        item = QTableWidgetItem(span)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.channel_table.setItem(row, 1, item)
        item = QTableWidgetItem(windows)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable)
        item.setToolTip("e.g. 0-1150; blank = all readings")
        self.channel_table.setItem(row, 2, item)

    def _channel_rows(self) -> dict[str, tuple[bool, str]]:
        rows = {}
        for row in range(self.channel_table.rowCount()):
            name = self.channel_table.item(row, 0).text()
            rows[name] = (self.channel_table.item(row, 0).checkState() == Qt.CheckState.Checked,
                          self.channel_table.item(row, 2).text().strip())
        return rows

    def _check_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.channel_table.rowCount()):
            self.channel_table.item(row, 0).setCheckState(state)

    def chosen_channels(self) -> list[str]:
        return [name for name, (checked, _text) in self._channel_rows().items() if checked]

    def _windows(self) -> dict[str, tuple[tuple[float, float], ...]]:
        """Validity windows per ticked channel; ValueError names a channel whose text does not parse."""
        valid = {}
        for name, (checked, text) in self._channel_rows().items():
            if checked and text:
                try:
                    valid[name] = parse_windows(text)
                except ValueError as exc:
                    raise ValueError(f"{name}: \"{text}\" is not a list of seconds like 0-1150 ({exc})") from exc
        return valid

    # --- options -----------------------------------------------------------------------------

    def _toggle_advanced(self, checked: bool) -> None:
        self._advanced.setVisible(checked)
        icon = "fa6s.chevron-down" if checked else "fa6s.chevron-right"
        self.advanced_button.setIcon(awesome_icon(icon, ICON_MUTED))

    def _browse_out(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose the results folder", self.out_edit.text())
        if path:
            self.out_edit.setText(path)

    def region(self) -> tuple[int, int, int, int] | None:
        return self.region_combo.currentData()

    def options(self) -> MatchOptions:
        fixed = self.time_combo.currentData() == "frame"
        return MatchOptions(
            roi=self.region(),
            preset=self.preset_combo.currentData() if self.preset_combo is not None else None,
            channels=tuple(self.chosen_channels()),
            valid=self._windows(),
            lag_frame=self.frame_spin.value() - 1 if fixed else None,
            spot=self.spot_spin.value(),
            tc_min_c=self.tc_min_spin.value(),
            jump_k=self.jump_spin.value(),
            jump_hold_s=self.hold_spin.value(),
            below_range=self.below_check.isChecked(),
        )

    def _statistics_bytes(self) -> float:
        """Size of the frame statistics: two float32 values per pixel and time bin.

        Bins are the logger interval, or the time between the frames used when
        that is longer (as ``tcmatch.sample_recording`` chooses them).
        """
        x0, y0, x1, y1 = self.region() or (0, 0, self.metadata.width, self.metadata.height)
        step = self.table.interval if self.table is not None else 1.0
        fps = self.metadata.nominal_fps or 0.0
        if fps > 0:
            frame_interval = max(1, len(self.metadata.presets)) / fps  # one preset's frames on superframing
            step = step if frame_interval <= 1.01 * step else frame_interval
        bins = self.metadata.duration_seconds / max(step, 1e-3) + 1
        return 8.0 * bins * (x1 - x0) * (y1 - y0)

    def _update(self) -> None:
        self.frame_spin.setEnabled(self.time_combo.currentData() == "frame")
        self.current_button.setEnabled(self.time_combo.currentData() == "frame")
        size = self._statistics_bytes()
        gigabytes = f"{size / 1e9:.1f} GB" if size >= 1e8 else f"{size / 1e6:.0f} MB"
        valid = True
        if self.table is None:
            text = "Choose the TC file first."
            valid = False
        elif not self.chosen_channels():
            text = "Tick at least one thermocouple."
            valid = False
        elif not self.cache_check.isChecked() and size * MEMORY_MARGIN > MEMORY_LIMIT:
            text = (f"The frame statistics ({gigabytes}) are too large to hold in memory: draw a box ROI "
                    "around the battery, or keep the statistics in the results folder.")
            valid = False
        else:
            where = "kept in the results folder" if self.cache_check.isChecked() else "held in memory"
            text = (f"Reads all {self.metadata.num_frames:,} frames once (a few minutes for a long "
                    f"recording); frame statistics about {gigabytes}, {where}.")
            if size > LARGE_STATISTICS:
                text += " Large: a box ROI around the battery makes this faster and smaller."
        self.estimate_label.setText(text)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(valid)

    def _restore(self, previous: dict) -> None:
        """Settings of the previous run on this recording (the TC file is read again)."""
        if not previous:
            return
        options: MatchOptions | None = previous.get("options")
        if options is not None:
            for index in range(self.region_combo.count()):
                if self.region_combo.itemData(index) == (tuple(options.roi) if options.roi else None):
                    self.region_combo.setCurrentIndex(index)
            if self.preset_combo is not None and options.preset is not None:
                index = self.preset_combo.findData(options.preset)
                if index >= 0:
                    self.preset_combo.setCurrentIndex(index)
            if options.lag_frame is not None:
                self.time_combo.setCurrentIndex(self.time_combo.findData("frame"))
                self.frame_spin.setValue(options.lag_frame + 1)
            self.spot_spin.setValue(options.spot)
            self.tc_min_spin.setValue(options.tc_min_c)
            self.jump_spin.setValue(options.jump_k)
            self.hold_spin.setValue(options.jump_hold_s)
            self.below_check.setChecked(options.below_range)
        index = self.params_combo.findData(previous.get("parameters_from", "player"))
        self.params_combo.setCurrentIndex(max(0, index))
        self.cache_check.setChecked(previous.get("cache_dir") is not None or "cache_dir" not in previous)
        if previous.get("tc_file"):
            self.file_edit.setText(str(previous["tc_file"]))
            self._load(sheet=previous.get("sheet"))

    # --- accept ------------------------------------------------------------------------------

    def _out_dir(self) -> Path:
        return Path(self.out_edit.text().strip()).expanduser()

    def accept(self) -> None:
        try:
            self._windows()
        except ValueError as exc:
            MessageDialog.warning(self, "Use only (s)", str(exc))
            return
        if not self.out_edit.text().strip():
            MessageDialog.warning(self, "Results folder", "Choose a folder for the results.")
            return
        out = self._out_dir()
        recording = self.metadata.path.name
        outputs = [out / name for name in RESULT_FILES] + [out / f"{recording}.rois.json"]
        replace = confirm_replace(self, outputs, self._confirmed)
        if replace is None:
            return
        self._replace = [str(path) for path in replace]
        super().accept()

    def parameters(self) -> dict:
        out = self._out_dir()
        return {
            "kind": "tc",
            "recording": str(self.metadata.path),
            "tc_file": self.file_edit.text().strip(),
            "sheet": (self.table.sheet or None) if self.table is not None else None,
            "options": self.options(),
            "out_dir": str(out),
            "cache_dir": str(out / "cache") if self.cache_check.isChecked() else None,
            "parameters_from": str(self.params_combo.currentData()),
            "replace": list(self._replace),
            "channel_rows": [(name, checked, text) for name, (checked, text) in self._channel_rows().items()],
        }


class TcResultsDialog(FramelessDialog):
    """Each TC's pixel and emissivity, the summary and the figure; use them in the player or a workbook."""

    WORKBOOK = 2  # done() code: save the Excel workbook

    add_rois_requested = Signal()
    emissivity_requested = Signal(float)

    def __init__(self, output: RunOutput, out_dir: Path | None, parent=None) -> None:
        super().__init__("Emissivity from TCs: Results", parent)
        self.setMinimumWidth(720)
        self.output = output
        self.out_dir = out_dir
        result = output.result
        found = [ch for ch in result.channels if ch.match.found and ch.match.pixel is not None]
        valued = [ch for ch in result.channels if ch.eps is not None]

        given = " (given)" if result.lag_fixed else ""
        headline = QLabel(f"Logger time 0 = frame {result.ignition_frame + 1:,} (recording time "
                          f"{result.lag_s:.0f} s){given}. {len(found)} of {len(result.channels)} TC(s) found, "
                          f"{len(valued)} with an emissivity.")
        headline.setWordWrap(True)
        self.body.addWidget(headline)
        note = QLabel("Each emissivity is the median of that TC's usable events. Check them in the figure: "
                      "a flame or a loose TC shows as IR and TC drifting apart. TCs on differently painted "
                      "spots keep different values.")
        note.setObjectName("FieldLabel")
        note.setWordWrap(True)
        self.body.addWidget(note)

        self.tc_table = QTableWidget(len(result.channels), 7)
        self.tc_table.setHorizontalHeaderLabels(["TC", "Pixel (row, col)", "Match r", "Emissivity", "Events",
                                              "Range", "Status"])
        self.tc_table.verticalHeader().setVisible(False)
        self.tc_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tc_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.tc_table.horizontalHeader()
        for column in range(6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        for row, ch in enumerate(result.channels):
            m = ch.match
            pixel = f"{m.pixel[0]}, {m.pixel[1]}" if m.found and m.pixel is not None else "-"
            r = f"{m.r:.3f}" if m.found and math.isfinite(m.r) else (f"{m.own_r:.2f}" if math.isfinite(m.own_r)
                                                                       else "-")
            eps = f"{ch.eps[1]:.2f}" if ch.eps is not None else "-"
            spread = f"{ch.eps[0]:.2f} to {ch.eps[2]:.2f}" if ch.eps is not None and ch.eps_events > 1 else "-"
            cells = (ch.name, pixel, r, eps, str(ch.eps_events) if ch.eps is not None else "0", spread,
                     _status(ch))
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.tc_table.setItem(row, column, item)
            self.tc_table.setRowHeight(row, 30)
        self.tc_table.setMinimumHeight(min(260, 40 + 30 * len(result.channels)))
        self.body.addWidget(self.tc_table)

        tabs = QTabWidget()
        tabs.setObjectName("BottomTabs")
        summary = QTextEdit()
        summary.setObjectName("ReportText")
        summary.setReadOnly(True)
        summary.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        summary.setPlainText(_summary(output))
        tabs.addTab(summary, "Summary")
        figure = output.files.get("figure")
        if figure is not None and Path(figure).exists():
            pixmap = QPixmap(str(figure))
            if not pixmap.isNull():
                scroll = QScrollArea()
                scroll.setObjectName("PickerScroll")
                scroll.setWidgetResizable(False)
                image = QLabel()
                image.setPixmap(pixmap)
                image.setToolTip(f"Per TC, top to bottom: the time-offset search, the correlation map, IR and "
                                 f"TC with the events (green), the matching emissivity. Saved as {Path(figure).name}")
                scroll.setWidget(image)
                tabs.addTab(scroll, "Figure")
        tabs.setMinimumHeight(280)
        self.body.addWidget(tabs, 1)

        use_row = QHBoxLayout()
        use_row.addWidget(QLabel("Emissivity"))
        self.eps_combo = ChevronComboBox()
        if result.tcs_agree and result.common_eps is not None and len(valued) > 1:
            self.eps_combo.addItem(f"All TCs agree: {result.common_eps:.2f}", float(result.common_eps))
        for ch in valued:
            events = f"{ch.eps_events} events" if ch.eps_events > 1 else "1 event"
            self.eps_combo.addItem(f"{ch.name}: {ch.eps[1]:.2f} ({events})", float(ch.eps[1]))
        use_row.addWidget(self.eps_combo, 1)
        self.eps_button = QPushButton("Set in Player")
        self.eps_button.setToolTip("Use this emissivity in the Measurement panel (one value for the whole image)")
        self.eps_button.clicked.connect(self._use_emissivity)
        use_row.addWidget(self.eps_button)
        if not valued:
            self.eps_combo.addItem("No TC has an emissivity", None)
            self.eps_combo.setEnabled(False)
            self.eps_button.setEnabled(False)
        self.body.addLayout(use_row)

        actions = QHBoxLayout()
        self.rois_button = QPushButton("Add TC ROIs to Player")
        self.rois_button.setToolTip("An N × N box at each TC pixel (the block the fit used), and the ignition "
                                    "frame set to logger time 0")
        self.rois_button.clicked.connect(self._add_rois)
        self.workbook_button = QPushButton("Save Excel Workbook…")
        self.workbook_button.setToolTip("ROIs at the TC pixels, each starting at its TC's emissivity, with the "
                                        "TC readings on the TC Compare sheet")
        self.workbook_button.clicked.connect(lambda: self.done(self.WORKBOOK))
        shapes = roi_shapes(result)
        if not shapes:
            self.rois_button.setEnabled(False)
            self.workbook_button.setEnabled(False)
            self.workbook_button.setToolTip("No TC pixel was found")
        elif result.preset is not None:
            self.workbook_button.setEnabled(False)
            self.workbook_button.setToolTip("The Excel export cannot separate superframing presets yet")
        self.folder_button = QPushButton("Open Results Folder")
        self.folder_button.setProperty("variant", "ghost")
        self.folder_button.setEnabled(out_dir is not None)
        self.folder_button.clicked.connect(self._open_folder)
        actions.addWidget(self.rois_button)
        actions.addWidget(self.workbook_button)
        actions.addWidget(self.folder_button)
        actions.addStretch(1)
        close = QPushButton("Close")
        close.setProperty("accent", True)
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        self.body.addLayout(actions)
        self._add_size_grip()
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        height = int(available.height() * 0.85) if available is not None else 800
        self.resize(max(self.minimumWidth(), 820), min(height, 860))

    def _use_emissivity(self) -> None:
        value = self.eps_combo.currentData()
        if value is not None:
            self.emissivity_requested.emit(float(value))

    def _add_rois(self) -> None:
        self.add_rois_requested.emit()
        self.rois_button.setEnabled(False)
        self.rois_button.setText("ROIs Added")

    def _open_folder(self) -> None:
        if self.out_dir is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.out_dir)))


def _status(ch) -> str:
    """One line on a TC's outcome for the results table."""
    m = ch.match
    if not m.found or m.pixel is None:
        return ch.flags[0] if ch.flags else "not found"
    if not ch.trusted:
        return "not trusted: its IR and TC disagree beyond any emissivity"
    if ch.eps is None:
        return "no stretch to fit (see the summary)" if not ch.events else "no usable event"
    flags = f"; {ch.flags[0]}" if ch.flags else ""
    return "ok" + flags


def _summary(output: RunOutput) -> str:
    from .tcmatch import summary_text

    text = summary_text(output.result)
    p = output.parameters
    if p is not None:
        text += (f"\nObject parameters from the player: distance {p.get('distance', 0.0):g} m, reflected "
                 f"{p.get('reflected_temp', 0.0) - 273.15:.1f} °C, atmosphere {p.get('atmosphere_temp', 0.0) - 273.15:.1f} °C, "
                 f"humidity {100 * p.get('relative_humidity', 0.0):.0f} %.\n")
    return text
