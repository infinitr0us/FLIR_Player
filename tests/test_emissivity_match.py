# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cell-zone workbooks: Use/Fit rows on TC Compare, the Emissivity Match sheet, mean-signal areas,
charts for many ROIs, and the TC fit's prefill for zones."""
from __future__ import annotations

import math
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

openpyxl = pytest.importorskip("openpyxl")

from flir_player.calibration import CalibrationReport  # noqa: E402
from flir_player.excel_export import (  # noqa: E402
    ExportData,
    ExportOptions,
    RoiData,
    SourceData,
    SourceSpec,
    TcPrefill,
    estimate,
)
from flir_player.models import RoiShape  # noqa: E402
from flir_player.radiometry import KELVIN, MeasurementParameters, coefficients, count_status  # noqa: E402
from flir_player.workbook import MATCH, MATCH_CANDIDATES, RAMP, write_workbook  # noqa: E402

from test_radiometry import T650  # noqa: E402

PARAMS = MeasurementParameters(emissivity=0.95, reflected_k=293.15, atmosphere_k=293.15, transmission=0.99,
                               distance_m=8.0, humidity=0.5)
TRUE_EPS = 0.94


def _zone_temps(rows: int, zones: int) -> dict[str, np.ndarray]:
    """°C per zone: 20 °C, then a ramp to 520 °C, each zone 5 s later than the one before."""
    t = np.arange(rows, dtype=np.float64)
    return {f"Cell {k + 1}": 20.0 + 500.0 * np.clip((t - 5 * k) / 40.0, 0.0, 1.0) for k in range(zones)}


def _counts(temp_c, eps):
    k1, k2 = coefficients(T650, PARAMS.with_(emissivity=eps))
    return (T650.planck.signal(np.asarray(temp_c) + KELVIN) + k2) / k1


def _zone_source(rows: int = 60, zones: int = 6, eps: dict[str, float] | None = None) -> tuple[SourceData, dict]:
    temps = _zone_temps(rows, zones)
    rois = []
    for k, (name, temp) in enumerate(temps.items()):
        mean = _counts(temp, (eps or {}).get(name, TRUE_EPS))
        counts = {"mean": mean, "max": mean + 15.0, "min": mean - 15.0}
        status = count_status(T650, counts["min"], counts["max"]).astype(np.int8)
        shape = RoiShape(k + 1, "rect", ((10.0 + 5 * k, 10.0), (14.0 + 5 * k, 14.0)), name)
        rois.append(RoiData(shape=shape, pixels=16, mode="mean", counts=counts, block=None, status=status))
    start = datetime(2026, 9, 3, 15, 51, 27)
    info = {"file": "C:/data/zones.csq", "file_name": "zones.csq", "file_size": 1, "camera": "FLIR T650sc",
            "camera_short": "T650sc", "camera_serial": "1", "lens": "FOL25", "width": 120, "height": 40,
            "frames": rows * 30, "fps": 30.0, "start": start, "end": start + timedelta(seconds=rows),
            "ignition_frame": 0, "calibration": "test calibration", "saturation_threshold": None}
    report = CalibrationReport(T650, "verified", "test calibration")
    source = SourceData(spec=SourceSpec(Path(info["file"]), tuple(r.shape for r in rois)), label="T650sc", info=info,
                        fps=30.0, num_frames=rows * 30, frames=np.arange(rows) * 30,
                        clock=[start + timedelta(seconds=float(i)) for i in range(rows)], rois=rois, report=report,
                        initial=PARAMS, file_parameters=PARAMS, validation_rows=np.empty(0, int))
    return source, temps


def _prefill(temps, pairs=(("Cell 3", "T1"), ("Cell 6", "T2")), offset_k=0.0, **extra) -> TcPrefill:
    rows = len(next(iter(temps.values())))
    names = tuple(tc for _roi, tc in pairs)
    columns = tuple(tuple(float(v) + offset_k for v in temps[roi]) for roi, _tc in pairs)
    return TcPrefill(times=tuple(float(t) for t in range(rows)), columns=columns, names=names, roi_tc=tuple(pairs),
                     **extra)


