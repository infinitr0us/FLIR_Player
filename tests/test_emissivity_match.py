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


def _counts(temp_c, eps, params=PARAMS):
    k1, k2 = coefficients(T650, params.with_(emissivity=eps))
    return (T650.planck.signal(np.asarray(temp_c) + KELVIN) + k2) / k1


def _zone_source(rows: int = 60, zones: int = 6, eps: dict[str, float] | None = None, params=None,
                 temps: dict[str, np.ndarray] | None = None, label: str = "T650sc") -> tuple[SourceData, dict]:
    params = params or PARAMS
    temps = temps or _zone_temps(rows, zones)
    rois = []
    for k, (name, temp) in enumerate(temps.items()):
        mean = _counts(temp, (eps or {}).get(name, TRUE_EPS), params)
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
    source = SourceData(spec=SourceSpec(Path(info["file"]), tuple(r.shape for r in rois)), label=label, info=info,
                        fps=30.0, num_frames=rows * 30, frames=np.arange(rows) * 30,
                        clock=[start + timedelta(seconds=float(i)) for i in range(rows)], rois=rois, report=report,
                        initial=params, file_parameters=params, validation_rows=np.empty(0, int))
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
    assert match.note.startswith("The best value is the lowest emissivity allowed") and "read colder" in match.note
    cold = _Match(_write(tmp_path, source, _prefill(temps, offset_k=-40.0), name="b.xlsx"), 2)
    assert cold.best == pytest.approx(0.98)
    assert cold.note.startswith("The best value is the highest emissivity allowed") and "read hotter" in cold.note


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


# --- Codex review 2026-10-10 (local/notes/2026-10-10-cell-zones/codex/, run review-cell-zones) -------------


def _match_with(tmp_path, name, *, eps=None, lo=None, hi=None, step=None, **prefill_extra):
    """A zone workbook whose Match inputs start at other values (the defaults are module constants)."""
    import flir_player.workbook as wbmod

    source, temps = _zone_source(eps=eps)
    saved = wbmod.MATCH_DEFAULTS
    defaults = list(saved)
    for k, value in enumerate((lo, hi, step)):
        if value is not None:
            defaults[k] = value
    wbmod.MATCH_DEFAULTS = tuple(defaults)
    try:
        path = _write(tmp_path, source, _prefill(temps, **prefill_extra), name=name)
    finally:
        wbmod.MATCH_DEFAULTS = saved
    return path


def test_z1_rois_sharing_a_name_are_not_paired_by_name(tmp_path) -> None:
    from test_tcmatch import SPOT_A, SPOT_B

    from flir_player import tcmatch
    from flir_player.zones import pair_tcs

    a = RoiShape(1, "rect", ((SPOT_A[1] - 1.0, SPOT_A[0] - 1.0), (SPOT_A[1] + 2.0, SPOT_A[0] + 2.0)), "Cell 3")
    b = RoiShape(2, "rect", ((SPOT_B[1] - 1.0, SPOT_B[0] - 1.0), (SPOT_B[1] + 2.0, SPOT_B[0] + 2.0)), "Cell 3")
    pairing = pair_tcs({"TC A": SPOT_A, "TC B": SPOT_B}, [a, b], 40, 48)
    assert pairing.shared == {}  # two ROIs, one TC each: nothing is shared
    assert pairing.duplicates == ["Cell 3"] and "more than one ROI is named Cell 3" in pairing.describe()
    _tcmatch, result, table = _scene_result()
    with pytest.raises(ValueError, match="named Cell 3"):
        tcmatch.tc_prefill(result, table, rois=[a, b], frame=0, size=(48, 40))


def test_z2_a_fine_step_still_reaches_the_highest_emissivity(tmp_path) -> None:
    path = _match_with(tmp_path, "fine.xlsx", eps={"Cell 3": 0.97, "Cell 6": 0.97}, step=0.001)
    match = _Match(path, 2)
    eps = [e for e in match.eps if isinstance(e, float)]
    assert len(eps) == MATCH_CANDIDATES and eps[0] == pytest.approx(0.90) and eps[-1] == pytest.approx(0.98)
    assert match.best == pytest.approx(0.97, abs=0.0011)  # step widened to 0.002
    formulas = openpyxl.load_workbook(path)[MATCH]
    assert "MATCH_STEPUSED" in formulas.cell(14, 12).value and formulas["C8"].value.startswith("=MAX(MATCH_STEP,")


