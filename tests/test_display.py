# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from conftest import wait_until

from flir_player.main_window import MainWindow
from flir_player.render import (
    ISO_ABOVE_COLOR,
    ISO_BELOW_COLOR,
    ISO_INTERVAL_COLOR,
    SEG_ABOVE_COLOR,
    SEG_BELOW_COLOR,
    apply_isotherm_overlay,
    apply_segmentation_overlay,
    isotherm_mask,
    load_custom_palettes,
    lut_from_stops,
    palette_lut,
    palette_names,
    plateau_equalized_indices,
    register_custom_palette,
    render_rgb,
    segmentation_overlays,
    unregister_custom_palette,
)

ROOT = Path(__file__).resolve().parents[1]


# --- plateau equalization (§4.3.2) ----------------------------------------------


def test_plateau_equalization_identity_at_zero() -> None:
    normalized = np.linspace(0.0, 1.0, 64).reshape(8, 8)
    indices = plateau_equalized_indices(normalized, 0.0)
    np.testing.assert_array_equal(
        indices, np.clip(normalized * 255.0, 0, 255).astype(np.uint8)
    )


def test_plateau_equalization_expands_dominant_bin() -> None:
    # 90 % of pixels share one value; equalization should spread the rest apart.
    normalized = np.full((16, 16), 0.5)
    normalized[0, 0] = 0.0
    normalized[0, 1] = 1.0
    indices = plateau_equalized_indices(normalized, 1.0)
    assert indices[0, 0] < indices[0, 1]
    assert indices[0, 1] == 255
    assert indices[-1, -1] != indices[0, 0]


def test_render_rgb_pe_path_matches_shape_and_dtype() -> None:
    data = np.random.default_rng(4).normal(20.0, 5.0, size=(32, 24))
    rgb = render_rgb(data, "Iron", 10.0, 30.0, pe=0.8)
    assert rgb.shape == (32, 24, 3)
    assert rgb.dtype == np.uint8
    assert rgb.flags.c_contiguous


# --- segmentation (§4.8.5) ---------------------------------------------------------


def test_segmentation_masks_split_out_of_range_pixels() -> None:
    data = np.array([[1.0, 5.0, 9.0], [np.nan, 7.0, 3.0]])
    below, above = segmentation_overlays(data, 3.0, 7.0)
    assert below.tolist() == [[True, False, False], [False, False, False]]
    assert above.tolist() == [[False, False, True], [False, False, False]]


def test_segmentation_overlay_paints_fixed_colors() -> None:
    data = np.array([[0.0, 5.0, 10.0]])
    rgb = render_rgb(data, "Grayscale", 0.0, 10.0)
    below, above = segmentation_overlays(data, 2.0, 8.0)
    painted = apply_segmentation_overlay(rgb, below, above)
    assert tuple(painted[0, 0]) == SEG_BELOW_COLOR
    assert tuple(painted[0, 2]) == SEG_ABOVE_COLOR
    assert tuple(painted[0, 1]) == tuple(rgb[0, 1])


# --- isotherms (§4.4.1) -------------------------------------------------------------


def test_isotherm_masks_per_mode() -> None:
    data = np.array([[1.0, 5.0, 9.0], [np.nan, 3.0, 7.0]])
    assert isotherm_mask(data, "off", 5.0, 7.0) is None
    assert isotherm_mask(data, "above", 5.0, 0.0).tolist() == [
        [False, True, True],
        [False, False, True],
    ]
    assert isotherm_mask(data, "below", 5.0, 0.0).tolist() == [
        [True, True, False],
        [False, True, False],
    ]
    # interval tolerates swapped limits
    assert isotherm_mask(data, "interval", 7.0, 3.0).tolist() == [
        [False, True, False],
        [False, True, True],
    ]


def test_isotherm_overlay_uses_mode_color() -> None:
    data = np.arange(16, dtype=float).reshape(4, 4)
    rgb = render_rgb(data, "Grayscale", 0.0, 15.0)
    for mode, color in (
        ("above", ISO_ABOVE_COLOR),
        ("below", ISO_BELOW_COLOR),
        ("interval", ISO_INTERVAL_COLOR),
    ):
        painted = apply_isotherm_overlay(rgb, data, mode, 4.0, 7.0)
        mask = isotherm_mask(data, mode, 4.0, 7.0)
        assert tuple(painted[mask][0]) == color
        assert np.array_equal(painted[~mask], rgb[~mask])


# --- palettes (§4.9.4.2–3) ------------------------------------------------------------


def test_palette_lut_invert_reverses() -> None:
    forward = palette_lut("Iron")
    inverted = palette_lut("Iron", invert=True)
    np.testing.assert_array_equal(inverted, forward[::-1])


def test_new_builtin_palettes_resolve() -> None:
    for name in ("Magma", "Plasma", "Cividis", "Hot", "Copper", "Bone"):
        lut = palette_lut(name)
        assert lut.shape == (256, 3)
        assert name in palette_names()


def test_lut_from_stops_endpoints_and_midpoint() -> None:
    lut = lut_from_stops([(0.0, (0, 0, 0)), (1.0, (255, 255, 255))])
    assert tuple(lut[0]) == (0, 0, 0)
    assert tuple(lut[255]) == (255, 255, 255)
    assert abs(int(lut[128][0]) - 128) <= 1


