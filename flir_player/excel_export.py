# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Raw-count extraction for the Excel workbook (Qt-free; runs on the SDK thread).

One or more recordings (e.g. several cameras filming one fire test) are
sampled on a shared timeline in seconds from each recording's ignition frame,
because the cameras' clocks are aligned by hand at ignition. For every sample
and ROI the raw counts are kept:

* spots: the pixel's count;
* areas up to ``pixel_limit`` pixels: every pixel's count, so Excel computes an
  exact mean temperature for any emissivity;
* larger areas: equal-count bins (pixel count and mean count per bin), whose
  mean temperature Excel computes to within the error the Validation sheet
  reports.

Extremes and percentiles are stored as counts too; they convert exactly.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace as dataclass_replace  # run_export has a replace= argument
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

try:
    import fnv
    import fnv.file
except ModuleNotFoundError:
    fnv = None  # type: ignore[assignment]

from .calibration import (
    CalibrationReport,
    derive_calibration,
    preserved_state,
    read_array,
    sdk_version,
    user_calibration_risk,
    set_unit_safely,
    write_parameters,
)
from .geometry import roi_coordinates
from .jobs import JobCancelled, OutputTransaction
from .models import FrameRate, RoiShape
from .radiometry import MeasurementParameters, count_status
from .fff import saved_object_parameters
from .sdktime import TimestampRepair, recording_rate

ROI_SET_FORMAT = "flir-player-roi-set"
EXCEL_COLUMNS = 16_384
AREA_STATS = ("mean", "max", "min")
EXTRA_STATS = ("median", "p95")
ROI_KINDS = ("cursor", "rect", "ellipse", "line")


# --- ROI sets -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoiSet:
    rois: tuple[RoiShape, ...]
    ignition_frame: int | None = None
    recording: str = ""
    size: tuple[int, int] | None = None  # (width, height)


def roi_set_path(recording: str | Path) -> Path:
    """Default ROI-set file kept next to a recording."""
    recording = Path(recording)
    return recording.with_name(recording.name + ".rois.json")


def save_roi_set(path: str | Path, roi_set: RoiSet) -> None:
    payload = {
        "format": ROI_SET_FORMAT,
        "version": 1,
        "recording": roi_set.recording,
        "size": list(roi_set.size) if roi_set.size else None,
        "ignition_frame": roi_set.ignition_frame,
        "rois": [{"name": shape.name, "kind": shape.kind,
                  "points": [[float(x), float(y)] for x, y in shape.points]}
                 for shape in roi_set.rois],
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_roi_set(path: str | Path) -> RoiSet:
    """Read a ROI set; anything malformed raises ValueError (or OSError)."""
    name = Path(path).name
    payload = json.loads(Path(path).read_text(encoding="utf-8"))  # JSONDecodeError is a ValueError
    if not isinstance(payload, dict) or payload.get("format") != ROI_SET_FORMAT:
        raise ValueError(f"{name} is not a FLIR Thermal Player ROI set")
    items = payload.get("rois", [])
    if not isinstance(items, list):
        raise ValueError(f"{name}: the ROI list is not valid")
    rois = []
    for number, item in enumerate(items, start=1):
        try:
            kind = str(item["kind"])
            points = tuple((float(x), float(y)) for x, y in item["points"])
            label = str(item.get("name") or f"ROI {number}")
        except (TypeError, KeyError, ValueError, AttributeError, OverflowError) as exc:
            raise ValueError(f"ROI {number} in {name} is not valid") from exc
        if (kind not in ROI_KINDS or len(points) != (1 if kind == "cursor" else 2)
                or not all(math.isfinite(v) for point in points for v in point)):
            raise ValueError(f"ROI {number} in {name} is not valid")
        # names are shown on one line (readout badge, workbook headers)
        label = " ".join(part.strip() for part in label.splitlines() if part.strip()) or f"ROI {number}"
        rois.append(RoiShape(id=number, kind=kind, points=points, name=label))
    try:
        ignition = payload.get("ignition_frame")
        size = payload.get("size")
        return RoiSet(rois=tuple(rois), ignition_frame=None if ignition is None else int(ignition),
                      recording=str(payload.get("recording", "")),
                      size=tuple(int(v) for v in size) if size else None)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name}: the ignition frame or image size is not valid") from exc


