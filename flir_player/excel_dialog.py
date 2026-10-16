# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Excel workbook export dialog: raw counts with live temperature formulas."""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
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
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .excel_export import ExportOptions, estimate, load_roi_set, roi_set_path
from .export_dialogs import _path_row, confirm_replace
from .geometry import roi_coordinates
from .jobs import unique_destination
from .models import RoiShape, VideoMetadata
from .settings import app_settings
from .widgets import ChevronComboBox, FramelessDialog, MessageDialog

_SHEETS = (("tc", "TC Compare"), ("summary", "Summary"), ("charts", "Charts"),
           ("roimap", "ROI map"), ("validation", "Validation (vs SDK)"))
_TC_FILE_KEY = "excel/tc_file"
_TC_PAIRS_KEY = "excel/tc_pairs"


def _duplicate_names(names) -> list[str]:
    names = tuple(names)
    return sorted({name for name in names if names.count(name) > 1})


def parse_tc_pairs(text: str, tc_names, roi_names) -> tuple[dict[str, str], tuple[str, ...]]:
    """Parse exact, case-sensitive TC = ROI names; return all errors for the status line."""
    roi_names = tuple(roi_names)
    pairs, errors = {}, []
    seen_tc, seen_roi = set(), set()
    for item in re.split(r"[,;]", text):
        if not item.strip():
            continue
        parts = [part.strip() for part in item.split("=")]
        if len(parts) != 2 or not all(parts):
            errors.append(f"Use TC name = ROI name for {item.strip()!r}.")
            continue
        tc, roi = parts
        if tc not in tc_names:
            errors.append(f"Unknown TC: {tc}.")
        if roi not in roi_names:
            errors.append(f"Unknown ROI: {roi}.")
        if tc in seen_tc:
            errors.append(f"TC {tc} is paired twice.")
        if roi in seen_roi:
            errors.append(f"ROI {roi} is paired twice.")
        seen_tc.add(tc)
        seen_roi.add(roi)
        pairs[tc] = roi
    repeated = _duplicate_names(roi_names)
    if repeated:
        errors.append(f"More than one ROI is named {', '.join(repeated)}: give them different names.")
    if not pairs and not errors:
        errors.append("Enter at least one pair, such as T1 = Cell 3.")
    if len(pairs) > 10:
        errors.append("TC Compare holds at most 10 TC columns; pair no more than 10 TCs.")
    return pairs, tuple(errors)


def _default_tc_pairs(tc_names, roi_names) -> str:
    """The lab's top-face TCs, by cell number, only where the zone name is unambiguous."""
    pairs = []
    for tc, number in (("T1", 3), ("T2", 6), ("T3", 9)):
        matches = [name for name in roi_names if re.fullmatch(rf".+ {number}", name)]
        if tc in tc_names and len(matches) == 1:
            pairs.append(f"{tc} = {matches[0]}")
    return ", ".join(pairs)


