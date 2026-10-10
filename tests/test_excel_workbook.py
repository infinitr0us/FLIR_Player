# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Excel workbook structure, formulas and cached values (no SDK needed)."""
from __future__ import annotations

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
    RoiSet,
    SourceData,
    SourceSpec,
    bin_counts,
    estimate,
    frames_for,
    load_roi_set,
    roi_set_path,
    save_roi_set,
    timeline,
)
from flir_player.models import RoiShape  # noqa: E402
from flir_player.radiometry import MeasurementParameters, count_status, object_temperature  # noqa: E402
from flir_player.workbook import write_workbook  # noqa: E402

from test_radiometry import T650  # noqa: E402

PARAMS = MeasurementParameters(emissivity=0.95, reflected_k=293.15, atmosphere_k=293.15,
                               transmission=None, distance_m=2.0, humidity=0.5)


def _source(label: str, rows: int, rng, *, verified: bool = True, bins: int = 16) -> SourceData:
    frames = np.arange(rows) * 30
    frames[-2:] = -1  # the recording ends before the timeline does
    spot = RoiShape(1, "cursor", ((10.5, 10.5),), "TC1")
    circle = RoiShape(2, "ellipse", ((20.0, 20.0), (26.0, 26.0)), "TC2")
    box = RoiShape(3, "rect", ((0.0, 0.0), (40.0, 30.0)), "Box")
    base = np.linspace(8000.0, 30000.0, rows)
    rois = []
    for shape, pixels in ((spot, 1), (circle, 32), (box, 1200)):
        values = base[:, None] + rng.normal(0, 300, (rows, pixels))
        values[frames < 0] = np.nan
        if shape.kind == "cursor":
            counts = {"value": values[:, 0]}
            block, mode = None, "spot"
        else:
            counts = {"mean": values.mean(1), "max": values.max(1), "min": values.min(1)}
            if pixels <= 400:
                block, mode = values, "pixels"
            else:
                mode = "bins"
                block = np.full((rows, 2 * bins), np.nan)
                for i in range(rows):
                    if frames[i] >= 0:
                        block[i] = bin_counts(values[i], bins, T650.planck.temperature)
        low = counts.get("min", counts.get("value"))
        high = counts.get("max", counts.get("value"))
        status = np.where(np.isfinite(high), count_status(T650, low, high), -1).astype(np.int8)
        roi = RoiData(shape=shape, pixels=pixels, mode=mode, counts=counts, block=block, status=status)
        if verified:
            vrows = np.array([0, rows // 2])
            fixed = PARAMS.with_(transmission=0.99)
            roi.validation = {name: object_temperature(T650, fixed, counts[name][vrows]) for name in counts}
            if mode == "pixels":
                roi.validation["mean"] = np.nanmean(object_temperature(T650, fixed, block[vrows]), axis=1)
            roi.validation_clamped = np.zeros(vrows.size, dtype=bool)
        rois.append(roi)
    calibration = T650 if verified else None
    report = (CalibrationReport(calibration, "verified", "test calibration")
              if verified else CalibrationReport(None, "unavailable", "counts only", kind="user"))
    start = datetime(2026, 9, 3, 15, 51, 27)
    info = {"file": f"C:/data/{label}.csq", "file_name": f"{label}.csq", "file_size": 1, "camera": "FLIR T650sc",
            "camera_short": "T650sc", "camera_serial": "1", "lens": "FOL25", "width": 64, "height": 48,
            "frames": 1000, "fps": 30.0, "start": start, "end": start + timedelta(seconds=33),
            "ignition_frame": 0, "calibration": report.message, "saturation_threshold": None}
    return SourceData(
        spec=SourceSpec(Path(info["file"]), tuple(r.shape for r in rois)), label=label, info=info, fps=30.0,
        num_frames=1000, frames=frames, clock=[start + timedelta(seconds=float(i)) if f >= 0 else None
                                               for i, f in enumerate(frames)],
        rois=rois, report=report, initial=PARAMS.with_(transmission=0.99), file_parameters=PARAMS.with_(transmission=0.99),
        validation_rows=np.array([0, rows // 2]) if verified else np.empty(0, int))


@pytest.fixture(scope="module")
def workbook(tmp_path_factory):
    rng = np.random.default_rng(7)
    rows = 12
    data = ExportData(times=np.arange(rows, dtype=float) - 2.0,
                      sources=[_source("Camera A", rows, rng), _source("A8303sc", rows, rng, verified=False)],
                      options=ExportOptions(bins=16), created=datetime(2026, 9, 26, 10, 0),
                      versions={"tool": "test", "sdk": "5.0.1"})
    path = tmp_path_factory.mktemp("xlsx") / "book.xlsx"
    write_workbook(path, data)
    return path, data


def _header_map(ws) -> dict[tuple[str, str], int]:
    """(group title, column header) → column, from rows 4 and 5."""
    groups = {}
    for merged in ws.merged_cells.ranges:
        if merged.min_row == 4:
            for c in range(merged.min_col, merged.max_col + 1):
                groups[c] = ws.cell(4, merged.min_col).value
    out = {}
    for c in range(1, ws.max_column + 1):
        head = ws.cell(5, c).value
        group = groups.get(c, ws.cell(4, c).value)
        if head is not None:
            out[(group, str(head))] = c
    return out


def test_sheets_names_and_hidden_pixels(workbook) -> None:
    book = openpyxl.load_workbook(workbook[0])
    assert book.sheetnames == ["Start Here", "Settings", "Data", "Charts", "TC Compare", "Emissivity Match", "Summary",
                               "ROI Map", "Validation", "Counts", "Pixels", "Source"]
    assert book["Pixels"].sheet_state == "hidden"
    names = set(book.defined_names.keys())
    for name in ("UNIT", "CLAMP_MODE", "G_EPS", "G_TAUMODE", "SRC_1_R", "SRC_1_TAU", "ROI_01_K1",
                 "ROI_03_CLO", "ROI_02_T", "TIME", "BASE_START", "TC_OFFSET"):
        assert name in names, name


def test_data_formulas_reference_counts_and_carry_model_values(workbook) -> None:
    formulas = openpyxl.load_workbook(workbook[0])["Data"]
    values = openpyxl.load_workbook(workbook[0], data_only=True)["Data"]
    heads = _header_map(values)  # stat headers are formulas that follow the unit
    spot = heads[("Camera A · TC1", "Value (°C)")]
    mean_pixels = heads[("Camera A · TC2", "Mean (°C)")]
    mean_bins = heads[("Camera A · Box", "Mean (°C)")]
    assert formulas.cell(6, spot).value.startswith("=IF(ISNUMBER('Counts'!")
    assert "SUMPRODUCT('Settings'!$" in formulas.cell(6, mean_pixels).value  # B, cited by cell (see _direct)
    assert "/LN(" in formulas.cell(6, mean_pixels).value
    assert "SUMPRODUCT('Pixels'!" in formulas.cell(6, mean_bins).value
    data = workbook[1]
    params = data.sources[0].initial
    roi_spot, roi_circle, roi_box = data.sources[0].rois
    for i in range(10):
        expected = float(object_temperature(T650, params, roi_spot.counts["value"][i])) - 273.15
        assert values.cell(6 + i, spot).value == pytest.approx(expected, abs=1e-9)
        exact = float(np.mean(object_temperature(T650, params, roi_circle.block[i]))) - 273.15
        assert values.cell(6 + i, mean_pixels).value == pytest.approx(exact, abs=1e-9)
    # rows past the end of the recording are #N/A, statuses blank
    assert values.cell(6 + 11, spot).value == "#N/A"
    assert formulas.cell(6 + 11, spot + 1).value is None


def test_counts_only_source_has_status_but_no_formulas(workbook) -> None:
    formulas = openpyxl.load_workbook(workbook[0])["Data"]
    heads = _header_map(openpyxl.load_workbook(workbook[0], data_only=True)["Data"])
    column = heads[("A8303sc · TC1", "Value (°C)")]
    assert formulas.cell(6, column).value is None
    assert formulas.cell(6, column + 1).value == "Counts only"
    summary = openpyxl.load_workbook(workbook[0])["Summary"]
    texts = [summary.cell(r, 4).value for r in range(14, 20)]
    assert "Counts only — no temperature calibration" in texts


def test_no_cells_dropped_in_constant_memory_headers(workbook) -> None:
    book = openpyxl.load_workbook(workbook[0])
    counts = _header_map(book["Counts"])
    assert ("Camera A", "Frame") in counts and ("A8303sc", "Camera clock") in counts
    assert ("Camera A · Box", "Pixels") in counts
    tc = book["TC Compare"]
    assert tc.cell(9, 1).value == "Time (s)" and tc.cell(9, 11).value == "TC 10"
    assert tc.cell(8, 1).value.startswith("Paste logger data")
    pixels = book["Pixels"]
    assert pixels.cell(5, 2).value == "p1"  # first area ROI block


def test_summary_uses_prefixed_aggregate_and_validation_passes(workbook) -> None:
    summary = openpyxl.load_workbook(workbook[0])["Summary"]
    formulas = [summary.cell(14, c).value for c in range(4, 11)]
    assert all("_xlfn.AGGREGATE" in f for f in formulas if isinstance(f, str) and "AGGREGATE" in f)
    assert any("_xlfn.AGGREGATE" in f for f in formulas if isinstance(f, str))
    validation = openpyxl.load_workbook(workbook[0], data_only=True)["Validation"]
    assert validation["D4"].value == pytest.approx(0.0, abs=1e-6)
    assert str(validation["E4"].value).startswith("PASS")


def test_settings_holds_inputs_and_calibration(workbook) -> None:
    settings = openpyxl.load_workbook(workbook[0])["Settings"]
    assert settings["B4"].value == "°C"
    assert settings["B9"].value == pytest.approx(0.95)
    assert settings["B13"].value == "Manual value"  # initial transmission is a number
    values = openpyxl.load_workbook(workbook[0], data_only=True)["Settings"]
    row = next(r for r in range(18, 30) if values.cell(r, 2).value == "Camera A")
    assert values.cell(row, 19).value == pytest.approx(T650.planck.R)  # column S, collapsed
    assert settings.column_dimensions["S"].hidden


def test_timeline_is_aligned_to_ignition() -> None:
    options = ExportOptions(step_s=2.0)
    times = timeline([(-5.3, 9.9), (-1.0, 20.2)], options, 30.0)
    assert times[0] == -4.0 and times[-1] == 20.0 and 0.0 in times
    frames = frames_for(times, ignition=100, fps=30.0, num_frames=400)
    assert frames[list(times).index(0.0)] == 100
    assert frames[-1] == 700 or frames[-1] == -1
    every = timeline([(-0.1, 0.1)], ExportOptions(every_frame=True), 30.0)
    assert np.allclose(np.diff(every), 1 / 30.0)
    with pytest.raises(ValueError):
        timeline([(0.0, 1.0)], ExportOptions(start_s=5.0, end_s=4.0), 30.0)


def test_bins_bound_the_mean_temperature_error() -> None:
    rng = np.random.default_rng(3)
    values = np.concatenate([rng.normal(7200, 30, 9000), rng.uniform(9000, 45000, 2200)])
    exact = object_temperature(T650, PARAMS, values).mean()
    block = bin_counts(values, 128, T650.planck.temperature)
    n, m = block[:128], block[128:]
    approx = (n * object_temperature(T650, PARAMS, m)).sum() / n.sum()
    assert n.sum() == values.size
    assert abs(approx - exact) < 0.02
    assert np.all(np.isfinite(object_temperature(T650, PARAMS, m)))  # empty bins stay finite


def test_estimate_scales_with_rows_and_pixels() -> None:
    small = estimate(100, [("cursor", 1)], ExportOptions())
    large = estimate(100, [("rect", 5000)], ExportOptions())
    assert large["cells"] > small["cells"] > 0


def test_roi_set_round_trip(tmp_path) -> None:
    recording = tmp_path / "test.csq"
    rois = (RoiShape(4, "cursor", ((1.5, 2.5),), "TC1"),
            RoiShape(9, "ellipse", ((1.0, 2.0), (5.0, 6.0)), "Circle"))
    save_roi_set(roi_set_path(recording), RoiSet(rois, ignition_frame=123, recording="test.csq", size=(64, 48)))
    loaded = load_roi_set(roi_set_path(recording))
    assert roi_set_path(recording).name == "test.csq.rois.json"
    assert [(r.kind, r.points, r.name) for r in loaded.rois] == [(r.kind, r.points, r.name) for r in rois]
    assert loaded.ignition_frame == 123 and loaded.size == (64, 48)
    bad = tmp_path / "bad.json"
    bad.write_text('{"format": "something else"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_roi_set(bad)
