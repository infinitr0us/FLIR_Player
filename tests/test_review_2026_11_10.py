# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regressions for the review of 0.6.0..0.10.0 (local/notes/2026-11-10-review-v060-v0100/REVIEW.md).

f1 mean-signal areas: Max, Min and percentiles are in or out of range by their own counts;
f2 a TC is looked up by its name before any number; f3 an added recording's ROI set must fit its image;
f4 only From/To the dialog derived move onto a superframing recording's clock; f5 the Emissivity Match scores
every candidate on the same rows; f6 no fit where no emissivity gives a temperature; r1 the fit tries the
neighbouring whole hours when nothing matches near the clocks; r2 logger time 0 at the ignition frame.
"""
from __future__ import annotations

import dataclasses
import math
from datetime import datetime, timedelta
from types import SimpleNamespace

import flir_player  # noqa: F401  (the SDK DLL directory before numpy)
import numpy as np
import pytest

openpyxl = pytest.importorskip("openpyxl")

from flir_player import excel_dialog, tc_dialog, tcmatch  # noqa: E402
from flir_player.excel_export import ExportOptions, SourceSpec, TcPrefill, roi_status, run_export  # noqa: E402
from flir_player.models import RoiShape  # noqa: E402
from flir_player.radiometry import KELVIN, MeasurementParameters, object_temperature  # noqa: E402
from flir_player.tcdata import TcTable  # noqa: E402
from flir_player.tcmatch import CLOCK_WINDOW_S, MatchOptions, clock_estimate, fit_eps  # noqa: E402
from flir_player.workbook import CLAMP_BLANK, _Writer  # noqa: E402

from conftest import wait_until  # noqa: E402
from test_emissivity_match import PARAMS as MATCH_PARAMS, _Match, _prefill, _write as _write_zones, _zone_source  # noqa: E402
from test_radiometry import T650  # noqa: E402
from test_tcmatch import make_scene  # noqa: E402
from test_v080_tc_clocks import (  # noqa: E402,F401  (fixtures)
    isolated_settings, lab_file, load, make_dialog, table as clock_table)
from test_v090_a8303 import _A8303Like, _export as _export_a8303, ats  # noqa: E402,F401
from test_v091_validation import _areas, _write  # noqa: E402
from test_v0_10_presets import _Superframing  # noqa: E402


def _data(path, formulas=False):
    """(header cells of row 5, sample rows) of the Data sheet."""
    sheet = openpyxl.load_workbook(path, data_only=not formulas)["Data"]
    head = [cell.value for cell in sheet[5]]
    return head, [row for row in sheet.iter_rows(min_row=6, values_only=True) if row[0] is not None]


# --- f1: mean-signal areas ----------------------------------------------------------------------------------


def test_f1_extremes_of_a_mean_signal_area_are_blank_by_their_own_counts(tmp_path, ats) -> None:  # noqa: F811
    """A box over 500 to 795 K at the 250-600 °C preset (523 to 873 K): its mean is in range (Status OK), its
    coldest pixel is not. The minimum used to show (about 230 °C, far off on this camera) under "Leave them blank"."""
    pytest.importorskip("fnv")
    out = tmp_path / "mean.xlsx"
    box = RoiShape(1, "rect", ((0.0, 0.0), (8.0, 4.0)), "Box")
    run_export(out, [SourceSpec(ats, (box,), ignition_frame=0, preset=1)],
               ExportOptions(step_s=1.0, area_means="signal", extremes=True, extra_stats=("p95",)),
               handles={ats.resolve(): _Superframing()}, replace=[out])
    assert openpyxl.load_workbook(out)["Settings"]["B5"].value == CLAMP_BLANK
    head, rows = _data(out)
    col = {name.split(" (")[0]: k for k, name in enumerate(head) if name}
    assert rows and all(row[col["Status"]] == "OK" for row in rows)
    assert all(isinstance(row[col["Mean"]], float) and isinstance(row[col["Max"]], float) for row in rows)
    assert all(isinstance(row[col["P95"]], float) for row in rows)
    assert all(row[col["Min"]] == "#N/A" for row in rows)  # below the preset's range, whatever the mean
    assert all(row[col["Spread"]] == "#N/A" for row in rows)  # of a blank extreme: blank
    _head, formulas = _data(out, formulas=True)
    assert '<>"OK"' in formulas[0][col["Mean"]] and "'Counts'!" in formulas[0][col["Mean"]]
    minimum = formulas[0][col["Min"]]
    assert '="Saturated"' in minimum and '<>"OK"' not in minimum and minimum.count("'Counts'!") >= 3


def test_f1_other_areas_keep_their_row_status(tmp_path, ats) -> None:  # noqa: F811
    """An area of pixel temperatures is still blank as a whole where one pixel is out of range."""
    pytest.importorskip("fnv")
    out = tmp_path / "pixels.xlsx"
    box = RoiShape(1, "rect", ((0.0, 0.0), (8.0, 4.0)), "Box")
    run_export(out, [SourceSpec(ats, (box,), ignition_frame=0, preset=1)],
               ExportOptions(step_s=1.0, extremes=True), handles={ats.resolve(): _Superframing()}, replace=[out])
    head, rows = _data(out)
    col = {name.split(" (")[0]: k for k, name in enumerate(head) if name}
    assert all(row[col["Status"]] == "Below range" for row in rows)
    assert all(row[col[name]] == "#N/A" for row in rows for name in ("Mean", "Max", "Min"))
    _head, formulas = _data(out, formulas=True)
    assert all('<>"OK"' in formulas[0][col[name]] for name in ("Max", "Min"))


def test_f1_the_grey_format_follows_each_statistics_own_count(tmp_path) -> None:
    source = _areas(11)
    box = source.rois[1]
    low = T650.count_limits()["calibrated"][0]
    box.counts["min"] = np.where(np.isfinite(box.counts["min"]), low - 500.0, np.nan)  # cold pixels, mean in range
    box.status = np.where(np.isfinite(box.counts["mean"]), roi_status(T650, "mean", box.counts), -1).astype(np.int8)
    path = _write(tmp_path, [source], area_means="signal", extremes=True)
    head, _rows = _data(path)
    rules = {}
    for area in openpyxl.load_workbook(path)["Data"].conditional_formatting:
        column = next(iter(area.sqref.ranges)).min_col - 1
        rules.setdefault(head[column], []).extend(formula for rule in area.rules for formula in rule.formula)
    by_count = [name for name, formulas in rules.items() if any("'Counts'!" in f for f in formulas)]
    assert sorted(set(name.split(" (")[0] for name in by_count)) == ["Max", "Min", "Spread"]
    assert rules and all(not any("'Counts'!" in f for f in formulas) for name, formulas in rules.items()
                         if name.startswith("Mean"))
    # this workbook has no "Leave them blank": its formulas are as before
    _head, rows = _data(path, formulas=True)
    assert all("CLAMP_MODE" not in str(cell) and "Leave them blank" not in str(cell) for cell in rows[0])


# --- f2: TC names --------------------------------------------------------------------------------------------


def _table(names) -> TcTable:
    t = np.arange(0.0, 400.0)
    values = np.column_stack([100.0 + 100.0 * k + 0.5 * t for k in range(len(names))])
    return TcTable(times=t, values=values, names=tuple(names))


def test_f2_a_name_comes_before_a_number() -> None:
    cells = _table(("3", "6", "9"))  # TCs headed by their cell numbers
    assert [cells.index(key) for key in ("3", "6", "9")] == [0, 1, 2]
    assert cells.index(2) == 1 and cells.index("1") == 0  # an int, or digits no channel is named: a number
    swapped = _table(("2", "1", "3"))
    assert [swapped.names[swapped.index(key)] for key in ("1", "2", "3")] == ["1", "2", "3"]
    with pytest.raises(KeyError):
        cells.index("12")


def test_f2_names_alike_but_for_capitals_are_their_own() -> None:
    twins = _table(("T1", "t1", "Wall"))
    assert twins.index("T1") == 0 and twins.index("t1") == 1 and twins.index("wall") == 2
    with pytest.raises(KeyError, match="More than one TC channel"):
        _table(("Ab", "aB")).index("AB")  # neither exactly: no guess
    assert tcmatch.resolve_pixels({"t1": (4, 5)}, twins.names) == {"t1": (4, 5)}
    assert tcmatch.resolve_pixels({"AB": (4, 5)}, ("Ab", "aB")) == {}


def test_f2_the_fit_reads_the_channels_ticked_and_every_column_by_default() -> None:
    table = _table(("2", "1", "3"))
    _grid, chosen = tcmatch.prepare_channels(table, MatchOptions(channels=("1",)), 1.0)
    assert [(c.name, c.index, c.values[0]) for c in chosen] == [("1", 1, 200.0)]
    _grid, every = tcmatch.prepare_channels(_table(("3", "6", "9")), MatchOptions(), 1.0)
    assert [c.name for c in every] == ["3", "6", "9"]


def test_f2_the_workbook_gets_each_tcs_own_readings() -> None:
    table = _table(("2", "Other"))
    channels = [SimpleNamespace(name=name, eps=None, events=[], match=SimpleNamespace(found=True, pixel=(1, 1 + 4 * k)))
                for k, name in enumerate(table.names)]
    result = SimpleNamespace(channels=channels, preset=None, fps=10.0, lag_s=5.0, ignition_frame=50,
                             num_frames=1000, origin_s=None, step=1.0)
    rois = (RoiShape(1, "rect", ((0.0, 0.0), (3.0, 3.0)), "Cell 1"), RoiShape(2, "rect", ((4.0, 0.0), (7.0, 3.0)), "Cell 2"))
    prefill = tcmatch.tc_prefill(result, table, rois=rois, size=(30, 20))
    assert prefill.names == ("2", "Other")
    assert prefill.columns[0][0] == 100.0 and prefill.columns[1][0] == 200.0  # "2" is the first column, not the second
    pairs = tcmatch.clock_prefill(_table(("2", "1")), lag_s=0.0, fps=10.0, frame=0, pairs={"2": "Cell 1"})
    assert pairs.names[0] == "2" and pairs.columns[0][0] == 100.0 and dict(pairs.roi_use)["Cell 1"]


# --- f3: an added recording's ROI set ----------------------------------------------------------------------


def test_f3_an_roi_set_saved_for_another_image_size_is_refused(tmp_path, ats) -> None:  # noqa: F811
    pytest.importorskip("fnv")
    rois = (RoiShape(1, "cursor", ((2.5, 2.5),), "Spot"),)
    out = tmp_path / "size.xlsx"
    with pytest.raises(ValueError, match="saved for a 16 × 12 image, but the recording is 8 × 8"):
        run_export(out, [SourceSpec(ats, rois, ignition_frame=0, preset=1, roi_size=(16, 12))],
                   ExportOptions(step_s=5.0), handles={ats.resolve(): _Superframing()}, replace=[out])
    assert not out.exists()
    message = run_export(out, [SourceSpec(ats, rois, ignition_frame=0, preset=1, roi_size=(8, 8))],
                         ExportOptions(step_s=5.0), handles={ats.resolve(): _Superframing()}, replace=[out])
    assert "1 ROIs" in message


def test_f3_the_dialog_passes_an_added_recordings_set_size(make_dialog, tmp_path) -> None:  # noqa: F811
    dialog = make_dialog()
    other = tmp_path / "other.csq"
    rois = (RoiShape(1, "rect", ((0.0, 0.0), (4.0, 4.0)), "Box"),)
    dialog._others.append({"path": other, "rois": rois, "ignition": 7, "size": (640, 480)})
    dialog._add_row(other, "other", 1, ignition=7)
    sources = dialog.parameters()["sources"]
    assert "roi_size" not in sources[0]  # the open recording's ROIs were drawn on it
    assert sources[1]["roi_size"] == (640, 480) and sources[1]["ignition_frame"] == 7


# --- f4: From and To on a superframing recording ---------------------------------------------------------------


def test_f4_bounds_typed_by_hand_stay_where_typed(tmp_path, ats) -> None:  # noqa: F811
    """The recording's clock gains 4 s after frame 60: the TC readings move onto it, From 10 / To 20 do not."""
    pytest.importorskip("fnv")
    prefill = TcPrefill(times=(0.0, 1.0, 2.0), columns=((20.0, 21.0, 22.0),), names=("T1",),
                        roi_tc=(("Cell 1", "T1"),), offset_s=-30.0, frame=80, fps=2.0)
    options = ExportOptions(step_s=1.0, start_s=10.0, end_s=20.0, tc_prefill=prefill)
    book = _export_a8303(tmp_path, ats, _A8303Like(clock_gap=4.0), preset=1, ignition=80, options=options)
    offset = next(row[1] for row in book["TC Compare"].iter_rows(values_only=True)
                  if row[0] == "Logger time offset (s)")
    assert offset == pytest.approx(-34.0)
    times = [row[0] for row in book["Data"].iter_rows(min_row=6, values_only=True) if row[0] is not None]
    assert (times[0], times[-1]) == (10, 20)
    # one bound derived, the other typed: only the derived one moves
    mixed = dataclasses.replace(prefill, bounds=(10.0, None))
    book = _export_a8303(tmp_path, ats, _A8303Like(clock_gap=4.0), preset=1, ignition=80,  # replaces the first
                         options=dataclasses.replace(options, tc_prefill=mixed))
    times = [row[0] for row in book["Data"].iter_rows(min_row=6, values_only=True) if row[0] is not None]
    assert (times[0], times[-1]) == (6, 20)


