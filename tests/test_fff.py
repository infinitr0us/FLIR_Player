# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""FFF CameraInfo parser: synthetic records and the sample recordings."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from flir_player.fff import read_camera_info

SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"


def fff_blob(header_order: str = ">", record_order: str = "<", *, planck_b: float = 1403.5,
             prefix: bytes = b"") -> bytes:
    """A minimal FFF block: header, one directory entry, one CameraInfo record."""
    record = bytearray(0x400)
    struct.pack_into(record_order + "H", record, 0, 2)
    floats = {0x20: 0.95, 0x24: 1.0, 0x28: 293.15, 0x2C: 293.15, 0x30: 293.15, 0x34: 1.0,
              0x3C: 0.5, 0x58: 15391.119141, 0x5C: planck_b, 0x60: 1.6, 0x70: 0.006569,
              0x74: 0.01262, 0x78: -0.002276, 0x7C: -0.00667, 0x80: 1.9, 0x90: 923.15,
              0x94: 373.15, 0x98: 933.15, 0x9C: 273.15, 0xA0: 923.15, 0xA4: 373.15,
              0xA8: 943.15, 0xAC: 213.15, 0x30C: 0.126859}
    for offset, value in floats.items():
        struct.pack_into(record_order + "f", record, offset, value)
    record[0xD4:0xD4 + 11] = b"FLIR T650sc"
    record[0x104:0x104 + 8] = b"55908418"
    struct.pack_into(record_order + "i", record, 0x308, -5781)
    struct.pack_into(record_order + "HH", record, 0x310, 5949, 48672)
    header = bytearray(0x40)
    header[0:4] = b"FFF\0"
    version = 101 if header_order == ">" else 100
    struct.pack_into(header_order + "III", header, 0x14, version, 0x40, 1)
    entry = struct.pack(header_order + "HHIIII", 0x20, 0, 100, 1, 0x60, len(record)) + bytes(12)
    return prefix + bytes(header) + entry + bytes(record)


@pytest.mark.parametrize("header_order, record_order", [(">", "<"), ("<", "<"), (">", ">")])
def test_parses_a_synthetic_record(tmp_path, header_order, record_order) -> None:
    path = tmp_path / "frame.csq"
    path.write_bytes(fff_blob(header_order, record_order, prefix=b"\0" * 37))
    info = read_camera_info(path)
    assert info is not None
    assert info["camera_model"] == "FLIR T650sc" and info["camera_serial"] == "55908418"
    cal = info.calibration()
    assert cal.planck.R == pytest.approx(15391.119141 / 0.126859, rel=1e-6)
    assert (cal.planck.B, cal.planck.O) == (1403.5, -5781.0)
    assert cal.limits.calibrated == pytest.approx((373.15, 923.15))
    assert cal.limits.clip == pytest.approx((273.15, 933.15))
    assert cal.limits.raw == (5949.0, 48672.0)
    assert cal.atmosphere.X == pytest.approx(1.9)


def test_foreign_and_damaged_files_return_none(tmp_path) -> None:
    ats = tmp_path / "a.ats"
    ats.write_bytes(b"FLIR ATS-US File" + bytes(4096))
    truncated = tmp_path / "t.seq"
    truncated.write_bytes(fff_blob()[:0x70])
    implausible = tmp_path / "i.seq"
    implausible.write_bytes(fff_blob(planck_b=-3.0))
    garbage = tmp_path / "g.seq"
    garbage.write_bytes(b"FFF\0" + bytes(np.random.default_rng(1).integers(0, 255, 5000, dtype=np.uint8)))
    for path in (ats, truncated, implausible, garbage, tmp_path / "missing.seq"):
        assert read_camera_info(path) is None


def test_sample_seq_and_csq_carry_the_t650sc_calibration() -> None:
    for name in ("2.seq", "3.csq"):
        info = read_camera_info(SAMPLES / name)
        assert info is not None
        cal = info.calibration()
        assert cal.planck.B == 1403.5 and cal.planck.O == -5781.0
        assert cal.planck.R == pytest.approx(121325.0, abs=0.01)
        assert info["camera_model"] == "FLIR T650sc"
    assert read_camera_info(SAMPLES / "1.ats") is None
