# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Behaviour that must not depend on the FileSDK version (5.0.1 → 2026.1)."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from flir_player.jobs import extract_recording
from flir_player.sdktime import TimestampRepair

SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"


def _written(tmp_path, when: datetime) -> Path:
    path = tmp_path / "clip.ats"
    path.write_bytes(b"x")
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


def test_ats_placeholder_year_is_moved_into_the_recording_year(tmp_path) -> None:
    # FileSDK ≥ 2024.7: day 95 of the year in 1976 (a leap year) is 4 April
    repair = TimestampRepair(_written(tmp_path, datetime(2022, 4, 5, 11, 26, 43)))
    first = repair(datetime(1976, 4, 4, 11, 25, 22, 864903))
    last = repair(datetime(1976, 4, 4, 11, 26, 43, 798107))
    assert first == datetime(2022, 4, 5, 11, 25, 22, 864903) and repair.repaired
    assert (last - first).total_seconds() == pytest.approx(80.933204)
    # real years pass through unchanged
    assert TimestampRepair(None)(datetime(2026, 9, 3, 15, 51, 27)) == datetime(2026, 9, 3, 15, 51, 27)
    assert repair(None) is None


def test_recording_just_before_new_year_uses_the_previous_year(tmp_path) -> None:
    # written on 2 January, recorded on 31 December (day 365 = 30 Dec 1976)
    repair = TimestampRepair(_written(tmp_path, datetime(2023, 1, 2, 9, 0)))
    assert repair(datetime(1976, 12, 30, 23, 0)) == datetime(2022, 12, 31, 23, 0)


def test_extraction_progress_counts_completed_frames_for_any_sdk(tmp_path) -> None:
    class Options:
        progress_callback = None

    class FakeImager:
        def __init__(self, first):
            self.first = first

        def extract(self, dest, options):
            for current in range(self.first, self.first + 10):
                options.progress_callback(current, 10)
            Path(dest).write_bytes(b"ats")
            return True

    source = tmp_path / "source.ats"
    source.write_bytes(b"s")
    for first in (0, 1):  # 2024.7+ counts from 0, 5.0.1 from 1
        seen = []
        ok, message = extract_recording(FakeImager(first), source, tmp_path / f"out{first}.ats", Options(),
                                        progress=lambda done, total: seen.append(done))
        assert ok, message
        assert seen == list(range(1, 11))


def test_sdk_preload_never_runs_after_fnv_was_imported(tmp_path, monkeypatch) -> None:
    import sys

    import flir_player

    # Loading FileSDK 2026.1's DLLs after `import fnv` crashes the process.
    monkeypatch.setitem(sys.modules, "fnv", sys.modules.get("fnv") or object())

    def refuse(*_args, **_kwargs):
        raise AssertionError("DLLs must not be loaded once fnv is imported")

    monkeypatch.setattr(flir_player.ctypes, "WinDLL", refuse)
    log = tmp_path / "sdk.log"
    monkeypatch.setenv("FLIR_SDK_DEBUG", str(log))
    flir_player._preload_file_sdk()
    assert "skipped" in log.read_text(encoding="utf-8")


def test_player_shows_the_repaired_ats_date() -> None:
    import fnv.file

    from flir_player.source import FlirVideoSource

    raw = fnv.file.ImagerFile(str(SAMPLES / "1.ats"))
    raw.get_frame(0)
    sdk_year = raw.frame_info.time.year
    raw.close()
    source = FlirVideoSource()
    metadata = source.open(SAMPLES / "1.ats")
    try:
        written = datetime.fromtimestamp((SAMPLES / "1.ats").stat().st_mtime)
        if sdk_year == 1976:
            assert metadata.start_time.year == written.year
            assert abs((written - metadata.start_time).total_seconds()) < 2 * 86400
            assert any(label == "Recording date" for label, _ in metadata.source_details)
        else:  # FileSDK 5.0.1 invents the current year instead; left as it is
            assert metadata.start_time.year == sdk_year
        assert metadata.duration_seconds > 0
    finally:
        source.close()
