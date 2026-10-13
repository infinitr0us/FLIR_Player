# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""How the picture is turned and mirrored on screen and in image/movie exports (Qt-free).

Only the picture turns. Frames, ROIs, zones, TC pixels and every exported number stay in the
recording's own pixel coordinates, so a view turned for a camera mounted on its side measures
exactly what the unturned view measures.

Coordinates are continuous, as on the canvas: pixel i spans [i, i + 1). The picture is first
turned clockwise by ``turns`` quarter turns, then mirrored left-right (``flip_h``) and top-bottom
(``flip_v``) as it is then seen.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_END_STEPS = {"right": (1, 0), "left": (-1, 0), "top": (0, -1), "bottom": (0, 1)}  # zones.ENDS


@dataclass(frozen=True, slots=True)
class Orientation:
    turns: int = 0  # clockwise quarter turns, 0-3
    flip_h: bool = False
    flip_v: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "turns", int(self.turns) % 4)
        object.__setattr__(self, "flip_h", bool(self.flip_h))
        object.__setattr__(self, "flip_v", bool(self.flip_v))

    @property
    def identity(self) -> bool:
        return self.turns == 0 and not self.flip_h and not self.flip_v

    @property
    def swaps_axes(self) -> bool:
        """True when the picture's width is the recording's height (a quarter turn)."""
        return self.turns % 2 == 1

    def turned(self, step: int) -> "Orientation":
        """The orientation that turns the picture as now shown by ``step`` quarter turns
        (+1 clockwise, -1 anticlockwise). A turned left-right mirror is a top-bottom mirror
        of the turned picture, so an odd step swaps the two mirrors."""
        if step % 2:
            return Orientation(self.turns + step, self.flip_v, self.flip_h)
        return Orientation(self.turns + step, self.flip_h, self.flip_v)

    def display_size(self, width: int, height: int) -> tuple[int, int]:
        """(width, height) of the shown picture of a ``width`` x ``height`` recording."""
        return (height, width) if self.swaps_axes else (width, height)

    def to_display(self, x: float, y: float, width: int, height: int) -> tuple[float, float]:
        """Recording coordinates -> shown picture coordinates (``width``/``height``: the recording's)."""
        if self.turns == 1:
            u, v = height - y, x
        elif self.turns == 2:
            u, v = width - x, height - y
        elif self.turns == 3:
            u, v = y, width - x
        else:
            u, v = x, y
        shown_width, shown_height = self.display_size(width, height)
        if self.flip_h:
            u = shown_width - u
        if self.flip_v:
            v = shown_height - v
        return (u, v)

    def to_image(self, u: float, v: float, width: int, height: int) -> tuple[float, float]:
        """Shown picture coordinates -> recording coordinates (the inverse of ``to_display``)."""
        shown_width, shown_height = self.display_size(width, height)
        if self.flip_h:
            u = shown_width - u
        if self.flip_v:
            v = shown_height - v
        if self.turns == 1:
            return (v, height - u)
        if self.turns == 2:
            return (width - u, height - v)
        if self.turns == 3:
            return (width - v, u)
        return (u, v)

    def shown_end(self, end: str) -> str:
        """Where a box end in recording terms ("right", "left", "top", "bottom") appears on screen."""
        dx, dy = _END_STEPS[end]
        u0, v0 = self.to_display(1.0, 1.0, 2, 2)
        u1, v1 = self.to_display(1.0 + dx, 1.0 + dy, 2, 2)
        step = (round(u1 - u0), round(v1 - v0))
        return next(name for name, s in _END_STEPS.items() if s == step)

    def recording_end(self, shown: str) -> str:
        """The box end in recording terms that appears at the ``shown`` end on screen."""
        return next(end for end in _END_STEPS if self.shown_end(end) == shown)

    def pixel_to_image(self, column: int, row: int, width: int, height: int) -> tuple[int, int]:
        """Recording pixel (column, row) shown at picture pixel (column, row). Pixel centres map
        to pixel centres, so the result is exact."""
        x, y = self.to_image(column + 0.5, row + 0.5, width, height)
        return (min(max(int(np.floor(x)), 0), width - 1), min(max(int(np.floor(y)), 0), height - 1))

    def apply(self, picture: np.ndarray) -> np.ndarray:
        """Turn and mirror an (H, W) or (H, W, C) array as shown (contiguous; the input itself
        when nothing changes)."""
        if self.identity:
            return picture
        out = np.rot90(picture, k=-self.turns) if self.turns else picture
        if self.flip_h:
            out = out[:, ::-1]
        if self.flip_v:
            out = out[::-1]
        return np.ascontiguousarray(out)
