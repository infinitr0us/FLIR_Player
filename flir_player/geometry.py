# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Canonical ROI coverage in integer image pixels.

ROI points are continuous image coordinates in which pixel ``i`` spans
``[i, i + 1)``, the same convention the canvas draws with. Box and ellipse
corners round to pixel boundaries, so boxes are half open and contain the
pixels whose centres lie inside the outline; ellipses test those pixel centres.
Spots and line endpoints select the pixel that contains them (floor). Lines
sample each major-axis pixel once, nearest minor coordinate with half-pixel
ties toward the smaller coordinate. Reversing a line reverses its profile
order without changing its pixel set. Corners clamp to the image edges and
points to its pixels.

Four-sided ROIs ("quad", the cell zones) round their corners to pixel
boundaries like a box and contain the pixels whose centres lie inside, tested
in exact integers. A centre on an edge belongs to the side the edge's
direction gives it (a top-left fill rule), so zones sharing an edge never
share a pixel, and an upright quad covers exactly the pixels of the box with
its corners. A quad whose sides cross covers nothing; the image clips it.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from threading import local

import numpy as np

_local = local()
_CACHE_BYTES = 16 * 1024 * 1024


def roi_coordinates(shape, height: int, width: int):
    key = (shape.kind, shape.points, height, width)
    cache = getattr(_local, "cache", None)
    if cache is None:
        cache = _local.cache = OrderedDict()
        _local.bytes = 0
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    result = _coordinates(shape.kind, shape.points, height, width)
    size = sum(a.nbytes for a in result)
    if size <= _CACHE_BYTES:
        while cache and (_local.bytes + size > _CACHE_BYTES or len(cache) >= 128):
            _, old = cache.popitem(last=False)
            _local.bytes -= sum(a.nbytes for a in old)
        for array in result:
            array.flags.writeable = False
        cache[key] = result
        _local.bytes += size
    return result


def pixel_index(kind: str, x: float, y: float, width: int, height: int) -> tuple[int, int]:
    """Integer position of one ROI point: the containing pixel (0..size-1)
    for spots and line endpoints, the nearest pixel boundary (0..size) for
    box/ellipse corners, so a box can reach the last row and column."""
    if kind in ("cursor", "line"):
        return (max(0, min(math.floor(x), width - 1)), max(0, min(math.floor(y), height - 1)))
    return (max(0, min(int(round(x)), width)), max(0, min(int(round(y)), height)))


def area_extent(kind: str, points, height: int, width: int):
    """(x_min, x_max, y_min, y_max, pixel count) of a box, ellipse or quad ROI, or
    None when it covers no pixel.

    Exactly the pixels ``roi_coordinates`` returns, found in O(rows) without
    building them, for readouts that follow the pointer while a large ellipse
    is drawn (a full-frame ellipse takes ~30 ms through ``roi_coordinates``).
    """
    if kind == "quad":  # zones are small; the exact pixels are cheap
        ys, xs = _quad_coordinates(points, height, width) if width >= 1 and height >= 1 else ((), ())
        if not len(ys):
            return None
        return (int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max()), int(len(ys)))
    if width < 1 or height < 1 or len(points) != 2:
        return None
    (x0, y0), (x1, y1) = (pixel_index(kind, x, y, width, height) for x, y in points)
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    if right == left or bottom == top:
        return None
    if kind == "rect":
        return (left, right - 1, top, bottom - 1, (right - left) * (bottom - top))
    rows = np.arange(top, bottom, dtype=np.int64)
    row_term = ((2 * rows + 1 - top - bottom) / (bottom - top)) ** 2

    def inside(xs):  # the test of _coordinates, evaluated in the same float64 steps
        return ((2 * xs + 1 - left - right) / (right - left)) ** 2 + row_term[:, None] <= 1.0

    # The test is monotonic in |2x + 1 - left - right|, so each row is one
    # interval symmetric about the centre. Estimate its right end, then settle
    # it with the exact test over a window around the estimate.
    first = (left + right) // 2  # innermost column right of the centre
    estimate = np.floor((right - left) * np.sqrt(np.clip(1.0 - row_term, 0.0, None)) / 2.0
                        + (left + right - 1) / 2.0).astype(np.int64)
    window = np.clip(estimate[:, None] + np.arange(-3, 4), first, right - 1)
    ok = inside(window)
    has = ok.any(axis=1)
    if not has.any():
        return None
    x_max = np.where(ok, window, first - 1).max(axis=1)[has]
    x_min = left + right - 1 - x_max
    covered = rows[has]
    return (int(x_min.min()), int(x_max.max()), int(covered[0]), int(covered[-1]),
            int((x_max - x_min + 1).sum()))


def quad_corners(points) -> tuple[tuple[int, int], ...] | None:
    """A quad's corners on pixel boundaries (rounded as box corners, not clamped), or None when it is
    not four finite points."""
    if len(points) != 4:
        return None
    try:
        values = [(float(x), float(y)) for x, y in points]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for point in values for v in point):
        return None
    return tuple((int(round(x)), int(round(y))) for x, y in values)


def quad_label_width(corners) -> float:
    """Room for a quad's label as drawn (``corners`` in screen coordinates): the shorter of the two opposite sides
    that run more across the screen, measured across it, so a leaning zone's label does not reach its neighbour."""
    p = [(float(x), float(y)) for x, y in corners]
    along = (abs(p[1][0] - p[0][0]), abs(p[2][0] - p[3][0]))
    across = (abs(p[3][0] - p[0][0]), abs(p[2][0] - p[1][0]))
    return min(along) if sum(along) >= sum(across) else min(across)


