# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from conftest import wait_until

from flir_player.main_window import MainWindow
from flir_player.models import RoiShape
from flir_player.processing import (
    ProcessingState,
    TemporalBuffer,
    apply_file_operation,
    apply_point_filter,
    apply_spatial_filter,
    box_mean,
    median_filter,
    roi_stats_app,
    state_from_dict,
)

ROOT = Path(__file__).resolve().parents[1]


# --- state ------------------------------------------------------------------


def test_processing_state_activity_flags() -> None:
    assert not ProcessingState().is_active
    assert ProcessingState(file_op="subtract").is_active
    assert ProcessingState(point=("gain", 2.0)).is_active
    assert ProcessingState(spatial=("median", 3)).is_active
    assert ProcessingState(temporal=("average", 5)).is_active


def test_state_from_dict_roundtrip() -> None:
    state = state_from_dict(
        {"point": ("offset", 2.5), "spatial": ("gaussian", 5), "temporal": ("min", 7)}
    )
    assert state.point == ("offset", 2.5)
    assert state.spatial == ("gaussian", 5)
    assert state.temporal == ("min", 7)
    assert state.file_op is None  # file_op is owned by the reference loader


# --- file operation (§4.9.5.3) --------------------------------------------------


def test_file_operation_math() -> None:
    data = np.array([[4.0, 8.0], [16.0, 32.0]])
    ref = np.array([[2.0, 4.0], [8.0, 0.0]])
    np.testing.assert_array_equal(
        apply_file_operation(data, ref, "subtract"), [[2.0, 4.0], [8.0, 32.0]]
    )
    np.testing.assert_array_equal(
        apply_file_operation(data, ref, "add"), [[6.0, 12.0], [24.0, 32.0]]
    )
    np.testing.assert_array_equal(
        apply_file_operation(data, ref, "multiply"), [[8.0, 32.0], [128.0, 0.0]]
    )
    divided = apply_file_operation(data, ref, "divide")
    assert divided[0, 0] == 2.0 and divided[0, 1] == 2.0 and divided[1, 0] == 2.0
    assert np.isnan(divided[1, 1])  # division by zero → NaN, not inf
    assert apply_file_operation(data, None, "subtract") is data


# --- point filters (§4.8.6) ------------------------------------------------------


def test_point_filters() -> None:
    data = np.array([[1.0, 4.0], [-1.0, 0.0]])
    np.testing.assert_array_equal(
        apply_point_filter(data, "gain", 2.0), [[2.0, 8.0], [-2.0, 0.0]]
    )
    np.testing.assert_array_equal(
        apply_point_filter(data, "offset", 1.5), [[2.5, 5.5], [0.5, 1.5]]
    )
    assert apply_point_filter(data, "exp", 0.0)[0, 0] == pytest.approx(np.e)
    ln = apply_point_filter(data, "ln", 0.0)
    assert ln[0, 1] == pytest.approx(np.log(4.0))
    assert np.isnan(ln[1, 0]) and np.isnan(ln[1, 1])  # guarded non-positive input
    sqrt = apply_point_filter(data, "sqrt", 0.0)
    assert sqrt[0, 1] == pytest.approx(2.0)
    assert np.isnan(sqrt[1, 0])
    assert apply_point_filter(data, "none", 0.0) is data


# --- spatial filters (§4.8.6) ------------------------------------------------------


def test_box_mean_preserves_constant_and_is_odd_sized() -> None:
    data = np.full((9, 9), 3.0)
    np.testing.assert_allclose(box_mean(data, 3), 3.0)
    # interior mean of a known patch
    ramp = np.arange(25, dtype=float).reshape(5, 5)
    blurred = box_mean(ramp, 3)
    assert blurred[2, 2] == pytest.approx(np.mean(ramp[1:4, 1:4]))


def test_box_mean_ignores_nan_neighbors() -> None:
    data = np.ones((5, 5))
    data[2, 2] = np.nan
    blurred = box_mean(data, 3)
    assert blurred[0, 0] == pytest.approx(1.0)
    assert np.isfinite(blurred[2, 2])  # hole filled by the neighborhood mean


def test_gaussian_smooths_impulse() -> None:
    data = np.zeros((9, 9))
    data[4, 4] = 27.0
    smoothed = apply_spatial_filter(data, "gaussian", 3)
    assert smoothed[4, 4] < 27.0
    assert smoothed.sum() == pytest.approx(27.0, rel=0.05)


def test_median_removes_salt_noise() -> None:
    data = np.zeros((7, 7))
    data[3, 3] = 100.0
    filtered = median_filter(data, 3)
    assert filtered[3, 3] == 0.0
    assert apply_spatial_filter(data, "none", 3) is data


# --- temporal filters (§4.8.6) ------------------------------------------------------


