# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Absolute frame timestamps from the File SDK, with the ATS year repaired.

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


def _place(stamp: datetime, year: int) -> datetime:
    """``stamp``'s day of the year and time of day in ``year`` (1977 = the year after)."""
    start = datetime(stamp.year, 1, 1, tzinfo=stamp.tzinfo)
    return datetime(year + stamp.year - PLACEHOLDER_YEAR, 1, 1, tzinfo=stamp.tzinfo) + (stamp - start)