def test_f4_the_dialog_marks_the_bounds_it_derived(qapp, make_dialog, lab_file) -> None:  # noqa: F811
    dialog = make_dialog()
    load(qapp, dialog, lab_file)
    assert (dialog.start_spin.value(), dialog.end_spin.value()) == (0.0, 39.0)
    assert dialog.options().tc_prefill.bounds == (0.0, 39.0)
    dialog.end_spin.setValue(30.0)  # typed by hand
    assert dialog.options().tc_prefill.bounds == (0.0, None)
    dialog.start_check.setChecked(False)
    assert dialog.options().tc_prefill.bounds == (None, None)
    dialog.start_check.setChecked(True)
    dialog.ignition_spin.setValue(130)  # another time 0: the numbers left are no longer the logger's span
    assert dialog.options().tc_prefill.bounds == (None, None)


# --- f5: the Emissivity Match scores every candidate on the same rows ------------------------------------------


def test_f5_a_candidate_that_leaves_rows_without_a_temperature_is_not_scored(tmp_path) -> None:
    """Hot surroundings (reflected 1000 K): below the true 0.94 the cold rows have no temperature, and a
    candidate scored on the hot rows alone could win. Such candidates are #N/A now."""
    params = MATCH_PARAMS.with_(reflected_k=1000.0)
    source, temps = _zone_source(params=params)
    prefill = _prefill(temps)
    match = _Match(_write_zones(tmp_path, source, prefill), zones=2)
    cell3 = next(roi for roi in source.rois if roi.shape.name == "Cell 3")
    counts = cell3.counts["mean"]
    at = {eps: np.isfinite(object_temperature(T650, params.with_(emissivity=eps), counts)).sum() for eps in (0.90, 0.98)}
    assert 0 < at[0.90] < at[0.98] == counts.size  # the test's premise: 0.90 loses the cold rows
    assert match.eps[0] == pytest.approx(0.90) and match.combined[0] == "#N/A"
    assert match.best == pytest.approx(0.94) and match.best_error < 0.01
    assert match.pairs["Cell 3"][3] == at[0.98]  # Rows used: the rows every scored candidate covers
    formula = match.formulas.cell(match.c0, 14).value.text  # the first candidate of the first ROI (an array formula)
    assert "<>'Emissivity Match'!$N$" in formula and formula.count("_FITR=1") == 2  # its rows against those scored
    hidden = [row for row in match.ws.iter_rows(values_only=True) if row[11] and str(row[11]).startswith("Rows scored")]
    assert len(hidden) == 2 and hidden[0][13] == at[0.98]