def test_z3_limit_notes_follow_the_limits_entered(tmp_path) -> None:
    # step 0.03: 0.90, 0.93, 0.96 and the highest limit 0.98 itself; 0.96 is inside the limits
    match = _Match(_match_with(tmp_path, "coarse.xlsx", eps={"Cell 3": 0.96, "Cell 6": 0.96}, step=0.03), 2)
    assert [e for e in match.eps if isinstance(e, float)] == pytest.approx([0.90, 0.93, 0.96, 0.98])
    assert match.best == pytest.approx(0.96) and match.note == "Inside the limits."
    single = _Match(_match_with(tmp_path, "one.xlsx", eps={"Cell 3": 0.98, "Cell 6": 0.98}, lo=0.95, hi=0.95), 2)
    assert single.best == pytest.approx(0.95) and single.note.startswith("Only one emissivity is tried")


def test_z4_per_roi_emissivities_are_named_on_the_match_sheet(tmp_path) -> None:
    path = _match_with(tmp_path, "overrides.xlsx", roi_eps=(("Cell 3", 0.8),))
    ws = openpyxl.load_workbook(path)[MATCH]
    assert "clear those cells" in ws["A5"].value
    plain = openpyxl.load_workbook(_match_with(tmp_path, "plain.xlsx"))[MATCH]
    assert plain["A5"].value is None


def test_z5_a_hand_paired_zone_workbook_keeps_every_chart(tmp_path) -> None:
    source, _temps = _zone_source(zones=18)
    charts = _chart_xml(_write(tmp_path, source, None))  # TCs pasted and picked later in Excel
    titles = [re.findall(r"<a:t>([^<]*)</a:t>", xml)[0] for xml in charts]
    assert {f"Cell {k}" for k in range(1, 19)} <= set(titles)
    match_chart = charts[titles.index("Error against emissivity")]
    assert match_chart.count("<c:ser>") == 19  # all TCs + every zone (empty until paired)


def test_z6_direct_references_leave_string_literals_alone() -> None:
    from flir_player.workbook import _Writer

    writer = _Writer.__new__(_Writer)
    writer._refs = {"ROI_01_K1": "'Settings'!$S$27", "UNIT_A": "'Settings'!$T$4"}
    out = writer._direct('=IF(ROI_01_K1>0,"ROI_01_K1 UNIT_A",UNIT_A*ROI_01_K1)')
    assert out == "=IF('Settings'!$S$27>0,\"ROI_01_K1 UNIT_A\",'Settings'!$T$4*'Settings'!$S$27)"
    assert writer._direct("=ROI_01_TCR+ROI_01_K10") == "=ROI_01_TCR+ROI_01_K10"  # unknown names stay


@pytest.mark.parametrize("eps, note", [
    (0.904, "Inside the limits: the best match lies between the lowest candidates"),
    (0.976, "Inside the limits: the best match lies between the highest candidates"),
    (0.85, "The best value is the lowest emissivity allowed; the best match may lie below it. There the zones "
           "read colder"),  # cold surroundings: at 0.90 a surface of 0.85 reads cold
    (1.0, "The best value is the highest emissivity allowed; the best match may lie above it. There the zones "
          "read hotter"),
])
def test_z7_a_limit_note_needs_the_error_to_keep_falling_beyond_it(tmp_path, eps, note) -> None:
    match = _Match(_match_with(tmp_path, f"edge_{eps}.xlsx", eps={"Cell 3": eps, "Cell 6": eps}), 2)
    assert match.best == pytest.approx(min(max(round(eps, 2), 0.90), 0.98))
    assert match.note.startswith(note)