# --- request ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """One recording of the workbook."""

    path: Path
    rois: tuple[RoiShape, ...]
    ignition_frame: int = 0
    label: str = ""
    parameters: dict | None = None  # SDK object-parameter values; None = the file's own


@dataclass(frozen=True, slots=True)
class ExportOptions:
    start_s: float | None = None  # seconds from ignition; None = earliest data
    end_s: float | None = None  # None = latest data
    step_s: float = 1.0
    # one row per frame interval of the first recording (its mean rate): exactly
    # one row per frame with the "frames" time base; with "clock" each row takes
    # the frame nearest its time, so frames repeat or drop where the clock has gaps
    every_frame: bool = False
    time_base: str = "frames"  # "frames": index / frame rate; "clock": camera timestamps
    extra_stats: tuple[str, ...] = ()
    pixel_limit: int = 400
    bins: int = 128
    validation_rows: int = 24
    sheets: frozenset[str] = frozenset({"charts", "tc", "summary", "roimap", "validation"})
    unit: str = "°C"


# --- collected data -------------------------------------------------------------------


@dataclass
class RoiData:
    shape: RoiShape
    pixels: int
    mode: str  # "spot" | "pixels" | "bins"
    counts: dict[str, np.ndarray]  # stat → counts per row (NaN where no frame)
    block: np.ndarray | None  # rows × pixels, or rows × (2 · bins): counts then mean counts
    status: np.ndarray  # int8 per row (radiometry.STATUS_LABELS; -1 = no frame)
    validation: dict[str, np.ndarray] = field(default_factory=dict)  # stat → SDK kelvin
    validation_clamped: np.ndarray | None = None  # bool per validation row


@dataclass
class SourceData:
    spec: SourceSpec
    label: str
    info: dict[str, Any]
    fps: float
    num_frames: int
    frames: np.ndarray  # frame index per row, -1 when outside the recording
    clock: list[datetime | None]
    rois: list[RoiData]
    report: CalibrationReport
    initial: MeasurementParameters
    file_parameters: MeasurementParameters
    validation_rows: np.ndarray  # row indices
    map_png: bytes | None = None


@dataclass
class ExportData:
    times: np.ndarray  # seconds from ignition per row
    sources: list[SourceData]
    options: ExportOptions
    created: datetime
    versions: dict[str, str]


# --- timeline -----------------------------------------------------------------------


def source_span(num_frames: int, fps: float, ignition: int) -> tuple[float, float]:
    """Earliest and latest time (s from ignition) a recording covers."""
    return (0 - ignition) / fps, (num_frames - 1 - ignition) / fps


def timeline(spans: list[tuple[float, float]], options: ExportOptions, first_fps: float) -> np.ndarray:
    """Sample times, whole multiples of the interval from ignition (t = 0 is a row)."""
    step = 1.0 / first_fps if options.every_frame else float(options.step_s)
    if not step > 0:
        raise ValueError("The sampling interval must be positive")
    start = min(s for s, _ in spans) if options.start_s is None else float(options.start_s)
    end = max(e for _, e in spans) if options.end_s is None else float(options.end_s)
    first = math.ceil(start / step - 1e-9)
    last = math.floor(end / step + 1e-9)
    if last < first:
        raise ValueError("The time range is empty")
    count = last - first + 1
    if count > 1_048_000:
        raise ValueError(f"{count} rows exceed Excel's sheet limit; use a longer interval")
    return step * np.arange(first, last + 1, dtype=np.float64)


def frames_for(times: np.ndarray, ignition: int, fps: float, num_frames: int) -> np.ndarray:
    frames = ignition + np.rint(times * fps).astype(np.int64)
    return np.where((frames >= 0) & (frames < num_frames), frames, -1)


