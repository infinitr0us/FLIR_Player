# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable

import numpy as np
import qtawesome as qta
import qtawesome.iconic_font as qta_font
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSettings, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .models import ROI_COLORS, CadenceInfo, UnitOption, VideoMetadata
from .plots import HistogramPlotPanel, ProfilePlotPanel, TemporalPlotPanel
from .render import format_value, isotherm_color, lut_from_stops, palette_lut, palette_names


# QtAwesome otherwise tries to persist its bundled fonts in the Windows user
# font directory. Loading them directly keeps the app portable and sandbox-safe.
qta_font.IconicFont._get_fonts_directory = lambda self: str(
    Path(qta.__file__).resolve().parent / "fonts"
)

# Icon colors kept in sync with the design tokens in style.py.
ICON_TEXT = "#E9EDF2"
ICON_SECONDARY = "#C7CED8"
ICON_MUTED = "#97A1AF"
ICON_DISABLED = "#5F6B7A"
ICON_ACCENT = "#F5A524"
ICON_ON_ACCENT = "#241A05"


def tinted_icon(icon: QIcon, color: str, size: int = 24) -> QIcon:
    pixmap = icon.pixmap(size, size)
    painter = QPainter(pixmap)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(pixmap.rect(), QColor(color))
    painter.end()
    return QIcon(pixmap)


def standard_icon(widget: QWidget, standard, color: str = ICON_TEXT, size: int = 24) -> QIcon:
    return tinted_icon(widget.style().standardIcon(standard), color, size)


def awesome_icon(name: str, color: str = ICON_TEXT) -> QIcon:
    return qta.icon(name, color=color, color_disabled=ICON_DISABLED, scale_factor=0.88)


def value_table_item(text: str) -> QTableWidgetItem:
    """Table value cell: tabular font, full text on tooltip when long."""
    item = QTableWidgetItem(text)
    item.setFont(QFont("Consolas", 10))
    if len(text) > 60:
        item.setToolTip(text)
    return item


class ChevronComboBox(QComboBox):
    """QComboBox whose drop-down chevron is drawn by the central stylesheet.

    Kept as a named alias for the call sites that predate the QSS arrow.
    """


class TitleBar(QWidget):
    open_requested = Signal()
    export_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(56)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 0, 0)
        layout.setSpacing(8)

        mark = QLabel()
        mark.setFixedSize(24, 24)
        mark.setPixmap(awesome_icon("fa6s.diamond", ICON_ACCENT).pixmap(22, 22))
        mark.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(mark)

        title = QLabel("FLIR THERMAL PLAYER")
        title.setObjectName("AppTitle")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(title)

        layout.addSpacing(18)

        self.open_button = QToolButton()
        self.open_button.setObjectName("FilledButton")
        self.open_button.setIcon(awesome_icon("fa6s.folder-open"))
        self.open_button.setIconSize(QSize(18, 18))
        self.open_button.setToolTip("Open recording (Ctrl+O)")
        self.open_button.clicked.connect(self.open_requested)
        layout.addWidget(self.open_button)

        self.filename_label = QLabel("No recording")
        self.filename_label.setObjectName("Filename")
        self.filename_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.filename_label, 1)

        self.export_button = QToolButton()
        self.export_button.setObjectName("ExportButton")
        self.export_button.setText("Export")
        self.export_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.export_button.setIcon(awesome_icon("fa6s.file-export", ICON_ACCENT))
        self.export_button.setIconSize(QSize(15, 15))
        self.export_button.setToolTip("Export the current frame (Ctrl+E)")
        self.export_button.clicked.connect(self.export_requested)
        self.export_button.setEnabled(False)
        layout.addWidget(self.export_button)

        layout.addSpacing(8)
        self.minimize_button = self._caption_button("fa6s.minus")
        self.maximize_button = self._caption_button("fa6s.window-maximize")
        self.close_button = self._caption_button("fa6s.xmark", close=True)
        self.minimize_button.clicked.connect(lambda: self.window().showMinimized())
        self.maximize_button.clicked.connect(self.toggle_maximized)
        self.close_button.clicked.connect(lambda: self.window().close())
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)

    def _caption_button(self, icon_name: str, close: bool = False) -> QToolButton:
        button = QToolButton()
        button.setObjectName("CloseButton" if close else "CaptionButton")
        button.setIcon(awesome_icon(icon_name, ICON_SECONDARY))
        button.setIconSize(QSize(15, 15))
        return button

    def set_filename(self, filename: str) -> None:
        self.filename_label.setText(filename or "No recording")
        self.filename_label.setToolTip(filename)

    def set_export_menu(self, menu: QMenu) -> None:
        self.export_button.setMenu(menu)
        self.export_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)

    def set_open_menu(self, menu: QMenu) -> None:
        self.open_button.setMenu(menu)
        self.open_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)

    def toggle_maximized(self) -> None:
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()
        self.update_maximize_icon()

    def update_maximize_icon(self) -> None:
        icon_name = (
            "fa6s.window-restore"
            if self.window().isMaximized()
            else "fa6s.window-maximize"
        )
        self.maximize_button.setIcon(awesome_icon(icon_name, ICON_SECONDARY))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


ROI_TOOLS: tuple[tuple[str, str, str], ...] = (
    ("select", "fa6s.arrow-pointer", "Select / edit ROI"),
    ("rect", "fa6s.vector-square", "Box ROI"),
    ("ellipse", "fa6s.circle", "Ellipse ROI"),
    ("line", "fa6s.grip-lines", "Line ROI"),
    ("cursor", "fa6s.crosshairs", "Spot ROI (click to pin)"),
)

_HANDLE_HIT_PX = 9


