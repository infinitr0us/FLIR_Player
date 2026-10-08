# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find thermocouple pixels in an IR recording and fit the surface emissivity (Qt-free).

The method of the 0922 battery test, made reusable:

1. ``sample_recording`` reads every frame once (on a superframing recording,
   the frames of one preset) and keeps, per time bin and pixel of a region,
   the mean count and the detrended spread of the counts within the bin. Bins
   sit on the recording's frame-number time base, so A700 recordings get the
   slow-clock correction of ``sdktime``; superframing frames are timed by the
   camera clock (``preset_frames``), which dropped frames do not shift.
2. ``locate`` correlates every pixel's counts with the blackbody signal of
   every TC temperature, over all time offsets. Counts are linear in the
   radiance the camera receives, and that radiance is linear in the blackbody
   signal of the surface for any emissivity, reflected temperature and
   atmosphere, so at the TC's own pixel the relation is a straight line (r = 1
   up to noise) whatever the emissivity. (Apparent temperatures would bend it
   for ε < 1, and a neighbouring pixel with a different amplitude could then
   correlate better.) The logger has one clock, so one offset serves all TCs;
   each TC then gets the pixel whose history matches its shape best, plus the
   nearby pixels that match almost as well (the position uncertainty).
3. ``select_events`` picks the stretches to fit by rules that never look at
   how well IR and TC agree: TC working and hot enough, no flames in front of
   the spot (flicker), IR inside the calibrated range.
4. ``fit_event`` fits the emissivity per event; ``analyse`` adds the spread
   over the candidate pixels and checks that the events agree with each other
   (an emissivity fitted on one event should predict the others).

Seconds: "recording time" is frame index / frame rate; "logger time" is the
TC file's own time column. ``lag_s`` is the recording time of logger time 0.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import warnings
import zipfile
from dataclasses import asdict, dataclass, field, replace as dataclass_replace  # write_workbook has a replace= argument
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

try:
    import fnv
    import fnv.file
except ModuleNotFoundError:  # pure parts stay importable without the SDK
    fnv = None  # type: ignore[assignment]

from .calibration import CalibrationReport, derive_calibration, preserved_state, read_array, set_unit_safely
from .jobs import JobCancelled
from .models import RoiShape
from .radiometry import (
    KELVIN,
    Calibration,
    MeasurementParameters,
    RangeLimits,
    compensated_signal,
    coefficients,
    count_status,
    object_temperature,
)
from .sdktime import TimestampRepair, recording_rate
from .tcdata import ChannelFlags, TcTable, failure_flags, read_tc_table, resample, window_mask

Progress = Callable[[int, int], None]
Abort = Callable[[], bool]
MEMORY_LIMIT = 3e9  # bytes of samples kept in memory without a cache folder
MIN_TC_SAMPLES = 60  # a TC with fewer usable logger bins is not searched for
MIN_TC_RANGE_K = 10.0  # nor one whose readings span less than this (5th to 95th percentile)
REFINE_BINS = 10  # the binned offset is refined at full resolution within this many bins
AMBIGUOUS_SCORE = 0.01  # a shared offset this close to the best one (a minute or more away) is a warning
MAX_CANDIDATES = 200  # nearby pixels (best-correlated first) whose emissivity spread is reported
PHYSICAL_EPS_MAX = 1.05  # an event whose unconstrained fit wants more than this is not physical
EPS_GRID = np.round(np.arange(20, 1001) / 1000.0, 3)  # bounded fit: 0.02 to 1 (bare metal is low)
FIT_BLOCK = 2048  # samples per block of the bounded fit (memory: grid × block)


# --- options ----------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchOptions:
    """Settings of one analysis. Times are logger seconds unless noted."""

    roi: tuple[int, int, int, int] | None = None  # x0, y0, x1, y1 (end exclusive); None = whole image
    preset: int | None = None  # superframing preset; None = the lowest-temperature one
    step_s: float | None = None  # bin width; None = the logger interval
    channels: tuple[str, ...] = ()  # TC channels to use; () = all
    valid: Mapping[str, tuple[tuple[float, float], ...]] = field(default_factory=dict)  # per channel
    scope: tuple[float, float] | None = None  # logger window for all channels (e.g. before a quench)
    search_outside_windows: bool = False  # True: the offset/pixel search also uses data outside ``valid``
    lag_s: float | None = None  # fixed offset (recording seconds of logger time 0)
    lag_frame: int | None = None  # fixed offset as the frame (0-based) of logger time 0; wins over ``lag_s``
    lag_range: tuple[float, float] | None = None  # search window for the offset (recording seconds)
    pixels: Mapping[str, tuple[int, int]] = field(default_factory=dict)  # fixed (row, col) per channel
    spot: int = 3  # spot block N (N × N pixels)
    min_overlap: float = 0.6  # share of a TC's usable samples the recording must cover
    min_r: float = 0.8  # a TC counts as found when its best correlation reaches this
    delta_r: float = 0.01  # candidate pixels: correlation within this of the best
    search_radius: int = 6  # candidate pixels lie within this many pixels of the best
    coarse_pixels: int = 12_000  # pixels of the binned image used to find the offset
    tc_min_c: float = 50.0  # fit only where the TC reads at least this
    flicker_max: float = 0.02  # flame filter: within-bin spread / (T − ambient)
    jump_k: float = 30.0  # a TC rising more than this within ``jump_window_s`` has a flame on it ...
    jump_window_s: float = 5.0
    jump_hold_s: float = 30.0  # ... and is not fitted from the jump until this long after it (0 = off)
    below_range: bool = False  # also fit IR below the calibrated range (extrapolated)
    bound_margin_k: float = 20.0  # IR at ε = 1 above the TC by more than this: no ε can match
    min_event_s: float = 30.0
    gap_s: float = 5.0  # shorter interruptions do not split an event
    reflected_c: float | None = None  # None = the recording's value
    consistent_eps: float = 0.05  # events agree when their emissivities span at most this


# --- sampling -----------------------------------------------------------------------------


@dataclass
class Samples:
    """Per time bin and pixel statistics of a region of one recording."""

    path: Path
    times: np.ndarray  # recording seconds of each bin centre
    mean: np.ndarray  # (bins, h, w) mean count, NaN where the bin has no frame
    spread: np.ndarray  # (bins, h, w) detrended within-bin SD of the count (NaN under 3 frames)
    counts: np.ndarray  # frames per bin
    first_frame: np.ndarray  # first frame index of each bin (-1 when empty)
    origin: tuple[int, int]  # (x0, y0) of the region in the image
    size: tuple[int, int]  # (width, height) of the image
    fps: float
    step: float
    report: CalibrationReport
    params: MeasurementParameters  # the recording's own object parameters
    info: dict[str, Any] = field(default_factory=dict)
    frame_seconds: np.ndarray | None = None  # recording seconds of every frame by its clock (superframing)

    @property
    def calibration(self) -> Calibration | None:
        return self.report.calibration if self.report.verified else None

    @property
    def flicker_available(self) -> bool:
        """True when typical bins hold the 3+ frames a within-bin spread needs."""
        filled = self.counts[self.counts > 0]
        return bool(filled.size and np.median(filled) >= 3)

    def apparent(self, counts) -> np.ndarray:
        """Temperature (°C) at emissivity 1 for counts of this recording."""
        return object_temperature(self.calibration, self.params.with_(emissivity=1.0), counts) - KELVIN

    def seconds_at(self, frame: int) -> float:
        """Recording time of frame ``frame`` (the inverse of ``frame_at``)."""
        t = self.frame_seconds
        if t is None or t.size == 0:
            return frame / self.fps
        if frame <= 0:
            return float(t[0] + frame / self.fps)
        if frame >= t.size:
            return float(t[-1] + (frame - t.size + 1) / self.fps)
        return float(t[frame])

    def frame_at(self, seconds: float) -> int:
        """Frame index at recording time ``seconds`` (beyond the ends: extrapolated at the frame rate)."""
        t = self.frame_seconds
        if t is None or t.size == 0:
            return int(round(seconds * self.fps))
        if seconds <= t[0]:
            return int(round((seconds - t[0]) * self.fps))
        if seconds >= t[-1]:
            return int(t.size - 1 + round((seconds - t[-1]) * self.fps))
        return int(np.argmin(np.abs(t - seconds)))


@dataclass(frozen=True)
class PresetLayout:
    """Frames of each preset of a superframing recording, and when each frame was taken (empty: one preset)."""

    frames: Mapping[int, np.ndarray] = field(default_factory=dict)  # preset → its frame indices, ascending
    ranges: Mapping[int, tuple[float, float]] = field(default_factory=dict)  # preset → calibrated range (K)
    seconds: np.ndarray | None = None  # clock time of every frame from the first
    breaks: int = 0  # places where the preset cycle breaks (dropped frames)

    def __bool__(self) -> bool:
        return bool(self.frames)


def preset_frames(im: Any, *, repair: Callable[[Any], Any] | None = None, progress: Progress | None = None,
                  abort: Abort | None = None) -> PresetLayout:
    """Which frames belong to which preset of a superframing recording, and their clock times.

    Every frame header is read once: its ``Preset`` field and its clock stamp
    (``repair`` fixes the stamps). Checking a sample of frames is not enough:
    two dropped frames can cancel out between the checks, and dropping a whole
    preset cycle keeps the order while frame number / frame rate drifts. So the
    frames of a superframing recording are always timed by their clock.
    """
    info = getattr(im, "source_info", None)
    if not _superframing(im):
        return PresetLayout()
    n = int(im.num_frames)
    repair = repair or (lambda stamp: stamp)
    labels = np.zeros(n, dtype=np.int64)
    seconds = np.full(n, np.nan)
    origin = None
    for i in range(n):
        if i % 256 == 0:
            _check(abort)
            if progress is not None:
                progress(i, n)
        im.get_frame(i)
        label = next((str(e["value"]) for e in im.frame_info if str(e["name"]) == "Preset"), None)
        if label is None:
            raise ValueError("The recording has several presets but its frames do not say which")
        labels[i] = int(label)
        stamp = repair(im.frame_info.time)
        if isinstance(stamp, datetime):
            origin = stamp if origin is None else origin
            seconds[i] = (stamp - origin).total_seconds()
    if progress is not None:
        progress(n, n)
    if not np.all(np.isfinite(seconds)) or np.any(np.diff(seconds) < 0):
        raise ValueError("The clock of this superframing recording cannot time its frames "
                         "(missing or backward time stamps)")
    frames = {int(p): np.flatnonzero(labels == p) for p in np.unique(labels)}
    pattern = list(dict.fromkeys(int(v) for v in labels[:4 * len(frames)]))  # the cycle, in order
    follows = {p: pattern[(k + 1) % len(pattern)] for k, p in enumerate(pattern)}
    breaks = int(sum(follows.get(int(a)) != int(b) for a, b in zip(labels[:-1], labels[1:])))
    ranges = {}
    all_presets = list(getattr(info, "preset_info", ()) or ())
    for p in frames:
        # the per-frame preset numbers count from 1 on superframing files, matching preset_info's index
        entry = all_presets[p] if 0 <= p < len(all_presets) else None
        if entry is not None and getattr(entry, "calibrated", False):
            ranges[p] = (float(entry.min_temp), float(entry.max_temp))
    return PresetLayout(frames=frames, ranges=ranges, seconds=seconds, breaks=breaks)


