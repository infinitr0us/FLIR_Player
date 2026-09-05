# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from conftest import wait_until

from flir_player.main_window import MainWindow
from flir_player.models import RoiShape
from flir_player.plots import line_profile_values, roi_values


SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"

GRID = np.arange(100, dtype=float).reshape(10, 10)


def test_line_profile_values_diagonal() -> None:
    distances, values = line_profile_values(GRID, (0.0, 0.0), (9.0, 9.0))
    assert distances[0] == 0.0 and distances[-1] == pytest.approx(np.hypot(9, 9))
    assert len(values) == 10  # one sample per covered pixel
    assert values[0] == GRID[0, 0]
    assert values[-1] == GRID[9, 9]


def test_roi_values_shapes() -> None:
    rect = RoiShape(id=1, kind="rect", points=((2.0, 1.0), (5.0, 4.0)), name="Box")
    values = roi_values(GRID, rect)
    assert sorted(values) == sorted(GRID[1:4, 2:5].ravel())

    cursor = RoiShape(id=2, kind="cursor", points=((3.0, 4.0),), name="Spot")
    assert roi_values(GRID, cursor) == pytest.approx([GRID[4, 3]])

    ellipse = RoiShape(id=3, kind="ellipse", points=((0.0, 0.0), (9.0, 9.0)), name="E")
    ellipse_values = roi_values(GRID, ellipse)
    assert 0 < ellipse_values.size < 81  # inside the bounding box
    assert GRID[0, 0] not in ellipse_values  # corner is outside the ellipse

    line = RoiShape(id=4, kind="line", points=((0.0, 0.0), (9.0, 0.0)), name="L")
    assert sorted(roi_values(GRID, line)) == sorted(GRID[0, :])


def _open_window(qapp):
    window = MainWindow()
    window.show()
    window.open_path(SAMPLES / "2.seq")
    assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
    return window


def test_profile_tab_shows_line_profile(qapp) -> None:
    window = _open_window(qapp)
    try:
        window._add_roi("line", ((10.0, 10.0), (400.0, 300.0)))
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 1)
        panels = window.bottom_panel
        panels.tabs.setCurrentWidget(panels.profile)
        window._update_plots(window.current_packet, force=True)
        assert len(panels.profile.canvas.ax.lines) == 1
        assert panels.profile.canvas.ax.get_title() == "Line 1"

        # non-line selection shows the hint instead
        window._add_roi("rect", ((100.0, 100.0), (150.0, 140.0)))
        window._update_plots(window.current_packet, force=True)
        assert len(panels.profile.canvas.ax.lines) == 0
        assert panels.profile.canvas.ax.texts
    finally:
        window.close()
        qapp.processEvents()


def test_histogram_tab_whole_image_and_roi(qapp) -> None:
    window = _open_window(qapp)
    try:
        panels = window.bottom_panel
        panels.tabs.setCurrentWidget(panels.histogram)
        window._update_plots(window.current_packet, force=True)
        assert panels.histogram.canvas.ax.get_title() == "Whole image"
        assert len(panels.histogram.canvas.ax.patches) == panels.histogram.BINS

        window._add_roi("rect", ((100.0, 100.0), (150.0, 140.0)))
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 1)
        window._update_plots(window.current_packet, force=True)
        assert panels.histogram.canvas.ax.get_title() == "Box 1"
    finally:
        window.close()
        qapp.processEvents()


def test_temporal_tab_accumulates_and_clears(qapp) -> None:
    window = _open_window(qapp)
    try:
        window._add_roi("rect", ((100.0, 100.0), (150.0, 140.0)))
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 1)
        for index in (20, 30, 40):
            window.seek_to(index)
            assert wait_until(qapp, lambda i=index: window.current_packet.index == i)

        panels = window.bottom_panel
        panels.tabs.setCurrentWidget(panels.temporal)
        window._update_plots(window.current_packet, force=True)
        assert len(window._temporal[1]) >= 4  # open frame + 3 seeks
        assert len(panels.temporal.canvas.ax.lines) == 1
        xdata = panels.temporal.canvas.ax.lines[0].get_xdata()
        assert list(xdata) == sorted(xdata)  # sorted along time even after seeks

        panels.temporal.stat_combo.setCurrentIndex(1)  # Min
        assert len(panels.temporal.canvas.ax.lines) == 1

        panels.temporal.clear_button.click()
        assert window._temporal == {}
    finally:
        window.close()
        qapp.processEvents()
