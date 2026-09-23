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


def _coordinates(kind, points, height, width):
    empty = (np.empty(0, dtype=np.int32), np.empty(0, dtype=np.int32))
    if width < 1 or height < 1:
        return empty
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
