# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Parity tests for the Phase-1 performance work (H1/L2/H5-dedupe).

Covers: SDK built-in Image-ROI statistics vs the NumPy reduction they replace,
the processing-active fallback path, extrema-reuse in display_scale, the
frame-0 packet captured during open(), and temporal-sample deduplication.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from conftest import wait_until

from flir_player.main_window import MainWindow
from flir_player.processing import ProcessingState
from flir_player.render import DisplayState, display_scale
from flir_player.source import FlirVideoSource


SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"


def _numpy_reference(data: np.ndarray):
    finite_mask = np.isfinite(data)
    num_pixels = int(finite_mask.sum())
    if not num_pixels:
        return (0.0, 0.0, 0.0, 0.0, 0, None, None)
    finite = data[finite_mask]
    return (
        float(finite.min()),
        float(finite.max()),
        float(finite.mean()),
        float(finite.std()),
        num_pixels,
        None,  # positions checked separately by value
        None,
    )


def _check_packet_stats(packet, data: np.ndarray) -> None:
    minimum, maximum, mean, std_dev, num_pixels, _, _ = _numpy_reference(data)
    assert packet.minimum == pytest.approx(minimum, rel=1e-6, abs=1e-9)
    assert packet.maximum == pytest.approx(maximum, rel=1e-6, abs=1e-9)
    assert packet.mean == pytest.approx(mean, rel=1e-5, abs=1e-9)
    assert packet.std_dev == pytest.approx(std_dev, rel=1e-4, abs=1e-9)
    assert packet.num_pixels == num_pixels
    if num_pixels:
        # Tied extrema may resolve to different (equally valid) pixels.
        x_min, y_min = packet.min_position
        x_max, y_max = packet.max_position
        assert data[y_min, x_min] == pytest.approx(packet.minimum)
        assert data[y_max, x_max] == pytest.approx(packet.maximum)


@pytest.mark.parametrize("filename", ("1.ats", "2.seq"))
def test_sdk_image_roi_stats_match_numpy(filename: str) -> None:
    source = FlirVideoSource()
    try:
        source.open(SAMPLES / filename)
        assert source._image_roi is not None  # the SDK path is actually exercised
        for unit in source.available_units:
            source.set_unit(unit.key)
            for index in (0, 3, 17, 101):
                packet = source.read_frame(index)
                assert not source.processing.is_active
                _check_packet_stats(packet, packet.data)
    finally:
        source.close()


def test_sdk_image_roi_stats_csq_with_stored_roi() -> None:
    if not (SAMPLES / "3.csq").exists():
        pytest.skip("sample recording '3.csq' is not present in local/data/")
    source = FlirVideoSource()
    try:
        source.open(SAMPLES / "3.csq")
        # 3.csq stores an extra cursor ROI; the built-in one must still be found.
        assert source._image_roi is not None
        for index in (0, 500, 5000):
            packet = source.read_frame(index)
            _check_packet_stats(packet, packet.data)
    finally:
        source.close()


def test_processing_active_stats_use_numpy_path() -> None:
    source = FlirVideoSource()
    try:
        source.open(SAMPLES / "2.seq")
        baseline = source.read_frame(10)
        source.set_processing(ProcessingState(point=("offset", 5.0)))
        assert source.processing.is_active
        packet = source.read_frame(10)
        _check_packet_stats(packet, packet.data)
        # the offset filter must actually have shifted the data
        assert packet.mean == pytest.approx(baseline.mean + 5.0, rel=1e-4)
    finally:
        source.close()


def test_take_first_packet_consumed_once() -> None:
    source = FlirVideoSource()
    try:
        source.open(SAMPLES / "1.ats")
        packet = source.take_first_packet()
        assert packet is not None and packet.index == 0
        assert source.take_first_packet() is None
        # regular decoding is unaffected afterwards
        assert source.read_frame(0).index == 0
    finally:
        source.close()


def test_display_scale_extrema_matches_full_scan() -> None:
    rng = np.random.default_rng(7)
    cases = [
        rng.normal(25.0, 4.0, size=(48, 64)).astype(np.float32),
        np.full((16, 16), 3.5, dtype=np.float32),  # constant → padding path
    ]
    data_with_nan = cases[0].copy()
    data_with_nan[0, 0] = np.nan
    data_with_nan[10, 20] = np.inf
    cases.append(data_with_nan)
    dynamic = DisplayState()
    for data in cases:
        scanned = display_scale(data, dynamic)
        finite = data[np.isfinite(data)]
        extrema = (float(finite.min()), float(finite.max()))
        reused = display_scale(data, dynamic, extrema=extrema)
        assert reused == pytest.approx(scanned)


