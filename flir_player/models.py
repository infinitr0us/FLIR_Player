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

    @property
    def filename(self) -> str:
        return self.path.name

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
