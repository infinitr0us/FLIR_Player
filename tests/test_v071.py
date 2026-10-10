# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""0.7.1: reopening a TC fit from its results folder, the export's time 0 from the fit, zone status from
the mean signal, and the export button and measurement hint (from writing the engineers' guide)."""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path

import numpy as np
import pytest

from flir_player import tcmatch
from flir_player.excel_export import roi_status
from flir_player.models import RoiShape, VideoMetadata
from flir_player.tcmatch import MatchOptions, MatchResult, RunOutput, load_saved_run, options_from_dict, saved_run
from test_radiometry import T650
from test_tc_dialog import _fake_run, _write_csv
from test_tcmatch import make_scene


def _roundtrip(result: MatchResult) -> MatchResult:
    return MatchResult.from_dict(json.loads(json.dumps(result.to_dict())))


# --- the result and its options come back from result.json -----------------------------------------


def test_a_result_survives_its_json() -> None:
    samples, table = make_scene()
    result, _location, _series = tcmatch.analyse(samples, table, MatchOptions(spot=1))
    again = _roundtrip(result)
    assert tcmatch.summary_text(again) == tcmatch.summary_text(result)
    assert tcmatch.workbook_origin(again) == tcmatch.workbook_origin(result)
    assert [(s.name, s.points) for s, _ in tcmatch.tc_rois(again)] == [(s.name, s.points) for s, _ in tcmatch.tc_rois(result)]
    first, second = tcmatch.tc_prefill(result, table), tcmatch.tc_prefill(again, table)
    assert first.roi_tc == second.roi_tc and first.roi_eps == second.roi_eps and first.roi_use == second.roi_use
    assert first.window == second.window and first.offset_s == pytest.approx(second.offset_s)
    failed = next(ch for ch in again.channels if not ch.match.found)  # NaN correlations come back as NaN
    assert failed.match.pixel is None and math.isnan(failed.match.r)
    assert again.channels[0].match.r_map is None


def test_options_survive_their_json() -> None:
    options = MatchOptions(roi=(10, 20, 30, 40), channels=("A", "B"), valid={"A": ((0.0, 30.0), (50.0, 90.0))},
                           scope=(0.0, 900.0), lag_range=(10.0, 20.0), pixels={"B": (5, 6)}, spot=5, tc_min_c=80.0)
    data = json.loads(json.dumps(dataclasses.asdict(options)))
    assert options_from_dict({**data, "from_a_later_version": 1}) == options


# --- reopening a results folder ---------------------------------------------------------------------


def _saved(tmp_path, monkeypatch, table=None, tc_dir=None):
    samples, scene_table = make_scene()
    samples.info["frames"] = int(samples.counts.sum())
    table = table or scene_table
    _fake_run(monkeypatch, samples, table)
    data = tmp_path / "data"
    data.mkdir()
    recording = data / "synthetic.seq"
    recording.write_bytes(bytes(1000))  # its size identifies it
    folder = tc_dir or data
    folder.mkdir(exist_ok=True)
    tc = _write_csv(folder / "tc.csv", ["A"], [[0, 20.0]])  # read through the patched reader: path and content count
    out = data / "synthetic_tcmatch"
    output = tcmatch.run_analysis(recording, tc, MatchOptions(spot=1, roi=(0, 0, 48, 40)), out_dir=out,
                                  parameters={"emissivity": 0.95, "reflected_temp": 293.15, "atmosphere_temp": 293.15,
                                              "est_atmospheric_transmission": 0.0, "distance": 8.0,
                                              "relative_humidity": 0.5, "ext_optics_temp": 293.15,
                                              "ext_optics_transmission": 1.0})
    return output, recording, tc, out


def test_a_saved_run_reopens_with_its_tc_file(tmp_path, monkeypatch) -> None:
    output, recording, tc, out = _saved(tmp_path, monkeypatch)
    saved = saved_run(out, recording, output.result.num_frames)
    assert saved.tc_file == tc.resolve() and saved.run_info
    assert saved.options.roi == (0, 0, 48, 40) and saved.parameters["distance"] == 8.0
    again = load_saved_run(saved)
    assert again.result.lag_s == pytest.approx(output.result.lag_s)
    assert again.tc_file == tc.resolve() and set(again.files) >= {"json", "summary", "rois"}
    # another recording, or the same name with another frame count, is not this run
    assert saved_run(out, recording.with_name("other.seq")) is None
    assert saved_run(out, recording, output.result.num_frames + 1) is None
    assert saved_run(tmp_path / "nowhere", recording) is None


def test_an_older_result_finds_its_tc_file_by_name(tmp_path, monkeypatch) -> None:
    samples, table = make_scene()
    output, recording, tc, out = _saved(tmp_path, monkeypatch, dataclasses.replace(table, source="tc.csv"))
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    del data["run"]  # as 0.7.0 wrote it
    (out / "result.json").write_text(json.dumps(data), encoding="utf-8")
    saved = saved_run(out, recording)
    assert not saved.run_info and saved.tc_file == recording.parent / "tc.csv" and saved.parameters is None
    from flir_player.tc_dialog import saved_request

    assert saved_request(saved, None)["parameters_from"] == "player"  # the player's default then


def test_a_missing_tc_file_or_column_is_reported(tmp_path, monkeypatch) -> None:
    output, recording, tc, out = _saved(tmp_path, monkeypatch)
    content = tc.read_bytes()
    tc.unlink()
    saved = saved_run(out, recording)
    assert saved.tc_file == tc.resolve()  # where the fit read it: the dialog shows it, the load reports it
    with pytest.raises(ValueError, match="tc.csv was not found where the fit read it"):
        load_saved_run(saved)
    _samples, table = make_scene()
    tc.write_bytes(content)
    narrow = dataclasses.replace(table, names=("TC A", "TC B", "Other"))
    monkeypatch.setattr(tcmatch, "read_tc_table", lambda path, sheet=None, time_column=None: narrow)
    with pytest.raises(ValueError, match="no column TC C"):
        load_saved_run(saved_run(out, recording))
    (out / "result.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be read"):
        saved_run(out, recording)


def test_the_setup_dialog_offers_the_last_runs_region(qapp, tmp_path, monkeypatch) -> None:
    from flir_player.tc_dialog import TcCalibrationDialog, saved_request

    output, recording, tc, out = _saved(tmp_path, monkeypatch)
    saved = saved_run(out, recording)
    request = saved_request(saved, output.table)
    assert request["parameters_from"] == "player" and request["cache_dir"] == str(out / "cache")
    assert [(name, checked) for name, checked, _ in request["channel_rows"]] == [
        ("TC A", True), ("TC B", True), ("TC C", True)]
    metadata = VideoMetadata(path=recording, width=48, height=40, num_frames=30_000, start_time=None, end_time=None,
                             duration_seconds=1000.0, nominal_fps=30.0)
    dialog = TcCalibrationDialog(metadata, (), current_frame=0, previous=request, has_results=True)
    assert dialog.region() == (0, 0, 48, 40) and dialog.region_combo.currentText().startswith("Last run's region")
    dialog.deleteLater()
    box = RoiShape(1, "rect", ((0.0, 0.0), (48.0, 40.0)), "Box 1")  # the same region as a box: no extra entry
    dialog = TcCalibrationDialog(metadata, (box,), current_frame=0, previous=request)
    texts = [dialog.region_combo.itemText(k) for k in range(dialog.region_combo.count())]
    assert dialog.region() == (0, 0, 48, 40) and not any(t.startswith("Last run's") for t in texts)
    dialog.deleteLater()


# --- the export takes time 0 and the range from the fit ----------------------------------------------


def test_the_export_starts_at_logger_time_zero(qapp) -> None:
    from flir_player.excel_dialog import ExcelExportDialog

    samples, table = make_scene()
    result, _l, _s = tcmatch.analyse(samples, table, MatchOptions(spot=3))
    metadata = VideoMetadata(path=Path("C:/data/scene.seq"), width=48, height=40, num_frames=30_000,
                             start_time=None, end_time=None, duration_seconds=1000.0, nominal_fps=30.0)
    zones = tuple(RoiShape(k + 1, "rect", ((float(c), 0.0), (float(c + 4), 40.0)), f"Cell {k + 1}")
                  for k, c in enumerate(range(0, 48, 4)))
    dialog = ExcelExportDialog(metadata, zones, current_frame=0, ignition_frame=4110,
                               tc_run=RunOutput(result=result, table=table))
    frame, offset = tcmatch.workbook_origin(result)
    assert dialog.ignition_spin.value() == frame + 1
    assert dialog.start_check.isChecked() and dialog.end_check.isChecked()
    assert dialog.start_spin.value() == pytest.approx(table.times[0] + offset, abs=0.1)
    assert dialog.end_spin.value() == pytest.approx(min(table.times[-1] + offset, (30_000 - 1 - frame) / 30.0), abs=0.1)
    assert f"Time 0 is logger time 0 (frame {frame + 1:,})" in dialog.tc_label.text()
    assert dialog.options().tc_prefill.offset_s == pytest.approx(0.0, abs=0.05)
    dialog.ignition_spin.setValue(frame + 101)  # any time 0 still works: the logger offset follows
    assert f"Time 0 (frame {frame + 101:,}) is logger time {100 / result.fps - offset:.1f} s" in dialog.tc_label.text()
    assert dialog.options().tc_prefill.offset_s == pytest.approx(result.lag_s - (frame + 100) / result.fps)
    # without a usable fit nothing is changed
    plain = ExcelExportDialog(metadata, zones, current_frame=0, ignition_frame=4110)
    assert plain.ignition_spin.value() == 4111 and not plain.start_check.isChecked()


# --- zone status from the mean signal ------------------------------------------------------------------


def test_a_zone_measured_by_its_mean_signal_is_flagged_by_that_mean() -> None:
    limits = T650.count_limits()
    low, high = limits["calibrated"]
    raw_high = limits["raw"][1]
    counts = {"min": np.array([low - 50, low + 10, low - 50, low + 10]),
              "max": np.array([low + 400, low + 400, raw_high, high + 50]),
              "mean": np.array([low + 100, low - 5, low + 100, low + 200])}
    # pixel temperatures: the coldest/hottest pixel decides
    assert roi_status(T650, "pixels", counts).tolist() == [1, 0, 3, 2]
    # mean signal: the mean decides; a clipped pixel still marks the row saturated
    assert roi_status(T650, "mean", counts).tolist() == [0, 1, 3, 0]
    assert roi_status(T650, "spot", {"value": np.array([low - 1, low + 1])}).tolist() == [1, 0]


# --- the export button and the measurement hint ------------------------------------------------------


def test_the_export_button_opens_its_menu_and_the_hint_explains_counts(qapp) -> None:
    from PySide6.QtWidgets import QToolButton

    from flir_player.main_window import MainWindow

    window = MainWindow()
    try:
        button = window.title_bar.export_button
        assert button.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup
        texts = [action.text() for action in button.menu().actions()]
        assert texts[0] == "Rendered frame (PNG)" and "Excel workbook (live temperatures)…" in texts
        assert "Ctrl+E" in button.toolTip()
        inspector = window.inspector
        inspector.set_object_parameters({"emissivity": 0.95, "can_change": False})
        assert inspector.params_hint.isVisibleTo(inspector)
        inspector.set_object_parameters({"emissivity": 0.95, "can_change": True})
        assert not inspector.params_hint.isVisibleTo(inspector)
    finally:
        window.close()


# --- Codex review 2026-10-10 (local/notes/2026-10-10-v071-fixes, run review-v071) ----------------------


def test_r1_a_moved_tc_file_is_found_by_content_never_by_name_alone(tmp_path, monkeypatch) -> None:
    output, recording, tc, out = _saved(tmp_path, monkeypatch, tc_dir=tmp_path / "logger")
    saved = saved_run(out, recording)
    assert saved.tc_sha256 == tcmatch.file_sha256(tc)
    moved = recording.parent / "tc.csv"
    moved.write_bytes(tc.read_bytes())
    tc.unlink()
    assert load_saved_run(saved_run(out, recording)).tc_file == moved  # same content, next to the recording
    moved.write_text("Time (s),A\n0,99\n", encoding="utf-8")  # another test's logger file of the same name
    with pytest.raises(ValueError, match="not found where the fit read it"):
        load_saved_run(saved_run(out, recording))
    tc.write_text("Time (s),A\n0,21\n", encoding="utf-8")  # back at its place, but edited since the fit
    with pytest.raises(ValueError, match="changed since the fit"):
        load_saved_run(saved_run(out, recording))


def test_r2_another_recording_of_the_same_name_and_length_is_not_this_run(tmp_path, monkeypatch) -> None:
    output, recording, tc, out = _saved(tmp_path, monkeypatch)
    assert saved_run(out, recording, output.result.num_frames) is not None
    recording.write_bytes(bytes(999))
    assert saved_run(out, recording, output.result.num_frames) is None


@pytest.mark.parametrize("damage", ["list", "run list", "tc_file number", "rules list", "channels missing"])
def test_r3_a_damaged_result_is_reported_not_raised(tmp_path, monkeypatch, damage) -> None:
    output, recording, tc, out = _saved(tmp_path, monkeypatch)
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    if damage == "list":
        data = [data]
    elif damage == "run list":
        data["run"] = [1]
    elif damage == "tc_file number":
        data["run"]["tc_file"] = 42
    elif damage == "rules list":
        data["channels"][0]["rules"] = [1]
    else:
        del data["channels"]
    (out / "result.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be read"):
        saved_run(out, recording)


def test_r3_opening_a_recording_survives_any_error_from_its_results_folder(qapp, tmp_path, monkeypatch) -> None:
    from conftest import wait_until
    from flir_player import main_window as mw
    from flir_player import tc_dialog

    def broken(*_args, **_kwargs):
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(tc_dialog, "saved_run", broken)
    recording = tmp_path / "scene.seq"
    (tmp_path / "scene_tcmatch").mkdir()
    (tmp_path / "scene_tcmatch" / "result.json").write_text("{}", encoding="utf-8")
    window = mw.MainWindow()
    try:
        metadata = VideoMetadata(path=recording, width=48, height=40, num_frames=30, start_time=None,
                                 end_time=None, duration_seconds=1.0, nominal_fps=30.0)
        window.metadata = metadata
        window._reopen_saved_tc_fit(metadata)  # returns at once: the reading runs in the background
        assert wait_until(qapp, lambda: window.canvas.notice.startswith("The last TC fit was not reopened"), timeout=10)
        assert "list" in window.canvas.notice
    finally:
        window.metadata = None
        window.close()


def test_r9_a_recording_replaced_during_the_sampling_stops_the_run(tmp_path, monkeypatch) -> None:
    samples, table = make_scene()
    samples.info["frames"] = int(samples.counts.sum())
    _fake_run(monkeypatch, samples, table)
    recording = tmp_path / "synthetic.seq"
    recording.write_bytes(bytes(1000))
    tc = _write_csv(tmp_path / "tc.csv", ["A"], [[0, 20.0]])

    def replaced_while_sampling(path, options, *, step_s, im, progress, abort, cache_dir, stage):
        recording.write_bytes(bytes([2]) * 1000)  # another recording of the same name and size
        return samples

    monkeypatch.setattr(tcmatch, "sample_recording", replaced_while_sampling)
    with pytest.raises(ValueError, match="synthetic.seq changed while it was read"):
        tcmatch.run_analysis(recording, tc, MatchOptions(spot=1), out_dir=tmp_path / "out")
    assert not (tmp_path / "out" / "result.json").exists()


def test_r10_the_reopen_reads_nothing_of_the_recording_on_the_gui_thread(qapp, tmp_path, monkeypatch) -> None:
    import threading

    from conftest import wait_until
    from flir_player import main_window as mw

    output, recording, tc, out = _saved(tmp_path, monkeypatch)
    monkeypatch.setattr(mw, "results_folder", lambda path: out)
    threads = []
    real = tcmatch.recording_fingerprint

    def watched(path):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(path)

    monkeypatch.setattr(tcmatch, "recording_fingerprint", watched)
    window = mw.MainWindow()
    try:
        window.metadata = VideoMetadata(path=recording, width=48, height=40, num_frames=output.result.num_frames,
                                        start_time=None, end_time=None, duration_seconds=1.0, nominal_fps=30.0)
        window._reopen_saved_tc_fit(window.metadata)
        assert threads == []  # nothing read yet when it returns
        assert wait_until(qapp, lambda: "output" in window._tc_runs.get(str(recording), {}), timeout=10)
        assert threads == [False]  # read once, off the GUI thread
    finally:
        window.metadata = None
        window.close()


def test_r4_the_label_gives_the_logger_time_when_logger_zero_is_outside_the_recording(qapp) -> None:
    from flir_player.excel_dialog import ExcelExportDialog

    samples, table = make_scene()
    result, _l, _s = tcmatch.analyse(samples, table, MatchOptions(spot=3))
    early = dataclasses.replace(result, lag_s=-100.0, ignition_frame=-3000)  # the logger started 100 s before
    metadata = VideoMetadata(path=Path("C:/data/scene.seq"), width=48, height=40, num_frames=30_000,
                             start_time=None, end_time=None, duration_seconds=1000.0, nominal_fps=30.0)
    zones = tuple(RoiShape(k + 1, "rect", ((float(c), 0.0), (float(c + 4), 40.0)), f"Cell {k + 1}")
                  for k, c in enumerate(range(0, 48, 4)))
    dialog = ExcelExportDialog(metadata, zones, current_frame=0, tc_run=RunOutput(result=early, table=table))
    assert dialog.ignition_spin.value() == 1  # the first frame: logger time 0 is not in the recording
    assert "Time 0 (frame 1) is logger time 100.0 s" in dialog.tc_label.text()
    assert dialog.start_spin.value() == 0.0  # the logger range clipped to the recording
    assert dialog.options().tc_prefill.offset_s == pytest.approx(-100.0)


# --- Codex review, second pass (run review-v071-cont) --------------------------------------------------


def test_r5_the_saved_hash_is_the_content_the_fit_parsed(tmp_path, monkeypatch) -> None:
    samples, table = make_scene()
    samples.info["frames"] = int(samples.counts.sum())
    _fake_run(monkeypatch, samples, table)
    data = tmp_path / "data"
    data.mkdir()
    recording = data / "synthetic.seq"
    recording.write_bytes(bytes(1000))
    tc = _write_csv(data / "tc.csv", ["A"], [[0, 20.0]])
    parsed = tcmatch.file_sha256(tc)

    def edited_while_sampling(path, options, *, step_s, im, progress, abort, cache_dir, stage):
        tc.write_text("Time (s),A\n0,99\n", encoding="utf-8")  # someone edits the logger file meanwhile
        return samples

    monkeypatch.setattr(tcmatch, "sample_recording", edited_while_sampling)
    out = data / "synthetic_tcmatch"
    tcmatch.run_analysis(recording, tc, MatchOptions(spot=1), out_dir=out)
    saved = saved_run(out, recording)
    assert saved.tc_sha256 == parsed  # not the edited file's
    with pytest.raises(ValueError, match="changed since the fit"):
        load_saved_run(saved)
    # an edit during the read itself stops the run
    _fake_run(monkeypatch, samples, table)
    real_read = tcmatch.read_tc_table

    def read_and_edit(path, sheet=None, time_column=None):
        tc.write_text("Time (s),A\n0,98\n", encoding="utf-8")
        return real_read(path, sheet=sheet, time_column=time_column)

    monkeypatch.setattr(tcmatch, "read_tc_table", read_and_edit)
    with pytest.raises(ValueError, match="changed while it was read"):
        tcmatch.run_analysis(recording, tc, MatchOptions(spot=1))


def test_r6_another_recording_of_the_same_size_is_not_this_run(tmp_path, monkeypatch) -> None:
    output, recording, tc, out = _saved(tmp_path, monkeypatch)
    saved = saved_run(out, recording, output.result.num_frames)
    assert saved is not None
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert data["run"]["recording_fingerprint"] == tcmatch.recording_fingerprint(recording)
    recording.write_bytes(bytes([1]) * 1000)  # same name, length and size, other content
    assert saved_run(out, recording, output.result.num_frames) is None
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    recording.write_bytes(bytes(1000))  # the original again, moved with its results: still this run
    (moved / recording.name).write_bytes(recording.read_bytes())
    assert saved_run(out, moved / recording.name, output.result.num_frames) is not None


def test_r7_damaged_options_are_reported_at_load_and_in_the_callback(qapp, tmp_path, monkeypatch) -> None:
    from flir_player import main_window as mw

    output, recording, tc, out = _saved(tmp_path, monkeypatch)
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    for damage in ({"valid": {"TC A": [[1.0]]}}, {"roi": [1, 2, 3]}, {"pixels": {"TC A": [1]}}, {"scope": [5.0]}):
        broken = {**data, "options": {**data["options"], **damage}}
        (out / "result.json").write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(ValueError, match="cannot be read"):
            saved_run(out, recording)
    (out / "result.json").write_text(json.dumps(data), encoding="utf-8")
    saved = saved_run(out, recording)

    def broken_request(*_args, **_kwargs):
        raise ValueError("not enough values to unpack")

    monkeypatch.setattr(mw, "saved_request", broken_request)
    window = mw.MainWindow()
    try:
        window.metadata = VideoMetadata(path=recording, width=48, height=40, num_frames=output.result.num_frames,
                                        start_time=None, end_time=None, duration_seconds=1.0, nominal_fps=30.0)
        window._on_saved_tc_fit(str(recording), saved, load_saved_run(saved), "")
        assert window.canvas.notice.startswith("The last TC fit") and "output" not in window._tc_runs[str(recording)]
    finally:
        window.metadata = None
        window.close()


def test_r8_an_unreadable_candidate_does_not_end_the_search(tmp_path, monkeypatch) -> None:
    output, recording, tc, out = _saved(tmp_path, monkeypatch, tc_dir=tmp_path / "logger")
    first = recording.parent / "tc.csv"  # the first place looked at: cannot be read
    first.write_bytes(tc.read_bytes())
    second = out / "tc.csv"  # a later one: the copy
    second.write_bytes(tc.read_bytes())
    tc.unlink()
    real_hash = tcmatch.file_sha256

    def guarded(path):
        if Path(path) == first:
            raise PermissionError(13, "Access is denied")
        return real_hash(path)

    monkeypatch.setattr(tcmatch, "file_sha256", guarded)
    assert load_saved_run(saved_run(out, recording)).tc_file == second
    second.unlink()
    with pytest.raises(ValueError, match="could not read tc.csv"):
        load_saved_run(saved_run(out, recording))