def _cross(o, a, b) -> int:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segments_cross(p, q, r, s) -> bool:
    """True when segments pq and rs share a point (touching counts)."""
    d1, d2, d3, d4 = _cross(r, s, p), _cross(r, s, q), _cross(p, q, r), _cross(p, q, s)
    if ((d1 > 0) != (d2 > 0) and d1 != 0 and d2 != 0) and ((d3 > 0) != (d4 > 0) and d3 != 0 and d4 != 0):
        return True

    def on(a, b, c):  # c on segment ab, given collinear
        return min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])

    return ((d1 == 0 and on(r, s, p)) or (d2 == 0 and on(r, s, q)) or (d3 == 0 and on(p, q, r))
            or (d4 == 0 and on(p, q, s)))


def quad_triangles(corners) -> list[tuple[tuple[int, int], ...]]:
    """Two triangles making up a simple quad (corners in order), each turning the way the quad turns; [] for
    a quad without area or whose sides cross. A concave quad is cut along the diagonal from its reflex
    corner, which lies inside it."""
    p = list(corners)
    twice_area = sum(p[k][0] * p[(k + 1) % 4][1] - p[(k + 1) % 4][0] * p[k][1] for k in range(4))
    if twice_area == 0 or _segments_cross(p[0], p[1], p[2], p[3]) or _segments_cross(p[1], p[2], p[3], p[0]):
        return []
    sign = 1 if twice_area > 0 else -1
    turns = [_cross(p[k - 1], p[k], p[(k + 1) % 4]) * sign for k in range(4)]
    r = next((k for k in range(4) if turns[k] < 0), 0)  # the reflex corner, if any
    a, b, c, d = (p[(r + k) % 4] for k in range(4))
    return [t for t in ((a, b, c), (a, c, d)) if _cross(*t) != 0]


def _quad_coordinates(points, height: int, width: int):
    empty = (np.empty(0, dtype=np.int32), np.empty(0, dtype=np.int32))
    corners = quad_corners(points)
    triangles = quad_triangles(corners) if corners is not None else []
    if not triangles:
        return empty
    xs_all = [x for x, _y in corners]
    ys_all = [y for _x, y in corners]
    left, right = max(0, min(xs_all)), min(width, max(xs_all))
    top, bottom = max(0, min(ys_all)), min(height, max(ys_all))
    if right <= left or bottom <= top:
        return empty
    yy, xx = np.mgrid[top:bottom, left:right].astype(np.int64)
    cx, cy = 2 * xx + 1, 2 * yy + 1  # pixel centres, doubled to stay whole numbers
    inside = np.zeros(cx.shape, dtype=bool)
    for triangle in triangles:
        if _cross(*triangle) < 0:
            triangle = triangle[::-1]
        mask = np.ones(cx.shape, dtype=bool)
        for k in range(3):
            (ax, ay), (bx, by) = triangle[k], triangle[(k + 1) % 3]
            dx, dy = bx - ax, by - ay
            value = dx * (cy - 2 * ay) - dy * (cx - 2 * ax)
            # a centre on the edge goes to one side only: the triangle whose edge runs this way takes it
            mask &= (value > 0) | ((value == 0) & ((dy < 0) | ((dy == 0) & (dx > 0))))
        inside |= mask
    rows, cols = np.nonzero(inside)
    return (rows + top).astype(np.int32), (cols + left).astype(np.int32)


def _coordinates(kind, points, height, width):
    empty = (np.empty(0, dtype=np.int32), np.empty(0, dtype=np.int32))
    if width < 1 or height < 1:
        return empty
    if kind == "quad":
        return _quad_coordinates(points, height, width)
    points = tuple(pixel_index(kind, x, y, width, height) for x, y in points)
    if kind == "cursor" and len(points) == 1:
        return np.array([points[0][1]], np.int32), np.array([points[0][0]], np.int32)
    if len(points) != 2:
        return empty
    (x0, y0), (x1, y1) = points
    if kind == "line":
        steep = abs(y1 - y0) > abs(x1 - x0)
        if steep:
            x0, y0, x1, y1 = y0, x0, y1, x1
        reverse = x0 > x1
        if reverse:
            x0, y0, x1, y1 = x1, y1, x0, y0
        major = x1 - x0
        xs = np.arange(x0, x1 + 1, dtype=np.int32)
        ys = (y0 + (2 * (xs.astype(np.int64) - x0) * (y1 - y0) + major - 1)
              // (2 * major)).astype(np.int32) if major else np.array([y0], np.int32)
        if reverse:
            xs, ys = xs[::-1], ys[::-1]
        return (xs, ys) if steep else (ys, xs)
    if kind in {"rect", "ellipse"}:
        left, right = sorted((x0, x1))
        top, bottom = sorted((y0, y1))
        if right == left or bottom == top:
            return empty
        yy, xx = np.mgrid[top:bottom, left:right].astype(np.int32)
        if kind == "ellipse":
            inside = ((2 * xx + 1 - left - right) / (right - left)) ** 2 + (
                (2 * yy + 1 - top - bottom) / (bottom - top)) ** 2 <= 1.0
            return yy[inside], xx[inside]
        return yy.ravel(), xx.ravel()
    return empty
