# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Frame processing pipeline (ResearchIR §4.8.6 filters, §4.9.5.3 file operation).

Pure NumPy, SDK-free and fully unit-testable. The pipeline runs inside
``source.read_frame`` in fixed order: file operation → point filter → spatial
filter → temporal filter. While it is active, ROI statistics are computed
app-side from the processed array (the SDK only sees pre-processing data).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from .models import RoiShape, RoiStats

POINT_FILTERS: tuple[str, ...] = ("gain", "offset", "exp", "ln", "sqrt")
SPATIAL_FILTERS: tuple[str, ...] = ("gaussian", "average", "median")
TEMPORAL_FILTERS: tuple[str, ...] = ("min", "max", "average", "subtract")
FILE_OPERATIONS: tuple[str, ...] = ("subtract", "add", "multiply", "divide")


@dataclass(frozen=True, slots=True)
class ProcessingState:
    """Which pipeline stages are active. ``none`` disables a stage."""

    file_op: str | None = None
    point: tuple[str, float] = ("none", 1.0)
    spatial: tuple[str, int] = ("none", 3)
    temporal: tuple[str, int] = ("none", 5)

    @property
    def is_active(self) -> bool:
        return (
            self.file_op is not None
            or self.point[0] != "none"
            or self.spatial[0] != "none"
            or self.temporal[0] != "none"
        )


def state_from_dict(values: dict) -> ProcessingState:
    """Rebuild a state from the plain dict the UI sends across threads."""
    return ProcessingState(
        file_op=values.get("file_op"),
        point=tuple(values.get("point", ("none", 1.0))),
        spatial=tuple(values.get("spatial", ("none", 3))),
        temporal=tuple(values.get("temporal", ("none", 5))),
    )


# --- file operation (§4.9.5.3) ----------------------------------------------------


def apply_file_operation(
    data: np.ndarray, reference: np.ndarray | None, operation: str | None
) -> np.ndarray:
    if reference is None or operation is None:
        return data
    if operation == "subtract":
        return data - reference
    if operation == "add":
        return data + reference
    if operation == "multiply":
        return data * reference
    if operation == "divide":
        safe = np.where(reference != 0, reference, 1.0)
        return np.where(reference != 0, data / safe, np.nan)
    return data


# --- point filters (§4.8.6) ---------------------------------------------------------


def apply_point_filter(data: np.ndarray, name: str, value: float) -> np.ndarray:
    if name == "gain":
        return data * float(value)
    if name == "offset":
        return data + float(value)
    if name == "exp":
        return np.exp(data)
    if name == "ln":
        return np.log(np.where(data > 0, data, np.nan))
    if name == "sqrt":
        return np.sqrt(np.where(data >= 0, data, np.nan))
    return data


# --- spatial filters (§4.8.6) ---------------------------------------------------------


def _odd_size(size: int) -> int:
    return max(3, int(size) | 1)


def _moving_sum_axis(data: np.ndarray, size: int, axis: int) -> np.ndarray:
    """Windowed sum along one axis with edge-replicate padding (odd ``size``)."""
    radius = size // 2
    pad_width = [(0, 0)] * data.ndim
    pad_width[axis] = (radius, radius)
    padded = np.pad(data, pad_width, mode="edge")
    cumulative = np.cumsum(padded, axis=axis)
    leading = np.zeros_like(np.take(cumulative, [0], axis=axis))
    cumulative = np.concatenate([leading, cumulative], axis=axis)
    count = data.shape[axis]
    high = np.arange(size, size + count)
    low = np.arange(count)
    return np.take(cumulative, high, axis=axis) - np.take(cumulative, low, axis=axis)


