# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Absolute frame timestamps from the File SDK, with the ATS year repaired.

ATS/SFMOV recordings carry an IRIG-style clock: day of the year and time of
day, but no year. FileSDK 2024.7 and later place that clock in 1976, a leap
year, so every date after 28 February also comes out a day early. (FileSDK
5.0.1 used the current year instead.) Such stamps are moved into the year the
file was written, taken from its modification time, or the year before when
that would put the recording after the file was written. The shift is
constant, so intervals between frames stay exact. Stamps with a real year
(SEQ, CSQ) pass through unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

PLACEHOLDER_YEAR = 1976


class TimestampRepair:
    """Callable mapping one recording's SDK stamps onto calendar dates."""

    def __init__(self, path: str | Path | None) -> None:
        try:
            self._written = datetime.fromtimestamp(Path(path).stat().st_mtime) if path else None
        except OSError:
            self._written = None
        self._shift: timedelta | None = None
        self.repaired = False  # True once a placeholder-year stamp was moved

    def __call__(self, stamp):
        if not isinstance(stamp, datetime):
            return None
        if stamp.year != PLACEHOLDER_YEAR or self._written is None:
            return stamp
        if self._shift is None:
            base = datetime(PLACEHOLDER_YEAR, 1, 1, tzinfo=stamp.tzinfo)
            year = self._written.year
            shift = datetime(year, 1, 1, tzinfo=stamp.tzinfo) - base
            if (stamp + shift).replace(tzinfo=None) > self._written + timedelta(days=1):
                shift = datetime(year - 1, 1, 1, tzinfo=stamp.tzinfo) - base
            self._shift = shift
        self.repaired = True
        return stamp + self._shift
