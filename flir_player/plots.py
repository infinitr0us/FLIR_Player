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
        self.history_note = QLabel()
        self.history_note.setObjectName("FieldLabel")
        self.add_header_widget(self.history_note)

        # persistent artists: refreshed via set_data instead of ax.clear()
        self._lines: dict[str, object] = {}
        self._message = None
        self._legend = None

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
        names = [name for name, *_ in series]
        legend_dirty = False
        for stale in [name for name in self._lines if name not in names]:
            self._lines.pop(stale).remove()
            legend_dirty = True
        for name, color, seconds, values in series:
            if seconds.size:
                # appended samples arrive ordered; seeks can insert older ones
                order = (
                    np.argsort(seconds)
                    if np.any(seconds[1:] < seconds[:-1])
                    else slice(None)
                )
                x, y = envelope(seconds[order], values[order])
            else:
                x, y = seconds, values
            line = self._lines.get(name)
            if line is None:
                (line,) = ax.plot(x, y, color=color, linewidth=1.2, label=name)
                self._lines[name] = line
                legend_dirty = True
            else:
                line.set_data(x, y)
        if not series:
            if self._message is None:
                self._message = ax.text(
                    0.5, 0.5, "Play or scrub to collect data",
                    transform=ax.transAxes, ha="center", va="center",
                    color=MUTED, fontsize=10,
                )
        elif self._message is not None:
            self._message.remove()
            self._message = None
        ax.set_xlabel("time (s)", fontsize=8)
        ax.set_ylabel(f"{statistic} ({suffix})" if suffix else statistic, fontsize=8)
        if legend_dirty:
            if self._legend is not None:
                self._legend.remove()
                self._legend = None
            if self._lines:
                self._legend = ax.legend(
                    fontsize=7, facecolor=SURFACE, edgecolor=GRID, labelcolor=INK
                )
        ax.relim()
        ax.autoscale_view()
        self.canvas.draw_idle()


def envelope(
    seconds: np.ndarray, values: np.ndarray, max_points: int = 2000
) -> tuple[np.ndarray, np.ndarray]:
    """Min/max envelope downsampling for display of long temporal series.

    Emits (min, max) per bucket so short thermal excursions stay visible
    instead of being averaged or skipped away. Series at or below
    ``max_points`` are returned unchanged. Bucket edges are linspace-based,
    so the newest remainder is never dropped and the emitted time extent
    always reaches the series' final timestamp.
    """
    if seconds.size <= max_points:
        return seconds, values
    buckets = max(1, max_points // 2)
    edges = np.linspace(0, seconds.size, buckets + 1).astype(int)
    # seconds.size > buckets, so edges are strictly increasing and every
    # reduceat slice is non-empty; the last slice covers the newest remainder
    starts = edges[:-1]
    mids = (starts + edges[1:] - 1) // 2
    out_t = np.repeat(seconds[mids], 2)
    out_t[-1] = seconds[-1]  # keep the chart's right edge at the newest sample
    out_v = np.empty(buckets * 2, dtype=float)
    out_v[0::2] = np.minimum.reduceat(values, starts)
    out_v[1::2] = np.maximum.reduceat(values, starts)
    return out_t, out_v


def line_profile_values(data: np.ndarray, start: tuple[float, float], end: tuple[float, float]):
    from .geometry import roi_coordinates
    from .models import RoiShape
    ys, xs = roi_coordinates(RoiShape(0, "line", (start, end), ""), *data.shape)
    distances = np.hypot(xs.astype(float) - xs[0], ys.astype(float) - ys[0]) if xs.size else np.empty(0)
    return distances, data[ys, xs]


def roi_values(data: np.ndarray, shape) -> np.ndarray:
    from .geometry import roi_coordinates
    ys, xs = roi_coordinates(shape, *data.shape)
    return data[ys, xs]
