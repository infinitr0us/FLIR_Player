# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

import pytest

from flir_player.source import (
    OBJECT_PARAMETER_FIELDS,
    FlirVideoSource,
    _base_frame_rate,
    _cadence_info,
    _cadence_rows,
)


SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"


@pytest.mark.parametrize(
    ("filename", "shape", "frames"),
    (("1.ats", (720, 1280), 876), ("2.seq", (480, 640), 2685)),
)
def test_supplied_recordings_open_and_decode(filename, shape, frames) -> None:
    source = FlirVideoSource()
    try:
        metadata = source.open(SAMPLES / filename)
        packet = source.read_frame(0, request_id=42)
        assert metadata.num_frames == frames
        assert packet.data.shape == shape
        assert packet.request_id == 42
        assert packet.minimum < packet.maximum
        assert packet.timestamp is not None
        assert source.available_units
    finally:
        source.close()


def test_temperature_switch_when_supported() -> None:
    source = FlirVideoSource()
    try:
        source.open(SAMPLES / "2.seq")
        keys = {option.key for option in source.available_units}
        assert "temperature_factory_c" in keys
        source.set_unit("temperature_factory_c")
        packet = source.read_frame(10)
        assert packet.unit.suffix == "°C"
        assert packet.maximum < 500.0
    finally:
        source.close()


def test_extended_units_object_signal_and_fahrenheit() -> None:
    source = FlirVideoSource()
    try:
        source.open(SAMPLES / "2.seq")
        keys = {option.key for option in source.available_units}
        assert "object_signal" in keys
        assert "temperature_factory_f" in keys
        assert "temperature_factory_r" in keys

        source.set_unit("temperature_factory_c")
        celsius = source.read_frame(10)
        source.set_unit("temperature_factory_f")
        fahrenheit = source.read_frame(10)
        assert fahrenheit.unit.suffix == "°F"
        assert fahrenheit.mean == pytest.approx(celsius.mean * 9.0 / 5.0 + 32.0, rel=1e-3)

        source.set_unit("object_signal")
        signal = source.read_frame(10)
        assert signal.unit.suffix == ""
        assert signal.maximum > signal.minimum
    finally:
        source.close()


def test_ats_exposes_counts_only() -> None:
    source = FlirVideoSource()
    try:
        source.open(SAMPLES / "1.ats")
        assert [option.key for option in source.available_units] == ["counts"]
    finally:
        source.close()


def test_object_parameters_apply_and_reset() -> None:
    source = FlirVideoSource()
    try:
        source.open(SAMPLES / "2.seq")
        source.set_unit("temperature_factory_c")
        baseline = source.read_frame(10).mean

        snapshot = source.read_object_parameters()
        for field in OBJECT_PARAMETER_FIELDS:
            assert field in snapshot
        assert "can_change" in snapshot

        edited = dict(snapshot)
        edited["emissivity"] = 0.5
        applied = source.apply_object_parameters(edited)
        assert applied["emissivity"] == pytest.approx(0.5)
        assert source.read_frame(10).mean != pytest.approx(baseline)

        restored = source.apply_object_parameters(None)
        assert restored["emissivity"] == pytest.approx(snapshot["emissivity"])
        assert source.read_frame(10).mean == pytest.approx(baseline)
    finally:
        source.close()


# --- capture cadence ---------------------------------------------------------------


def test_ats_sample_reports_uneven_capture_cadence() -> None:
    """1.ats stored ~60 % of a 60.93 Hz capture grid (frames dropped at capture).

    The numbers are checked against an independent full-file timestamp sweep
    (local/notes/2026-07-28-ats-playback-jitter): every recorded interval is an
    exact multiple of 1/60.9328 s, spanning 1463 grid slots for 876 frames.
    """
    source = FlirVideoSource()
    try:
        metadata = source.open(SAMPLES / "1.ats")
        cadence = metadata.cadence
        assert cadence is not None
        assert cadence.base_fps == pytest.approx(60.9328, abs=1e-3)
        assert cadence.stored_frames == 876
        assert cadence.expected_frames == 1463
        assert cadence.missing_frames == 587
        assert cadence.kept_fraction == pytest.approx(0.5988, abs=1e-3)
        assert not cadence.is_even
        details = dict(metadata.source_details)
        assert "60.93 Hz camera rate" in details["Frame rate"]
        assert details["Frame cadence"].startswith("Uneven")
        assert "876 of 1463" in details["Frame cadence"]
    finally:
        source.close()


