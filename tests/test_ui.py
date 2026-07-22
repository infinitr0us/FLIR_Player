# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

from conftest import wait_until

from PySide6.QtWidgets import QFileDialog, QPushButton, QSizeGrip

from flir_player.export_dialogs import BatchExtractDialog
from flir_player.main_window import MainWindow
from flir_player.style import APP_STYLESHEET, TOKENS
from flir_player.widgets import ElidingLabel, MetadataPickerDialog


SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"


def test_main_window_opens_plays_seeks_switches_units_and_exports(qapp, tmp_path, monkeypatch) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
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
        window.open_path(SAMPLES / "2.seq")
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


def test_stylesheet_icon_assets_exist() -> None:
    """The QSS references SVG glyphs that must ship with the package."""
    for key in ("CHECK_ICON", "CHEVRON_ICON", "CHEVRON_DISABLED_ICON"):
        assert Path(TOKENS[key]).is_file(), f"missing icon for {key}"


def test_button_variant_states_follow_variant_base_rules() -> None:
    """Variant :focus/:disabled rules must come after the variant base rules,
    or Qt's equal-specificity last-one-wins cascade makes a disabled accent or
    ghost button look enabled and hides its focus ring."""
    for variant in ('[accent="true"]', '[variant="ghost"]'):
        base = APP_STYLESHEET.index(f"QPushButton{variant} {{")
        for state in (":focus", ":disabled"):
            rule = APP_STYLESHEET.index(f"QPushButton{variant}{state} {{")
            assert rule > base, f"QPushButton{variant}{state} precedes its base rule"


def test_eliding_label_elides_long_text_with_tooltip(qapp) -> None:
    label = ElidingLabel()
    label.resize(80, 20)
    label.show()
    try:
        long_text = "FLIR A655sc · 63901234-and-a-very-long-serial"
        label.setText(long_text)
        qapp.processEvents()
        assert label.text() != long_text
        assert label.fontMetrics().horizontalAdvance(label.text()) <= label.width()
        assert label.toolTip() == long_text

        label.setText("A655")
        qapp.processEvents()
        assert label.text() == "A655"
        assert label.toolTip() == ""
    finally:
        label.close()
        qapp.processEvents()


def test_list_dialogs_have_size_grip(qapp) -> None:
    """Frameless dialogs with expanding content stay user-resizable."""
    picker = MetadataPickerDialog(["Time", "Trigger"], set())
    assert picker.findChild(QSizeGrip) is not None
    picker.close()
    batch = BatchExtractDialog()
    assert batch.findChild(QSizeGrip) is not None
    batch.close()
    qapp.processEvents()


def test_accent_button_disabled_state_renders_distinctly(qapp) -> None:
    """Rendered probe: a disabled accent button must not keep the enabled fill."""
    enabled = QPushButton("Export")
    enabled.setProperty("accent", True)
    disabled = QPushButton("Export")
    disabled.setProperty("accent", True)
    disabled.setEnabled(False)
    try:
        enabled.show()
        disabled.show()
        qapp.processEvents()

        def background(button: QPushButton) -> str:
            image = button.grab().toImage()
            # inside the border, left of the label text (padding is 14px)
            return image.pixelColor(10, image.height() // 2).name().upper()

        assert background(enabled) == TOKENS["ACCENT"].upper()
        assert background(disabled) == TOKENS["CONTROL"].upper()
    finally:
        enabled.close()
        disabled.close()
        qapp.processEvents()
