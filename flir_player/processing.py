# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Frame processing pipeline (ResearchIR §4.8.6 filters, §4.9.5.3 file operation).

Pure NumPy, SDK-free and fully unit-testable. The pipeline runs inside
``source.read_frame`` in fixed order: file operation → point filter → spatial
filter → temporal filter. While it is active, ROI statistics are computed
app-side from the processed array (the SDK only sees pre-processing data).
"""

from __future__ import annotations

import warnings
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
    # Counts are uint16. Promote before arithmetic, including products whose
    # exact integer values exceed float32's 24-bit mantissa.
    dtype = np.float64 if data.dtype.kind in "iu" or reference.dtype.kind in "iu" else np.result_type(data, reference)
    data = data.astype(dtype, copy=False)
    reference = reference.astype(dtype, copy=False)
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
    """NaN-aware box (window) average via the cumsum integral — O(N) per axis.

    The value integral stays float64 on purpose: window sums of counts-scale
    data exceed float32's exact-integer range (2^24), which would band. The
    0/1 weight integral is float32-safe (max count < 2^24 pixels) and halves
    its memory bandwidth.
    """
    size = _odd_size(size)
    valid = np.isfinite(data)
    values = data.astype(np.float64)
    values[~valid] = 0.0
    weights = valid.astype(np.float32)
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


# A sliding-window median exposes HxWxKxK elements to np.nanmedian, which
# materializes them internally (~791 MiB for 15x15 at 1280x720). Until a
# compiled kernel is selected (dependency decision: see the performance
# review note), the kernel is clamped to this budget and the user is warned.
_MEDIAN_ELEMENT_BUDGET = 100_000_000


def median_max_size(num_pixels: int) -> int:
    """Largest odd median kernel within the element budget for a frame of
    ``num_pixels`` pixels. Mirrors the processing-side clamp so the UI can
    constrain its spinner to the kernel that will actually be applied."""
    size = int(np.sqrt(_MEDIAN_ELEMENT_BUDGET / max(1, int(num_pixels))))
    return max(3, size if size % 2 else size - 1)


def _median_size_within_budget(data: np.ndarray, size: int) -> int:
    size = _odd_size(size)
    clamped = min(size, median_max_size(data.size))
    if clamped != size:
        warnings.warn(
            f"median kernel {size}×{size} on a {data.shape[1]}×{data.shape[0]} "
            f"frame exceeds the memory budget; clamped to {clamped}×{clamped}",
            RuntimeWarning,
            stacklevel=3,
        )
    return clamped


def median_filter(data: np.ndarray, size: int) -> np.ndarray:
    size = _median_size_within_budget(data, size)
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
    """Ring of recent pipeline-input frames for temporal filters.

    Storage is a preallocated contiguous (depth, H, W) ring, float32 for
    Counts/float32 inputs and float64 for higher-precision processing,
    replacing the per-frame ``np.stack`` of the whole history (~105 MiB
    transient at depth 30, 1280×720). ``average`` maintains rolling finite
    sum/count arrays — O(pixels) per frame instead of O(depth × pixels).
    ``min``/``max`` reduce the ring in place; ``subtract`` uses the oldest
    frame in the window (unchanged semantics).
    """

    def __init__(self) -> None:
        self._ring: np.ndarray | None = None
        self._sum: np.ndarray | None = None
        self._valid: np.ndarray | None = None
        self._index = 0  # next write position
        self._count = 0  # frames currently in the window
        self._depth = 0

    def reset(self) -> None:
        self._ring = None
        self._sum = None
        self._valid = None
        self._index = 0
        self._count = 0
        self._depth = 0

    def __len__(self) -> int:
        return self._count

    def apply(self, data: np.ndarray, name: str, depth: int) -> np.ndarray:
        depth = max(2, int(depth))
        if (
            self._ring is None
            or self._depth != depth
            or self._ring.shape[1:] != data.shape
            or self._ring.dtype != np.result_type(data.dtype, np.float32)
        ):
            self._ring = np.empty((depth,) + data.shape, dtype=np.result_type(data.dtype, np.float32))
            self._sum = np.zeros(data.shape, dtype=np.float64)
            self._valid = np.zeros(data.shape, dtype=np.float64)
            self._index = 0
            self._count = 0
            self._depth = depth
        assert self._sum is not None and self._valid is not None

        if self._count == self._depth:
            # evict the oldest frame from the rolling statistics
            outgoing = self._ring[self._index]
            np.subtract(
                self._sum, np.where(np.isfinite(outgoing), outgoing, 0.0), out=self._sum
            )
            np.subtract(self._valid, np.isfinite(outgoing), out=self._valid)
        else:
            self._count += 1

        slot = self._ring[self._index]
        np.copyto(slot, data, casting="unsafe")  # retain the processing dtype
        np.add(self._sum, np.where(np.isfinite(slot), slot, 0.0), out=self._sum)
        np.add(self._valid, np.isfinite(slot), out=self._valid)
        self._index = (self._index + 1) % self._depth

        if name == "subtract":
            # sliding subtraction: current minus oldest frame in the window
            oldest = self._ring[self._index] if self._count == self._depth else self._ring[0]
            return data - oldest
        if name == "average":
            with np.errstate(invalid="ignore", divide="ignore"):
                return self._sum / self._valid
        window = self._ring if self._count == self._depth else self._ring[: self._count]
        if name == "min":
            return np.nanmin(window, axis=0)
        if name == "max":
            return np.nanmax(window, axis=0)
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


# Backward-compatible name for callers; one shared geometry implementation.
from .geometry import roi_coordinates as _roi_coordinates