def test_seq_sample_reports_no_cadence_without_a_valid_base_rate() -> None:
    """2.seq's preset does not flag frame_rate_valid, so no grid is claimed.

    A missing base rate must read as "unknown", not as "even" — inventing one
    from the SDK's placeholder would invent a dropped-frame count with it.
    """
    source = FlirVideoSource()
    try:
        metadata = source.open(SAMPLES / "2.seq")
        assert metadata.cadence is None
        details = dict(metadata.source_details)
        assert details["Frame rate"] == "29.98 fps"
        assert "Frame cadence" not in details
    finally:
        source.close()


# --- cadence derivation (pure logic; no SDK or sample recording needed) -------------


class _FakePreset:
    def __init__(self, available: bool, valid: bool, rate: float) -> None:
        self.available = available
        self.frame_rate_valid = valid
        self.frame_rate = rate


class _FakeImager:
    def __init__(self, *presets) -> None:
        self.source_info = type("Info", (), {"preset_info": presets})()


def test_base_rate_ignores_unavailable_and_invalid_presets() -> None:
    imager = _FakeImager(
        _FakePreset(available=False, valid=True, rate=200.0),  # not the active preset
        _FakePreset(available=True, valid=False, rate=30.0),  # SDK placeholder value
        _FakePreset(available=True, valid=True, rate=60.0),
    )
    assert _base_frame_rate(imager) == pytest.approx(60.0)
    assert _base_frame_rate(_FakeImager(_FakePreset(True, False, 30.0))) == 0.0
    assert _base_frame_rate(_FakeImager()) == 0.0


@pytest.mark.parametrize(
    ("span", "stored", "reason"),
    (
        (0.0, 100, "zero span carries no cadence information"),
        (10.0, 1, "a single frame spans no interval"),
        (10.0, 900, "more frames stored than the base rate implies"),
    ),
)
def test_cadence_is_none_when_it_cannot_be_derived(span, stored, reason) -> None:
    imager = _FakeImager(_FakePreset(available=True, valid=True, rate=60.0))
    assert _cadence_info(imager, span, stored) is None, reason


def test_cadence_counts_grid_slots_inclusively() -> None:
    imager = _FakeImager(_FakePreset(available=True, valid=True, rate=60.0))
    cadence = _cadence_info(imager, 10.0, 301)
    assert cadence is not None
    assert cadence.expected_frames == 601  # 600 intervals -> 601 slots
    assert cadence.stored_frames == 301
    assert cadence.missing_frames == 300
    assert cadence.kept_fraction == pytest.approx(301 / 601)
    assert not cadence.is_even


def test_near_complete_cadence_counts_as_even() -> None:
    imager = _FakeImager(_FakePreset(available=True, valid=True, rate=60.0))
    cadence = _cadence_info(imager, 10.0, 600)  # one slot short of 601
    assert cadence is not None and cadence.is_even
    assert dict(_cadence_rows(cadence, 59.9))["Frame cadence"] == "Even"
    # an even cadence reports one plain rate, not an average/base pair
    assert dict(_cadence_rows(cadence, 59.9))["Frame rate"] == "59.90 fps"


def test_cadence_rows_omit_unknown_values() -> None:
    assert _cadence_rows(None, 0.0) == ()
    assert dict(_cadence_rows(None, 30.0)) == {"Frame rate": "30.00 fps"}
