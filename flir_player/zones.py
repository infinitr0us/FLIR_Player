# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cell zones: one box split into equal boxes, and which zone each TC sits in (Qt-free).

A battery module seen from the side is a row of cells. Splitting a box drawn over the row into
equal boxes gives one zone per cell, numbered from the chosen end (the heater end on the fire
tests). Equal widths assume the camera faces the row square on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .geometry import roi_coordinates

ENDS = ("right", "left", "top", "bottom")  # where zone 1 is


@dataclass(frozen=True, slots=True)
class ZoneSpec:
    """How a box was split; kept so the zones can be re-split or joined back."""

    box: tuple[tuple[float, float], tuple[float, float]]  # (left, top), (right, bottom)
    count: int = 18
    start: str = "right"
    gap: int = 1  # pixel columns (rows) left out between neighbouring zones
    prefix: str = "Cell"


def normalized_box(points) -> tuple[tuple[float, float], tuple[float, float]]:
    """(left, top), (right, bottom) with corners on pixel boundaries, as box coverage rounds them."""
    (x0, y0), (x1, y1) = points
    left, right = sorted((int(round(x0)), int(round(x1))))
    top, bottom = sorted((int(round(y0)), int(round(y1))))
    return (float(left), float(top)), (float(right), float(bottom))


def split_box(spec: ZoneSpec, width: int | None = None, height: int | None = None
              ) -> list[tuple[str, tuple[tuple[float, float], tuple[float, float]]]]:
    """(name, box corners) of each zone, zone 1 at ``spec.start``.

    Zone edges lie on pixel boundaries (box coverage would otherwise round ties apart).
    ``spec.gap`` whole pixel columns (rows, for a vertical split) are left out at each inner
    boundary, centred on it, so the mixed pixels where two cells meet belong to no zone; the outer
    edges stay where the box is. Zone sizes differ by at most one pixel. Raises ValueError when
    the box is too small for the zones, or, given the image size, when a zone covers no pixel.
    """
    if spec.start not in ENDS:
        raise ValueError(f"Zone 1 must be at one of: {', '.join(ENDS)}")
    if not (isinstance(spec.count, int) and spec.count >= 1):
        raise ValueError("The number of zones must be a whole number from 1")
    if not (isinstance(spec.gap, int) and spec.gap >= 0):
        raise ValueError("The gap between zones must be a whole number of pixels, not negative")
    (left, top), (right, bottom) = normalized_box(spec.box)
    if right <= left or bottom <= top:
        raise ValueError("The box covers no pixel")
    along_x = spec.start in ("right", "left")
    low, high = (int(left), int(right)) if along_x else (int(top), int(bottom))
    step = (high - low) / spec.count
    # inner boundary j (ascending): the lower zone ends at ends[j], the upper one starts gap later
    ends = [math.floor(low + j * step - spec.gap / 2 + 0.5) for j in range(1, spec.count)]
    starts = [low] + [end + spec.gap for end in ends]
    ends = ends + [high]
    if any(end <= start for start, end in zip(starts, ends)):
        raise ValueError(f"{spec.count} zones with a {spec.gap} px gap need a box at least "
                         f"{spec.count * (1 + spec.gap) - spec.gap} px long; this one is {high - low} px")
    ascending = list(zip(starts, ends))
    if spec.start in ("right", "bottom"):
        ascending.reverse()
    prefix = " ".join(spec.prefix.split()) or "Zone"
    zones = []
    for k, (lo, hi) in enumerate(ascending):
        lo, hi = float(lo), float(hi)
        points = ((lo, top), (hi, bottom)) if along_x else ((left, lo), (right, hi))
        zones.append((f"{prefix} {k + 1}", points))
    if width is not None and height is not None:
        for name, points in zones:
            if not _covers(points, height, width):
                raise ValueError(f"{name} would cover no pixel; use a longer box, fewer zones or a smaller gap")
    return zones


def _covers(points, height: int, width: int) -> bool:
    probe = _Probe("rect", tuple(points))
    return roi_coordinates(probe, height, width)[0].size > 0


@dataclass(frozen=True, slots=True)
class _Probe:  # the fields roi_coordinates reads
    kind: str
    points: tuple


@dataclass
class Pairing:
    """Which ROI each TC pixel lies in."""

    pairs: dict[str, str] = field(default_factory=dict)  # TC → ROI name
    outside: list[str] = field(default_factory=list)  # TCs whose pixel lies in no ROI
    shared: dict[str, list[str]] = field(default_factory=dict)  # ROI → every TC inside it (when several)

    def describe(self) -> str:
        """One line for a dialog: "T1 → Cell 3 · T2 → Cell 6; T3 is in no ROI"."""
        text = " · ".join(f"{tc} → {roi}" for tc, roi in self.pairs.items()) or "No TC pixel lies in an ROI"
        if self.outside:
            text += f"; {', '.join(self.outside)} {'is' if len(self.outside) == 1 else 'are'} in no ROI"
        for roi, tcs in self.shared.items():
            text += f"; {roi} holds {', '.join(tcs)} (paired with {tcs[0]})"
        return text


def pair_tcs(pixels: Mapping[str, tuple[int, int]], rois: Sequence, height: int, width: int) -> Pairing:
    """Pair each TC pixel (row, col) with the smallest box, ellipse or spot containing it.

    The smallest ROI wins, so a search box around the whole module never takes a TC from its zone.
    Lines (profiles) are not paired. An ROI holding several TC pixels is paired with the first of
    them (in ``pixels`` order); the others stay unpaired and are listed in ``shared``.
    """
    areas = []
    for shape in rois:
        if shape.kind == "line":
            continue
        ys, xs = roi_coordinates(shape, height, width)
        if ys.size:
            areas.append((int(ys.size), shape, set(zip(ys.tolist(), xs.tolist()))))
    areas.sort(key=lambda item: item[0])
    result = Pairing()
    inside: dict[str, list[str]] = {}
    for tc, (row, col) in pixels.items():
        owner = next((shape.name for _n, shape, cells in areas if (int(row), int(col)) in cells), None)
        if owner is None:
            result.outside.append(tc)
            continue
        inside.setdefault(owner, []).append(tc)
    for roi, tcs in inside.items():
        result.pairs[tcs[0]] = roi
        if len(tcs) > 1:
            result.shared[roi] = tcs
    order = list(pixels)
    result.pairs = dict(sorted(result.pairs.items(), key=lambda item: order.index(item[0])))
    return result


def short_label(name: str) -> str | None:
    """The trailing number of an ROI name ("Cell 12" → "12"), or None when it has none."""
    parts = name.rsplit(None, 1)
    return parts[-1] if len(parts) == 2 and parts[-1].isdigit() else None
