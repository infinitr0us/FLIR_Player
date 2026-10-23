# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""0.10.0 presets joined, evaluated by Microsoft Excel itself (opt-in, local only: ``FLIR_EXCEL_LIVE=1``).

Each row's constants follow its preset in the Data, TC Compare and Emissivity Match formulas; Excel's results must
equal the cached ones Python wrote, also after an emissivity changes on Settings.
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
pytest.importorskip("fnv")

from flir_player.calibration import RADIANCE_SCALE  # noqa: E402
from flir_player.excel_export import ExportOptions, SourceSpec, TcPrefill, run_export  # noqa: E402
from flir_player.models import RoiShape  # noqa: E402
from flir_player.radiometry import (Calibration, MeasurementParameters, PlanckConstants, matching_emissivity,  # noqa: E402
                                    object_temperature)

from test_v0_10_presets import ATMOSPHERE, PRESETS, _Superframing  # noqa: E402

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


def _joined_book(tmp_path, options_kw):
    ats = tmp_path / "1006_A8303sc.ats"
    ats.write_bytes(b"synthetic, no FFF record")
    im = _Superframing(shutter=range(40, 44))
    scene = [im.scene(k) for k in range(60)]
    prefill = TcPrefill(times=tuple(float(t) for t in range(60)),
                        columns=(tuple(float(s[7, 7]) - 280.0 for s in scene), tuple(float(s[2, 1]) - 270.0 for s in scene)),
                        names=("Tree 1 m", "T2"), roi_tc=(("Flame", "Tree 1 m"), ("Battery", "T2"), ("Box", "T2")))
    rois = (RoiShape(1, "cursor", ((1.5, 2.5),), "Battery"), RoiShape(2, "cursor", ((7.5, 7.5),), "Flame"),
            RoiShape(3, "rect", ((4.0, 4.0), (7.0, 7.0)), "Box"))  # spans presets 1 and 2 over time
    out = tmp_path / "joined.xlsx"
    run_export(out, [SourceSpec(ats, rois, ignition_frame=0, preset=(1, 2))],
               ExportOptions(step_s=1.0, tc_prefill=prefill, **options_kw), handles={ats.resolve(): im}, replace=[out])
    return out, im


def _cal(preset) -> Calibration:
    """A preset's curve on the workbook's signal (the SDK's radiance × RADIANCE_SCALE)."""
    curve = PRESETS[int(preset)][1]
    return Calibration(planck=PlanckConstants(curve.R * RADIANCE_SCALE, curve.B, curve.F, curve.O), atmosphere=ATMOSPHERE)


def _numbers(sheet, rows, cols):
    return [[sheet.cell(r, c).value for c in cols] for r in rows]


@pytest.mark.parametrize("options_kw", [{}, {"area_means": "signal"}, {"pixel_limit": 4, "bins": 4}],
                         ids=["pixels", "signal", "bins"])
def test_excel_reproduces_a_joined_workbook(excel, tmp_path, options_kw) -> None:
    path, im = _joined_book(tmp_path, options_kw)
    cached = openpyxl.load_workbook(path, data_only=True)
    width = cached["Data"].max_column
    before = _numbers(cached["Data"], range(6, 66), range(3, width + 1))
    counts_head = [c.value for c in cached["Counts"][5]]
    pre_cols = [i + 1 for i, h in enumerate(counts_head) if h == "Preset"]
    presets = {int(v) for r in _numbers(cached["Counts"], range(6, 66), pre_cols) for v in r if v is not None}
    assert presets == {1, 2}
    book = _com(excel.Workbooks.Open, str(path.resolve()))
    try:
        _com(excel.CalculateFull)
        data = book.Worksheets("Data")
        after = [[data.Cells(r, c).Value for c in range(3, width + 1)] for r in range(6, 66)]
        compared = 0
        for row_before, row_after in zip(before, after):
            for b, a in zip(row_before, row_after):
                if isinstance(b, float):
                    assert isinstance(a, float) and a == pytest.approx(b, abs=1e-6)
                    compared += 1
                elif b == "#N/A":
                    assert a == XL_NA
        assert compared > 100
        validation = book.Worksheets("Validation")
        assert str(validation.Range("E4").Value).startswith("PASS"), validation.Range("E4").Value
        # TC Compare: each row's matching emissivity with its own preset's curve
        tc = book.Worksheets("TC Compare")
        head = [tc.Cells(9, c).Value for c in range(1, 60)]
        flame_eps = [c + 1 for c, h in enumerate(head) if h == "Matching emissivity"][1]  # Battery, Flame, Box
        flame_counts = next(i + 1 for i, h in enumerate(counts_head) if h == "Value (signal)" and i + 1 > pre_cols[0])
        counts = cached["Counts"]
        file = MeasurementParameters(emissivity=0.95, transmission=None, distance_m=3.3, humidity=0.3)
        tau = ATMOSPHERE.transmission(3.3, 0.3, 293.15)
        checked = 0
        for k in range(60):
            value, preset = counts.cell(6 + k, flame_counts).value, counts.cell(6 + k, pre_cols[1]).value
            excel_eps = tc.Cells(10 + k, flame_eps).Value
            if value is None:
                assert excel_eps == XL_NA
                continue
            tc_k = float(im.scene(k)[7, 7]) - 280.0 + 273.15
            expected = matching_emissivity(_cal(preset), file.with_(transmission=tau), value, tc_k)
            assert excel_eps == pytest.approx(float(expected), rel=1e-6)
            checked += 1
        assert checked >= 50
        # Emissivity Match: Excel's array formulas give the results Python cached
        match, cached_match = book.Worksheets("Emissivity Match"), cached["Emissivity Match"]
        rows = [r for r in range(1, 60) if cached_match.cell(r, 1).value in ("Battery", "Flame", "Box")]
        assert len(rows) == 3
        for r in rows:
            for c in (5, 7, 8, 9, 10):
                b = cached_match.cell(r, c).value
                if isinstance(b, float):
                    assert match.Cells(r, c).Value == pytest.approx(b, abs=1e-6), (r, c)
        # an emissivity of the flame's own on Settings reaches the preset-2 constants
        settings = book.Worksheets("Settings")
        row = next(r for r in range(1, 80) if settings.Cells(r, 3).Value == "Flame")
        settings.Cells(row, 6).Value = 0.8
        _com(excel.Calculate)
        flame_col = next(c for c in range(3, width + 1)
                         if str(data.Cells(4, c).MergeArea.Cells(1, 1).Value).endswith("· Flame"))
        p = file.with_(transmission=tau, emissivity=0.8)
        for k in range(0, 60, 7):
            value, preset = counts.cell(6 + k, flame_counts).value, counts.cell(6 + k, pre_cols[1]).value
            if value is None:
                continue
            expected = float(object_temperature(_cal(preset), p, value)) - 273.15
            got = data.Cells(6 + k, flame_col).Value
            if got != XL_NA:
                assert got == pytest.approx(expected, abs=1e-6)
    finally:
        book.Close(SaveChanges=False)
