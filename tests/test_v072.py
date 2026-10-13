# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""0.7.2: turning the view by quarter turns (the picture and image/movie exports turn; ROIs, zones and
values stay in the recording's pixels), zone ends named as shown, and box names that reuse the lowest
free number (from the engineers' feedback of 2026-10-12)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog

from flir_player.compose import ExportOptions, compose_frame
from flir_player.geometry import roi_coordinates
from flir_player.models import RoiShape
from flir_player.orientation import Orientation
from flir_player.render import DisplayState, render_frame_rgb
from flir_player.widgets import ThermalCanvas
from flir_player.zone_dialog import ZoneSplitDialog
from flir_player.zones import ENDS, ZoneSpec

SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"
W, H = 20, 10
ALL = [Orientation(t, fh, fv) for t in range(4) for fh in (False, True) for fv in (False, True)]


def _name(o: Orientation) -> str:
    return f"t{o.turns}{'h' if o.flip_h else ''}{'v' if o.flip_v else ''}"


# --- the orientation maths ------------------------------------------------------------------------


@pytest.mark.parametrize("o", ALL, ids=_name)
def test_orientation_maps_agree_with_the_turned_picture(o):
    image = np.arange(W * H).reshape(H, W)
    shown = o.apply(image)
    assert shown.shape == o.display_size(W, H)[::-1] and shown.flags.c_contiguous
    for row in range(shown.shape[0]):
        for column in range(shown.shape[1]):
            x, y = o.pixel_to_image(column, row, W, H)
            assert shown[row, column] == image[y, x]
    for x, y in [(0.0, 0.0), (3.25, 7.5), (W, H), (W, 0.0)]:
        assert o.to_image(*o.to_display(x, y, W, H), W, H) == pytest.approx((x, y))
    u, v = o.to_display(4.5, 2.5, W, H)  # a pixel centre lands in the shown pixel holding it
    assert shown[int(v), int(u)] == image[2, 4]


@pytest.mark.parametrize("o", ALL, ids=_name)
def test_a_turn_turns_the_picture_as_shown(o):
    image = np.arange(W * H).reshape(H, W)
    assert np.array_equal(o.turned(1).apply(image), np.rot90(o.apply(image), k=-1))
    assert np.array_equal(o.turned(-1).apply(image), np.rot90(o.apply(image), k=1))
    assert o.turned(1).turned(-1) == o and o.turned(2).turned(2) == o


def test_zone_ends_are_named_as_shown():
    # a clockwise turn shows the recording's top at the right (a camera on its side, up = image left)
    assert Orientation(1).recording_end("right") == "top"
    assert Orientation(1).shown_end("left") == "top"
    assert Orientation(3).recording_end("right") == "bottom"
    assert Orientation(0, flip_h=True).recording_end("right") == "left"
    for o in ALL:
        assert sorted(o.shown_end(end) for end in ENDS) == sorted(ENDS)
        assert all(o.recording_end(o.shown_end(end)) == end for end in ENDS)


# --- rendering and exports ------------------------------------------------------------------------


def test_render_turns_and_flips_after_the_overlays():
    data = np.linspace(0.0, 1.0, W * H).reshape(H, W)
    plain, *_ = render_frame_rgb(data, DisplayState())
    for o in ALL:
        turned, *_ = render_frame_rgb(data, DisplayState(flip_h=o.flip_h, flip_v=o.flip_v, rotation=o.turns))
        assert np.array_equal(turned, o.apply(plain)) and turned.flags.c_contiguous


@pytest.mark.parametrize("o", ALL, ids=_name)
def test_export_overlays_mark_the_same_pixel_when_turned(o):
    rgb = o.apply(np.zeros((48, 64, 3), np.uint8))
    spot = RoiShape(1, "cursor", ((10.5, 20.5),), "Spot")  # recording pixel (10, 20)
    u, v = o.to_display(10.5, 20.5, 64, 48)
    composed = compose_frame(rgb, ExportOptions(color_bar=False, rois=True, roi_names=False),
                             rois=(spot,), flips=(o.flip_h, o.flip_v), turns=o.turns)
    assert tuple(composed[int(v), int(u)]) == (76, 194, 255)  # ROI_COLORS[1]
    marked = compose_frame(rgb, ExportOptions(color_bar=False, rois=False, markers=True),
                           max_position=(10, 20), flips=(o.flip_h, o.flip_v), turns=o.turns)
    assert tuple(marked[int(v), int(u)]) == (255, 122, 144)
    # a box outline passes through its turned corner pixel
    box = RoiShape(2, "rect", ((5.0, 6.0), (30.0, 40.0)), "Box")
    outlined = compose_frame(rgb, ExportOptions(color_bar=False, rois=True, roi_names=False),
                             rois=(box,), flips=(o.flip_h, o.flip_v), turns=o.turns)
    cu, cv = o.to_display(5.5, 6.5, 64, 48)
    assert outlined[int(cv), int(cu)].any()


# --- the canvas -----------------------------------------------------------------------------------


def _encoded_canvas(qapp, o: Orientation, scale: float):
    """Canvas whose drawn colours encode the recording pixel shown (R = 10·x, G = 20·y)."""
    raw = np.arange(W * H, dtype=float).reshape(H, W)
    rgb = np.zeros((H, W, 3), np.uint8)
    rgb[..., 0] = np.arange(W)[None, :] * 10
    rgb[..., 1] = np.arange(H)[:, None] * 20
    shown_width, shown_height = o.display_size(W, H)
    canvas = ThermalCanvas()
    canvas.setMinimumSize(1, 1)
    canvas.resize(int(shown_width * scale), int(shown_height * scale))  # no letterbox
    canvas.show()
    canvas.set_orientation(o)
    canvas.set_frame(o.apply(rgb), raw, "counts")
    image = canvas.grab().toImage()
    drawn = {}
    for y in range(image.height()):
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            drawn[(x, y)] = (round(colour.red() / 10), round(colour.green() / 20))
    return canvas, raw, drawn


@pytest.mark.parametrize("scale", [10.0, 7.5])
@pytest.mark.parametrize("o", [o for o in ALL if o.turns], ids=_name)
def test_turned_canvas_probes_spots_and_boxes_measure_the_drawn_pixels(qapp, o, scale):
    canvas, raw, drawn = _encoded_canvas(qapp, o, scale)
    try:
        for (x, y), pixel in drawn.items():
            assert canvas._pixel_at(QPointF(x, y)) == pixel, (x, y)
        cells = {}
        for (x, y), pixel in drawn.items():
            cells.setdefault(pixel, []).append((x + 0.5, y + 0.5))
        centre = {pixel: tuple(np.mean(points, axis=0)) for pixel, points in cells.items()}
        px, py, value = canvas._probe_at(QPointF(37, 22))
        assert (px, py) == drawn[(37, 22)] and value == raw[py, px]
        marker = canvas._image_to_widget(px + 0.5, py + 0.5)
        assert abs(marker.x() - centre[(px, py)][0]) < 1 and abs(marker.y() - centre[(px, py)][1]) < 1
        emitted = []
        canvas.roi_drawn.connect(lambda kind, points: emitted.append((kind, points)))
        canvas.set_roi_tool("cursor")
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(37, 22))
        ys, xs = roi_coordinates(RoiShape(1, *emitted[-1], "Spot"), H, W)
        assert (int(xs[0]), int(ys[0])) == drawn[(37, 22)]
        canvas.set_roi_tool("rect")
        start, end = QPoint(13, 22), QPoint(47, 68)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        canvas.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(end), QPointF(end),
                                          Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                          Qt.KeyboardModifier.NoModifier))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
        ys, xs = roi_coordinates(RoiShape(2, *emitted[-1], "Box"), H, W)
        inside = {pixel for pixel, (cx, cy) in centre.items() if 13 < cx < 47 and 22 < cy < 68}
        assert set(zip(xs.tolist(), ys.tolist())) == inside
    finally:
        canvas.close()


