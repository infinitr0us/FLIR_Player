# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Emissivity Match sheet evaluated by Microsoft Excel itself (opt-in, local only).

Needs Excel and pywin32; run with ``FLIR_EXCEL_LIVE=1``. Excel's results must equal the Python
reference cached in the file, and edits (emissivity, limits, Use) must move them as expected.
"""
from __future__ import annotations

import os
import time

import numpy as np
import pytest

if os.environ.get("FLIR_EXCEL_LIVE") != "1":
    pytest.skip("set FLIR_EXCEL_LIVE=1 to run tests in Microsoft Excel", allow_module_level=True)
com_client = pytest.importorskip("win32com.client")
openpyxl = pytest.importorskip("openpyxl")

from test_emissivity_match import TRUE_EPS, _prefill, _write, _zone_source  # noqa: E402

XL_NA = -2146826246


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


def _open(excel, path):
    book = _com(excel.Workbooks.Open, str(path.resolve()))
    _com(excel.CalculateFull)
    return book


def _same(excel_value, cached, rel=1e-7):
    if isinstance(cached, (int, float)) and not isinstance(cached, bool):
        assert isinstance(excel_value, float), (excel_value, cached)
        assert excel_value == pytest.approx(cached, rel=rel, abs=1e-9)
    elif cached in ("#N/A", None, ""):
        assert excel_value in (XL_NA, None, ""), (excel_value, cached)
    else:
        assert excel_value == cached


def _compare_sheet(book, path, rows, cols):
    """Every Match cell in rows × cols: Excel's value against the cached (Python) one."""
    cached = openpyxl.load_workbook(path, data_only=True)["Emissivity Match"]
    sheet = book.Worksheets("Emissivity Match")
    for r in rows:
        for c in cols:
            _same(sheet.Cells(r, c).Value, cached.cell(r, c).value)


def test_excel_reproduces_the_python_match(excel, tmp_path) -> None:
    source, temps = _zone_source()
    prefill = _prefill(temps, roi_use=(("Cell 3", ((25.0, 55.0),)),))
    path = _write(tmp_path, source, prefill)
    book = _open(excel, path)
    try:
        # result block, notes, pairs table (2 ROIs) and the candidate table (2 ROIs + all)
        _compare_sheet(book, path, range(6, 9), (7,))
        _compare_sheet(book, path, (9, 10), (4,))
        _compare_sheet(book, path, range(13, 15), range(2, 11))
        _compare_sheet(book, path, range(13, 13 + 41), range(12, 16))
        sheet = book.Worksheets("Emissivity Match")
        assert sheet.Range("G6").Value == pytest.approx(TRUE_EPS)
        # one emissivity on Settings for every zone: at the true value the error is gone
        settings = book.Worksheets("Settings")
        settings.Range("B9").Value = TRUE_EPS
        _com(excel.Calculate)
        assert sheet.Range("G8").Value < 0.01
        assert sheet.Range("G13").Value < 0.01 and sheet.Range("G14").Value < 0.01
        # coarser limits: the nearest candidate wins
        sheet.Range("B6").Value = 0.5
        sheet.Range("B7").Value = 1.0
        sheet.Range("B8").Value = 0.05
        _com(excel.Calculate)
        eps = [sheet.Cells(13 + j, 12).Value for j in range(11)]
        assert eps == pytest.approx([0.5 + 0.05 * j for j in range(11)])
        assert sheet.Cells(13 + 11, 12).Value == XL_NA
        assert sheet.Range("G6").Value == pytest.approx(0.95)
        # Use = 0 everywhere for Cell 6: no rows, no errors; Cell 3 alone decides
        tc = book.Worksheets("TC Compare")
        heads = [tc.Cells(9, c).Value for c in range(1, 60)]
        use6 = [c + 1 for c, v in enumerate(heads) if v == "Use (1/0)"][1]
        tc.Range(tc.Cells(10, use6), tc.Cells(69, use6)).Value = 0
        _com(excel.Calculate)
        assert sheet.Range("E14").Value == 0 and sheet.Range("G14").Value == "–"
        assert sheet.Range("G6").Value == pytest.approx(0.95)
        # a narrower window drops rows of Cell 3
        rows_before = sheet.Range("E13").Value
        sheet.Range("B9").Value = 40.0
        _com(excel.Calculate)
        assert 0 < sheet.Range("E13").Value < rows_before
    finally:
        book.Close(False)


@pytest.mark.parametrize("offset_k, text", [(40.0, "At the lowest"), (-40.0, "At the highest")])
def test_excel_notes_for_values_at_a_limit(excel, tmp_path, offset_k, text) -> None:
    source, temps = _zone_source()
    path = _write(tmp_path, source, _prefill(temps, offset_k=offset_k))
    book = _open(excel, path)
    try:
        sheet = book.Worksheets("Emissivity Match")
        assert str(sheet.Range("D9").Value).startswith(text)
        _compare_sheet(book, path, range(13, 15), range(2, 11))
    finally:
        book.Close(False)


def test_excel_flags_tcs_that_disagree(excel, tmp_path) -> None:
    source, temps = _zone_source(eps={"Cell 6": 0.56})
    path = _write(tmp_path, source, _prefill(temps))
    book = _open(excel, path)
    try:
        sheet = book.Worksheets("Emissivity Match")
        assert str(sheet.Range("D10").Value).startswith("The TCs' own values run from 0.56 to 0.94")
        # leaving the bare zone out of the common value brings back the painted one's
        sheet.Range("D14").Value = "No"
        _com(excel.Calculate)
        assert sheet.Range("G6").Value == pytest.approx(TRUE_EPS)
        assert sheet.Range("D10").Value in ("", None)
        assert np.isfinite(sheet.Range("G7").Value)
    finally:
        book.Close(False)


@pytest.mark.parametrize("step, eps", [(0.03, 0.96), (0.001, 0.97)])
def test_excel_candidate_grid_reaches_the_highest_limit(excel, tmp_path, step, eps) -> None:
    from test_emissivity_match import _match_with

    path = _match_with(tmp_path, f"grid_{step}.xlsx", eps={"Cell 3": eps, "Cell 6": eps}, step=step)
    book = _open(excel, path)
    try:
        _compare_sheet(book, path, range(13, 13 + 41), range(12, 16))  # candidates, all TCs, both ROIs
        _compare_sheet(book, path, range(6, 9), (7,))
        _compare_sheet(book, path, (9,), (4,))
        sheet = book.Worksheets("Emissivity Match")
        assert sheet.Range("C8").Value == pytest.approx(max(step, 0.08 / 40))
    finally:
        book.Close(False)
