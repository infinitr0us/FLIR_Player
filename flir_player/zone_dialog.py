# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Split a box ROI into cell zones, previewed on the image while the dialog is open."""
from __future__ import annotations

from dataclasses import replace
from typing import Collection, Mapping

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
)

from .models import RoiShape
from .orientation import Orientation
from .widgets import ChevronComboBox, FramelessDialog
from .zones import (ENDS, MAX_GAP, MAX_LEAN, MAX_ZONES, ZONE_KIND, ZoneSpec, far_label, normalized_box,
                    normalized_far, pair_tcs, perspective_ratio, split_box)

END_LABELS = {"right": "Right end", "left": "Left end", "top": "Top end", "bottom": "Bottom end"}


class ZoneSplitDialog(FramelessDialog):
    """Number of zones, where zone 1 is, the gap between zones, names, the box edges, the slant, the
    dividers' lean and the camera's distance to each end (perspective).

    ``preview`` carries the zones as (name, corners) pairs on every valid change, or an empty
    list when the settings cannot be split; ``tc_pixels`` (TC → (row, col)) from a TC fit of the
    recording adds a line saying which zone each TC pixel falls in. The ends are named as the
    picture is shown (``orientation``: the view's turn and flips); the spec keeps recording terms.
    """

    JOIN = 2  # done() code: join the zones back into one box

    preview = Signal(object)

    def __init__(self, spec: ZoneSpec, width: int, height: int, *,
                 tc_pixels: Mapping[str, tuple[int, int]] | None = None, regroup: bool = False,
                 taken: Collection[str] = (), orientation: Orientation = Orientation(), parent=None) -> None:
        super().__init__("Split Box into Zones", parent)
        self.setMinimumWidth(460)
        self._width, self._height = width, height
        self._tc_pixels = dict(tc_pixels or {})
        self._taken = set(taken)  # names of the other ROIs: zones must not repeat them
        self._zones: list = []
        intro = QLabel("One zone per cell along the box. Zone 1 is at the end you choose (the heater end on the "
                       "fire tests). The gap leaves out the mixed pixels where two cells meet. If the module runs at "
                       "a slant in the picture, give its rows at the box's last column too. If the cells' dividers "
                       "lean in the picture, set the lean; if one end of the module is farther from the camera, give "
                       "both distances. Afterwards a divider can be dragged by a zone's corner.")
        intro.setObjectName("FieldLabel")
        intro.setWordWrap(True)
        self.body.addWidget(intro)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, MAX_ZONES)
        self.count_spin.setValue(spec.count)
        self.start_combo = ChevronComboBox()
        for shown in ENDS:  # labels as seen on screen, data in recording terms
            self.start_combo.addItem(END_LABELS[shown], orientation.recording_end(shown))
        self.start_combo.setCurrentIndex(max(0, self.start_combo.findData(spec.start)))
        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(0, MAX_GAP)
        self.gap_spin.setSuffix(" px")
        self.gap_spin.setValue(spec.gap)
        self.gap_spin.setToolTip("Lines of pixels left out between neighbouring zones, where two cells meet")
        self.prefix_edit = QLineEdit(spec.prefix)
        self.prefix_edit.setToolTip("Zones are named prefix 1, prefix 2, …")
        (left, top), (right, bottom) = normalized_box(spec.box)  # whole pixels, rounded as the slant is
        self.x0 = self._spin(0, width - 1, int(left))
        self.x1 = self._spin(0, width - 1, int(right) - 1)
        self.y0 = self._spin(0, height - 1, int(top))
        self.y1 = self._spin(0, height - 1, int(bottom) - 1)
        # the slant: the zones' rows at the box's last column (columns at its last row for a split along the
        # rows); they follow the box's own until set by hand, and again after the split turns
        self._along = spec.start in ("right", "left")
        a, b = (top, bottom) if self._along else (left, right)
        if spec.far is not None:
            try:  # a saved slant on whole pixels, rounded as the box's corners are
                a, b = normalized_far(spec)
            except ValueError:
                spec = replace(spec, far=None)
        limit = (height if self._along else width) - 1
        self.f0 = self._spin(0, limit, int(a))
        self.f1 = self._spin(0, limit, int(b) - 1)
        self._linked = spec.far is None
        for spin in (self.f0, self.f1):
            spin.setToolTip("Where the module is at the other end of the box. The same as the box's: no slant. "
                            "Otherwise each zone takes the module's rows (columns) at its centre, so the zones "
                            "step along a module that runs at a slant in the picture.")
        self.far_label = QLabel()
        self.lean_spin = QSpinBox()
        self.lean_spin.setRange(-MAX_LEAN, MAX_LEAN)
        self.lean_spin.setSuffix(" px")
        self.lean_spin.setValue(spec.lean if isinstance(spec.lean, int) and abs(spec.lean) <= MAX_LEAN else 0)
        self.lean_spin.setToolTip("How far each divider's end on the band's lower edge (right edge, for zones along "
                                  "the rows) lies along the zones from its end on the upper (left) edge, in pixels. "
                                  "Positive: towards the box's right (bottom) end. Match it by eye to the cell "
                                  "edges in the preview.")
        self.near_distance = self._distance_spin()
        self.far_distance = self._distance_spin()
        if spec.distances is not None:
            try:
                perspective_ratio(spec)
                self.near_distance.setValue(float(spec.distances[0]))
                self.far_distance.setValue(float(spec.distances[1]))
            except ValueError:
                pass
        for spin in (self.near_distance, self.far_distance):
            spin.setToolTip("The camera's distance to each end of the module, in any one unit (metres, say). With "
                            "both given, cells narrow towards the farther end as the camera sees them; empty: "
                            "equal widths.")
        self._moved = tuple(spec.moved)
        self._moved_count = spec.count
        self.keep_moved = QCheckBox()
        self.keep_moved.setChecked(bool(self._moved))
        self.keep_moved.setVisible(bool(self._moved))
        rows = (("Zones", self.count_spin), ("Zone 1 at the", self.start_combo), ("Gap between zones", self.gap_spin),
                ("Names", self.prefix_edit))
        for r, (label, widget) in enumerate(rows):
            grid.addWidget(QLabel(label), r, 0)
            grid.addWidget(widget, r, 1, 1, 3)
        edge_rows = (("Columns", self.x0, self.x1, "to"), ("Rows", self.y0, self.y1, "to"),
                     (self.far_label, self.f0, self.f1, "to"),
                     ("Camera distance", self.near_distance, self.far_distance, "and"))
        for r, (label, first, last, between) in enumerate(edge_rows, start=len(rows)):
            grid.addWidget(label if isinstance(label, QLabel) else QLabel(label), r, 0)
            grid.addWidget(first, r, 1)
            grid.addWidget(QLabel(between), r, 2)
            grid.addWidget(last, r, 3)
        r = len(rows) + len(edge_rows)
        grid.addWidget(QLabel("Divider lean"), r, 0)
        grid.addWidget(self.lean_spin, r, 1)
        grid.addWidget(self.keep_moved, r + 1, 0, 1, 4)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        # the labels keep their room: the slant row's label changes with the split's direction
        metrics = self.far_label.fontMetrics()
        grid.setColumnMinimumWidth(0, max(metrics.horizontalAdvance(text) for text in (
            far_label("right").capitalize(), far_label("top").capitalize(), "Camera distance", "Gap between zones")) + 8)
        self.body.addLayout(grid)
        edges = QLabel("Columns and rows are the box's first and last pixels, as the image readout shows them. "
                       "Camera distances: at zone 1's end, then at the far end.")
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

        for spin in (self.count_spin, self.gap_spin, self.x0, self.x1, self.y0, self.y1, self.lean_spin,
                     self.near_distance, self.far_distance):
            spin.valueChanged.connect(self._update)
        self.keep_moved.toggled.connect(self._update)
        for spin in (self.f0, self.f1):
            spin.valueChanged.connect(self._far_edited)
        self.start_combo.currentIndexChanged.connect(self._update)
        self.prefix_edit.textChanged.connect(self._update)
        self._sync_far()

    @staticmethod
    def _distance_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 10000.0)
        spin.setDecimals(2)
        spin.setSingleStep(0.1)
        spin.setSpecialValueText("not set")  # 0: no perspective
        return spin

    @staticmethod
    def _spin(low: int, high: int, value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, max(low, high))
        spin.setValue(max(low, min(high, value)))
        return spin

    def spec(self) -> ZoneSpec:
        left, right = sorted((self.x0.value(), self.x1.value()))
        top, bottom = sorted((self.y0.value(), self.y1.value()))
        start = str(self.start_combo.currentData())
        near = (float(top), float(bottom + 1)) if start in ("right", "left") else (float(left), float(right + 1))
        a, b = sorted((self.f0.value(), self.f1.value()))
        far = (float(a), float(b + 1))
        first, last = self.near_distance.value(), self.far_distance.value()
        distances = (first, last) if first > 0 or last > 0 else None
        return ZoneSpec(box=((float(left), float(top)), (float(right + 1), float(bottom + 1))),
                        count=self.count_spin.value(), start=start, gap=self.gap_spin.value(),
                        prefix=self.prefix_edit.text(), far=None if far == near else far, lean=self.lean_spin.value(),
                        distances=distances, moved=self._moved if self._keeps_moved() else ())

    def _keeps_moved(self) -> bool:
        """Dividers moved by hand stay only while the number of zones is the one they were moved in."""
        return bool(self._moved) and self.keep_moved.isChecked() and self.count_spin.value() == self._moved_count

    def _far_edited(self) -> None:
        self._linked = False
        self._update()

    def _sync_far(self) -> None:
        """Name the slant row for the split's direction; while not set by hand, it follows the box."""
        start = str(self.start_combo.currentData())
        along = start in ("right", "left")
        if along != self._along:  # the split turned: the slant row means the other direction now
            self._along = along
            self._linked = True
        self.far_label.setText(far_label(start).capitalize())
        if self._linked:
            first, last = (self.y0, self.y1) if along else (self.x0, self.x1)
            limit = (self._height if along else self._width) - 1
            for spin, value in ((self.f0, first.value()), (self.f1, last.value())):
                spin.blockSignals(True)
                spin.setRange(0, max(0, limit))
                spin.setValue(value)
                spin.blockSignals(False)

    def zones(self) -> list:
        """(name, corners) of the zones the dialog shows (empty when the settings cannot split)."""
        return list(self._zones)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._update()

    def _update(self) -> None:
        self._sync_far()
        if self._moved:
            same = self.count_spin.value() == self._moved_count
            self.keep_moved.setEnabled(same)
            count = len(self._moved)
            self.keep_moved.setText(
                f"Keep the {count} divider{'s' if count != 1 else ''} moved by hand" if same else
                f"The {count} divider{'s' if count != 1 else ''} moved by hand cannot be kept with another number "
                "of zones")
        spec = self.spec()
        ok = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        try:
            if spec.distances is not None and min(spec.distances) <= 0:
                raise ValueError("Give the camera's distance at both ends, or at neither (equal widths)")
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
        a, c = (0, 1) if along else (1, 0)  # along the zones, across them
        # a zone's size where the band's middle line crosses it: the mean of its two edges
        widths = [round(((p[1][a] - p[0][a]) + (p[2][a] - p[3][a])) / 2, 1) for _name, p in self._zones]
        heights = [round(((p[3][c] - p[0][c]) + (p[2][c] - p[1][c])) / 2, 1) for _name, p in self._zones]

        def span(values) -> str:
            return f"{min(values):g}" if min(values) == max(values) else f"{min(values):g}-{max(values):g}"

        text = (f"{spec.count} zone{'s' if spec.count != 1 else ''} of {span(widths)} × {span(heights)} pixels "
                f"({length:g} px long box, {spec.gap} px gap{'s' if spec.gap != 1 else ''}).")
        if spec.far is not None:
            near = (top, bottom) if along else (left, right)
            ends = (left, right - 1) if along else (top, bottom - 1)
            kind, at = ("Rows", "column") if along else ("Columns", "row")
            text += (f" {kind} {near[0]:g}-{near[1] - 1:g} at {at} {ends[0]:g}, "
                     f"{spec.far[0]:g}-{spec.far[1] - 1:g} at {at} {ends[1]:g}: the zones follow the slant.")
        if spec.lean:
            text += f" Dividers lean {abs(spec.lean)} px across the band."
        if spec.distances is not None:
            text += (f" Zone 1's end {spec.distances[0]:g}, the far end {spec.distances[1]:g} from the camera: "
                     "the zones narrow towards the farther end.")
        if spec.moved:
            text += f" {len(spec.moved)} divider{'s' if len(spec.moved) != 1 else ''} moved by hand."
        self.info_label.setStyleSheet("")
        self.info_label.setText(text)
        if self._tc_pixels:
            shapes = [RoiShape(k + 1, ZONE_KIND, points, name) for k, (name, points) in enumerate(self._zones)]
            pairing = pair_tcs(self._tc_pixels, shapes, self._height, self._width)
            self.tc_label.setText("TC pixels from the last fit: " + pairing.describe() + ".")
        ok.setEnabled(True)
        self.preview.emit(list(self._zones))
