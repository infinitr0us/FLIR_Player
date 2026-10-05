# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""A700 recordings written by ResearchIR (0922 battery test, v0.5.3).

Two problems of the 0922 A700 SEQ:
* its timestamps run 1.6 % slow (30 Hz frames stamped 32.8 ms apart), so the
  clock implies 30.48 fps against a TC logger and a second camera;
* a ResearchIR workspace saved in the file overrides the camera's object
  parameters (ε 1, 3 m, τ 1 over the recorded ε 0.95, 1 m), and the File SDK
  applies it on open.

``local/data/a700_clip.seq`` holds the first 150 frames of that recording with
its saved workspace (``local/notes/2026-10-04-0922-fire-test/make_a700_clip.py``).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import wait_until
from flir_player.fff import describe_parameters, saved_object_parameters
from flir_player.models import FrameRate
from flir_player.sdktime import frame_rate, typical_frame_rate

SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"
CLIP = "a700_clip.seq"
CAMERA = {"emissivity": 0.95, "reflected_temp": 293.15, "atmosphere_temp": 293.15,
          "est_atmospheric_transmission": 0.0, "distance": 1.0, "relative_humidity": 0.5,
          "ext_optics_temp": 293.15, "ext_optics_transmission": 1.0}


# --- frame rate (pure) ------------------------------------------------------------------


def test_slow_a700_clock_snaps_to_the_camera_rate() -> None:
    rate = frame_rate(105_965, 3476.414, 0.0, 30.5)  # the whole 0922 A700 recording
    assert rate.corrected and rate.fps == 30.0
    assert rate.clock_fps == pytest.approx(30.4808, abs=1e-3)
    assert rate.clock_error == pytest.approx(0.016, abs=5e-4)


@pytest.mark.parametrize("frames, span, preset, expected", [
    (104_556, 3485.683, 0.0, 29.9957),  # T650sc CSQ with 1 s clock corrections: kept
    (2_901, 100.0, 0.0, 29.0),  # fewer frames than 30 Hz allows (drops): the clock is right
    (3_151, 100.0, 0.0, 31.5),  # 5 % fast: not a camera rate, kept
    (3_048, 100.0, 30.0, 30.47),  # a trusted preset rate: cadence logic owns the case
])
def test_other_clocks_are_kept(frames, span, preset, expected) -> None:
    rate = frame_rate(frames, span, preset, (frames - 1) / span)
    assert not rate.corrected and rate.fps == pytest.approx(expected, abs=1e-3)


@pytest.mark.parametrize("clock, camera", [(60.9, 60.0), (9.1, 9.0), (15.3, 15.0), (25.5, 25.0)])
def test_slow_clocks_snap_to_the_nearest_camera_rate(clock, camera) -> None:
    frames = int(round(clock * 200)) + 1
    assert frame_rate(frames, 200.0, 0.0, clock) == FrameRate(camera, pytest.approx(clock, abs=1e-6), camera)


def test_dropped_frames_are_not_mistaken_for_a_slow_clock() -> None:
    # 60 Hz with a 29.5 s outage averages 30.5 fps, but its frames are 1/60 s apart
    assert not frame_rate(3051, 100.0, 0.0, 60.0).corrected
    assert not frame_rate(3051, 100.0).corrected  # interval unknown: keep the clock
    assert frame_rate(3051, 100.0, 0.0, 30.4).corrected


def _stamps(intervals):
    from datetime import datetime, timedelta
    times = [datetime(2026, 9, 22, 11)]
    for seconds in intervals:
        times.append(times[-1] + timedelta(seconds=seconds))
    return lambda index: times[index]


def test_typical_frame_rate_is_the_median_interval() -> None:
    jitter = [0.0328 + 0.003 * ((k % 7) - 3) / 3 for k in range(3000)]  # A700-like 29-36 ms
    assert typical_frame_rate(_stamps(jitter), 3001) == pytest.approx(30.49, abs=0.5)
    outage = [1 / 60] * 1525 + [29.5] + [1 / 60] * 1524
    assert typical_frame_rate(_stamps(outage), 3051) == pytest.approx(60.0, rel=1e-4)
    assert typical_frame_rate(lambda index: None, 100) == 0.0


def test_degenerate_spans_fall_back_to_30_fps() -> None:
    assert frame_rate(1, 0.0) == FrameRate(30.0)
    assert frame_rate(100, 0.0) == FrameRate(30.0)


# --- saved ResearchIR settings (pure) ------------------------------------------------------


def _with_tail(tmp_path: Path, xml: str) -> Path:
    path = tmp_path / "recording.seq"
    path.write_bytes(b"FFF\0" + bytes(5000) + xml.encode("utf-8"))
    return path