def test_display_scale_extrema_all_nan_renders_identically() -> None:
    # The scan path returns (0, 1) for an all-NaN frame while sanitized extrema
    # give (-1, 1); every pixel is NaN either way, so the RGB output must match.
    from flir_player.render import render_frame_rgb

    data = np.full((16, 16), np.nan, dtype=np.float32)
    state = DisplayState()
    rgb_scanned, _, _ = render_frame_rgb(data, state)
    rgb_reused, _, _ = render_frame_rgb(data, state, extrema=(0.0, 0.0))
    assert np.array_equal(rgb_scanned, rgb_reused)


def test_temporal_samples_dedupe_by_frame_index(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window._add_roi("rect", ((100.0, 100.0), (150.0, 140.0)))
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 1)
        roi_id = window.current_packet.roi_stats[0].id

        packet = window.current_packet
        for _ in range(3):  # simulate tab switches / statistic changes re-presenting
            window._update_plots(packet)
            window._update_plots(packet, force=True)
        assert len(window._temporal[roi_id]) == 1

        window.seek_to(packet.index + 1)
        assert wait_until(qapp, lambda: window.current_packet.index == packet.index + 1)
        window._update_plots(window.current_packet)
        window._update_plots(window.current_packet)
        assert len(window._temporal[roi_id]) == 2
    finally:
        window.close()
        qapp.processEvents()


# --- Phase 2: float32 colorization + fused overlays -------------------------------
#
# The float64 → float32 render rewrite is accepted to be visually lossless
# rather than bit-identical: normalization rounding can shift a LUT index by
# ±1 at bin edges. Measured bounds used as thresholds below (this machine):
# linear path ≤3 channel diff on ≤1% of pixels; PE path ≤5 on ≤0.003%;
# overlay painting is exact uint8 assignment → bit-identical.

def _legacy_render_rgb(data, palette, low, high, pe=0.0, invert=False):
    """The pre-optimization float64 render pipeline, kept as the reference."""
    from flir_player.render import palette_lut, plateau_equalized_indices

    span = high - low
    if not np.isfinite(span) or span <= 0:
        span = 1.0
    normalized = np.nan_to_num((data.astype(np.float64, copy=False) - low) / span)
    if pe > 0.0:
        indices = plateau_equalized_indices(normalized, pe)
    else:
        indices = np.clip(normalized * 255.0, 0.0, 255.0).astype(np.uint8)
    return np.ascontiguousarray(palette_lut(palette, invert)[indices])


def _assert_tolerance_parity(new, old, max_channel_diff=8, max_diff_fraction=0.02):
    assert new.shape == old.shape and new.dtype == old.dtype
    assert new.flags.c_contiguous
    diff = np.abs(new.astype(np.int16) - old.astype(np.int16)).max(axis=2)
    assert int(diff.max()) <= max_channel_diff
    assert float((diff > 0).mean()) <= max_diff_fraction


def test_render_rgb_tolerance_parity_synthetic() -> None:
    from flir_player.render import render_rgb

    rng = np.random.default_rng(3)
    cases = [
        rng.integers(9000, 14000, size=(64, 80)).astype(np.uint16),
        rng.normal(28.0, 6.0, size=(64, 80)).astype(np.float32),
        np.where(rng.random((64, 80)) < 0.05, np.nan, rng.normal(300, 20, (64, 80))),
    ]
    for data in cases:
        low, high = float(np.nanmin(data)), float(np.nanmax(data))
        for pe in (0.0, 0.8):
            new = render_rgb(data, "Iron", low, high, pe=pe)
            old = _legacy_render_rgb(data, "Iron", low, high, pe=pe)
            _assert_tolerance_parity(new, old)


def test_render_rgb_tolerance_parity_real_files() -> None:
    from flir_player.render import render_rgb

    source = FlirVideoSource()
    try:
        source.open(SAMPLES / "2.seq")
        for unit_key in ("counts", "temperature_factory_c"):
            source.set_unit(unit_key)
            data = source.read_frame(100).data
            low, high = float(np.nanmin(data)), float(np.nanmax(data))
            for palette in ("Iron", "Rainbow", "Grayscale"):
                for pe in (0.0, 0.8):
                    new = render_rgb(data, palette, low, high, pe=pe)
                    old = _legacy_render_rgb(data, palette, low, high, pe=pe)
                    _assert_tolerance_parity(new, old)
    finally:
        source.close()