def estimate(rows: int, rois: list[tuple[str, int]], options: ExportOptions) -> dict[str, float]:
    """Rough size of a workbook: rows and (pixels per ROI) → cells and MB."""
    stats = len(AREA_STATS) + len(options.extra_stats)
    cells = 0
    for kind, pixels in rois:
        if kind == "cursor":
            cells += rows * 3  # counts, temperature, status
        else:
            block = pixels if pixels <= options.pixel_limit else 2 * options.bins
            cells += rows * (2 * stats + 1 + block)
    if "tc" in options.sheets:
        cells += rows * 7 * len(rois)
    return {"rows": rows, "cells": cells, "megabytes": cells * 14 / 1e6}


def fit_pixel_limit(pixel_counts: list[int], options: ExportOptions) -> int:
    """Largest pixel limit whose Pixels sheet fits Excel's 16,384 columns.

    An area ROI stores its pixels (width n) up to the limit, else bins (width
    2·bins). Binning only saves columns for ROIs wider than 2·bins, so the
    limit is lowered no further than that; beyond it the layout cannot fit.
    """
    budget = EXCEL_COLUMNS - 1  # column A holds the time

    def width(limit: int) -> int:
        return sum(n if n <= limit else 2 * options.bins for n in pixel_counts)

    floor = 2 * options.bins
    candidates = sorted({options.pixel_limit} | {n for n in pixel_counts if floor <= n < options.pixel_limit}
                        | {min(floor, options.pixel_limit)}, reverse=True)
    for limit in candidates:
        if width(limit) <= budget:
            return limit
    raise ValueError(f"{len(pixel_counts)} area ROIs need {width(min(floor, options.pixel_limit)):,} columns, "
                     f"more than an Excel sheet has ({EXCEL_COLUMNS:,}); export fewer ROIs per workbook")


class _ClockSearch:
    """Nearest-timestamp frame search on (non-decreasing) camera timestamps.

    ``repair`` maps raw SDK stamps to calendar time (``sdktime.TimestampRepair``);
    ``origin`` must already be repaired.
    """

    def __init__(self, im: Any, n: int, origin: datetime, repair: Callable[[Any], Any] | None = None):
        self.im, self.n, self.origin = im, n, origin
        self.repair = repair or (lambda stamp: stamp)
        self._seconds: dict[int, float] = {}

    def time(self, index: int) -> float:
        """Seconds of frame ``index`` from ``origin`` (the ignition frame's stamp)."""
        if index not in self._seconds:
            self.im.get_frame(int(index))
            stamp = self.repair(self.im.frame_info.time)
            self._seconds[index] = ((stamp - self.origin).total_seconds()
                                    if isinstance(stamp, datetime) else math.nan)
        return self._seconds[index]

    def span(self) -> tuple[float, float]:
        return self.time(0), self.time(self.n - 1)

    def nearest(self, target: float, guess: int, tolerance: float) -> int:
        """Frame nearest ``target`` seconds, or -1 outside the recording."""
        n = self.n
        first, last = self.span()
        if not first - tolerance <= target <= last + tolerance:
            return -1
        if n == 1:
            return 0
        g = max(0, min(n - 1, int(guess)))
        # gallop from the guess until t(lo) <= target < t(hi) brackets it
        if self.time(g) <= target:
            lo, step = g, 1
            hi = min(n - 1, g + step)
            while hi < n - 1 and self.time(hi) <= target:
                lo, step = hi, step * 2
                hi = min(n - 1, g + step)
            if self.time(hi) <= target:
                return hi  # at or after the last frame
        else:
            hi, step = g, 1
            lo = max(0, g - step)
            while lo > 0 and self.time(lo) > target:
                hi, step = lo, step * 2
                lo = max(0, g - step)
            if self.time(lo) > target:
                return lo  # before the first frame
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.time(mid) <= target:
                lo = mid
            else:
                hi = mid
        return lo if target - self.time(lo) <= self.time(hi) - target else hi


# --- collection -----------------------------------------------------------------------