def test_f5_a_perfect_fit_on_half_the_rows_no_longer_wins(tmp_path) -> None:
    params = MATCH_PARAMS.with_(reflected_k=1000.0, transmission=1.0)
    source, _temps = _zone_source(rows=2, zones=1, params=params)
    roi = source.rois[0]
    tc = np.array([200.0, 426.85])
    # row 2 sits exactly on 0.90; row 1 has no temperature there, and reads far from its TC at any emissivity
    hot = (0.90 * T650.planck.signal(tc[1] + KELVIN) + 0.10 * T650.planck.signal(1000.0))
    roi.counts = {"mean": np.array([8721.48, hot]), "max": np.array([8736.48, hot + 15]),
                  "min": np.array([8706.48, hot - 15])}
    roi.status = roi_status(T650, "mean", roi.counts)
    assert not np.any(roi.status)  # both rows inside the calibrated range
    kelvin = object_temperature(T650, params.with_(emissivity=0.90), roi.counts["mean"])
    assert math.isnan(kelvin[0]) and kelvin[1] - KELVIN == pytest.approx(tc[1], abs=1e-6)
    prefill = TcPrefill(times=(0.0, 1.0), columns=(tuple(tc),), names=("T1",), roi_tc=(("Cell 1", "T1"),))
    match = _Match(_write_zones(tmp_path, source, prefill, name="half.xlsx"), zones=1)
    assert match.combined[0] == "#N/A"  # 0.90: a 0 K error on one row of two
    assert match.best > 0.90 and match.best_error > 10.0 and match.pairs["Cell 1"][3] == 2


