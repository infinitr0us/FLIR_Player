# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from matplotlib import colormaps
from PySide6.QtCore import QSettings


PALETTES: dict[str, str] = {
    "Iron": "afmhot",
    "Inferno": "inferno",
    "Magma": "magma",
    "Plasma": "plasma",
    "Grayscale": "gray",
    "Viridis": "viridis",
    "Cividis": "cividis",
    "Hot": "hot",
    "Copper": "copper",
    "Bone": "bone",
    "Rainbow": "jet",
}

# Custom gradient palettes created in the palette editor (§4.9.4.3). Names map to
# ready 256×3 LUTs; the stop lists are kept for re-editing and persistence.
CUSTOM_PALETTES: dict[str, np.ndarray] = {}
CUSTOM_PALETTE_STOPS: dict[str, list] = {}


def palette_names() -> tuple[str, ...]:
    return tuple(PALETTES) + tuple(CUSTOM_PALETTES)


def lut_from_stops(stops) -> np.ndarray:
    """Build a 256×3 uint8 LUT from (position 0..1, (r, g, b)) color stops."""
    deduped: dict[float, tuple[int, int, int]] = {}
    for position, rgb in stops:
        deduped[float(position)] = tuple(int(c) for c in rgb)
    if len(deduped) < 2:
        raise ValueError("A palette needs at least two color stops")
    positions = np.array(sorted(deduped))
    colors = np.array([deduped[p] for p in positions], dtype=np.float64)
    xs = np.linspace(0.0, 1.0, 256)
    lut = np.stack(
        [np.interp(xs, positions, colors[:, channel]) for channel in range(3)], axis=1
    )
    return np.ascontiguousarray(np.clip(lut + 0.5, 0, 255).astype(np.uint8))


def register_custom_palette(name: str, stops) -> None:
    name = str(name).strip()
    if not name:
        raise ValueError("Palette name must not be empty")
    CUSTOM_PALETTE_STOPS[name] = [
        (float(p), tuple(int(c) for c in rgb)) for p, rgb in stops
    ]
    CUSTOM_PALETTES[name] = lut_from_stops(stops)
    save_custom_palettes()


def unregister_custom_palette(name: str) -> None:
    CUSTOM_PALETTES.pop(name, None)
    CUSTOM_PALETTE_STOPS.pop(name, None)
    save_custom_palettes()


def load_custom_palettes() -> None:
    raw = QSettings("Local", "FLIR Thermal Player").value("palettes/custom", "[]")
    try:
        entries = json.loads(str(raw))
    except (TypeError, ValueError):
        return
    for entry in entries if isinstance(entries, list) else []:
        try:
            CUSTOM_PALETTE_STOPS[entry["name"]] = [
                (float(p), tuple(int(c) for c in rgb)) for p, rgb in entry["stops"]
            ]
            CUSTOM_PALETTES[entry["name"]] = lut_from_stops(entry["stops"])
        except (KeyError, TypeError, ValueError):
            continue


def save_custom_palettes() -> None:
    entries = [
        {"name": name, "stops": [[p, list(rgb)] for p, rgb in stops]}
        for name, stops in CUSTOM_PALETTE_STOPS.items()
    ]
    QSettings("Local", "FLIR Thermal Player").setValue(
        "palettes/custom", json.dumps(entries)
    )


@lru_cache(maxsize=None)
def _builtin_lut(cmap_name: str) -> np.ndarray:
    rgba = colormaps[cmap_name](np.linspace(0.0, 1.0, 256), bytes=True)
    return np.ascontiguousarray(rgba[:, :3], dtype=np.uint8)


def palette_lut(name: str, invert: bool = False) -> np.ndarray:
    lut = CUSTOM_PALETTES.get(name)
    if lut is None:
        lut = _builtin_lut(PALETTES.get(name, PALETTES["Iron"]))
    return lut[::-1] if invert else lut


def display_range(
    data: np.ndarray,
    fixed_minimum: float | None = None,
    fixed_maximum: float | None = None,
) -> tuple[float, float]:
    if fixed_minimum is not None and fixed_maximum is not None:
        low, high = float(fixed_minimum), float(fixed_maximum)
    else:
        finite = data[np.isfinite(data)]
        if not finite.size:
            return 0.0, 1.0
        low, high = float(np.min(finite)), float(np.max(finite))

    if not np.isfinite(low) or not np.isfinite(high):
        return 0.0, 1.0
    if high < low:
        low, high = high, low
    if high == low:
        padding = max(1.0, abs(high) * 0.01)
        low -= padding
        high += padding
    return low, high


