# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Excel workbook export dialog: raw counts with live temperature formulas."""

from __future__ import annotations

from pathlib import Path

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
from .widgets import ChevronComboBox, FramelessDialog, MessageDialog

_SHEETS = (("tc", "TC Compare"), ("summary", "Summary"), ("charts", "Charts"),
           ("roimap", "ROI map"), ("validation", "Validation (vs SDK)"))


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

        # thermocouples from this session's TC fit
        frame, box = _panel("Thermocouples")
        self.tc_check = QCheckBox("Fill TC Compare from the last TC fit")
        self.tc_check.setToolTip(
            "The logger data on TC Compare at the fitted time offset, each TC paired with the smallest ROI holding "
            "its pixel (e.g. its cell zone), and Use = 1 on the seconds the fit used (no flames on the TC, IR in "
            "range). One emissivity then drives every ROI (no per-ROI values).")
        self.tc_label = QLabel()
        self.tc_label.setObjectName("FieldLabel")
        self.tc_label.setWordWrap(True)
        box.addWidget(self.tc_check)
        box.addWidget(self.tc_label)
        panels.addWidget(frame)
        self._tc_pairing = self._pair_tcs()
        self.tc_check.setChecked(self._tc_pairing is not None and bool(self._tc_pairing.pairs))
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
        for widget in (self.start_spin, self.end_spin, self.step_spin):
            widget.valueChanged.connect(self._update)
        self.sampling_combo.currentIndexChanged.connect(self._update)
        for check in self.sheet_checks.values():
            check.toggled.connect(self._update)
        self._update()
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

    def _tc_usable(self) -> str:
        """Why the TC fit cannot fill TC Compare now ("" when it can)."""
        if self.tc_run is None:
            return "No TC fit of this recording in this session (Measurement → Fit Emissivity from TCs…)."
        if self.tc_run.result.preset is not None:
            return "The TC fit used one preset of a superframing recording, which the workbook cannot separate yet."
        if not self.sheet_checks["tc"].isChecked():
            return "Tick the TC Compare sheet to fill it."
        if self.time_base_combo.currentData() != "frames":
            return "The TC fit's time offset is on the frame-number time base; choose that time base."
        if not self._tc_pairing.pairs:
            return "No TC pixel of the last fit lies in an ROI."
        return ""

    def tc_prefill(self):
        """The TC Compare contents from the last fit for the open recording's ROIs, or None."""
        if not self.tc_check.isChecked() or self._tc_usable():
            return None
        from .tcmatch import tc_prefill

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
        self.tc_check.setEnabled(not reason)
        if reason:
            self.tc_label.setText(reason)
        else:
            self.tc_label.setText("Pairs: " + self._tc_pairing.describe() + "." if self.tc_check.isChecked()
                                  else "Unticked: paste the logger data on TC Compare yourself.")
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
        replace = confirm_replace(self, [self._dest()], self._confirmed)
        if replace is None:
            return
        self._replace = [str(path) for path in replace]
        super().accept()

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
