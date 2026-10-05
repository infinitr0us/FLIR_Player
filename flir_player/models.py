# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np


@dataclass(frozen=True, slots=True)
class UnitOption:
    """A File SDK unit exposed in the player UI."""

    key: str
    label: str
    suffix: str


ROI_COLORS: tuple[str, ...] = (
    "#F5A524",
    "#4CC2FF",
    "#7CE38B",
    "#FF7A90",
    "#C792EA",
    "#FFD866",
    "#5BE7D8",
    "#FF9E64",
)

ROI_KIND_LABELS: dict[str, str] = {"rect": "Box", "ellipse": "Ellipse", "line": "Line", "cursor": "Spot"}


@dataclass(frozen=True, slots=True)
class CadenceInfo:
    """How the stored frames sit on the camera's capture grid.

    ``base_fps`` is the rate the recording's active camera preset reports, so
    the span between the first and last frame timestamps implies how many
    frames that rate would have produced (``expected_frames``). A recording
    holding fewer than that skipped capture grid slots — frames dropped while
    recording, or decimated during extraction — and its stored timestamps then
    sit on an uneven grid. Playback paced from those timestamps is faithfully
    uneven as a result, which is what this record exists to explain.

    Only built when the preset rate is marked valid; ``None`` metadata means
    "no base rate to compare against", never "cadence is even".
    """

    base_fps: float
    expected_frames: int
    stored_frames: int

    @property
    def kept_fraction(self) -> float:
        if self.expected_frames <= 0:
            return 1.0
        return min(1.0, self.stored_frames / self.expected_frames)

    @property
    def missing_frames(self) -> int:
        return max(0, self.expected_frames - self.stored_frames)

    @property
    def is_even(self) -> bool:
        """True when essentially every capture grid slot was stored.

        The 2 % tolerance absorbs rounding in ``expected_frames`` and the odd
        single dropped frame; the recordings this is meant to flag sit far
        below it (the measured ATS sample keeps 59.9 %).
        """
        return self.kept_fraction >= 0.98


@dataclass(frozen=True, slots=True)
class FrameRate:
    """The rate that turns frame numbers into seconds (``sdktime.frame_rate``).

    ``clock_fps`` is the rate the first and last timestamps imply (0 when
    unknown). ``camera_fps`` is set when those timestamps run slow: more frames
    were stored than their span allows at any camera rate, so ``fps`` is the
    nominal camera rate instead and frame times come from the frame number.
    """

    fps: float
    clock_fps: float = 0.0
    camera_fps: float = 0.0

    @property
    def corrected(self) -> bool:
        return self.camera_fps > 0

    @property
    def clock_error(self) -> float:
        """How far the timestamps run slow, as a fraction (0.016 = 1.6 %)."""
        return self.clock_fps / self.camera_fps - 1.0 if self.corrected else 0.0


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    """Metadata needed by the player without retaining the SDK object."""

    path: Path
    width: int
    height: int
    num_frames: int
    start_time: datetime | None
    end_time: datetime | None
    duration_seconds: float
    nominal_fps: float
    camera_model: str = ""
    camera_serial: str = ""
    source_details: tuple[tuple[str, str], ...] = ()
    cadence: CadenceInfo | None = None
    rate: FrameRate | None = None
    # Object parameters the SDK applied on open from settings saved in the file
    # (a ResearchIR workspace override) where they differ from the camera's
    # recorded values; the player opens with the camera's values instead.
    saved_parameters: dict[str, float] | None = None
    saved_by: str = ""  # "ResearchIR" when its workspace XML holds the override

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def frame_timed(self) -> bool:
        """True when seconds come from frame numbers, not the (slow) timestamps."""
        return self.rate is not None and self.rate.corrected

    def fallback_seconds_for_frame(self, index: int) -> float:
        if self.nominal_fps <= 0:
            return 0.0
        return max(0.0, min(index, self.num_frames - 1) / self.nominal_fps)


@dataclass(frozen=True, slots=True)
class RoiShape:
    """App-owned ROI geometry in image pixel coordinates (ResearchIR §4.5)."""

    id: int
    kind: str  # "rect" | "ellipse" | "line" | "cursor"
    points: tuple[tuple[float, float], ...]
    name: str


@dataclass(frozen=True, slots=True)
class RoiStats:
    """Detached per-frame statistics for one ROI, in the current unit."""

    id: int
    name: str
    kind: str
    minimum: float
    maximum: float
    mean: float
    std_dev: float
    num_pixels: int
    value: float  # cursor ROIs: the pixel value at the cursor position
    min_position: tuple[int, int] | None
    max_position: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class FramePacket:
    """A detached frame safe to hand from the decode thread to Qt."""

    index: int
    data: np.ndarray
    timestamp: datetime | None
    unit: UnitOption
    minimum: float
    maximum: float
    mean: float
    request_id: int = 0
    roi_stats: tuple[RoiStats, ...] = ()
    std_dev: float = 0.0
    num_pixels: int = 0
    min_position: tuple[int, int] | None = None
    max_position: tuple[int, int] | None = None
    clip_mask: np.ndarray | None = None
    metadata_entries: tuple[tuple[str, str], ...] = ()
    # True when the corresponding optional payload was evaluated during decode.
    # ``clip_mask is None`` / empty entries are then valid full results, not
    # "not loaded" — the packet cache keys its sufficiency check off these.
    clip_loaded: bool = True
    metadata_loaded: bool = True
    revision: int = 0
    processing: object = None
    analysis: tuple = ()


@dataclass(frozen=True, slots=True)
class StatisticsSnapshot:
    packet: FramePacket
    metadata: VideoMetadata
    rois: tuple[RoiShape, ...]
