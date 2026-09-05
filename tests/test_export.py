# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from flir_player.settings import app_settings

import csv
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
import pytest

from conftest import wait_until

from PIL import Image

from flir_player.compose import ExportOptions, compose_frame
from flir_player.export import (
    MovieWriter,
    frame_burn_label,
    save_tiff16,
    save_tiff_float,
    stats_csv_header,
    stats_csv_row,
    write_stats_csv,
)
from flir_player.main_window import MainWindow
from flir_player.models import RoiShape
from flir_player.render import DisplayState, render_frame_rgb

SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"


def _gradient_rgb(width: int = 64, height: int = 48) -> np.ndarray:
    ramp = np.linspace(0, 255, width, dtype=np.uint8)
    return np.ascontiguousarray(np.repeat(ramp[None, :, None], height, axis=0).repeat(3, axis=2))


# --- compose_frame ---------------------------------------------------------------


def test_compose_off_options_is_identity() -> None:
    rgb = _gradient_rgb()
    options = ExportOptions(color_bar=False, rois=False, markers=False, timestamp=False)
    out = compose_frame(rgb, options)
    np.testing.assert_array_equal(out, rgb)


def test_compose_color_bar_appends_strip() -> None:
    rgb = _gradient_rgb()
    options = ExportOptions(color_bar=True, rois=False)
    out = compose_frame(rgb, options, palette="Iron", scale=(0.0, 255.0), suffix="counts")
    assert out.shape[1] == rgb.shape[1] + 104
    # original pixels preserved on the left
    np.testing.assert_array_equal(out[:, : rgb.shape[1]], rgb)
    # strip is not empty background
    assert out[:, rgb.shape[1] + 20 :].std() > 0


def test_compose_draws_rois_and_timestamp() -> None:
    rgb = _gradient_rgb()
    shape = RoiShape(id=1, kind="rect", points=((8.0, 8.0), (24.0, 24.0)), name="Box 1")
    options = ExportOptions(color_bar=False, rois=True, roi_names=True, timestamp=True)
    out = compose_frame(rgb, options, rois=(shape,), label="Frame 1 · 00:01.000")
    assert not np.array_equal(out, rgb)
    # the rectangle outline is drawn inside the ROI bounds area
    border_region = out[8:24, 8:24]
    assert border_region.std() > rgb[8:24, 8:24].std()


def test_compose_flips_roi_coordinates_with_image() -> None:
    rgb = _gradient_rgb()
    shape = RoiShape(id=2, kind="cursor", points=((5.0, 5.0),), name="Spot 1")
    options = ExportOptions(color_bar=False, rois=True, roi_names=False)
    plain = compose_frame(rgb, options, rois=(shape,))
    flipped = compose_frame(rgb, options, rois=(shape,), flips=(True, False))
    # with flip_h the cursor cross must move to the mirrored x position
    assert not np.array_equal(plain[:, :12], flipped[:, -12:])
    assert np.array_equal(plain[:, :12], flipped[:, :12]) is False


# --- TIFF export --------------------------------------------------------------------


def test_tiff16_roundtrip(tmp_path) -> None:
    data = np.linspace(100.0, 200.0, 64, dtype=np.float32).reshape(8, 8)
    dest = tmp_path / "frame.tif"
    save_tiff16(dest, data, 100.0, 200.0)
    loaded = np.asarray(Image.open(dest))
    assert loaded.dtype == np.uint16
    assert loaded.max() == 65535
    assert loaded.min() == 0
    # monotonic mapping preserved
    assert loaded[-1, -1] > loaded[0, 0]


def test_tiff_float_roundtrip(tmp_path) -> None:
    data = np.array([[1.5, -2.25], [300.125, 0.0]], dtype=np.float32)
    dest = tmp_path / "frame.tif"
    save_tiff_float(dest, data)
    loaded = np.asarray(Image.open(dest))
    assert loaded.dtype == np.float32
    np.testing.assert_allclose(loaded, data, rtol=1e-6)


