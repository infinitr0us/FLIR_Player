# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""The whole-picture review of v0.5.2..v0.7.4 (2026-10-14, three Codex passes: engine, workbook and zones, GUI):
one test per confirmed finding (w = engine, x = workbook and zones, g = GUI)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from flir_player.calibration import sdk_values
from flir_player.excel_export import ExportOptions, SourceSpec, run_export
from flir_player.models import RoiShape
from flir_player.radiometry import NEUTRAL, MeasurementParameters, object_temperature
from test_radiometry import T650


class _AlternatingImager:
    """An SDK handle whose frames alternate two presets (two calibrations of the same scene), like the A8303."""
    width = height = 8
    num_frames = 323

    def __init__(self):
        import fnv
        self.unit, self.temp_type = fnv.Unit.COUNTS, fnv.TempType.KELVIN
        self.supported_units = [fnv.Unit.COUNTS, fnv.Unit.TEMPERATURE_FACTORY]
        self.object_parameters = NS(**sdk_values(NEUTRAL))
        self.source_info = NS(camera_model="Synthetic multi-preset", preset_info=[
            NS(available=True, calibrated=True, min_temp=373.15, max_temp=923.15, frame_rate_valid=True,
               frame_rate=30.0, num_frames=self.num_frames // 2 + k) for k in (1, 0)])
        self.cals = [T650, replace(T650, planck=replace(T650.planck, R=T650.planck.R * 1.8))]
        self.true_k = np.linspace(400, 650, 64).reshape(8, 8)
        self.get_frame(0)

    def reset_object_parameters(self):
        self.object_parameters = NS(**sdk_values(NEUTRAL))

    def get_frame(self, index):
        import fnv
        self.index = index
        self.frame_info = NS(time=datetime(2026, 10, 14) + timedelta(seconds=index / 30))
        cal = self.cals[index % 2]
        counts = cal.planck.signal(self.true_k)
        self.final = counts if self.unit == fnv.Unit.COUNTS else object_temperature(
            cal, MeasurementParameters.from_sdk(vars(self.object_parameters)), counts)
        self.status = np.zeros((8, 8), np.int8)

    def update_frame(self):
        self.get_frame(self.index)

    def has_unit(self, unit):
        return unit in self.supported_units

    def close(self):
        pass


def test_x1_the_excel_export_refuses_a_superframing_recording(tmp_path) -> None:
    pytest.importorskip("fnv")  # the fake handle uses the SDK's unit names
    path = tmp_path / "flames.ats"
    path.write_bytes(b"synthetic")
    im = _AlternatingImager()
    spec = SourceSpec(path, (RoiShape(1, "cursor", ((3.5, 3.5),), "Cell 1"),))
    with pytest.raises(ValueError, match="several presets"):
        run_export(tmp_path / "out.xlsx", [spec], ExportOptions(every_frame=True), handles={path.resolve(): im})
    assert not (tmp_path / "out.xlsx").exists()


# --- engine (w) ---------------------------------------------------------------------------------------


def _one_pixel(counts, params=None, step: float = 1.0):
    """Samples of a 1 x 1 region (pixel (0, 0)), one bin per ``step`` s, from its counts."""
    from flir_player.tcmatch import Samples
    from test_tcmatch import PARAMS, REPORT

    n = len(counts)
    per_bin = int(round(30 * step))
    return Samples(path=Path("synthetic.seq"), times=np.arange(n) * step,
                   mean=np.asarray(counts, dtype=np.float32)[:, None, None],
                   spread=np.zeros((n, 1, 1), np.float32), counts=np.full(n, per_bin, np.int32),
                   first_frame=np.arange(n, dtype=np.int64) * per_bin, origin=(0, 0), size=(1, 1), fps=30.0,
                   step=step, report=REPORT, params=params or PARAMS,
                   info={"frames": n * per_bin, "camera": "Synthetic", "region": [0, 0, 1, 1]})


def test_w1_an_emissivity_that_leaves_seconds_unexplained_does_not_win() -> None:
    from flir_player.tcmatch import fit_eps
    from test_tcmatch import A700, PARAMS, counts_for

    hot_room = PARAMS.with_(reflected_k=573.15)  # 300 °C reflected: a low emissivity explains nothing cold
    tc = np.r_[np.full(60, 100.0), np.full(60, 200.0)]
    counts = np.r_[counts_for(tc[:60], 0.8, hot_room), counts_for(tc[60:], 0.2, hot_room)]
    fit = fit_eps(A700, hot_room, counts, tc)
    shown = object_temperature(A700, hot_room.with_(emissivity=fit.eps), counts) - 273.15
    assert np.isfinite(shown).all()  # it was 0.2 with "RMSE 0" over the half it could explain
    assert fit.rmse_k > 1.0


def test_w2_a_broken_time_column_is_not_replaced_by_a_heating_tc() -> None:
    from flir_player.tcdata import parse_rows

    rows = [("Time [s]", "T1 [C]")] + [(float(i if i < 8 else -1), 100.0 + i) for i in range(10)]
    table = parse_rows(rows)  # T1 rises throughout; the time column runs past the log
    assert table.times.tolist() == list(range(8))
    assert table.values[:, 0].tolist() == [100.0 + i for i in range(8)]
    assert any("stops rising" in note for note in table.notes)
    # a column with a temperature unit is never the time axis, headed or not
    rows = [("t", "Oven (°C)")] + [(float(i if i < 8 else -1), 100.0 + i) for i in range(10)]
    assert parse_rows(rows).values[:, 0].tolist() == [100.0 + i for i in range(8)]


def test_w4_a_reflected_temperature_given_for_the_fit_goes_into_its_workbook(tmp_path, monkeypatch) -> None:
    from flir_player import tcmatch
    from flir_player.tcmatch import MatchOptions
    from test_tc_dialog import _fake_run, _write_csv
    from test_tcmatch import make_scene

    samples, table = make_scene()
    samples.info["frames"] = int(samples.counts.sum())
    _fake_run(monkeypatch, samples, table)
    seen = {}
    def fake_workbook(dest, *args, parameters=None, **kwargs):
        seen["p"] = parameters
        Path(dest).write_bytes(b"workbook")

    monkeypatch.setattr(tcmatch, "write_workbook", fake_workbook)
    recording = tmp_path / "synthetic.seq"
    recording.write_bytes(bytes(1000))
    tc = _write_csv(tmp_path / "tc.csv", ["A"], [[0, 20.0]])
    output = tcmatch.run_analysis(recording, tc, MatchOptions(spot=1, reflected_c=300.0), out_dir=tmp_path / "out",
                                  workbook=True, figure=False)
    assert seen["p"]["reflected_temp"] == pytest.approx(573.15)
    # kept with the run, so a workbook from the reopened fit converts the same way
    assert output.parameters["reflected_temp"] == pytest.approx(573.15)
    again = tcmatch.load_saved_run(tcmatch.saved_run(tmp_path / "out", recording))
    assert again.parameters["reflected_temp"] == pytest.approx(573.15) and again.parameters_known


def test_w5_a_pixel_given_for_t1_does_not_fix_t10() -> None:
    from flir_player import tcmatch
    from flir_player.tcdata import TcTable
    from flir_player.tcmatch import MatchOptions, resolve_pixels
    from test_tcmatch import make_scene

    samples, table = make_scene()
    both = TcTable(table.times, np.column_stack([table.values[:, 0]] * 2), ("T1", "T10"))
    result, _location, _series = tcmatch.analyse(samples, both, MatchOptions(spot=1, pixels={"T1": (9, 12)}))
    assert {ch.name: ch.match.fixed for ch in result.channels} == {"T1": True, "T10": False}
    # with only T10 chosen, the pixel given for T1 still means T1
    result, _location, _series = tcmatch.analyse(samples, both, MatchOptions(spot=1, channels=("T10",),
                                                                              pixels={"T1": (9, 12)}))
    assert [(ch.name, ch.match.fixed) for ch in result.channels] == [("T10", False)]
    assert resolve_pixels({"t1": (1, 2)}, ["T1", "T10"]) == {"T1": (1, 2)}  # any case
    assert resolve_pixels({"T": (1, 2)}, ["T1", "A"]) == {"T1": (1, 2)}  # a start only one name has
    assert resolve_pixels({"T": (1, 2)}, ["T1", "T2"]) == {}
    assert resolve_pixels({"2": (1, 2)}, ["T1", "T2"]) == {"T2": (1, 2)}  # by number
    assert resolve_pixels({"2": (5, 5), "t2": (1, 2)}, ["T1", "T2"]) == {"T2": (1, 2)}  # the name before a number


def _cache_files(folder: Path, key: str, **meta) -> None:
    np.save(folder / f"{key}_mean.npy", np.zeros((3, 1, 1), np.float32))
    np.save(folder / f"{key}_spread.npy", np.zeros((3, 1, 1), np.float32))
    np.savez(folder / f"{key}_meta.npz", times=np.arange(3.0), counts=np.ones(3), first_frame=np.arange(3),
             complete=True, **meta)


def test_w6_statistics_of_another_recording_with_the_same_name_size_and_time_are_not_reused(tmp_path) -> None:
    from flir_player.tcmatch import _load_cache

    _cache_files(tmp_path, "new", fingerprint=np.array("aaa"))
    assert _load_cache(tmp_path, "new", "aaa") is not None
    assert _load_cache(tmp_path, "new", "bbb") is None
    _cache_files(tmp_path, "old")  # a cache from before 0.7.5 has no fingerprint: still used
    assert _load_cache(tmp_path, "old", "bbb") is not None


def test_w8_tc_readings_headed_in_fahrenheit_or_kelvin_are_refused() -> None:
    from flir_player.tcdata import parse_rows

    body = [(float(i), 212.0 + i) for i in range(10)]
    for header in ("T1 (°F)", "T1 deg F", "T1 (kelvin)", "T1 [°K]", "T1 deg K"):
        with pytest.raises(ValueError, match="°F or kelvin"):
            parse_rows([("Time [s]", header)] + body)
    for header in ("TC (F)", "TC K", "Type K TC"):  # a position, a type-K thermocouple
        assert parse_rows([("Time [s]", header)] + body).values[0, 0] == 212.0


def test_w2_a_mostly_blank_time_column_is_not_replaced_by_a_heating_tc() -> None:
    from flir_player.tcdata import parse_rows

    rows = [("Time [s]", "T1", "T2")] + [(0.0 if i == 0 else None, 100.1 + i, 20.0 + (i % 3)) for i in range(10)]
    with pytest.raises(ValueError, match="time"):
        parse_rows(rows)  # T1 rises, but the header says column 1 is the time


def test_w9_a_fit_with_open_ended_windows_reopens() -> None:
    import json

    from flir_player import tcmatch
    from flir_player.tcdata import parse_windows
    from flir_player.tcmatch import MatchOptions, options_from_dict
    from test_tcmatch import make_scene

    samples, table = make_scene()
    windows = {"TC A": parse_windows("50-"), "TC B": parse_windows("-300, 320-")}
    result, _location, _series = tcmatch.analyse(samples, table, MatchOptions(spot=1, valid=windows,
                                                                               scope=(10.0, float("inf"))))
    options = options_from_dict(json.loads(json.dumps(result.to_dict()))["options"])
    assert options.valid == windows and options.scope == (10.0, float("inf"))


def test_w9_the_dialog_shows_open_window_ends_empty() -> None:
    from flir_player.tc_dialog import windows_text
    from flir_player.tcdata import parse_windows

    for text in ("50-", "-1150", "0-1150", "-20-30", "-", "0-1150, 1300-"):
        windows = parse_windows(text)
        assert parse_windows(windows_text(windows)) == windows
    assert windows_text(parse_windows("50-")) == "50-"


def test_w10_no_tc_near_the_clocks_says_what_to_do() -> None:
    from flir_player import tcmatch
    from flir_player.tcdata import TcTable
    from flir_player.tcmatch import MatchOptions
    from test_tcmatch import counts_for

    rng = np.random.default_rng(921)
    tc = 200 + rng.uniform(0, 30, 120)
    counts = counts_for(200 + rng.uniform(0, 30, 700), 0.8)
    counts[350:470] = counts_for(tc, 0.8)  # the TC's shape is at 350 s; the clocks say 0 s
    samples = _one_pixel(counts)
    samples.info["clock_start"] = datetime(2026, 1, 1, 12)
    table = TcTable(np.arange(120.0), tc[:, None], ("T1",), clock_zero=datetime(2026, 1, 1, 12))
    result, _location, _series = tcmatch.analyse(samples, table, MatchOptions(spot=1, clock_window_s=120.0,
                                                                               jump_hold_s=0))
    assert result.near_clocks and not result.channels[0].match.found
    assert any("No TC was found within 120 s" in w and "Anywhere in the recording" in w for w in result.warnings)


def test_w6_new_statistics_record_the_recording_fingerprint(tmp_path) -> None:
    from flir_player import tcmatch
    from test_tcmatch import _FakeImager

    frames = np.ones((20, 3, 4), np.float32)
    t = np.arange(20) / 10.0
    tcmatch._accumulate(_FakeImager(frames), np.arange(20), t, 1.0, (0, 0, 4, 3), None, lambda: False, tmp_path, "k",
                        "fff")
    with np.load(tmp_path / "k_meta.npz") as data:
        assert str(data["fingerprint"]) == "fff"
    assert tcmatch._load_cache(tmp_path, "k", "fff") is not None and tcmatch._load_cache(tmp_path, "k", "g") is None
    assert not list(tmp_path.glob("*.partial.npz"))


def test_w11_a_cut_off_cache_file_means_no_cache(tmp_path) -> None:
    from flir_player.tcmatch import _load_cache

    _cache_files(tmp_path, "cut")
    meta = tmp_path / "cut_meta.npz"
    meta.write_bytes(meta.read_bytes()[:len(meta.read_bytes()) // 2])
    assert _load_cache(tmp_path, "cut") is None


def test_w13_the_summary_gives_seconds_when_a_bin_is_longer_than_a_second() -> None:
    import re

    from flir_player import tcmatch
    from flir_player.tcdata import TcTable
    from flir_player.tcmatch import MatchOptions
    from test_tcmatch import counts_for

    times = np.arange(220) * 10.0
    tc = 100 + 40 * np.sin(times / 300)
    samples = _one_pixel(counts_for(tc, 0.8), step=10.0)
    table = TcTable(times, tc[:, None], ("T1",))
    result, _location, _series = tcmatch.analyse(samples, table, MatchOptions(spot=1, lag_s=0,
                                                                               pixels={"T1": (0, 0)}))
    events = result.channels[0].events
    assert events
    shown = [float(v) for v in re.findall(r"Event \d+-\d+ s[^(]*\(([\d.]+) s,", tcmatch.summary_text(result))]
    assert shown == [e.samples * 10.0 for e in events]
    assert tcmatch._rule_note({"tc usable": 22}, 10.0) == "tc usable: 220 s"


# --- workbook and zones (x) ---------------------------------------------------------------------------


def test_x2_rows_where_a_zone_clips_are_left_out_of_the_match(tmp_path) -> None:
    import re

    import openpyxl

    from flir_player.workbook import MATCH
    from test_emissivity_match import _Match, _prefill, _write, _zone_source

    source, temps = _zone_source()
    base = _Match(_write(tmp_path, source, _prefill(temps), name="clean.xlsx"), 2).pairs["Cell 3"][3]
    cell3 = next(r for r in source.rois if r.shape.name == "Cell 3")
    cell3.status = cell3.status.copy()
    cell3.status[30:41] = 3  # a pixel of the zone clips while its mean count is still in range
    path = _write(tmp_path, source, _prefill(temps), name="clipped.xlsx")
    assert _Match(path, 2).pairs["Cell 3"][3] == base - 11
    book = openpyxl.load_workbook(path)
    fit_cells = [c.value for row in book["TC Compare"].iter_rows(min_row=10, max_row=10) for c in row
                 if isinstance(c.value, str) and f"'{MATCH}'!$B$9" in c.value]  # the Fit flags (from MATCH_FROM)
    status = [re.search(r"'Data'!([A-Z]+)6<>\"Saturated\"", f) for f in fit_cells]
    assert fit_cells and all(status)  # Excel recomputes the same rows from the Status column
    assert all(book["Data"][f"{s.group(1)}5"].value == "Status" for s in status)


def test_x3_zones_with_gaps_are_all_as_wide() -> None:
    from flir_player.zones import ZoneSpec, split_box

    for length, count, gap in ((180, 18, 4), (161, 18, 1), (271, 18, 2), (271, 18, 0), (63, 3, 20)):
        zones = split_box(ZoneSpec(((0, 0), (length, 5)), count=count, start="left", gap=gap), 640, 480)
        widths = [p[1][0] - p[0][0] for _name, p in zones]
        assert max(widths) - min(widths) <= 1, (length, count, gap, widths)
        assert zones[0][1][0][0] == gap // 2 and zones[-1][1][1][0] == length - gap // 2
    with pytest.raises(ValueError, match="at least 63 px"):  # the length it names is enough
        split_box(ZoneSpec(((0, 0), (60, 5)), count=3, start="left", gap=20))


def test_x4_more_than_255_rois_take_more_overview_charts(tmp_path) -> None:
    import re
    import zipfile

    from test_emissivity_match import _write, _zone_source

    source, _temps = _zone_source(rows=3, zones=256)
    path = _write(tmp_path, source, None, name="many.xlsx")
    with zipfile.ZipFile(path) as book:
        charts = [book.read(n).decode("utf-8") for n in book.namelist() if re.match(r"xl/charts/chart\d+\.xml", n)]
    counts = {title: chart.count("<c:ser>") for chart in charts
              for title in re.findall(r"<a:t>(ROIs \d+-\d+ of 256)", chart)}
    assert counts == {"ROIs 1-255 of 256": 255, "ROIs 256-256 of 256": 1}


def test_x5_a_saved_zone_count_beyond_the_dialog_is_refused(tmp_path) -> None:
    import json

    from flir_player.excel_export import ROI_SET_FORMAT, load_roi_set

    def saved(count: int, gap: int = 0) -> Path:
        path = tmp_path / f"set_{count}_{gap}.rois.json"
        path.write_text(json.dumps({"format": ROI_SET_FORMAT, "version": 1,
                                    "rois": [{"kind": "rect", "points": [[0, 0], [8, 8]], "name": "Cell 1"}],
                                    "zone_groups": [{"rois": [0], "box": [[0, 0], [8, 8]], "count": count,
                                                     "start": "left", "gap": gap}]}), encoding="utf-8")
        return path

    assert load_roi_set(saved(200)).zone_groups[0].spec.count == 200
    for count, gap in ((2 ** 40, 0), (201, 0), (2, 21)):
        with pytest.raises(ValueError, match="Zone group 1"):
            load_roi_set(saved(count, gap))
    for size in ([640], [640, 0], [640, 480, 3]):  # a width and a height, or nothing
        path = tmp_path / "sized.rois.json"
        path.write_text(json.dumps({"format": ROI_SET_FORMAT, "version": 1, "size": size, "rois": []}),
                        encoding="utf-8")
        with pytest.raises(ValueError, match="image size"):
            load_roi_set(path)


def test_x6_a_roi_set_for_another_image_size_is_refused(tmp_path, monkeypatch) -> None:
    from flir_player import main_window as mw
    from flir_player.excel_export import RoiSet, save_roi_set

    path = tmp_path / "other.rois.json"
    save_roi_set(path, RoiSet(rois=(RoiShape(1, "rect", ((10, 10), (20, 20)), "Box 1"),), size=(320, 240)))
    errors = []
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    fake = NS(metadata=NS(width=640, height=480, path=tmp_path / "rec.csq"), current_packet=object(),
              _show_error=lambda title, text: errors.append(text),
              _clear_rois=lambda: pytest.fail("the ROIs were replaced"))
    mw.MainWindow._load_roi_set(fake)
    assert errors and "320 × 240" in errors[0] and "640 × 480" in errors[0]


# --- GUI (g) ------------------------------------------------------------------------------------------


def test_g1_a_recording_put_at_a_fitted_path_does_not_inherit_the_fit(tmp_path, monkeypatch) -> None:
    from flir_player import main_window as mw

    recording = tmp_path / "a.csq"
    recording.write_bytes(b"A" * 10)
    started = []

    class Reader:
        def __init__(self, *args):
            self.done = NS(connect=lambda slot: None)

        def start_kept(self):
            started.append(True)

    monkeypatch.setattr(mw, "SavedFitReader", Reader)
    key = str(recording)
    fake = NS(_tc_runs={key: {"output": "fit of A", "identity": mw._file_identity(recording)}},
              _ignition_frames={key: 4110}, _open_request_id=7, _on_saved_tc_fit=None)
    metadata = NS(path=recording, num_frames=1)
    mw.MainWindow._reopen_saved_tc_fit(fake, metadata)
    assert not started and fake._tc_runs[key]["output"] == "fit of A"  # the same file keeps its fit
    recording.write_bytes(b"B" * 12)  # another recording at the same path
    mw.MainWindow._reopen_saved_tc_fit(fake, metadata)
    assert started and key not in fake._tc_runs  # its own fit, if any, is read (and checked) from its folder
    assert key not in fake._ignition_frames  # the old fit's time zero goes with it


def test_g1_a_saved_fit_read_for_an_earlier_open_is_dropped(tmp_path) -> None:
    from flir_player import main_window as mw

    recording = tmp_path / "a.csq"
    recording.write_bytes(b"B" * 12)
    fake = NS(metadata=NS(path=recording), _tc_runs={}, _open_request_id=8,
              _notify=lambda text: pytest.fail("an old read was used"))
    mw.MainWindow._on_saved_tc_fit(fake, str(recording), object(), object(), "", 7, (10, 1))
    assert fake._tc_runs == {}


def test_g2_a_workbook_from_a_fit_saved_before_0_7_1_asks_first(tmp_path, monkeypatch) -> None:
    import json

    from flir_player import main_window as mw
    from flir_player.tcmatch import load_saved_run, saved_run
    from test_v071 import _saved

    output, recording, _tc, out = _saved(tmp_path, monkeypatch)
    assert load_saved_run(saved_run(out, recording)).parameters_known
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    del data["run"]  # as 0.6.0 and 0.7.0 wrote it (the TC file found by its name, next to the recording)
    data["tc_source"] = "tc.csv"
    (out / "result.json").write_text(json.dumps(data), encoding="utf-8")
    legacy = load_saved_run(saved_run(out, recording))
    assert not legacy.parameters_known
    asked = []
    monkeypatch.setattr(mw.MessageDialog, "question", lambda parent, title, text: asked.append(text) or False)
    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", lambda *a, **k: pytest.fail("saved after No"))
    mw.MainWindow._save_tc_workbook(NS(metadata=NS(path=recording)), legacy, out)
    assert asked and "before 0.7.1" in asked[0]


def test_g3_the_search_region_of_a_box_is_the_pixels_it_measures() -> None:
    from flir_player.geometry import roi_coordinates
    from flir_player.tc_dialog import _box_region

    for points in (((12.8, 9.8), (20.2, 20.2)), ((-3, -3), (5.4, 5.6)), ((630.4, 470.6), (700, 500))):
        box = RoiShape(1, "rect", points, "Box 1")
        rows, cols = roi_coordinates(box, 480, 640)
        assert _box_region(box, 640, 480) == (cols.min(), rows.min(), cols.max() + 1, rows.max() + 1)
    assert _box_region(RoiShape(1, "rect", ((10.2, 10.2), (10.4, 10.4)), "Box 1"), 640, 480)[2] == 0


def test_g4_a_rerun_without_a_figure_does_not_reopen_the_old_one(tmp_path, monkeypatch) -> None:
    from flir_player import tcmatch
    from flir_player.tcmatch import MatchOptions, load_saved_run, saved_run
    from test_v071 import _saved

    _output, recording, tc, out = _saved(tmp_path, monkeypatch)
    assert "figure" in load_saved_run(saved_run(out, recording)).files
    tcmatch.run_analysis(recording, tc, MatchOptions(spot=1, roi=(0, 0, 48, 40)), out_dir=out, figure=False)
    assert (out / "overview.png").is_file()  # left from the first run
    assert "figure" not in load_saved_run(saved_run(out, recording)).files