def test_z7_each_roi_keeps_its_own_probe_and_own_value(tmp_path) -> None:
    """The per-ROI error functions outlive their loop: each must keep its own ROI's data."""
    path = _match_with(tmp_path, "two.xlsx", eps={"Cell 3": 0.86, "Cell 6": 0.70})
    match = _Match(path, 2)
    assert match.pairs["Cell 3"][7] == pytest.approx(0.86) and match.pairs["Cell 6"][7] == pytest.approx(0.70)
    ws = openpyxl.load_workbook(path, data_only=True)[MATCH]
    h0 = 13 + MATCH_CANDIDATES + 1  # Excel row of the helper label
    minus = [ws.cell(h0 + 1, 14 + k).value for k in range(2)]
    plus = [ws.cell(h0 + 2, 14 + k).value for k in range(2)]
    assert minus[0] != pytest.approx(minus[1]) and plus[0] != pytest.approx(plus[1])
    assert plus[0] > minus[0] and plus[1] > minus[1]  # both want less than 0.90
    assert ws.cell(h0 + 4, 12).value == pytest.approx(0.05) and ws.cell(h0 + 3 + 96, 12).value == pytest.approx(1.0)
    bias = [ws.cell(h0 + 3, 14 + k).value for k in range(2)]
    assert bias[0] < 0 and bias[1] < 0  # both truly below 0.90: at 0.90 they read cold (20 °C surroundings)
    formulas = openpyxl.load_workbook(path)[MATCH]
    assert formulas.row_dimensions[h0 + 1].hidden and formulas.row_dimensions[h0 + 50].hidden


def test_z8_repeated_mapped_names_are_refused_in_every_recording(tmp_path) -> None:
    import dataclasses

    from flir_player.workbook import write_workbook as write

    source, temps = _zone_source()
    shapes = list(source.rois)
    shapes[1] = dataclasses.replace(shapes[1], shape=dataclasses.replace(shapes[1].shape, name="Cell 3"))
    twice = dataclasses.replace(source, rois=shapes, label="Camera B")
    data = ExportData(times=np.arange(60, dtype=float), sources=[twice],
                      options=ExportOptions(area_means="signal", tc_prefill=_prefill(temps)),
                      created=datetime(2026, 10, 10), versions={"tool": "test", "sdk": ""})
    with pytest.raises(ValueError, match="Camera B: more than one ROI is named Cell 3"):
        write(tmp_path / "twice.xlsx", data)


def test_r1_hot_surroundings_reverse_the_direction_and_the_note_follows_the_residuals(tmp_path) -> None:
    """Reflected 500 °C above zones at ~200 °C: a higher emissivity makes the IR hotter, not colder."""
    hot = PARAMS.with_(reflected_k=773.15)
    temps = {f"Cell {k + 1}": 190.0 + 20.0 * np.linspace(0, 1, 60) for k in range(6)}
    source, temps = _zone_source(eps={"Cell 3": 0.85, "Cell 6": 0.85}, params=hot, temps=temps)
    match = _Match(_write(tmp_path, source, _prefill(temps)), 2)
    assert match.best == pytest.approx(0.90)
    assert match.note.startswith("The best value is the lowest emissivity allowed") and "read hotter" in match.note
    assert match.pairs["Cell 3"][6] > 0  # bias at Settings' 0.95: hotter too


def test_r2_hidden_helper_rows_never_cover_the_pairs_table_or_the_chart(tmp_path) -> None:
    from flir_player.workbook import CHART_ROWS

    sources = [_zone_source(zones=18, label=f"Camera {k}")[0] for k in (1, 2, 3)]
    data = ExportData(times=np.arange(60, dtype=float), sources=sources, options=ExportOptions(area_means="signal"),
                      created=datetime(2026, 10, 10), versions={"tool": "test", "sdk": ""})
    write_workbook(tmp_path / "many.xlsx", data)
    ws = openpyxl.load_workbook(tmp_path / "many.xlsx")[MATCH]
    p0, n = 13, 54
    assert [ws.cell(p0 + k, 1).value for k in (0, n - 1)] == ["Cell 1", "Cell 18"]
    visible = range(1, p0 + n + 1 + CHART_ROWS)
    assert not any(ws.row_dimensions[r].hidden for r in visible)
    hidden = [r for r in range(1, ws.max_row + 1) if ws.row_dimensions[r].hidden]
    assert hidden and min(hidden) > p0 + n + CHART_ROWS and len(hidden) == 4 + 96
