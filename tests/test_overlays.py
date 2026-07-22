# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

from conftest import wait_until

from PySide6.QtGui import QColor

from flir_player.main_window import MainWindow
from flir_player.render import CLIP_COLOR


SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"


def test_scale_from_active_roi(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)

        window._add_roi("rect", ((100.0, 100.0), (150.0, 140.0)))
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 1)

        window._change_range_mode("roi")
        stats = window.current_packet.roi_stats[0]
        assert window.range_mode == "roi"
        assert window.inspector.roi_button.isChecked()
        assert window.color_scale._minimum == stats.minimum
        assert window.color_scale._maximum == stats.maximum

        # deselecting falls back to whole-frame dynamic scaling
        window._select_roi(None)
        assert window.color_scale._minimum == window.current_packet.minimum
        assert window.color_scale._maximum == window.current_packet.maximum
    finally:
        window.close()
        qapp.processEvents()


def test_clipping_overlay_marks_out_of_range_pixels(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window._change_unit("temperature_factory_c")
        assert wait_until(qapp, lambda: window.current_packet.unit.key == "temperature_factory_c")

        mask = window.current_packet.clip_mask
        assert mask is not None and int(mask.sum()) > 0
        ys, xs = mask.nonzero()
        y, x = int(ys[0]), int(xs[0])

        expected = QColor(*CLIP_COLOR)
        assert window.canvas.image.pixelColor(x, y) == expected

        window.inspector.clipping_check.setChecked(False)
        assert window.canvas.image.pixelColor(x, y) != expected
    finally:
        window.close()
        qapp.processEvents()


def test_minmax_markers_toggle(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        assert not window.canvas._show_markers

        window.inspector.markers_check.setChecked(True)
        assert window.canvas._show_markers
        assert window.current_packet.min_position is not None
        assert window.current_packet.max_position is not None
        canvas = window.canvas
        canvas.repaint()  # smoke: marker painting must not raise

        window.inspector.markers_check.setChecked(False)
        assert not window.canvas._show_markers
    finally:
        window.close()
        qapp.processEvents()