def _write(tmp_path, source, prefill, *, name="zones.xlsx", sheets=None):
    options = ExportOptions(area_means="signal", tc_prefill=prefill)
    if sheets is not None:
        options = ExportOptions(area_means="signal", tc_prefill=prefill, sheets=sheets)
    rows = source.frames.size
    data = ExportData(times=np.arange(rows, dtype=float), sources=[source], options=options,
                      created=datetime(2026, 10, 10), versions={"tool": "test", "sdk": ""})
    path = tmp_path / name
    write_workbook(path, data)
    return path


class _Match:
    """Cached results of the Emissivity Match sheet, read back with openpyxl."""

    def __init__(self, path: Path, zones: int):
        book = openpyxl.load_workbook(path, data_only=True)
        self.formulas = openpyxl.load_workbook(path)[MATCH]
        self.ws = ws = book[MATCH]
        self.best = ws["G6"].value
        self.best_error = ws["G7"].value
        self.error_now = ws["G8"].value
        self.note = ws["D9"].value
        self.spread = ws["D10"].value
        self.p0 = self.c0 = 13  # Excel row of the first ROI and of the first candidate
        self.pairs = {ws.cell(self.p0 + k, 1).value: [ws.cell(self.p0 + k, c).value for c in range(2, 11)]
                      for k in range(zones)}
        self.eps = [ws.cell(self.c0 + j, 12).value for j in range(MATCH_CANDIDATES)]  # column L
        self.combined = [ws.cell(self.c0 + j, 13).value for j in range(MATCH_CANDIDATES)]
        self.hidden = {ws.cell(self.c0 - 1, 14 + k).value:
                       bool(ws.column_dimensions[ws.cell(1, 14 + k).column_letter].hidden) for k in range(zones)}


def test_the_match_finds_the_true_emissivity_of_the_zones(tmp_path) -> None:
    source, temps = _zone_source()
    match = _Match(_write(tmp_path, source, _prefill(temps)), 2)  # only the paired zones are compared
    assert match.best == pytest.approx(TRUE_EPS)
    assert match.best_error < 0.05
    assert match.note == "Inside the limits."
    assert not match.spread  # "" (read back as None)
    # candidates 0.90..0.98, then #N/A rows (hidden by formatting)
    assert match.eps[:9] == pytest.approx([0.90 + 0.01 * k for k in range(9)])
    assert match.eps[9] == "#N/A" and match.combined[9] == "#N/A"
    assert match.combined[4] == pytest.approx(match.best_error)
    # the error grows away from the true value on both sides
    assert match.combined[0] > match.combined[2] > match.combined[4] < match.combined[6] < match.combined[8]
    # pairs: TC, include, rows, ε now (Settings 0.95), error now > 0, own best ≈ true, error at best ≈ 0
    tc, include, rows, eps_now, err_now, bias_now, own, at_best = match.pairs["Cell 3"][1:]
    assert (tc, include, eps_now) == ("T1", "Yes", pytest.approx(0.95))
    assert rows > 20 and err_now > 0.5 and bias_now < 0  # ε 0.95 > 0.94 reads a little cold
    assert own == pytest.approx(TRUE_EPS, abs=1e-6) and at_best < 0.05
    assert set(match.pairs) == {"Cell 3", "Cell 6"} and not any(match.hidden.values())


def test_hot_reading_tcs_pin_the_best_value_at_the_lowest_limit(tmp_path) -> None:
    source, temps = _zone_source()
    match = _Match(_write(tmp_path, source, _prefill(temps, offset_k=40.0)), 2)
    assert match.best == pytest.approx(0.90)
    assert match.note.startswith("At the lowest emissivity allowed")
    cold = _Match(_write(tmp_path, source, _prefill(temps, offset_k=-40.0), name="b.xlsx"), 2)
    assert cold.best == pytest.approx(0.98)
    assert cold.note.startswith("At the highest emissivity allowed")