def choose_preset(layout: PresetLayout, requested: int | None = None) -> tuple[int | None, RangeLimits | None]:
    """(preset, its calibrated range as limits) of a superframing recording; (None, None) otherwise.

    Without a request, the preset with the lowest range: closest to TC temperatures.
    """
    if not layout:
        return None, None
    preset = requested
    if preset is None:
        preset = min(layout.frames, key=lambda p: layout.ranges.get(p, (math.inf, math.inf))[0])
    if preset not in layout.frames:
        raise ValueError(f"Preset {preset} is not in this recording (presets: "
                         f"{', '.join(str(p) for p in sorted(layout.frames))})")
    return preset, (RangeLimits(calibrated=tuple(layout.ranges[preset])) if preset in layout.ranges else None)


def _file_key(path: Path, *extra) -> str:
    stat = path.stat()
    text = json.dumps([str(path.resolve()), stat.st_size, int(stat.st_mtime), *extra])
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _cache_key(path: Path, options: MatchOptions, step: float, clock: bool = False) -> str:
    # clock-timed bins (superframing) differ from frame-number bins
    return _file_key(path, options.roi, options.preset, round(step, 9), 2, *(["clock"] if clock else []))


def _superframing(im: Any) -> bool:
    info = getattr(im, "source_info", None)
    presets = [p for p in (getattr(info, "preset_info", ()) or ())
               if getattr(p, "available", False) and int(getattr(p, "num_frames", 0) or 0) > 0]
    return len(presets) >= 2


def _cached_layout(im: Any, path: Path, cache_dir: Path | None, repair, progress, abort,
                   stage: Callable[[str], None] | None = None) -> PresetLayout:
    """``preset_frames``, kept in ``cache_dir`` (the scan reads every frame header)."""
    if not _superframing(im):
        return PresetLayout()
    store = Path(cache_dir) / f"{_file_key(path, 'presets', 1)}_presets.npz" if cache_dir is not None else None
    if store is not None and store.exists():
        try:
            with np.load(store) as data:
                presets = [int(p) for p in data["presets"]]
                return PresetLayout(frames={p: data[f"frames_{p}"] for p in presets},
                                    ranges={p: tuple(data[f"range_{p}"]) for p in presets if f"range_{p}" in data},
                                    seconds=data["seconds"], breaks=int(data["breaks"]))
        except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
            pass  # unreadable (e.g. a run stopped while writing it): scan again
    if stage is not None:
        stage("Reading the frame headers (superframing presets)")
    layout = preset_frames(im, repair=repair, progress=progress, abort=abort)
    if store is not None and layout:
        arrays = {f"frames_{p}": f for p, f in layout.frames.items()}
        arrays.update({f"range_{p}": np.asarray(r) for p, r in layout.ranges.items()})
        partial = store.with_name(store.stem + ".partial.npz")
        np.savez(partial, presets=np.array(sorted(layout.frames)), seconds=layout.seconds, breaks=layout.breaks,
                 **arrays)
        os.replace(partial, store)  # complete files only
    return layout


def sample_recording(path: str | Path, options: MatchOptions, *, step_s: float = 1.0, im: Any = None,
                     progress: Progress | None = None, abort: Abort | None = None,
                     cache_dir: str | Path | None = None, stage: Callable[[str], None] | None = None) -> Samples:
    """Read the recording once into per-bin statistics (see the module docstring).

    ``im`` lends an open ImagerFile (its state is restored); otherwise the
    file is opened and closed here. ``cache_dir`` keeps the statistics on disk
    for later runs with the same region, preset and bin width. ``stage`` hears
    which step runs (for a progress label).
    """
    path = Path(path)
    owned = im is None
    if owned:
        im = fnv.file.ImagerFile(str(path))
    abort = abort or (lambda: False)
    stage = stage or (lambda text: None)
    try:
        with preserved_state(im):
            set_unit_safely(im, fnv.Unit.COUNTS)
            n, height, width = int(im.num_frames), int(im.height), int(im.width)
            x0, y0, x1, y1 = options.roi or (0, 0, width, height)
            x0, x1 = max(0, int(x0)), min(width, int(x1))
            y0, y1 = max(0, int(y0)), min(height, int(y1))
            if x1 <= x0 or y1 <= y0:
                raise ValueError("The region does not cover any pixel of the image")
            repair = TimestampRepair(path)
            rate = recording_rate(im, repair)
            fps = rate.fps
            if cache_dir is not None:
                cache_dir = Path(cache_dir)
                cache_dir.mkdir(parents=True, exist_ok=True)
            layout = _cached_layout(im, path, cache_dir, repair, progress, abort, stage)
            info: dict[str, Any] = {
                "camera": str(getattr(im.source_info, "camera_model", "") or "").strip(),
                "frames": n, "fps": fps, "clock_fps": rate.clock_fps, "rate_corrected": rate.corrected,
                "region": [x0, y0, x1, y1], "presets": {str(k): v for k, v in layout.ranges.items()},
            }
            preset, limits = choose_preset(layout, options.preset)
            if preset is not None:
                frames = np.asarray(layout.frames[preset], dtype=np.int64)
                info["preset"] = preset
                if layout.breaks:
                    info["preset_breaks"] = layout.breaks
            else:
                frames = np.arange(n)
            # recording seconds of each frame: frame number / rate, or the clock on superframing files
            frame_times = layout.seconds[frames] if layout.seconds is not None else frames / fps
            interval = float(np.median(np.diff(frame_times))) if frames.size > 1 else 1.0 / fps
            step = float(step_s) if interval <= 1.01 * float(step_s) else interval
            info["step"] = step
            key = _cache_key(path, options, step, clock=layout.seconds is not None) if cache_dir is not None else None
            cached = _load_cache(cache_dir, key) if key else None
            if cached is not None:
                stage("Using the frame statistics saved by an earlier run")
                times, mean, spread, counts, first_frame = cached
            else:
                stage(f"Reading {frames.size:,} frames")
                times, mean, spread, counts, first_frame = _accumulate(
                    im, frames, frame_times, step, (x0, y0, x1, y1), progress, abort, cache_dir, key)
            if abort():
                raise JobCancelled("Cancelled")
            stage("Checking the temperature calibration")
            indices = None
            if layout:  # calibrate on this preset's frames: the hottest bins and a spread
                peak = np.array([np.nanmax(mean[k]) if counts[k] else -np.inf for k in range(mean.shape[0])])
                hot = [int(first_frame[k]) for k in np.argsort(peak)[::-1][:4] if first_frame[k] >= 0]
                spread_frames = frames[np.unique(np.linspace(0, frames.size - 1, 4).astype(int))]
                indices = sorted(set(hot) | {int(f) for f in spread_frames})
            report = derive_calibration(im, path, abort=abort, indices=indices, limits=limits)
        params = (MeasurementParameters.from_sdk(report.file_parameters) if report.file_parameters
                  else MeasurementParameters())
        info["calibration"] = report.message
        return Samples(path=path, times=times, mean=mean, spread=spread, counts=counts, first_frame=first_frame,
                       origin=(x0, y0), size=(width, height), fps=fps, step=step, report=report, params=params,
                       info=info, frame_seconds=layout.seconds)
    finally:
        if owned:
            try:
                im.close()
            except Exception:
                pass


def _accumulate(im, frames, frame_times, step, region, progress, abort, cache_dir, key):
    """Per-bin statistics of ``frames`` (recording seconds ``frame_times``, ascending) in ``region``."""
    x0, y0, x1, y1 = region
    h, w = y1 - y0, x1 - x0
    frame_times = np.asarray(frame_times, dtype=np.float64)
    bins_of = np.floor(frame_times / step + 0.5).astype(np.int64)
    nbins = int(bins_of[-1]) + 1 if frames.size else 0
    shape = (nbins, h, w)
    nbytes = 2 * 4 * nbins * h * w
    if cache_dir is None and nbytes > MEMORY_LIMIT:
        raise ValueError(f"The statistics would take {nbytes / 1e9:.1f} GB of memory; draw a smaller region "
                         "around the battery or give a cache folder")
    if cache_dir is not None:
        (Path(cache_dir) / f"{key}_meta.npz").unlink(missing_ok=True)  # complete only when rewritten
        mean = np.lib.format.open_memmap(Path(cache_dir) / f"{key}_mean.npy", mode="w+", dtype=np.float32,
                                         shape=shape)
        spread = np.lib.format.open_memmap(Path(cache_dir) / f"{key}_spread.npy", mode="w+", dtype=np.float32,
                                           shape=shape)
        mean[:] = np.nan
        spread[:] = np.nan
    else:
        mean = np.full(shape, np.nan, dtype=np.float32)
        spread = np.full(shape, np.nan, dtype=np.float32)
    counts = np.zeros(nbins, dtype=np.int32)
    first_frame = np.full(nbins, -1, dtype=np.int64)
    s1 = np.zeros((h, w))
    s2 = np.zeros((h, w))
    stx = np.zeros((h, w))
    acc = {"n": 0, "st": 0.0, "stt": 0.0, "bin": -1}

    def finish() -> None:
        k, m = acc["bin"], acc["n"]
        if k < 0 or m == 0:
            return
        mu = s1 / m
        mean[k] = mu
        counts[k] = m
        if m >= 3:
            sxx = s2 - s1 * mu
            stt = acc["stt"] - acc["st"] ** 2 / m
            if stt > 0:
                sxt = stx - acc["st"] * mu
                resid = sxx - sxt * sxt / stt
            else:
                resid = sxx
            spread[k] = np.sqrt(np.maximum(resid, 0.0) / m)

    total = int(frames.size)
    for done, (frame, at, k) in enumerate(zip(frames, frame_times, bins_of)):
        if done % 256 == 0:
            if abort():
                raise JobCancelled("Cancelled")
            if progress is not None:
                progress(done, total)
        if k != acc["bin"]:
            finish()
            acc.update(n=0, st=0.0, stt=0.0, bin=int(k))
            s1.fill(0)
            s2.fill(0)
            stx.fill(0)
            first_frame[k] = int(frame)
        x = read_array(im, int(frame))[y0:y1, x0:x1].astype(np.float64)
        tau = at - k * step
        s1 += x
        s2 += x * x
        stx += tau * x
        acc["n"] += 1
        acc["st"] += tau
        acc["stt"] += tau * tau
    finish()
    if progress is not None:
        progress(total, total)
    times = np.arange(nbins) * step
    if cache_dir is not None:
        mean.flush()
        spread.flush()
        np.savez(Path(cache_dir) / f"{key}_meta.npz", times=times, counts=counts, first_frame=first_frame,
                 complete=True)
    return times, mean, spread, counts, first_frame