@pytest.mark.parametrize("o", ALL, ids=_name)
@pytest.mark.parametrize("handle", ["nw", "ne", "sw", "se"])
def test_resizing_keeps_the_corner_opposite_on_screen(qapp, o, handle):
    canvas = ThermalCanvas()
    canvas.set_orientation(o)
    shape = RoiShape(1, "rect", ((50.0, 50.0), (100.0, 100.0)), "Box")
    canvas._drag = dict(orig=shape, handle=handle)
    anchor = canvas._dragged_shape((45.0, 45.0)).points[0]
    corners = [o.to_display(x, y, 640, 480) for x in (50.0, 100.0) for y in (50.0, 100.0)]
    u, v = o.to_display(*anchor, 640, 480)
    # the anchor sits at the screen corner diagonally opposite the dragged handle
    assert u == (max if "w" in handle else min)(c[0] for c in corners)
    assert v == (max if "n" in handle else min)(c[1] for c in corners)


def test_quarter_turn_refits_the_view(qapp):
    canvas = ThermalCanvas()
    canvas.set_frame(np.zeros((H, W, 3), np.uint8), np.zeros((H, W)), "counts")
    canvas.set_zoom_level(2.0)
    canvas.set_orientation(Orientation(2))
    assert canvas._zoom == 2.0  # a half turn keeps the shape
    canvas.set_orientation(Orientation(3))
    assert canvas._zoom is None