def _workspace(attributes: str) -> str:
    return ("<?xml version=\"1.0\"?>\r\n<workspaceFileSettings>\r\n<palette name=\"Iron\" />\r\n"
            f"<objectParameters {attributes} />\r\n</workspaceFileSettings>\r\n")


def test_an_override_is_read_with_sdk_field_names(tmp_path) -> None:
    path = _with_tail(tmp_path, _workspace(
        'override="True" emissivity="1" distance="3" reflectedTemp="293.15" atmosphereTemp="293.15" '
        'extOpticsTemp="293.15" extOpticsTransmission="1" estAtmosphericTransmission="1" '
        'relativeHumidity="0.5"'))
    saved = saved_object_parameters(path)
    assert saved == {"emissivity": 1.0, "reflected_temp": 293.15, "atmosphere_temp": 293.15,
                     "est_atmospheric_transmission": 1.0, "distance": 3.0, "relative_humidity": 0.5,
                     "ext_optics_temp": 293.15, "ext_optics_transmission": 1.0}
    assert describe_parameters(saved, CAMERA) == "ε 1.000 · 3 m · τ 1.000"


@pytest.mark.parametrize("attributes", [
    'override="False" emissivity="0.98" distance="7"',  # the T650sc SEQ and CSQ samples
    'emissivity="0" useEmissivity="false" distance="0" useDistance="false"',  # the 2024 CSQ sample
    'override="true" emissivity="0" distance="3"',  # no usable emissivity
])
def test_non_overrides_are_ignored(tmp_path, attributes) -> None:
    assert saved_object_parameters(_with_tail(tmp_path, _workspace(attributes))) is None


@pytest.mark.parametrize("xml", [
    _workspace("override='true' emissivity='1' distance='3'"),  # single quotes
    _workspace('override="true" emissivity="1" distance="3"').replace(
        "<palette", '<!-- <objectParameters override="false" /> --><palette'),  # a comment first
    _workspace('override="true" emissivity="1" distance="3"').replace(
        '<palette name="Iron" />', "<palette>" + '<color value="#000000" />' * 12000 + "</palette>"),  # 300 KB
], ids=["single-quotes", "comment-first", "300-KB-workspace"])
def test_overrides_in_any_valid_xml_form_are_read(tmp_path, xml) -> None:
    saved = saved_object_parameters(_with_tail(tmp_path, xml))
    assert saved == {"emissivity": 1.0, "distance": 3.0}


def test_entities_are_never_expanded(tmp_path) -> None:
    xml = _workspace('override="true" emissivity="&e;"').replace(
        "<workspaceFileSettings>", '<!DOCTYPE w [<!ENTITY e "1">]><workspaceFileSettings>')
    assert saved_object_parameters(_with_tail(tmp_path, xml)) is None


def test_files_without_a_workspace_are_ignored(tmp_path) -> None:
    assert saved_object_parameters(_with_tail(tmp_path, "")) is None
    assert saved_object_parameters(tmp_path / "missing.seq") is None


# --- the A700 clip (File SDK) ----------------------------------------------------------------


def test_a700_opens_with_camera_values_and_frame_time() -> None:
    from flir_player.render import frame_seconds
    from flir_player.source import FlirVideoSource

    source = FlirVideoSource()
    metadata = source.open(SAMPLES / CLIP)
    try:
        assert metadata.frame_timed and metadata.nominal_fps == 30.0
        assert metadata.rate.clock_fps > 30.3
        assert metadata.duration_seconds == pytest.approx(149 / 30.0)
        assert source.read_object_parameters()["emissivity"] == pytest.approx(0.95)
        assert metadata.saved_parameters["emissivity"] == 1.0
        assert metadata.saved_parameters["distance"] == 3.0
        assert metadata.saved_by == "ResearchIR"
        details = dict(metadata.source_details)
        assert details["Frame rate"] == "30.00 fps (camera rate)"
        assert "slow" in details["Timestamps"]
        assert details["Saved settings"].endswith("ε 1.000 · 3 m · τ 1.000")
        packet = source.read_frame(90)
        assert frame_seconds(packet.timestamp, metadata, 90) == pytest.approx(3.0)

        assert source.apply_object_parameters(metadata.saved_parameters)["emissivity"] == 1.0
        assert source.apply_object_parameters(None)["emissivity"] == pytest.approx(0.95)
    finally:
        source.close()


def test_recordings_without_an_override_open_as_before() -> None:
    from flir_player.source import FlirVideoSource

    source = FlirVideoSource()
    metadata = source.open(SAMPLES / "2.seq")
    try:
        assert metadata.saved_parameters is None and not metadata.frame_timed
        assert metadata.nominal_fps == pytest.approx(29.976, abs=1e-3)
        assert source.read_object_parameters()["emissivity"] == pytest.approx(0.98)
        assert "Saved settings" not in dict(metadata.source_details)
    finally:
        source.close()


