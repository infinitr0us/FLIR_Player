# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only parser for the FLIR FFF "CameraInfo" record (SEQ, CSQ, FFF, JPG).

Handheld and A-series recordings store per frame an FFF record whose
CameraInfo block carries the factory calibration (Planck R1, R2, B, F, O),
the atmospheric-model constants, the range limits and the recorded object
parameters. The File SDK does not expose these, so they are read here.

Layout as documented by ExifTool (``FLIR.pm``) and verified on T650sc SEQ and
CSQ files: a 64-byte header ("FFF\\0", creator, version at 0x14 deciding the
header byte order, directory offset and entry count), 32-byte directory
entries (type 0x20 = CameraInfo), and a CameraInfo record whose first 16-bit
word is 2 in its own byte order. Only the start of the file is read.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .radiometry import AtmosphereConstants, Calibration, PlanckConstants, RangeLimits

_MAGICS = (b"FFF\0", b"AFF\0")
_SCAN_BYTES = 1 << 20
_CAMERA_INFO = 0x20

_FLOATS = {
    "emissivity": 0x20, "object_distance": 0x24, "reflected_k": 0x28,
    "atmosphere_k": 0x2C, "window_k": 0x30, "window_transmission": 0x34,
    "relative_humidity": 0x3C, "planck_r1": 0x58, "planck_b": 0x5C, "planck_f": 0x60,
    "atm_alpha1": 0x70, "atm_alpha2": 0x74, "atm_beta1": 0x78, "atm_beta2": 0x7C,
    "atm_x": 0x80, "range_max_k": 0x90, "range_min_k": 0x94, "max_clip_k": 0x98,
    "min_clip_k": 0x9C, "max_warn_k": 0xA0, "min_warn_k": 0xA4,
    "max_saturated_k": 0xA8, "min_saturated_k": 0xAC, "planck_r2": 0x30C,
}
_STRINGS = {
    "camera_model": (0xD4, 32), "camera_part_number": (0xF4, 16),
    "camera_serial": (0x104, 16), "camera_software": (0x114, 16),
    "lens_model": (0x170, 32), "lens_part_number": (0x190, 16),
    "lens_serial": (0x1A0, 16), "filter_model": (0x1EC, 16),
}
_INTS = {"planck_o": (0x308, "i"), "raw_min": (0x310, "H"), "raw_max": (0x312, "H")}
_RECORD_MIN = 0x314


@dataclass(frozen=True, slots=True)
class CameraInfo:
    """Decoded CameraInfo fields; temperatures in K, humidity 0–1."""

    fields: dict

    def __getitem__(self, key):
        return self.fields[key]

    def calibration(self) -> Calibration:
        f = self.fields
        planck = PlanckConstants(R=f["planck_r1"] / f["planck_r2"], B=f["planck_b"],
                                 F=f["planck_f"], O=float(f["planck_o"]))
        atmosphere = AtmosphereConstants(X=f["atm_x"], alpha1=f["atm_alpha1"],
                                         alpha2=f["atm_alpha2"], beta1=f["atm_beta1"],
                                         beta2=f["atm_beta2"])

        def pair(low, high):
            a, b = f.get(low), f.get(high)
            return (a, b) if a and b and 0 < a < b else None

        raw = (float(f["raw_min"]), float(f["raw_max"])) if 0 < f["raw_min"] < f["raw_max"] else None
        limits = RangeLimits(
            calibrated=pair("min_warn_k", "max_warn_k") or pair("range_min_k", "range_max_k"),
            clip=pair("min_clip_k", "max_clip_k"),
            saturated=pair("min_saturated_k", "max_saturated_k"),
            raw=raw,
        )
        return Calibration(planck=planck, atmosphere=atmosphere, limits=limits,
                           source="FFF CameraInfo record")


def _plausible(fields: dict) -> bool:
    try:
        return (0 < fields["emissivity"] <= 1.0 and 50 < fields["planck_b"] < 20000
                and 0 < fields["planck_f"] < 10 and fields["planck_r1"] > 0
                and fields["planck_r2"] > 0)
    except (KeyError, TypeError):
        return False


def _decode_record(blob: bytes, start: int, length: int) -> dict | None:
    if length < _RECORD_MIN or start < 0 or start + _RECORD_MIN > len(blob):
        return None
    for order in ("<", ">"):
        if struct.unpack_from(order + "H", blob, start)[0] != 2:
            continue
        fields: dict = {}
        for name, offset in _FLOATS.items():
            fields[name] = float(struct.unpack_from(order + "f", blob, start + offset)[0])
        for name, (offset, size) in _STRINGS.items():
            raw = blob[start + offset:start + offset + size].split(b"\0")[0]
            fields[name] = raw.decode("latin-1").strip()
        for name, (offset, code) in _INTS.items():
            fields[name] = struct.unpack_from(order + code, blob, start + offset)[0]
        if _plausible(fields):
            return fields
    return None


def _records(blob: bytes, magic_at: int):
    """(type, absolute offset, length) of each directory entry of one FFF block."""
    header = blob[magic_at:magic_at + 0x40]
    if len(header) < 0x40:
        return
    version = struct.unpack_from(">I", header, 0x14)[0]
    order = ">" if 100 <= version < 200 else "<"
    _version, directory, count = struct.unpack_from(order + "III", header, 0x14)
    if not 0 < count <= 256:
        return
    for index in range(count):
        entry = magic_at + directory + 0x20 * index
        if entry + 0x20 > len(blob):
            return
        kind, _sub, _ver, _id, offset, length = struct.unpack_from(order + "HHIIII", blob, entry)
        yield kind, magic_at + offset, length


def read_camera_info(path: str | Path, scan_bytes: int = _SCAN_BYTES) -> CameraInfo | None:
    """First CameraInfo record of an FFF-based recording, or None.

    Never raises for foreign or damaged files: an ATS/SFMOV recording, a
    truncated header or an implausible record all return None.
    """
    try:
        with open(path, "rb") as handle:
            blob = handle.read(scan_bytes)
    except OSError:
        return None
    position = 0
    for _ in range(8):  # the first few FFF blocks are enough
        candidates = [blob.find(magic, position) for magic in _MAGICS]
        candidates = [c for c in candidates if c >= 0]
        if not candidates:
            return None
        magic_at = min(candidates)
        try:
            for kind, start, length in _records(blob, magic_at):
                if kind == _CAMERA_INFO:
                    fields = _decode_record(blob, start, length)
                    if fields is not None:
                        return CameraInfo(fields)
        except struct.error:
            pass
        position = magic_at + 4
    return None
