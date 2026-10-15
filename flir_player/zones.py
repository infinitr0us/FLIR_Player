# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cell zones: one box split into equal boxes, and which zone each TC sits in (Qt-free).

A battery module seen from the side is a row of cells. Splitting a box drawn over the row into
equal boxes gives one zone per cell, numbered from the chosen end (the heater end on the fire
tests). Equal widths assume the camera faces the row square on. A row that runs at a slant in the
picture (a camera looking along the module from above) is followed by giving its rows at the box's
last column as well: each zone keeps the box's columns for its cell and takes the row's rows at its
own centre, so the zones step along the slant and stay plain boxes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .geometry import roi_coordinates

ENDS = ("right", "left", "top", "bottom")  # where zone 1 is
MAX_ZONES = 200  # the split dialog's limits, also for zone groups read from a saved set
MAX_GAP = 20


@dataclass(frozen=True, slots=True)
class ZoneSpec:
    """How a box was split; kept so the zones can be re-split or joined back."""

    box: tuple[tuple[float, float], tuple[float, float]]  # (left, top), (right, bottom)
    count: int = 18
    start: str = "right"
    gap: int = 1  # pixel columns (rows) left out between neighbouring zones
    prefix: str = "Cell"
    # the zones' rows (top, bottom) at the box's last column, or their columns (left, right) at its last
    # row for a split along the rows; end exclusive like ``box``. None: the box's own (no slant)
    far: tuple[float, float] | None = None


def normalized_box(points) -> tuple[tuple[float, float], tuple[float, float]]:
    """(left, top), (right, bottom) with corners on pixel boundaries, as box coverage rounds them."""
    (x0, y0), (x1, y1) = points
    left, right = sorted((int(round(x0)), int(round(x1))))
    top, bottom = sorted((int(round(y0)), int(round(y1))))
    return (float(left), float(top)), (float(right), float(bottom))


def far_label(start: str) -> str:
    """What ``ZoneSpec.far`` holds for a split from ``start``: "rows at the last column" or the like."""
    return "rows at the last column" if start in ("right", "left") else "columns at the last row"


def normalized_far(spec: ZoneSpec) -> tuple[int, int]:
    """``spec.far`` on pixel boundaries, rounded as ``normalized_box`` rounds the box's corners, in order.

    Raises ValueError when it is not two finite numbers or covers no pixel."""
    try:
        a, b = (float(v) for v in spec.far)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"The {far_label(spec.start)} must be two numbers") from exc
    if not (math.isfinite(a) and math.isfinite(b)):
        raise ValueError(f"The {far_label(spec.start)} must be numbers")
    a, b = sorted((int(round(a)), int(round(b))))
    if b <= a:
        raise ValueError(f"The {far_label(spec.start)} must cover at least one pixel")
    return a, b


