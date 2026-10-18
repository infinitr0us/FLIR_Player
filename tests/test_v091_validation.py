# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regressions for 0.9.1: the Validation sheet checks every area's max and min, with or without "Max and min".

With more than 8 areas the Excel dialog starts with means from the mean signal and no max/min columns. In 0.9.0
every Validation row was then an approximation, nothing was compared, and the sheet said "NOT CHECKED — the SDK
clamped every sample" although nothing was clamped (local/notes/2026-10-18-a8303-answers/FINDINGS.md).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

openpyxl = pytest.importorskip("openpyxl")

from flir_player.excel_export import ExportData, ExportOptions, SourceSpec, run_export  # noqa: E402
from flir_player.models import RoiShape  # noqa: E402
from flir_player.workbook import write_workbook  # noqa: E402

from test_excel_workbook import _source  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"
SUMMARY = "Largest |difference| over checked samples (K)"


def _areas(seed: int, *, mean_signal: bool = True):
    """A test source with its two areas only (no spot, whose value would be checked anyway)."""
    source = _source("Camera A", 12, np.random.default_rng(seed))
    source.rois = [roi for roi in source.rois if roi.mode != "spot"]
    if mean_signal:  # stored as a mean-signal export stores them: no pixel block
        for roi in source.rois:
            roi.mode, roi.block = "mean", None
    return source


def _write(tmp_path, sources, name="book.xlsx", **options) -> Path:
    path = tmp_path / name
    write_workbook(path, ExportData(times=np.arange(12, dtype=float) - 2.0, sources=sources,
                                    options=ExportOptions(bins=16, **options), created=datetime(2026, 10, 18),
                                    versions={"tool": "test", "sdk": ""}))
    return path


def _validation(path: Path):
    """(summary row, sample rows) of the Validation sheet: cached values and formulas side by side."""
    values = list(openpyxl.load_workbook(path, data_only=True)["Validation"].iter_rows(values_only=True))
    formulas = list(openpyxl.load_workbook(path)["Validation"].iter_rows(values_only=True))
    summary = next(row for row in values if row[0] == SUMMARY)
    start = next(k for k, row in enumerate(values) if row[0] == "Recording") + 1
    samples = [(v, f) for v, f in zip(values[start:], formulas[start:]) if v[0]]
    return summary, samples


def test_mean_signal_areas_without_max_and_min_are_checked_by_their_extremes(tmp_path) -> None:
    path = _write(tmp_path, [_areas(5)], area_means="signal", extremes=False)
    data_head = list(openpyxl.load_workbook(path)["Data"].iter_rows(min_row=5, max_row=5, values_only=True))[0]
    assert not any(str(h).startswith(("Max", "Min")) for h in data_head if h)  # the dialog's default case
    summary, samples = _validation(path)
    assert isinstance(summary[3], float) and summary[3] <= 1e-6 and summary[4].startswith("PASS")
    stats = {(v[1], v[2]) for v, _ in samples}
    assert stats == {(roi, stat) for roi in ("TC2", "Box") for stat in ("Mean", "Max", "Min")}
    for value, formula in samples:
        if value[2] == "Mean":
            assert "approximation" in value[9] and value[8] in (None, "")
        else:  # the extreme pixel's count is in the formula: Max and min have no Counts column
            assert "Counts" not in formula[6] and "in the formula" in value[9]
            assert abs(value[7]) <= 1e-6 and value[8] == pytest.approx(abs(value[7]))


def test_exported_max_and_min_are_checked_from_the_counts_sheet(tmp_path) -> None:
    path = _write(tmp_path, [_areas(6)], area_means="signal", extremes=True)
    summary, samples = _validation(path)
    assert summary[4].startswith("PASS")
    extremes = [(v, f) for v, f in samples if v[2] in ("Max", "Min")]
    assert extremes and all("Counts" in f[6] and v[9].startswith("exact") for v, f in extremes)


def test_every_sample_clamped_says_so(tmp_path) -> None:
    source = _areas(7)
    for roi in source.rois:
        roi.validation_clamped[:] = True
    summary, samples = _validation(_write(tmp_path, [source], area_means="signal", extremes=False))
    assert summary[3] == "–" and summary[4] == "NOT CHECKED — the SDK clamped every sample"
    assert samples and all(v[9].startswith("skipped") for v, _ in samples)


def test_only_approximations_say_so(tmp_path) -> None:
    source = _areas(8)
    for roi in source.rois:  # no extreme where the samples are: only the mean-signal means are left
        for stat in ("max", "min"):
            roi.counts[stat] = roi.counts[stat].copy()
            roi.counts[stat][source.validation_rows] = np.nan
    summary, samples = _validation(_write(tmp_path, [source], area_means="signal", extremes=False))
    assert summary[3] == "–" and summary[4] == "NOT CHECKED — no exact sample to compare"
    assert samples and all(v[2] == "Mean" for v, _ in samples)


def test_a_frameless_extreme_is_left_out_not_written_as_nan(tmp_path) -> None:
    source = _areas(9)
    box = source.rois[1]
    box.counts["max"] = box.counts["max"].copy()
    box.counts["max"][source.validation_rows[0]] = np.nan
    summary, samples = _validation(_write(tmp_path, [source], area_means="signal", extremes=False))
    assert summary[4].startswith("PASS")
    rows = [v for v, _ in samples if v[1] == "Box" and v[2] == "Max"]
    assert len(rows) == source.validation_rows.size - 1


def test_a_recording_exported_with_the_many_area_defaults_passes(tmp_path) -> None:
    pytest.importorskip("fnv")
    rois = tuple(RoiShape(k + 1, "rect", ((266.0 + 12 * k, 236.0), (274.0 + 12 * k, 244.0)), f"Box {k + 1}")
                 for k in range(9))
    dest = tmp_path / "defaults.xlsx"
    run_export(dest, [SourceSpec(path=SAMPLES / "2.seq", rois=rois)],
               ExportOptions(start_s=0.0, end_s=5.0, step_s=1.0, area_means="signal", extremes=False))
    summary, samples = _validation(dest)
    assert isinstance(summary[3], float) and summary[3] < 1e-3 and summary[4].startswith("PASS")
    assert {v[2] for v, _ in samples} == {"Mean", "Max", "Min"}
