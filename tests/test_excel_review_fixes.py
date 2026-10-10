# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the cross-review findings on the Excel export (no SDK)."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

openpyxl = pytest.importorskip("openpyxl")

from flir_player.excel_export import (  # noqa: E402
    ExportData,
    ExportOptions,
    _ClockSearch,
    fit_pixel_limit,
)
from flir_player.jobs import JobCancelled  # noqa: E402
from flir_player.workbook import write_workbook  # noqa: E402

from test_excel_workbook import PARAMS, _source  # noqa: E402


def _data(sources, rows=12, **options):
    return ExportData(times=np.arange(rows, dtype=float) - 2.0, sources=sources,
                      options=ExportOptions(bins=16, **options), created=datetime(2026, 9, 26),
                      versions={"tool": "test", "sdk": ""})


def test_second_recording_keeps_its_own_atmosphere(tmp_path) -> None:
    rng = np.random.default_rng(5)
    first, second = _source("Camera A", 12, rng), _source("Camera B", 12, rng)
    second.initial = second.initial.with_(atmosphere_k=333.15, humidity=0.8)
    path = tmp_path / "atm.xlsx"
    write_workbook(path, _data([first, second]))
    settings = openpyxl.load_workbook(path)["Settings"]
    row = next(r for r in range(18, 30) if settings.cell(r, 2).value == "Camera B")
    assert settings.cell(row, 7).value == pytest.approx(60.0)  # atmosphere override (°C)
    assert settings.cell(row, 8).value == pytest.approx(80.0)  # humidity override (%)
    assert settings.cell(row, 14).value.startswith("=IF(ISNUMBER(G")
    assert "O" in settings.cell(row, 16).value or settings.cell(row, 16).value.startswith("=IF(ISNUMBER(I")
    first_row = next(r for r in range(18, 30) if settings.cell(r, 2).value == "Camera A")
    assert settings.cell(first_row, 7).value is None and settings.cell(first_row, 8).value is None


def test_temperature_formulas_guard_the_curve_domain(tmp_path) -> None:
    path = tmp_path / "domain.xlsx"
    write_workbook(path, _data([_source("Camera A", 12, np.random.default_rng(1))]))
    data = openpyxl.load_workbook(path)["Data"]
    spot = data.cell(6, 3).value
    mean = data.cell(6, 5).value
    # the names are cited by their cells (workbook._direct): SMAX and the clamp counts live on Settings
    assert "<'Settings'!$" in spot and "MEDIAN('Settings'!$" in spot and ">0" in spot
    assert "<'Settings'!$" in mean and "MIN('Pixels'!" in mean and "MAX('Pixels'!" in mean


def test_tc_helpers_need_an_ir_sample_and_charts_use_the_display_unit(tmp_path) -> None:
    path = tmp_path / "tc.xlsx"
    write_workbook(path, _data([_source("Camera A", 12, np.random.default_rng(2))]))
    tc = openpyxl.load_workbook(path)["TC Compare"]
    headers = {tc.cell(9, c).value: c for c in range(13, tc.max_column + 1) if tc.cell(9, c).value}
    eps = tc.cell(10, headers["Matching emissivity"]).value
    assert eps.startswith("=IF(AND(ISNUMBER('Counts'!")
    assert "Matching ε (mean radiance)" in headers  # area ROI label
    assert "TC (display unit)" in headers
    tc_formula = tc.cell(10, headers["TC (°C)"]).value
    assert "INDEX('TC Compare'!$A$10" in tc_formula and "-'TC Compare'!$B$6," in tc_formula  # TC_OFFSET


def test_summary_windows_round_inwards_and_carry_real_results(tmp_path) -> None:
    path = tmp_path / "summary.xlsx"
    write_workbook(path, _data([_source("Camera A", 12, np.random.default_rng(3))]))
    summary = openpyxl.load_workbook(path)["Summary"]
    assert "(INDEX(TIME,MATCH(BASE_START,TIME,1))<BASE_START)" in summary["E4"].value
    assert summary["E5"].value == "=IFERROR(MATCH(BASE_END,TIME,1),0)"
    cached = openpyxl.load_workbook(path, data_only=True)
    peak = cached["Summary"].cell(14, 5).value
    data_values = [cached["Data"].cell(r, 3).value for r in range(6, 18)]
    assert peak == pytest.approx(max(v for v in data_values if isinstance(v, float)))
    assert cached["Summary"].cell(14, 8).value == "not reached" or isinstance(cached["Summary"].cell(14, 8).value, float)


def test_writing_honours_cancellation(tmp_path) -> None:
    with pytest.raises(JobCancelled):
        write_workbook(tmp_path / "cancel.xlsx", _data([_source("Camera A", 12, np.random.default_rng(4))]),
                       abort=lambda: True)


def test_pixel_limit_fits_excel_columns() -> None:
    options = ExportOptions(bins=128)
    assert fit_pixel_limit([400] * 10, options) == 400
    # 41 × 400-pixel ROIs overflow 16,384 columns: the largest ones are binned
    limit = fit_pixel_limit([400] * 41, options)
    assert limit < 400 and sum(n if n <= limit else 256 for n in [400] * 41) <= 16_383
    # small ROIs are never binned (bins would be wider)
    assert fit_pixel_limit([50] * 300, options) == 400
    with pytest.raises(ValueError):
        fit_pixel_limit([5000] * 70, options)


class _FakeClock:
    """ImagerFile stand-in with scripted timestamps (seconds)."""

    def __init__(self, seconds):
        self._seconds = list(seconds)
        self._index = 0
        self.reads = 0

    def get_frame(self, index):
        self._index = int(index)
        self.reads += 1

    @property
    def frame_info(self):
        base = datetime(2026, 9, 3, 12, 0, 0)

        class Info:
            time = base + timedelta(seconds=self._seconds[self._index])
        return Info


def test_clock_search_is_bracketed_and_uses_real_coverage() -> None:
    base = datetime(2026, 9, 3, 12, 0, 0)
    # a one-second clock jump at frame 500 (30 fps)
    seconds = [i / 30 for i in range(500)] + [i / 30 + 1 for i in range(500, 1000)]
    im = _FakeClock(seconds)
    search = _ClockSearch(im, 1000, base)
    assert search.nearest(17.0, guess=17 * 30, tolerance=1 / 60) == 499
    assert search.nearest(-1.0, guess=0, tolerance=1 / 60) == -1
    # gaps: coverage comes from the timestamps, relative to the ignition stamp
    im = _FakeClock([0, 1, 2, 10, 11, 12])
    search = _ClockSearch(im, 6, base + timedelta(seconds=10))
    assert search.span() == (-10.0, 2.0)
    assert search.nearest(-10.0, guess=0, tolerance=0.5) == 0
    assert search.nearest(4.0, guess=5, tolerance=0.5) == -1
    assert search.nearest(-4.0, guess=2, tolerance=0.5) == 2  # 2 s is nearer than 10 s
