# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Analysis plot tabs (ResearchIR §4.7.1): profile, histogram, temporal.

matplotlib is embedded via FigureCanvasQTAgg (already a project dependency).
All panels are passive: the main window pushes data in, panels only render it.
"""

from __future__ import annotations

import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
)

SURFACE = "#182029"
INK = "#C7CED8"
GRID = "#242D3A"
MUTED = "#97A1AF"


def _style_axes(ax) -> None:
    ax.set_facecolor(SURFACE)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.5, alpha=0.6)
    ax.yaxis.label.set_color(INK)
    ax.xaxis.label.set_color(INK)
    ax.title.set_color(INK)


class PlotCanvas(FigureCanvasQTAgg):
    def __init__(self) -> None:
        figure = Figure(figsize=(6, 2.2), facecolor=SURFACE, tight_layout=True)
        self.ax = figure.add_subplot(111)
        _style_axes(self.ax)
        super().__init__(figure)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)


class _PlotPanel(QFrame):
    """Base: PlotCanvas with an optional header row (title + controls).

    Panels whose tab name and in-plot title already say what they are pass
    title=None and skip the header to avoid a redundant heading.
    """

    def __init__(self, title: str | None = None, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 8)
        layout.setSpacing(4)

        self._header = QHBoxLayout()
        self._header.setSpacing(8)
        self._header_added = title is not None
        if title is not None:
            self.title_label = QLabel(title)
            self.title_label.setObjectName("FieldLabel")
            self._header.addWidget(self.title_label)
            self._header.addStretch(1)
            layout.addLayout(self._header)

        self.canvas = PlotCanvas()
        layout.addWidget(self.canvas)

    def add_header_widget(self, widget) -> None:
        if not self._header_added:
            self.layout().insertLayout(0, self._header)
            self._header_added = True
        self._header.addWidget(widget)

    def show_message(self, text: str) -> None:
        self.canvas.ax.clear()
        _style_axes(self.canvas.ax)
        self.canvas.ax.text(
            0.5, 0.5, text, transform=self.canvas.ax.transAxes,
            ha="center", va="center", color=MUTED, fontsize=10,
        )
        self.canvas.draw_idle()


class ProfilePlotPanel(_PlotPanel):
    """Values along a line ROI (§4.7.1.2)."""

    def __init__(self, parent=None) -> None:
        super().__init__(None, parent)

    def set_profile(self, distances: np.ndarray, values: np.ndarray, label: str, suffix: str) -> None:
        ax = self.canvas.ax
        ax.clear()
        _style_axes(ax)
        ax.plot(distances, values, color="#F5A524", linewidth=2.0)
        ax.set_xlabel("Distance along line (px)", fontsize=8)
        ax.set_ylabel(suffix or "value", fontsize=8)
        ax.set_title(label, fontsize=9)
        self.canvas.draw_idle()


class HistogramPlotPanel(_PlotPanel):
    """Value distribution of an ROI or the whole image (§4.7.1.4)."""

    BINS = 128

    def __init__(self, parent=None) -> None:
        super().__init__(None, parent)

    def set_values(self, values: np.ndarray, label: str, suffix: str) -> None:
        values = values[np.isfinite(values)]
        if values.size == 0:
            self.show_message("No valid pixels")
            return
        counts, edges = np.histogram(values, bins=self.BINS)
        centers = (edges[:-1] + edges[1:]) / 2.0
        ax = self.canvas.ax
        ax.clear()
        _style_axes(ax)
        ax.bar(centers, counts, width=(edges[1] - edges[0]), color="#4CC2FF", edgecolor="none")
        ax.set_xlabel(suffix or "value", fontsize=8)
        ax.set_ylabel("pixels", fontsize=8)
        ax.set_title(label, fontsize=9)
        self.canvas.draw_idle()


class TemporalPlotPanel(_PlotPanel):
    """ROI statistic versus time (§4.7.1.3). Data accumulates passively."""

    STATISTICS: tuple[tuple[str, str], ...] = (
        ("Mean", "mean"),
        ("Min", "minimum"),
        ("Max", "maximum"),
        ("Std Dev", "std_dev"),
    )

    def __init__(self, parent=None) -> None:
        super().__init__("Temporal", parent)
        self.stat_combo = QComboBox()
        for label, _attr in self.STATISTICS:
            self.stat_combo.addItem(label, label)
        self.stat_combo.setMinimumWidth(120)
        self.add_header_widget(self.stat_combo)

        self.clear_button = QToolButton()
        self.clear_button.setObjectName("TransportButton")
        self.clear_button.setText("Clear")
        self.add_header_widget(self.clear_button)

    @property
    def statistic_label(self) -> str:
        return str(self.stat_combo.currentData())

    def set_series(
        self,
        series: list[tuple[str, str, np.ndarray, np.ndarray]],
        statistic: str,
        suffix: str,
    ) -> None:
        """series: (name, color, seconds, values) per ROI."""
        ax = self.canvas.ax
        ax.clear()
        _style_axes(ax)
        if not series:
            ax.text(
                0.5, 0.5, "Play or scrub to collect data",
                transform=ax.transAxes, ha="center", va="center",
                color=MUTED, fontsize=10,
            )
        for name, color, seconds, values in series:
            if seconds.size:
                order = np.argsort(seconds)
                ax.plot(
                    seconds[order], values[order],
                    color=color, linewidth=1.2, label=name,
                )
        ax.set_xlabel("time (s)", fontsize=8)
        ax.set_ylabel(f"{statistic} ({suffix})" if suffix else statistic, fontsize=8)
        if any(s[2].size for s in series):
            legend = ax.legend(fontsize=7, facecolor=SURFACE, edgecolor=GRID, labelcolor=INK)
        self.canvas.draw_idle()


def line_profile_values(data: np.ndarray, start: tuple[float, float], end: tuple[float, float]):
    """Nearest-neighbor samples along a line; returns (distances, values)."""
    height, width = data.shape
    length = int(round(float(np.hypot(end[0] - start[0], end[1] - start[1])))) + 1
    xs = np.linspace(start[0], end[0], length)
    ys = np.linspace(start[1], end[1], length)
    xi = np.clip(np.round(xs).astype(int), 0, width - 1)
    yi = np.clip(np.round(ys).astype(int), 0, height - 1)
    return np.arange(length, dtype=float), data[yi, xi]


def roi_values(data: np.ndarray, shape) -> np.ndarray:
    """Flat array of pixel values covered by an ROI shape (app-side geometry)."""
    height, width = data.shape

    def clip_point(point):
        return (
            max(0, min(int(round(point[0])), width - 1)),
            max(0, min(int(round(point[1])), height - 1)),
        )

    if shape.kind == "cursor":
        x, y = clip_point(shape.points[0])
        return data[y : y + 1, x : x + 1].ravel()
    if shape.kind == "line":
        _, values = line_profile_values(data, shape.points[0], shape.points[1])
        return np.asarray(values).ravel()
    if len(shape.points) != 2:
        return np.empty(0)
    (x0, y0), (x1, y1) = clip_point(shape.points[0]), clip_point(shape.points[1])
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    if shape.kind == "rect":
        return data[top:bottom, left:right].ravel()
    if shape.kind == "ellipse":
        yy, xx = np.mgrid[top:bottom, left:right]
        cx = (left + right) / 2.0
        cy = (top + bottom) / 2.0
        rx = max((right - left) / 2.0, 0.5)
        ry = max((bottom - top) / 2.0, 0.5)
        mask = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1.0
        return data[top:bottom, left:right][mask].ravel()
    return np.empty(0)