# --- the player -----------------------------------------------------------------------------------


@pytest.fixture
def window(qapp):
    from conftest import wait_until
    from flir_player.main_window import MainWindow

    window = MainWindow()
    window.show()
    window.open_path(SAMPLES / "2.seq")
    assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy, timeout=20)
    yield window
    window.close()
    qapp.processEvents()


def test_rotate_buttons_turn_the_view_only(qapp, window):
    plain = window.canvas.image.copy()
    stats_before = window.current_packet.data.copy()
    window.inspector.rotate_right_button.click()
    assert window.rotation == 1 and window.canvas._orientation == Orientation(1)
    turned = window.canvas.image
    assert (turned.width(), turned.height()) == (plain.height(), plain.width())
    for x, y in [(3, 5), (100, 40), (plain.width() - 1, plain.height() - 1)]:
        u, v = Orientation(1).to_display(x + 0.5, y + 0.5, plain.width(), plain.height())
        assert turned.pixelColor(int(u), int(v)) == plain.pixelColor(x, y)
    assert np.array_equal(window.current_packet.data, stats_before)  # values never turn
    # a turn turns what is shown: a left-right mirror becomes a top-bottom mirror
    window.inspector.flip_h_button.click()
    assert window.flip_h and window.rotation == 1
    window.inspector.rotate_left_button.click()
    assert (window.rotation, window.flip_h, window.flip_v) == (0, False, True)
    assert window.inspector.flip_v_button.isChecked() and not window.inspector.flip_h_button.isChecked()
    assert window._display_state(window.current_packet).orientation == Orientation(0, False, True)
    window.inspector.flip_v_button.click()
    for _ in range(4):
        window.inspector.rotate_left_button.click()
    assert window.orientation.identity


def test_zone_one_follows_the_end_shown_on_screen(qapp, window, monkeypatch):
    window._rotate_view(1)  # the recording's top is shown at the right
    window._add_roi("rect", ((40.0, 200.0), (52.0, 380.0)))

    def accept(self):
        self.show()
        self.count_spin.setValue(6)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ZoneSplitDialog, "exec", accept)
    window._split_zones()  # default: zone 1 at the right end as shown
    group = window._zone_groups[-1]
    assert group["spec"].start == "top" and window._zone_defaults.start == "right"
    zones = [roi for roi in window._rois if roi.id in group["ids"]]
    assert zones[0].name == "Cell 1" and zones[0].points[0][1] == 200.0  # the top rows of the recording
    dialog = ZoneSplitDialog(group["spec"], 640, 480, orientation=window.orientation)
    assert dialog.start_combo.currentText() == "Right end" and dialog.spec().start == "top"
    dialog.close()


def test_box_names_reuse_the_lowest_free_number(qapp, window):
    for k in range(3):
        window._add_roi("rect", ((40.0 + 30 * k, 200.0), (60.0 + 30 * k, 212.0)))
    assert [roi.name for roi in window._rois] == ["Box 1", "Box 2", "Box 3"]
    window._delete_roi(window._rois[1].id)
    window._add_roi("rect", ((200.0, 200.0), (220.0, 212.0)))
    window._add_roi("cursor", ((5.5, 5.5),))
    window._add_roi("rect", ((250.0, 200.0), (270.0, 212.0)))
    assert [roi.name for roi in window._rois] == ["Box 1", "Box 3", "Box 2", "Spot 1", "Box 4"]
    window._clear_rois()
    window._add_roi("rect", ((40.0, 200.0), (60.0, 212.0)))
    assert window._rois[0].name == "Box 1"
