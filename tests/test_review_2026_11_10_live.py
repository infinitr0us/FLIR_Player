# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""The review fixes of 2026-11-10 that change formulas, evaluated by Microsoft Excel itself (opt-in, local only:
``FLIR_EXCEL_LIVE=1``): a mean-signal area's statistics by their own counts (values and the grey format), and the
Emissivity Match scoring every candidate on the same rows."""
from __future__ import annotations

import os

import numpy as np
import pytest

if os.environ.get("FLIR_EXCEL_LIVE") != "1":
    pytest.skip("set FLIR_EXCEL_LIVE=1 to run tests in Microsoft Excel", allow_module_level=True)
com_client = pytest.importorskip("win32com.client")
openpyxl = pytest.importorskip("openpyxl")

from flir_player.excel_export import ExportOptions, SourceSpec, run_export  # noqa: E402
from flir_player.models import RoiShape  # noqa: E402
from flir_player.workbook import CLAMP_BLANK, CLAMP_OFF, MATCH_CANDIDATES  # noqa: E402

from test_emissivity_match import PARAMS, _prefill, _write, _zone_source  # noqa: E402
from test_match_live import XL_NA, _com, _compare_sheet, _open, excel  # noqa: E402,F401  (fixture)


def test_excel_blanks_and_greys_a_mean_signal_areas_statistics_by_their_own_counts(excel, tmp_path) -> None:  # noqa: F811
    pytest.importorskip("fnv")
    from test_v0_10_presets import _Superframing

    ats = tmp_path / "1006_A8303sc.ats"
    ats.write_bytes(b"synthetic, no FFF record")
    out = tmp_path / "mean.xlsx"
    box = RoiShape(1, "rect", ((0.0, 0.0), (8.0, 4.0)), "Box")  # 500 to 795 K at the 523-873 K preset
    run_export(out, [SourceSpec(ats, (box,), ignition_frame=0, preset=1)],
               ExportOptions(step_s=1.0, area_means="signal", extremes=True),
               handles={ats.resolve(): _Superframing()}, replace=[out])
    cached = openpyxl.load_workbook(out, data_only=True)["Data"]
    head = [str(cell.value or "").split(" (")[0] for cell in cached[5]]
    col = {name: k + 1 for k, name in enumerate(head) if name}
    book = _open(excel, out)
    try:
        data, settings = book.Worksheets("Data"), book.Worksheets("Settings")
        assert settings.Range("B5").Value == CLAMP_BLANK
        for r in range(6, 30):
            assert data.Cells(r, col["Status"]).Value == "OK"
            assert data.Cells(r, col["Min"]).Value == XL_NA and data.Cells(r, col["Spread"]).Value == XL_NA
            for name in ("Mean", "Max"):
                assert data.Cells(r, col[name]).Value == pytest.approx(cached.cell(r, col[name]).value, abs=1e-6)
        # shown instead of blank: the coldest pixel's value is there, and grey (the format's italic), by its own
        # count on the Counts sheet; the mean and the hottest pixel are inside the range and plain
        settings.Range("B5").Value = CLAMP_OFF
        _com(excel.Calculate)
        for r in (6, 17, 29):
            minimum = data.Cells(r, col["Min"])
            # (the range is one of counts: at the recorded ε 0.95 the pixel can read just above 250 °C)
            assert isinstance(minimum.Value, float) and minimum.Value < 260.0
            assert minimum.DisplayFormat.Font.Italic is True
            assert data.Cells(r, col["Spread"]).DisplayFormat.Font.Italic is True
            assert data.Cells(r, col["Mean"]).DisplayFormat.Font.Italic is False
            assert data.Cells(r, col["Max"]).DisplayFormat.Font.Italic is False
    finally:
        book.Close(SaveChanges=False)


def test_excel_scores_every_candidate_on_the_same_rows(excel, tmp_path) -> None:  # noqa: F811
    """Hot surroundings (reflected 1000 K): the candidates below the true 0.94 give the cold rows no temperature
    and are #N/A in Excel as in the cached values; lowering the highest emissivity changes the rows scored."""
    source, temps = _zone_source(params=PARAMS.with_(reflected_k=1000.0))
    path = _write(tmp_path, source, _prefill(temps))
    cached = openpyxl.load_workbook(path, data_only=True)["Emissivity Match"]
    book = _open(excel, path)
    try:
        _compare_sheet(book, path, range(13, 13 + MATCH_CANDIDATES), (12, 13, 14, 15), rel=1e-6)
        _compare_sheet(book, path, (13, 14), (5, 7, 8, 9, 10), rel=1e-6)
        sheet = book.Worksheets("Emissivity Match")
        assert sheet.Range("G6").Value == pytest.approx(0.94)
        assert sheet.Cells(13, 13).Value == XL_NA and cached.cell(13, 13).value == "#N/A"  # 0.90
        rows_all = sheet.Cells(13, 5).Value
        assert rows_all == 60
        # with 0.92 the highest candidate, fewer rows have a temperature: Rows used follows, every candidate
        # left is scored on those rows, and the best is the limit
        sheet.Range("B7").Value = 0.92
        _com(excel.Calculate)
        fewer = sheet.Cells(13, 5).Value
        assert 0 < fewer < rows_all
        assert sheet.Range("G6").Value == pytest.approx(0.92)
        values = [sheet.Cells(13 + j, 13).Value for j in range(3)]  # 0.90, 0.91, 0.92
        assert values[0] == XL_NA and values[1] == XL_NA and isinstance(values[2], float)
    finally:
        book.Close(SaveChanges=False)
    assert np.isfinite(rows_all)


def test_excel_scores_all_tcs_only_where_every_tc_has_a_score(excel, tmp_path) -> None:  # noqa: F811
    """Cell 1 has no score at 0.90 (hot surroundings), cell 2 fits it exactly: "All TCs" is #N/A there in Excel
    too, and Excel's best value is the cached one. Excluding cell 1 leaves cell 2 alone, which 0.90 fits."""
    t = np.arange(60, dtype=np.float64)
    temps = {"Cell 1": 20.0 + 500.0 * np.clip(t / 40.0, 0.0, 1.0), "Cell 2": np.full(60, 500.0)}
    source, temps = _zone_source(params=PARAMS.with_(reflected_k=1000.0), temps=temps,
                                 eps={"Cell 1": 0.94, "Cell 2": 0.90})
    path = _write(tmp_path, source, _prefill(temps, pairs=(("Cell 1", "T1"), ("Cell 2", "T2"))), name="two.xlsx")
    cached = openpyxl.load_workbook(path, data_only=True)["Emissivity Match"]
    book = _open(excel, path)
    try:
        _compare_sheet(book, path, range(13, 13 + MATCH_CANDIDATES), (12, 13, 14, 15), rel=1e-6)
        sheet = book.Worksheets("Emissivity Match")
        assert sheet.Cells(13, 13).Value == XL_NA and isinstance(sheet.Cells(13, 15).Value, float)
        assert sheet.Range("G6").Value == pytest.approx(cached["G6"].value) and cached["G6"].value > 0.90
        sheet.Cells(13, 4).Value = "No"  # cell 1 no longer takes part
        _com(excel.Calculate)
        assert sheet.Range("G6").Value == pytest.approx(0.90)
        assert sheet.Cells(13, 13).Value == pytest.approx(0.0, abs=0.01)
    finally:
        book.Close(SaveChanges=False)