# --- f6: no fit where no emissivity gives a temperature ------------------------------------------------------


def test_f6_no_candidate_with_a_temperature_is_no_fit() -> None:
    tc = np.linspace(150.0, 400.0, 60)
    counts = 0.8 * T650.planck.signal(tc + KELVIN) + 0.2 * T650.planck.signal(293.15)
    sane = fit_eps(T650, MeasurementParameters(), counts, tc)
    assert sane.eps == pytest.approx(0.8) and sane.rmse_k < 0.5
    hot_window = MeasurementParameters(window_transmission=0.1, window_k=773.15)
    assert not np.isfinite(object_temperature(T650, hot_window, counts)).any()
    lost = fit_eps(T650, hot_window, counts, tc)
    assert lost.n == 60 and math.isnan(lost.eps) and math.isnan(lost.rmse_k) and not lost.at_bound


def test_f6_such_an_event_gives_its_tc_no_emissivity() -> None:
    samples, table = make_scene(frames_per_bin=1)  # too few frames per bin for the flicker rule
    samples.params = MeasurementParameters(window_transmission=0.1, window_k=773.15)
    result, _location, _series = tcmatch.analyse(samples, table, MatchOptions(spot=1))
    events = [e for ch in result.channels for e in ch.events]
    assert events and all(math.isnan(e.fit.eps) and not e.physical and not e.used for e in events)
    assert all(ch.eps is None for ch in result.channels) and result.common_eps is None and result.pooled is None
    text = tcmatch.summary_text(result)
    assert "no fit: no emissivity from 0 to 1 explains the IR" in text and "emissivity 0.02" not in text