def plateau_equalized_indices(normalized: np.ndarray, aggressiveness: float) -> np.ndarray:
    """Remap normalized [0, 1] values through a plateau-clipped histogram CDF.

    ResearchIR §4.3.2 "Plateau Equalization": histogram counts are clipped at a
    plateau value before equalization. ``aggressiveness`` p in [0, 1] maps to a
    clip level of ``total ** (1 - p)``: p=0 is identity (linear AGC), p=1 is
    full histogram equalization.
    """
    indices = np.clip(normalized * 255.0, 0.0, 255.0).astype(np.uint8)
    p = min(1.0, max(0.0, float(aggressiveness)))
    if p <= 0.0:
        return indices
    counts = np.bincount(indices.ravel(), minlength=256).astype(np.float64)
    total = float(counts.sum())
    if total <= 0.0:
        return indices
    clip = max(1.0, total ** (1.0 - p))
    cdf = np.cumsum(np.minimum(counts, clip))
    cdf_total = float(cdf[-1])
    if cdf_total <= 0.0:
        return indices
    mapping = np.clip(cdf / cdf_total * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
    return mapping[indices]


def render_rgb(
    data: np.ndarray,
    palette: str,
    low: float,
    high: float,
    pe: float = 0.0,
    invert: bool = False,
) -> np.ndarray:
    span = high - low
    if not np.isfinite(span) or span <= 0:
        span = 1.0
    normalized = np.nan_to_num((data.astype(np.float64, copy=False) - low) / span)
    if pe > 0.0:
        indices = plateau_equalized_indices(normalized, pe)
    else:
        indices = np.clip(normalized * 255.0, 0.0, 255.0).astype(np.uint8)
    return np.ascontiguousarray(palette_lut(palette, invert)[indices])


CLIP_COLOR = (46, 168, 255)


def apply_clip_overlay(rgb: np.ndarray, mask: np.ndarray | None) -> np.ndarray:
    """Paint clipped/invalid pixels in a fixed warning color (ResearchIR §4.6.4)."""
    if mask is None:
        return rgb
    overlaid = np.array(rgb, copy=True)
    overlaid[mask] = CLIP_COLOR
    return overlaid


SEG_BELOW_COLOR = (32, 80, 200)
SEG_ABOVE_COLOR = (224, 64, 48)


def segmentation_overlays(
    data: np.ndarray, minimum: float, maximum: float
) -> tuple[np.ndarray, np.ndarray]:
    """Boolean masks of finite pixels outside the valid range (ResearchIR §4.8.5)."""
    low = float(min(minimum, maximum))
    high = float(max(minimum, maximum))
    finite = np.isfinite(data)
    return (data < low) & finite, (data > high) & finite


def apply_segmentation_overlay(
    rgb: np.ndarray, below: np.ndarray, above: np.ndarray
) -> np.ndarray:
    """Paint out-of-range pixels blue (below) / red (above), ResearchIR-style."""
    overlaid = np.array(rgb, copy=True)
    overlaid[below] = SEG_BELOW_COLOR
    overlaid[above] = SEG_ABOVE_COLOR
    return overlaid


ISO_ABOVE_COLOR = (255, 122, 144)
ISO_BELOW_COLOR = (76, 194, 255)
ISO_INTERVAL_COLOR = (124, 227, 139)


def isotherm_mask(
    data: np.ndarray, mode: str, limit1: float, limit2: float
) -> np.ndarray | None:
    """Boolean mask of pixels inside the isotherm band (ResearchIR §4.4.1)."""
    finite = np.isfinite(data)
    if mode == "above":
        return (data >= limit1) & finite
    if mode == "below":
        return (data <= limit1) & finite
    if mode == "interval":
        low = min(limit1, limit2)
        high = max(limit1, limit2)
        return (data >= low) & (data <= high) & finite
    return None


def isotherm_color(mode: str) -> tuple[int, int, int]:
    return {
        "above": ISO_ABOVE_COLOR,
        "below": ISO_BELOW_COLOR,
        "interval": ISO_INTERVAL_COLOR,
    }.get(mode, ISO_ABOVE_COLOR)


def apply_isotherm_overlay(
    rgb: np.ndarray, data: np.ndarray, mode: str, limit1: float, limit2: float
) -> np.ndarray:
    mask = isotherm_mask(data, mode, limit1, limit2)
    if mask is None or not mask.any():
        return rgb
    overlaid = np.array(rgb, copy=True)
    overlaid[mask] = isotherm_color(mode)
    return overlaid


@dataclass(frozen=True, slots=True)
class DisplayState:
    """Everything that decides how a raw frame becomes the displayed image.

    Shared by the live view and the export pipeline so exports are WYSIWYG.
    """

    palette: str = "Iron"
    inverted: bool = False
    pe: float = 0.0  # 0 = linear AGC; >0 = plateau-equalization aggressiveness
    range_mode: str = "dynamic"  # "dynamic" | "fixed" | "roi"
    fixed_min: float = 0.0
    fixed_max: float = 1.0
    roi_minmax: tuple[float, float] | None = None  # scale from the active ROI
    segmentation: tuple[bool, float, float] = (False, 0.0, 1.0)
    isotherm: tuple[str, float, float] = ("off", 0.0, 1.0)
    clipping: bool = True
    flip_h: bool = False
    flip_v: bool = False


def display_scale(data: np.ndarray, state: DisplayState) -> tuple[float, float]:
    """Auto/fixed/ROI/segmentation-restricted display range for a frame."""
    if state.range_mode == "fixed":
        return display_range(data, state.fixed_min, state.fixed_max)
    if state.range_mode == "roi" and state.roi_minmax is not None:
        return display_range(data, state.roi_minmax[0], state.roi_minmax[1])
    seg_on, seg_min, seg_max = state.segmentation
    if seg_on:
        low = min(seg_min, seg_max)
        high = max(seg_min, seg_max)
        valid = data[np.isfinite(data) & (data >= low) & (data <= high)]
        if valid.size:
            return display_range(valid)
    return display_range(data)


def render_frame_rgb(
    data: np.ndarray,
    state: DisplayState,
    clip_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, float, float]:
    """Full display pipeline: palette/PE → overlays → flips. Returns (rgb, low, high)."""
    low, high = display_scale(data, state)
    rgb = render_rgb(data, state.palette, low, high, pe=state.pe, invert=state.inverted)
    seg_on, seg_min, seg_max = state.segmentation
    if seg_on:
        below, above = segmentation_overlays(data, seg_min, seg_max)
        rgb = apply_segmentation_overlay(rgb, below, above)
    iso_mode, iso_l1, iso_l2 = state.isotherm
    if iso_mode != "off":
        rgb = apply_isotherm_overlay(rgb, data, iso_mode, iso_l1, iso_l2)
    if state.clipping:
        rgb = apply_clip_overlay(rgb, clip_mask)
    if state.flip_h:
        rgb = np.fliplr(rgb)
    if state.flip_v:
        rgb = np.flipud(rgb)
    if state.flip_h or state.flip_v:
        rgb = np.ascontiguousarray(rgb)
    return rgb, low, high


def format_time(seconds: float) -> str:
    milliseconds = max(0, int(round(float(seconds) * 1000.0)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{minutes:02d}:{secs:02d}.{millis:03d}"


def frame_seconds(packet_timestamp, metadata, frame_index: int) -> float:
    if packet_timestamp is not None and metadata.start_time is not None:
        try:
            value = (packet_timestamp - metadata.start_time).total_seconds()
            if value >= 0:
                return min(float(value), max(metadata.duration_seconds, float(value)))
        except (OverflowError, TypeError, ValueError):
            pass
    return metadata.fallback_seconds_for_frame(frame_index)


def format_value(value: float, suffix: str) -> str:
    if suffix == "counts":
        return f"{value:.0f} counts"
    if not suffix:
        return f"{value:.0f}"
    if suffix in {"°C", "°F", "K", "°R"}:
        return f"{value:.2f} {suffix}"
    return f"{value:.3g} {suffix}".strip()
