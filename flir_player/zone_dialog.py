# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Split a box ROI into equal cell zones, previewed on the image while the dialog is open."""
from __future__ import annotations

from typing import Collection, Mapping

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
)

from .models import RoiShape
from .widgets import ChevronComboBox, FramelessDialog
from .zones import ENDS, ZoneSpec, pair_tcs, split_box

END_LABELS = {"right": "Right end", "left": "Left end", "top": "Top end", "bottom": "Bottom end"}


class ZoneSplitDialog(FramelessDialog):
    """Number of zones, where zone 1 is, the gap between zones, names and the box edges.

    ``preview`` carries the zones as (name, corners) pairs on every valid change, or an empty
    list when the settings cannot be split; ``tc_pixels`` (TC → (row, col)) from a TC fit of the
    recording adds a line saying which zone each TC pixel falls in.
    """

    JOIN = 2  # done() code: join the zones back into one box

    preview = Signal(object)

    def __init__(self, spec: ZoneSpec, width: int, height: int, *,
                 tc_pixels: Mapping[str, tuple[int, int]] | None = None, regroup: bool = False,
                 taken: Collection[str] = (), parent=None) -> None:
        super().__init__("Split Box into Zones", parent)
        self.setMinimumWidth(460)
        self._width, self._height = width, height
        self._tc_pixels = dict(tc_pixels or {})
        self._taken = set(taken)  # names of the other ROIs: zones must not repeat them
        self._zones: list = []
        intro = QLabel("Equal zones along the box, one per cell. Zone 1 is at the end you choose (the heater end "
                       "on the fire tests). The gap leaves out the mixed pixels where two cells meet.")
        intro.setObjectName("FieldLabel")
        intro.setWordWrap(True)
        self.body.addWidget(intro)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 200)
        self.count_spin.setValue(spec.count)
        self.start_combo = ChevronComboBox()
        for end in ENDS:
            self.start_combo.addItem(END_LABELS[end], end)
        self.start_combo.setCurrentIndex(ENDS.index(spec.start))
        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(0, 20)
        self.gap_spin.setSuffix(" px")
        self.gap_spin.setValue(spec.gap)
        self.gap_spin.setToolTip("Pixel columns (rows, for a vertical split) left out between neighbouring zones")
        self.prefix_edit = QLineEdit(spec.prefix)
        self.prefix_edit.setToolTip("Zones are named prefix 1, prefix 2, …")
        (left, top), (right, bottom) = spec.box
        self.x0 = self._spin(0, width - 1, int(left))
        self.x1 = self._spin(0, width - 1, int(right) - 1)
        self.y0 = self._spin(0, height - 1, int(top))
        self.y1 = self._spin(0, height - 1, int(bottom) - 1)
        rows = (("Zones", self.count_spin), ("Zone 1 at the", self.start_combo), ("Gap between zones", self.gap_spin),
                ("Names", self.prefix_edit))
        for r, (label, widget) in enumerate(rows):
            grid.addWidget(QLabel(label), r, 0)
            grid.addWidget(widget, r, 1, 1, 3)
        grid.addWidget(QLabel("Columns"), len(rows), 0)
        grid.addWidget(self.x0, len(rows), 1)
        grid.addWidget(QLabel("to"), len(rows), 2)
        grid.addWidget(self.x1, len(rows), 3)
        grid.addWidget(QLabel("Rows"), len(rows) + 1, 0)
        grid.addWidget(self.y0, len(rows) + 1, 1)
        grid.addWidget(QLabel("to"), len(rows) + 1, 2)
        grid.addWidget(self.y1, len(rows) + 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        self.body.addLayout(grid)
        edges = QLabel("Columns and rows are the box's first and last pixels, as the image readout shows them.")
        edges.setObjectName("FieldLabel")
        edges.setWordWrap(True)
        self.body.addWidget(edges)

        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.body.addWidget(self.info_label)
        self.tc_label = QLabel()
        self.tc_label.setObjectName("FieldLabel")
        self.tc_label.setWordWrap(True)
        self.tc_label.setVisible(bool(self._tc_pixels))
        self.body.addWidget(self.tc_label)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                           | QDialogButtonBox.StandardButton.Cancel)
        ok = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Split")
        ok.setProperty("accent", True)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        if regroup:
            join = QPushButton("Join Back into One Box")
            join.setToolTip("Replace the zones with the box they were split from")
            join.clicked.connect(lambda: self.done(self.JOIN))
            self.button_box.addButton(join, QDialogButtonBox.ButtonRole.ResetRole)
        self.body.addWidget(self.button_box)

        for spin in (self.count_spin, self.gap_spin, self.x0, self.x1, self.y0, self.y1):
            spin.valueChanged.connect(self._update)
        self.start_combo.currentIndexChanged.connect(self._update)
        self.prefix_edit.textChanged.connect(self._update)

    @staticmethod
    def _spin(low: int, high: int, value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, max(low, high))
        spin.setValue(max(low, min(high, value)))
        return spin

    def spec(self) -> ZoneSpec:
        left, right = sorted((self.x0.value(), self.x1.value()))
        top, bottom = sorted((self.y0.value(), self.y1.value()))
        return ZoneSpec(box=((float(left), float(top)), (float(right + 1), float(bottom + 1))),
                        count=self.count_spin.value(), start=str(self.start_combo.currentData()),
                        gap=self.gap_spin.value(), prefix=self.prefix_edit.text())

    def zones(self) -> list:
        """(name, corners) of the zones the dialog shows (empty when the settings cannot split)."""
        return list(self._zones)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._update()

    def _update(self) -> None:
        spec = self.spec()
        ok = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        try:
            self._zones = split_box(spec, self._width, self._height)
            clash = [name for name, _points in self._zones if name in self._taken]
            if clash:
                shown = ", ".join(clash[:3]) + ("…" if len(clash) > 3 else "")
                raise ValueError(f"Other ROIs already use the names {shown}: choose other names (the workbook "
                                 "pairs ROIs and TCs by name)")
        except ValueError as exc:
            self._zones = []
            self.info_label.setText(str(exc))
            self.info_label.setStyleSheet("color: #E06C75;")
            self.tc_label.setText("")
            ok.setEnabled(False)
            self.preview.emit([])
            return
        (left, top), (right, bottom) = spec.box
        along = spec.start in ("right", "left")
        length = (right - left) if along else (bottom - top)
        across = (bottom - top) if along else (right - left)
        widths = [(p[1][0] - p[0][0]) if along else (p[1][1] - p[0][1]) for _name, p in self._zones]
        size = f"{min(widths):g}" if min(widths) == max(widths) else f"{min(widths):g}-{max(widths):g}"
        self.info_label.setStyleSheet("")
        self.info_label.setText(
            f"{spec.count} zone{'s' if spec.count != 1 else ''} of {size} × {across:g} pixels "
            f"({length:g} px long box, {spec.gap} px gap{'s' if spec.gap != 1 else ''}).")
        if self._tc_pixels:
            shapes = [RoiShape(k + 1, "rect", points, name) for k, (name, points) in enumerate(self._zones)]
            pairing = pair_tcs(self._tc_pixels, shapes, self._height, self._width)
            self.tc_label.setText("TC pixels from the last fit: " + pairing.describe() + ".")
        ok.setEnabled(True)
        self.preview.emit(list(self._zones))