# --- r1: whole hours between the clocks --------------------------------------------------------------------


def test_r1_the_other_whole_hours_are_kept_with_the_estimate() -> None:
    start = datetime(2026, 10, 6, 14)
    log = np.arange(0.0, 7200.0)
    late = clock_estimate(start - timedelta(minutes=50), start, log, 5400.0)  # logger time 0 50 min before
    assert late.lag_s == 600.0 and late.hours == 1  # the nearest to the recording's start: an hour off
    assert [(other.lag_s, other.hours) for other in late.others][0] == (-3000.0, 0)
    assert all(not other.others for other in late.others) and "1 h" not in late.others[0].note
    near = clock_estimate(start + timedelta(minutes=5), start, np.arange(0.0, 600.0), 900.0)
    assert near.lag_s == 300.0 and near.others == ()  # no other hour puts the log inside the recording


def test_r1_a_fit_tries_the_neighbouring_hours_when_nothing_matches_near_the_clocks() -> None:
    """Logger time 0 is 3,737 s into the recording and the clocks agree, but the estimate takes the hour nearest
    the recording's start: 137 s. Nothing matches there; an hour later the TCs do."""
    lag = 3737
    samples, table = make_scene(lag=lag, n_ir=4300)
    start = datetime(2026, 1, 1, 12)
    samples.info["clock_start"] = start
    table = TcTable(times=table.times, values=table.values, names=table.names, source=table.source,
                    clock_zero=start + timedelta(seconds=lag), clock_note="logging started (test)")
    estimate = clock_estimate(table.clock_zero, start, table.times, 4299.0)
    assert estimate.lag_s == 137.0 and [o.lag_s for o in estimate.others] == [3737.0]
    result, _location, _series = tcmatch.analyse(samples, table, MatchOptions(spot=1, clock_window_s=CLOCK_WINDOW_S))
    assert result.lag_s == float(lag) and result.near_clocks
    assert result.clock_lag_s == float(lag) and result.clock_frame == lag * 30
    found = [ch.name for ch in result.channels if ch.match.found]
    assert found == ["TC A", "TC B"]
    warning = next(w for w in result.warnings if w.startswith("No TC was found within 120 s of 137 s"))
    assert "1 h later, at 3737 s, the TCs match" in warning and "half an hour" in warning
    assert not any("nor whole hours" in w for w in result.warnings)