def test_lut_from_stops_requires_two_stops() -> None:
    with pytest.raises(ValueError):
        lut_from_stops([(0.5, (10, 20, 30))])


def test_custom_palette_register_roundtrip(tmp_path) -> None:
    from PySide6.QtCore import QSettings

    QSettings("Local", "FLIR Thermal Player").remove("palettes/custom")
    stops = [(0.0, (255, 0, 0)), (1.0, (0, 0, 255))]
    try:
        register_custom_palette("Unit Test Ramp", stops)
        assert "Unit Test Ramp" in palette_names()
        lut = palette_lut("Unit Test Ramp")
        assert tuple(lut[0]) == (255, 0, 0)
        assert tuple(lut[255]) == (0, 0, 255)

        # persistence: wipe in-memory state and reload from QSettings
        from flir_player import render

        render.CUSTOM_PALETTES.clear()
        render.CUSTOM_PALETTE_STOPS.clear()
        load_custom_palettes()
        assert tuple(palette_lut("Unit Test Ramp")[0]) == (255, 0, 0)
    finally:
        unregister_custom_palette("Unit Test Ramp")
        QSettings("Local", "FLIR Thermal Player").remove("palettes/custom")
    assert "Unit Test Ramp" not in palette_names()


# --- flip coordinate mapping (§4.6.3) ----------------------------------------------------


def test_canvas_flip_mapping_roundtrip(qapp) -> None:
    from flir_player.widgets import ThermalCanvas

    canvas = ThermalCanvas()
    canvas.show()
    rgb = np.zeros((10, 20, 3), dtype=np.uint8)
    raw = np.arange(200, dtype=float).reshape(10, 20)
    canvas.set_frame(np.ascontiguousarray(rgb), raw, "counts")
    canvas._image_rect = canvas.rect()  # direct mapping without letterboxing
    width = canvas.rect().width()
    height = canvas.rect().height()
    try:
        canvas.set_flips(True, False)
        # raw x=0 must appear at the right edge of the widget
        assert canvas._image_to_widget(0.0, 5.0).x() == pytest.approx(width * 19 / 20)
        assert canvas._widget_to_image(canvas._image_to_widget(3.0, 4.0)) == pytest.approx(
            (3.0, 4.0)
        )
        canvas.set_flips(False, True)
        assert canvas._image_to_widget(5.0, 0.0).y() == pytest.approx(height * 9 / 10)
        canvas.set_flips(True, True)
        point = canvas._widget_to_image(canvas._image_to_widget(7.0, 8.0))
        assert point == pytest.approx((7.0, 8.0))
    finally:
        canvas.close()


# --- MainWindow integration --------------------------------------------------------------


def test_flip_state_roundtrips_into_render(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(ROOT / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        window._change_flips(True, False)
        assert window.flip_h and not window.flip_v
        assert window.canvas._flip_h
        image = window.canvas.image
        width = image.width()
        # flipped render must be a mirror of an unflipped render of the same frame
        window._change_flips(False, False)
        plain = window.canvas.image
        window._change_flips(True, False)
        flipped = window.canvas.image
        x = width // 3
        y = image.height() // 2
        assert flipped.pixelColor(width - 1 - x, y) == plain.pixelColor(x, y)
    finally:
        window.close()
        qapp.processEvents()


def test_isotherm_seeds_limits_and_paints(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(ROOT / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        window.inspector.isotherm_combo.setCurrentIndex(1)  # Above
        qapp.processEvents()
        assert window.isotherm_mode == "above"
        low, high = window._current_scale(window.current_packet)
        assert window.iso_limit1 == pytest.approx((low + high) / 2.0)
        # color bar reflects the active isotherm
        assert window.color_scale._iso_mode == "above"
        # dragging on the color bar updates the value and the inspector spins
        window._isotherm_dragged("l1", low + 1.0)
        assert window.iso_limit1 == pytest.approx(low + 1.0)
        assert window.inspector.iso_limit1_spin.value() == pytest.approx(low + 1.0)
    finally:
        window.close()
        qapp.processEvents()


def test_segmentation_enable_seeds_frame_range(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(ROOT / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        window.inspector.segmentation_check.setChecked(True)
        qapp.processEvents()
        assert window.segmentation_on
        assert window.seg_min == pytest.approx(window.current_packet.minimum)
        assert window.seg_max == pytest.approx(window.current_packet.maximum)
        # restricting the valid range drives the dynamic scale
        packet = window.current_packet
        window._change_segmentation(True, packet.minimum, (packet.minimum + packet.maximum) / 2)
        low, high = window._current_scale(packet)
        assert high < packet.maximum
    finally:
        window.close()
        qapp.processEvents()


def test_enhancement_toggle_updates_state(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(ROOT / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        window.inspector.enhancement_combo.setCurrentIndex(1)
        qapp.processEvents()
        assert window.enhancement == "pe"
        assert window.inspector.pe_row.isVisibleTo(window.inspector)
        window._change_enhancement("pe", 0.9)
        image_pe = window.canvas.image
        window._change_enhancement("linear", 0.0)
        assert window.enhancement == "linear"
        # PE render differs from the linear render of the same frame
        assert image_pe != window.canvas.image
    finally:
        window.close()
        qapp.processEvents()