def _region(values: np.ndarray, roi: RoiShape, options: ExportOptions, apparent=None):
    """(stat counts, block row) of one ROI in one frame."""
    if roi.kind == "cursor":
        return {"value": float(values[0])}, None
    stats = {"min": float(values.min()), "max": float(values.max()), "mean": float(values.mean())}
    if "median" in options.extra_stats:
        stats["median"] = float(np.percentile(values, 50, method="lower"))
    if "p95" in options.extra_stats:
        stats["p95"] = float(np.percentile(values, 95, method="lower"))
    if values.size <= options.pixel_limit:
        return stats, values
    return stats, bin_counts(values, options.bins, apparent)


def bin_counts(values: np.ndarray, bins: int, apparent=None) -> np.ndarray:
    """Pixel count and mean count of ``bins`` bins, concatenated.

    Bins have equal width in apparent (blackbody) temperature when
    ``apparent`` (counts → K) is given, which bounds each bin's temperature
    spread: for a flame-adjacent 11,200-pixel box the mean temperature is then
    within 0.01 K of the exact one with 128 bins (0.37 K with 64 equal-count
    bins). Empty bins get weight 0 and a valid count so formulas stay finite.
    """
    ordered = np.sort(np.asarray(values, dtype=np.float64))
    index = None
    if apparent is not None:
        kelvin = apparent(ordered)
        if np.all(np.isfinite(kelvin)) and kelvin[-1] > kelvin[0]:
            edges = np.linspace(kelvin[0], kelvin[-1], bins + 1)
            index = np.clip(np.searchsorted(edges, kelvin, side="right") - 1, 0, bins - 1)
    if index is None:
        index = (np.arange(ordered.size) * bins) // ordered.size
    counts = np.bincount(index, minlength=bins).astype(np.float64)
    sums = np.bincount(index, weights=ordered, minlength=bins)
    means = np.where(counts > 0, sums / np.maximum(counts, 1.0), ordered[0])
    return np.concatenate([counts, means])


def _saturation_threshold(im: Any) -> float | None:
    try:
        for entry in im.frame_info:
            if str(entry["name"]) == "SaturationThreshold":
                return float(str(entry["value"]))
    except Exception:
        pass
    return None