# --- movie writer --------------------------------------------------------------------


def test_movie_writer_mp4_roundtrip(tmp_path) -> None:
    dest = tmp_path / "clip.mp4"
    writer = MovieWriter(dest, fps=10, fmt="mp4")
    for index in range(5):
        writer.append(np.full((48, 64, 3), index * 40, np.uint8))
    writer.close()
    assert dest.is_file() and dest.stat().st_size > 0
    reader = imageio.get_reader(str(dest))
    frames = sum(1 for _ in reader)
    reader.close()
    assert frames == 5


def test_movie_writer_crops_odd_dimensions(tmp_path) -> None:
    dest = tmp_path / "odd.wmv"
    writer = MovieWriter(dest, fps=5, fmt="wmv")
    writer.append(np.full((61, 63, 3), 128, np.uint8))  # odd w/h
    writer.close()
    assert dest.is_file() and dest.stat().st_size > 0


# --- stats sidecar ---------------------------------------------------------------------


def test_stats_csv_rows(qapp, tmp_path) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
    finally:
        packet = window.current_packet
        window.close()
        qapp.processEvents()
    rows = [stats_csv_row(packet, packet.unit.label)]
    dest = tmp_path / "stats.csv"
    write_stats_csv(dest, stats_csv_header([]), rows)
    with dest.open() as handle:
        parsed = list(csv.reader(handle))
    assert parsed[0][:3] == ["frame", "timestamp", "unit"]
    assert parsed[1][0] == str(packet.index + 1)
    assert parsed[1][2] == packet.unit.label


def test_frame_burn_label_contains_frame_and_time(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        label = frame_burn_label(window.current_packet, window.metadata)
        assert label.startswith(f"Frame {window.current_packet.index + 1} · ")
    finally:
        window.close()
        qapp.processEvents()


# --- end-to-end through the decoder ---------------------------------------------------


def _payload(window, **overrides):
    packet = window.current_packet
    payload = {
        "options": ExportOptions(color_bar=False, rois=False, timestamp=False),
        "display": window._display_state(packet),
        "selected_roi_id": None,
        "rois": (),
        "suffix": packet.unit.suffix,
        "unit_label": packet.unit.label,
        "stats_csv": False,
    }
    payload.update(overrides)
    return payload


def test_series_export_end_to_end(qapp, tmp_path) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        window._add_roi("rect", ((10.0, 10.0), (50.0, 40.0)))
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 1)
        finished: list[tuple[bool, str]] = []
        window.decoder.export_finished.connect(lambda ok, msg: finished.append((ok, msg)))
        payload = _payload(
            window,
            kind="series",
            dest=str(tmp_path),
            base_name="seq",
            fmt="png",
            start_frame=0,
            end_frame=2,
            decimation=1,
            stats_csv=True,
            rois=tuple(window._rois),
        )
        window.decoder.request_export_sequence(payload)
        assert wait_until(qapp, lambda: len(finished) == 1, timeout=30.0)
        assert finished[0][0], finished[0][1]
        for index in range(1, 4):
            assert (tmp_path / f"seq_{index:05d}.png").is_file()
        stats = tmp_path / "seq_stats.csv"
        assert stats.is_file()
        with stats.open() as handle:
            rows = list(csv.reader(handle))
        assert len(rows) == 4  # header + 3 frames
        assert any("Box 1 mean" in cell for cell in rows[0])
    finally:
        window.close()
        qapp.processEvents()


def test_movie_export_end_to_end(qapp, tmp_path) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        finished: list[tuple[bool, str]] = []
        window.decoder.export_finished.connect(lambda ok, msg: finished.append((ok, msg)))
        payload = _payload(
            window,
            kind="movie",
            dest=str(tmp_path / "out.mp4"),
            fmt="mp4",
            fps=10.0,
            start_frame=0,
            end_frame=4,
            decimation=1,
        )
        window.decoder.request_export_sequence(payload)
        assert wait_until(qapp, lambda: len(finished) == 1, timeout=30.0)
        assert finished[0][0], finished[0][1]
        dest = tmp_path / "out.mp4"
        assert dest.is_file() and dest.stat().st_size > 0
        reader = imageio.get_reader(str(dest))
        assert sum(1 for _ in reader) == 5
        reader.close()
    finally:
        window.close()
        qapp.processEvents()