def test_a700_workbook_uses_the_camera_rate(tmp_path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    from flir_player.excel_export import ExportOptions, SourceSpec, run_export
    from flir_player.models import RoiShape

    dest = tmp_path / "a700.xlsx"
    spot = (RoiShape(1, "cursor", ((320.5, 240.5),), "Spot"),)
    run_export(dest, [SourceSpec(SAMPLES / CLIP, spot)],
               ExportOptions(step_s=1.0, validation_rows=4, sheets=frozenset({"validation"})))
    book = openpyxl.load_workbook(dest, data_only=True)
    counts = book["Counts"]
    frames = [counts.cell(r, 2).value for r in range(6, 11)]
    assert frames == [1, 31, 61, 91, 121]  # 1-based, one row per 30 frames
    source = {book["Source"].cell(r, 1).value: book["Source"].cell(r, 2).value for r in range(1, 80)}
    assert source["Frame rate"] == "30.0000 fps (camera rate)"
    assert source["Saved ResearchIR override"].startswith("ε 1.000 · 3 m · τ 1.000")
    assert str(book["Validation"]["E4"].value).startswith("PASS")


def test_player_offers_the_saved_values(qapp) -> None:
    from flir_player.main_window import MainWindow

    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / CLIP)
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        panel = window.inspector.params_panel
        assert panel.saved_button.isVisibleTo(panel)
        assert panel.saved_button.text() == "Use Saved ResearchIR Values"
        assert "ResearchIR" in window.canvas.notice
        assert window.transport is not None and window.metadata.frame_timed

        window._change_unit("temperature_factory_c")
        assert wait_until(qapp, lambda: window.current_packet.unit.key == "temperature_factory_c"
                          and not window._busy)
        camera_mean = window.current_packet.mean
        panel.saved_button.click()
        assert wait_until(qapp, lambda: not window._busy and window._object_params["emissivity"] == 1.0)
        assert window.current_packet.mean != camera_mean
        panel.reset_button.click()
        assert wait_until(qapp, lambda: not window._busy
                          and abs(window._object_params["emissivity"] - 0.95) < 1e-6)

        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.metadata is not None
                          and window.metadata.filename == "2.seq" and not window._busy)
        assert not panel.saved_button.isVisibleTo(panel)
    finally:
        window.close()
        qapp.processEvents()


def test_a700_clock_time_base_keeps_the_timestamps_rate(tmp_path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    from flir_player.excel_export import ExportOptions, SourceSpec, run_export
    from flir_player.models import RoiShape

    spot = (RoiShape(1, "cursor", ((320.5, 240.5),), "Spot"),)
    for ignition in (0, 20):
        dest = tmp_path / f"clock_{ignition}.xlsx"
        run_export(dest, [SourceSpec(SAMPLES / CLIP, spot, ignition_frame=ignition)],
                   ExportOptions(every_frame=True, time_base="clock", sheets=frozenset()))
        book = openpyxl.load_workbook(dest, data_only=True)
        counts = book["Counts"]
        frames = [counts.cell(r, 2).value for r in range(6, 6 + 200)]
        frames = [f for f in frames if isinstance(f, (int, float))]
        # rows every mean clock interval (30.6 fps here), not every 1/30 s (147 rows); the
        # nearest-stamp pick on jittery stamps may skip one frame and repeat another
        assert len(frames) >= (150 if ignition == 0 else 149)  # the grid is anchored at ignition
        assert len(set(frames)) >= 140
        source = {book["Source"].cell(r, 1).value: book["Source"].cell(r, 2).value for r in range(1, 80)}
        assert "This workbook follows them" in source["Note"]


def test_workbook_says_which_parameters_it_starts_from(tmp_path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    from flir_player.excel_export import ExportOptions, SourceSpec, run_export
    from flir_player.models import RoiShape

    spot = (RoiShape(1, "cursor", ((320.5, 240.5),), "Spot"),)
    saved = saved_object_parameters(SAMPLES / CLIP)
    for parameters, expected in ((None, "camera's recorded values"), (saved, "starts from these values")):
        dest = tmp_path / f"start_{expected[:6]}.xlsx"
        run_export(dest, [SourceSpec(SAMPLES / CLIP, spot, parameters=parameters)],
                   ExportOptions(step_s=1.0, sheets=frozenset()))
        sheet = openpyxl.load_workbook(dest, data_only=True)["Source"]
        rows = {sheet.cell(r, 1).value: sheet.cell(r, 2).value for r in range(1, 80)}
        assert expected in rows["Saved ResearchIR override"]
