# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cell zones: one box split into a row of four-sided zones, and which zone each TC sits in (Qt-free).

A battery module seen from the side is a row of cells. Splitting a box drawn over the row gives one
zone per cell, numbered from the chosen end (the heater end on the fire tests). The box's columns
give the row's length and its rows the band's rows at its first column; a row that runs at a slant
in the picture also gets the band's rows at the box's last column, and the band's edges run straight
between the two. The dividers between cells cross the band upright, or lean by a set number of
pixels where the camera sees the module at an angle (the "twist" of the 3D view). Cells are equal in
width, or narrow towards the far end when the camera's distance to each end of the row is given (a
perspective view). Single dividers can be moved by hand afterwards.

Zones are quads ("quad" ROIs): corners on pixel boundaries, (low-side first-edge, high-side
first-edge, high-side second-edge, low-side second-edge) where "low" is the box's left (top) end
and the first edge is the band's top (left, for a split along the rows). An upright zone covers the
same pixels as the box with its corners. Positions are computed in exact fractions, rounded half up.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Mapping, Sequence

import numpy as np

from .geometry import _quad_coordinates, quad_corners, roi_coordinates

ENDS = ("right", "left", "top", "bottom")  # where zone 1 is
MAX_ZONES = 200  # the split dialog's limits, also for zone groups read from a saved set
MAX_GAP = 20
MAX_LEAN = 200  # pixels a divider may lean across the band
ZONE_KIND = "quad"