def _spans(low: int, high: int, count: int, gap: int) -> tuple[list[int], list[int]]:
    """(starts, ends) of ``count`` zones over pixels low..high-1, ascending, end exclusive.

    Inner boundary j: the lower zone ends at ends[j], the upper one starts ``gap`` later (the gap centred
    on the boundary); the box's ends lose half a gap too (the module's ends are mixed pixels as well), so
    every zone is as wide, to a pixel.
    """
    step = (high - low) / count
    ends = [math.floor(low + j * step - gap / 2 + 0.5) for j in range(1, count)]
    return [low + gap // 2] + [end + gap for end in ends], ends + [high - gap // 2]


def split_box(spec: ZoneSpec, width: int | None = None, height: int | None = None
              ) -> list[tuple[str, tuple[tuple[float, float], tuple[float, float]]]]:
    """(name, box corners) of each zone, zone 1 at ``spec.start``.

    Zone edges lie on pixel boundaries (box coverage would otherwise round ties apart).
    ``spec.gap`` whole pixel columns (rows, for a vertical split) are left out at each inner
    boundary, centred on it, so the mixed pixels where two cells meet belong to no zone; the box's
    ends lose half a gap (rounded down) each. Zone sizes differ by at most one pixel. With ``spec.far`` (rounded
    to whole pixels like the box's corners), the zones' rows (columns) run in a straight line from
    the box's at its first column (row) to ``spec.far`` at its last, each zone taking them at its
    centre, rounded half up. Raises
    ValueError when the box is too small for the zones, or, given the image size, when a zone
    covers no pixel.
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
    near = (int(top), int(bottom)) if along_x else (int(left), int(right))  # across, at the first column (row)
    far = normalized_far(spec) if spec.far is not None else near
    starts, ends = _spans(low, high, spec.count, spec.gap)
    if any(end <= start for start, end in zip(starts, ends)):
        shortest = next((n for n in range(high - low + 1, spec.count * (spec.gap + 2) + 2)
                         if all(e > a for a, e in zip(*_spans(0, n, spec.count, spec.gap)))),
                        spec.count * (spec.gap + 1))
        raise ValueError(f"{spec.count} zones with a {spec.gap} px gap need a box at least "
                         f"{shortest} px long; this one is {high - low} px")
    ascending = list(zip(starts, ends))
    if spec.start in ("right", "bottom"):
        ascending.reverse()
    prefix = " ".join(spec.prefix.split()) or "Zone"
    zones = []
    for k, (lo, hi) in enumerate(ascending):
        a, b = near
        if far != near:
            # across the split at this zone's centre, n + (f - n) * (lo + hi - 2 low) / (2 (high - low)),
            # rounded half up in whole numbers: a band of constant height keeps its height in every zone
            # (floats can land a hair below a .5), and one a pixel or more tall at both ends covers a pixel
            den = 2 * (high - low)
            t = lo + hi - 2 * low
            a, b = ((2 * (n * den + (f - n) * t) + den) // (2 * den) for n, f in zip(near, far))
        lo, hi, a, b = float(lo), float(hi), float(a), float(b)
        points = ((lo, a), (hi, b)) if along_x else ((a, lo), (b, hi))
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
    duplicates: list[str] = field(default_factory=list)  # names of paired ROIs that other ROIs also carry

    def describe(self) -> str:
        """One line for a dialog: "T1 → Cell 3 · T2 → Cell 6; T3 is in no ROI"."""
        text = " · ".join(f"{tc} → {roi}" for tc, roi in self.pairs.items()) or "No TC pixel lies in an ROI"
        if self.outside:
            text += f"; {', '.join(self.outside)} {'is' if len(self.outside) == 1 else 'are'} in no ROI"
        for roi, tcs in self.shared.items():
            text += f"; {roi} holds {', '.join(tcs)} (paired with {tcs[0]})"
        for roi in self.duplicates:
            text += f"; more than one ROI is named {roi}"
        return text


def pair_tcs(pixels: Mapping[str, tuple[int, int]], rois: Sequence, height: int, width: int) -> Pairing:
    """Pair each TC pixel (row, col) with the smallest box, ellipse or spot containing it.

    The smallest ROI wins, so a search box around the whole module never takes a TC from its zone.
    Lines (profiles) are not paired. An ROI holding several TC pixels is paired with the first of
    them (in ``pixels`` order); the others stay unpaired and are listed in ``shared``. The workbook pairs
    ROIs and TCs by name, so a paired ROI whose name another ROI also carries is listed in ``duplicates``.
    """
    rois = list(rois)
    areas = []
    for index, shape in enumerate(rois):
        if shape.kind == "line":
            continue
        ys, xs = roi_coordinates(shape, height, width)
        if ys.size:
            areas.append((int(ys.size), index, set(zip(ys.tolist(), xs.tolist()))))
    areas.sort(key=lambda item: item[0])
    result = Pairing()
    inside: dict[int, list[str]] = {}  # by position: two ROIs may share a name
    for tc, (row, col) in pixels.items():
        owner = next((index for _n, index, cells in areas if (int(row), int(col)) in cells), None)
        if owner is None:
            result.outside.append(tc)
            continue
        inside.setdefault(owner, []).append(tc)
    names = [shape.name for shape in rois]
    for index, tcs in inside.items():
        roi = names[index]
        result.pairs[tcs[0]] = roi
        if len(tcs) > 1:
            result.shared[roi] = tcs
        if names.count(roi) > 1 and roi not in result.duplicates:
            result.duplicates.append(roi)
    order = list(pixels)
    result.pairs = dict(sorted(result.pairs.items(), key=lambda item: order.index(item[0])))
    return result


def short_label(name: str) -> str | None:
    """The trailing number of an ROI name ("Cell 12" → "12"), or None when it has none."""
    parts = name.rsplit(None, 1)
    return parts[-1] if len(parts) == 2 and parts[-1].isdigit() else None