class AnalysisToolbar(QFrame):
    """Vertical ROI tool strip on the left of the image stage (ResearchIR §4.5)."""

    tool_changed = Signal(str)
    delete_requested = Signal()
    stats_toggled = Signal(bool)
    zoom_in_requested = Signal()
    zoom_out_requested = Signal()
    zoom_fit_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("AnalysisToolbar")
        self.setFixedWidth(48)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 8, 6, 8)
        layout.setSpacing(6)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QToolButton] = {}
        for tool, icon_name, tooltip in ROI_TOOLS:
            button = QToolButton()
            button.setObjectName("AnalysisButton")
            button.setIcon(awesome_icon(icon_name, ICON_TEXT))
            button.setIconSize(QSize(17, 17))
            button.setToolTip(tooltip)
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, t=tool: self.tool_changed.emit(t))
            self._group.addButton(button)
            layout.addWidget(button)
            self._buttons[tool] = button
        self._buttons["select"].setChecked(True)

        layout.addSpacing(4)
        layout.addWidget(self._separator(), 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(4)

        self.stats_button = QToolButton()
        self.stats_button.setObjectName("AnalysisButton")
        self.stats_button.setIcon(awesome_icon("fa6s.table", ICON_TEXT))
        self.stats_button.setIconSize(QSize(16, 16))
        self.stats_button.setToolTip("Show / hide analysis panel")
        self.stats_button.setCheckable(True)
        self.stats_button.toggled.connect(self.stats_toggled)
        layout.addWidget(self.stats_button)

        self.delete_button = QToolButton()
        self.delete_button.setObjectName("AnalysisButton")
        self.delete_button.setIcon(awesome_icon("fa6s.trash-can", ICON_SECONDARY))
        self.delete_button.setIconSize(QSize(16, 16))
        self.delete_button.setToolTip("Delete selected ROI (Del)")
        self.delete_button.clicked.connect(self.delete_requested)
        layout.addWidget(self.delete_button)

        layout.addSpacing(4)
        layout.addWidget(self._separator(), 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(4)
        self.zoom_in_button = self._zoom_button(
            "fa6s.magnifying-glass-plus", "Zoom in (+)", self.zoom_in_requested
        )
        self.zoom_out_button = self._zoom_button(
            "fa6s.magnifying-glass-minus", "Zoom out (-)", self.zoom_out_requested
        )
        self.zoom_fit_button = self._zoom_button(
            "fa6s.expand", "Fit to window (0)", self.zoom_fit_requested
        )
        layout.addWidget(self.zoom_in_button)
        layout.addWidget(self.zoom_out_button)
        layout.addWidget(self.zoom_fit_button)
        layout.addStretch(1)
        self.set_enabled(False)

    def _zoom_button(self, icon_name: str, tooltip: str, signal) -> QToolButton:
        button = QToolButton()
        button.setObjectName("AnalysisButton")
        button.setIcon(awesome_icon(icon_name, ICON_SECONDARY))
        button.setIconSize(QSize(16, 16))
        button.setToolTip(tooltip)
        button.clicked.connect(signal)
        return button

    @staticmethod
    def _separator() -> QFrame:
        line = QFrame()
        line.setObjectName("Hairline")
        line.setFixedSize(24, 1)
        return line

    def set_enabled(self, enabled: bool) -> None:
        for button in self._buttons.values():
            button.setEnabled(enabled)
        self.stats_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)
        self.zoom_in_button.setEnabled(enabled)
        self.zoom_out_button.setEnabled(enabled)
        self.zoom_fit_button.setEnabled(enabled)

    def current_tool(self) -> str:
        for tool, button in self._buttons.items():
            if button.isChecked():
                return tool
        return "select"


class StatisticsPanel(QFrame):
    """Per-ROI statistics table with a whole-image column (ResearchIR §4.7.1.1)."""

    save_requested = Signal()

    METRICS: tuple[str, ...] = ("Min", "Max", "Mean", "Std Dev", "Pixels", "Value")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        header.addStretch(1)

        self.image_toggle = self._header_button("fa6s.image", "Show whole-image column", checkable=True)
        self.image_toggle.setChecked(True)
        self.image_toggle.toggled.connect(lambda _checked=False: self._render())
        self.pause_toggle = self._header_button("fa6s.pause", "Pause statistics updates", checkable=True)
        self.save_button = self._header_button("fa6s.floppy-disk", "Save statistics to a CSV file")
        self.save_button.clicked.connect(self.save_requested)
        for button in (self.image_toggle, self.pause_toggle, self.save_button):
            header.addWidget(button)
        layout.addLayout(header)

        self.table = QTableWidget(len(self.METRICS), 0)
        self.table.setVerticalHeaderLabels(self.METRICS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        self._last: tuple | None = None

    def _header_button(self, icon_name: str, tooltip: str, checkable: bool = False) -> QToolButton:
        button = QToolButton()
        button.setObjectName("TransportButton")
        button.setIcon(awesome_icon(icon_name, ICON_TEXT))
        button.setIconSize(QSize(15, 15))
        button.setToolTip(tooltip)
        button.setCheckable(checkable)
        return button

    def set_statistics(self, roi_stats, image_stats, suffix: str) -> None:
        self._last = (tuple(roi_stats), image_stats, suffix)
        if self.pause_toggle.isChecked():
            return
        self._render()

    def _render(self) -> None:
        if self._last is None:
            return
        roi_stats, image_stats, suffix = self._last
        columns: list[tuple[str, str, tuple]] = []
        if self.image_toggle.isChecked() and image_stats is not None:
            columns.append(("Image", "image", tuple(image_stats)))
        for stats in roi_stats:
            columns.append((stats.name, stats.kind, stats))

        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels([name for name, _, _ in columns])
        for column, (_, kind, stats) in enumerate(columns):
            if kind == "image":
                minimum, maximum, mean, std_dev, num_pixels = stats
                value = None
            else:
                minimum = stats.minimum
                maximum = stats.maximum
                mean = stats.mean
                std_dev = stats.std_dev
                num_pixels = stats.num_pixels
                value = stats.value if kind == "cursor" else None
            cells = (
                format_value(minimum, suffix),
                format_value(maximum, suffix),
                format_value(mean, suffix),
                f"{std_dev:.2f} {suffix}".strip(),
                str(num_pixels),
                format_value(value, suffix) if value is not None else "—",
            )
            for row, text in enumerate(cells):
                item = value_table_item(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, item)

    def to_rows(self) -> list[list[str]]:
        headers = ["Metric"]
        for column in range(self.table.columnCount()):
            item = self.table.horizontalHeaderItem(column)
            headers.append(item.text() if item is not None else "")
        rows = [headers]
        for row in range(self.table.rowCount()):
            label_item = self.table.verticalHeaderItem(row)
            cells = [label_item.text() if label_item is not None else ""]
            for column in range(self.table.columnCount()):
                item = self.table.item(row, column)
                cells.append(item.text() if item is not None else "")
            rows.append(cells)
        return rows


class ThermalCanvas(QWidget):
    probe_changed = Signal(object)
    fullscreen_requested = Signal()
    roi_drawn = Signal(str, object)
    roi_selected = Signal(object)
    roi_moved = Signal(object)
    roi_delete_requested = Signal(object)

    EMPTY_HINT = "Drop a recording onto the window or press Ctrl+O to browse"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(480, 320)
        self._image = QImage()
        self._pixmap = QPixmap()
        self._raw: np.ndarray | None = None
        self._suffix = ""
        self._image_rect = QRect()
        self._probe: tuple[int, int, float] | None = None
        self._message = "Open a FLIR recording to begin"
        self._rois: list = []
        self._selected_roi: int | None = None
        self._roi_tool = "select"
        self._draft: tuple[str, tuple[float, float], tuple[float, float]] | None = None
        self._drag: dict | None = None
        self._drag_shape = None
        self._show_markers = False
        self._marker_image: tuple = (None, None)
        self._marker_rois: tuple = ()
        self._flip_h = False
        self._flip_v = False
        self._zoom: float | None = None  # None = fit to window
        self._pan = QPointF(0, 0)
        self._pan_drag: dict | None = None
        self._pan_candidate: dict | None = None
        self._deselect_pending = False
        self._minimap_rect = QRect()
        self._minimap_drag = False

    @property
    def image(self) -> QImage:
        return self._image

    def set_message(self, message: str) -> None:
        self._message = message
        self.update()

    def set_frame(self, rgb: np.ndarray, raw: np.ndarray, suffix: str) -> None:
        height, width, _ = rgb.shape
        self._image = QImage(
            rgb.data,
            width,
            height,
            int(rgb.strides[0]),
            QImage.Format.Format_RGB888,
        ).copy()
        self._pixmap = QPixmap.fromImage(self._image)
        self._raw = raw
        self._suffix = suffix
        self._message = ""
        self._probe = None
        self._marker_image = (None, None)
        self._marker_rois = ()
        self.update()

    def set_overlay_options(self, markers: bool) -> None:
        self._show_markers = markers
        self.update()

    def set_flips(self, flip_h: bool, flip_v: bool) -> None:
        """Mirror the displayed image; coordinates stay in raw image space (§4.6.3)."""
        self._flip_h = bool(flip_h)
        self._flip_v = bool(flip_v)
        self.update()

    # --- zoom / pan ----------------------------------------------------------

    ZOOM_LEVELS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0)

    def set_zoom_fit(self) -> None:
        self._zoom = None
        self._pan = QPointF(0, 0)
        self.update()

    def set_zoom_level(self, zoom: float) -> None:
        self._set_zoom(float(zoom), QPointF(self.width() / 2, self.height() / 2))

    def zoom_step(self, direction: int) -> None:
        """Step through the fixed zoom levels (ResearchIR §4.9.4.1)."""
        if self._raw is None:
            return
        current = self._current_zoom()
        if direction > 0:
            larger = [z for z in self.ZOOM_LEVELS if z > current + 1e-9]
            target = larger[0] if larger else min(current * 2.0, 8.0)
        else:
            smaller = [z for z in self.ZOOM_LEVELS if z < current - 1e-9]
            target = smaller[-1] if smaller else current / 2.0
        if target <= self._fit_scale() + 1e-9:
            self.set_zoom_fit()
            return
        self.set_zoom_level(target)

    def zoom_at(self, factor: float, anchor: QPointF) -> None:
        """Continuous zoom (mouse wheel) keeping the anchor point stationary."""
        if self._raw is None:
            return
        target = max(0.05, min(8.0, self._current_zoom() * factor))
        if target <= self._fit_scale() + 1e-9:
            self.set_zoom_fit()
            return
        self._set_zoom(target, anchor)

    def _set_zoom(self, zoom: float, anchor: QPointF) -> None:
        if self._raw is None:
            return
        image_point = self._widget_to_image(anchor)
        zero_pan_rect = self._rect_for(zoom, QPointF(0, 0))
        dx, dy = image_point
        if self._flip_h:
            dx = self._raw.shape[1] - 1.0 - dx
        if self._flip_v:
            dy = self._raw.shape[0] - 1.0 - dy
        wx = zero_pan_rect.left() + dx * zero_pan_rect.width() / self._raw.shape[1]
        wy = zero_pan_rect.top() + dy * zero_pan_rect.height() / self._raw.shape[0]
        self._zoom = float(zoom)
        self._pan = QPointF(anchor.x() - wx, anchor.y() - wy)
        self._clamp_pan()
        self.update()

    def _current_zoom(self) -> float:
        return self._fit_scale() if self._zoom is None else self._zoom

    def _fit_scale(self) -> float:
        if self._pixmap.isNull():
            return 1.0
        return min(
            self.width() / self._pixmap.width(), self.height() / self._pixmap.height()
        )

    def _rect_for(self, zoom: float, pan: QPointF) -> QRect:
        source = self._pixmap.size()
        target_width = max(1, int(round(source.width() * zoom)))
        target_height = max(1, int(round(source.height() * zoom)))
        x = (self.width() - target_width) // 2 + int(round(pan.x()))
        y = (self.height() - target_height) // 2 + int(round(pan.y()))
        return QRect(x, y, target_width, target_height)

    def _clamp_pan(self) -> None:
        if self._zoom is None or self._raw is None:
            self._pan = QPointF(0, 0)
            return
        rect = self._rect_for(self._zoom, QPointF(0, 0))
        self._pan = QPointF(
            self._clamp_axis(self._pan.x(), rect.width(), self.width()),
            self._clamp_axis(self._pan.y(), rect.height(), self.height()),
        )

    @staticmethod
    def _clamp_axis(pan: float, image_extent: float, viewport_extent: float) -> float:
        if image_extent <= viewport_extent:
            return 0.0  # smaller than the viewport: stay centered
        max_pan = (image_extent - viewport_extent) / 2.0
        return max(-max_pan, min(pan, max_pan))

    def set_overlay_data(self, min_position, max_position, roi_stats) -> None:
        self._marker_image = (min_position, max_position)
        self._marker_rois = tuple(roi_stats)
        self.update()

    def clear_frame(self, message: str = "Open a FLIR recording to begin") -> None:
        self._image = QImage()
        self._pixmap = QPixmap()
        self._raw = None
        self._probe = None
        self._rois = []
        self._selected_roi = None
        self._draft = None
        self._drag = None
        self._drag_shape = None
        self._zoom = None
        self._pan = QPointF(0, 0)
        self._pan_drag = None
        self._pan_candidate = None
        self._deselect_pending = False
        self._minimap_drag = False
        self._message = message
        self.update()

    def set_rois(self, rois: list, selected_id: int | None) -> None:
        self._rois = list(rois)
        self._selected_roi = selected_id
        self.update()

    def set_roi_tool(self, tool: str) -> None:
        self._roi_tool = tool
        self._draft = None
        self._restore_tool_cursor()
        self.update()

    def _restore_tool_cursor(self) -> None:
        if self._roi_tool == "select":
            self.unsetCursor()
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

    def resizeEvent(self, event) -> None:
        self._clamp_pan()
        super().resizeEvent(event)

    def wheelEvent(self, event) -> None:
        if self._raw is None:
            super().wheelEvent(event)
            return
        steps = event.angleDelta().y() / 120.0
        if steps:
            self.zoom_at(1.25 ** steps, event.position())
            event.accept()

    # --- coordinate mapping -------------------------------------------------

    def _widget_to_image(self, pos) -> tuple[float, float]:
        if self._raw is None or self._image_rect.width() <= 0 or self._image_rect.height() <= 0:
            return (0.0, 0.0)
        x = (pos.x() - self._image_rect.left()) * self._raw.shape[1] / self._image_rect.width()
        y = (pos.y() - self._image_rect.top()) * self._raw.shape[0] / self._image_rect.height()
        x = max(0.0, min(x, self._raw.shape[1] - 1.0))
        y = max(0.0, min(y, self._raw.shape[0] - 1.0))
        if self._flip_h:
            x = self._raw.shape[1] - 1.0 - x
        if self._flip_v:
            y = self._raw.shape[0] - 1.0 - y
        return (x, y)

    def _image_to_widget(self, x: float, y: float) -> QPointF:
        if self._raw is None:
            return QPointF()
        if self._flip_h:
            x = self._raw.shape[1] - 1.0 - x
        if self._flip_v:
            y = self._raw.shape[0] - 1.0 - y
        wx = self._image_rect.left() + x * self._image_rect.width() / self._raw.shape[1]
        wy = self._image_rect.top() + y * self._image_rect.height() / self._raw.shape[0]
        return QPointF(wx, wy)

    # --- painting -------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#06080B"))
        if self._pixmap.isNull():
            self._paint_empty_state(painter)
            return

        self._image_rect = self._rect_for(self._current_zoom(), self._pan)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.drawPixmap(self._image_rect, self._pixmap)

        if self._probe is not None and self._raw is not None and self._roi_tool == "select":
            self._paint_probe(painter)
        self._paint_rois(painter)
        if self._show_markers:
            self._paint_markers(painter)
        if self._zoom is not None:
            self._paint_minimap(painter)
            self._paint_zoom_indicator(painter)

    def _paint_minimap(self, painter: QPainter) -> None:
        """Overview thumbnail with the current viewport rectangle (§4.8.4)."""
        thumb_width = 152
        if self._pixmap.width() <= 0:
            return
        aspect = self._pixmap.height() / self._pixmap.width()
        thumb_height = max(24, int(round(thumb_width * aspect)))
        margin = 12
        rect = QRect(
            self.width() - thumb_width - margin,
            self.height() - thumb_height - margin,
            thumb_width,
            thumb_height,
        )
        self._minimap_rect = rect
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        frame = rect.adjusted(-3, -3, 3, 3)
        path = QPainterPath()
        path.addRoundedRect(QRectF(frame), 5, 5)
        painter.fillPath(path, QColor(9, 13, 15, 225))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(rect, self._pixmap)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.setPen(QPen(QColor("#333E4D"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(frame), 5, 5)

        view = self._visible_display_fraction()
        viewport = QRectF(
            rect.left() + view.x() * rect.width(),
            rect.top() + view.y() * rect.height(),
            max(4.0, view.width() * rect.width()),
            max(4.0, view.height() * rect.height()),
        )
        painter.setPen(QPen(QColor(ICON_ACCENT), 1.5))
        painter.drawRect(viewport)

    def _visible_display_fraction(self) -> QRectF:
        """Visible part of the image as fractions of the displayed image size."""
        visible = self._image_rect.intersected(self.rect())
        if self._image_rect.width() <= 0 or self._image_rect.height() <= 0:
            return QRectF(0, 0, 1, 1)
        return QRectF(
            (visible.left() - self._image_rect.left()) / self._image_rect.width(),
            (visible.top() - self._image_rect.top()) / self._image_rect.height(),
            visible.width() / self._image_rect.width(),
            visible.height() / self._image_rect.height(),
        )

    def _pan_minimap_to(self, wpos: QPointF) -> None:
        rect = self._minimap_rect
        if rect.width() <= 0 or rect.height() <= 0 or self._zoom is None:
            return
        fx = max(0.0, min(1.0, (wpos.x() - rect.left()) / rect.width()))
        fy = max(0.0, min(1.0, (wpos.y() - rect.top()) / rect.height()))
        zero_pan = self._rect_for(self._zoom, QPointF(0, 0))
        self._pan = QPointF(
            self.width() / 2 - (zero_pan.left() + fx * zero_pan.width()),
            self.height() / 2 - (zero_pan.top() + fy * zero_pan.height()),
        )
        self._clamp_pan()
        self.update()

    def _paint_zoom_indicator(self, painter: QPainter) -> None:
        text = f"{round(self._current_zoom() * 100)}%"
        painter.setFont(QFont("Consolas", 10))
        metrics = painter.fontMetrics()
        text_rect = metrics.boundingRect(text).adjusted(-8, -4, 8, 4)
        text_rect.moveTopRight(QPoint(self.width() - 14, 12))
        path = QPainterPath()
        path.addRoundedRect(QRectF(text_rect), 5, 5)
        painter.fillPath(path, QColor(9, 13, 15, 200))
        painter.setPen(QPen(QColor("#242D3A"), 1))
        painter.drawPath(path)
        painter.setPen(QColor("#C7CED8"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_markers(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        min_pos, max_pos = self._marker_image
        self._paint_marker(painter, min_pos, QColor("#4CC2FF"))
        self._paint_marker(painter, max_pos, QColor("#FF7A90"))
        for stats in self._marker_rois:
            color = QColor(ROI_COLORS[stats.id % len(ROI_COLORS)])
            self._paint_marker(painter, stats.min_position, color)
            self._paint_marker(painter, stats.max_position, color)

    def _paint_marker(self, painter: QPainter, position, color: QColor) -> None:
        if position is None:
            return
        center = self._image_to_widget(*position)
        for width, pen_color in ((5, QColor("#07090A")), (2, color)):
            painter.setPen(QPen(pen_color, width))
            painter.drawLine(
                QPointF(center.x() - 6, center.y()), QPointF(center.x() + 6, center.y())
            )
            painter.drawLine(
                QPointF(center.x(), center.y() - 6), QPointF(center.x(), center.y() + 6)
            )

    def _paint_empty_state(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        center_x = self.width() // 2
        center_y = self.height() // 2

        icon = awesome_icon("fa6s.temperature-half", "#333E4D")
        painter.drawPixmap(center_x - 22, center_y - 76, icon.pixmap(44, 44))

        font = QFont("Segoe UI", 14)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor("#C7CED8"))
        painter.drawText(
            QRect(0, center_y - 20, self.width(), 26),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            self._message,
        )

        painter.setFont(QFont("Segoe UI", 11))
        painter.setPen(QColor("#5F6B7A"))
        painter.drawText(
            QRect(0, center_y + 10, self.width(), 22),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            self.EMPTY_HINT,
        )

    def _paint_probe(self, painter: QPainter) -> None:
        px, py, value = self._probe
        screen_x = self._image_rect.left() + int((px + 0.5) * self._image_rect.width() / self._raw.shape[1])
        screen_y = self._image_rect.top() + int((py + 0.5) * self._image_rect.height() / self._raw.shape[0])
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#07090A"), 4))
        painter.drawEllipse(QPoint(screen_x, screen_y), 7, 7)
        painter.setPen(QPen(QColor(ICON_ACCENT), 2))
        painter.drawEllipse(QPoint(screen_x, screen_y), 7, 7)
        text = f"x {px}  y {py}  ·  {format_value(value, self._suffix)}"
        painter.setFont(QFont("Consolas", 10))
        metrics = painter.fontMetrics()
        text_rect = metrics.boundingRect(text).adjusted(-10, -6, 10, 6)
        text_rect.moveTopLeft(QPoint(self._image_rect.left() + 14, self._image_rect.top() + 14))
        path = QPainterPath()
        path.addRoundedRect(QRectF(text_rect), 6, 6)
        painter.fillPath(path, QColor(9, 13, 15, 225))
        painter.setPen(QPen(QColor("#242D3A"), 1))
        painter.drawPath(path)
        painter.setPen(QColor("#F4F6F7"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_rois(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for shape in self._rois:
            if self._drag_shape is not None and shape.id == self._drag["id"]:
                shape = self._drag_shape
            selected = shape.id == self._selected_roi
            color = QColor(ROI_COLORS[shape.id % len(ROI_COLORS)])
            self._paint_roi_shape(painter, shape, color, selected)
        if self._draft is not None:
            kind, anchor, current = self._draft
            preview = ("cursor", (current,)) if kind == "cursor" else (kind, (anchor, current))
            pen = QPen(QColor(ICON_ACCENT), 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            self._draw_roi_geometry(painter, preview[0], preview[1])

    def _paint_roi_shape(self, painter: QPainter, shape, color: QColor, selected: bool) -> None:
        if selected:
            painter.setPen(QPen(QColor("#07090A"), 4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            self._draw_roi_geometry(painter, shape.kind, shape.points)
        painter.setPen(QPen(color, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        self._draw_roi_geometry(painter, shape.kind, shape.points)
        self._paint_roi_label(painter, shape, color)
        if selected:
            for _, handle_pos in self._handles_for(shape):
                painter.setPen(QPen(QColor("#07090A"), 1))
                painter.setBrush(color)
                painter.drawRect(
                    int(handle_pos.x()) - 4, int(handle_pos.y()) - 4, 8, 8
                )

    def _draw_roi_geometry(self, painter: QPainter, kind: str, points) -> None:
        if kind == "cursor" and len(points) == 1:
            center = self._image_to_widget(*points[0])
            painter.drawLine(
                QPointF(center.x() - 8, center.y()), QPointF(center.x() + 8, center.y())
            )
            painter.drawLine(
                QPointF(center.x(), center.y() - 8), QPointF(center.x(), center.y() + 8)
            )
            painter.drawEllipse(center, 3, 3)
            return
        if len(points) != 2:
            return
        first = self._image_to_widget(*points[0])
        second = self._image_to_widget(*points[1])
        if kind == "line":
            painter.drawLine(first, second)
        elif kind == "rect":
            painter.drawRect(QRectF(first, second).normalized())
        elif kind == "ellipse":
            painter.drawEllipse(QRectF(first, second).normalized())

    def _paint_roi_label(self, painter: QPainter, shape, color: QColor) -> None:
        anchor = self._label_anchor(shape)
        if anchor is None:
            return
        painter.setFont(QFont("Segoe UI", 10))
        metrics = painter.fontMetrics()
        text_rect = metrics.boundingRect(shape.name).adjusted(-6, -3, 6, 3)
        text_rect.moveBottomLeft(
            QPoint(int(anchor.x()), int(anchor.y()) - 6)
        )
        path = QPainterPath()
        path.addRoundedRect(QRectF(text_rect), 4, 4)
        painter.fillPath(path, QColor(9, 13, 15, 200))
        painter.setPen(QPen(color, 1))
        painter.drawPath(path)
        painter.setPen(QColor("#F4F6F7"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, shape.name)

    def _label_anchor(self, shape) -> QPointF | None:
        if shape.kind == "cursor" and len(shape.points) == 1:
            point = self._image_to_widget(*shape.points[0])
            return QPointF(point.x() - 8, point.y() - 10)
        if len(shape.points) == 2:
            first = self._image_to_widget(*shape.points[0])
            second = self._image_to_widget(*shape.points[1])
            rect = QRectF(first, second).normalized()
            return rect.topLeft()
        return None

    # --- hit testing ----------------------------------------------------------

    def _handles_for(self, shape) -> list[tuple[str, QPointF]]:
        if len(shape.points) != 2 or shape.kind == "cursor":
            return []
        first = self._image_to_widget(*shape.points[0])
        second = self._image_to_widget(*shape.points[1])
        if shape.kind == "line":
            return [("p0", first), ("p1", second)]
        rect = QRectF(first, second).normalized()
        return [
            ("nw", rect.topLeft()),
            ("ne", rect.topRight()),
            ("sw", rect.bottomLeft()),
            ("se", rect.bottomRight()),
        ]

    def _hit_roi(self, wpos) -> tuple[int, str] | None:
        ordered = sorted(
            self._rois, key=lambda shape: shape.id != self._selected_roi
        )
        for shape in reversed(ordered):
            for handle, handle_pos in self._handles_for(shape):
                if (handle_pos - wpos).manhattanLength() <= _HANDLE_HIT_PX * 2 and (
                    abs(handle_pos.x() - wpos.x()) <= _HANDLE_HIT_PX
                    and abs(handle_pos.y() - wpos.y()) <= _HANDLE_HIT_PX
                ):
                    return (shape.id, handle)
        for shape in reversed(self._rois):
            if self._point_on_shape(wpos, shape):
                return (shape.id, "body")
        return None

    def _point_on_shape(self, wpos, shape) -> bool:
        if shape.kind == "cursor" and len(shape.points) == 1:
            center = self._image_to_widget(*shape.points[0])
            return (
                abs(center.x() - wpos.x()) <= 10 and abs(center.y() - wpos.y()) <= 10
            )
        if len(shape.points) != 2:
            return False
        first = self._image_to_widget(*shape.points[0])
        second = self._image_to_widget(*shape.points[1])
        if shape.kind == "line":
            return _distance_to_segment(wpos, first, second) <= 6.0
        rect = QRectF(first, second).normalized()
        if shape.kind == "rect":
            return rect.contains(wpos)
        if shape.kind == "ellipse" and rect.width() > 0 and rect.height() > 0:
            dx = (wpos.x() - rect.center().x()) / (rect.width() / 2.0)
            dy = (wpos.y() - rect.center().y()) / (rect.height() / 2.0)
            return dx * dx + dy * dy <= 1.0
        return False

    # --- mouse / key interaction ----------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._raw is None:
            super().mousePressEvent(event)
            return
        wpos = event.position()
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_drag = {"start": wpos, "pan": QPointF(self._pan)}
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if self._zoom is not None and self._minimap_rect.contains(wpos.toPoint()):
            self._minimap_drag = True
            self._pan_minimap_to(wpos)
            event.accept()
            return
        if self._roi_tool == "select":
            hit = self._hit_roi(wpos)
            if hit is not None:
                roi_id, handle = hit
                if roi_id != self._selected_roi:
                    self._selected_roi = roi_id
                    self.roi_selected.emit(roi_id)
                shape = next(s for s in self._rois if s.id == roi_id)
                self._drag = {
                    "id": roi_id,
                    "handle": handle,
                    "start": self._widget_to_image(wpos),
                    "orig": shape,
                }
                self._drag_shape = shape
                self.update()
            elif self._zoom is not None:
                # click deselects; a real drag pans the viewport
                self._pan_candidate = {"start": wpos, "pan": QPointF(self._pan)}
                self._deselect_pending = self._selected_roi is not None
            elif self._selected_roi is not None:
                self._selected_roi = None
                self.roi_selected.emit(None)
                self.update()
            return
        if self._image_rect.contains(wpos.toPoint()):
            point = self._widget_to_image(wpos)
            self._draft = (self._roi_tool, point, point)
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        wpos = event.position()
        if self._minimap_drag:
            self._pan_minimap_to(wpos)
            return
        if self._pan_drag is not None:
            delta = wpos - self._pan_drag["start"]
            origin = self._pan_drag["pan"]
            self._pan = QPointF(origin.x() + delta.x(), origin.y() + delta.y())
            self._clamp_pan()
            self.update()
            return
        if self._pan_candidate is not None:
            if (wpos - self._pan_candidate["start"]).manhattanLength() > 4:
                self._pan_drag = self._pan_candidate
                self._pan_candidate = None
                self._deselect_pending = False
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if self._draft is not None:
            kind, anchor, _ = self._draft
            self._draft = (kind, anchor, self._widget_to_image(wpos))
            self.update()
            return
        if self._drag is not None and self._drag_shape is not None:
            self._drag_shape = self._dragged_shape(self._widget_to_image(wpos))
            self.update()
            return
        if self._roi_tool != "select":
            return
        if self._raw is None or not self._image_rect.contains(wpos.toPoint()):
            if self._probe is not None:
                self._probe = None
                self.probe_changed.emit(None)
                self.update()
            return
        x, y = self._widget_to_image(wpos)
        xi, yi = int(x), int(y)
        value = float(self._raw[yi, xi])
        self._probe = (xi, yi, value)
        self.probe_changed.emit(self._probe)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._pan_drag is not None:
            self._pan_drag = None
            self._restore_tool_cursor()
            self.update()
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._minimap_drag:
            self._minimap_drag = False
            event.accept()
            return
        if self._pan_drag is not None:
            self._pan_drag = None
            self._restore_tool_cursor()
            self.update()
            event.accept()
            return
        if self._pan_candidate is not None:
            if self._deselect_pending and self._selected_roi is not None:
                self._selected_roi = None
                self.roi_selected.emit(None)
            self._pan_candidate = None
            self._deselect_pending = False
            self.update()
            event.accept()
            return
        if self._draft is not None:
            kind, anchor, current = self._draft
            self._draft = None
            if kind == "cursor":
                self.roi_drawn.emit("cursor", (current,))
            else:
                distance = ((current[0] - anchor[0]) ** 2 + (current[1] - anchor[1]) ** 2) ** 0.5
                if distance >= 2.0:
                    self.roi_drawn.emit(kind, (anchor, current))
            self.update()
            return
        if self._drag is not None:
            moved = self._drag_shape
            original = self._drag["orig"]
            self._drag = None
            self._drag_shape = None
            if moved is not None and moved.points != original.points:
                self.roi_moved.emit(moved)
            self.update()
            return
        super().mouseReleaseEvent(event)

    def _dragged_shape(self, image_pos: tuple[float, float]):
        drag = self._drag
        original = drag["orig"]
        if drag["handle"] == "body":
            dx = image_pos[0] - drag["start"][0]
            dy = image_pos[1] - drag["start"][1]
            dx, dy = self._clamped_delta(original.points, dx, dy)
            points = tuple((x + dx, y + dy) for x, y in original.points)
            return replace(original, points=points)
        if drag["handle"] in {"p0", "p1"} and len(original.points) == 2:
            index = 0 if drag["handle"] == "p0" else 1
            points = list(original.points)
            points[index] = image_pos
            return replace(original, points=tuple(points))
        # corner handles on rect/ellipse: anchor opposite corner
        first, second = original.points
        anchors = {
            "nw": second,
            "se": first,
            "ne": (first[0], second[1]),
            "sw": (second[0], first[1]),
        }
        anchor = anchors.get(drag["handle"])
        if anchor is None:
            return original
        return replace(original, points=(anchor, image_pos))

    def _clamped_delta(
        self, points, dx: float, dy: float
    ) -> tuple[float, float]:
        if self._raw is None:
            return (0.0, 0.0)
        max_x, max_y = self._raw.shape[1] - 1.0, self._raw.shape[0] - 1.0
        for x, y in points:
            dx = max(-x, min(dx, max_x - x))
            dy = max(-y, min(dy, max_y - y))
        return (dx, dy)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Delete and self._selected_roi is not None:
            self.roi_delete_requested.emit(self._selected_roi)
            event.accept()
            return
        super().keyPressEvent(event)

    def leaveEvent(self, event) -> None:
        if self._probe is not None:
            self._probe = None
            self.probe_changed.emit(None)
            self.update()
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.fullscreen_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def _distance_to_segment(point, start, end) -> float:
    sx, sy = start.x(), start.y()
    ex, ey = end.x(), end.y()
    length_sq = (ex - sx) ** 2 + (ey - sy) ** 2
    if length_sq <= 0:
        return ((point.x() - sx) ** 2 + (point.y() - sy) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((point.x() - sx) * (ex - sx) + (point.y() - sy) * (ey - sy)) / length_sq))
    proj_x = sx + t * (ex - sx)
    proj_y = sy + t * (ey - sy)
    return ((point.x() - proj_x) ** 2 + (point.y() - proj_y) ** 2) ** 0.5


class ColorScaleWidget(QWidget):
    """Vertical color bar with value ticks and draggable isotherm limits (§4.4)."""

    isotherm_dragged = Signal(str, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(112)
        self._palette = "Iron"
        self._minimum = 0.0
        self._maximum = 1.0
        self._active = False
        self._invert = False
        self._unit = ""
        self._bar_rect = QRect()
        self._iso_mode = "off"
        self._iso_limit1 = 0.0
        self._iso_limit2 = 1.0
        self._dragging: str | None = None

    def set_scale(
        self,
        palette: str,
        minimum: float,
        maximum: float,
        invert: bool = False,
        unit: str = "",
    ) -> None:
        self._palette = palette
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._invert = bool(invert)
        self._unit = unit
        self._active = True
        self.update()

    def set_isotherm(self, mode: str, limit1: float, limit2: float) -> None:
        self._iso_mode = mode
        self._iso_limit1 = float(limit1)
        self._iso_limit2 = float(limit2)
        self.update()

    def clear(self) -> None:
        self._active = False
        self._dragging = None
        self.update()

    # --- value mapping ---------------------------------------------------------

    def _value_to_y(self, value: float) -> int:
        bar = self._bar_rect
        span = self._maximum - self._minimum
        fraction = 0.0 if span == 0 else (float(value) - self._minimum) / span
        fraction = max(0.0, min(1.0, fraction))
        return bar.bottom() - int(round(fraction * bar.height()))

    def _y_to_value(self, y: float) -> float:
        bar = self._bar_rect
        if bar.height() <= 0:
            return self._minimum
        fraction = (bar.bottom() - float(y)) / bar.height()
        fraction = max(0.0, min(1.0, fraction))
        return self._minimum + fraction * (self._maximum - self._minimum)

    def _iso_thresholds(self) -> list[tuple[str, float]]:
        if self._iso_mode == "interval":
            return [("l1", self._iso_limit1), ("l2", self._iso_limit2)]
        if self._iso_mode in {"above", "below"}:
            return [("l1", self._iso_limit1)]
        return []

    def _iso_zone(self) -> tuple[int, int] | None:
        """Pixel span (top_y, bottom_y) of the isotherm band on the bar."""
        bar = self._bar_rect
        if self._iso_mode == "above":
            return (bar.top(), self._value_to_y(self._iso_limit1))
        if self._iso_mode == "below":
            return (self._value_to_y(self._iso_limit1), bar.bottom())
        if self._iso_mode == "interval":
            top = self._value_to_y(max(self._iso_limit1, self._iso_limit2))
            bottom = self._value_to_y(min(self._iso_limit1, self._iso_limit2))
            return (top, bottom)
        return None

    # --- painting ----------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#06080B"))
        bar = QRect(16, 24, 16, max(40, self.height() - 48))
        self._bar_rect = bar
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not self._active:
            painter.setPen(QPen(QColor("#242D3A"), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(bar, 4, 4)
            return

        if self._unit:
            painter.setFont(QFont("Segoe UI", 9))
            painter.setPen(QColor("#5F6B7A"))
            caption_rect = QRectF(bar.left() - 2, 2, self.width() - bar.left() - 2, 16)
            painter.drawText(
                caption_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                painter.fontMetrics().elidedText(
                    self._unit, Qt.TextElideMode.ElideRight, int(caption_rect.width())
                ),
            )

        lut = np.ascontiguousarray(palette_lut(self._palette, self._invert)[::-1]).reshape(256, 1, 3)
        image = QImage(
            lut.data,
            1,
            256,
            int(lut.strides[0]),
            QImage.Format.Format_RGB888,
        ).copy()
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(bar), 4, 4)
        painter.setClipPath(clip)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(bar, image)
        zone = self._iso_zone()
        if zone is not None:
            top_y, bottom_y = zone
            painter.fillRect(
                QRect(bar.left(), top_y, bar.width() + 1, max(1, bottom_y - top_y)),
                QColor(*isotherm_color(self._iso_mode)),
            )
        painter.setClipping(False)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#242D3A"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(bar, 4, 4)
        painter.setFont(QFont("Consolas", 10))
        for step in range(6):
            fraction = step / 5.0
            y = bar.bottom() - int(fraction * bar.height())
            value = self._minimum + fraction * (self._maximum - self._minimum)
            painter.setPen(QPen(QColor("#49586C"), 1))
            painter.drawLine(bar.right() + 3, y, bar.right() + 8, y)
            label = self._format_tick(value)
            painter.setPen(QColor("#97A1AF"))
            painter.drawText(
                QRectF(bar.right() + 13, y - 11, 58, 22),
                Qt.AlignmentFlag.AlignVCenter,
                label,
            )
        self._paint_thresholds(painter, bar)

    def _paint_thresholds(self, painter: QPainter, bar: QRect) -> None:
        color = QColor(*isotherm_color(self._iso_mode))
        for _, value in self._iso_thresholds():
            y = self._value_to_y(value)
            painter.setPen(QPen(QColor("#07090A"), 3))
            painter.drawLine(bar.left() - 1, y, bar.right() + 1, y)
            painter.setPen(QPen(color, 1))
            painter.drawLine(bar.left() - 1, y, bar.right() + 1, y)
            handle = QPainterPath()
            handle.moveTo(QPointF(bar.left() - 10, y))
            handle.lineTo(QPointF(bar.left() - 2, y - 5))
            handle.lineTo(QPointF(bar.left() - 2, y + 5))
            handle.closeSubpath()
            painter.setPen(QPen(QColor("#07090A"), 1))
            painter.setBrush(color)
            painter.drawPath(handle)

    @staticmethod
    def _format_tick(value: float) -> str:
        if abs(value) >= 100 or float(value).is_integer():
            return f"{value:.0f}"
        return f"{value:.2f}"

    # --- isotherm dragging ---------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._active
            and self._iso_thresholds()
        ):
            y = event.position().y()
            x = event.position().x()
            for which, value in self._iso_thresholds():
                if abs(y - self._value_to_y(value)) <= 6 and x <= self._bar_rect.right() + 1:
                    self._dragging = which
                    self.setCursor(Qt.CursorShape.SizeVerCursor)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging is not None:
            self.isotherm_dragged.emit(self._dragging, self._y_to_value(event.position().y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging is not None:
            self._dragging = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ObjectParametersPanel(QFrame):
    """Editable measurement (object) parameters, ResearchIR §4.8.1 style."""

    applied = Signal(dict)
    reset_requested = Signal()

    # key, label, minimum, maximum, decimals, multiplier from display to SDK units
    FIELDS: tuple[tuple[str, str, float, float, int, float], ...] = (
        ("emissivity", "Emissivity", 0.0, 1.0, 2, 1.0),
        ("reflected_temp", "Reflected Temp (K)", 0.0, 1000.0, 1, 1.0),
        ("atmosphere_temp", "Atmosphere Temp (K)", 0.0, 1000.0, 1, 1.0),
        ("est_atmospheric_transmission", "Atm. Transmission", 0.0, 1.0, 2, 1.0),
        ("distance", "Distance (m)", 0.0, 100000.0, 1, 1.0),
        ("relative_humidity", "Rel. Humidity (%)", 0.0, 100.0, 0, 0.01),
        ("ext_optics_temp", "Ext. Optics Temp (K)", 0.0, 1000.0, 1, 1.0),
        ("ext_optics_transmission", "Ext. Optics Transm.", 0.0, 1.0, 2, 1.0),
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("InfoPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        self._spins: dict[str, QDoubleSpinBox] = {}
        self._scales: dict[str, float] = {}
        # Core rows stay visible; the atmosphere/optics rows fold away (§P8).
        for key, label, minimum, maximum, decimals, scale in self.FIELDS[:2]:
            layout.addLayout(self._field_row(key, label, minimum, maximum, decimals, scale))

        disc_row = QHBoxLayout()
        self.advanced_button = QToolButton()
        self.advanced_button.setObjectName("DisclosureButton")
        self.advanced_button.setText("Atmosphere && Optics")
        self.advanced_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_button.setIcon(awesome_icon("fa6s.chevron-right", ICON_MUTED))
        self.advanced_button.setIconSize(QSize(11, 11))
        self.advanced_button.setCheckable(True)
        self.advanced_button.toggled.connect(self._toggle_advanced)
        disc_row.addWidget(self.advanced_button)
        disc_row.addStretch(1)
        layout.addLayout(disc_row)

        self._advanced = QWidget()
        advanced_layout = QVBoxLayout(self._advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(4)
        for key, label, minimum, maximum, decimals, scale in self.FIELDS[2:]:
            advanced_layout.addLayout(
                self._field_row(key, label, minimum, maximum, decimals, scale)
            )
        self._advanced.setVisible(False)
        layout.addWidget(self._advanced)

        self._spins["est_atmospheric_transmission"].setToolTip(
            "Estimated atmospheric transmission (0 = compute automatically)"
        )

        self.reset_button = QPushButton("Reset to File Values")
        self.reset_button.setProperty("variant", "ghost")
        self.reset_button.setToolTip("Restore the object parameters stored in the recording")
        self.reset_button.clicked.connect(self.reset_requested)
        layout.addWidget(self.reset_button)

    def _field_row(
        self,
        key: str,
        label: str,
        minimum: float,
        maximum: float,
        decimals: int,
        scale: float,
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        text = QLabel(label)
        text.setObjectName("FieldLabel")
        row.addWidget(text, 1)
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        spin.setFixedWidth(88)  # uniform field length regardless of value width
        spin.setProperty("compact", True)
        spin.editingFinished.connect(self._emit_applied)
        row.addWidget(spin)
        self._spins[key] = spin
        self._scales[key] = scale
        return row

    def _toggle_advanced(self, checked: bool) -> None:
        self._advanced.setVisible(checked)
        icon = "fa6s.chevron-down" if checked else "fa6s.chevron-right"
        self.advanced_button.setIcon(awesome_icon(icon, ICON_MUTED))

    def set_parameters(self, snapshot: dict) -> None:
        for key, spin in self._spins.items():
            if key not in snapshot:
                continue
            spin.blockSignals(True)
            spin.setValue(float(snapshot[key]) / self._scales[key])
            spin.blockSignals(False)

    def _emit_applied(self) -> None:
        self.applied.emit(
            {key: spin.value() * self._scales[key] for key, spin in self._spins.items()}
        )


class ElidingLabel(QLabel):
    """QLabel that elides overflowing text and shows the full text as a tooltip.

    Used for unconstrained SDK strings (camera model/serial, unit labels)
    inside the fixed-width inspector, where wrapping is not an option and the
    horizontal scrollbar is disabled. The Ignored horizontal size policy keeps
    long content from stretching the layout; the text is re-elided on resize.
    """

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt naming)
        self._full_text = text
        self._apply_elide()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = self.width()
        if width <= 0:
            super().setText(self._full_text)
            return
        elided = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, width
        )
        super().setText(elided)
        self.setToolTip(self._full_text if elided != self._full_text else "")


class InspectorPanel(QWidget):
    unit_changed = Signal(str)
    palette_changed = Signal(str)
    palette_invert_toggled = Signal(bool)
    palette_edit_requested = Signal()
    range_mode_changed = Signal(str)
    fixed_range_changed = Signal(float, float)
    enhancement_changed = Signal(str, float)
    segmentation_changed = Signal(bool, float, float)
    isotherm_changed = Signal(str, float, float)
    object_parameters_applied = Signal(dict)
    object_parameters_reset = Signal()
    overlays_changed = Signal(bool, bool)
    flips_changed = Signal(bool, bool)
    corrections_changed = Signal(bool, bool)
    reference_requested = Signal()
    reference_cleared = Signal()
    filters_changed = Signal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Inspector")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(320)
        self._data_available = False
        self._busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("InspectorScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("InspectorContent")
        scroll.setWidget(content)
        layout.addWidget(scroll)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(8)

        heading = QLabel("Visualization")
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        layout.addSpacing(12)

        layout.addWidget(self._field_label("Unit"))
        self.unit_combo = ChevronComboBox()
        self.unit_combo.setIconSize(QSize(18, 18))
        self.unit_combo.setEnabled(False)
        self.unit_combo.currentIndexChanged.connect(self._unit_selected)
        layout.addWidget(self.unit_combo)
        layout.addSpacing(12)

        palette_header = QHBoxLayout()
        palette_header.setSpacing(8)
        palette_label = self._field_label("Color Map")
        palette_header.addWidget(palette_label, 1)
        self.palette_invert_check = QCheckBox("Invert")
        self.palette_invert_check.setToolTip("Reverse the color map")
        self.palette_invert_check.toggled.connect(self.palette_invert_toggled)
        palette_header.addWidget(self.palette_invert_check)
        self.palette_edit_button = QToolButton()
        self.palette_edit_button.setObjectName("TransportButton")
        self.palette_edit_button.setIcon(awesome_icon("fa6s.pen", ICON_TEXT))
        self.palette_edit_button.setIconSize(QSize(13, 13))
        self.palette_edit_button.setToolTip("Create / edit custom palettes")
        self.palette_edit_button.clicked.connect(self.palette_edit_requested)
        palette_header.addWidget(self.palette_edit_button)
        layout.addLayout(palette_header)
        self.palette_combo = ChevronComboBox()
        self.palette_combo.setIconSize(QSize(46, 14))
        for name in palette_names():
            self.palette_combo.addItem(self._palette_icon(name), name, name)
        self.palette_combo.currentTextChanged.connect(self.palette_changed)
        layout.addWidget(self.palette_combo)
        layout.addSpacing(12)

        layout.addWidget(self._field_label("Range Mode"))
        segmented = QFrame()
        segmented.setObjectName("Segmented")
        range_row = QHBoxLayout(segmented)
        range_row.setContentsMargins(2, 2, 2, 2)
        range_row.setSpacing(2)
        self.dynamic_button = QPushButton("Dynamic")
        self.roi_button = QPushButton("ROI")
        self.fixed_button = QPushButton("Fixed")
        for button in (self.dynamic_button, self.roi_button, self.fixed_button):
            button.setObjectName("SegmentButton")
            button.setCheckable(True)
        self.range_group = QButtonGroup(self)
        self.range_group.setExclusive(True)
        self.range_group.addButton(self.dynamic_button)
        self.range_group.addButton(self.roi_button)
        self.range_group.addButton(self.fixed_button)
        self.dynamic_button.setChecked(True)
        self.dynamic_button.clicked.connect(lambda: self._set_range_mode("dynamic"))
        self.roi_button.clicked.connect(lambda: self._set_range_mode("roi"))
        self.fixed_button.clicked.connect(lambda: self._set_range_mode("fixed"))
        range_row.addWidget(self.dynamic_button)
        range_row.addWidget(self.roi_button)
        range_row.addWidget(self.fixed_button)
        layout.addWidget(segmented)
        layout.addSpacing(4)

        self.range_panel = QFrame()
        self.range_panel.setObjectName("RangePanel")
        self.range_panel.setMinimumHeight(104)
        range_panel_layout = QVBoxLayout(self.range_panel)
        range_panel_layout.setContentsMargins(14, 10, 14, 12)
        range_panel_layout.setSpacing(8)
        range_title = QLabel("Fixed Range")
        range_title.setObjectName("FieldLabel")
        range_panel_layout.addWidget(range_title)
        range_fields = QHBoxLayout()
        range_fields.setSpacing(8)
        self.minimum_spin = self._range_spin()
        self.maximum_spin = self._range_spin()
        range_fields.addWidget(self.minimum_spin)
        dash = QLabel("–")
        dash.setObjectName("FieldLabel")
        dash.setAlignment(Qt.AlignmentFlag.AlignCenter)
        range_fields.addWidget(dash)
        range_fields.addWidget(self.maximum_spin)
        range_panel_layout.addLayout(range_fields)
        self.minimum_spin.editingFinished.connect(self._fixed_values_edited)
        self.maximum_spin.editingFinished.connect(self._fixed_values_edited)
        self.range_panel.setEnabled(False)
        layout.addWidget(self.range_panel)

        layout.addSpacing(8)

        layout.addWidget(self._field_label("Enhancement"))
        self.enhancement_combo = ChevronComboBox()
        self.enhancement_combo.addItem("Linear (AGC)", "linear")
        self.enhancement_combo.addItem("Plateau Equalization", "pe")
        self.enhancement_combo.currentIndexChanged.connect(self._enhancement_changed)
        layout.addWidget(self.enhancement_combo)
        self.pe_row = QWidget()
        pe_layout = QHBoxLayout(self.pe_row)
        pe_layout.setContentsMargins(0, 0, 0, 0)
        pe_layout.setSpacing(8)
        pe_label = QLabel("Aggressiveness")
        pe_label.setObjectName("FieldLabel")
        pe_layout.addWidget(pe_label, 1)
        self.pe_spin = QDoubleSpinBox()
        self.pe_spin.setRange(0.0, 100.0)
        self.pe_spin.setDecimals(0)
        self.pe_spin.setValue(50.0)
        self.pe_spin.setSuffix(" %")
        self.pe_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.pe_spin.setMinimumWidth(80)
        self.pe_spin.setMaximumWidth(96)
        self.pe_spin.setProperty("compact", True)
        self.pe_spin.setToolTip("Plateau clip level: higher is a stronger equalization")
        self.pe_spin.editingFinished.connect(self._enhancement_changed)
        pe_layout.addWidget(self.pe_spin)
        layout.addWidget(self.pe_row)
        self.pe_row.setVisible(False)

        layout.addWidget(self._field_label("Segmentation"))
        self.segmentation_check = QCheckBox("Enable")
        self.segmentation_check.setToolTip(
            "Limit the valid value range; out-of-range pixels are painted blue/red"
        )
        layout.addWidget(self.segmentation_check)
        seg_row = QHBoxLayout()
        seg_row.setSpacing(8)
        self.seg_min_spin = self._compact_spin()
        self.seg_max_spin = self._compact_spin()
        _, seg_min_box = self._captioned("Min", self.seg_min_spin)
        _, seg_max_box = self._captioned("Max", self.seg_max_spin)
        seg_row.addLayout(seg_min_box, 1)
        seg_row.addLayout(seg_max_box, 1)
        self.segmentation_check.toggled.connect(self._segmentation_changed)
        self.seg_min_spin.editingFinished.connect(self._segmentation_changed)
        self.seg_max_spin.editingFinished.connect(self._segmentation_changed)
        layout.addLayout(seg_row)

        layout.addWidget(self._field_label("Isotherm"))
        self.isotherm_combo = ChevronComboBox()
        for label, mode in (
            ("Off", "off"),
            ("Above", "above"),
            ("Below", "below"),
            ("Interval", "interval"),
        ):
            self.isotherm_combo.addItem(label, mode)
        self.isotherm_combo.setToolTip("Highlight pixels above/below/within value limits")
        layout.addWidget(self.isotherm_combo)
        iso_row = QHBoxLayout()
        iso_row.setSpacing(8)
        self.iso_limit1_spin = self._compact_spin()
        self.iso_limit2_spin = self._compact_spin()
        self._iso_caption1, iso_box1 = self._captioned("Threshold", self.iso_limit1_spin)
        self._iso_caption2, iso_box2 = self._captioned("High", self.iso_limit2_spin)
        iso_row.addLayout(iso_box1, 1)
        iso_row.addLayout(iso_box2, 1)
        self.isotherm_combo.currentIndexChanged.connect(self._isotherm_changed)
        self.iso_limit1_spin.editingFinished.connect(self._isotherm_changed)
        self.iso_limit2_spin.editingFinished.connect(self._isotherm_changed)
        self._iso_caption1.setVisible(False)
        self._iso_caption2.setVisible(False)
        self.iso_limit1_spin.setVisible(False)
        self.iso_limit2_spin.setVisible(False)
        layout.addLayout(iso_row)

        layout.addWidget(self._field_label("Overlays"))
        overlay_row = QHBoxLayout()
        overlay_row.setSpacing(12)
        self.clipping_check = QCheckBox("Clipping")
        self.clipping_check.setChecked(True)
        self.clipping_check.setToolTip("Highlight pixels outside the calibration range")
        self.markers_check = QCheckBox("Min/Max")
        self.markers_check.setToolTip("Mark min/max pixel locations of the image and ROIs")
        overlay_row.addWidget(self.clipping_check)
        overlay_row.addWidget(self.markers_check)
        overlay_row.addStretch(1)
        self.clipping_check.toggled.connect(self._overlays_changed)
        self.markers_check.toggled.connect(self._overlays_changed)
        layout.addLayout(overlay_row)

        layout.addWidget(self._field_label("Image"))
        flip_row = QHBoxLayout()
        flip_row.setSpacing(8)
        self.flip_h_button = QToolButton()
        self.flip_h_button.setObjectName("AnalysisButton")
        self.flip_h_button.setIcon(awesome_icon("fa6s.arrows-left-right", ICON_TEXT))
        self.flip_h_button.setIconSize(QSize(16, 16))
        self.flip_h_button.setToolTip("Flip image horizontally")
        self.flip_h_button.setCheckable(True)
        self.flip_v_button = QToolButton()
        self.flip_v_button.setObjectName("AnalysisButton")
        self.flip_v_button.setIcon(awesome_icon("fa6s.arrows-up-down", ICON_TEXT))
        self.flip_v_button.setIconSize(QSize(16, 16))
        self.flip_v_button.setToolTip("Flip image vertically")
        self.flip_v_button.setCheckable(True)
        flip_row.addWidget(self.flip_h_button)
        flip_row.addWidget(self.flip_v_button)
        flip_row.addStretch(1)
        self.flip_h_button.toggled.connect(self._flips_changed)
        self.flip_v_button.toggled.connect(self._flips_changed)
        layout.addLayout(flip_row)

        self.corrections_heading = self._field_label("Corrections")
        layout.addWidget(self.corrections_heading)
        self.corrections_row = QWidget()
        corrections_layout = QHBoxLayout(self.corrections_row)
        corrections_layout.setContentsMargins(0, 0, 0, 0)
        corrections_layout.setSpacing(12)
        self.nuc_check = QCheckBox("NUC")
        self.nuc_check.setToolTip("Apply the PC-side non-uniformity correction stored in the file")
        self.bp_check = QCheckBox("Bad Pixels")
        self.bp_check.setToolTip("Apply the bad-pixel map stored in the file")
        corrections_layout.addWidget(self.nuc_check)
        corrections_layout.addWidget(self.bp_check)
        corrections_layout.addStretch(1)
        self.nuc_check.toggled.connect(self._corrections_changed)
        self.bp_check.toggled.connect(self._corrections_changed)
        layout.addWidget(self.corrections_row)
        self.corrections_heading.hide()
        self.corrections_row.hide()

        layout.addSpacing(8)
        layout.addWidget(self._hairline())
        layout.addSpacing(12)

        self.params_heading = QLabel("Measurement")
        self.params_heading.setObjectName("SectionTitle")
        layout.addWidget(self.params_heading)
        layout.addSpacing(2)
        self.params_panel = ObjectParametersPanel()
        self.params_panel.applied.connect(self.object_parameters_applied)
        self.params_panel.reset_requested.connect(self.object_parameters_reset)
        layout.addWidget(self.params_panel)

        layout.addSpacing(8)
        layout.addWidget(self._hairline())
        layout.addSpacing(12)

        self.processing_heading = QLabel("Processing")
        self.processing_heading.setObjectName("SectionTitle")
        layout.addWidget(self.processing_heading)
        layout.addSpacing(2)
        processing_panel = QFrame()
        processing_panel.setObjectName("InfoPanel")
        processing_layout = QVBoxLayout(processing_panel)
        processing_layout.setContentsMargins(14, 10, 14, 12)
        processing_layout.setSpacing(6)

        ref_row = QHBoxLayout()
        ref_row.setSpacing(8)
        self.reference_button = QPushButton("Set Reference…")
        self.reference_button.setToolTip(
            "Apply an operation against a reference frame (File Operation)"
        )
        self.reference_button.clicked.connect(self.reference_requested)
        ref_row.addWidget(self.reference_button, 1)
        self.reference_clear_button = QToolButton()
        self.reference_clear_button.setObjectName("TransportButton")
        self.reference_clear_button.setIcon(awesome_icon("fa6s.xmark", ICON_SECONDARY))
        self.reference_clear_button.setIconSize(QSize(14, 14))
        self.reference_clear_button.setToolTip("Clear the reference")
        self.reference_clear_button.clicked.connect(self.reference_cleared)
        ref_row.addWidget(self.reference_clear_button)
        processing_layout.addLayout(ref_row)
        self.reference_label = QLabel("No reference")
        self.reference_label.setObjectName("InfoValue")
        self.reference_label.setWordWrap(True)
        processing_layout.addWidget(self.reference_label)

        self.point_combo, self.point_spin = self._filter_row(
            processing_layout,
            "Point Filter",
            (
                ("None", "none"),
                ("Gain ×", "gain"),
                ("Offset +", "offset"),
                ("Exp", "exp"),
                ("Ln", "ln"),
                ("Sqrt", "sqrt"),
            ),
            value=1.0,
        )
        self.spatial_combo, self.spatial_spin = self._filter_row(
            processing_layout,
            "Spatial Filter",
            (
                ("None", "none"),
                ("Gaussian", "gaussian"),
                ("Window Average", "average"),
                ("Median", "median"),
            ),
            value=3,
            integer=True,
            minimum=3,
            maximum=15,
        )
        # resolution-dependent median cap (memory budget); set on recording open
        self._median_size_cap: int | None = None
        self.temporal_combo, self.temporal_spin = self._filter_row(
            processing_layout,
            "Temporal Filter",
            (
                ("None", "none"),
                ("Min", "min"),
                ("Max", "max"),
                ("Frame Average", "average"),
                ("Sliding Subtract", "subtract"),
            ),
            value=5,
            integer=True,
            minimum=2,
            maximum=30,
        )
        layout.addWidget(processing_panel)
        self.processing_panel = processing_panel

        layout.addStretch(1)
        layout.addSpacing(8)
        layout.addWidget(self._hairline())
        layout.addSpacing(12)

        self.info_heading = QLabel("Frame Info")
        self.info_heading.setObjectName("SectionTitle")
        layout.addWidget(self.info_heading)
        layout.addSpacing(2)
        self.info_panel = QFrame()
        self.info_panel.setObjectName("InfoPanel")
        info_layout = QGridLayout(self.info_panel)
        info_layout.setContentsMargins(14, 12, 14, 12)
        info_layout.setHorizontalSpacing(12)
        info_layout.setVerticalSpacing(4)
        info_layout.setColumnStretch(1, 1)
        self._info_values: dict[str, QLabel] = {}
        for row, key in enumerate(("Frame", "Unit", "Range", "Resolution", "Camera", "Cursor")):
            caption = QLabel(key)
            caption.setObjectName("Caption")
            value = ElidingLabel("—")
            value.setObjectName("InfoValue")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            info_layout.addWidget(caption, row, 0)
            info_layout.addWidget(value, row, 1)
            self._info_values[key] = value
        layout.addWidget(self.info_panel)
        self._refresh_enabled_state()

    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        return label

    @staticmethod
    def _hairline() -> QFrame:
        line = QFrame()
        line.setObjectName("Hairline")
        line.setFixedHeight(1)
        return line

    @staticmethod
    def _captioned(caption: str, widget) -> tuple[QLabel, QVBoxLayout]:
        """Small tertiary caption above an input (Min/Max, Threshold, …)."""
        label = QLabel(caption)
        label.setObjectName("Caption")
        box = QVBoxLayout()
        box.setSpacing(2)
        box.addWidget(label)
        box.addWidget(widget)
        return label, box

    def _filter_row(
        self,
        parent_layout: QVBoxLayout,
        title: str,
        options: tuple[tuple[str, str], ...],
        value: float,
        integer: bool = False,
        minimum: float = -1.0e6,
        maximum: float = 1.0e6,
    ):
        """A filter combo + parameter spin row used by the Processing section."""
        parent_layout.addWidget(self._field_label(title))
        row = QHBoxLayout()
        row.setSpacing(8)
        combo = ChevronComboBox()
        for label, key in options:
            combo.addItem(label, key)
        row.addWidget(combo, 1)
        if integer:
            spin = QSpinBox()
            spin.setRange(int(minimum), int(maximum))
            spin.setValue(int(value))
        else:
            spin = QDoubleSpinBox()
            spin.setRange(float(minimum), float(maximum))
            spin.setDecimals(2)
            spin.setValue(float(value))
        spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        spin.setMinimumWidth(64)
        spin.setMaximumWidth(80)
        spin.setProperty("compact", True)
        row.addWidget(spin)
        parent_layout.addLayout(row)
        combo.currentIndexChanged.connect(self._filters_changed)
        spin.valueChanged.connect(self._filters_changed)
        return combo, spin

    def set_reference_label(self, text: str) -> None:
        self.reference_label.setText(text or "No reference")

    def reset_processing(self) -> None:
        """Restore Processing controls to their inactive defaults (new file)."""
        for combo, spin, value in (
            (self.point_combo, self.point_spin, 1),
            (self.spatial_combo, self.spatial_spin, 3),
            (self.temporal_combo, self.temporal_spin, 5),
        ):
            combo.blockSignals(True)
            spin.blockSignals(True)
            combo.setCurrentIndex(0)
            spin.setValue(value)
            combo.blockSignals(False)
            spin.blockSignals(False)
        self.set_reference_label("")
        self._filters_changed(update_only=True)

    def _filters_changed(self, *_args, update_only: bool = False) -> None:
        point_key = str(self.point_combo.currentData())
        self.point_spin.setVisible(point_key in {"gain", "offset"})
        self.spatial_spin.setVisible(str(self.spatial_combo.currentData()) != "none")
        self.temporal_spin.setVisible(str(self.temporal_combo.currentData()) != "none")
        self._apply_spatial_size_cap()
        if update_only:
            return
        self.filters_changed.emit(
            {
                "point": (point_key, float(self.point_spin.value())),
                "spatial": (
                    str(self.spatial_combo.currentData()),
                    int(self.spatial_spin.value()),
                ),
                "temporal": (
                    str(self.temporal_combo.currentData()),
                    int(self.temporal_spin.value()),
                ),
            }
        )

    def set_median_size_cap(self, cap: int | None) -> None:
        """Set the resolution-dependent median kernel cap (memory budget).

        The processing layer clamps oversized median kernels; the spinner
        must never offer a kernel different from the one applied (R5)."""
        self._median_size_cap = cap
        self._apply_spatial_size_cap()

    def _apply_spatial_size_cap(self) -> None:
        """Cap the spatial spinner only while Median is selected — Gaussian
        and window-average share the row and do not need the budget clamp."""
        is_median = str(self.spatial_combo.currentData()) == "median"
        cap = self._median_size_cap if is_median else None
        maximum = 15 if cap is None else max(3, min(15, int(cap)))
        if self.spatial_spin.maximum() != maximum:
            # setMaximum clamps an out-of-range value, emitting valueChanged →
            # the corrected state is dispatched to the processing layer
            self.spatial_spin.setMaximum(maximum)
        if is_median and maximum < 15:
            self.spatial_spin.setToolTip(
                f"Median kernel is capped at {maximum}×{maximum} for this "
                f"resolution (memory budget)"
            )
        else:
            self.spatial_spin.setToolTip("")

    @staticmethod
    def _range_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(-1.0e12, 1.0e12)
        spin.setDecimals(2)
        spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        spin.setMinimumWidth(90)
        return spin

    @staticmethod
    def _compact_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(-1.0e12, 1.0e12)
        spin.setDecimals(2)
        spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        spin.setMinimumWidth(72)
        spin.setMaximumWidth(88)
        spin.setProperty("compact", True)
        return spin

    @staticmethod
    def _palette_icon(name: str) -> QIcon:
        lut = palette_lut(name).reshape(1, 256, 3)
        image = QImage(
            lut.data,
            256,
            1,
            int(lut.strides[0]),
            QImage.Format.Format_RGB888,
        ).copy()
        return QIcon(QPixmap.fromImage(image).scaled(48, 14))

    def set_available_units(self, options: Iterable[UnitOption], selected_key: str) -> None:
        self.unit_combo.blockSignals(True)
        self.unit_combo.clear()
        selected_index = 0
        for index, option in enumerate(options):
            self.unit_combo.addItem(awesome_icon("fa6s.grip", ICON_MUTED), option.label, option.key)
            if option.key == selected_key:
                selected_index = index
        self.unit_combo.setCurrentIndex(selected_index)
        self.unit_combo.blockSignals(False)
        self._refresh_enabled_state()

    def add_palette(self, name: str, select: bool = True) -> None:
        """Append a (custom) palette to the combo if missing and optionally select it."""
        index = self.palette_combo.findText(name)
        if index < 0:
            self.palette_combo.addItem(self._palette_icon(name), name, name)
            index = self.palette_combo.count() - 1
        if select:
            self.palette_combo.setCurrentIndex(index)

    def remove_palette(self, name: str, fallback: str = "Iron") -> None:
        index = self.palette_combo.findText(name)
        if index < 0:
            return
        was_current = self.palette_combo.currentIndex() == index
        self.palette_combo.blockSignals(True)
        self.palette_combo.removeItem(index)
        if was_current:
            fallback_index = max(0, self.palette_combo.findText(fallback))
            self.palette_combo.setCurrentIndex(fallback_index)
        self.palette_combo.blockSignals(False)
        if was_current:
            self.palette_changed.emit(self.palette_combo.currentText())

    def set_palette_inverted(self, inverted: bool) -> None:
        self.palette_invert_check.blockSignals(True)
        self.palette_invert_check.setChecked(bool(inverted))
        self.palette_invert_check.blockSignals(False)

    def set_fixed_values(self, minimum: float, maximum: float) -> None:
        for spin, value in ((self.minimum_spin, minimum), (self.maximum_spin, maximum)):
            spin.blockSignals(True)
            spin.setValue(float(value))
            spin.blockSignals(False)

    def set_range_mode(self, mode: str) -> None:
        fixed = mode == "fixed"
        self.fixed_button.setChecked(fixed)
        self.roi_button.setChecked(mode == "roi")
        self.dynamic_button.setChecked(mode == "dynamic")
        self._refresh_enabled_state()

    def set_data_available(self, available: bool) -> None:
        self._data_available = bool(available)
        self._refresh_enabled_state()

    def set_object_parameters(self, snapshot: dict) -> None:
        self.params_panel.set_parameters(snapshot)

    def set_corrections(self, state: dict) -> None:
        """Show correction toggles only when the file carries them (§4.7)."""
        has_nuc = bool(state.get("has_nuc", False))
        has_bp = bool(state.get("has_bp", False))
        available = has_nuc or has_bp
        self.corrections_heading.setVisible(available)
        self.corrections_row.setVisible(available)
        for check, has, apply in (
            (self.nuc_check, has_nuc, state.get("apply_nuc", False)),
            (self.bp_check, has_bp, state.get("apply_bp", False)),
        ):
            check.setVisible(has)
            check.blockSignals(True)
            check.setChecked(bool(apply))
            check.blockSignals(False)
        self._refresh_enabled_state()

    def _corrections_changed(self) -> None:
        self.corrections_changed.emit(
            self.nuc_check.isChecked(), self.bp_check.isChecked()
        )

    def set_frame_info(
        self,
        metadata: VideoMetadata,
        frame_index: int,
        unit: UnitOption,
        range_mode: str,
    ) -> None:
        camera = metadata.camera_model or ""
        if camera and metadata.camera_serial:
            camera = f"{camera} · {metadata.camera_serial}"
        values = {
            "Frame": f"{frame_index + 1} / {metadata.num_frames}",
            "Unit": unit.label,
            "Range": range_mode.title(),
            "Resolution": f"{metadata.width}×{metadata.height}",
            "Camera": camera or "—",
        }
        for key, text in values.items():
            label = self._info_values[key]
            if label.text() != text:  # only the frame number changes per frame
                label.setText(text)

    def set_probe(self, probe, suffix: str) -> None:
        if probe is None:
            self._info_values["Cursor"].setText("—")
            return
        x, y, value = probe
        self._info_values["Cursor"].setText(f"x {x}, y {y} · {format_value(value, suffix)}")

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._refresh_enabled_state()

    def _unit_selected(self, index: int) -> None:
        key = self.unit_combo.itemData(index)
        if key:
            self.unit_changed.emit(str(key))

    def _set_range_mode(self, mode: str) -> None:
        self.set_range_mode(mode)
        self.range_mode_changed.emit(mode)

    def _enhancement_changed(self) -> None:
        mode = str(self.enhancement_combo.currentData())
        self.pe_row.setVisible(mode == "pe")
        self.enhancement_changed.emit(mode, self.pe_spin.value() / 100.0)

    def set_segmentation_values(self, minimum: float, maximum: float) -> None:
        for spin, value in ((self.seg_min_spin, minimum), (self.seg_max_spin, maximum)):
            spin.blockSignals(True)
            spin.setValue(float(value))
            spin.blockSignals(False)

    def _segmentation_changed(self) -> None:
        enabled = self.segmentation_check.isChecked()
        self.seg_min_spin.setEnabled(enabled and self._data_available and not self._busy)
        self.seg_max_spin.setEnabled(enabled and self._data_available and not self._busy)
        self.segmentation_changed.emit(
            enabled, self.seg_min_spin.value(), self.seg_max_spin.value()
        )

    def set_isotherm_values(self, limit1: float, limit2: float) -> None:
        for spin, value in ((self.iso_limit1_spin, limit1), (self.iso_limit2_spin, limit2)):
            spin.blockSignals(True)
            spin.setValue(float(value))
            spin.blockSignals(False)

    def _isotherm_changed(self) -> None:
        mode = str(self.isotherm_combo.currentData())
        active = mode != "off" and self._data_available and not self._busy
        interval = mode == "interval"
        self._iso_caption1.setText("Low" if interval else "Threshold")
        self._iso_caption1.setVisible(mode != "off")
        self._iso_caption2.setVisible(interval)
        self.iso_limit1_spin.setVisible(mode != "off")
        self.iso_limit2_spin.setVisible(interval)
        self.iso_limit1_spin.setEnabled(active)
        self.iso_limit2_spin.setEnabled(active and interval)
        self.isotherm_changed.emit(
            mode, self.iso_limit1_spin.value(), self.iso_limit2_spin.value()
        )

    def _fixed_values_edited(self) -> None:
        self.fixed_range_changed.emit(self.minimum_spin.value(), self.maximum_spin.value())

    def _overlays_changed(self) -> None:
        self.overlays_changed.emit(
            self.clipping_check.isChecked(), self.markers_check.isChecked()
        )

    def _flips_changed(self) -> None:
        self.flips_changed.emit(
            self.flip_h_button.isChecked(), self.flip_v_button.isChecked()
        )

    def _refresh_enabled_state(self) -> None:
        enabled = self._data_available and not self._busy
        self.unit_combo.setEnabled(enabled and self.unit_combo.count() > 0)
        self.palette_combo.setEnabled(enabled)
        self.palette_invert_check.setEnabled(enabled)
        self.palette_edit_button.setEnabled(enabled)
        self.dynamic_button.setEnabled(enabled)
        self.roi_button.setEnabled(enabled)
        self.fixed_button.setEnabled(enabled)
        self.range_panel.setEnabled(enabled and self.fixed_button.isChecked())
        self.enhancement_combo.setEnabled(enabled)
        self.pe_spin.setEnabled(enabled)
        self.segmentation_check.setEnabled(enabled)
        seg_on = enabled and self.segmentation_check.isChecked()
        self.seg_min_spin.setEnabled(seg_on)
        self.seg_max_spin.setEnabled(seg_on)
        self.isotherm_combo.setEnabled(enabled)
        iso_mode = str(self.isotherm_combo.currentData() or "off")
        iso_on = enabled and iso_mode != "off"
        self.iso_limit1_spin.setEnabled(iso_on)
        self.iso_limit2_spin.setEnabled(iso_on and iso_mode == "interval")
        self.params_panel.setEnabled(enabled)
        self.clipping_check.setEnabled(enabled)
        self.markers_check.setEnabled(enabled)
        self.flip_h_button.setEnabled(enabled)
        self.flip_v_button.setEnabled(enabled)
        self.nuc_check.setEnabled(enabled)
        self.bp_check.setEnabled(enabled)
        self.reference_button.setEnabled(enabled)
        self.reference_clear_button.setEnabled(enabled)
        self.point_combo.setEnabled(enabled)
        self.point_spin.setEnabled(enabled)
        self.spatial_combo.setEnabled(enabled)
        self.spatial_spin.setEnabled(enabled)
        self.temporal_combo.setEnabled(enabled)
        self.temporal_spin.setEnabled(enabled)
        self._filters_changed(update_only=True)


class SourceInfoPanel(QFrame):
    """Static per-recording property table (ResearchIR §4.8.3)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Property", "Value"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

    def set_details(self, rows: tuple[tuple[str, str], ...]) -> None:
        self.table.setRowCount(len(rows))
        for row, (key, value) in enumerate(rows):
            key_item = QTableWidgetItem(key)
            key_item.setForeground(QColor("#97A1AF"))
            self.table.setItem(row, 0, key_item)
            self.table.setItem(row, 1, value_table_item(value))


class FramelessDialog(QDialog):
    """Modal dialog with a themed title row instead of the native title bar.

    Windows keeps native title bars light even for dark apps, so dialogs draw
    their own: title on the left, close button on the right. Dragging the row
    moves the dialog; the close button and Esc reject it. Subclasses build
    their content in ``self.body`` (a QVBoxLayout below the title row).
    """

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setWindowTitle(title)

        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 10, 16, 16)
        self.body.setSpacing(10)

        title_bar = QWidget(self)
        title_bar.setObjectName("DialogTitleBar")
        title_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(title_bar)
        row.setContentsMargins(0, 0, 0, 6)
        row.setSpacing(8)
        label = QLabel(title, title_bar)
        label.setObjectName("DialogTitle")
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        close = QToolButton(title_bar)
        close.setObjectName("TabCloseButton")
        close.setIcon(awesome_icon("fa6s.xmark", ICON_SECONDARY))
        close.setIconSize(QSize(13, 13))
        close.setToolTip("Close")
        close.clicked.connect(self.reject)
        row.addWidget(label, 1)
        row.addWidget(close)
        self._title_bar = title_bar
        self.body.addWidget(title_bar)

    def mousePressEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._title_bar.geometry().contains(event.position().toPoint())
        ):
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def _add_size_grip(self) -> None:
        """Bottom-right grip restoring the resizing lost with the native frame.

        For dialogs with expanding content (scroll areas, file lists); call at
        the end of the subclass constructor so the grip stays at the bottom.
        """
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        row.addWidget(QSizeGrip(self))
        self.body.addLayout(row)


class MetadataPickerDialog(FramelessDialog):
    """Checkbox list selecting which frame metadata entries are displayed."""

    def __init__(self, names: list[str], hidden: set[str], parent=None) -> None:
        super().__init__("Choose Metadata Entries", parent)
        self.setMinimumWidth(360)
        layout = self.body

        buttons_row = QHBoxLayout()
        all_button = QPushButton("All")
        all_button.setProperty("variant", "ghost")
        none_button = QPushButton("None")
        none_button.setProperty("variant", "ghost")
        buttons_row.addStretch(1)
        buttons_row.addWidget(all_button)
        buttons_row.addWidget(none_button)
        layout.addLayout(buttons_row)

        scroll = QScrollArea()
        scroll.setObjectName("PickerScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("PickerContent")
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(2)
        self._boxes: dict[str, QCheckBox] = {}
        for name in names:
            box = QCheckBox(name)
            box.setMinimumHeight(26)
            box.setChecked(name not in hidden)
            content_layout.addWidget(box)
            self._boxes[name] = box
        content_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll)

        all_button.clicked.connect(lambda: self._set_all(True))
        none_button.clicked.connect(lambda: self._set_all(False))

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.button(QDialogButtonBox.StandardButton.Ok).setProperty("accent", True)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        self._add_size_grip()

    def _set_all(self, checked: bool) -> None:
        for box in self._boxes.values():
            box.setChecked(checked)

    def hidden_names(self) -> set[str]:
        return {name for name, box in self._boxes.items() if not box.isChecked()}


class GradientStripWidget(QWidget):
    """Gradient preview with draggable color stops (ResearchIR §4.9.4.3)."""

    stops_changed = Signal()

    HANDLE_HEIGHT = 10

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(False)
        self._stops: list[list] = [[0.0, (0, 0, 0)], [1.0, (255, 255, 255)]]
        self._selected: int | None = None
        self._dragging: int | None = None

    def set_stops(self, stops) -> None:
        self._stops = [[float(p), tuple(int(c) for c in rgb)] for p, rgb in stops]
        if len(self._stops) < 2:
            self._stops = [[0.0, (0, 0, 0)], [1.0, (255, 255, 255)]]
        self._selected = None
        self._dragging = None
        self.stops_changed.emit()
        self.update()

    def stops(self) -> list[tuple[float, tuple[int, int, int]]]:
        return [(p, tuple(rgb)) for p, rgb in sorted(self._stops, key=lambda s: s[0])]

    def selected_stop(self) -> int | None:
        return self._selected

    def selected_color(self) -> tuple[int, int, int] | None:
        if self._selected is None:
            return None
        return tuple(self._stops[self._selected][1])

    def set_selected_color(self, rgb: tuple[int, int, int]) -> None:
        if self._selected is None:
            return
        self._stops[self._selected][1] = tuple(int(c) for c in rgb)
        self.stops_changed.emit()
        self.update()

    def remove_selected(self) -> None:
        if self._selected is None or len(self._stops) <= 2:
            return
        del self._stops[self._selected]
        self._selected = None
        self._dragging = None
        self.stops_changed.emit()
        self.update()

    def _strip_rect(self) -> QRect:
        return QRect(10, 8, max(20, self.width() - 20), 26)

    def _x_to_pos(self, x: float) -> float:
        rect = self._strip_rect()
        return max(0.0, min(1.0, (x - rect.left()) / max(1, rect.width())))

    def _pos_to_x(self, pos: float) -> float:
        rect = self._strip_rect()
        return rect.left() + pos * rect.width()

    def _handle_at(self, wpos: QPointF) -> int | None:
        rect = self._strip_rect()
        for index, (pos, _rgb) in enumerate(self._stops):
            hx = self._pos_to_x(pos)
            if (
                abs(wpos.x() - hx) <= 7
                and rect.bottom() - 2 <= wpos.y() <= rect.bottom() + self.HANDLE_HEIGHT + 4
            ):
                return index
        return None

    def _color_at(self, pos: float) -> tuple[int, int, int]:
        try:
            lut = lut_from_stops(self.stops())
        except ValueError:
            return (0, 0, 0)
        index = max(0, min(255, int(round(pos * 255))))
        r, g, b = lut[index]
        return (int(r), int(g), int(b))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0B0F14"))
        rect = self._strip_rect()
        try:
            lut = lut_from_stops(self.stops())
        except ValueError:
            lut = None
        if lut is not None:
            row = np.ascontiguousarray(lut.reshape(1, 256, 3))
            image = QImage(
                row.data, 256, 1, int(row.strides[0]), QImage.Format.Format_RGB888
            ).copy()
            painter.drawImage(rect, image)
        painter.setPen(QPen(QColor("#333E4D"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)
        for index, (pos, rgb) in enumerate(self._stops):
            x = self._pos_to_x(pos)
            top = rect.bottom() + 2
            handle = QPainterPath()
            handle.moveTo(QPointF(x, top + self.HANDLE_HEIGHT))
            handle.lineTo(QPointF(x - 6, top))
            handle.lineTo(QPointF(x + 6, top))
            handle.closeSubpath()
            selected = index == self._selected
            painter.setPen(QPen(QColor(ICON_ACCENT) if selected else QColor("#07090A"), 2))
            painter.setBrush(QColor(*rgb))
            painter.drawPath(handle)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        wpos = event.position()
        index = self._handle_at(wpos)
        if event.button() == Qt.MouseButton.RightButton:
            if index is not None and len(self._stops) > 2:
                del self._stops[index]
                self._selected = None
                self.stops_changed.emit()
                self.update()
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if index is not None:
            self._selected = index
            self._dragging = index
        elif self._strip_rect().contains(wpos.toPoint()):
            pos = self._x_to_pos(wpos.x())
            self._stops.append([pos, self._color_at(pos)])
            self._selected = len(self._stops) - 1
            self._dragging = self._selected
            self.stops_changed.emit()
        else:
            self._selected = None
        self.update()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging is None:
            super().mouseMoveEvent(event)
            return
        self._stops[self._dragging][0] = self._x_to_pos(event.position().x())
        self.stops_changed.emit()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging is not None:
            self._dragging = None
            # Re-sync selection with the sorted stop order.
            self._selected = None
            self.stops_changed.emit()
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class PaletteEditorDialog(FramelessDialog):
    """Create or edit a custom gradient palette (ResearchIR §4.9.4.3)."""

    def __init__(
        self,
        parent=None,
        name: str = "",
        stops=None,
        existing: bool = False,
    ) -> None:
        super().__init__("Edit Palette" if existing else "New Palette", parent)
        self.setMinimumWidth(420)
        self.deleted = False

        layout = self.body

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_label = QLabel("Name")
        name_label.setObjectName("FieldLabel")
        name_row.addWidget(name_label)
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("Palette name")
        name_row.addWidget(self.name_edit, 1)
        layout.addLayout(name_row)

        self.strip = GradientStripWidget()
        if stops:
            self.strip.set_stops(stops)
        layout.addWidget(self.strip)

        hint = QLabel("Click the bar to add a stop · drag to move · right-click a stop to remove")
        hint.setObjectName("FieldLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        tools_row = QHBoxLayout()
        tools_row.setSpacing(8)
        self.color_button = QPushButton("Color…")
        self.color_button.setToolTip("Change the color of the selected stop")
        self.color_button.clicked.connect(self._pick_color)
        tools_row.addWidget(self.color_button)
        self.remove_button = QPushButton("Remove Stop")
        self.remove_button.setToolTip("Remove the selected stop (at least two remain)")
        self.remove_button.clicked.connect(self.strip.remove_selected)
        tools_row.addWidget(self.remove_button)
        tools_row.addStretch(1)
        if existing:
            self.delete_button = QPushButton("Delete Palette")
            self.delete_button.clicked.connect(self._delete)
            tools_row.addWidget(self.delete_button)
        layout.addLayout(tools_row)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.button(QDialogButtonBox.StandardButton.Ok).setProperty("accent", True)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _pick_color(self) -> None:
        current = self.strip.selected_color()
        if current is None:
            return
        color = QColorDialog.getColor(QColor(*current), self, "Stop Color")
        if color.isValid():
            self.strip.set_selected_color(
                (color.red(), color.green(), color.blue())
            )

    def _delete(self) -> None:
        self.deleted = True
        self.accept()

    def palette_name(self) -> str:
        return self.name_edit.text().strip()

    def palette_stops(self) -> list[tuple[float, tuple[int, int, int]]]:
        return self.strip.stops()


class ReferenceDialog(FramelessDialog):
    """Choose a reference frame and operation for File Operation (§4.9.5.3)."""

    OPERATIONS: tuple[tuple[str, str], ...] = (
        ("Subtract", "subtract"),
        ("Add", "add"),
        ("Multiply", "multiply"),
        ("Divide", "divide"),
    )

    def __init__(self, metadata: VideoMetadata, parent=None) -> None:
        super().__init__("Reference Frame", parent)
        self.setMinimumWidth(420)

        layout = self.body

        file_label = QLabel("Reference source")
        file_label.setObjectName("FieldLabel")
        layout.addWidget(file_label)
        file_row = QHBoxLayout()
        file_row.setSpacing(8)
        self.path_edit = QLineEdit(str(metadata.path))
        file_row.addWidget(self.path_edit, 1)
        browse = QToolButton()
        browse.setText("…")
        browse.setToolTip("Choose a recording")
        browse.clicked.connect(self._browse)
        file_row.addWidget(browse)
        layout.addLayout(file_row)

        row = QHBoxLayout()
        row.setSpacing(8)
        frame_label = QLabel("Frame")
        frame_label.setObjectName("FieldLabel")
        row.addWidget(frame_label)
        self.frame_spin = QSpinBox()
        self.frame_spin.setRange(1, max(1, metadata.num_frames))
        self.frame_spin.setValue(1)
        self.frame_spin.setToolTip(
            "1-based; clamped to the frame count of the chosen file"
        )
        row.addWidget(self.frame_spin)
        row.addSpacing(12)
        op_label = QLabel("Operation")
        op_label.setObjectName("FieldLabel")
        row.addWidget(op_label)
        self.op_combo = ChevronComboBox()
        for label, key in self.OPERATIONS:
            self.op_combo.addItem(label, key)
        row.addWidget(self.op_combo, 1)
        layout.addLayout(row)

        hint = QLabel(
            "The reference is converted to the current unit when supported; "
            "its image size must match the open recording."
        )
        hint.setObjectName("FieldLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.button(QDialogButtonBox.StandardButton.Ok).setProperty("accent", True)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose reference recording",
            str(Path(self.path_edit.text()).parent),
            "FLIR recordings (*.seq *.SEQ *.ats *.ATS *.sfmov *.SFMOV *.csq *.CSQ "
            "*.fff *.FFF *.ptw *.PTW *.tif *.TIF *.tiff *.TIFF);;All files (*.*)",
        )
        if path:
            self.path_edit.setText(path)

    def parameters(self) -> dict:
        return {
            "path": self.path_edit.text().strip(),
            "frame_index": max(0, self.frame_spin.value() - 1),
            "op": str(self.op_combo.currentData()),
        }


class MetadataPanel(QFrame):
    """Per-frame header metadata table with user-pickable entries (§4.10)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        hint = QLabel("Frame header entries")
        hint.setObjectName("FieldLabel")
        header.addWidget(hint)
        header.addStretch(1)
        self.choose_button = QToolButton()
        self.choose_button.setObjectName("TransportButton")
        self.choose_button.setIcon(awesome_icon("fa6s.filter", ICON_TEXT))
        self.choose_button.setIconSize(QSize(15, 15))
        self.choose_button.setToolTip("Choose which entries to display")
        self.choose_button.clicked.connect(self._choose)
        header.addWidget(self.choose_button)
        layout.addLayout(header)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Name", "Value"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        self._entries: tuple[tuple[str, str], ...] = ()
        self._known: list[str] = []
        settings = QSettings("Local", "FLIR Thermal Player")
        self._hidden: set[str] = set(settings.value("metadata/hidden", [], type=list))

    def set_entries(self, entries: tuple[tuple[str, str], ...]) -> None:
        self._entries = tuple(entries)
        for name, _ in entries:
            if name not in self._known:
                self._known.append(name)
        self._render()

    def _render(self) -> None:
        visible = [(n, v) for n, v in self._entries if n not in self._hidden]
        self.table.setRowCount(len(visible))
        for row, (name, value) in enumerate(visible):
            name_item = QTableWidgetItem(name)
            name_item.setForeground(QColor("#97A1AF"))
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, value_table_item(value))

    def _choose(self) -> None:
        if not self._known:
            return
        dialog = MetadataPickerDialog(self._known, self._hidden, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._hidden = dialog.hidden_names()
            QSettings("Local", "FLIR Thermal Player").setValue(
                "metadata/hidden", sorted(self._hidden)
            )
            self._render()


class BottomPanel(QFrame):
    """Tabbed analysis container docked above the transport bar."""

    close_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("TransportBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 8)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("BottomTabs")
        self.statistics = StatisticsPanel()
        self.temporal = TemporalPlotPanel()
        self.profile = ProfilePlotPanel()
        self.histogram = HistogramPlotPanel()
        self.metadata = MetadataPanel()
        self.source = SourceInfoPanel()
        self.tabs.addTab(self.statistics, "Statistics")
        self.tabs.addTab(self.temporal, "Temporal")
        self.tabs.addTab(self.profile, "Profile")
        self.tabs.addTab(self.histogram, "Histogram")
        self.tabs.addTab(self.metadata, "Metadata")
        self.tabs.addTab(self.source, "Source")
        layout.addWidget(self.tabs)

        close_button = QToolButton()
        close_button.setObjectName("TabCloseButton")
        close_button.setIcon(awesome_icon("fa6s.xmark", ICON_SECONDARY))
        close_button.setIconSize(QSize(13, 13))
        close_button.setToolTip("Hide analysis panel")
        close_button.clicked.connect(self.close_requested)
        self.tabs.setCornerWidget(close_button)


class TimelineSlider(QSlider):
    """Seek slider with draggable start/end play-range markers (ResearchIR §4.2)."""

    range_changed = Signal(int, int)

    _HANDLE_RADIUS = 8
    _GROOVE_HEIGHT = 6
    _MARKER_HIT_PX = 7

    def __init__(self, orientation, parent=None) -> None:
        super().__init__(orientation, parent)
        self._range_start: int | None = None
        self._range_end: int | None = None
        self._dragging_marker: str | None = None

    # --- play-range state ---------------------------------------------------

    def play_range(self) -> tuple[int, int] | None:
        if self._range_start is None or self._range_end is None:
            return None
        return (min(self._range_start, self._range_end), max(self._range_start, self._range_end))

    def set_play_range(self, start: int, end: int) -> None:
        self._range_start = max(self.minimum(), min(int(start), self.maximum()))
        self._range_end = max(self.minimum(), min(int(end), self.maximum()))
        self.update()
        self._emit_range()

    def clear_play_range(self) -> None:
        self._range_start = None
        self._range_end = None
        self._dragging_marker = None
        self.update()

    def _emit_range(self) -> None:
        play_range = self.play_range()
        if play_range is not None:
            self.range_changed.emit(play_range[0], play_range[1])

    # --- geometry -----------------------------------------------------------

    def _groove_rect(self) -> QRect:
        margin = self._HANDLE_RADIUS
        return QRect(
            margin,
            (self.height() - self._GROOVE_HEIGHT) // 2,
            max(1, self.width() - 2 * margin),
            self._GROOVE_HEIGHT,
        )

    def _value_to_x(self, value: float) -> float:
        groove = self._groove_rect()
        span = self.maximum() - self.minimum()
        fraction = 0.0 if span <= 0 else (value - self.minimum()) / span
        return groove.left() + max(0.0, min(1.0, fraction)) * groove.width()

    def _x_to_value(self, x: float) -> int:
        groove = self._groove_rect()
        if groove.width() <= 0:
            return self.minimum()
        fraction = (x - groove.left()) / groove.width()
        span = self.maximum() - self.minimum()
        return int(round(self.minimum() + max(0.0, min(1.0, fraction)) * span))

    # --- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        groove = self._groove_rect()
        enabled = self.isEnabled()
        groove_color = QColor("#333E4D") if enabled else QColor("#242D3A")
        accent = QColor("#F5A524") if enabled else QColor("#242D3A")

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(groove_color)
        painter.drawRoundedRect(groove, 3, 3)

        play_range = self.play_range()
        if play_range is not None:
            x0 = self._value_to_x(play_range[0])
            x1 = self._value_to_x(play_range[1])
            band = QRectF(
                x0,
                groove.center().y() - 5.0,
                max(2.0, x1 - x0),
                10.0,
            )
            painter.setBrush(QColor(245, 165, 36, 46 if enabled else 20))
            painter.drawRoundedRect(band, 3, 3)

        handle_x = self._value_to_x(self.value())
        sub = QRectF(groove)
        sub.setRight(handle_x)
        painter.setBrush(accent)
        painter.drawRoundedRect(sub, 3, 3)

        if play_range is not None:
            for value in play_range:
                self._paint_marker(painter, self._value_to_x(value), groove, enabled)

        handle_color = QColor("#F5A524") if enabled else QColor("#5F6B7A")
        painter.setBrush(handle_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(
            QPointF(handle_x, groove.center().y()),
            self._HANDLE_RADIUS,
            self._HANDLE_RADIUS,
        )
        painter.end()

    def _paint_marker(self, painter: QPainter, x: float, groove: QRect, enabled: bool) -> None:
        top = groove.top() - 8
        marker = QPainterPath()
        marker.moveTo(QPointF(x, groove.top() - 1))
        marker.lineTo(QPointF(x - 5, top))
        marker.lineTo(QPointF(x + 5, top))
        marker.closeSubpath()
        painter.setPen(QPen(QColor("#07090A"), 1))
        painter.setBrush(QColor("#F5A524") if enabled else QColor("#5F6B7A"))
        painter.drawPath(marker)

    # --- marker dragging ------------------------------------------------------

    def _marker_at(self, x: float) -> str | None:
        play_range = self.play_range()
        if play_range is None or not self.isEnabled():
            return None
        for which, value in (("start", play_range[0]), ("end", play_range[1])):
            if abs(x - self._value_to_x(value)) <= self._MARKER_HIT_PX:
                return which
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            marker = self._marker_at(event.position().x())
            if marker is not None:
                self._dragging_marker = marker
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging_marker is not None:
            value = self._x_to_value(event.position().x())
            if self._dragging_marker == "start":
                self._range_start = value
            else:
                self._range_end = value
            self.update()
            self._emit_range()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging_marker is not None:
            self._dragging_marker = None
            self.update()
            self._emit_range()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class TransportBar(QWidget):
    play_toggled = Signal()
    seek_requested = Signal(int)
    scrub_preview = Signal(int)
    speed_changed = Signal(float)
    loop_toggled = Signal(bool)
    constant_rate_toggled = Signal(bool)
    fullscreen_requested = Signal()

    _RATE_TOOLTIP = (
        "Constant-rate playback (R)\n\n"
        "Off: frames are paced from their recorded timestamps, so a recording "
        "with dropped frames plays back as unevenly as it was captured.\n"
        "On: frames are paced evenly at the recording's average rate. Total "
        "duration is unchanged; the elapsed-time readout still shows recorded "
        "time, so it jumps across gaps."
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("TransportBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(84)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 14, 14, 14)
        layout.setSpacing(10)

        self.previous_button = self._transport_button("fa6s.backward-step")
        self.play_button = self._transport_button("fa6s.play", play=True)
        self.next_button = self._transport_button("fa6s.forward-step")
        self.previous_button.setToolTip("Previous frame (Left)")
        self.play_button.setToolTip("Play / pause (Space)")
        self.next_button.setToolTip("Next frame (Right)")
        self.previous_button.clicked.connect(lambda: self._seek_relative(-1))
        self.play_button.clicked.connect(self.play_toggled)
        self.next_button.clicked.connect(lambda: self._seek_relative(1))
        layout.addWidget(self.previous_button)
        layout.addWidget(self.play_button)
        layout.addWidget(self.next_button)
        layout.addSpacing(14)

        self.current_time = QLabel("00:00.000")
        self.current_time.setObjectName("TimeCurrent")
        self.current_time.setMinimumWidth(78)
        self.current_time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.current_time)

        self.slider = TimelineSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setEnabled(False)
        self.slider.sliderReleased.connect(lambda: self.seek_requested.emit(self.slider.value()))
        self.slider.valueChanged.connect(self._slider_value_changed)
        layout.addWidget(self.slider, 1)

        self.total_time = QLabel("00:00.000")
        self.total_time.setObjectName("TimeLabel")
        self.total_time.setMinimumWidth(78)
        layout.addWidget(self.total_time)
        layout.addSpacing(10)

        badge = QFrame()
        badge.setObjectName("TransportBadge")
        badge_layout = QHBoxLayout(badge)
        badge_layout.setContentsMargins(16, 0, 16, 0)
        self.frame_label = QLabel("Frame — / —")
        self.frame_label.setObjectName("FrameLabel")
        badge_layout.addWidget(self.frame_label)
        badge.setMinimumHeight(40)
        badge.setMinimumWidth(146)
        layout.addWidget(badge)

        self.speed_combo = ChevronComboBox()
        self.speed_combo.setMinimumWidth(92)
        for label, value in (("0.25×", 0.25), ("0.5×", 0.5), ("1×", 1.0), ("2×", 2.0), ("4×", 4.0)):
            self.speed_combo.addItem(label, value)
        self.speed_combo.setCurrentIndex(2)
        self.speed_combo.currentIndexChanged.connect(
            lambda index: self.speed_changed.emit(float(self.speed_combo.itemData(index)))
        )
        layout.addWidget(self.speed_combo)

        self.loop_button = self._transport_button("fa6s.repeat")
        self.loop_button.setProperty("small", True)
        self.loop_button.setToolTip("Loop playback (L)")
        self.loop_button.setCheckable(True)
        self.loop_button.toggled.connect(self.loop_toggled)
        layout.addWidget(self.loop_button)

        self.constant_rate_button = self._transport_button("fa6s.wave-square")
        self.constant_rate_button.setProperty("small", True)
        self.constant_rate_button.setToolTip(self._RATE_TOOLTIP)
        self.constant_rate_button.setCheckable(True)
        self.constant_rate_button.toggled.connect(self.constant_rate_toggled)
        layout.addWidget(self.constant_rate_button)

        self.fullscreen_button = self._transport_button("fa6s.expand")
        self.fullscreen_button.setProperty("small", True)
        self.fullscreen_button.setToolTip("Full screen (F)")
        self.fullscreen_button.clicked.connect(self.fullscreen_requested)
        layout.addWidget(self.fullscreen_button)
        layout.addWidget(QSizeGrip(self))

        self.set_enabled(False)

    def _transport_button(self, icon_name: str, play: bool = False) -> QToolButton:
        button = QToolButton()
        if play:
            button.setObjectName("PlayButton")
            button.setIcon(awesome_icon(icon_name, ICON_ON_ACCENT))
            button.setIconSize(QSize(20, 20))
        else:
            button.setObjectName("TransportButton")
            button.setIcon(awesome_icon(icon_name, ICON_TEXT))
            button.setIconSize(QSize(17, 17))
        return button

    def set_enabled(self, enabled: bool) -> None:
        for widget in (
            self.previous_button,
            self.play_button,
            self.next_button,
            self.slider,
            self.speed_combo,
            self.loop_button,
            self.constant_rate_button,
            self.fullscreen_button,
        ):
            widget.setEnabled(enabled)

    def set_loop(self, loop: bool) -> None:
        self.loop_button.blockSignals(True)
        self.loop_button.setChecked(bool(loop))
        self.loop_button.blockSignals(False)

    def set_constant_rate(self, constant: bool) -> None:
        self.constant_rate_button.blockSignals(True)
        self.constant_rate_button.setChecked(bool(constant))
        self.constant_rate_button.blockSignals(False)

    def set_cadence(self, cadence: CadenceInfo | None) -> None:
        """Point the rate toggle at the open recording's measured cadence.

        Recordings whose capture grid is intact need no explanation; the ones
        that dropped frames get the numbers appended to the tooltip, so the
        control that fixes the symptom also states the cause.
        """
        tooltip = self._RATE_TOOLTIP
        if cadence is not None and not cadence.is_even:
            tooltip += (
                f"\n\nThis recording stored {cadence.stored_frames} of "
                f"{cadence.expected_frames} frames ({cadence.kept_fraction * 100:.1f} %) "
                f"at {cadence.base_fps:.2f} Hz — its uneven playback is in the file."
            )
        self.constant_rate_button.setToolTip(tooltip)

    def set_video(self, num_frames: int, duration: float) -> None:
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, num_frames - 1))
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        from .render import format_time

        self.total_time.setText(format_time(duration))
        self.frame_label.setText(f"Frame 1 / {num_frames}")
        self.set_enabled(True)

    def set_frame(self, index: int, num_frames: int, seconds: float) -> None:
        from .render import format_time

        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(index)
            self.slider.blockSignals(False)
        self.current_time.setText(format_time(seconds))
        self.frame_label.setText(f"Frame {index + 1} / {num_frames}")

    def set_playing(self, playing: bool) -> None:
        self.play_button.setIcon(awesome_icon("fa6s.pause" if playing else "fa6s.play", ICON_ON_ACCENT))
        self.play_button.setToolTip("Pause (Space)" if playing else "Play (Space)")

    def current_speed(self) -> float:
        return float(self.speed_combo.currentData())

    def _seek_relative(self, delta: int) -> None:
        self.seek_requested.emit(max(self.slider.minimum(), min(self.slider.value() + delta, self.slider.maximum())))

    def _slider_value_changed(self, value: int) -> None:
        if self.slider.isSliderDown():
            self.scrub_preview.emit(value)