def test_temporal_buffer_statistics_and_subtract() -> None:
    buffer = TemporalBuffer()
    f1 = np.full((2, 2), 1.0)
    f2 = np.full((2, 2), 3.0)
    f3 = np.full((2, 2), 5.0)
    buffer.apply(f1, "average", 3)
    np.testing.assert_allclose(buffer.apply(f2, "average", 3), 2.0)
    np.testing.assert_allclose(buffer.apply(f3, "average", 3), 3.0)
    # depth respected: window keeps only the last two frames (f3, f3)
    np.testing.assert_allclose(buffer.apply(f3, "average", 2), 5.0)

    buffer.reset()
    assert len(buffer) == 0
    buffer.apply(f1, "min", 5)
    np.testing.assert_allclose(buffer.apply(f2, "min", 5), 1.0)
    np.testing.assert_allclose(buffer.apply(f3, "max", 5), 5.0)

    buffer.reset()
    buffer.apply(f1, "subtract", 3)
    np.testing.assert_allclose(buffer.apply(f3, "subtract", 3), f3 - f1)


# --- app-side ROI stats --------------------------------------------------------------


def _shape(kind: str, points) -> RoiShape:
    return RoiShape(id=1, kind=kind, points=tuple(points), name="Test")


def test_roi_stats_app_rect_matches_numpy() -> None:
    data = np.arange(100, dtype=float).reshape(10, 10)
    (stats,) = roi_stats_app(data, (_shape("rect", ((2.0, 2.0), (5.0, 6.0))),))
    region = data[2:6, 2:5].ravel()
    assert stats.minimum == region.min()
    assert stats.maximum == region.max()
    assert stats.mean == pytest.approx(region.mean())
    assert stats.std_dev == pytest.approx(region.std())
    assert stats.num_pixels == region.size
    assert stats.min_position == (2, 2)
    assert stats.max_position == (4, 5)


def test_roi_stats_app_ellipse_inside_bounding_box() -> None:
    data = np.ones((20, 20))
    (stats,) = roi_stats_app(data, (_shape("ellipse", ((2.0, 2.0), (18.0, 18.0))),))
    assert 0 < stats.num_pixels < 16 * 16
    assert stats.mean == pytest.approx(1.0)


def test_roi_stats_app_line_and_cursor() -> None:
    data = np.arange(100, dtype=float).reshape(10, 10)
    (line,) = roi_stats_app(data, (_shape("line", ((0.0, 0.0), (9.0, 0.0))),))
    assert line.num_pixels == 10
    assert line.minimum == 0.0 and line.maximum == 9.0

    (cursor,) = roi_stats_app(data, (_shape("cursor", [(3.0, 4.0)]),))
    assert cursor.num_pixels == 1
    assert cursor.value == pytest.approx(data[4, 3])
    assert cursor.min_position == (3, 4)


def test_roi_stats_app_handles_nan() -> None:
    data = np.full((5, 5), 2.0)
    data[0, 0] = np.nan
    (stats,) = roi_stats_app(data, (_shape("rect", ((0.0, 0.0), (4.0, 4.0))),))
    assert stats.num_pixels == 15  # 4×4 region (exclusive end), one NaN dropped
    assert stats.mean == pytest.approx(2.0)


# --- end-to-end through the decoder -------------------------------------------------


def test_reference_subtract_end_to_end(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(ROOT / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        window._reference_params = {
            "path": str(ROOT / "2.seq"),
            "frame_index": 0,
            "op": "subtract",
        }
        window.seek_to(0)
        assert wait_until(qapp, lambda: window.current_packet.index == 0)
        request_id = window._activate_request()
        window.decoder.request_reference(window._reference_params, 0, request_id)
        assert wait_until(
            qapp,
            lambda: window.inspector.reference_label.text().startswith("2.seq"),
        )
        assert wait_until(
            qapp, lambda: not window._busy and window.current_packet is not None
        )
        # frame 0 minus itself is ~0 everywhere
        assert window.current_packet.mean == pytest.approx(0.0, abs=1e-6)
        assert window.inspector.reference_label.text() == "2.seq · frame 1"

        # ROI stats switch to the app-side path while processing is active
        window._add_roi("rect", ((10.0, 10.0), (40.0, 30.0)))
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 1)
        assert window.current_packet.roi_stats[0].mean == pytest.approx(0.0, abs=1e-6)

        # a later frame minus the reference is nonzero again
        window.seek_to(50)
        assert wait_until(qapp, lambda: window.current_packet.index == 50)
        assert abs(window.current_packet.mean) > 1e-3

        # clearing restores the plain frame
        window._clear_reference()
        assert wait_until(
            qapp, lambda: window.inspector.reference_label.text() == "No reference"
        )
        assert window.current_packet is not None
        plain_mean = window.current_packet.mean
        assert abs(plain_mean) > 1e-3
    finally:
        window.close()
        qapp.processEvents()


def test_filters_end_to_end_offset(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(ROOT / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        baseline = window.current_packet.mean
        window._change_filters({"point": ("offset", 10.0)})
        assert wait_until(
            qapp,
            lambda: window.current_packet is not None
            and abs(window.current_packet.mean - (baseline + 10.0)) < 1e-3,
        )
        window._change_filters({"point": ("none", 1.0)})
        assert wait_until(
            qapp,
            lambda: abs(window.current_packet.mean - baseline) < 1e-3,
        )
    finally:
        window.close()
        qapp.processEvents()