def collect_source(im: Any, spec: SourceSpec, times: np.ndarray, options: ExportOptions,
                   report: CalibrationReport, *, progress: Callable[[int], None] | None = None,
                   abort: Callable[[], bool] | None = None, rate: FrameRate | None = None) -> SourceData:
    """Sample one recording on ``times``; the handle's state is restored after.

    ``rate`` (``sdktime.recording_rate``) is measured here when not given.
    """
    n = int(im.num_frames)
    height, width = int(im.height), int(im.width)
    rois = [shape for shape in spec.rois if roi_coordinates(shape, height, width)[0].size]
    if not rois:
        raise ValueError(f"{spec.path.name}: no ROI covers any pixel")
    coordinates = [roi_coordinates(shape, height, width) for shape in rois]
    repair = TimestampRepair(spec.path)  # ATS clocks carry no year
    with preserved_state(im):
        set_unit_safely(im, fnv.Unit.COUNTS)
        stamps = []
        for index in (0, n - 1):
            im.get_frame(index)
            stamps.append(repair(im.frame_info.time))
        if rate is None:
            rate = recording_rate(im, repair)
        fps = rate.fps
        clock_fps = rate.clock_fps or fps  # camera timestamps: their own mean rate
        frames = frames_for(times, spec.ignition_frame, fps, n)
        search = None
        if options.time_base == "clock":
            im.get_frame(max(0, min(n - 1, spec.ignition_frame)))
            origin = repair(im.frame_info.time)
            if isinstance(origin, datetime):
                search = _ClockSearch(im, n, origin, repair)
                tolerance = 0.5 / clock_fps
        rows = times.size
        data = []
        for shape, (ys, _xs) in zip(rois, coordinates):
            names = ("value",) if shape.kind == "cursor" else AREA_STATS + tuple(options.extra_stats)
            block_width = (0 if shape.kind == "cursor" else
                           ys.size if ys.size <= options.pixel_limit else 2 * options.bins)
            data.append(RoiData(
                shape=shape, pixels=int(ys.size),
                mode="spot" if shape.kind == "cursor" else (
                    "pixels" if ys.size <= options.pixel_limit else "bins"),
                counts={name: np.full(rows, np.nan) for name in names},
                block=np.full((rows, block_width), np.nan) if block_width else None,
                status=np.full(rows, -1, dtype=np.int8)))
        clock: list[datetime | None] = [None] * rows
        saturation = None
        apparent = report.calibration.planck.temperature if report.verified else None
        for row in range(rows):
            if abort is not None and abort():
                raise JobCancelled("Export cancelled")
            frame = int(frames[row])
            if search is not None:
                guess = spec.ignition_frame + int(round(times[row] * clock_fps))
                frame = search.nearest(float(times[row]), guess, tolerance)
                frames[row] = frame
            if frame < 0:
                continue
            values = read_array(im, frame).astype(np.float64)
            clock[row] = repair(im.frame_info.time)
            if saturation is None:
                saturation = _saturation_threshold(im) or 0.0
            for roi, (ys, xs) in zip(data, coordinates):
                stats, block = _region(values[ys, xs], roi.shape, options, apparent)
                for name, value in stats.items():
                    roi.counts[name][row] = value
                if block is not None:
                    roi.block[row, :block.size] = block
            if progress is not None:
                progress(row + 1)
        for roi in data:
            low = roi.counts.get("min", roi.counts.get("value"))
            high = roi.counts.get("max", roi.counts.get("value"))
            present = np.isfinite(high)
            if report.verified:
                status = count_status(report.calibration, low, high)
            else:
                status = np.zeros(rows, dtype=np.int8)
                if saturation:
                    status = np.where(high >= saturation, 3, status).astype(np.int8)
            roi.status = np.where(present, status, -1).astype(np.int8)
        validation_rows = _validation_rows(frames, options.validation_rows) if report.verified else np.empty(0, int)
        if validation_rows.size:
            _validate(im, data, coordinates, frames, validation_rows, options)
        map_png = _roi_map(im, rois, spec, frames) if "roimap" in options.sheets else None
    file_parameters = MeasurementParameters.from_sdk(report.file_parameters) if report.file_parameters else MeasurementParameters()
    initial = MeasurementParameters.from_sdk(spec.parameters) if spec.parameters else file_parameters
    info = _source_info(im, spec, fps, stamps, report, saturation)
    info["date_inferred"] = repair.repaired
    info["clock_fps"] = rate.clock_fps
    info["rate_corrected"] = rate.corrected
    saved = saved_object_parameters(spec.path)
    if saved is not None and any(not math.isclose(value, report.file_parameters.get(key, value),
                                                  rel_tol=1e-5, abs_tol=1e-5)
                                 for key, value in saved.items()):
        info["saved_parameters"] = saved
        info["saved_parameters_used"] = same_parameters(MeasurementParameters.from_sdk(saved), initial)
    return SourceData(spec=spec, label=spec.label or info["camera_short"], info=info, fps=fps,
                      num_frames=n, frames=frames, clock=clock, rois=data, report=report,
                      initial=initial, file_parameters=file_parameters,
                      validation_rows=validation_rows, map_png=map_png)


def same_parameters(a: MeasurementParameters, b: MeasurementParameters) -> bool:
    """True when two parameter sets give the same temperatures (to float tolerance)."""
    def close(x, y) -> bool:
        if x is None or y is None:
            return x is y
        return math.isclose(float(x), float(y), rel_tol=1e-5, abs_tol=1e-5)
    fields = ("emissivity", "reflected_k", "atmosphere_k", "transmission", "window_transmission", "window_k")
    if not all(close(getattr(a, name), getattr(b, name)) for name in fields):
        return False
    if a.transmission is None:  # automatic transmission also depends on distance and humidity
        return close(a.distance_m, b.distance_m) and close(a.humidity, b.humidity)
    return True


def _validation_rows(frames: np.ndarray, count: int) -> np.ndarray:
    present = np.flatnonzero(frames >= 0)
    if not present.size or count <= 0:
        return np.empty(0, dtype=int)
    picks = np.unique(np.rint(np.linspace(0, present.size - 1, min(count, present.size))).astype(int))
    return present[picks]