def test_r1_no_match_at_any_hour_still_never_searches_everything() -> None:
    samples, table = make_scene(lag=2000, n_ir=4300)  # the truth is at no whole hour from the clocks
    start = datetime(2026, 1, 1, 12)
    samples.info["clock_start"] = start
    table = TcTable(times=table.times, values=table.values, names=table.names, source=table.source,
                    clock_zero=start + timedelta(seconds=3737), clock_note="logging started (test)")
    result, _location, _series = tcmatch.analyse(samples, table, MatchOptions(spot=1, clock_window_s=CLOCK_WINDOW_S))
    assert not any(ch.match.found for ch in result.channels) and result.clock_lag_s == 137.0
    assert any("(nor whole hours earlier or later)" in w for w in result.warnings)


# --- r2: logger time 0 at the ignition frame ----------------------------------------------------------------


def _choose(dialog, mode: str) -> None:
    dialog.tc_zero_combo.setCurrentIndex(dialog.tc_zero_combo.findData(mode))


def test_r2_logger_time_0_at_the_ignition_frame_moves_the_readings_and_use(qapp, make_dialog, lab_file) -> None:  # noqa: F811
    dialog = make_dialog()
    load(qapp, dialog, lab_file)
    assert dialog.tc_zero_combo.isEnabled() and dialog.tc_zero_combo.currentData() == "clock"
    dialog.ignition_spin.setValue(144)  # by the clocks: time 0 moves, the readings stay where the clocks put them
    by_clocks = dialog.options().tc_prefill
    assert by_clocks.offset_s == pytest.approx(-2.0) and by_clocks.frame == 143
    assert dict(by_clocks.roi_use)["Cell 3"] == ((pytest.approx(-2.5), pytest.approx(37.5)),)
    _choose(dialog, "frame")
    by_frame = dialog.options().tc_prefill
    assert by_frame.offset_s == 0 and by_frame.frame is None and by_frame.fps is None
    assert dict(by_frame.roi_use)["Cell 3"] == ((-0.5, 39.5),)  # Use sits on the readings it was made from
    assert (dialog.start_spin.value(), dialog.end_spin.value()) == (0.0, 39.0)
    text = dialog.tc_label.text()
    assert "Logger time 0 is the ignition frame (frame 144), as chosen." in text
    assert "The clocks put it at frame 124, 2.0 s earlier." in text
    dialog.ignition_spin.setValue(124)
    assert "The clocks put it at frame 124, the same frame." in dialog.tc_label.text()
    _choose(dialog, "clock")
    assert dialog.ignition_spin.value() == 124 and dialog.options().tc_prefill.frame == 123


def test_r2_the_choice_is_kept_for_the_recording_and_its_tc_file(qapp, make_dialog, lab_file, isolated_settings) -> None:  # noqa: F811
    dialog = make_dialog()
    load(qapp, dialog, lab_file)
    _choose(dialog, "frame")
    dialog.ignition_spin.setValue(151)
    dialog.reject()
    assert excel_dialog.remembered_tc_zero(dialog.metadata.path) is None  # only an export keeps it
    dialog.accept()
    assert excel_dialog.remembered_tc_zero(dialog.metadata.path) == 150
    again = make_dialog()
    assert wait_until(qapp, lambda: not again._tc_reading and again._tc_table is not None)
    assert again.tc_zero_combo.currentData() == "frame" and again.ignition_spin.value() == 151
    assert again.options().tc_prefill.offset_s == 0
    _choose(again, "clock")
    assert again.ignition_spin.value() == 124
    again.accept()
    assert excel_dialog.remembered_tc_zero(dialog.metadata.path) is None
    third = make_dialog()
    assert wait_until(qapp, lambda: not third._tc_reading and third._tc_table is not None)
    assert third.tc_zero_combo.currentData() == "clock" and third.ignition_spin.value() == 124


def test_r2_without_a_clock_only_the_ignition_frame_can_be_logger_time_0(qapp, make_dialog, lab_file,  # noqa: F811
                                                                           monkeypatch) -> None:
    data = dataclasses.replace(clock_table(), clock_zero=None)
    monkeypatch.setattr(tc_dialog, "read_tc_table", lambda *a, **k: data)
    dialog = make_dialog()
    load(qapp, dialog, lab_file)
    assert not dialog.tc_zero_combo.isEnabled() and dialog.tc_zero_combo.currentData() == "frame"
    assert "taken as logger time 0" in dialog.tc_label.text()
    assert dialog.options().tc_prefill.offset_s == 0 and dialog.options().tc_prefill.frame is None