def test_tcs_on_differently_painted_zones_are_called_out(tmp_path) -> None:
    source, temps = _zone_source(eps={"Cell 6": 0.56})
    match = _Match(_write(tmp_path, source, _prefill(temps)), 2)
    assert match.pairs["Cell 3"][7] == pytest.approx(TRUE_EPS, abs=1e-6)
    assert match.pairs["Cell 6"][7] == pytest.approx(0.56, abs=1e-6)
    assert match.spread.startswith("The TCs' own values run from 0.56 to 0.94")
    assert match.best == pytest.approx(0.90)  # the bare zone pulls the common value to the limit


def test_use_rows_and_the_calibrated_range_decide_the_fit_rows(tmp_path) -> None:
    source, temps = _zone_source()
    # Cell 3's TC fit used 30-49 s only (logger seconds, already on the workbook axis)
    prefill = _prefill(temps, roi_use=(("Cell 3", ((30.0, 49.0),)),))
    path = _write(tmp_path, source, prefill)
    book = openpyxl.load_workbook(path, data_only=True)
    tc = book["TC Compare"]
    head = [tc.cell(9, c).value for c in range(1, tc.max_column + 1)]
    use_cols = [c + 1 for c, v in enumerate(head) if v == "Use (1/0)"]
    fit_cols = [c + 1 for c, v in enumerate(head) if v == "Fit row"]
    assert len(use_cols) == len(fit_cols) == 2  # filled from the fit: only the paired zones
    k3, k6 = 0, 1  # Cell 3 and Cell 6 blocks
    use3 = [tc.cell(10 + i, use_cols[k3]).value for i in range(60)]
    fit3 = [tc.cell(10 + i, fit_cols[k3]).value for i in range(60)]
    assert use3 == [1 if 30 <= i <= 49 else 0 for i in range(60)]
    assert fit3 == use3  # Cell 3 is above 100 °C (the T650's range) from 18 s
    fit6 = [tc.cell(10 + i, fit_cols[k6]).value for i in range(60)]
    t6 = temps["Cell 6"]
    assert fit6 == [1 if t6[i] >= 100.0 + 1e-6 else 0 for i in range(60)]  # every row, in range only
    match = _Match(path, 2)
    assert match.pairs["Cell 3"][3] == 20
    # the formulas: Use is an input, Fit row a formula over it
    formulas = openpyxl.load_workbook(path)["TC Compare"]
    assert str(formulas.cell(10, fit_cols[k3]).value).startswith("=IF(AND(")
    assert formulas.cell(10, use_cols[k3]).value == 0


def test_spots_listed_in_match_exclude_start_excluded(tmp_path) -> None:
    source, temps = _zone_source()
    prefill = _prefill(temps, match_exclude=("Cell 6",))
    match = _Match(_write(tmp_path, source, prefill), 2)
    assert match.pairs["Cell 6"][2] == "No"
    assert match.pairs["Cell 6"][8] == "–"  # no candidate errors for an excluded ROI
    assert match.best == pytest.approx(TRUE_EPS)  # from Cell 3 alone


def test_formulas_are_array_formulas_over_named_ranges(tmp_path) -> None:
    source, temps = _zone_source()
    path = _write(tmp_path, source, _prefill(temps))
    ws = openpyxl.load_workbook(path)[MATCH]
    cell = ws.cell(13, 14)  # Cell 3 (column N, the first ROI) at the first candidate
    text = cell.value.text if hasattr(cell.value, "text") else str(cell.value)
    assert "ROI_03_FITR" in text and "ROI_03_TCR" in text and "ROI_03_CNT" in text
    assert "_xlfn" not in text  # classic functions only (AGGREGATE elsewhere carries its prefix)
    names = openpyxl.load_workbook(path).defined_names
    for name in ("MATCH_LO", "MATCH_HI", "MATCH_STEP", "MATCH_FROM", "MATCH_TO", "MATCH_EPS", "MATCH_ALL",
                 "SRC_1_CNTLO", "SRC_1_CNTHI", "ROI_03_USER", "ROI_03_FITR", "ROI_03_CNT"):
        assert name in names, name


