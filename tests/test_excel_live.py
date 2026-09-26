# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Workbook formulas evaluated by Microsoft Excel itself (opt-in, local only).

Needs Excel and pywin32; run with ``FLIR_EXCEL_LIVE=1``. Skipped otherwise
(and always on CI, which has no Excel).
"""
from __future__ import annotations

import os
import time
from datetime import datetime

import numpy as np
import pytest

if os.environ.get("FLIR_EXCEL_LIVE") != "1":
    pytest.skip("set FLIR_EXCEL_LIVE=1 to run tests in Microsoft Excel", allow_module_level=True)
com_client = pytest.importorskip("win32com.client")
openpyxl = pytest.importorskip("openpyxl")

from flir_player.excel_export import ExportData, ExportOptions  # noqa: E402
from flir_player.radiometry import object_temperature  # noqa: E402
from flir_player.workbook import write_workbook  # noqa: E402

from test_excel_workbook import _source  # noqa: E402
from test_radiometry import T650  # noqa: E402


def _com(fn, *args):
    import pywintypes
    for _ in range(600):
        try:
            return fn(*args)
        except pywintypes.com_error as exc:
            if exc.args[0] not in (-2147418111, -2147417846):
                raise
            time.sleep(0.1)
    raise TimeoutError("Excel stayed busy")


@pytest.fixture(scope="module")
def excel():
    app = com_client.DispatchEx("Excel.Application")
    app.Visible = False
    app.DisplayAlerts = False
    yield app
    app.Quit()


def _book(tmp_path, sources, rows=14):
    path = tmp_path / "live.xlsx"
    data = ExportData(times=np.arange(rows, dtype=float) - 2.0, sources=sources,
                      options=ExportOptions(bins=16), created=datetime(2026, 9, 26),
                      versions={"tool": "test", "sdk": ""})
    write_workbook(path, data)
    return path, data


def _open(excel, path):
    book = _com(excel.Workbooks.Open, str(path.resolve()))
    _com(excel.CalculateFull)
    return book


def test_invalid_parameters_give_na_not_nonsense(excel, tmp_path) -> None:
    path, _ = _book(tmp_path, [_source("Camera A", 14, np.random.default_rng(1))])
    book = _open(excel, path)
    try:
        settings, data = book.Worksheets("Settings"), book.Worksheets("Data")
        settings.Range("B9").Value = 0.01   # emissivity
        settings.Range("B10").Value = 100.0  # reflected temperature (°C)
        _com(excel.Calculate)
        values = [data.Cells(r, c).Value for r in range(6, 18) for c in (3, 5, 6, 7)]
        numbers = [v for v in values if isinstance(v, float)]
        assert all(v > -273.15 for v in numbers)
        # clamping works on counts, so spots and pixel means agree on the clip range
        settings.Range("B5").Value = "Clamp like FLIR software"
        settings.Range("B9").Value = 0.1
        _com(excel.Calculate)
        clamped = [data.Cells(r, c).Value for r in range(6, 16) for c in (3, 5)]
        assert all(isinstance(v, float) and -0.01 <= v <= 660.01 for v in clamped), clamped
    finally:
        book.Close(SaveChanges=False)


def test_tc_interpolation_handles_exact_and_last_samples(excel, tmp_path) -> None:
    path, _ = _book(tmp_path, [_source("Camera A", 14, np.random.default_rng(2))])
    book = _open(excel, path)
    try:
        settings, tc = book.Worksheets("Settings"), book.Worksheets("TC Compare")
        tc.Range("A10:B11").Value = ((0.0, 100.0), (10.0, 110.0))
        roi_row = next(r for r in range(20, 40) if settings.Cells(r, 3).Value == "TC1")
        settings.Cells(roi_row, 8).Value = "TC 1"
        _com(excel.Calculate)
        column = next(c for c in range(13, 40) if tc.Cells(9, c).Value == "TC (°C)")
        by_time = {tc.Cells(r, 13).Value: tc.Cells(r, column).Value for r in range(10, 24)}
        assert by_time[0.0] == pytest.approx(100.0)
        assert by_time[5.0] == pytest.approx(105.0)
        assert by_time[10.0] == pytest.approx(110.0)  # exact last sample
        assert not isinstance(by_time[11.0], float) and not isinstance(by_time[-1.0], float)
        # the chart helper follows the display unit
        settings.Range("B4").Value = "K"
        _com(excel.Calculate)
        helper = next(c for c in range(13, 40) if tc.Cells(9, c).Value == "TC (display unit)")
        row5 = next(r for r in range(10, 24) if tc.Cells(r, 13).Value == 5.0)
        assert tc.Cells(row5, helper).Value == pytest.approx(378.15)
    finally:
        book.Close(SaveChanges=False)


def test_summary_window_uses_samples_inside_it(excel, tmp_path) -> None:
    path, data = _book(tmp_path, [_source("Camera A", 14, np.random.default_rng(3))])
    book = _open(excel, path)
    try:
        summary, sheet = book.Worksheets("Summary"), book.Worksheets("Data")
        summary.Range("B4").Value = 0.5
        summary.Range("B5").Value = 3.5
        _com(excel.Calculate)
        inside = [sheet.Cells(r, 3).Value for r in range(6, 20) if 0.5 <= sheet.Cells(r, 1).Value <= 3.5]
        assert len(inside) == 3
        assert summary.Cells(14, 4).Value == pytest.approx(np.mean(inside))
        summary.Range("B4").Value = 100.0  # an empty window
        summary.Range("B5").Value = 200.0
        _com(excel.Calculate)
        assert summary.Cells(14, 4).Value == "–"
    finally:
        book.Close(SaveChanges=False)


def test_recalculation_keeps_every_recordings_own_parameters(excel, tmp_path) -> None:
    rng = np.random.default_rng(4)
    first, second = _source("Camera A", 14, rng), _source("Camera B", 14, rng)
    second.initial = second.initial.with_(atmosphere_k=333.15, humidity=0.8, emissivity=0.85,
                                          transmission=0.8, reflected_k=313.15)
    path, _ = _book(tmp_path, [first, second])
    cached = openpyxl.load_workbook(path, data_only=True)["Data"]
    before = [[cached.cell(r, c).value for c in range(3, 20)] for r in range(6, 18)]
    book = _open(excel, path)
    try:
        data = book.Worksheets("Data")
        after = [[data.Cells(r, c).Value for c in range(3, 20)] for r in range(6, 18)]
        for row_before, row_after in zip(before, after):
            for b, a in zip(row_before, row_after):
                if isinstance(b, float):
                    assert a == pytest.approx(b, abs=1e-6)
        # and the second camera's spot really uses its own atmosphere
        roi = second.rois[0]
        expected = object_temperature(T650, second.initial, roi.counts["value"][0]) - 273.15
        column = next(c for c in range(3, 40) if str(data.Cells(4, c).MergeArea.Cells(1, 1).Value)
                      .startswith("Camera B · TC1"))
        assert data.Cells(6, column).Value == pytest.approx(float(expected), abs=1e-6)
    finally:
        book.Close(SaveChanges=False)