@dataclass(frozen=True, slots=True)
class ZoneSpec:
    """How a box was split; kept so the zones can be re-split, adjusted or joined back."""

    box: tuple[tuple[float, float], tuple[float, float]]  # (left, top), (right, bottom)
    count: int = 18
    start: str = "right"
    gap: int = 1  # pixel columns (rows) left out between neighbouring zones
    prefix: str = "Cell"
    # the zones' rows (top, bottom) at the box's last column, or their columns (left, right) at its last
    # row for a split along the rows; end exclusive like ``box``. None: the box's own (no slant)
    far: tuple[float, float] | None = None
    # pixels a divider's end on the band's second edge (bottom; right for a split along the rows) lies along
    # the row from its end on the first edge (top; left): positive towards the box's right (bottom) end
    lean: int = 0
    # the camera's distance to zone 1's end of the row and to the far end (any one unit): cells then narrow
    # towards the farther end as a perspective view shows them. None: equal widths
    distances: tuple[float, float] | None = None
    # dividers moved by hand: (index from the box's left (top) end, 0..count, position on the first edge, on
    # the second edge), positions along the row in pixel-boundary coordinates of the divider's middle line
    moved: tuple[tuple[int, float, float], ...] = ()


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
    """(starts, ends) of ``count`` equal zones over pixels low..high-1, ascending, end exclusive (upright,
    no perspective: the shortest box the zones fit in, for the error message)."""
    step = (high - low) / count
    ends = [math.floor(low + j * step - gap / 2 + 0.5) for j in range(1, count)]
    return [low + gap // 2] + [end + gap for end in ends], ends + [high - gap // 2]


@dataclass(frozen=True, slots=True)
class _Band:
    along_x: bool  # zones side by side along x (zone 1 at the right or left end)
    low: int  # the box's first and end-exclusive last pixel along the row
    high: int
    near: tuple[int, int]  # the band's first and second edge across the row at ``low``
    far: tuple[int, int]  # ... at ``high``

    def edge(self, which: int, along: int) -> int:
        """The band's edge ``which`` (0 first, 1 second) across the row at ``along``, rounded half up."""
        n, f = self.near[which], self.far[which]
        if n == f:
            return n
        value = n + Fraction((f - n) * (along - self.low), self.high - self.low)
        return math.floor(value + Fraction(1, 2))

    def point(self, along: int, across: int) -> tuple[float, float]:
        return (float(along), float(across)) if self.along_x else (float(across), float(along))


def _band(spec: ZoneSpec) -> _Band:
    if spec.start not in ENDS:
        raise ValueError(f"Zone 1 must be at one of: {', '.join(ENDS)}")
    if not (isinstance(spec.count, int) and spec.count >= 1):
        raise ValueError("The number of zones must be a whole number from 1")
    if not (isinstance(spec.gap, int) and spec.gap >= 0):
        raise ValueError("The gap between zones must be a whole number of pixels, not negative")
    if not (isinstance(spec.lean, int) and abs(spec.lean) <= MAX_LEAN):
        raise ValueError(f"The lean must be a whole number of pixels from -{MAX_LEAN} to {MAX_LEAN}")
    (left, top), (right, bottom) = normalized_box(spec.box)
    if right <= left or bottom <= top:
        raise ValueError("The box covers no pixel")
    along_x = spec.start in ("right", "left")
    low, high = (int(left), int(right)) if along_x else (int(top), int(bottom))
    near = (int(top), int(bottom)) if along_x else (int(left), int(right))
    far = normalized_far(spec) if spec.far is not None else near
    return _Band(along_x, low, high, near, far)


def perspective_ratio(spec: ZoneSpec) -> Fraction | None:
    """Far end's distance over zone 1's end's (``spec.distances``), or None for equal widths.

    Raises ValueError when the distances are not two positive finite numbers."""
    if spec.distances is None:
        return None
    try:
        first, last = (float(v) for v in spec.distances)
    except (TypeError, ValueError) as exc:
        raise ValueError("The camera distances must be two numbers") from exc
    if not (math.isfinite(first) and math.isfinite(last) and first > 0 and last > 0):
        raise ValueError("The camera distances must be positive numbers")
    return Fraction(last) / Fraction(first)


def divider_positions(spec: ZoneSpec) -> list[tuple[Fraction, Fraction]]:
    """Each divider's middle line, from the box's low end (index 0) to its high end (``spec.count``): its
    position along the row on the band's first and second edge, in pixel-boundary coordinates.

    Dividers split the row's length equally, or in perspective (``spec.distances``): a point a fraction u of
    the row's length from zone 1's end shows at u (1 + k) / (1 + k u) of its length in the picture, k = far
    distance / near distance - 1 (a straight row at depths growing linearly along it). Each leans by
    ``spec.lean`` about the band's middle; dividers moved by hand (``spec.moved``) keep their own positions.
    """
    band = _band(spec)
    ratio = perspective_ratio(spec)
    k = ratio - 1 if ratio is not None else Fraction(0)
    length = band.high - band.low
    half_lean = Fraction(spec.lean, 2)
    from_high = spec.start in ("right", "bottom")  # zone 1 at the box's high end
    positions: list[tuple[Fraction, Fraction]] = [(Fraction(0), Fraction(0))] * (spec.count + 1)
    for j in range(spec.count + 1):  # j-th divider counted from zone 1's end
        u = Fraction(j, spec.count)
        shown = u * (1 + k) / (1 + k * u)
        middle = band.high - shown * length if from_high else band.low + shown * length
        index = spec.count - j if from_high else j
        positions[index] = (middle - half_lean, middle + half_lean)
    for index, first, second in spec.moved:
        if 0 <= index <= spec.count:
            positions[index] = (Fraction(first), Fraction(second))
    return positions


def _zone_ends(positions, index: int, edge: int, count: int, gap: int) -> tuple[int, int]:
    """(first pixel, end) along the row of the zone between dividers ``index`` and ``index + 1`` (from the
    box's low end) on band edge ``edge``: ``gap`` pixels are left out at each inner divider, centred on it,
    and the row's ends lose half a gap (rounded down)."""
    half = Fraction(1, 2)
    low, high = positions[index][edge], positions[index + 1][edge]
    start = math.floor(low + half) + gap // 2 if index == 0 else math.floor(low - Fraction(gap, 2) + half) + gap
    end = math.floor(high + half) - gap // 2 if index + 1 == count else math.floor(high - Fraction(gap, 2) + half)
    return start, end


def split_box(spec: ZoneSpec, width: int | None = None, height: int | None = None
              ) -> list[tuple[str, tuple[tuple[float, float], ...]]]:
    """(name, quad corners) of each zone, zone 1 at ``spec.start`` (``ZONE_KIND`` ROIs).

    Corners lie on pixel boundaries: along the row where each divider meets the band's edges (``gap`` whole
    pixels left out at each inner divider, centred on it; the row's ends lose half a gap, rounded down), and
    across it on the band's straight edges, rounded half up. Raises ValueError when a zone would be less than
    a pixel wide on either edge or, given the image size, would cover no pixel.
    """
    band = _band(spec)
    positions = divider_positions(spec)
    count, gap = spec.count, spec.gap
    ends = [[_zone_ends(positions, i, edge, count, gap) for edge in (0, 1)] for i in range(count)]
    narrow = next((i for i, pair in enumerate(ends) if any(b <= a for a, b in pair)), None)
    if narrow is not None:
        if spec.distances is None and not spec.moved:
            shortest = next((n for n in range(band.high - band.low + 1, count * (gap + 2) + 2 + abs(spec.lean))
                             if all(e > a for a, e in zip(*_spans(0, n, count, gap)))), count * (gap + 1))
            raise ValueError(f"{count} zones with a {gap} px gap need a box at least "
                             f"{shortest} px long; this one is {band.high - band.low} px")
        number = count - narrow if spec.start in ("right", "bottom") else narrow + 1
        raise ValueError(f"{' '.join(spec.prefix.split()) or 'Zone'} {number} would be less than a pixel wide: "
                         "move its dividers apart, or use fewer zones or a smaller gap")
    order = range(count - 1, -1, -1) if spec.start in ("right", "bottom") else range(count)
    prefix = " ".join(spec.prefix.split()) or "Zone"
    zones = []
    for number, i in enumerate(order, start=1):
        (a0, b0), (a1, b1) = ends[i]
        corners = (band.point(a0, band.edge(0, a0)), band.point(b0, band.edge(0, b0)),
                   band.point(b1, band.edge(1, b1)), band.point(a1, band.edge(1, a1)))
        zones.append((f"{prefix} {number}", corners))
    shared = _shared_pixel(zones)
    if shared is not None:
        raise ValueError(f"{shared[0]} and {shared[1]} would share pixels: the dividers lean too far for so thin a "
                         "band; use a smaller lean, a wider gap or a taller box")
    if width is not None and height is not None:
        for name, points in zones:
            if not _covers(points, height, width):
                raise ValueError(f"{name} would cover no pixel; use a longer box, fewer zones or a smaller gap")
    return zones


def _shared_pixel(zones) -> tuple[str, str] | None:
    """The first two zones holding a common pixel, or None. Corners rounded to whole pixels can let neighbours
    overlap where the dividers lean much more than the band is tall; such a split is refused."""
    corners = [quad_corners(points) for _name, points in zones]
    if any(c is None for c in corners):
        return None
    left = min(x for c in corners for x, _y in c)
    top = min(y for c in corners for _x, y in c)
    width = max(x for c in corners for x, _y in c) - left
    height = max(y for c in corners for _x, y in c) - top
    if width <= 0 or height <= 0:
        return None
    owner = np.full((height, width), -1, dtype=np.int32)
    for k, c in enumerate(corners):
        ys, xs = _quad_coordinates(tuple((x - left, y - top) for x, y in c), height, width)
        taken = owner[ys, xs]
        if (taken >= 0).any():
            return zones[int(taken[taken >= 0][0])][0], zones[k][0]
        owner[ys, xs] = k
    return None


def zone_names(spec: ZoneSpec) -> list[str]:
    """The zones' names in zone order (zone 1 first), as ``split_box`` gives them."""
    prefix = " ".join(spec.prefix.split()) or "Zone"
    return [f"{prefix} {number}" for number in range(1, spec.count + 1)]


# --- adjusting zones by hand ------------------------------------------------------------------


def zone_index(spec: ZoneSpec, number: int) -> int:
    """The position from the box's low end (0..count-1) of zone ``number`` (1 = zone 1)."""
    return spec.count - number if spec.start in ("right", "bottom") else number - 1


def corner_divider(spec: ZoneSpec, number: int, corner: int) -> tuple[int, int, Fraction]:
    """(divider index, band edge, offset) for corner ``corner`` (0-3, as ``split_box`` orders them) of zone
    ``number``: the divider that corner sits on, and what to add to a position for that corner to get the
    divider's middle line (half a gap, as ``_zone_ends`` leaves it out)."""
    index = zone_index(spec, number)
    high_side = corner in (1, 2)
    divider = index + 1 if high_side else index
    edge = 0 if corner in (0, 1) else 1
    outer = divider in (0, spec.count)
    half_gap = Fraction(spec.gap // 2) if outer else Fraction(spec.gap, 2)
    return divider, edge, half_gap if high_side else -half_gap


def _kept_position(spec: ZoneSpec, divider: int, position: Fraction) -> Fraction:
    """A divider end's position on the half-pixel grid that gives the same zone corners as ``position`` (so it
    survives a float round trip): the inner dividers' gap is centred on them, the outer ones lose gap // 2."""
    half = Fraction(1, 2)
    if divider in (0, spec.count):
        return Fraction(math.floor(position + half))
    return math.floor(position - Fraction(spec.gap, 2) + half) + Fraction(spec.gap, 2)


def move_divider(spec: ZoneSpec, divider: int, edge: int, position) -> ZoneSpec:
    """``spec`` with one end of divider ``divider`` (index from the box's low end) at ``position`` along the
    row; its other end keeps the corners it gives now. Raises ValueError for a divider index the spec lacks."""
    if not 0 <= divider <= spec.count or edge not in (0, 1):
        raise ValueError("No such divider")
    positions = divider_positions(spec)
    ends = [_kept_position(spec, divider, value) for value in positions[divider]]
    ends[edge] = Fraction(position)
    kept = tuple(m for m in spec.moved if m[0] != divider)
    moved = tuple(sorted(kept + ((divider, float(ends[0]), float(ends[1])),)))
    return replace(spec, moved=moved)


def translate_spec(spec: ZoneSpec, dx: int, dy: int) -> ZoneSpec:
    """``spec`` moved by whole pixels (``dx`` columns, ``dy`` rows): box, slant and moved dividers alike."""
    (x0, y0), (x1, y1) = spec.box
    along, across = (dx, dy) if spec.start in ("right", "left") else (dy, dx)
    far = None if spec.far is None else (spec.far[0] + across, spec.far[1] + across)
    moved = tuple((index, first + along, second + along) for index, first, second in spec.moved)
    return replace(spec, box=((x0 + dx, y0 + dy), (x1 + dx, y1 + dy)), far=far, moved=moved)


def _covers(points, height: int, width: int) -> bool:
    probe = _Probe(ZONE_KIND if len(points) == 4 else "rect", tuple(points))
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