def test_render_frame_rgb_fused_overlays_bit_exact() -> None:
    """Fused in-place overlays must equal the legacy copy-per-overlay chain."""
    from flir_player.render import (
        DisplayState,
        apply_clip_overlay,
        apply_isotherm_overlay,
        apply_segmentation_overlay,
        display_scale,
        render_frame_rgb,
        segmentation_overlays,
    )

    rng = np.random.default_rng(11)
    data = rng.normal(300.0, 25.0, size=(96, 128))
    data[5, 5] = np.nan
    low, high = float(np.nanmin(data)), float(np.nanmax(data))
    clip_mask = np.zeros(data.shape, bool)
    clip_mask[::7, ::5] = True
    state = DisplayState(
        segmentation=(True, low + 10.0, high - 10.0),
        isotherm=("interval", (low + high) / 2.0, high - 5.0),
        clipping=True,
    )

    new_rgb, _, _ = render_frame_rgb(data, state, clip_mask, extrema=(low, high))
    scale = display_scale(data, state, extrema=(low, high))  # same scale both ways
    old_rgb = _legacy_render_rgb(data, "Iron", *scale)
    below, above = segmentation_overlays(data, low + 10.0, high - 10.0)
    old_rgb = apply_segmentation_overlay(old_rgb, below, above)
    old_rgb = apply_isotherm_overlay(
        old_rgb, data, "interval", (low + high) / 2.0, high - 5.0
    )
    old_rgb = apply_clip_overlay(old_rgb, clip_mask)
    assert np.array_equal(new_rgb, old_rgb)


# --- Phase 3: latest-wins requests + on-demand analysis payloads -------------------

def test_decoder_coalesces_pending_frame_requests() -> None:
    from flir_player.decoder import DecoderThread

    decoder = DecoderThread()
    decoder.request_frame(1, 1)
    decoder.request_frame(2, 2)
    decoder.request_frame(3, 3)
    decoder.request_unit("counts", 2, 4)  # state changes keep FIFO order
    decoder.request_frame(4, 5)
    with decoder._commands.mutex:
        commands = list(decoder._commands.queue)
    # all pending frame reads collapse to the newest; the state change is
    # untouched and keeps its position (the GUI discards stale request IDs,
    # so dropping superseded reads cannot present anything out of order)
    assert commands == [
        ("unit", ("counts", 2, 4)),
        ("frame", (4, 5, True, True)),
    ]


def test_read_frame_requirement_flags() -> None:
    source = FlirVideoSource()
    try:
        source.open(SAMPLES / "1.ats")
        lean = source.read_frame(0, need_clip=False, need_metadata=False)
        assert lean.clip_mask is None
        assert lean.metadata_entries == ()
        full = source.read_frame(0, need_clip=True, need_metadata=True)
        assert len(full.metadata_entries) > 0
        # stats are identical either way (SDK path is requirement-independent)
        assert lean.minimum == full.minimum and lean.mean == full.mean
    finally:
        source.close()


