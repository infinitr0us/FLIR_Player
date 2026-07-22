# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

import pytest

from flir_player.source import OBJECT_PARAMETER_FIELDS, FlirVideoSource


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