def _load_cache(cache_dir: Path, key: str):
    meta = cache_dir / f"{key}_meta.npz"
    if not meta.exists():
        return None
    try:
        with np.load(meta) as data:
            if not bool(data["complete"]):
                return None
            times, counts, first_frame = data["times"], data["counts"], data["first_frame"]
        mean = np.load(cache_dir / f"{key}_mean.npy", mmap_mode="r")
        spread = np.load(cache_dir / f"{key}_spread.npy", mmap_mode="r")
    except (OSError, ValueError, KeyError):
        return None
    if mean.shape[0] != times.size or spread.shape != mean.shape:
        return None
    return times, mean, spread, counts, first_frame


# --- the TC side ---------------------------------------------------------------------------


@dataclass
class TcChannel:
    """One TC prepared for matching: values on the logger bin grid and why rows are unusable."""

    name: str
    index: int
    values: np.ndarray  # °C per logger bin (NaN off the data)
    usable: np.ndarray  # bool per logger bin: fit the emissivity here (validity windows applied)
    locate_usable: np.ndarray  # bool per logger bin: use for the time and pixel search
    flags: ChannelFlags
    windows: tuple[tuple[float, float], ...]
    calm: np.ndarray | None = None  # bool per logger bin: no flame on the TC (``flame_free``); None = all

    @property
    def searchable(self) -> bool:
        """Enough varying data to correlate: a flat stretch matches anything."""
        x = self.values[self.locate_usable]
        return x.size >= MIN_TC_SAMPLES and float(np.ptp(np.percentile(x, [5, 95]))) >= MIN_TC_RANGE_K


def prepare_channels(table: TcTable, options: MatchOptions, step: float) -> tuple[np.ndarray, list[TcChannel]]:
    """(logger bin times, channels) for the chosen channels.

    Readings outside a channel's validity windows are not used at all. With
    ``search_outside_windows`` the offset and pixel search still uses them
    (inside the scope, where the TC-only failure flags keep them): a TC that
    only reads low, e.g. a lifted tape, still follows the events in time. A
    failure that changes the shape (a sudden drop) would mislead the search,
    hence the default.
    """
    first = math.ceil(table.times[0] / step - 1e-9)
    last = math.floor(table.times[-1] / step + 1e-9)
    grid = np.arange(first, last + 1) * step
    keys = options.channels or tuple(str(k + 1) for k in range(len(table.names)))
    channels = []
    for key in keys:
        index = table.index(key)
        name = table.names[index]
        flags = failure_flags(table, index)
        rows_ok = ~flags.flagged
        windows = _windows_for(options.valid, table, index)
        values = resample(table, index, grid, usable=rows_ok)
        base = np.isfinite(values)
        if options.scope is not None:
            base &= window_mask(grid, [options.scope])
        usable = base & window_mask(grid, windows)
        # jumps are found on the logger's own rows, and only among readings declared valid
        rows = rows_ok & window_mask(table.times, windows)
        if options.scope is not None:
            rows &= window_mask(table.times, [options.scope])
        calm = flame_free(table.times, np.where(rows, table.values[:, index], np.nan), grid, options.jump_k,
                          options.jump_window_s, options.jump_hold_s)
        channels.append(TcChannel(name=name, index=index, values=values, usable=usable,
                                  locate_usable=base if options.search_outside_windows else usable,
                                  flags=flags, windows=windows, calm=calm))
    return grid, channels


def flame_free(times: np.ndarray, values: np.ndarray, grid: np.ndarray, jump_k: float, window_s: float,
               hold_s: float) -> np.ndarray:
    """Per ``grid`` time: False from ``window_s`` before a TC jump until ``hold_s`` after it (TC data only).

    A flame or hot gas jet on the junction makes a taped TC jump faster than
    the surface under it can heat (0903: +500 K in seconds), and the TC then
    reads neither the gas nor the surface while both cool; such stretches are
    not fitted. A jump is a reading more than ``jump_k`` above any earlier
    reading within ``window_s`` on the logger's own rows (``times``; missing
    readings are skipped, so a spike that rises and falls inside the window,
    or one whose peak was dropped, counts). A logger slower than the window
    cannot show a jump, so none is found then.
    Runaway heating of the cell itself is slower (0922: a few K/s), so it stays.
    A hit whose effect outlasts ``hold_s`` (0903's 700-980 °C jets decay over
    about 100 s) leaves its tail in the fit; the median over a TC's events
    keeps such an event from setting the TC's value.
    """
    grid = np.asarray(grid, dtype=np.float64)
    calm = np.ones(grid.size, dtype=bool)
    values = np.asarray(values, dtype=np.float64)
    ok = np.isfinite(values) & np.isfinite(np.asarray(times, dtype=np.float64))
    t, x = np.asarray(times, dtype=np.float64)[ok], values[ok]
    if hold_s <= 0 or t.size < 2:
        return calm
    first = np.searchsorted(t, t - window_s - 1e-9, side="left")  # earliest row within the window
    for i in range(1, t.size):
        if first[i] < i and x[i] - x[first[i]:i].min() > jump_k:
            calm &= ~((grid >= t[i] - window_s - 1e-9) & (grid <= t[i] + hold_s + 1e-9))
    return calm


def _windows_for(valid: Mapping[str, Sequence[tuple[float, float]]], table: TcTable, index: int):
    for key, windows in valid.items():
        try:
            if table.index(key) == index:
                return tuple(windows)
        except KeyError:
            continue
    return ()


# --- locating ------------------------------------------------------------------------------


@dataclass
class ChannelMatch:
    name: str
    found: bool
    r: float  # best correlation at the shared offset (full resolution)
    pixel: tuple[int, int] | None  # (row, col) in the image
    candidates: list[tuple[int, int]]  # pixels within delta_r of the best, near it
    own_lag_s: float | None  # this TC's own best offset (coarse search)
    own_r: float  # coarse best correlation
    runner_up_lag_s: float | None
    runner_up_r: float
    fixed: bool = False  # pixel given by the user
    r_map: np.ndarray | None = None  # full-resolution correlation over the region


@dataclass
class Location:
    lag_s: float
    lag_fixed: bool
    channels: list[ChannelMatch]
    curve_lags_s: np.ndarray  # coarse search: offsets
    curve_r: np.ndarray  # coarse search: best r per offset and channel
    binning: int
    score: float = math.nan  # shared-offset score (mean best r of the found TCs)
    runner_up_lag_s: float | None = None  # best shared offset more than a minute away
    runner_up_score: float = math.nan


def _next_fft(n: int) -> int:
    return 1 << max(1, int(n - 1).bit_length())