def test_clip_mask_refetched_when_overlay_enabled(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window._change_unit("temperature_factory_c")
        assert wait_until(qapp, lambda: window.current_packet.unit.key == "temperature_factory_c")

        window._change_overlays(False, False)
        window.seek_to(10)
        assert wait_until(qapp, lambda: window.current_packet.index == 10)
        assert window.current_packet.clip_mask is None  # decoded lean

        window._change_overlays(True, False)  # paused → must refetch with a mask
        assert wait_until(qapp, lambda: window.current_packet.clip_mask is not None)
    finally:
        window.close()
        qapp.processEvents()


def test_metadata_entries_fetched_when_tab_visible(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        # the initial open packet is always full; later interactive reads with
        # the panel hidden are lean
        window.seek_to(5)
        assert wait_until(qapp, lambda: window.current_packet.index == 5)
        assert window.current_packet.metadata_entries == ()

        window._toggle_statistics(True)
        window.bottom_panel.tabs.setCurrentWidget(window.bottom_panel.metadata)
        assert wait_until(qapp, lambda: len(window.current_packet.metadata_entries) > 0)
    finally:
        window.close()
        qapp.processEvents()


def test_metadata_repopulates_after_panel_reopen(qapp) -> None:
    """R2: hide → seek → reopen with Metadata selected must refetch entries."""
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window._toggle_statistics(True)
        window.bottom_panel.tabs.setCurrentWidget(window.bottom_panel.metadata)
        metadata_table = window.bottom_panel.metadata.table
        assert wait_until(qapp, lambda: metadata_table.rowCount() > 0)

        window._toggle_statistics(False)  # Metadata stays the current tab
        window.seek_to(5)
        assert wait_until(qapp, lambda: window.current_packet.index == 5)
        assert not window.current_packet.metadata_loaded  # decoded lean

        # reopening fires no tab change; the reopen itself must refetch
        window._toggle_statistics(True)
        assert wait_until(qapp, lambda: window.current_packet.metadata_loaded)
        assert wait_until(qapp, lambda: metadata_table.rowCount() > 0)
    finally:
        window.close()
        qapp.processEvents()


def test_metadata_refetch_during_playback_does_not_stall(qapp) -> None:
    """R2 while playing: the on-demand refetch must not stall the decode chain."""
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window._toggle_statistics(True)
        window.bottom_panel.tabs.setCurrentWidget(window.bottom_panel.metadata)
        assert wait_until(qapp, lambda: window.bottom_panel.metadata.table.rowCount() > 0)
        window._toggle_statistics(False)
        window.seek_to(0)
        assert wait_until(qapp, lambda: window.current_packet.index == 0)

        window.toggle_playback()
        assert window.playing
        try:
            window._toggle_statistics(True)  # reopen mid-playback
            assert wait_until(qapp, lambda: window.current_packet.metadata_loaded)
            index_after_refetch = window.current_packet.index
            assert wait_until(
                qapp,
                lambda: window.current_packet.index >= index_after_refetch + 2,
                timeout=5.0,
            )
            assert window.playing
        finally:
            window.pause_playback()
    finally:
        window.close()
        qapp.processEvents()


# --- Phase 4: prefetch + late-frame dropping ----------------------------------------

def test_skip_ahead_index_rules(qapp) -> None:
    """Late playback jumps to the media-clock frame; temporal filters forbid it."""
    from dataclasses import replace
    from datetime import timedelta
    from types import SimpleNamespace

    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window.playing = True
        window.playback_speed = 1.0
        packet = window.current_packet
        fps = window.metadata.nominal_fps
        end = window.metadata.num_frames - 1

        # not late → plain +1
        window._playback_anchor_timestamp = packet.timestamp
        window._playback_anchor_index = packet.index
        window._playback_clock = SimpleNamespace(elapsed=lambda: 0)
        assert window._skip_ahead_index(packet, end) == packet.index + 1

        # 100 s behind the media clock → jump, capped at one second of media
        window._playback_clock = SimpleNamespace(elapsed=lambda: 100_000)
        expected = min(packet.index + max(1, int(fps)), end)
        window._perf["skipped"] = 0
        assert window._skip_ahead_index(packet, end) == expected
        assert window._perf["skipped"] == expected - packet.index - 1

        # temporal filter active → never skip decode, only presentation drops
        window._filters_state["temporal"] = ("average", 5)
        assert window._skip_ahead_index(packet, end) == packet.index + 1
    finally:
        window.close()
        qapp.processEvents()


def test_present_due_drops_superseded_packets(qapp) -> None:
    from dataclasses import replace
    from datetime import timedelta
    from types import SimpleNamespace

    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window.playing = True
        base = window.current_packet
        anchor = base.timestamp
        window._playback_anchor_timestamp = anchor
        window._playback_anchor_index = base.index
        # wall clock far past all three packets' due times
        window._playback_clock = SimpleNamespace(elapsed=lambda: 100_000)
        window._perf = {"presented": 0, "dropped": 0, "skipped": 0}
        packets = [
            replace(base, index=base.index + i, timestamp=anchor + timedelta(seconds=i))
            for i in (0, 1, 2)
        ]
        window._ready.extend(packets)
        window._present_due()
        assert window.current_packet.index == packets[-1].index  # newest due wins
        assert window._perf["dropped"] == 2
        assert window._perf["presented"] == 1
        assert not window._ready
    finally:
        window.close()
        qapp.processEvents()


def test_next_decode_requested_on_arrival(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window.playing = True
        window._playback_clock.start()
        window._playback_outstanding = True
        requested: list[int] = []
        original = window.decoder.request_frame
        window.decoder.request_frame = lambda index, rid, **kwargs: requested.append(index)
        try:
            window._on_frame_ready(window.current_packet)
        finally:
            window.decoder.request_frame = original
        # the next decode is requested immediately on arrival, before presentation
        assert requested == [window.current_packet.index + 1]
        assert len(window._ready) == 1
    finally:
        window.close()
        qapp.processEvents()


def test_playback_keeps_up_end_to_end(qapp) -> None:
    import time

    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        start_index = window.current_packet.index
        window.toggle_playback()
        assert window.playing
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            qapp.processEvents()
            if window.current_packet.index - start_index >= 10:
                break
            time.sleep(0.01)
        advanced = window.current_packet.index - start_index
        window.pause_playback()
        # 2.seq is ~30 fps; 10 frames in well under the old serialized budget
        assert advanced >= 10
    finally:
        window.close()
        qapp.processEvents()


# --- Follow-up F1: end-of-range presentation (R1) -----------------------------------


def _record_presentations(window) -> list[int]:
    """Wrap _present_packet to record every presented frame index."""
    presented: list[int] = []
    original = window._present_packet

    def record(packet) -> None:
        presented.append(packet.index)
        original(packet)

    window._present_packet = record
    return presented


def test_playback_presents_final_frame_before_stop(qapp) -> None:
    """R1: the range's terminal frame must be shown before playback stops.

    Decode reaches the range end before presentation; the old code paused (or
    wrapped) at decode-arrival time and discarded the queued terminal packet.
    """
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window.loop_playback = False
        window._play_range_changed(0, 2)
        window.seek_to(0)
        assert wait_until(qapp, lambda: window.current_packet.index == 0)
        presented = _record_presentations(window)
        window.toggle_playback()
        assert window.playing
        assert wait_until(qapp, lambda: not window.playing, timeout=5.0)
        qapp.processEvents()
        assert presented == [1, 2]
        assert window.current_packet.index == 2
    finally:
        window.close()
        qapp.processEvents()


def test_playback_loop_presents_full_range_each_wrap(qapp) -> None:
    """R1: loop wraps show every frame of the range, including its end."""
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window.loop_playback = True
        window._play_range_changed(0, 2)
        window.seek_to(0)
        assert wait_until(qapp, lambda: window.current_packet.index == 0)
        presented = _record_presentations(window)
        window.toggle_playback()
        assert window.playing
        try:
            assert wait_until(qapp, lambda: len(presented) >= 6, timeout=5.0)
        finally:
            window.pause_playback()
        assert presented[:6] == [1, 2, 0, 1, 2, 0]
    finally:
        window.close()
        qapp.processEvents()


def test_end_pending_defers_pause_until_terminal_presented(qapp) -> None:
    """Decode hitting the range end pauses only after the terminal frame shows."""
    from dataclasses import replace
    from types import SimpleNamespace

    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window.loop_playback = False
        window._play_range_changed(0, 2)
        window.seek_to(1)
        assert wait_until(qapp, lambda: window.current_packet.index == 1)
        window.playing = True
        window._playback_anchor_timestamp = window.current_packet.timestamp
        window._playback_anchor_index = window.current_packet.index
        window._playback_clock = SimpleNamespace(elapsed=lambda: 0)
        terminal = replace(window.current_packet, index=2)
        window._ready.append(terminal)
        window._latest_decoded = terminal

        window._request_next_playback_frame(terminal)  # decode-side range end
        assert window._end_pending
        assert window.playing  # pause deferred while the terminal packet is queued

        window._playback_clock = SimpleNamespace(elapsed=lambda: 100_000)
        window._present_due()  # presents the terminal frame, then resolves
        assert window.current_packet.index == 2
        assert not window.playing
        assert not window._end_pending
    finally:
        window.close()
        qapp.processEvents()


def test_play_range_growth_resumes_after_end_pending(qapp) -> None:
    """Growing the range while end-pending resumes decoding past the old end."""
    from dataclasses import replace
    from types import SimpleNamespace

    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window.loop_playback = False
        window._play_range_changed(0, 2)
        window.seek_to(1)
        assert wait_until(qapp, lambda: window.current_packet.index == 1)
        window.playing = True
        window._playback_anchor_timestamp = window.current_packet.timestamp
        window._playback_anchor_index = window.current_packet.index
        window._playback_clock = SimpleNamespace(elapsed=lambda: 0)
        terminal = replace(window.current_packet, index=2)
        window._ready.append(terminal)
        window._latest_decoded = terminal
        window._request_next_playback_frame(terminal)
        assert window._end_pending

        requested: list[int] = []
        original = window.decoder.request_frame
        window.decoder.request_frame = lambda index, rid, **kwargs: requested.append(index)
        try:
            window._play_range_changed(0, 5)
        finally:
            window.decoder.request_frame = original
        assert not window._end_pending
        assert requested == [3]
    finally:
        window.close()
        qapp.processEvents()


# --- Phase 5: byte-budgeted cache, streaming export, temporal history ----------------

def _fake_packet(index: int, shape=(4, 4), with_mask=False):
    from dataclasses import replace

    from flir_player.models import FramePacket, UnitOption

    packet = FramePacket(
        index=index,
        data=np.zeros(shape, dtype=np.float32),
        timestamp=None,
        unit=UnitOption("counts", "Counts", "counts"),
        minimum=0.0,
        maximum=1.0,
        mean=0.5,
        clip_mask=np.zeros(shape, dtype=bool) if with_mask else None,
    )
    return replace(packet)


def test_packet_cache_byte_budget_and_lru() -> None:
    from flir_player.decoder import _PacketCache

    # each packet: 4x4 float32 = 64 B (+16 B mask) → 80 B; budget for ~2.5
    cache = _PacketCache(budget_bytes=200, max_count=100)
    for index in range(5):
        cache.put(("counts", index), _fake_packet(index, with_mask=True))
    assert cache.bytes_used <= 200
    assert len(cache) == 2  # 2 x 80 B fit; oldest evicted
    assert cache.evictions == 3
    assert cache.get(("counts", 0)) is None
    assert cache.get(("counts", 4)) is not None

    # LRU: touching the oldest survivor protects it from the next eviction
    cache.get(("counts", 3))
    cache.put(("counts", 5), _fake_packet(5, with_mask=True))
    assert cache.get(("counts", 4)) is None
    assert cache.get(("counts", 3)) is not None

    # count cap still applies with a huge byte budget
    counted = _PacketCache(budget_bytes=10**9, max_count=3)
    for index in range(6):
        counted.put(("counts", index), _fake_packet(index))
    assert len(counted) == 3


def test_packet_cache_payload_sufficiency_flags() -> None:
    """R4: empty-but-loaded payloads satisfy requests; lean copies do not.

    ``clip_mask is None`` is the *common* full result (no clipped pixels), so
    judging leanness by the payload itself rejected nearly every cached frame
    and forced redundant SDK reads. The loaded flags decide instead, and a
    hit is counted only when the entry satisfies the request.
    """
    from dataclasses import replace

    from flir_player.decoder import _PacketCache

    cache = _PacketCache(budget_bytes=10**6, max_count=10)
    full = _fake_packet(0)  # clip_mask None, entries (), both loaded by default
    lean = _fake_packet(1)
    lean = replace(lean, clip_loaded=False, metadata_loaded=False)
    cache.put(("counts", 0), full)
    cache.put(("counts", 1), lean)

    # a fully-checked frame with nothing clipped serves a clipping request
    assert cache.get(("counts", 0), need_clip=True, need_metadata=True) is full
    assert cache.hits == 1 and cache.misses == 0
    # a lean copy is refused for the same request, without counting a hit
    assert cache.get(("counts", 1), need_clip=True) is None
    assert cache.get(("counts", 1), need_metadata=True) is None
    assert cache.hits == 1 and cache.misses == 2
    # ... but still serves requests that do not need those payloads
    assert cache.get(("counts", 1)) is lean
    assert cache.hits == 2 and cache.misses == 2


def test_cached_frames_serve_repeated_seeks(qapp, monkeypatch) -> None:
    """R4 end-to-end: repeated seeks hit the cache instead of the SDK."""
    from flir_player.source import FlirVideoSource

    calls: list[int] = []
    original = FlirVideoSource.read_frame

    def counting(self, index, request_id=0, need_clip=True, need_metadata=True):
        calls.append(index)
        return original(self, index, request_id, need_clip, need_metadata)

    monkeypatch.setattr(FlirVideoSource, "read_frame", counting)

    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window.seek_to(5)
        assert wait_until(qapp, lambda: window.current_packet.index == 5)
        window.seek_to(0)
        assert wait_until(qapp, lambda: window.current_packet.index == 0)
        window.seek_to(5)
        assert wait_until(qapp, lambda: window.current_packet.index == 5)
        # frame 0 was read exactly once (at open, handed out via
        # take_first_packet); frame 5 was decoded once and served from the
        # cache on the second seek (clipping overlay on). Before the
        # completeness flags the second seek to 5 re-read the SDK ([0, 5, 5]).
        assert calls == [0, 5]
    finally:
        window.close()
        qapp.processEvents()


def test_stats_csv_writer_streams_rows(tmp_path) -> None:
    from flir_player.export import StatsCsvWriter, stats_csv_header

    dest = tmp_path / "stream.csv"
    writer = StatsCsvWriter(dest, stats_csv_header(["Roi 1"]))
    writer.append([1, "t", "counts", 1, 2, 1.5, 0.5, 4, 1, 2, 1.5, 0.5])
    writer.append([2, "t", "counts", 1, 2, 1.5, 0.5, 4, 1, 2, 1.5, 0.5])
    writer.close()
    import csv as csv_module

    with dest.open() as handle:
        rows = list(csv_module.reader(handle))
    assert len(rows) == 3 and "Roi 1 std_dev" in rows[0]
    assert rows[0][-1] == "analysis_revision"


def test_tiff_series_bypasses_rgb(qapp, tmp_path) -> None:
    import csv as csv_module

    from PIL import Image

    from test_export import _payload

    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        finished: list[tuple[bool, str]] = []
        window.decoder.export_finished.connect(lambda ok, msg: finished.append((ok, msg)))
        payload = _payload(
            window,
            kind="series",
            dest=str(tmp_path),
            base_name="raw",
            fmt="tiff16",
            start_frame=0,
            end_frame=1,
            decimation=1,
            stats_csv=True,
        )
        window.decoder.request_export_sequence(payload)
        assert wait_until(qapp, lambda: len(finished) == 1, timeout=30.0)
        assert finished[0][0], finished[0][1]
        for index in range(1, 3):
            path = tmp_path / f"raw_{index:05d}.tif"
            assert path.is_file()
            with Image.open(path) as image:
                assert image.mode == "I;16"
        with (tmp_path / "raw_stats.csv").open() as handle:
            rows = list(csv_module.reader(handle))
        assert len(rows) == 3  # header + 2 streamed rows
    finally:
        window.close()
        qapp.processEvents()


def test_temporal_history_capped(qapp) -> None:
    from flir_player.main_window import TEMPORAL_POINT_CAP

    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        window._add_roi("rect", ((100.0, 100.0), (150.0, 140.0)))
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 1)
        roi_id = window.current_packet.roi_stats[0].id
        stats = window.current_packet.roi_stats[0]
        history = window._temporal.setdefault(roi_id, {})
        # simulate a very long session (keys disjoint from the current index)
        base = 10**6
        for index in range(base, base + TEMPORAL_POINT_CAP + 50):
            history[index] = (float(index), stats.mean, stats.minimum, stats.maximum, stats.std_dev)
        # next accumulation must trim back below the cap, not grow unbounded
        window._update_plots(window.current_packet)
        size = len(window._temporal[roi_id])
        assert TEMPORAL_POINT_CAP // 2 <= size <= TEMPORAL_POINT_CAP
    finally:
        window.close()
        qapp.processEvents()


def test_envelope_preserves_excursions() -> None:
    from flir_player.plots import envelope

    seconds = np.arange(10000, dtype=float)
    values = np.zeros(10000)
    values[4321] = 500.0  # short spike must survive downsampling
    x, y = envelope(seconds, values, max_points=2000)
    assert x.size <= 2000
    assert y.max() == 500.0
    assert y.min() == 0.0
    # short series pass through untouched
    small_t, small_v = envelope(seconds[:100], values[:100])
    assert small_t.size == 100


def test_envelope_keeps_newest_remainder() -> None:
    """R3: non-divisible lengths must not drop the newest samples."""
    from flir_player.plots import envelope

    for size in (2001, 2002, 2999, 5000, 100_001):
        seconds = np.arange(size, dtype=float) / 30.0
        values = np.zeros(size)
        values[-1] = 100.0  # final-sample spike
        x, y = envelope(seconds, values, max_points=2000)
        assert x.size <= 2000
        assert y.max() == 100.0
        assert x[-1] == seconds[-1]  # the chart reaches the newest timestamp
        assert np.all(np.diff(x) >= 0)  # monotonic for the line artist

    # bucket min/max match a brute-force reference on random data
    rng = np.random.default_rng(3)
    size = 7777
    seconds = np.arange(size, dtype=float)
    values = rng.normal(size=size)
    x, y = envelope(seconds, values, max_points=2000)
    edges = np.linspace(0, size, 1001).astype(int)
    ref_min = [values[edges[i] : edges[i + 1]].min() for i in range(1000)]
    ref_max = [values[edges[i] : edges[i + 1]].max() for i in range(1000)]
    assert np.allclose(y[0::2], ref_min)
    assert np.allclose(y[1::2], ref_max)


def test_temporal_plot_reuses_line_artists(qapp) -> None:
    from flir_player.plots import TemporalPlotPanel

    panel = TemporalPlotPanel()
    try:
        seconds = np.array([0.0, 1.0, 2.0])
        values = np.array([1.0, 2.0, 3.0])
        panel.set_series([("Box 1", "#FFF", seconds, values)], "Mean", "counts")
        first_line = panel._lines["Box 1"]
        panel.set_series(
            [("Box 1", "#FFF", np.array([0.0, 1.0, 2.0, 3.0]), np.array([1.0, 2.0, 3.0, 4.0]))],
            "Mean",
            "counts",
        )
        assert panel._lines["Box 1"] is first_line  # set_data, not replot
        panel.set_series([], "Mean", "counts")  # ROI gone → artist removed
        assert "Box 1" not in panel._lines
    finally:
        # flush the pending draw_idle timer before the panel is destroyed,
        # otherwise the QTimer.singleShot fires on a deleted C++ widget
        qapp.processEvents()
        panel.deleteLater()
        qapp.processEvents()


# --- Phase 6: filter kernels ---------------------------------------------------------

def test_temporal_average_matches_stacked_nanmean() -> None:
    from flir_player.processing import TemporalBuffer

    rng = np.random.default_rng(5)
    frames = [rng.normal(20.0, 5.0, size=(12, 9)).astype(np.float32) for _ in range(12)]
    frames[3][0, 0] = np.nan
    frames[7][4, 4] = np.nan
    depth = 4
    buffer = TemporalBuffer()
    for i, frame in enumerate(frames):
        result = buffer.apply(frame, "average", depth)
        window = [f.astype(np.float32) for f in frames[max(0, i - depth + 1) : i + 1]]
        with np.errstate(invalid="ignore"):
            reference = np.nanmean(np.stack(window), axis=0)
        np.testing.assert_allclose(result, reference, rtol=1e-6, atol=1e-6)


def test_temporal_min_max_subtract_match_reference() -> None:
    from flir_player.processing import TemporalBuffer

    rng = np.random.default_rng(6)
    frames = [rng.normal(0.0, 10.0, size=(8, 6)).astype(np.float32) for _ in range(10)]
    for name, reducer in (("min", np.nanmin), ("max", np.nanmax)):
        buffer = TemporalBuffer()
        for i, frame in enumerate(frames):
            result = buffer.apply(frame, name, 3)
            window = np.stack(frames[max(0, i - 2) : i + 1])
            np.testing.assert_array_equal(result, reducer(window, axis=0))
    buffer = TemporalBuffer()
    for i, frame in enumerate(frames):
        result = buffer.apply(frame, "subtract", 3)
        oldest = frames[max(0, i - 2)]
        np.testing.assert_allclose(result, frame - oldest, rtol=1e-6, atol=1e-6)


def test_temporal_ring_reuses_storage() -> None:
    from flir_player.processing import TemporalBuffer

    buffer = TemporalBuffer()
    frame = np.ones((6, 6), dtype=np.float32)
    buffer.apply(frame, "average", 3)
    ring = buffer._ring
    for _ in range(10):
        buffer.apply(frame, "average", 3)
    assert buffer._ring is ring  # no per-frame history reallocation
    assert len(buffer) == 3


def test_box_mean_bit_identical_to_legacy() -> None:
    from flir_player.processing import box_mean

    def legacy(data, size):
        valid = np.isfinite(data)
        values = np.where(valid, data, 0.0).astype(np.float64)
        weights = valid.astype(np.float64)
        from flir_player.processing import _moving_sum_axis

        sums = _moving_sum_axis(_moving_sum_axis(values, size, 0), size, 1)
        counts = _moving_sum_axis(_moving_sum_axis(weights, size, 0), size, 1)
        with np.errstate(invalid="ignore", divide="ignore"):
            return sums / np.where(counts > 0, counts, np.nan)

    rng = np.random.default_rng(9)
    data = rng.integers(0, 65535, size=(64, 48)).astype(np.float64)
    data[3, 3] = np.nan
    data[10, 20] = np.inf
    for size in (3, 5, 7):
        np.testing.assert_array_equal(box_mean(data, size), legacy(data, size))


def test_median_kernel_clamped_with_warning() -> None:
    import warnings

    from flir_player.processing import _MEDIAN_ELEMENT_BUDGET, median_filter

    data = np.random.default_rng(2).normal(size=(400, 300))  # 15x15 → 27M < budget
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no clamp warning within budget
        median_filter(data, 15)

    # find the size that first exceeds the budget on this frame
    size = 3
    while data.size * size * size <= _MEDIAN_ELEMENT_BUDGET:
        size += 2
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        median_filter(data, size)
    assert any("clamped" in str(item.message) for item in caught)


def test_median_spin_capped_by_frame_size(qapp) -> None:
    """R5: the spinner must not offer a kernel the processing layer would clamp."""
    from flir_player.processing import median_max_size

    assert median_max_size(1280 * 720) == 9
    assert median_max_size(640 * 480) >= 15

    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "1.ats")  # 1280×720
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        combo = window.inspector.spatial_combo
        spin = window.inspector.spatial_spin
        combo.setCurrentIndex(combo.findData("median"))
        assert spin.maximum() == 9
        spin.setValue(15)  # clamps to the cap and re-dispatches the state
        assert spin.value() == 9
        assert window._filters_state["spatial"] == ("median", 9)
        combo.setCurrentIndex(combo.findData("gaussian"))  # shared row: cap lifted
        assert spin.maximum() == 15
    finally:
        window.close()
        qapp.processEvents()