def test_mean_signal_areas_store_no_pixels_and_say_so(tmp_path) -> None:
    source, temps = _zone_source()
    path = _write(tmp_path, source, _prefill(temps))
    book = openpyxl.load_workbook(path)
    settings = book["Settings"]
    types = [settings.cell(r, 4).value for r in range(1, settings.max_row + 1)
             if str(settings.cell(r, 3).value or "").startswith("Cell ")]
    assert types == ["Box (mean signal)"] * 6
    assert book["Pixels"].max_column == 1  # time only
    data = book["Data"]
    assert "mean signal" in data["A3"].value
    mean_formula = data.cell(6, 3).value
    assert "'Counts'!" in mean_formula and "Pixels" not in mean_formula
    source_sheet = book["Source"]
    values = {source_sheet.cell(r, 1).value: source_sheet.cell(r, 2).value for r in range(1, source_sheet.max_row + 1)}
    assert values["Area means"] == "from the mean signal (mean counts)"


def _chart_xml(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        names = sorted((n for n in archive.namelist() if re.match(r"xl/charts/chart\d+\.xml", n)),
                       key=lambda n: int(re.findall(r"\d+", n)[0]))
        return [archive.read(n).decode("utf-8") for n in names]


def test_many_zones_get_an_ordered_ramp_and_charts_only_for_tc_zones(tmp_path) -> None:
    source, temps = _zone_source(zones=18)
    path = _write(tmp_path, source, _prefill(temps, pairs=(("Cell 3", "T1"), ("Cell 6", "T2"), ("Cell 9", "T3"))))
    charts = _chart_xml(path)
    titles = [re.findall(r"<a:t>([^<]*)</a:t>", xml)[0] for xml in charts]
    # Charts: overview, one per TC zone, matching ε; Emissivity Match: error against emissivity
    assert titles.count("All ROIs (darkest = first ROI)") == 1
    assert {"Cell 3", "Cell 6", "Cell 9"} <= set(titles) and "Cell 1" not in titles
    assert "Matching emissivity (from TC Compare)" in titles and "Error against emissivity" in titles
    assert len(charts) == 6
    overview = charts[titles.index("All ROIs (darkest = first ROI)")]
    colours = [c for c in re.findall(r'<a:srgbClr val="([0-9A-F]{6})"', overview) if c != "E0E0E0"]
    assert colours[0] == RAMP[-1].lstrip("#").upper() and colours[-1] == RAMP[0].lstrip("#").upper()
    assert len(set(colours)) == 18
    match_chart = charts[titles.index("Error against emissivity")]
    assert match_chart.count("<c:ser>") == 4  # all TCs + three zones


def test_few_rois_keep_the_workbook_palette_and_every_chart(tmp_path) -> None:
    source, temps = _zone_source(zones=4)
    charts = _chart_xml(_write(tmp_path, source, _prefill(temps, pairs=(("Cell 3", "T1"),))))
    titles = [re.findall(r"<a:t>([^<]*)</a:t>", xml)[0] for xml in charts]
    assert titles.count("All ROIs") == 1 and {"Cell 1", "Cell 2", "Cell 3", "Cell 4"} <= set(titles)


def test_a_workbook_without_tc_compare_has_no_match_sheet(tmp_path) -> None:
    source, temps = _zone_source()
    path = _write(tmp_path, source, None, sheets=frozenset({"charts", "summary"}))
    assert MATCH not in openpyxl.load_workbook(path).sheetnames


def test_no_logger_data_gives_an_empty_match_with_guidance(tmp_path) -> None:
    source, _temps = _zone_source()
    match = _Match(_write(tmp_path, source, None), 6)  # pasted by hand later: every ROI is compared
    assert match.best == "–" and match.note.startswith("No ROI has rows to compare yet")
    assert all(v[3] == 0 for v in match.pairs.values()) and match.pairs["Cell 1"][1] == "no TC"
    assert not any(match.hidden.values())  # nothing is paired yet, so nothing is hidden


def test_a_paired_workbook_compares_only_its_pairs(tmp_path) -> None:
    source, _temps = _zone_source()
    prefill = TcPrefill(times=(0.0, 1.0), columns=((20.0, 20.0),), names=("T1",), roi_tc=(("Cell 3", "T1"),))
    match = _Match(_write(tmp_path, source, prefill), 1)
    assert set(match.pairs) == {"Cell 3"}
    tc = openpyxl.load_workbook(tmp_path / "zones.xlsx")["TC Compare"]
    assert "Only the ROIs the TC fit paired" in str(tc["D6"].value)


def test_estimate_counts_the_new_columns() -> None:
    pixels = ExportOptions()
    signal = ExportOptions(area_means="signal")
    rois = [("rect", 60)] * 18
    assert estimate(100, rois, signal)["cells"] == 100 * 18 * (2 * 3 + 1 + 9)
    assert estimate(100, rois, pixels)["cells"] == 100 * 18 * (2 * 3 + 1 + 60 + 9)
    lean = ExportOptions(area_means="signal", extremes=False, tc_prefill=TcPrefill(
        times=(0.0,), columns=((1.0,), (1.0,)), names=("T1", "T2"), roi_tc=(("Cell 3", "T1"), ("Cell 6", "T2"))))
    assert estimate(100, rois, lean)["cells"] == 100 * 18 * (2 * 1 + 1) + 100 * 9 * 2


def test_without_extremes_areas_keep_only_their_mean(tmp_path) -> None:
    source, temps = _zone_source()
    options = ExportOptions(area_means="signal", extremes=False, tc_prefill=_prefill(temps))
    data = ExportData(times=np.arange(60, dtype=float), sources=[source], options=options,
                      created=datetime(2026, 10, 10), versions={"tool": "test", "sdk": ""})
    write_workbook(tmp_path / "lean.xlsx", data)
    book = openpyxl.load_workbook(tmp_path / "lean.xlsx")
    heads = [book["Data"].cell(5, c).value for c in range(1, book["Data"].max_column + 1)]
    assert heads.count('="Mean ("&UNIT&")"') == 6
    assert not any("Max" in str(h) or "Spread" in str(h) for h in heads)
    assert heads.count("Status") == 6  # still from each zone's min and max counts
    counts = [book["Counts"].cell(5, c).value for c in range(1, book["Counts"].max_column + 1)]
    assert "Max (counts)" not in counts and counts.count("Mean (counts)") == 6


def test_use_flags_cover_whole_rows_inside_each_range() -> None:
    prefill = TcPrefill(times=(0.0,), columns=((1.0,),), names=("A",), roi_use=(("Z", ((1.5, 3.5), (7.0, 7.0))),))
    flags = prefill.use_flags("Z", np.arange(10.0))
    assert flags.tolist() == [0, 0, 1, 1, 0, 0, 0, 1, 0, 0]
    assert prefill.use_flags("other", np.arange(3.0)) is None
    with pytest.raises(ValueError, match="earlier to a later"):
        TcPrefill(times=(0.0,), columns=((1.0,),), names=("A",), roi_use=(("Z", ((3.0, 1.0),)),)).check(10, 10)
    assert math.isclose(sum(flags), 3)


# --- the TC fit's prefill for zones (tcmatch) ----------------------------------------------------


def _scene_result():
    from test_tcmatch import make_scene

    from flir_player import tcmatch
    from flir_player.tcmatch import MatchOptions

    samples, table = make_scene()
    result, _location, _series = tcmatch.analyse(samples, table, MatchOptions(spot=3))
    return tcmatch, result, table


def test_events_keep_the_seconds_their_fit_used() -> None:
    _tcmatch, result, _table = _scene_result()
    used = [e for ch in result.channels for e in ch.events if e.used]
    assert used
    for event in used:
        assert event.fitted, event.label
        seconds = sum(b - a + 1 for a, b in event.fitted)
        assert seconds == event.samples - event.excluded_bound  # every fitted bin, once
        assert all(event.start_s <= a <= b <= event.end_s for a, b in event.fitted)
    assert "fitted" in result.to_dict()["channels"][0]["events"][0]


def test_zone_prefill_pairs_tcs_with_their_zones_and_marks_their_fitted_rows() -> None:
    from test_tcmatch import SPOT_A, SPOT_B

    tcmatch, result, table = _scene_result()
    search = RoiShape(1, "rect", ((0.0, 0.0), (48.0, 40.0)), "Search box")
    zone_a = RoiShape(2, "rect", ((SPOT_A[1] - 2.0, SPOT_A[0] - 1.0), (SPOT_A[1] + 3.0, SPOT_A[0] + 2.0)), "Cell 3")
    zone_b = RoiShape(3, "rect", ((SPOT_B[1] - 2.0, SPOT_B[0] - 1.0), (SPOT_B[1] + 3.0, SPOT_B[0] + 2.0)), "Cell 6")
    frame = 3000  # any ignition frame: the offset follows from it
    prefill = tcmatch.tc_prefill(result, table, rois=[search, zone_a, zone_b], frame=frame, size=(48, 40))
    assert dict(prefill.roi_tc) == {"Cell 3": "TC A", "Cell 6": "TC B"}
    assert prefill.roi_eps == () and prefill.match_exclude == ()
    assert prefill.offset_s == pytest.approx(result.lag_s - frame / result.fps)
    channels = {ch.name: ch for ch in result.channels}
    for roi, tc in prefill.roi_tc:
        runs = dict(prefill.roi_use)[roi]
        expected = [(a - 0.5 + prefill.offset_s, b + 0.5 + prefill.offset_s)
                    for e in channels[tc].events if e.used for a, b in e.fitted]
        assert list(runs) == pytest.approx(expected)
    prefill.check(100_000, 10)
    # the default prefill (spots and boxes at the TC pixels) excludes its spots from the common value
    default = tcmatch.tc_prefill(result, table)
    assert set(default.match_exclude) == {"TC A spot", "TC B spot"}
    assert {roi for roi, _runs in default.roi_use} == {roi for roi, _tc in default.roi_tc}


def test_zone_prefill_needs_the_image_size_and_refuses_superframing() -> None:
    from dataclasses import replace

    tcmatch, result, table = _scene_result()
    with pytest.raises(ValueError, match="image size"):
        tcmatch.tc_prefill(result, table, rois=[RoiShape(1, "rect", ((0.0, 0.0), (4.0, 4.0)), "Z")])
    with pytest.raises(ValueError, match="superframing"):
        tcmatch.tc_prefill(replace(result, preset=1), table, frame=10)


def test_row_formulas_cite_cells_not_names(tmp_path) -> None:
    """Excel resolves names slowly when it opens a workbook: rows cite the cells, names stay defined."""
    source, temps = _zone_source()
    path = _write(tmp_path, source, _prefill(temps))
    book = openpyxl.load_workbook(path)
    for sheet in ("Data", "TC Compare"):
        ws = book[sheet]
        for row in ws.iter_rows(min_row=10, max_row=12):
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    assert not re.search(r"\b(ROI_\d+|SRC_\d+|UNIT_[AB]|TC_OFFSET|MATCH_(FROM|TO))", cell.value), \
                        (sheet, cell.coordinate, cell.value)
    names = book.defined_names
    for name in ("ROI_03_K1", "SRC_1_B", "UNIT_A", "TC_OFFSET", "MATCH_FROM"):
        assert name in names
    fit = [c.value for c in book["TC Compare"][10]
           if isinstance(c.value, str) and c.value.startswith("=IF(AND(") and "=1," in c.value][0]
    assert "'Emissivity Match'!$B$9" in fit and "'Emissivity Match'!$B$10" in fit  # the window
