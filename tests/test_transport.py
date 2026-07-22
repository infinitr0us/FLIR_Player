# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import wait_until

from PySide6.QtCore import QSettings, Qt

from flir_player.extract import ExtractDialog
from flir_player.main_window import MainWindow
from flir_player.models import VideoMetadata
from flir_player.widgets import TimelineSlider

SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"


# --- TimelineSlider model ----------------------------------------------------------


def test_play_range_set_normalize_clear(qapp) -> None:
    slider = TimelineSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 99)
    try:
        assert slider.play_range() is None
        slider.set_play_range(30, 10)  # swapped on purpose
        assert slider.play_range() == (10, 30)
        slider.set_play_range(-5, 250)  # clamped into the slider range
        assert slider.play_range() == (0, 99)
        slider.clear_play_range()
        assert slider.play_range() is None
    finally:
        slider.close()


def test_play_range_emits_and_marker_hit(qapp) -> None:
    slider = TimelineSlider(Qt.Orientation.Horizontal)
    slider.resize(400, 24)
    slider.show()
    slider.setRange(0, 100)
    emissions: list[tuple[int, int]] = []
    slider.range_changed.connect(lambda s, e: emissions.append((s, e)))
    try:
        slider.set_play_range(20, 60)
        assert emissions == [(20, 60)]
        x_start = slider._value_to_x(20)
        assert slider._marker_at(x_start) == "start"
        assert slider._marker_at(slider._value_to_x(60)) == "end"
        assert slider._marker_at(slider._value_to_x(40)) is None
        # marker value mapping round-trips
        assert slider._x_to_value(x_start) == 20
    finally:
        slider.close()


def test_extract_dialog_seeds_from_play_range(qapp) -> None:
    metadata = VideoMetadata(
        path=Path("clip.seq"),
        width=2,
        height=2,
        num_frames=100,
        start_time=None,
        end_time=None,
        duration_seconds=10.0,
        nominal_fps=10.0,
    )
    dialog = ExtractDialog(metadata, None, default_range=(9, 19))
    try:
        assert dialog.start_spin.value() == 10
        assert dialog.end_spin.value() == 20
    finally:
        dialog.close()


# --- MainWindow loop / range behavior ----------------------------------------------------


@pytest.fixture()
def loop_setting_preserved():
    settings = QSettings("Local", "FLIR Thermal Player")
    previous = settings.value("playback/loop", False, type=bool)
    settings.setValue("playback/loop", False)  # deterministic starting state
    yield
    settings.setValue("playback/loop", previous)


def _loaded_window(qapp) -> MainWindow:
    window = MainWindow()
    window.show()
    window.open_path(SAMPLES / "2.seq")
    assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
    return window


def test_playback_bounds_follow_play_range(qapp, loop_setting_preserved) -> None:
    window = _loaded_window(qapp)
    try:
        last = window.metadata.num_frames - 1
        assert window._playback_bounds() == (0, last)
        window.transport.slider.set_play_range(5, 25)
        assert window._playback_bounds() == (5, 25)
        window._clear_play_range()
        assert window._playback_bounds() == (0, last)
    finally:
        window.close()
        qapp.processEvents()


def test_loop_wrap_requests_range_start(qapp, loop_setting_preserved) -> None:
    window = _loaded_window(qapp)
    try:
        window._change_loop(True)
        window.transport.slider.set_play_range(10, 20)
        window.seek_to(20)
        assert wait_until(qapp, lambda: window.current_packet.index == 20)

        requested: list[int] = []
        original = window.decoder.request_frame
        window.decoder.request_frame = lambda index, rid: requested.append(index)
        window.playing = True
        try:
            window._request_next_playback_frame()
        finally:
            window.decoder.request_frame = original
            window.playing = False
        assert requested == [10]
        assert window._wrap_pending
        window._wrap_pending = False
    finally:
        window.close()
        qapp.processEvents()


def test_no_loop_pauses_at_range_end(qapp, loop_setting_preserved) -> None:
    window = _loaded_window(qapp)
    try:
        window._change_loop(False)
        window.transport.slider.set_play_range(10, 20)
        window.seek_to(20)
        assert wait_until(qapp, lambda: window.current_packet.index == 20)
        window.playing = True
        window._request_next_playback_frame()
        assert not window.playing
        assert not window._wrap_pending
    finally:
        window.close()
        qapp.processEvents()


def test_toggle_playback_at_range_end_restarts_at_range_start(qapp, loop_setting_preserved) -> None:
    window = _loaded_window(qapp)
    try:
        window._change_loop(False)
        window.transport.slider.set_play_range(10, 20)
        window.seek_to(20)
        assert wait_until(qapp, lambda: window.current_packet.index == 20)
        window.toggle_playback()
        assert wait_until(
            qapp,
            lambda: window.playing and window.current_packet is not None
            and window.current_packet.index >= 10,
        )
    finally:
        window.close()
        qapp.processEvents()


def test_loop_setting_persists(qapp, loop_setting_preserved) -> None:
    window = _loaded_window(qapp)
    try:
        assert not window.loop_playback
        window.transport.loop_button.setChecked(True)
        assert window.loop_playback
        assert QSettings("Local", "FLIR Thermal Player").value(
            "playback/loop", False, type=bool
        )
    finally:
        window.close()
        qapp.processEvents()