def _panel(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("RangePanel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 10, 14, 12)
    layout.setSpacing(6)
    label = QLabel(title)
    label.setObjectName("FieldLabel")
    layout.addWidget(label)
    return frame, layout


class ExcelExportDialog(FramelessDialog):
    """Choose recordings, time range, sampling and workbook contents."""

    def __init__(self, metadata: VideoMetadata, rois: tuple[RoiShape, ...], current_frame: int,
                 ignition_frame: int | None = None, tc_run=None, parent=None) -> None:
        super().__init__("Export Excel Workbook", parent)
        self.setMinimumWidth(640)
        self.metadata = metadata
        self.rois = tuple(rois)
        self.tc_run = tc_run  # this session's TC fit of the recording (tcmatch.RunOutput), or None
        self._others: list[dict] = []  # added recordings: path, label, rois, ignition
        self._replace: list[str] = []
        self._confirmed: list[str] = []
        self._tc_table = None
        self._tc_clock = None
        self._tc_request = 0
        self._tc_read_path = ""
        self._tc_read_sheet = None
        self._tc_reading = False
        self._tc_error = "Choose the logger's export of the TC readings."
        self._tc_auto_pairs = ""
        self._tc_cached = None
        layout = self.body
        # Panels scroll on small screens; output, estimate and buttons stay put.
        scroll = QScrollArea()
        scroll.setObjectName("PickerScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("PickerContent")
        panels = QVBoxLayout(content)
        panels.setContentsMargins(0, 0, 4, 0)
        panels.setSpacing(10)
        scroll.setWidget(content)

        intro = QLabel("Raw counts at the ROIs, with temperatures as live Excel formulas (change emissivity "
                       "in the workbook). Time is seconds from each recording's ignition frame.")
        intro.setWordWrap(True)
        panels.addWidget(intro)

        # recordings
        frame, box = _panel("Recordings")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Recording", "ROIs", "Ignition frame"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(92)
        self.table.setMaximumHeight(150)
        box.addWidget(self.table)
        buttons = QHBoxLayout()
        add = QPushButton("Add recording…")
        add.setToolTip("Another camera or part of the same test. Its ROIs come from the ROI set saved "
                       "next to it (Export → Save ROI set… while it is open).")
        add.clicked.connect(self._add_recording)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove_recording)
        self.use_current = QPushButton("Ignition = current frame")
        self.use_current.setToolTip("Set the open recording's ignition (t = 0) to the frame on screen")
        self.use_current.clicked.connect(lambda: self.ignition_spin.setValue(current_frame + 1))
        buttons.addWidget(add)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        buttons.addWidget(self.use_current)
        box.addLayout(buttons)
        panels.addWidget(frame)

        self.ignition_spin = QSpinBox()
        self.ignition_spin.setRange(1, metadata.num_frames)
        self.ignition_spin.setValue((ignition_frame if ignition_frame is not None else current_frame) + 1)
        self.ignition_spin.setToolTip("Frame number (as shown in the player) that is t = 0 s")
        self._add_row(metadata.path, metadata.camera_model.replace("FLIR ", "") or metadata.path.stem,
                      len(self.rois), current=True)

        # time and sampling
        frame, box = _panel("Time range and sampling")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        self.start_check = QCheckBox("From")
        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(-1e7, 1e7)
        self.start_spin.setDecimals(1)
        self.start_spin.setSuffix(" s")
        self.start_spin.setValue(-60.0)
        self.start_check.setToolTip("Unchecked: from the first frame of the recordings")
        self.end_check = QCheckBox("To")
        self.end_spin = QDoubleSpinBox()
        self.end_spin.setRange(-1e7, 1e7)
        self.end_spin.setDecimals(1)
        self.end_spin.setSuffix(" s")
        self.end_spin.setValue(max(0.0, round(metadata.duration_seconds, 1)))
        self.end_check.setToolTip("Unchecked: to the last frame of the recordings")
        grid.addWidget(self.start_check, 0, 0)
        grid.addWidget(self.start_spin, 0, 1)
        grid.addWidget(QLabel("from ignition"), 0, 2)
        grid.addWidget(self.end_check, 1, 0)
        grid.addWidget(self.end_spin, 1, 1)
        grid.addWidget(QLabel("from ignition"), 1, 2)
        self.sampling_combo = ChevronComboBox()
        self.sampling_combo.addItem("Every … seconds", "seconds")
        self.sampling_combo.addItem("Every frame", "frame")
        self.sampling_combo.setToolTip(
            "Every frame: one row per frame of the first recording with the frame-number time base. "
            "With camera timestamps the rows follow its mean frame interval and each takes the nearest "
            "frame, so a frame can repeat or be skipped where the clock has gaps.")
        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(0.001, 3600.0)
        self.step_spin.setDecimals(3)
        self.step_spin.setValue(1.0)
        self.step_spin.setSuffix(" s")
        grid.addWidget(QLabel("Sampling"), 2, 0)
        grid.addWidget(self.sampling_combo, 2, 1)
        grid.addWidget(self.step_spin, 2, 2)
        self.time_base_combo = ChevronComboBox()
        self.time_base_combo.addItem("Frame number ÷ frame rate (recommended)", "frames")
        self.time_base_combo.addItem("Camera timestamps", "clock")
        self.time_base_combo.setToolTip("Camera clocks can jump (e.g. 1 s steps); the frame count is "
                                        "smooth for recordings without dropped frames.")
        grid.addWidget(QLabel("Time base"), 3, 0)
        grid.addWidget(self.time_base_combo, 3, 1, 1, 2)
        box.addLayout(grid)
        panels.addWidget(frame)

        # contents
        frame, box = _panel("Workbook contents")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        stats_row = QHBoxLayout()
        self.extremes_check = QCheckBox("Max and min")
        self.extremes_check.setToolTip("Hottest and coldest pixel of each area. Every column is a formula Excel "
                                       "reads when it opens the workbook: leave them out for many ROIs.")
        areas = sum(1 for roi in self.rois if roi.kind in ("rect", "ellipse"))
        self.extremes_check.setChecked(areas <= 8)
        self.median_check = QCheckBox("Median")
        self.p95_check = QCheckBox("95th percentile")
        stats_row.addWidget(QLabel("Mean, plus"))
        stats_row.addWidget(self.extremes_check)
        stats_row.addWidget(self.median_check)
        stats_row.addWidget(self.p95_check)
        stats_row.addStretch(1)
        grid.addWidget(QLabel("Area ROIs"), 0, 0)
        grid.addLayout(stats_row, 0, 1)
        self.unit_combo = ChevronComboBox()
        for unit in ("°C", "K", "°F"):
            self.unit_combo.addItem(unit, unit)
        self.params_combo = ChevronComboBox()
        self.params_combo.addItem("Parameters from each file", "file")
        self.params_combo.addItem("Parameters from the player (open recording)", "player")
        self.params_combo.setToolTip("Starting emissivity, reflected temperature, distance etc. in the "
                                     "workbook; all remain editable there.")
        options_row = QHBoxLayout()
        options_row.addWidget(self.unit_combo)
        options_row.addWidget(self.params_combo, 1)
        grid.addWidget(QLabel("Unit, start values"), 1, 0)
        grid.addLayout(options_row, 1, 1)
        self.means_combo = ChevronComboBox()
        self.means_combo.addItem("From every pixel (exact; larger file)", "pixels")
        self.means_combo.addItem("From the mean signal (smaller file)", "signal")
        self.means_combo.setToolTip(
            "Every pixel: the mean of the pixels' temperatures, recomputed for any emissivity (each pixel is stored). "
            "Mean signal: the temperature of the area's mean counts; the same for an even area, and what the TC "
            "comparison matches. For many ROIs (cell zones) it keeps the workbook small.")
        self.means_combo.setCurrentIndex(1 if areas > 8 else 0)
        grid.addWidget(QLabel("Area means"), 2, 0)
        grid.addWidget(self.means_combo, 2, 1)
        grid.setColumnStretch(1, 1)
        box.addLayout(grid)
        sheets = QGridLayout()
        self.sheet_checks = {}
        for k, (key, label) in enumerate(_SHEETS):
            check = QCheckBox(label)
            check.setChecked(True)
            self.sheet_checks[key] = check
            sheets.addWidget(check, k // 3, k % 3)
        box.addLayout(sheets)
        self.sidecar_check = QCheckBox("Save the ROI set and ignition frame next to the recording")
        self.sidecar_check.setChecked(True)
        box.addWidget(self.sidecar_check)
        panels.addWidget(frame)

        # thermocouples from a fit or directly from the logger file
        frame, box = _panel("Thermocouples")
        self.tc_check = QCheckBox("Fill TC Compare")
        self.tc_check.setToolTip(
            "Fill the logger data and pair TCs with zones. From a fit, Use = 1 marks the seconds the fit used. "
            "From a file, it marks valid TC readings at least 50 °C with no flame jump. "
            "One emissivity drives every ROI.")
        self.tc_source_combo = ChevronComboBox()
        if tc_run is not None:
            self.tc_source_combo.addItem("From the last TC fit", "fit")
        self.tc_source_combo.addItem("From a TC file, timed by the clocks", "file")
        self.tc_file_box = QWidget()
        tc_form = QFormLayout(self.tc_file_box)
        tc_form.setContentsMargins(0, 0, 0, 0)
        row, self.tc_file_edit, tc_browse = _path_row("Choose the TC file")
        saved_path = app_settings().value(_TC_FILE_KEY, "", type=str)
        fit_path = getattr(tc_run, "tc_file", None)
        self.tc_file_edit.setText(str(fit_path) if fit_path else
                                  saved_path if saved_path and Path(saved_path).is_file() else "")
        tc_browse.clicked.connect(self._browse_tc)
        tc_form.addRow("TC file", row)
        self.tc_sheet_combo = ChevronComboBox()
        self.tc_sheet_combo.setEnabled(False)
        tc_form.addRow("Sheet", self.tc_sheet_combo)
        self.tc_pairs_edit = QLineEdit()
        self.tc_pairs_edit.setPlaceholderText("T1 = Cell 3, T2 = Cell 6, T3 = Cell 9")
        self.tc_pairs_edit.setToolTip("TC name = ROI name, separated by commas or semicolons. "
                                     "Use the exact names in the TC file and ROI list, including capitals.")
        tc_form.addRow("TCs and zones", self.tc_pairs_edit)
        self.tc_label = QLabel()
        self.tc_label.setObjectName("FieldLabel")
        self.tc_label.setWordWrap(True)
        box.addWidget(self.tc_check)
        box.addWidget(self.tc_source_combo)
        box.addWidget(self.tc_file_box)
        box.addWidget(self.tc_label)
        panels.addWidget(frame)
        self._tc_pairing = self._pair_tcs()
        use_fit = tc_run is not None and not self._tc_usable()
        self.tc_source_combo.setCurrentIndex(self.tc_source_combo.findData("fit" if use_fit else "file"))
        self.tc_check.setChecked(True)
        if use_fit:
            self._time_from_fit()
        panels.addStretch(1)
        layout.addWidget(scroll, 1)

        form = QFormLayout()
        default = unique_destination(metadata.path.parent / f"{metadata.path.stem}.xlsx")
        row, self.path_edit, browse = _path_row("Choose workbook file")
        self.path_edit.setText(str(default))
        browse.clicked.connect(self._browse)
        form.addRow("Workbook", row)
        layout.addLayout(form)
        self.estimate_label = QLabel()
        self.estimate_label.setObjectName("FieldLabel")
        self.estimate_label.setWordWrap(True)
        layout.addWidget(self.estimate_label)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText("Export")
        ok_button.setProperty("accent", True)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)
        self._add_size_grip()

        for widget in (self.start_check, self.end_check, self.extremes_check, self.median_check, self.p95_check,
                       self.tc_check):
            widget.toggled.connect(self._update)
        self.means_combo.currentIndexChanged.connect(self._update)
        self.time_base_combo.currentIndexChanged.connect(self._update)
        self.ignition_spin.valueChanged.connect(self._update)
        for widget in (self.start_spin, self.end_spin, self.step_spin):
            widget.valueChanged.connect(self._update)
        self.sampling_combo.currentIndexChanged.connect(self._update)
        for check in self.sheet_checks.values():
            check.toggled.connect(self._update)
        self.tc_source_combo.currentIndexChanged.connect(self._tc_source_changed)
        self.tc_file_edit.textChanged.connect(self._tc_path_changed)
        self.tc_file_edit.editingFinished.connect(self._tc_file_edited)
        self.tc_sheet_combo.currentTextChanged.connect(lambda sheet: self._load_tc(sheet=sheet))
        self.tc_pairs_edit.textChanged.connect(self._update)
        self._tc_source_changed()
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry().height() if screen is not None else 900
        fixed = sum(self.body.itemAt(i).sizeHint().height() for i in range(self.body.count())
                    if self.body.itemAt(i).widget() is not scroll)
        wanted = content.sizeHint().height() + fixed + 16 * self.body.count() + 40
        self.resize(max(self.minimumWidth(), content.sizeHint().width() + 48), min(wanted, int(available * 0.9)))

    # --- recordings ------------------------------------------------------------------

    def _add_row(self, path: Path, label: str, rois: int, *, current: bool = False,
                 ignition: int | None = None) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, 40)
        self.table.setItem(row, 0, QTableWidgetItem(label))
        item = QTableWidgetItem(("● " if current else "") + str(path))
        item.setToolTip("The open recording" if current else str(path))
        self.table.setItem(row, 1, item)
        self.table.setItem(row, 2, QTableWidgetItem(str(rois)))
        if current:
            self.table.setCellWidget(row, 3, self.ignition_spin)
        else:
            spin = QSpinBox()
            spin.setRange(1, 10_000_000)
            spin.setValue((ignition or 0) + 1)
            self.table.setCellWidget(row, 3, spin)
        for column in (1, 2):
            cell = self.table.item(row, column)
            cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def _add_recording(self) -> None:
        from .main_window import _file_dialog_filter
        path, _ = QFileDialog.getOpenFileName(self, "Add recording", str(self.metadata.path.parent),
                                              _file_dialog_filter())
        if not path:
            return
        recording = Path(path).resolve()
        if recording == self.metadata.path.resolve() or any(o["path"] == recording for o in self._others):
            MessageDialog.information(self, "Add recording", "That recording is already in the list.")
            return
        sidecar = roi_set_path(recording)
        if not sidecar.exists():
            chosen, _ = QFileDialog.getOpenFileName(
                self, f"ROI set for {recording.name}", str(recording.parent),
                "ROI sets (*.rois.json *.json);;All files (*.*)")
            if not chosen:
                MessageDialog.information(
                    self, "Add recording",
                    f"{recording.name} has no saved ROI set. Open it in the player, draw its ROIs, "
                    "set the ignition frame and use Export → Save ROI set…, then add it here.")
                return
            sidecar = Path(chosen)
        try:
            roi_set = load_roi_set(sidecar)
        except (OSError, ValueError, KeyError) as exc:
            MessageDialog.warning(self, "Add recording", f"Could not read the ROI set: {exc}")
            return
        if not roi_set.rois:
            MessageDialog.warning(self, "Add recording", "The ROI set contains no ROIs.")
            return
        entry = {"path": recording, "rois": roi_set.rois, "ignition": roi_set.ignition_frame or 0}
        self._others.append(entry)
        self._add_row(recording, recording.stem, len(roi_set.rois), ignition=entry["ignition"])
        self._update()

    def _remove_recording(self) -> None:
        row = self.table.currentRow()
        if row <= 0:
            return  # the open recording stays
        self.table.removeRow(row)
        del self._others[row - 1]
        self._update()

    # --- TC file -------------------------------------------------------------------------

    def _tc_source_changed(self) -> None:
        if self.tc_source_combo.currentData() == "file" and not self._tc_read_path:
            self._load_tc(sheet=None)
        else:
            self._update()

    def _browse_tc(self) -> None:
        from .tcdata import TEXT_SUFFIXES, WORKBOOK_SUFFIXES

        start = self.tc_file_edit.text().strip() or str(app_settings().value("tcmatch/last_dir", "", type=str)
                                                       or self.metadata.path.parent)
        patterns = " ".join(f"*{suffix}" for suffix in WORKBOOK_SUFFIXES + TEXT_SUFFIXES)
        path, _ = QFileDialog.getOpenFileName(self, "Choose the TC file", start,
                                              f"TC logger data ({patterns});;All files (*.*)")
        if path:
            self.tc_file_edit.setText(path)
            self._load_tc(sheet=None)

    def _tc_path_changed(self) -> None:
        # Invalidate immediately, including while typing: a previous read must never land on this path.
        self._tc_request += 1
        self._tc_table = self._tc_clock = self._tc_cached = None
        self._tc_reading = False
        self._tc_read_path = ""
        self._tc_error = "Choose the logger's export of the TC readings."
        self.tc_sheet_combo.blockSignals(True)
        self.tc_sheet_combo.clear()
        self.tc_sheet_combo.blockSignals(False)
        self._update()

    def _tc_file_edited(self) -> None:
        if self.tc_file_edit.text().strip() != self._tc_read_path:
            self._load_tc(sheet=None)

    def _load_tc(self, sheet: str | None) -> None:
        # Imported here because tc_dialog also uses this module's panel helper.
        from .tc_dialog import _TableReader, _keep_until_done

        self._tc_request += 1
        self._tc_read_path = self.tc_file_edit.text().strip()
        self._tc_read_sheet = sheet
        self._tc_table = self._tc_clock = self._tc_cached = None
        path = Path(self._tc_read_path).expanduser() if self._tc_read_path else None
        self._tc_reading = path is not None and path.is_file()
        self._tc_error = ("Reading the TC file…" if self._tc_reading else "No such file." if path is not None
                          else "Choose the logger's export of the TC readings.")
        self._update()
        if self._tc_reading:
            reader = _TableReader(self._tc_request, path, sheet)
            reader.read.connect(self._tc_loaded)
            _keep_until_done(reader)
            reader.start()

    def _tc_loaded(self, request: int, names, table, error: str) -> None:
        if request != self._tc_request:
            return
        from .tcdata import suggest_sheet
        from .tcmatch import clock_estimate

        self._tc_reading = False
        self._tc_table = table
        self._tc_error = f"Could not read TC data: {error}" if table is None else ""
        sheets = list(names or [])
        selected = table.sheet if table is not None and table.sheet else self._tc_read_sheet or suggest_sheet(sheets)
        self.tc_sheet_combo.blockSignals(True)
        self.tc_sheet_combo.clear()
        self.tc_sheet_combo.addItems(sheets)
        if selected in sheets:
            self.tc_sheet_combo.setCurrentText(selected)
        self.tc_sheet_combo.blockSignals(False)
        if table is not None:
            self._tc_clock = clock_estimate(table.clock_zero, self.metadata.start_time, table.times,
                                            self.metadata.duration_seconds, logger_note=table.clock_note)
            roi_names = tuple(roi.name for roi in self.rois)
            # Keep text edited in this dialog, including errors the user still needs to fix.
            if self.tc_pairs_edit.text() == self._tc_auto_pairs:
                saved = app_settings().value(_TC_PAIRS_KEY, "", type=str)
                _pairs, errors = parse_tc_pairs(saved, table.names, roi_names)
                self._tc_auto_pairs = saved if not errors else _default_tc_pairs(table.names, roi_names)
                self.tc_pairs_edit.setText(self._tc_auto_pairs)
            if (self._tc_clock is not None and self.tc_source_combo.currentData() == "file"
                    and self.tc_check.isChecked() and not self.metadata.presets
                    and self.time_base_combo.currentData() == "frames" and self.metadata.nominal_fps > 0):
                fps = self.metadata.nominal_fps
                frame = min(max(round(self._tc_clock.lag_s * fps), 0), self.metadata.num_frames - 1)
                self._time_from_logger(table, self._tc_clock.lag_s, fps, frame)
        self._update()

    def _file_pairs(self):
        return parse_tc_pairs(self.tc_pairs_edit.text(), self._tc_table.names,
                              (roi.name for roi in self.rois))

    def _file_clock_text(self) -> str:
        if self._tc_table is None:
            return ""
        clock = self._tc_clock
        if clock is None:
            why = ("the TC file does not give the logger's clock" if self._tc_table.clock_zero is None
                   else "the recording has no clock" if self.metadata.start_time is None
                   else "the clocks put the logger's data outside this recording")
            return f"Clocks: {why}. Time 0 at the ignition frame is taken as logger time 0."
        fps = self.metadata.nominal_fps if not self.metadata.presets else 0.0
        minutes, seconds = divmod(round(abs(clock.lag_s)), 60)
        sign = "minus " if clock.lag_s < 0 else ""
        frame_text = f"frame {round(clock.lag_s * fps) + 1:,} (" if fps > 0 else ""
        text = (f"Clocks: logger time 0 at {frame_text}{sign}{minutes} min {seconds} s into the recording"
                + (")." if fps > 0 else "."))
        if fps > 0:
            ignition = self.ignition_spin.value()
            logger = (ignition - 1) / fps - clock.lag_s
            text += (" Time 0 is logger time 0." if abs(logger) <= 0.5 / fps + 1e-9 else
                     f" Time 0 (frame {ignition:,}) is logger time {logger:,.1f} s.")
        return text

    # --- options -------------------------------------------------------------------------

    def _pair_tcs(self):
        """Which ROI each TC pixel of the last fit lies in (``zones.Pairing``), or None without a fit."""
        if self.tc_run is None:
            return None
        from .zones import pair_tcs

        result = self.tc_run.result
        pixels = {ch.name: tuple(ch.match.pixel) for ch in result.channels[:10]
                  if ch.match.found and ch.match.pixel is not None}
        return pair_tcs(pixels, self.rois, self.metadata.height, self.metadata.width)

    def _fit_origin(self) -> int:
        """The frame (0-based) of the fit's logger time 0, as the workbook can use it (inside the recording)."""
        from .tcmatch import workbook_origin

        return workbook_origin(self.tc_run.result)[0]

    def _time_from_fit(self) -> None:
        """Time 0 = logger time 0 and From/To = the logger's first and last second, inside the recording.

        Filling TC Compare from the fit works with any ignition frame (the logger offset follows), but
        then the workbook's seconds are the TC file's seconds and the range is where TCs exist.
        """
        result, table = self.tc_run.result, self.tc_run.table
        frame = self._fit_origin()
        self._time_from_logger(table, result.lag_s, result.fps, frame)

    def _time_from_logger(self, table, lag_s: float, fps: float, frame: int) -> None:
        self.ignition_spin.setValue(frame + 1)
        offset = float(lag_s - frame / fps)  # workbook time = logger time + offset
        first, last = -frame / fps, (self.metadata.num_frames - 1 - frame) / fps
        times = table.times[np.isfinite(table.times)]
        if not times.size:
            return
        # the spins keep 0.1 s: the logger's seconds rounded (4805.996 is 4806.0), inside the recording
        low, high = math.ceil(first * 10) / 10, math.floor(last * 10) / 10
        start = min(max(round(float(times[0]) + offset, 1), low), high)
        end = min(max(round(float(times[-1]) + offset, 1), low), high)
        self.start_check.setChecked(True)
        self.start_spin.setValue(start)
        self.end_check.setChecked(True)
        self.end_spin.setValue(end)

    def _tc_usable(self) -> str:
        """Why the selected source cannot fill TC Compare now ("" when it can)."""
        from_file = self.tc_source_combo.currentData() == "file"
        if self.metadata.presets or (self.tc_run is not None and self.tc_run.result.preset is not None):
            return "The workbook cannot separate the presets of a superframing recording yet."
        if not self.sheet_checks["tc"].isChecked():
            return "Tick the TC Compare sheet to fill it."
        if self.time_base_combo.currentData() != "frames":
            return "The TC time offset is on the frame-number time base; choose that time base."
        if from_file:
            repeated = _duplicate_names(roi.name for roi in self.rois)
            if repeated and self._tc_table is None:
                return f"More than one ROI is named {', '.join(repeated)}: give them different names."
            if self._tc_table is None:
                return self._tc_error
            if not math.isfinite(self.metadata.nominal_fps) or self.metadata.nominal_fps <= 0:
                return "The recording has no frame rate to place the TC readings."
            pairs, errors = self._file_pairs()
            if errors:
                return " ".join(errors)
        else:
            if self.tc_run is None:
                return "No TC fit of this recording in this session (Measurement → Fit Emissivity from TCs…)."
            if not self._tc_pairing.pairs:
                return "No TC pixel of the last fit lies in an ROI."
            if self._tc_pairing.duplicates:
                return (f"More than one ROI is named {', '.join(self._tc_pairing.duplicates)}: the workbook pairs ROIs "
                        "and TCs by name, so give them different names (e.g. split again with other names).")
            pairs = self._tc_pairing.pairs
        paired = set(pairs.values())
        for other in self._others:  # the pairing applies by name to added recordings too
            names = [roi.name for roi in other["rois"]]
            repeated = sorted(name for name in paired if names.count(name) > 1)
            if repeated:
                return (f"{other['path'].name} has more than one ROI named {', '.join(repeated)}: the workbook pairs "
                        "ROIs and TCs by name.")
        return ""

    def tc_prefill(self):
        """The TC Compare contents for the open recording's ROIs, or None."""
        if not self.tc_check.isChecked() or self._tc_usable():
            return None
        from .tcmatch import clock_prefill, tc_prefill

        if self.tc_source_combo.currentData() == "file":
            pairs, _errors = self._file_pairs()
            fps = self.metadata.nominal_fps
            frame = self.ignition_spin.value() - 1
            lag = self._tc_clock.lag_s if self._tc_clock is not None else frame / fps
            key = (self._tc_request, tuple(pairs.items()), lag, fps, frame)
            if self._tc_cached is None or self._tc_cached[0] != key:
                self._tc_cached = (key, clock_prefill(self._tc_table, lag_s=lag, fps=fps, frame=frame, pairs=pairs))
            return self._tc_cached[1]

        return tc_prefill(self.tc_run.result, self.tc_run.table, rois=self.rois,
                          frame=self.ignition_spin.value() - 1, size=(self.metadata.width, self.metadata.height))

    def options(self) -> ExportOptions:
        extra = tuple(name for name, check in (("median", self.median_check), ("p95", self.p95_check))
                      if check.isChecked())
        return ExportOptions(
            start_s=self.start_spin.value() if self.start_check.isChecked() else None,
            end_s=self.end_spin.value() if self.end_check.isChecked() else None,
            step_s=float(self.step_spin.value()),
            every_frame=self.sampling_combo.currentData() == "frame",
            time_base=str(self.time_base_combo.currentData()),
            extra_stats=extra,
            extremes=self.extremes_check.isChecked(),
            area_means=str(self.means_combo.currentData()),
            sheets=frozenset(key for key, check in self.sheet_checks.items() if check.isChecked()),
            unit=str(self.unit_combo.currentData()),
            tc_prefill=self.tc_prefill(),
        )

    def _update(self) -> None:
        self.start_spin.setEnabled(self.start_check.isChecked())
        self.end_spin.setEnabled(self.end_check.isChecked())
        self.step_spin.setEnabled(self.sampling_combo.currentData() == "seconds")
        reason = self._tc_usable()
        from_file = self.tc_source_combo.currentData() == "file"
        self.tc_file_box.setVisible(from_file)
        self.tc_sheet_combo.setEnabled(not self._tc_reading and self.tc_sheet_combo.count() > 1)
        self.tc_check.setEnabled(not reason or (from_file and self._tc_reading))
        if from_file:
            parts = []
            if self._tc_table is not None:
                pairs, errors = self._file_pairs()
                if not errors:
                    parts.append("Pairs: " + " · ".join(f"{tc} -> {roi}" for tc, roi in pairs.items()) + ".")
                parts.append(self._file_clock_text())
            if reason:
                parts.append(reason)
                if not self._tc_reading:
                    parts.append("TC Compare will not be filled.")
            elif not self.tc_check.isChecked():
                parts.append("Unticked: paste the logger data on TC Compare yourself.")
            if "A700" in self.metadata.camera_model:
                parts.append("The A700 clock can run slow. The TC and zone curves on TC Compare show any shift; "
                             "time 0 can be moved by hand.")
            self.tc_label.setText(" ".join(parts))
            self.tc_label.setToolTip(self._tc_clock.note if self._tc_clock is not None else
                                     self._tc_table.clock_note if self._tc_table is not None else "")
        elif reason:
            self.tc_label.setText(reason)
        elif self.tc_check.isChecked():
            result = self.tc_run.result
            ignition = self.ignition_spin.value()
            logger = (ignition - 1) / result.fps - result.lag_s  # logger time at the workbook's time 0
            time0 = (f" Time 0 is logger time 0 (frame {ignition:,})." if abs(logger) < 0.5 / result.fps else
                     f" Time 0 (frame {ignition:,}) is logger time {logger:,.1f} s.")
            self.tc_label.setText("Pairs: " + self._tc_pairing.describe() + "." + time0)
        else:
            self.tc_label.setText("Unticked: paste the logger data on TC Compare yourself.")
        options = self.options()
        fps = self.metadata.nominal_fps or 30.0
        rate = self.metadata.rate
        if options.time_base == "clock" and rate is not None and rate.clock_fps > 0:
            fps = rate.clock_fps  # rows follow the timestamps' own mean rate
        ignition = self.ignition_spin.value() - 1
        start = options.start_s if options.start_s is not None else -ignition / fps
        end = options.end_s if options.end_s is not None else (self.metadata.num_frames - 1 - ignition) / fps
        step = 1.0 / fps if options.every_frame else options.step_s
        rows = max(0, int((end - start) / step) + 1) if end >= start else 0
        pixels = []
        for roi in self.rois:
            ys, _ = roi_coordinates(roi, self.metadata.height, self.metadata.width)
            pixels.append((roi.kind, int(ys.size)))
        for other in self._others:
            pixels += [(roi.kind, 50) for roi in other["rois"]]
        size = estimate(rows, pixels, options)
        cells = size["cells"]
        cells_text = f"{cells / 1e6:.1f} million" if cells >= 1e6 else f"{cells:,.0f}"
        megabytes = size["megabytes"]
        text = (f"About {rows:,} rows, {cells_text} cells, "
                + (f"~{megabytes:.0f} MB." if megabytes >= 1 else "< 1 MB."))
        valid = rows > 0 and bool(self.rois)
        if rows > 1_048_000:
            text += " Too many rows for Excel: sample less often."
            valid = False
        elif size["cells"] > 8e6:
            text += " Large: Excel may be slow; consider a longer interval."
        if not self.rois:
            text = "Draw at least one ROI on the open recording first."
        self.estimate_label.setText(text)
        if from_file and self._tc_reading and self.tc_check.isChecked() and self.sheet_checks["tc"].isChecked():
            valid = False
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(valid)

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Choose workbook file", self.path_edit.text(),
                                              "Excel workbook (*.xlsx)")
        if path:
            self._confirmed.append(path)
            self.path_edit.setText(path)

    def _dest(self) -> Path:
        dest = Path(self.path_edit.text()).expanduser()
        return dest if dest.suffix.lower() == ".xlsx" else dest.with_suffix(".xlsx")

    def accept(self) -> None:
        from .jobs import aliases

        if self.tc_source_combo.currentData() == "file":
            self._tc_file_edited()
        if not self.button_box.button(QDialogButtonBox.StandardButton.Ok).isEnabled():
            return
        tc_path = (self.tc_file_edit.text().strip() if self.tc_source_combo.currentData() == "file"
                   else getattr(self.tc_run, "tc_file", None))
        if tc_path and aliases(self._dest(), Path(tc_path).expanduser()):
            MessageDialog.warning(self, "Choose workbook file", "That is the TC file. Choose another name for the workbook.")
            return
        replace = confirm_replace(self, [self._dest()], self._confirmed)
        if replace is None:
            return
        self._replace = [str(path) for path in replace]
        if self.tc_source_combo.currentData() == "file" and self.tc_prefill() is not None:
            settings = app_settings()
            settings.setValue(_TC_FILE_KEY, str(Path(self._tc_read_path).expanduser().resolve()))
            settings.setValue(_TC_PAIRS_KEY, self.tc_pairs_edit.text().strip())
        super().accept()

    def done(self, result: int) -> None:
        self._tc_request += 1  # a reader may outlive the dialog, but must not change a closed dialog's choices
        self._tc_reading = False
        super().done(result)

    def parameters(self) -> dict:
        labels = [self.table.item(r, 0).text().strip() for r in range(self.table.rowCount())]
        ignitions = [self.table.cellWidget(r, 3).value() - 1 for r in range(self.table.rowCount())]
        sources = [{"path": str(self.metadata.path), "label": labels[0], "rois": self.rois,
                    "ignition_frame": ignitions[0], "current": True}]
        for k, other in enumerate(self._others, start=1):
            sources.append({"path": str(other["path"]), "label": labels[k], "rois": other["rois"],
                            "ignition_frame": ignitions[k], "current": False})
        return {
            "kind": "excel",
            "dest": str(self._dest()),
            "sources": sources,
            "options": self.options(),
            "parameters_from": str(self.params_combo.currentData()),
            "save_sidecar": self.sidecar_check.isChecked(),
            "replace": list(self._replace),
        }