# --- second round: the review of these fixes (Codex, 2026-11-10) ------------------------------------------------


def test_s1_all_tcs_is_scored_only_where_every_tc_is(tmp_path) -> None:
    """Cell 1 (true 0.94, 20 to 520 °C) has no score at 0.90 under hot surroundings; cell 2 (true 0.90) fits it
    exactly. "All TCs" took cell 2's 0 K alone at 0.90 and chose it; a candidate must cover every TC."""
    params = MATCH_PARAMS.with_(reflected_k=1000.0)
    t = np.arange(60, dtype=np.float64)
    temps = {"Cell 1": 20.0 + 500.0 * np.clip(t / 40.0, 0.0, 1.0), "Cell 2": np.full(60, 500.0)}
    source, temps = _zone_source(params=params, temps=temps, eps={"Cell 1": 0.94, "Cell 2": 0.90})
    prefill = _prefill(temps, pairs=(("Cell 1", "T1"), ("Cell 2", "T2")))
    match = _Match(_write_zones(tmp_path, source, prefill, name="two.xlsx"), zones=2)
    first = [match.ws.cell(match.c0, column).value for column in (13, 14, 15)]  # 0.90: all TCs, cell 1, cell 2
    assert first[1] == "#N/A" and first[2] == pytest.approx(0.0, abs=0.01)
    assert first[0] == "#N/A"  # not cell 2's alone
    assert match.best > 0.90 and match.best_error > 1.0
    assert match.pairs["Cell 1"][3] == 60 and match.pairs["Cell 2"][3] == 60
    together = match.formulas.cell(match.c0, 13).value.text
    assert "<MATCH_PARTS" in together


def test_s2_a_preset_pick_is_exact_beside_a_huge_constant() -> None:
    """SMAX is 1e300 for a curve with F >= 1 ("no limit"); the pick of another preset's finite SMAX as a
    difference from it came out as 0."""
    layout = SimpleNamespace(presets=(1, 2, 3))
    pick = _Writer._pick(layout, "SRC_1_SMAX", "P")
    assert pick == "(SRC_1_SMAX*(1-(P=2)-(P=3))+SRC_1_SMAXP2*(P=2)+SRC_1_SMAXP3*(P=3))"
    values = {"SRC_1_SMAXP2": "1213246.135", "SRC_1_SMAXP3": "77.5", "SRC_1_SMAX": "1e300"}

    def at(preset):
        expression = pick.replace("=", "==")
        for name, value in values.items():  # longest names first: SRC_1_SMAX is a prefix of the others
            expression = expression.replace(name, value)
        return eval(expression, {"P": preset})  # noqa: S307  (numbers and comparisons only)

    assert at(1) == 1e300 and at(2) == 1213246.135 and at(3) == 77.5 and at(None) == 1e300
    assert _Writer._pick(SimpleNamespace(presets=()), "SRC_1_SMAX", "P") == "SRC_1_SMAX"
    assert _Writer._pick(layout, "SRC_1_SMAX", None) == "SRC_1_SMAX"


def test_s3_a_window_for_an_ambiguous_tc_name_is_an_error_not_no_window() -> None:
    twins = _table(("Ab", "aB"))
    with pytest.raises(ValueError, match="names more than one TC"):
        tcmatch.prepare_channels(twins, MatchOptions(valid={"AB": ((10.0, 20.0),)}), 1.0)
    _grid, channels = tcmatch.prepare_channels(twins, MatchOptions(valid={"aB": ((10.0, 20.0),)}), 1.0)
    assert [int(c.usable.sum()) for c in channels] == [400, 11]  # the exact name: its own window only
    _grid, channels = tcmatch.prepare_channels(twins, MatchOptions(valid={"Zz": ((10.0, 20.0),)}), 1.0)
    assert [int(c.usable.sum()) for c in channels] == [400, 400]  # a name the table lacks: as before, skipped
    # a start that several names share is ambiguous too (it was skipped like a missing name); one name's is not
    pair = _table(("TC A", "TC B", "Wall"))
    with pytest.raises(ValueError, match="names more than one TC"):
        tcmatch.prepare_channels(pair, MatchOptions(valid={"TC": ((10.0, 20.0),)}), 1.0)
    _grid, channels = tcmatch.prepare_channels(pair, MatchOptions(valid={"Wa": ((10.0, 20.0),)}), 1.0)
    assert [int(c.usable.sum()) for c in channels] == [400, 400, 11]
    with pytest.raises(KeyError):
        pair.index("TC")