def box_mean(data: np.ndarray, size: int) -> np.ndarray:
    """NaN-aware box (window) average via the cumsum integral — O(N) per axis."""
    size = _odd_size(size)
    valid = np.isfinite(data)
    values = np.where(valid, data, 0.0).astype(np.float64)
    weights = valid.astype(np.float64)
    sums = _moving_sum_axis(_moving_sum_axis(values, size, 0), size, 1)
    counts = _moving_sum_axis(_moving_sum_axis(weights, size, 0), size, 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return sums / np.where(counts > 0, counts, np.nan)


def gaussian_blur(data: np.ndarray, size: int) -> np.ndarray:
    """Gaussian approximation: three box-blur passes."""
    result = data
    for _ in range(3):
        result = box_mean(result, size)
    return result


def median_filter(data: np.ndarray, size: int) -> np.ndarray:
    size = _odd_size(size)
    radius = size // 2
    padded = np.pad(data.astype(np.float32), radius, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, (size, size))
    return np.nanmedian(windows, axis=(-2, -1))


def apply_spatial_filter(data: np.ndarray, name: str, size: int) -> np.ndarray:
    if name == "average":
        return box_mean(data, size)
    if name == "gaussian":
        return gaussian_blur(data, size)
    if name == "median":
        return median_filter(data, size)
    return data


# --- temporal filters (§4.8.6) ---------------------------------------------------------


class TemporalBuffer:
    """Ring buffer of recent pipeline-input frames for temporal filters."""

    def __init__(self) -> None:
        self._frames: deque[np.ndarray] = deque()

    def reset(self) -> None:
        self._frames.clear()

    def __len__(self) -> int:
        return len(self._frames)

    def apply(self, data: np.ndarray, name: str, depth: int) -> np.ndarray:
        depth = max(2, int(depth))
        self._frames.append(data)
        while len(self._frames) > depth:
            self._frames.popleft()
        if name == "subtract":
            # sliding subtraction: current minus oldest frame in the window
            return data - self._frames[0]
        stack = np.stack(tuple(self._frames))
        if name == "average":
            return np.nanmean(stack, axis=0)
        if name == "min":
            return np.nanmin(stack, axis=0)
        if name == "max":
            return np.nanmax(stack, axis=0)
        return data


# --- app-side ROI statistics (used while the pipeline is active) ----------------------


def roi_stats_app(data: np.ndarray, shapes: tuple[RoiShape, ...]) -> tuple[RoiStats, ...]:
    """ROI statistics computed from a processed frame (mirrors SDK semantics)."""
    height, width = data.shape
    results: list[RoiStats] = []
    for shape in shapes:
        ys, xs = _roi_coordinates(shape, height, width)
        values = data[ys, xs].astype(np.float64) if ys.size else np.empty(0)
        finite = np.isfinite(values)
        fvalues = values[finite]
        fys = ys[finite]
        fxs = xs[finite]
        if fvalues.size == 0:
            results.append(
                RoiStats(
                    id=shape.id,
                    name=shape.name,
                    kind=shape.kind,
                    minimum=0.0,
                    maximum=0.0,
                    mean=0.0,
                    std_dev=0.0,
                    num_pixels=0,
                    value=0.0,
                    min_position=None,
                    max_position=None,
                )
            )
            continue
        low_index = int(np.argmin(fvalues))
        high_index = int(np.argmax(fvalues))
        results.append(
            RoiStats(
                id=shape.id,
                name=shape.name,
                kind=shape.kind,
                minimum=float(fvalues[low_index]),
                maximum=float(fvalues[high_index]),
                mean=float(fvalues.mean()),
                std_dev=float(fvalues.std()),
                num_pixels=int(fvalues.size),
                value=float(fvalues.mean()),
                min_position=(int(fxs[low_index]), int(fys[low_index])),
                max_position=(int(fxs[high_index]), int(fys[high_index])),
            )
        )
    return tuple(results)


def _roi_coordinates(
    shape: RoiShape, height: int, width: int
) -> tuple[np.ndarray, np.ndarray]:
    """(ys, xs) index arrays covered by the ROI geometry."""

    def clip_point(point: tuple[float, float]) -> tuple[int, int]:
        return (
            max(0, min(int(round(point[0])), width - 1)),
            max(0, min(int(round(point[1])), height - 1)),
        )

    if shape.kind == "cursor" and len(shape.points) == 1:
        x, y = clip_point(shape.points[0])
        return np.array([y]), np.array([x])

    if shape.kind == "line" and len(shape.points) == 2:
        (x0, y0), (x1, y1) = shape.points
        length = int(round(float(np.hypot(x1 - x0, y1 - y0)))) + 1
        xs = np.clip(np.round(np.linspace(x0, x1, length)).astype(int), 0, width - 1)
        ys = np.clip(np.round(np.linspace(y0, y1, length)).astype(int), 0, height - 1)
        return ys, xs

    if shape.kind in {"rect", "ellipse"} and len(shape.points) == 2:
        (x0, y0), (x1, y1) = clip_point(shape.points[0]), clip_point(shape.points[1])
        left, right = sorted((x0, x1))
        top, bottom = sorted((y0, y1))
        yy, xx = np.mgrid[top:bottom, left:right]
        if shape.kind == "ellipse":
            cx = (left + right) / 2.0
            cy = (top + bottom) / 2.0
            rx = max((right - left) / 2.0, 0.5)
            ry = max((bottom - top) / 2.0, 0.5)
            inside = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1.0
            return yy[inside].ravel(), xx[inside].ravel()
        return yy.ravel(), xx.ravel()

    return np.empty(0, dtype=int), np.empty(0, dtype=int)
