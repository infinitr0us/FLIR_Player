# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from conftest import wait_until

from PySide6.QtCore import QPointF

from flir_player.main_window import MainWindow
from flir_player.widgets import ThermalCanvas

SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"


def _canvas_with_frame(qapp, width: int = 200, height: int = 100) -> ThermalCanvas:
    canvas = ThermalCanvas()
    canvas.show()
    canvas.resize(800, 600)
    qapp.processEvents()
    rgb = np.zeros((height, width, 3), dtype=np.uint8)
    raw = np.arange(width * height, dtype=float).reshape(height, width)
    canvas.set_frame(np.ascontiguousarray(rgb), raw, "counts")
    qapp.processEvents()
    canvas.repaint()
    return canvas


def test_fit_is_default_and_zoom_rect_scales(qapp) -> None:
    canvas = _canvas_with_frame(qapp)
    try:
        canvas.repaint()
        fit_rect = canvas._image_rect
        assert canvas._zoom is None
        assert fit_rect.width() == 800  # fit: min(800/200, 600/100) = 4
        assert fit_rect.height() == 400

        canvas.set_zoom_level(2.0)
        canvas.repaint()
        assert canvas._image_rect.width() == 400
        assert canvas._image_rect.height() == 200

        canvas.set_zoom_fit()
        canvas.repaint()
        assert canvas._image_rect == fit_rect
    finally:
        canvas.close()


def test_zoom_at_keeps_anchor_stationary(qapp) -> None:
    canvas = _canvas_with_frame(qapp)
    try:
        canvas.repaint()
        anchor = QPointF(200, 300)
        image_point = canvas._widget_to_image(anchor)
        canvas.zoom_at(1.25, anchor)  # fit=4 → 5
        assert canvas._zoom == pytest.approx(5.0)
        mapped = canvas._image_to_widget(*image_point)
        assert mapped.x() == pytest.approx(anchor.x(), abs=1.5)
        assert mapped.y() == pytest.approx(anchor.y(), abs=1.5)
    finally:
        canvas.close()


def test_pan_is_clamped_to_image_edges(qapp) -> None:
    canvas = _canvas_with_frame(qapp)
    try:
        canvas.set_zoom_level(8.0)  # 1600×800 image in an 800×600 viewport
        canvas._pan = QPointF(10000, -10000)
        canvas._clamp_pan()
        assert canvas._pan.x() == pytest.approx((1600 - 800) / 2)
        assert canvas._pan.y() == pytest.approx(-(800 - 600) / 2)

        # zoomed-out axis (smaller than viewport) stays centered
        canvas.set_zoom_level(5.0)  # 1000×500
        canvas._pan = QPointF(0, 123)
        canvas._clamp_pan()
        assert canvas._pan.y() == 0.0
    finally:
        canvas.close()


def test_zoom_step_cycles_levels_and_snaps_to_fit(qapp) -> None:
    canvas = _canvas_with_frame(qapp)
    try:
        # fixture fit scale is 4.0 (200×100 image in 800×600)
        canvas.set_zoom_level(4.0)
        canvas.zoom_step(1)  # no larger fixed level → doubles, capped at 8
        assert canvas._zoom == pytest.approx(8.0)
        canvas.zoom_step(-1)  # 4.0 equals the fit scale → snap back to fit
        assert canvas._zoom is None
        canvas.set_zoom_level(2.0)
        canvas.zoom_step(1)  # next level 4.0 ≤ fit → snap to fit
        assert canvas._zoom is None
    finally:
        canvas.close()


def test_minimap_viewport_fraction_and_click(qapp) -> None:
    canvas = _canvas_with_frame(qapp)
    try:
        canvas.repaint()
        assert canvas._visible_display_fraction().width() == pytest.approx(1.0)
        canvas.set_zoom_level(8.0)  # 1600×800 → viewport shows half of the width
        canvas.repaint()
        view = canvas._visible_display_fraction()
        assert view.width() == pytest.approx(0.5)
        assert view.height() == pytest.approx(0.75)

        # clicking the minimap center recenters the viewport (pan ≈ 0)
        canvas._pan = QPointF(100, 0)
        canvas._clamp_pan()
        rect = canvas._minimap_rect
        center = QPointF(rect.left() + rect.width() / 2, rect.top() + rect.height() / 2)
        canvas._pan_minimap_to(center)
        assert canvas._pan.x() == pytest.approx(0.0, abs=2.0)
        assert canvas._pan.y() == pytest.approx(0.0, abs=2.0)
    finally:
        canvas.close()


def test_zoom_resets_on_new_file(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        window.canvas.set_zoom_level(2.0)
        assert window.canvas._zoom is not None
        window.open_path(SAMPLES / "2.seq")
        assert window.canvas._zoom is None
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        assert window.canvas._zoom is None
    finally:
        window.close()
        qapp.processEvents()
