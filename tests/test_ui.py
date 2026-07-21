# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

from conftest import wait_until

from PySide6.QtWidgets import QFileDialog

from flir_player.main_window import MainWindow


ROOT = Path(__file__).resolve().parents[1]


def test_main_window_opens_plays_seeks_switches_units_and_exports(qapp, tmp_path, monkeypatch) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(ROOT / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        assert window.metadata is not None
        assert window.current_packet.index == 0
        assert window.transport.slider.maximum() == window.metadata.num_frames - 1

        window.toggle_playback()
        assert wait_until(qapp, lambda: window.current_packet.index >= 4, timeout=2.0)
        window.pause_playback(invalidate=True)

        window.seek_to(100)
        assert wait_until(qapp, lambda: window.current_packet.index == 100)

        window._change_palette("Viridis")
        window._change_range_mode("fixed")
        window._change_unit("temperature_factory_c")
        assert wait_until(qapp, lambda: window.current_packet.unit.key == "temperature_factory_c")
        assert window.current_palette == "Viridis"
        assert window.range_mode == "fixed"
        assert not window.canvas.image.isNull()

        export_path = tmp_path / "frame.png"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            lambda *args, **kwargs: (str(export_path), "PNG image (*.png)"),
        )
        window._export("png")
        assert export_path.is_file() and export_path.stat().st_size > 0

        window.resize(1080, 720)
        qapp.processEvents()
        # all inspector sections stay visible at any height (scroll area handles overflow)
        assert window.inspector.range_panel.isVisible()
        assert window.inspector.info_panel.isVisible()
        assert window.inspector.params_panel.isVisible()

        window.toggle_focus_mode()
        qapp.processEvents()
        assert window._focus_mode and not window.title_bar.isVisible()
        window.toggle_focus_mode()
        qapp.processEvents()
        assert not window._focus_mode and window.title_bar.isVisible()
    finally:
        window.close()
        qapp.processEvents()


def test_object_parameters_panel_edits_apply(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(ROOT / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        assert window.inspector.params_panel.isEnabled()

        window._change_unit("temperature_factory_c")
        assert wait_until(qapp, lambda: window.current_packet.unit.key == "temperature_factory_c" and not window._busy)
        baseline = window.current_packet.mean

        spin = window.inspector.params_panel._spins["emissivity"]
        spin.setValue(0.5)
        spin.editingFinished.emit()
        assert wait_until(qapp, lambda: not window._busy and window.current_packet.mean != baseline)

        window.inspector.params_panel.reset_button.click()
        assert wait_until(
            qapp,
            lambda: not window._busy and abs(window.current_packet.mean - baseline) < 1e-6,
        )
    finally:
        window.close()
        qapp.processEvents()
