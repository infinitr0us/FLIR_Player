# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from conftest import wait_until

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent

from flir_player.main_window import MainWindow
from flir_player.models import RoiShape
from flir_player.source import FlirVideoSource


ROOT = Path(__file__).resolve().parents[1]

RECT = ((100.0, 100.0), (150.0, 140.0))


def _open_seq_source() -> FlirVideoSource:
    source = FlirVideoSource()
    source.open(ROOT / "2.seq")
    return source


def test_rect_roi_stats_match_numpy() -> None:
    source = _open_seq_source()
    try:
        shapes = (RoiShape(id=1, kind="rect", points=RECT, name="Box 1"),)
        source.set_rois(shapes)
        packet = source.read_frame(10)
        assert len(packet.roi_stats) == 1
        stats = packet.roi_stats[0]
        assert stats.name == "Box 1"
        assert stats.num_pixels == 50 * 40
        region = packet.data[100:140, 100:150]
        assert stats.mean == pytest.approx(float(np.mean(region)), rel=1e-3)
        assert stats.minimum == pytest.approx(float(np.min(region)), rel=1e-3)
        assert stats.maximum == pytest.approx(float(np.max(region)), rel=1e-3)
        assert stats.min_position is not None
    finally:
        source.close()


def test_ellipse_line_and_cursor_rois() -> None:
    source = _open_seq_source()
    try:
        shapes = (
            RoiShape(id=1, kind="ellipse", points=RECT, name="Ellipse 1"),
            RoiShape(id=2, kind="line", points=((10.0, 10.0), (200.0, 300.0)), name="Line 1"),
            RoiShape(id=3, kind="cursor", points=((320.0, 240.0),), name="Spot 1"),
        )
        source.set_rois(shapes)
        packet = source.read_frame(10)
        by_kind = {stats.kind: stats for stats in packet.roi_stats}
        assert set(by_kind) == {"ellipse", "line", "cursor"}

        ellipse = by_kind["ellipse"]
        assert 0 < ellipse.num_pixels < 50 * 40  # inside the bounding box

        assert by_kind["line"].num_pixels > 0

        cursor = by_kind["cursor"]
        assert cursor.value == pytest.approx(float(packet.data[240, 320]), rel=1e-3)
    finally:
        source.close()


def test_roi_update_and_removal() -> None:
    source = _open_seq_source()
    try:
        source.set_rois((RoiShape(id=1, kind="rect", points=RECT, name="Box 1"),))
        first = source.read_frame(10).roi_stats[0]

        moved = ((200.0, 200.0), (250.0, 240.0))
        source.set_rois((RoiShape(id=1, kind="rect", points=moved, name="Box 1"),))
        packet = source.read_frame(10)
        assert len(packet.roi_stats) == 1
        updated = packet.roi_stats[0]
        region = packet.data[200:240, 200:250]
        assert updated.mean == pytest.approx(float(np.mean(region)), rel=1e-3)
        assert updated.num_pixels == first.num_pixels

        source.set_rois(())
        assert source.read_frame(10).roi_stats == ()
    finally:
        source.close()


def test_roi_stats_follow_current_unit() -> None:
    source = _open_seq_source()
    try:
        source.set_rois((RoiShape(id=1, kind="rect", points=RECT, name="Box 1"),))
        source.set_unit("temperature_factory_c")
        celsius = source.read_frame(10).roi_stats[0]
        source.set_unit("temperature_factory_f")
        fahrenheit = source.read_frame(10).roi_stats[0]
        assert fahrenheit.mean == pytest.approx(celsius.mean * 9.0 / 5.0 + 32.0, rel=1e-3)
    finally:
        source.close()


def _mouse(type_, pos, button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton):
    return QMouseEvent(type_, QPointF(pos), button, buttons, Qt.KeyboardModifier.NoModifier)


def test_canvas_draw_select_move_and_delete_roi(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(ROOT / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        canvas = window.canvas
        canvas.repaint()  # offscreen: force paintEvent so the image rect is computed
        assert canvas._image_rect.width() > 0

        # draw a box with the mouse
        window.analysis_toolbar._buttons["rect"].click()
        start = QPointF(canvas._image_rect.center())
        canvas.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, start))
        canvas.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, start + QPointF(120, 80)))
        canvas.mouseReleaseEvent(_mouse(QMouseEvent.Type.MouseButtonRelease, start + QPointF(120, 80), buttons=Qt.MouseButton.NoButton))
        assert len(window._rois) == 1
        assert window._rois[0].kind == "rect"
        assert window._rois[0].name == "Box 1"
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 1)

        # pin a spot with the cursor tool
        window.analysis_toolbar._buttons["cursor"].click()
        canvas.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, start))
        canvas.mouseReleaseEvent(_mouse(QMouseEvent.Type.MouseButtonRelease, start, buttons=Qt.MouseButton.NoButton))
        assert [shape.kind for shape in window._rois] == ["rect", "cursor"]
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 2)

        # select the box and move it by dragging its body
        window.analysis_toolbar._buttons["select"].click()
        shape = window._rois[0]
        center_image = (
            (shape.points[0][0] + shape.points[1][0]) / 2,
            (shape.points[0][1] + shape.points[1][1]) / 2,
        )
        grab = canvas._image_to_widget(*center_image)
        canvas.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, grab))
        assert window._selected_roi_id == shape.id
        canvas.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, grab + QPointF(30, 20)))
        canvas.mouseReleaseEvent(_mouse(QMouseEvent.Type.MouseButtonRelease, grab + QPointF(30, 20), buttons=Qt.MouseButton.NoButton))
        moved = window._rois[0]
        assert moved.points != shape.points

        # delete the selected box with the Delete key
        delete = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier)
        canvas.keyPressEvent(delete)
        assert [s.kind for s in window._rois] == ["cursor"]
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 1)
    finally:
        window.close()
        qapp.processEvents()