def _validate(im, data, coordinates, frames, rows, options) -> None:
    """SDK temperatures (file-default parameters, kelvin) on a few rows."""
    im.reset_object_parameters()
    set_unit_safely(im, fnv.Unit.TEMPERATURE_FACTORY, fnv.TempType.KELVIN)
    for roi in data:
        names = ("value",) if roi.mode == "spot" else AREA_STATS + tuple(options.extra_stats)
        roi.validation = {name: np.full(rows.size, np.nan) for name in names}
        roi.validation_clamped = np.zeros(rows.size, dtype=bool)
    for k, row in enumerate(rows):
        kelvin = read_array(im, int(frames[row])).astype(np.float64)
        status = np.array(im.status, copy=True).reshape(kelvin.shape)
        for roi, (ys, xs) in zip(data, coordinates):
            values = kelvin[ys, xs]
            roi.validation_clamped[k] = bool(np.any(status[ys, xs] & (2 | 4)))
            if roi.mode == "spot":
                roi.validation["value"][k] = values[0]
                continue
            roi.validation["mean"][k] = values.mean()
            roi.validation["max"][k] = values.max()
            roi.validation["min"][k] = values.min()
            if "median" in roi.validation:
                roi.validation["median"][k] = np.percentile(values, 50, method="lower")
            if "p95" in roi.validation:
                roi.validation["p95"][k] = np.percentile(values, 95, method="lower")


def _roi_map(im, rois, spec: SourceSpec, frames: np.ndarray) -> bytes | None:
    """PNG of the ignition frame (apparent counts, Iron) with ROIs and names."""
    import io

    from PIL import Image

    from .compose import ExportOptions as ComposeOptions, compose_frame
    from .render import palette_lut

    try:
        set_unit_safely(im, fnv.Unit.COUNTS)
        n = int(im.num_frames)
        values = read_array(im, max(0, min(n - 1, spec.ignition_frame))).astype(np.float64)
        low, high = np.percentile(values, (1, 99.7))
        high = max(high, low + 1)
        lut = palette_lut("Iron")
        index = np.clip((values - low) / (high - low) * (len(lut) - 1), 0, len(lut) - 1).astype(np.intp)
        rgb = np.ascontiguousarray(lut[index][..., :3]).astype(np.uint8)
        composed = compose_frame(rgb, ComposeOptions(color_bar=False, rois=True, roi_names=True),
                                 palette="Iron", rois=tuple(rois))
        buffer = io.BytesIO()
        Image.fromarray(composed).save(buffer, "PNG")
        return buffer.getvalue()
    except Exception:
        return None


def _source_info(im, spec, fps, stamps, report, saturation) -> dict[str, Any]:
    si = im.source_info
    model = str(getattr(si, "camera_model", "") or "").strip()
    info = {
        "file": str(spec.path),
        "file_name": spec.path.name,
        "file_size": spec.path.stat().st_size if spec.path.exists() else 0,
        "camera": model,
        "camera_short": model.replace("FLIR ", "") or spec.path.stem,
        "camera_serial": str(getattr(si, "camera_serial", "") or "").strip(),
        "lens": str(getattr(si, "lens", "") or "").strip(),
        "width": int(im.width),
        "height": int(im.height),
        "frames": int(im.num_frames),
        "fps": fps,
        "start": stamps[0],
        "end": stamps[1],
        "ignition_frame": spec.ignition_frame,
        "calibration": report.message,
        "saturation_threshold": saturation or None,
    }
    if report.fff is not None:
        info["camera_serial"] = info["camera_serial"] or report.fff["camera_serial"]
        info["lens"] = info["lens"] or report.fff["lens_model"]
    return info


# --- orchestration ----------------------------------------------------------------------


def open_recording(path: Path) -> Any:
    return fnv.file.ImagerFile(str(path))