def lag_scan(X: np.ndarray, present: np.ndarray, Y: np.ndarray, usable: np.ndarray, lags: np.ndarray, *,
             min_samples=30, chunk: int = 1024, abort: Abort | None = None,
             progress: Progress | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Best Pearson r over pixels for every offset and channel, and that pixel.

    ``X`` (IR bins × pixels) and ``present`` (bool per IR bin); ``Y`` (logger
    bins × channels) and ``usable``; ``lags[i]`` is the IR bin of logger bin 0.
    Each r uses only the samples present on both sides (FFT cross-correlation
    of the masked sums), so partial overlaps and TC gaps are handled; offsets
    with fewer than ``min_samples`` (per channel, or one number) are skipped.
    ``X`` must be finite; its rows where ``present`` is False are ignored.
    """
    n_ir, P = X.shape
    n_tc, C = Y.shape
    nfft = _next_fft(n_ir + n_tc)
    rfft, irfft = np.fft.rfft, np.fft.irfft
    a = present.astype(np.float64)
    m = usable.astype(np.float64)
    need = np.broadcast_to(np.asarray(min_samples, dtype=np.float64), (C,))
    # centre each channel (Pearson is shift-invariant; this keeps the sums small)
    centre = np.array([np.nanmean(Y[usable[:, c], c]) if usable[:, c].any() else 0.0 for c in range(C)])
    ym = np.where(usable, Y - centre, 0.0)
    idx = np.where(lags >= 0, lags, nfft + lags)
    FA = rfft(a, nfft)[:, None]
    Fm, Fy, Fyy = rfft(m, nfft, axis=0), rfft(ym, nfft, axis=0), rfft(ym * ym, nfft, axis=0)

    def xc(f, g):  # c[L] = sum_j f[L + j] g[j]
        return irfft(f * np.conj(g), nfft, axis=0)[idx]

    N = np.rint(xc(FA, Fm))
    Sy, Syy = xc(FA, Fy), xc(FA, Fyy)
    with np.errstate(divide="ignore", invalid="ignore"):
        vy = Syy - Sy * Sy / N
    best = np.full((lags.size, C), -np.inf)
    where = np.zeros((lags.size, C), dtype=np.int64)
    offsets = X[present].mean(axis=0) if present.any() else np.zeros(P)
    for p0 in range(0, P, chunk):
        if abort is not None and abort():
            raise JobCancelled("Cancelled")
        block = np.where(present[:, None], X[:, p0:p0 + chunk] - offsets[p0:p0 + chunk], 0.0)
        F1, F2 = rfft(block, nfft, axis=0), rfft(block * block, nfft, axis=0)
        for c in range(C):
            Sxy = xc(F1, Fy[:, c:c + 1])
            Sx = xc(F1, Fm[:, c:c + 1])
            Sxx = xc(F2, Fm[:, c:c + 1])
            n = N[:, c:c + 1]
            with np.errstate(divide="ignore", invalid="ignore"):
                vx = Sxx - Sx * Sx / n
                r = (Sxy - Sx * Sy[:, c:c + 1] / n) / np.sqrt(vx * vy[:, c:c + 1])
            bad = (n < need[c]) | ~(vx > 1e-6 * np.maximum(n, 1)) | ~(vy[:, c:c + 1] > 0) | ~np.isfinite(r)
            r = np.where(bad, -np.inf, r)
            k = np.argmax(r, axis=1)
            value = r[np.arange(lags.size), k]
            better = value > best[:, c]
            best[better, c] = value[better]
            where[better, c] = p0 + k[better]
        if progress is not None:
            progress(min(P, p0 + chunk), P)
    return np.where(np.isfinite(best), best, np.nan), where


def _binned_counts(mean: np.ndarray, b: int, chunk: int = 128) -> np.ndarray:
    """(bins, h, w) → (bins, (h // b) · (w // b)) float64 block means, read ``chunk`` bins at a time.

    Only the binned result is held in memory (at most ``coarse_pixels`` per bin), never a float64 copy
    of the whole (possibly memmapped) statistics.
    """
    n, h, w = mean.shape
    hb, wb = h // b, w // b
    out = np.empty((n, hb * wb))
    for t0 in range(0, n, chunk):
        block = np.asarray(mean[t0:t0 + chunk, :hb * b, :wb * b], dtype=np.float64)
        out[t0:t0 + block.shape[0]] = block.reshape(block.shape[0], hb, b, wb, b).mean(axis=(2, 4)).reshape(
            block.shape[0], -1)
    return out


def tc_signal(calibration: Calibration, celsius) -> np.ndarray:
    """Blackbody signal (counts) of TC temperatures; NaN below absolute zero."""
    kelvin = np.asarray(celsius, dtype=np.float64) + KELVIN
    with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
        return np.where(kelvin > 0, calibration.planck.signal(np.where(kelvin > 0, kelvin, 1.0)), np.nan)


def _r_map(samples: Samples, lag_bins: int, grid_bins: np.ndarray, values: np.ndarray, usable: np.ndarray,
           rows_per_chunk: int = 16, min_samples: float = 30) -> np.ndarray:
    """Full-resolution Pearson r of every region pixel's counts against one TC's signal at one offset.

    All NaN when fewer than ``min_samples`` (at least 30) TC samples overlap the recording there.
    """
    ir_bins = lag_bins + grid_bins
    ok = usable & (ir_bins >= 0) & (ir_bins < samples.times.size)
    ok[ok] &= samples.counts[ir_bins[ok]] > 0
    nb, h, w = samples.mean.shape
    out = np.full((h, w), np.nan, dtype=np.float32)
    if ok.sum() < max(30.0, float(min_samples)):
        return out
    y = tc_signal(samples.calibration, values[ok])
    y = (y - y.mean()) / (y.std() or 1.0)
    sel = ir_bins[ok]
    for r0 in range(0, h, rows_per_chunk):
        block = np.asarray(samples.mean[sel, r0:r0 + rows_per_chunk, :], dtype=np.float64)
        block = block.reshape(sel.size, -1)
        mu = np.nanmean(block, axis=0)
        sd = np.nanstd(block, axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.nansum((block - mu) * y[:, None], axis=0) / y.size / sd
        r = np.where(sd > 1e-6 * np.maximum(np.abs(mu), 1.0), r, np.nan)
        out[r0:r0 + rows_per_chunk] = r.reshape(-1, w)
    return out


def locate(samples: Samples, grid: np.ndarray, channels: list[TcChannel], options: MatchOptions, *,
           progress: Progress | None = None, abort: Abort | None = None) -> Location:
    """Shared time offset and the best pixel of every TC (see the module docstring)."""
    if samples.calibration is None:
        raise ValueError(f"No verified temperature calibration for {samples.path.name}: "
                         f"{samples.report.message}")
    step = samples.step
    grid_bins = np.rint(grid / step).astype(np.int64)
    nb, h, w = samples.mean.shape
    b = 1
    while (h // b) * (w // b) > options.coarse_pixels:
        b += 1
    present = samples.counts > 0
    X = _binned_counts(samples.mean, b)
    X[~present] = np.nan
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels
        fill = np.nan_to_num(np.nanmean(X, axis=0))  # a pixel's NaN bins add nothing
    missing = np.isnan(X)
    if missing.any():
        X[missing] = np.broadcast_to(fill, X.shape)[missing]
    Y = tc_signal(samples.calibration, np.column_stack([c.values for c in channels]))
    U = np.column_stack([c.locate_usable for c in channels]) & np.isfinite(Y)
    Y = np.where(U, Y, 0.0)
    n_valid = U.sum(axis=0)
    too_few = np.array([not c.searchable for c in channels])
    U = U & ~too_few
    min_samples = np.maximum(30, options.min_overlap * n_valid)
    lo = -(grid_bins[-1]) - 1
    hi = nb - grid_bins[0]
    lags_all = np.arange(lo, hi + 1)  # IR bin of logger bin 0 = lag_bins + grid_bins[0]
    if options.lag_range is not None:
        a_, b_ = options.lag_range
        lags_all = lags_all[(lags_all * step >= a_) & (lags_all * step <= b_)]
    if options.lag_s is not None:
        fixed = int(round(options.lag_s / step))
        lags_all = np.arange(fixed - 2, fixed + 3)
    scan_lags = lags_all + grid_bins[0]
    curve, where = lag_scan(X, present, Y, U, scan_lags, min_samples=min_samples, abort=abort, progress=progress)
    lags_s = lags_all * step
    matches: list[ChannelMatch] = []
    found = []
    for c, ch in enumerate(channels):
        col = curve[:, c]
        if too_few[c] or not np.any(np.isfinite(col)):
            matches.append(ChannelMatch(ch.name, False, math.nan, None, [], None, math.nan, None, math.nan))
            continue
        k = int(np.nanargmax(col))
        far = np.abs(lags_s - lags_s[k]) > 60
        k2 = int(np.nanargmax(np.where(far, col, -np.inf))) if np.any(far & np.isfinite(col)) else None
        own = float(col[k])
        matches.append(ChannelMatch(ch.name, own >= options.min_r, math.nan, None, [], float(lags_s[k]), own,
                                    None if k2 is None else float(lags_s[k2]),
                                    math.nan if k2 is None else float(col[k2])))
        if own >= options.min_r:
            found.append(c)
    shared = math.nan
    runner_lag, runner = None, math.nan
    if options.lag_s is not None:
        lag_bins = int(round(options.lag_s / step))
    elif found:
        # every found TC must take part: an offset where one has no overlap scores low
        score = np.where(np.isfinite(curve[:, found]), curve[:, found], -1.0).mean(axis=1)
        k = int(np.argmax(score))
        lag_bins, shared = int(lags_all[k]), float(score[k])
        far = np.abs(lags_s - lags_s[k]) > 60
        if far.any():
            k2 = int(np.argmax(np.where(far, score, -np.inf)))
            runner_lag, runner = float(lags_s[k2]), float(score[k2])
    else:
        with warnings.catch_warnings():  # offsets without overlap are all-NaN rows
            warnings.simplefilter("ignore", RuntimeWarning)
            best = int(np.nanargmax(np.nanmax(curve, axis=1))) if np.any(np.isfinite(curve)) else 0
        lag_bins = int(lags_all[best])
    if options.lag_s is None and found and b > 1:
        refined_bins, refined = _refine_lag(samples, b, lag_bins, lags_all, where, found, grid_bins, Y, U,
                                            min_samples, options.search_radius)
        if math.isfinite(refined):
            lag_bins, shared = refined_bins, refined
    x0, y0 = samples.origin
    for c, (ch, match) in enumerate(zip(channels, matches)):
        if abort is not None and abort():
            raise JobCancelled("Cancelled")
        if too_few[c] and _fixed_pixel(options.pixels, ch.name, ch.index) is None:
            continue
        rmap = _r_map(samples, lag_bins, grid_bins, ch.values, ch.locate_usable, min_samples=min_samples[c])
        match.r_map = rmap
        fixed_pixel = _fixed_pixel(options.pixels, ch.name, ch.index)
        if fixed_pixel is not None:
            row, col = fixed_pixel
            match.pixel, match.fixed = (int(row), int(col)), True
            inside = 0 <= row - y0 < h and 0 <= col - x0 < w
            match.r = float(rmap[row - y0, col - x0]) if inside else math.nan
            match.candidates = [match.pixel]
            match.found = True
            continue
        if not np.any(np.isfinite(rmap)):
            match.found = False
            continue
        p = int(np.nanargmax(rmap))
        rr, cc = divmod(p, w)
        match.r = float(rmap[rr, cc])
        match.pixel = (rr + y0, cc + x0)
        match.found = match.r >= options.min_r  # at the shared offset, not at the TC's own best one
        rad = options.search_radius
        r_lo, r_hi = max(0, rr - rad), min(h, rr + rad + 1)
        c_lo, c_hi = max(0, cc - rad), min(w, cc + rad + 1)
        window = np.nan_to_num(rmap[r_lo:r_hi, c_lo:c_hi], nan=-2)
        ys, xs = np.nonzero(window >= match.r - options.delta_r)
        order = np.argsort(-window[ys, xs], kind="stable")  # best first: the best pixel leads
        match.candidates = [(int(ys[k] + r_lo + y0), int(xs[k] + c_lo + x0)) for k in order]
    return Location(lag_s=lag_bins * step, lag_fixed=options.lag_s is not None, channels=matches,
                    curve_lags_s=lags_s, curve_r=curve, binning=b, score=shared, runner_up_lag_s=runner_lag,
                    runner_up_score=runner)


def _refine_lag(samples: Samples, b: int, lag_bins: int, lags_all: np.ndarray, where: np.ndarray,
                found: list[int], grid_bins: np.ndarray, Y: np.ndarray, U: np.ndarray, min_samples: np.ndarray,
                radius: int) -> tuple[int, float]:
    """Full-resolution offset near the binned one, from windows around each found TC's best block.

    Averaging b × b pixels mixes a spot with neighbours that heat a little later, which shifts the
    binned offset by a few bins; the full-resolution pixels around the block settle it.
    """
    nb, h, w = samples.mean.shape
    wb = w // b
    k = int(np.flatnonzero(lags_all == lag_bins)[0])
    local = np.arange(lag_bins - REFINE_BINS, lag_bins + REFINE_BINS + 1)
    local = local[np.isin(local, lags_all)]  # never outside the searched (or requested) range
    present = samples.counts > 0
    curves = []
    pad = radius + b
    for c in found:
        row, col = divmod(int(where[k, c]), wb)
        r0, r1 = max(0, row * b - pad), min(h, row * b + b + pad)
        c0, c1 = max(0, col * b - pad), min(w, col * b + b + pad)
        X = np.asarray(samples.mean[:, r0:r1, c0:c1], dtype=np.float64).reshape(nb, -1)
        X[~present] = np.nan
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            fill = np.nan_to_num(np.nanmean(X, axis=0))
        missing = np.isnan(X)
        X[missing] = np.broadcast_to(fill, X.shape)[missing]
        curve, _where = lag_scan(X, present, Y[:, [c]], U[:, [c]], local + grid_bins[0],
                                 min_samples=min_samples[c])
        curves.append(np.where(np.isfinite(curve[:, 0]), curve[:, 0], -1.0))
    score = np.mean(curves, axis=0)
    best = int(np.argmax(score))
    if not score[best] > -1.0:  # no finite correlation anywhere: keep the binned offset
        return lag_bins, math.nan
    return int(local[best]), float(score[best])


def _fixed_pixel(pixels: Mapping[str, tuple[int, int]], name: str, index: int):
    for key, value in pixels.items():
        if key == name or key == str(index + 1) or name.casefold().startswith(str(key).casefold()):
            return value
    return None


# --- spots, events and fits ---------------------------------------------------------------------


@dataclass
class SpotSeries:
    """One spot (N × N block) on the logger grid."""

    counts: np.ndarray  # mean count of the block per logger bin (NaN without IR)
    flicker: np.ndarray  # relative within-bin flicker (NaN when unknown)
    status: np.ndarray  # radiometry.count_status (0 = in range; -1 = no IR)
    apparent: np.ndarray  # °C at ε = 1


def spot_rect(pixel: tuple[int, int], size: int, region: Sequence[int]) -> tuple[int, int, int, int]:
    """(row0, row1, col0, col1), image pixels, of the N × N spot at ``pixel`` clipped to ``region``.

    ``region`` is (x0, y0, x1, y1), end exclusive. The engine averages exactly these pixels and the
    exported box covers exactly these (``roi_shapes``).
    """
    x0, y0, x1, y1 = (int(v) for v in region)
    half = (size - 1) // 2
    row, col = pixel
    return (max(y0, row - half), min(y1, row - half + size), max(x0, col - half), min(x1, col - half + size))


def block_bounds(pixel: tuple[int, int], size: int, samples: Samples) -> tuple[int, int, int, int]:
    """``spot_rect`` in region coordinates (row0, row1, col0, col1)."""
    x0, y0 = samples.origin
    _nb, h, w = samples.mean.shape
    r0, r1, c0, c1 = spot_rect(pixel, size, (x0, y0, x0 + w, y0 + h))
    return r0 - y0, max(r0, r1) - y0, c0 - x0, max(c0, c1) - x0


def spot_series(samples: Samples, pixel: tuple[int, int], size: int, lag_bins: int,
                grid_bins: np.ndarray) -> SpotSeries:
    r0, r1, c0, c1 = block_bounds(pixel, size, samples)
    ir = lag_bins + grid_bins
    inside = (ir >= 0) & (ir < samples.times.size)
    n = grid_bins.size
    counts = np.full(n, np.nan)
    low = np.full(n, np.nan)
    high = np.full(n, np.nan)
    flick_k = np.full(n, np.nan)
    sel = ir[inside]
    cells = (r1 - r0) * (c1 - c0)  # explicit: no recording bin may overlap the logger (a forced offset)
    block = np.asarray(samples.mean[sel, r0:r1, c0:c1], dtype=np.float64).reshape(sel.size, cells)
    sd = np.asarray(samples.spread[sel, r0:r1, c0:c1], dtype=np.float64).reshape(sel.size, cells)
    with np.errstate(invalid="ignore"):
        counts[inside] = block.mean(axis=1)
        low[inside] = block.min(axis=1)
        high[inside] = block.max(axis=1)
        t_mean = samples.apparent(block)
        flick_k[inside] = np.mean(samples.apparent(block + sd) - t_mean, axis=1)
    apparent = samples.apparent(counts)
    finite = apparent[np.isfinite(apparent)]
    ambient = float(np.percentile(finite, 5)) if finite.size else 20.0
    with np.errstate(invalid="ignore", divide="ignore"):
        flicker = flick_k / np.maximum(apparent - ambient, 5.0)
    present = np.isfinite(counts)
    status = np.full(n, -1, dtype=np.int8)
    if present.any():
        status[present] = count_status(samples.calibration, low[present], high[present])
    return SpotSeries(counts=counts, flicker=flicker, status=status, apparent=apparent)


@dataclass
class Event:
    channel: str
    start_s: float
    end_s: float
    rows: np.ndarray  # logger-bin indices inside the event that pass the rules and can be fitted
    excluded_bound: np.ndarray  # rows that pass the rules but no ε in (0, 1] can explain (not fitted)


def _runs(mask: np.ndarray, gap: int) -> list[tuple[int, int]]:
    """Index runs of True, bridging gaps of up to ``gap`` False samples."""
    idx = np.flatnonzero(mask)
    if not idx.size:
        return []
    runs = []
    start = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev - 1 > gap:
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    return runs


def event_rules(channel: TcChannel, spot: SpotSeries, options: MatchOptions, flicker_known: bool) -> dict[str, np.ndarray]:
    """Per logger bin, each rule an event sample must pass (none compares IR with TC)."""
    with np.errstate(invalid="ignore"):
        rules = {
            "tc usable": channel.usable,
            f"tc ≥ {options.tc_min_c:g} °C": channel.values >= options.tc_min_c,
            "no flame on the tc": channel.calm if channel.calm is not None else np.ones(channel.values.size, bool),
            "ir present": spot.status >= 0,
        }
        if options.below_range:
            rules["ir in calibrated range or below"] = (spot.status == 0) | (spot.status == 1)
        else:
            rules["ir in calibrated range"] = spot.status == 0
        if flicker_known:
            rules["no flames (flicker)"] = spot.flicker < options.flicker_max
    return rules


def no_emissivity_fits(apparent_c, tc_c, reflected_c: float, margin_k: float) -> np.ndarray:
    """True where no ε in (0, 1] can explain the IR.

    The camera sees ε·S(TC) + (1 − ε)·S(T_refl), which lies between the two signals, so the
    temperature at ε = 1 must lie between the TC and the reflected temperature (± ``margin_k``).
    For a TC hotter than its surroundings this is "IR above the TC", the engineers' rule.
    """
    apparent_c = np.asarray(apparent_c, dtype=np.float64)
    tc_c = np.asarray(tc_c, dtype=np.float64)
    with np.errstate(invalid="ignore"):
        low = np.minimum(tc_c, reflected_c) - margin_k
        high = np.maximum(tc_c, reflected_c) + margin_k
        return (apparent_c > high) | (apparent_c < low)


def select_events(grid: np.ndarray, channel: TcChannel, spot: SpotSeries, options: MatchOptions, step: float,
                  flicker_known: bool, reflected_c: float = 20.0) -> list[Event]:
    rules = event_rules(channel, spot, options, flicker_known)
    ok = np.logical_and.reduce(list(rules.values()))
    bound = no_emissivity_fits(spot.apparent, channel.values, reflected_c, options.bound_margin_k)
    events = []
    for a, b in _runs(ok, int(round(options.gap_s / step))):
        if (b - a + 1) * step < options.min_event_s:
            continue
        rows = np.arange(a, b + 1)
        rows = rows[ok[rows]]
        events.append(Event(channel.name, float(grid[a]), float(grid[b]), rows=rows[~bound[rows]],
                            excluded_bound=rows[bound[rows]]))
    return events


@dataclass
class EpsFit:
    n: int
    eps_ls: float  # unconstrained least squares in the signal domain (can exceed 1)
    eps: float  # best ε ≤ 1 by temperature RMSE
    rmse_k: float
    bias_k: float  # mean IR − TC at ``eps``
    matching_median: float
    matching_p10: float
    matching_p90: float
    at_bound: bool  # the best ε ≤ 1 is 1: the data want at least that much


def fit_eps(calibration: Calibration, params: MeasurementParameters, counts: np.ndarray, tc_c: np.ndarray,
            grid: np.ndarray | None = None) -> EpsFit:
    """Emissivity that makes the IR at ``counts`` best match ``tc_c`` (°C)."""
    counts = np.asarray(counts, dtype=np.float64)
    tc_c = np.asarray(tc_c, dtype=np.float64)
    n = int(counts.size)
    if n == 0:
        return EpsFit(0, *(math.nan,) * 7, False)
    S = calibration.planck.signal
    s_r = float(S(params.reflected_k))
    s1 = compensated_signal(calibration, params, counts)
    x = S(tc_c + KELVIN) - s_r
    y = s1 - s_r
    eps_ls = float(np.sum(x * y) / np.sum(x * x)) if np.sum(x * x) > 0 else math.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        matching = y / x
    grid = EPS_GRID if grid is None else np.asarray(grid, dtype=np.float64)
    # K1(ε) = K1(1)/ε and K2(ε) = (K2(1) + (1 − ε)·S(T_refl))/ε (radiometry.coefficients)
    k1_one, k2_one = coefficients(calibration, params.with_(emissivity=1.0))
    k1 = k1_one / grid
    k2 = (k2_one + (1 - grid) * s_r) / grid
    total = np.zeros(grid.size)
    valid = np.zeros(grid.size)
    for s0 in range(0, n, FIT_BLOCK):
        c, ref = counts[s0:s0 + FIT_BLOCK], tc_c[s0:s0 + FIT_BLOCK]
        t = calibration.planck.temperature(k1[:, None] * c[None, :] - k2[:, None]) - KELVIN
        sq = (t - ref[None, :]) ** 2
        good = np.isfinite(sq)
        total += np.where(good, sq, 0.0).sum(axis=1)
        valid += good.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        rmse = np.where(valid > 0, np.sqrt(total / np.maximum(valid, 1)), math.inf)
    k = int(np.argmin(rmse))
    eps = float(grid[k])
    k1, k2 = coefficients(calibration, params.with_(emissivity=eps))
    t_best = calibration.planck.temperature(k1 * counts - k2) - KELVIN
    finite = matching[np.isfinite(matching)]
    pct = np.percentile(finite, [50, 10, 90]) if finite.size else (math.nan,) * 3
    return EpsFit(n=n, eps_ls=eps_ls, eps=eps, rmse_k=float(rmse[k]), bias_k=float(np.nanmean(t_best - tc_c)),
                  matching_median=float(pct[0]), matching_p10=float(pct[1]), matching_p90=float(pct[2]),
                  at_bound=eps >= float(grid[-1]))


@dataclass
class EventResult:
    channel: str
    start_s: float
    end_s: float
    samples: int  # bins passing the rules
    excluded_bound: int  # of those, no ε ≤ 1 could match
    tc_min_c: float
    tc_max_c: float
    fit: EpsFit
    pixel_eps: tuple[float, float, float]  # min, median, max of ε (≤ 1) over the candidate pixels
    pixel_eps_ls: tuple[float, float, float]
    pixel_n: int = 0  # candidate pixels fitted (the best-correlated, up to MAX_CANDIDATES)
    physical: bool = True  # False: the data want ε > PHYSICAL_EPS_MAX, or mostly no ε matches; not pooled
    used: bool = True  # pooled: physical, and its TC's events are mostly physical

    @property
    def label(self) -> str:
        return f"{self.channel} {self.start_s:.0f}-{self.end_s:.0f} s"


@dataclass
class ChannelResult:
    name: str
    match: ChannelMatch
    flags: list[str]
    events: list[EventResult]
    rules: dict[str, int]  # logger bins passing each event rule
    usable_bins: int
    trusted: bool = True  # False: most of its event seconds are not physical, so none of its events is used
    eps: tuple[float, float, float] | None = None  # min, median, max of its used events' ε (the median is its value)
    eps_events: int = 0


@dataclass
class MatchResult:
    recording: str
    tc_source: str
    camera: str
    fps: float
    step: float
    lag_s: float
    lag_fixed: bool
    lag_score: float  # mean best correlation of the found TCs at the offset
    runner_up_lag_s: float | None
    ignition_frame: int  # recording frame at logger time 0 (may lie outside the recording)
    num_frames: int
    region: list[int]
    preset: int | None
    channels: list[ChannelResult]
    pooled: EpsFit | None
    cross: list[dict]  # ε of one physical event applied to another
    spread: float | None  # max − min ε over the physical events
    consistent: bool | None  # span within ``consistent_eps``
    overlapping: bool | None  # the events' candidate-pixel ranges share a value
    tc_spread: float | None  # max − min of the TCs' own values (TCs with a used event); None under two TCs
    tcs_agree: bool | None  # tc_spread within ``consistent_eps``: one emissivity serves all TCs
    common_eps: float | None  # median of the TCs' values when they agree (or the only TC's); else None
    origin_s: float | None  # clock time of the workbook origin frame (superframing); None: frame / fps
    warnings: list[str]
    notes: list[str]
    reflected_c: float
    calibration: str
    spot: int
    options: dict

    def to_dict(self) -> dict:
        def clean(value):
            if isinstance(value, float):
                return None if not math.isfinite(value) else round(value, 6)
            if isinstance(value, np.ndarray):
                return None
            if isinstance(value, dict):
                return {str(k): clean(v) for k, v in value.items() if k != "r_map"}
            if isinstance(value, (list, tuple)):
                return [clean(v) for v in value]
            if isinstance(value, (np.floating, np.integer)):
                return clean(value.item())
            return value
        return clean(asdict(self))


def analyse(samples: Samples, table: TcTable, options: MatchOptions, *, progress: Progress | None = None,
            abort: Abort | None = None) -> tuple[MatchResult, Location, dict]:
    """Locate, select events, fit. Returns the result, the location and the per-channel series."""
    if options.lag_frame is not None:  # a frame is timed as the statistics are (by the clock on superframing)
        options = dataclass_replace(options, lag_s=samples.seconds_at(int(options.lag_frame)), lag_frame=None)
    grid, channels = prepare_channels(table, options, samples.step)
    location = locate(samples, grid, channels, options, progress=progress, abort=abort)
    step = samples.step
    lag_bins = int(round(location.lag_s / step))
    grid_bins = np.rint(grid / step).astype(np.int64)
    reflected_k = (options.reflected_c + KELVIN) if options.reflected_c is not None else samples.params.reflected_k
    params = samples.params.with_(reflected_k=reflected_k)
    cal = samples.calibration
    flicker_known = samples.flicker_available
    warnings: list[str] = []
    notes: list[str] = list(table.notes)
    if not flicker_known:
        warnings.append("The recording has fewer than 3 frames per time bin, so flames in front of a spot "
                        "cannot be detected; events are not filtered for flames.")
    results: list[ChannelResult] = []
    series: dict[str, dict] = {}
    all_events: list[tuple[EventResult, np.ndarray, np.ndarray, TcChannel]] = []
    pending: list[tuple[EventResult, np.ndarray, np.ndarray, TcChannel]] = []
    for ch, match in zip(channels, location.channels):
        _check(abort)
        flags = list(ch.flags.reasons)
        res = ChannelResult(name=ch.name, match=match, flags=flags, events=[], rules={},
                            usable_bins=int(ch.usable.sum()))
        results.append(res)
        if not ch.searchable:
            warnings.append(f"{ch.name}: too little usable, varying TC data ({int(ch.locate_usable.sum())} s); "
                            "not searched for.")
        if not match.found or match.pixel is None:
            continue
        spot = spot_series(samples, match.pixel, options.spot, lag_bins, grid_bins)
        rules = event_rules(ch, spot, options, flicker_known)
        res.rules = {name: int(mask.sum()) for name, mask in rules.items()}
        series[ch.name] = {"spot": spot, "channel": ch}
        neighbours = []
        for pixel in match.candidates[:MAX_CANDIDATES]:
            _check(abort)
            neighbours.append(spot_series(samples, pixel, options.spot, lag_bins, grid_bins))
        for event in select_events(grid, ch, spot, options, step, flicker_known, reflected_k - KELVIN):
            _check(abort)
            rows = event.rows
            every = np.sort(np.concatenate([rows, event.excluded_bound]))
            total = int(every.size)
            share = event.excluded_bound.size / max(1, total)
            fit = fit_eps(cal, params, spot.counts[rows], ch.values[rows]) if rows.size >= 3 else (
                fit_eps(cal, params, np.array([]), np.array([])))
            per_pixel, per_pixel_ls = [], []
            if rows.size >= 3:
                for other in neighbours:
                    _check(abort)
                    ok = np.isfinite(other.counts[rows])
                    if ok.sum() >= 3:
                        f = fit_eps(cal, params, other.counts[rows][ok], ch.values[rows][ok])
                        per_pixel.append(f.eps)
                        per_pixel_ls.append(f.eps_ls)
            # an event whose seconds no emissivity explains still counts against its TC's trust
            physical = rows.size >= 3 and not (fit.eps_ls > PHYSICAL_EPS_MAX or share > 0.5)
            er = EventResult(channel=ch.name, start_s=event.start_s, end_s=event.end_s, samples=total,
                             excluded_bound=int(event.excluded_bound.size),
                             tc_min_c=float(np.nanmin(ch.values[every])), tc_max_c=float(np.nanmax(ch.values[every])),
                             fit=fit, pixel_eps=_spread3(per_pixel), pixel_eps_ls=_spread3(per_pixel_ls),
                             pixel_n=len(per_pixel), physical=physical)
            res.events.append(er)
            if er.physical and share > 0.2:
                warnings.append(f"{er.label}: in {share:.0%} of the seconds no emissivity from 0 to 1 explains "
                                "the IR; those seconds were left out.")
            pending.append((er, spot.counts[rows], ch.values[rows], ch))
        rejected = [e for e in res.events if not e.physical]
        physical_s = sum(e.samples for e in res.events if e.physical)
        res.trusted = physical_s >= 0.5 * sum(e.samples for e in res.events)
        if rejected:
            spans = ", ".join(f"{e.start_s:.0f}-{e.end_s:.0f} s" for e in rejected)
            tail = ("These events are not used for the emissivity." if res.trusted else
                    "Most of its event time is like this, so none of its events is used: a spot that does "
                    "not see the TC point cannot be trusted where it happens to match.")
            warnings.append(f"{ch.name}: {len(rejected)} of {len(res.events)} event(s) ({spans}) need an "
                            "emissivity above 1, which no surface has, or no emissivity from 0 to 1 explains the "
                            "IR in most of their seconds. The spot may not see the TC point, or the TC reads low "
                            f"(contact, tape, failure). {tail}")
        for er, counts_, values_, ch_ in pending:
            er.used = er.physical and res.trusted
            if er.used:
                all_events.append((er, counts_, values_, ch_))
        pending.clear()
        own = [e.fit.eps for e in res.events if e.used and math.isfinite(e.fit.eps)]
        if own:  # the median: one event spoiled by a flame or a contact change does not move it
            res.eps, res.eps_events = _spread3(own), len(own)
        if match.found and not res.events:
            warnings.append(f"{ch.name}: no stretch passes the event rules ({_rule_note(res.rules, ch)}).")
    pooled = None
    cross: list[dict] = []
    spread = consistent = overlapping = None
    _check(abort)
    if all_events:
        pooled = fit_eps(cal, params, np.concatenate([c for _e, c, _t, _ch in all_events]),
                         np.concatenate([t for _e, _c, t, _ch in all_events]))
        _check(abort)
        epsilons = [e.fit.eps for e, *_ in all_events]
        if len(all_events) > 1:
            spread = float(max(epsilons) - min(epsilons))
            consistent = spread <= options.consistent_eps
            lows = [e.pixel_eps[0] if math.isfinite(e.pixel_eps[0]) else e.fit.eps for e, *_ in all_events]
            highs = [e.pixel_eps[2] if math.isfinite(e.pixel_eps[2]) else e.fit.eps for e, *_ in all_events]
            overlapping = max(lows) <= min(highs)
            for i, (ei, *_r) in enumerate(all_events):
                for j, (ej, cj, tj, _chj) in enumerate(all_events):
                    _check(abort)
                    if i == j:
                        continue
                    k1, k2 = coefficients(cal, params.with_(emissivity=ei.fit.eps))
                    t = cal.planck.temperature(k1 * cj - k2) - KELVIN
                    cross.append({"fit_on": ei.label, "applied_to": ej.label, "eps": ei.fit.eps,
                                  "rmse_k": float(np.sqrt(np.nanmean((t - tj) ** 2))),
                                  "own_rmse_k": ej.fit.rmse_k})
        else:
            warnings.append("Only one usable event, so the emissivity cannot be checked against another event "
                            "(more data, or another TC, is needed to validate it).")
    for res in results:
        m = res.match
        if m.found and m.own_lag_s is not None and not location.lag_fixed and abs(m.own_lag_s - location.lag_s) > 5:
            warnings.append(f"{res.name}: its own best time offset ({m.own_lag_s:.0f} s) differs from the shared "
                            f"one ({location.lag_s:.0f} s); check the TC or its pixel.")
        if m.pixel is not None and not m.fixed:
            row, col = m.pixel
            width, height = samples.size
            if min(row, height - 1 - row, col, width - 1 - col) < 3:
                warnings.append(f"{res.name}: the pixel ({row}, {col}) is at the image edge; the TC may lie "
                                "outside the view.")
    if (location.runner_up_lag_s is not None and math.isfinite(location.runner_up_score)
            and location.score - location.runner_up_score < AMBIGUOUS_SCORE):
        warnings.append(f"Another time offset ({location.runner_up_lag_s:.0f} s) fits almost as well as "
                        f"{location.lag_s:.0f} s (score {location.runner_up_score:.3f} vs {location.score:.3f}); "
                        "check the time match, or give it (e.g. from a sync event).")
    found_now = [m for m in location.channels if m.found and not m.fixed]
    if len(found_now) == 1 and not location.lag_fixed:
        notes.append("Only one TC was found, so the time offset rests on that TC alone.")
    medians = [res.eps[1] for res in results if res.eps is not None]
    tc_spread = float(max(medians) - min(medians)) if len(medians) > 1 else None
    tcs_agree = None if tc_spread is None else tc_spread <= options.consistent_eps
    if samples.frame_seconds is not None:
        breaks = samples.info.get("preset_breaks", 0)
        notes.append("Superframing: the frames of the chosen preset were timed by the camera clock"
                     + (f"; the preset cycle breaks {breaks} time(s) (dropped frames)." if breaks else "."))
    if tcs_agree:
        common = float(np.median(medians))
    elif len(medians) == 1:
        common = medians[0]
    else:
        common = None
    ignition = samples.frame_at(location.lag_s)
    n_frames = int(samples.info.get("frames", samples.first_frame.max(initial=0) + 1))
    origin_frame = min(max(ignition, 0), max(0, n_frames - 1))
    clock = samples.frame_seconds
    origin_s = float(clock[origin_frame]) if clock is not None and origin_frame < clock.size else None
    x0, y0 = samples.origin
    _nb, h, w = samples.mean.shape
    result = MatchResult(
        recording=samples.path.name, tc_source=table.source, camera=samples.info.get("camera", ""),
        fps=samples.fps, step=step, lag_s=location.lag_s, lag_fixed=location.lag_fixed, lag_score=location.score,
        runner_up_lag_s=location.runner_up_lag_s, ignition_frame=ignition,
        num_frames=int(samples.info.get("frames", samples.first_frame.max(initial=0) + 1)),
        region=[x0, y0, x0 + w, y0 + h], preset=samples.info.get("preset"), channels=results,
        pooled=pooled, cross=cross, spread=spread, consistent=consistent, overlapping=overlapping,
        tc_spread=tc_spread, tcs_agree=tcs_agree, common_eps=common, origin_s=origin_s, warnings=warnings,
        notes=notes,
        reflected_c=reflected_k - KELVIN, calibration=samples.report.message, spot=options.spot,
        options=_options_dict(options))
    _check(abort)
    return result, location, series


def _check(abort: Abort | None) -> None:
    if abort is not None and abort():
        raise JobCancelled("Cancelled")


def _spread3(values: list[float]) -> tuple[float, float, float]:
    finite = [v for v in values if math.isfinite(v)]
    if not finite:
        return (math.nan,) * 3
    return float(min(finite)), float(np.median(finite)), float(max(finite))


def _rule_note(counts: dict[str, int], channel: TcChannel) -> str:
    if not counts:
        return "no data"
    return ", ".join(f"{name}: {n} s" for name, n in counts.items())


def _options_dict(options: MatchOptions) -> dict:
    out = asdict(options)
    out["valid"] = {k: [list(w) for w in v] for k, v in options.valid.items()}
    out["pixels"] = {k: list(v) for k, v in options.pixels.items()}
    return out


# --- reporting ----------------------------------------------------------------------------------


def summary_text(result: MatchResult) -> str:
    """Plain-language summary for the engineers."""
    lines = [f"Recording: {result.recording} ({result.camera}, {result.fps:.2f} fps"
             + (f", preset {result.preset}" if result.preset is not None else "") + ")",
             f"TC file: {result.tc_source}",
             f"Time match: logger time 0 = recording time {result.lag_s:.0f} s (frame {result.ignition_frame + 1})"
             + (" (given)" if result.lag_fixed else ""),
             f"Spot size: {result.spot} × {result.spot} pixels; reflected temperature {result.reflected_c:.0f} °C",
             ""]
    for ch in result.channels:
        m = ch.match
        head = f"{ch.name}: "
        if not m.found or m.pixel is None:
            r = f"best correlation {m.own_r:.2f}" if math.isfinite(m.own_r) else "no usable data"
            lines.append(head + f"not found ({r}).")
        else:
            rows = [p[0] for p in m.candidates] or [m.pixel[0]]
            cols = [p[1] for p in m.candidates] or [m.pixel[1]]
            where = "given" if m.fixed else f"r = {m.r:.3f}"
            lines.append(head + f"pixel row {m.pixel[0]}, column {m.pixel[1]} ({where}); "
                         f"{len(m.candidates)} pixel(s) fit almost as well (rows {min(rows)}-{max(rows)}, "
                         f"columns {min(cols)}-{max(cols)}).")
        for flag in ch.flags:
            lines.append(f"  TC check: {flag}.")
        for e in ch.events:
            f = e.fit
            lo, med, hi = e.pixel_eps
            bound = " (at the limit of 1)" if f.at_bound else ""
            tag = "" if e.used else (" [not physical, not used]" if not e.physical else " [not used]")
            head = (f"  Event {e.start_s:.0f}-{e.end_s:.0f} s{tag} ({e.samples} s, "
                    f"TC {e.tc_min_c:.0f}-{e.tc_max_c:.0f} °C): ")
            if f.n == 0 or not math.isfinite(f.eps):
                lines.append(head + "no fit: no emissivity from 0 to 1 explains the IR in these seconds")
                continue
            lines.append(head + f"emissivity {f.eps:.2f}{bound}, RMSE {f.rmse_k:.0f} K"
                         + (f"; {e.pixel_n} nearby pixels give {lo:.2f}-{hi:.2f}" if math.isfinite(lo) else "")
                         + (f"; {e.excluded_bound} s left out (no emissivity explains the IR)"
                            if e.excluded_bound else ""))
    lines.append("")
    used = [e for ch in result.channels for e in ch.events if e.used]
    valued = [ch for ch in result.channels if ch.eps is not None]
    if valued:
        lines.append("Emissivity per TC (the median of its usable events; the range shows how far they spread):")
        for ch in valued:
            lo, med, hi = ch.eps
            lines.append(f"  {ch.name}: {med:.2f}" + (f" ({ch.eps_events} events, {lo:.2f}-{hi:.2f})"
                                                      if ch.eps_events > 1 else " (1 event)"))
    if result.tcs_agree is False:
        lines.append(f"The TCs differ (their values span {result.tc_spread:.2f}): each describes the surface at its "
                     "own spot, so no common emissivity is given.")
    else:
        if result.tcs_agree:
            lines.append(f"The TCs agree: emissivity {result.common_eps:.2f} (the median of their values).")
        if result.pooled is not None:
            p = result.pooled
            text = f"All usable events fitted together: emissivity {p.eps:.2f}, RMSE {p.rmse_k:.0f} K over {p.n} s."
            if result.common_eps is not None and abs(p.eps - result.common_eps) > result.options.get(
                    "consistent_eps", 0.05):
                text += (f" This is off the median ({result.common_eps:.2f}) because some events disagree; "
                         "the median is the better value.")
            lines.append(text)
    if result.spread is not None and result.tcs_agree is not False:
        if result.consistent:
            verdict = "agree"
        elif result.overlapping:
            verdict = "agree only within the pixel uncertainty"
        else:
            verdict = "do not agree"
        lines.append(f"The {len(used)} usable events {verdict}: their emissivities span {result.spread:.2f}.")
    if not used:
        lines.append("No usable event, so no emissivity was fitted.")
    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines += [f"- {w}" for w in result.warnings]
    if result.notes:
        lines.append("")
        lines.append("Notes on the TC file:")
        lines += [f"- {n}" for n in result.notes]
    return "\n".join(lines) + "\n"


def workbook_origin(result: MatchResult) -> tuple[int, float]:
    """(ignition frame inside the recording, the TC sheet's logger offset in s).

    The workbook's time 0 must be a frame of the recording. When logger time 0 falls outside it
    (the logger started before the camera, or after it stopped), the nearest end frame is time 0
    and the sheet's offset maps the logger onto it: logger time = workbook time − offset.
    """
    frame = min(max(int(result.ignition_frame), 0), max(0, int(result.num_frames) - 1))
    at = result.origin_s if result.origin_s is not None else frame / result.fps  # the clock on superframing
    return frame, float(result.lag_s - at)


def tc_prefill(result: MatchResult, table: TcTable):
    """TC Compare sheet contents for the workbook: the logger data, each ROI's TC and fitted
    emissivity (its override on Settings), the best event window."""
    from .excel_export import TcPrefill

    _frame, offset = workbook_origin(result)
    used = [ch for ch in result.channels if ch.match.found and ch.match.pixel is not None]
    names = tuple(ch.name for ch in result.channels)[:10]
    columns = tuple(tuple(float(v) for v in table.values[:, table.index(name) if name in table.names else k])
                    for k, name in enumerate(names))
    roi_tc, roi_eps = [], []
    for shape in roi_shapes(result):
        for ch in used:
            if shape.name.startswith(ch.name.split(" (")[0] + " ") and ch.name in names:
                roi_tc.append((shape.name, ch.name))
                if ch.eps is not None and math.isfinite(ch.eps[1]) and ch.eps[1] > 0:
                    roi_eps.append((shape.name, round(min(1.0, ch.eps[1]), 3)))
                break
    events = [e for ch in result.channels for e in ch.events if e.used]
    window = None
    if events:
        best = max(events, key=lambda e: e.fit.n)
        window = (best.start_s + offset, best.end_s + offset)  # on the workbook's axis
    return TcPrefill(times=tuple(float(t) for t in table.times), columns=columns, names=names,
                     roi_tc=tuple(roi_tc), window=window, offset_s=offset, roi_eps=tuple(roi_eps))


def write_workbook(dest: str | Path, recording: str | Path, result: MatchResult, table: TcTable, *,
                   progress: Progress | None = None, abort: Abort | None = None, handles: dict | None = None,
                   parameters: dict | None = None, replace=(), tool_version: str = "") -> str:
    """Excel workbook (``excel_export``) with spots/boxes at the TC pixels and the logger data filled in.

    Time 0 is logger time 0 (the ignition frame is the matched frame) and the
    sheet's logger offset is 0, unless logger time 0 lies outside the recording
    (``workbook_origin``). Rows cover the logger's span at its interval. Each
    TC's ROIs start at its fitted emissivity. ``handles``, ``replace`` and
    ``tool_version`` go to ``run_export``; ``parameters`` (SDK values) replace
    the recording's own object parameters, as in the analysis.
    """
    from .excel_export import ExportOptions, SourceSpec, run_export

    if result.preset is not None:
        raise ValueError("The Excel export cannot separate the presets of a superframing recording yet")
    shapes = tuple(roi_shapes(result))
    if not shapes:
        raise ValueError("No TC pixel was found, so the workbook would have no ROIs")
    frame, offset = workbook_origin(result)
    spec = SourceSpec(path=Path(recording), rois=shapes, ignition_frame=frame, parameters=parameters)
    options = ExportOptions(start_s=float(table.times[0]) + offset, end_s=float(table.times[-1]) + offset,
                            step_s=result.step, tc_prefill=tc_prefill(result, table))
    return run_export(dest, [spec], options, handles=handles, progress=progress, abort=abort, replace=replace,
                      tool_version=tool_version)


def roi_shapes(result: MatchResult) -> list[RoiShape]:
    """A spot and an N × N box at every found TC pixel, for the Excel export."""
    shapes: list[RoiShape] = []
    number = 1
    for ch in result.channels:
        m = ch.match
        if not m.found or m.pixel is None:
            continue
        row, col = m.pixel
        short = ch.name.split(" (")[0]
        shapes.append(RoiShape(id=number, kind="cursor", points=((col + 0.5, row + 0.5),), name=f"{short} spot"))
        number += 1
        r0, r1, c0, c1 = spot_rect(m.pixel, result.spot, result.region)
        if r1 <= r0 or c1 <= c0:
            continue
        shapes.append(RoiShape(id=number, kind="rect", points=((c0, r0), (c1, r1)), name=f"{short} {c1 - c0}×{r1 - r0}"))
        number += 1
    return shapes


def save_figure(path: str | Path, result: MatchResult, location: Location, series: dict, samples: Samples,
                grid: np.ndarray) -> None:
    """Overview PNG: offset search, correlation maps, IR vs TC with the events, matching ε."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    found = [(ch, m) for ch, m in zip(result.channels, location.channels) if ch.name in series]
    cols = max(1, len(found))
    fig, axes = plt.subplots(4, cols, figsize=(6.5 * cols, 15), squeeze=False)
    params = samples.params.with_(reflected_k=result.reflected_c + KELVIN)
    x0, y0 = samples.origin
    for j, (ch, m) in enumerate(found):
        s = series[ch.name]
        spot, tc = s["spot"], s["channel"]
        ax = axes[0, j]
        k = [c.name for c in location.channels].index(ch.name)
        ax.plot(location.curve_lags_s, location.curve_r[:, k], lw=0.8)
        ax.axvline(result.lag_s, color="#e34948", lw=0.8)
        ax.set_title(f"{ch.name}: best r over pixels vs offset", fontsize=9)
        ax.set_xlabel("recording time of logger time 0 (s)")
        ax = axes[1, j]
        if m.r_map is not None:
            h, w = m.r_map.shape
            im = ax.imshow(m.r_map, cmap="RdBu_r", vmin=-1, vmax=1, extent=(x0, x0 + w, y0 + h, y0))
            fig.colorbar(im, ax=ax, fraction=0.03)
        if m.pixel is not None:
            ax.plot(m.pixel[1] + 0.5, m.pixel[0] + 0.5, "k+", ms=12, mew=2)
            if m.candidates:
                ax.plot([c[1] + 0.5 for c in m.candidates], [c[0] + 0.5 for c in m.candidates], "k.", ms=2)
        ax.set_title(f"correlation map at the shared offset (r {m.r:.3f})", fontsize=9)
        ax = axes[2, j]
        ax.plot(grid, tc.values, color="#eb6834", lw=1.5, label=ch.name)
        ax.plot(grid, spot.apparent, color="#2a78d6", lw=0.8, label="IR at ε 1")
        used_events = [e for e in ch.events if e.used] or ch.events
        fitted = [e for e in used_events if e.fit.n > 0 and math.isfinite(e.fit.eps)]  # no-fit events have no ε
        best = ch.eps[1] if ch.eps is not None else (max(fitted, key=lambda e: e.fit.n).fit.eps if fitted else None)
        if best is not None:
            temp = object_temperature(samples.calibration, params.with_(emissivity=best), spot.counts) - KELVIN
            ax.plot(grid, temp, color="#1baf7a", lw=0.8, label=f"IR at ε {best:.2f}")
        for e in ch.events:
            ax.axvspan(e.start_s, e.end_s, color="#1baf7a", alpha=0.12, lw=0)
        ax.set_ylabel("°C")
        ax.legend(fontsize=8, frameon=False)
        ax = axes[3, j]
        S = samples.calibration.planck.signal
        s_r = float(S(params.reflected_k))
        with np.errstate(divide="ignore", invalid="ignore"):
            match = (compensated_signal(samples.calibration, params, spot.counts) - s_r) / (
                S(tc.values + KELVIN) - s_r)
        hot = tc.values >= result.options.get("tc_min_c", 50.0)
        ax.plot(grid[hot], match[hot], ".", ms=1.5, color="#8a8a8a")
        ax.axhline(1, color="#e34948", lw=0.8)
        ax.set_ylim(0, 2.5)
        ax.set_ylabel("matching ε")
        ax.set_xlabel("logger time (s)")
    fig.tight_layout()
    fig.savefig(path, dpi=80)
    plt.close(fig)


# --- orchestration ----------------------------------------------------------------------------------


@dataclass
class RunOutput:
    """What ``run_analysis`` produced: the result, the TC table it used, and the files written."""

    result: MatchResult
    table: TcTable
    files: dict[str, Path] = field(default_factory=dict)  # "summary", "json", "rois", "figure", "workbook"
    parameters: dict | None = None  # the SDK object parameters used instead of the recording's own


def run(recording: str | Path, tc_file: str | Path, options: MatchOptions, *, out_dir: str | Path | None = None,
        cache_dir: str | Path | None = None, sheet: str | None = None, time_column: int | str | None = None,
        figure: bool = True, workbook: bool = False,
        progress: Progress | None = None, abort: Abort | None = None,
        log: Callable[[str], None] | None = None) -> MatchResult:
    """Whole analysis; writes result.json, summary.txt, rois.json and overview.png into ``out_dir``.

    ``workbook`` also writes ``<recording>_vs_TC.xlsx`` there (see ``write_workbook``).
    """
    return run_analysis(recording, tc_file, options, out_dir=out_dir, cache_dir=cache_dir, sheet=sheet,
                        time_column=time_column, figure=figure, workbook=workbook, progress=progress, abort=abort,
                        log=log).result


def run_analysis(recording: str | Path, tc_file: str | Path, options: MatchOptions, *,
                 out_dir: str | Path | None = None, cache_dir: str | Path | None = None, sheet: str | None = None,
                 time_column: int | str | None = None, figure: bool = True, workbook: bool = False,
                 im: Any = None, parameters: dict | None = None,
                 progress: Progress | None = None, abort: Abort | None = None,
                 log: Callable[[str], None] | None = None,
                 stage: Callable[[str], None] | None = None) -> RunOutput:
    """``run``, keeping the TC table and the paths written; for the player's dialog.

    ``im`` lends the open recording (see ``sample_recording``); ``parameters``
    (SDK object-parameter values, e.g. the player's) replace the recording's
    own distance, reflected temperature and atmosphere; the emissivity is what
    the fit finds. ``stage`` hears which step runs.
    """
    from .excel_export import RoiSet, save_roi_set

    say = log or (lambda text: None)
    stage = stage or (lambda text: None)
    stage("Reading the TC file")
    table = read_tc_table(tc_file, sheet=sheet, time_column=time_column)
    step = options.step_s or table.interval
    say(f"TC file: {len(table.names)} channel(s), {table.times.size} rows every {table.interval:g} s")
    _check(abort)
    t0 = time.time()
    samples = sample_recording(recording, options, step_s=step, im=im, progress=progress, abort=abort,
                               cache_dir=cache_dir, stage=stage)
    if parameters is not None:
        samples.params = MeasurementParameters.from_sdk(parameters)
    say(f"Read {samples.info['frames']} frames in {time.time() - t0:.0f} s; {samples.report.message}")
    stage("Matching the TCs to pixels and fitting the emissivity")
    result, location, series = analyse(samples, table, options, progress=progress, abort=abort)
    _check(abort)
    output = RunOutput(result=result, table=table, parameters=dict(parameters) if parameters is not None else None)
    if out_dir is not None:
        stage("Saving the results")
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        files = output.files
        files["json"] = out / "result.json"
        files["json"].write_text(json.dumps(result.to_dict(), indent=1), encoding="utf-8")
        files["summary"] = out / "summary.txt"
        files["summary"].write_text(summary_text(result), encoding="utf-8")
        width, height = samples.size
        files["rois"] = out / f"{Path(recording).name}.rois.json"
        save_roi_set(files["rois"], RoiSet(rois=tuple(roi_shapes(result)), ignition_frame=workbook_origin(result)[0],
                                           recording=Path(recording).name, size=(width, height)))
        if figure and series:
            grid, _channels = prepare_channels(table, options, samples.step)
            files["figure"] = out / "overview.png"
            save_figure(files["figure"], result, location, series, samples, grid)
        if workbook:
            if result.preset is not None or not roi_shapes(result):
                say("No workbook: " + ("the Excel export cannot separate superframing presets yet"
                                       if result.preset is not None else "no TC pixel was found"))
            else:
                files["workbook"] = out / f"{Path(recording).stem}_vs_TC.xlsx"
                say(write_workbook(files["workbook"], recording, result, table, abort=abort,
                                   handles={Path(recording).resolve(): im} if im is not None else None,
                                   parameters=parameters))
    return output


def _pair(text: str, kind=float) -> tuple:
    parts = [p for p in text.replace(";", ",").split(",") if p.strip()]
    return tuple(kind(p) for p in parts)


def main(argv: Sequence[str] | None = None) -> int:
    from .tcdata import parse_windows

    parser = argparse.ArgumentParser(prog="python -m flir_player.tcmatch",
                                     description="Find TC pixels in an IR recording and fit the emissivity.")
    parser.add_argument("recording")
    parser.add_argument("tc_file")
    parser.add_argument("--out", help="folder for result.json, summary.txt, rois.json, overview.png")
    parser.add_argument("--cache", help="folder that keeps the sampled statistics between runs")
    parser.add_argument("--sheet", help="workbook sheet with the TC data (default: one named like TC data)")
    parser.add_argument("--time-column", help="header of the TC file's time column (default: found)")
    parser.add_argument("--roi", help="x0,y0,x1,y1 in pixels (end exclusive)")
    parser.add_argument("--preset", type=int)
    parser.add_argument("--step", type=float, help="time bin in s (default: the logger interval)")
    parser.add_argument("--channels", help="comma-separated TC channels (names or numbers)")
    parser.add_argument("--valid", action="append", default=[],
                        help='validity windows of one TC, e.g. "T2:0-1150" (repeatable)')
    parser.add_argument("--scope", help="logger window for all TCs, e.g. 0-2087")
    parser.add_argument("--lag", type=float, help="fixed offset: recording seconds of logger time 0")
    parser.add_argument("--lag-range", help="search window for the offset, recording seconds a,b")
    parser.add_argument("--pixel", action="append", default=[], help='fixed pixel of one TC, "T2:462,428" (row,col)')
    parser.add_argument("--spot", type=int, default=3)
    parser.add_argument("--tc-min", type=float, default=50.0)
    parser.add_argument("--reflected", type=float, help="reflected temperature, °C")
    parser.add_argument("--jump", type=float, default=30.0,
                        help="a TC rising more than this (K) within 5 s has a flame on it (default 30)")
    parser.add_argument("--hold", type=float, default=30.0,
                        help="seconds after such a jump that are not fitted (default 30; 0 = off)")
    parser.add_argument("--below-range", action="store_true",
                        help="also fit IR below the calibrated range (extrapolated)")
    parser.add_argument("--no-figure", action="store_true")
    parser.add_argument("--workbook", action="store_true", help="also write an Excel workbook with the TC data")
    args = parser.parse_args(argv)
    valid = {}
    for item in args.valid:
        key, _, windows = item.partition(":")
        valid[key.strip()] = parse_windows(windows)
    pixels = {}
    for item in args.pixel:
        key, _, where = item.partition(":")
        pixels[key.strip()] = _pair(where, int)
    scope = parse_windows(args.scope)[0] if args.scope else None
    options = MatchOptions(
        roi=_pair(args.roi, int) if args.roi else None, preset=args.preset, step_s=args.step,
        channels=tuple(c.strip() for c in args.channels.split(",")) if args.channels else (),
        valid=valid, scope=scope, lag_s=args.lag, lag_range=_pair(args.lag_range) if args.lag_range else None,
        pixels=pixels, spot=args.spot, tc_min_c=args.tc_min, reflected_c=args.reflected, jump_k=args.jump,
        jump_hold_s=args.hold, below_range=args.below_range)
    last = [0.0]

    def progress(done: int, total: int) -> None:
        if time.time() - last[0] > 5 or done == total:
            last[0] = time.time()
            print(f"  {done}/{total}", flush=True)

    out = args.out or str(Path(args.recording).with_suffix("")) + "_tcmatch"
    result = run(args.recording, args.tc_file, options, out_dir=out, cache_dir=args.cache, sheet=args.sheet,
                 time_column=args.time_column, figure=not args.no_figure, workbook=args.workbook, progress=progress,
                 log=lambda s: print(s, flush=True))
    print(summary_text(result))
    print(f"Results in {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import flir_player  # noqa: F401  (FileSDK DLL preload before fnv)

    sys.exit(main())