def _hours_scene(lag: int, clock_lag: int):
    samples, table = make_scene(lag=lag, n_ir=4300)
    start = datetime(2026, 1, 1, 12)
    samples.info["clock_start"] = start
    table = TcTable(times=table.times, values=table.values, names=table.names, source=table.source,
                    clock_zero=start + timedelta(seconds=clock_lag), clock_note="logging started (test)")
    samples.counts[:900] = 0  # no frames where the nearest whole hour puts the log (137 s)
    samples.mean[:900] = np.nan
    return samples, table


def test_s4_a_window_without_frames_goes_to_the_other_hours_before_the_whole_recording() -> None:
    samples, table = _hours_scene(lag=3737, clock_lag=3737)
    result, _location, _series = tcmatch.analyse(samples, table, MatchOptions(spot=1, clock_window_s=CLOCK_WINDOW_S))
    assert result.lag_s == 3737.0 and result.clock_lag_s == 3737.0 and result.near_clocks
    warning = next(w for w in result.warnings if "1 h later, at 3737 s, the TCs match" in w)
    assert warning.startswith("Too little of the TC data falls on the recording's frames to match within 120 s of 137 s")
    assert not any("whole recording was searched" in w for w in result.warnings)


def test_s4_the_whole_recording_is_not_searched_while_another_hour_can_be() -> None:
    samples, table = _hours_scene(lag=2000, clock_lag=3737)  # the TCs match at 2000 s, at no whole hour
    result, _location, _series = tcmatch.analyse(samples, table, MatchOptions(spot=1, clock_window_s=CLOCK_WINDOW_S))
    assert not any(ch.match.found for ch in result.channels) and abs(result.lag_s - 2000.0) > 600.0
    assert not any("whole recording was searched" in w for w in result.warnings)
    assert any("(nor whole hours earlier or later)" in w for w in result.warnings)


def test_s5_a_remembered_frame_is_not_restored_over_what_the_user_set(qapp, make_dialog, lab_file) -> None:  # noqa: F811
    dialog = make_dialog()
    load(qapp, dialog, lab_file)
    _choose(dialog, "frame")
    dialog.ignition_spin.setValue(151)
    dialog.accept()
    assert excel_dialog.remembered_tc_zero(dialog.metadata.path) == 150
    again = make_dialog()  # its TC file is being read: meanwhile the filling is unticked and a frame typed
    again.tc_check.setChecked(False)
    again.ignition_spin.setValue(101)
    assert wait_until(qapp, lambda: not again._tc_reading and again._tc_table is not None)
    assert again.ignition_spin.value() == 101 and again.tc_zero_combo.currentData() == "clock"
    edited = make_dialog()  # only the frame typed: it stays too
    edited.ignition_spin.setValue(77)
    assert wait_until(qapp, lambda: not edited._tc_reading and edited._tc_table is not None)
    assert edited.tc_zero_combo.currentData() == "clock" and edited.ignition_spin.value() == 124  # by the clocks


def test_s5_a_frame_chosen_for_one_tc_file_does_not_carry_over_to_another(qapp, make_dialog, lab_file) -> None:  # noqa: F811
    dialog = make_dialog()
    load(qapp, dialog, lab_file)
    _choose(dialog, "frame")
    dialog.ignition_spin.setValue(151)
    dialog.accept()
    again = make_dialog()
    assert wait_until(qapp, lambda: not again._tc_reading and again._tc_table is not None)
    assert again.tc_zero_combo.currentData() == "frame" and again.ignition_spin.value() == 151
    other = lab_file.with_name("other.xlsx")
    other.write_bytes(lab_file.read_bytes())
    load(qapp, again, other)  # another logger file: its clocks place it, not the frame kept for the first
    assert again.tc_zero_combo.currentData() == "clock" and again.ignition_spin.value() == 124
    prefill = again.options().tc_prefill
    assert prefill.frame == 123 and prefill.offset_s == pytest.approx(0.0)
