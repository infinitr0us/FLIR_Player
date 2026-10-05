# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Frame timestamps from the File SDK: the ATS year repair and the frame rate.

``frame_rate`` decides how frame numbers map to seconds, including recordings
whose timestamps run slow (ResearchIR A700 SEQ files).

ATS/SFMOV recordings carry an IRIG-style clock: day of the year and time of
day, but no year. FileSDK 2024.7 and later place that clock in 1976, a leap
year, so every date after 28 February also comes out a day early. (FileSDK
5.0.1 used the current year instead.) Such stamps keep their day of the year
and time of day but move into the year the file was written, taken from its
modification time, or the year before when that would put the recording after
the file was written. The year is decided once per recording, so intervals
between frames stay exact.

A recording that runs over New Year has its day counter roll back to day 1
(dated 1976 again, or 1977). A recording lasts far less than half a year, so
a stamp more than half a year before the first one seen belongs to the
following year (and one more than half a year after it, to the year before).
Stamps with a real year (SEQ, CSQ) pass through unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import FrameRate

PLACEHOLDER_YEAR = 1976
_ROLLOVER_YEAR = PLACEHOLDER_YEAR + 1  # a day counter past day 366
_HALF_YEAR = timedelta(days=183)


class TimestampRepair:
    """Callable mapping one recording's SDK stamps onto calendar dates."""

    def __init__(self, path: str | Path | None) -> None:
        try:
            self._written = datetime.fromtimestamp(Path(path).stat().st_mtime) if path else None
        except OSError:
            self._written = None
        self._year: int | None = None  # calendar year of the placeholder year
        self._anchor: datetime | None = None  # first repaired stamp (naive)
        self.repaired = False  # True once a placeholder-year stamp was moved

    def __call__(self, stamp):
        if not isinstance(stamp, datetime):
            return None
        if stamp.year not in (PLACEHOLDER_YEAR, _ROLLOVER_YEAR) or self._written is None:
            return stamp
        if self._year is None:
            year = self._written.year
            if _place(stamp, year).replace(tzinfo=None) > self._written + timedelta(days=1):
                year -= 1
            self._year = year
            self._anchor = _place(stamp, year).replace(tzinfo=None)
        result = _place(stamp, self._year)
        offset = result.replace(tzinfo=None) - self._anchor
        if offset < -_HALF_YEAR:
            result = _place(stamp, self._year + 1)
        elif offset > _HALF_YEAR:
            result = _place(stamp, self._year - 1)
        self.repaired = True
        return result


def preset_frame_rate(im: Any) -> float:
    """Camera rate of the recording's active preset, or 0.0 when unknown.

    Only presets that are both available and flag ``frame_rate_valid`` are
    trusted: the SDK leaves a placeholder 1.0/30.0 in ``frame_rate`` otherwise,
    and a made-up base rate would produce a made-up dropped-frame count.
    """
    info = getattr(im, "source_info", None)
    for preset in getattr(info, "preset_info", ()) or ():
        if getattr(preset, "available", False) and getattr(preset, "frame_rate_valid", False):
            try:
                rate = float(preset.frame_rate)
            except (TypeError, ValueError):
                continue
            if rate > 0:
                return rate
    return 0.0


# Camera rates (and their usual decimations) a slow clock is snapped to.
CAMERA_RATES = (60.0, 50.0, 30.0, 25.0, 20.0, 15.0, 10.0, 9.0, 7.5, 6.0, 5.0, 3.75, 3.0,
                2.5, 2.0, 1.0)
# Stored frames exceeding the clock span by this fraction: the stamps run slow.
# Dropped frames only ever push the clock rate below the camera rate, and
# 0.5 % stays clear of clock corrections (FLIR2229: 1 s jumps, 0.01 % overall).
SLOW_CLOCK = (0.005, 0.03)


def frame_rate(stored_frames: int, span_seconds: float, preset_fps: float = 0.0) -> FrameRate:
    """The rate for frame-number time, from the stored frames and their clock span.

    Normally the clock's own average rate. ResearchIR's A700 recordings stamp
    their 30 Hz frames 32.8 ms apart, so their clock implies 30.48 fps and runs
    1.6 % slow against the camera (and against a TC logger and a second
    camera). When no trusted preset rate exists and the clock rate sits
    0.5–3 % above a camera rate, that camera rate is used instead.
    """
    if stored_frames < 2 or not span_seconds > 0:
        return FrameRate(30.0)
    clock = (stored_frames - 1) / span_seconds
    if preset_fps <= 0:
        low, high = SLOW_CLOCK
        for rate in CAMERA_RATES:
            if 1 + low < clock / rate <= 1 + high:
                return FrameRate(rate, clock, rate)
    return FrameRate(clock, clock)


def _place(stamp: datetime, year: int) -> datetime:
    """``stamp``'s day of the year and time of day in ``year`` (1977 = the year after)."""
    start = datetime(stamp.year, 1, 1, tzinfo=stamp.tzinfo)
    return datetime(year + stamp.year - PLACEHOLDER_YEAR, 1, 1, tzinfo=stamp.tzinfo) + (stamp - start)
