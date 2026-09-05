# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Thread-safe export composition using PIL (ResearchIR §4.9.1.1, p. 61).

Unlike the QPainter overlays used by the live view, these helpers run anywhere —
including the decoder thread during movie/series export. All coordinates are in
raw image space; the caller passes the flip flags so they match the rendered
image exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from matplotlib import font_manager

from .models import ROI_COLORS, RoiShape, RoiStats
from .render import palette_lut, legend_colors


@dataclass(frozen=True, slots=True)
class ExportOptions:
    """Composition toggles for image/movie export (§4.9.1.1, p. 61)."""

    color_bar: bool = True
    rois: bool = True
    roi_names: bool = True
    markers: bool = False
    timestamp: bool = False
    border: bool = False


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_manager.findfont("DejaVu Sans"), size)


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _flip_point(
    x: float, y: float, width: int, height: int, flips: tuple[bool, bool]
) -> tuple[float, float]:
    if flips[0]:
        x = width - 1.0 - x
    if flips[1]:
        y = height - 1.0 - y
    return (x, y)


def compose_frame(
    rgb: np.ndarray,
    options: ExportOptions,
    *,
    palette: str = "Iron",
    inverted: bool = False,
    scale: tuple[float, float] = (0.0, 1.0),
    suffix: str = "",
    rois: tuple[RoiShape, ...] = (),
    roi_stats: tuple[RoiStats, ...] = (),
    min_position: tuple[int, int] | None = None,
    max_position: tuple[int, int] | None = None,
    label: str = "",
    flips: tuple[bool, bool] = (False, False),
    mapping=None,
    segmentation=(False, 0.0, 1.0),
    isotherm=("off", 0.0, 1.0),
) -> np.ndarray:
    """Compose the final export image: frame + overlays (+ optional color bar)."""
    image = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size

    if options.rois and rois:
        _draw_rois(draw, rois, width, height, flips, options.roi_names)
    if options.markers:
        _draw_markers(draw, roi_stats, min_position, max_position, width, height, flips)
    if options.timestamp and label:
        _draw_label(draw, label, (10, height - 10), anchor="ls", size=14)
    if options.border:
        draw.rectangle((0, 0, width - 1, height - 1), outline=(73, 88, 108), width=1)
    if options.color_bar:
        image = _append_color_bar(image, palette, inverted, scale, suffix,
                                  mapping, segmentation, isotherm)

    result = np.asarray(image, dtype=np.uint8)
    return np.ascontiguousarray(result)


def _draw_rois(
    draw: ImageDraw.ImageDraw,
    rois: tuple[RoiShape, ...],
    width: int,
    height: int,
    flips: tuple[bool, bool],
    with_names: bool,
) -> None:
    for shape in rois:
        color = _hex_rgb(ROI_COLORS[shape.id % len(ROI_COLORS)])
        if shape.kind == "cursor" and len(shape.points) == 1:
            cx, cy = _flip_point(*shape.points[0], width, height, flips)
            draw.line((cx - 8, cy, cx + 8, cy), fill=color, width=2)
            draw.line((cx, cy - 8, cx, cy + 8), fill=color, width=2)
            draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), outline=color, width=1)
            anchor = (cx - 10, cy - 12)
        elif len(shape.points) == 2:
            first = _flip_point(*shape.points[0], width, height, flips)
            second = _flip_point(*shape.points[1], width, height, flips)
            if shape.kind == "line":
                draw.line((*first, *second), fill=color, width=2)
            else:
                box = (
                    min(first[0], second[0]),
                    min(first[1], second[1]),
                    max(first[0], second[0]),
                    max(first[1], second[1]),
                )
                if shape.kind == "rect":
                    draw.rectangle(box, outline=color, width=2)
                elif shape.kind == "ellipse":
                    draw.ellipse(box, outline=color, width=2)
            anchor = (min(first[0], second[0]), min(first[1], second[1]) - 4)
        else:
            continue
        if with_names:
            _draw_label(draw, shape.name, anchor, anchor="ls", size=12, outline=color)


def _draw_markers(
    draw: ImageDraw.ImageDraw,
    roi_stats: tuple[RoiStats, ...],
    min_position,
    max_position,
    width: int,
    height: int,
    flips: tuple[bool, bool],
) -> None:
    def cross(position, color) -> None:
        if position is None:
            return
        x, y = _flip_point(float(position[0]), float(position[1]), width, height, flips)
        draw.line((x - 6, y, x + 6, y), fill=(7, 9, 10), width=4)
        draw.line((x, y - 6, x, y + 6), fill=(7, 9, 10), width=4)
        draw.line((x - 6, y, x + 6, y), fill=color, width=2)
        draw.line((x, y - 6, x, y + 6), fill=color, width=2)

    cross(min_position, (76, 194, 255))
    cross(max_position, (255, 122, 144))
    for stats in roi_stats:
        color = _hex_rgb(ROI_COLORS[stats.id % len(ROI_COLORS)])
        cross(stats.min_position, color)
        cross(stats.max_position, color)


def _draw_label(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[float, float],
    *,
    anchor: str,
    size: int,
    outline: tuple[int, int, int] = (51, 62, 77),
) -> None:
    font = _font(size)
    left, top, right, bottom = draw.textbbox(xy, text, font=font, anchor=anchor)
    draw.rectangle(
        (left - 4, top - 2, right + 4, bottom + 2), fill=(9, 13, 15, 200), outline=outline
    )
    draw.text(xy, text, font=font, fill=(244, 246, 247), anchor=anchor)


def _append_color_bar(
    image: Image.Image,
    palette: str,
    inverted: bool,
    scale: tuple[float, float],
    suffix: str,
    mapping=None,
    segmentation=(False, 0.0, 1.0),
    isotherm=("off", 0.0, 1.0),
) -> Image.Image:
    width, height = image.size
    strip_width = 104
    bar_left = width + 10  # inside the appended strip, not over the image
    bar_width = 22
    combined = Image.new("RGB", (width + strip_width, height), (6, 8, 11))
    combined.paste(image, (0, 0))

    lut = legend_colors(palette, inverted, mapping, scale, segmentation, isotherm)[::-1]
    gradient = Image.fromarray(np.ascontiguousarray(lut.reshape(256, 1, 3)), mode="RGB")
    gradient = gradient.resize((bar_width, max(1, height - 32)), Image.Resampling.NEAREST)
    combined.paste(gradient, (bar_left, 16))

    draw = ImageDraw.Draw(combined)
    draw.rectangle(
        (bar_left, 16, bar_left + bar_width, height - 17), outline=(51, 62, 77), width=1
    )
    low, high = scale
    font = _font(11)
    for step in range(7):
        fraction = step / 6.0
        y = (height - 17) - int(round(fraction * (height - 33)))
        value = low + fraction * (high - low)
        draw.line((bar_left + bar_width + 2, y, bar_left + bar_width + 6, y), fill=(73, 88, 108))
        text = _format_tick(value)
        if step == 6 and suffix:
            text = f"{text} {suffix}"
        draw.text(
            (bar_left + bar_width + 10, y),
            text,
            font=font,
            fill=(151, 161, 175),
            anchor="lm",
        )
    return combined


def _format_tick(value: float) -> str:
    if abs(value) >= 100 or float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.2f}"