def run_export(dest: str | Path, specs: list[SourceSpec], options: ExportOptions, *,
               handles: dict[Path, Any] | None = None, progress=None, abort=None,
               replace=(), tool_version: str = "") -> str:
    """Build the workbook; ``handles`` lends already-open ImagerFile objects.

    Borrowed handles are restored to their unit, scale and object parameters;
    recordings without a lent handle are opened and closed here.
    """
    from .workbook import write_workbook

    if not specs:
        raise ValueError("Add at least one recording")
    handles = dict(handles or {})
    owned: list[Any] = []
    abort = abort or (lambda: False)
    try:
        opened = []
        for spec in specs:
            key = Path(spec.path).resolve()
            im = handles.get(key)
            if im is not None and user_calibration_risk(im):
                # FileSDK 5.0.1 corrupts memory on unit changes of user-calibrated
                # recordings: leave the player's handle alone, use a fresh one
                # that never leaves COUNTS (ATS opens instantly).
                im = None
            if im is None:
                im = open_recording(key)
                owned.append(im)
            opened.append(im)
        infos = []
        for spec, im in zip(specs, opened):
            n = int(im.num_frames)
            if not 0 <= spec.ignition_frame < n:
                raise ValueError(f"{spec.path.name}: ignition frame {spec.ignition_frame + 1} is outside the recording")
            infos.append(n)
        reports = []
        for spec, im in zip(specs, opened):
            if abort():
                raise JobCancelled("Export cancelled")
            reports.append(derive_calibration(im, spec.path, abort=abort))
        spans, rates, pixel_counts, measured = [], [], [], []
        for spec, im in zip(specs, opened):
            repair = TimestampRepair(spec.path)  # an ATS clock can roll over at New Year
            with preserved_state(im):
                set_unit_safely(im, fnv.Unit.COUNTS)
                n = int(im.num_frames)
                first = _stamp(im, 0, repair)
                last = _stamp(im, n - 1, repair)
                ignition = _stamp(im, spec.ignition_frame, repair)
                rate = recording_rate(im, repair)
            measured.append(rate)
            fps = rate.fps
            # rows every frame interval: the frame rate, or the clock's mean rate
            rates.append(rate.clock_fps if options.time_base == "clock" and rate.clock_fps > 0 else fps)
            if options.time_base == "clock" and first and last and ignition:
                spans.append(((first - ignition).total_seconds(), (last - ignition).total_seconds()))
            else:
                spans.append(source_span(n, fps, spec.ignition_frame))
            height, width = int(im.height), int(im.width)
            pixel_counts += [int(roi_coordinates(shape, height, width)[0].size) for shape in spec.rois
                             if shape.kind != "cursor"]
        pixel_limit = fit_pixel_limit([count for count in pixel_counts if count], options)
        if pixel_limit != options.pixel_limit:
            options = dataclass_replace(options, pixel_limit=pixel_limit)
        times = timeline(spans, options, rates[0])
        total = times.size * len(specs) + 1
        done = 0
        sources = []
        labels_seen: dict[str, int] = {}
        for spec, im, report, rate in zip(specs, opened, reports, measured):
            offset = done

            def tick(value, offset=offset):
                if progress is not None:
                    progress(offset + value, total)

            source = collect_source(im, spec, times, options, report, progress=tick, abort=abort, rate=rate)
            count = labels_seen.get(source.label, 0) + 1
            labels_seen[source.label] = count
            if count > 1:
                source.label = f"{source.label} ({count})"
            sources.append(source)
            done += times.size
        data = ExportData(times=times, sources=sources, options=options, created=datetime.now(),
                          versions={"tool": tool_version, "sdk": sdk_version()})
        sources_paths = [Path(spec.path) for spec in specs]
        with OutputTransaction(sources_paths, abort, replace=replace) as job:
            stage = job.stage(Path(dest))
            write_workbook(stage, data, abort=abort)
            job.commit()
        if progress is not None:
            progress(total, total)
        verified = sum(1 for s in sources if s.report.verified)
        return (f"Wrote {Path(dest).name}: {times.size} rows, {sum(len(s.rois) for s in sources)} ROIs, "
                f"{verified}/{len(sources)} recording(s) with live temperatures")
    finally:
        for im in owned:
            try:
                im.close()
            except Exception:
                pass


def _stamp(im, index: int, repair: Callable[[Any], Any]) -> datetime | None:
    im.get_frame(int(index))
    stamp = repair(im.frame_info.time)
    return stamp if isinstance(stamp, datetime) else None