def test_batch_extract_reports_non_ats_honestly(qapp, tmp_path, monkeypatch) -> None:
    window = MainWindow()
    errors = []
    monkeypatch.setattr(window, "_show_error", lambda title, message: errors.append(message))
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        finished: list[tuple[bool, str]] = []
        window.decoder.export_finished.connect(lambda ok, msg: finished.append((ok, msg)))
        window.decoder.request_batch_extract(
            {
                "files": [str(SAMPLES / "1.ats"), str(SAMPLES / "2.seq")],
                "folder": str(tmp_path),
                "decimation": 10,
            }
        )
        assert wait_until(qapp, lambda: len(finished) == 1, timeout=60.0)
        _ok, message = finished[0]
        assert "1 of 2" in message
        assert "2.seq" in message  # reported as producing no output
        assert errors and "2.seq" in errors[0]
        assert (tmp_path / "1_extract.ats").is_file()
        assert not (tmp_path / "2_extract.ats").exists()
    finally:
        window.close()
        qapp.processEvents()


def test_roi_bitmask_export(qapp, tmp_path) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        window._add_roi("rect", ((10.0, 10.0), (60.0, 50.0)))
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 1)
        finished: list[tuple[bool, str]] = []
        window.decoder.export_finished.connect(lambda ok, msg: finished.append((ok, msg)))
        window.decoder.request_export_bitmasks(str(tmp_path))
        assert wait_until(qapp, lambda: len(finished) == 1, timeout=15.0)
        assert finished[0][0]
        mask = tmp_path / "Box_1_bitmask.png"
        assert mask.is_file()
        loaded = np.asarray(Image.open(mask))
        assert loaded.shape[:2] == (
            window.metadata.height,
            window.metadata.width,
        ) or loaded.shape[:2] == (window.metadata.width, window.metadata.height)
    finally:
        window.close()
        qapp.processEvents()


# --- display-state parity & recents ------------------------------------------------------


def test_render_frame_rgb_matches_window_pipeline(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        packet = window.current_packet
        state = window._display_state(packet)
        rgb_a, low_a, high_a = render_frame_rgb(packet.data, state, packet.clip_mask)
        # the canvas image is produced by the same state → identical pixels
        canvas_rgb = window.canvas.image
        x, y = canvas_rgb.width() // 2, canvas_rgb.height() // 2
        from PySide6.QtGui import QColor

        pixel = canvas_rgb.pixelColor(x, y)
        assert (pixel.red(), pixel.green(), pixel.blue()) == tuple(int(v) for v in rgb_a[y, x])
        assert (low_a, high_a) == window._current_scale(packet)
    finally:
        window.close()
        qapp.processEvents()


def test_recent_files_roundtrip(qapp) -> None:
    settings = app_settings()
    previous = settings.value("files/recent", [])
    settings.setValue("files/recent", [])
    window = MainWindow()
    window.show()
    try:
        assert window._recent_files() == []
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(
            qapp, lambda: window.current_packet is not None and not window._busy
        )
        recents = window._recent_files()
        assert len(recents) == 1
        assert recents[0].endswith("2.seq")
        # opening the same file again must not duplicate the entry
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: not window._busy)
        assert len(window._recent_files()) == 1
        # menu contains the entry + clear action
        texts = [a.text() for a in window._recent_menu.actions()]
        assert "2.seq" in texts
        window._clear_recent_files()
        assert window._recent_files() == []
    finally:
        settings.setValue("files/recent", previous)
        window.close()
        qapp.processEvents()
