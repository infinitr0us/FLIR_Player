# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Superframing presets joined into one range, and frames the camera's shutter covered (Qt-free).

An A8303sc cycles through presets (temperature ranges), one frame each, 17 ms apart, then waits for the next
cycle. Presets joined measure each pixel (in the player) or each area (in the workbook) with the lowest preset
whose range holds it, so one curve runs from 250 to 1,200 °C. A preset joins only if it agrees with the presets
below it: on the 1006 test a, preset 3 read every cold frame as 1,205 °C, about 540 K hotter than preset 2 where
both measure, because of a count offset FLIR's calibration does not remove (local/notes/2026-10-23-a8303-flames).

A joined choice is a tuple of presets, lowest range first, wherever one preset is an int.

The camera can also put its internal shutter (flag) in front of the sensor for a minute or more (about 70 s every
30 minutes on the A8303 fire tests); its frame headers then say ``FlagInFOV = T`` and the frames show no scene.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

import numpy as np

try:
    import fnv
except ModuleNotFoundError:
    fnv = None  # type: ignore[assignment]

from .jobs import JobCancelled

Preset = int | tuple[int, ...] | None  # one preset, presets joined (lowest range first), or every frame

KELVIN = 273.15
LEAVE_OUT_SHARE = 0.5  # share of contradicting pixels above which a preset is left out
MIN_PIXELS = 1000  # fewer informative pixels: the pair is not judged (the preset joins; it adds nothing wrong)
MARGIN_K = 100.0  # flames change by tens of kelvin within the 17 ms between the presets of a cycle
# where both presets read inside their ranges, their median difference: 18 to 112 K between healthy presets on the
# fire tests (flicker and the ranges' asymmetry), +649 K for 1006a's preset 3
DIFFERENCE_K = 300.0
_EDGES = np.arange(-3000.0, 3005.0, 5.0)  # bins of those differences (K)
CYCLES = 24  # cycles read by the check: the hottest half by the lowest preset's counts, the rest spread out
MAX_CYCLE_GAP_S = 0.5  # frames of one cycle are 17 ms apart; the next cycle comes about 1 s later


def base_preset(preset: Preset) -> int | None:
    """The preset that times and steps a choice: the lowest of presets joined, or the one preset."""
    if isinstance(preset, (tuple, list)):
        return int(preset[0]) if preset else None
    return None if preset is None else int(preset)


def is_joined(preset: Preset) -> bool:
    return isinstance(preset, (tuple, list)) and len(preset) > 1


def as_preset(value: Any) -> Preset:
    """A stored choice (JSON gives lists) back as a Preset."""
    if isinstance(value, (tuple, list)):
        presets = tuple(int(v) for v in value)
        return presets if len(presets) > 1 else (presets[0] if presets else None)
    return None if value is None else int(value)


def describe(preset: Preset, ranges: Mapping[int, tuple[float, float] | None] | None = None) -> str:
    """"Preset 2", or "Presets 1 and 2 joined (250 to 1200 °C)" when ``ranges`` (K) are known."""
    if preset is None:
        return "every frame"
    if not is_joined(preset):
        return f"Preset {base_preset(preset)}"
    presets = list(preset)
    names = ", ".join(str(p) for p in presets[:-1]) + f" and {presets[-1]}"
    span = ""
    if ranges and all(ranges.get(p) for p in presets):
        span = f" ({ranges[presets[0]][0] - KELVIN:.0f} to {ranges[presets[-1]][1] - KELVIN:.0f} °C)"
    return f"Presets {names} joined{span}"


def shutter_in_view(im: Any) -> bool:
    """True when the current frame's header says the camera's shutter covered the sensor (``FlagInFOV``)."""
    try:
        for entry in im.frame_info:
            if str(entry["name"]) == "FlagInFOV":
                return str(entry["value"]).strip().upper() in ("T", "TRUE", "1")
    except (TypeError, KeyError, AttributeError, IndexError):
        return False
    return False


def preset_of(im: Any, index: int) -> int:
    """The preset frame ``index`` belongs to (the SDK's header index when it has one)."""
    lookup = getattr(im, "get_preset", None)
    if lookup is not None:
        return int(lookup(int(index)))
    im.get_frame(int(index))
    return int(next(str(e["value"]) for e in im.frame_info if str(e["name"]) == "Preset"))


def by_range(presets: Sequence[int], ranges: Mapping[int, tuple[float, float] | None]) -> tuple[int, ...]:
    """``presets`` lowest calibrated range first (uncalibrated last)."""
    return tuple(sorted(presets, key=lambda p: ranges[p][0] if ranges.get(p) else float("inf")))


@dataclass(frozen=True, slots=True)
class PairCheck:
    lower: int
    upper: int
    informative: int  # pixels that tell: the upper preset in range, or the lower one above its range
    contradicting: int  # of those, pixels the other preset reads well colder (MARGIN_K) than that range allows
    both: int  # pixels inside both ranges
    difference_k: float | None  # median upper − lower over those (None: fewer than MIN_PIXELS)

    @property
    def share(self) -> float | None:
        return self.contradicting / self.informative if self.informative >= MIN_PIXELS else None

    @property
    def offset(self) -> bool:
        """Where both read inside their ranges, they differ by more than flames change within a cycle."""
        return self.difference_k is not None and abs(self.difference_k) > DIFFERENCE_K

    @property
    def agrees(self) -> bool:
        return not self.offset and (self.share is None or self.share <= LEAVE_OUT_SHARE)


@dataclass(frozen=True, slots=True)
class PresetCheck:
    """Which presets join: ``kept`` lowest range first; ``pairs`` each preset judged against the highest kept."""

    kept: tuple[int, ...]
    left_out: tuple[int, ...]
    pairs: tuple[PairCheck, ...]
    cycles: int  # cycles compared

    def notes(self) -> list[str]:
        """One plain line per preset judged, for the Source sheet and the player."""
        lines = []
        for pair in self.pairs:
            diff = (f"where both are inside their ranges it reads {pair.difference_k:+.0f} K against preset "
                    f"{pair.lower} (median of {pair.both:,} pixels)" if pair.difference_k is not None else "")
            if pair.agrees and pair.share is None and pair.difference_k is None:
                lines.append(f"Preset {pair.upper}: too few pixels in its range to compare with preset {pair.lower} "
                             f"({pair.informative:,}); joined.")
            elif pair.agrees:
                text = (f"in {pair.share:.1%} of {pair.informative:,} pixels their frames of one cycle differ by more "
                        f"than {MARGIN_K:.0f} K the impossible way" if pair.share is not None else "")
                lines.append(f"Preset {pair.upper} agrees with preset {pair.lower}: "
                             + "; ".join(part for part in (text, diff) if part) + " (flames flicker).")
            else:
                parts = []
                if pair.share is not None and pair.share > LEAVE_OUT_SHARE:
                    parts.append(f"of {pair.informative:,} pixels it reads inside its range, {pair.share:.0%} read more "
                                 f"than {MARGIN_K:.0f} K colder in preset {pair.lower}'s frame of the same cycle")
                if diff:
                    parts.append(diff + (f", more than {DIFFERENCE_K:.0f} K" if pair.offset else ""))
                lines.append(f"Preset {pair.upper} left out: " + "; ".join(parts) + ". Its frames carry an offset "
                             "(FLIR's File SDK reads them the same way).")
        return lines


class _Tally:
    """One pair's evidence, summed cycle by cycle (no frames kept)."""

    def __init__(self) -> None:
        self.informative = self.contradicting = self.both = 0
        self.histogram = np.zeros(_EDGES.size - 1, dtype=np.int64)

    def add(self, low: tuple[np.ndarray, np.ndarray], high: tuple[np.ndarray, np.ndarray],
            range_low: tuple[float, float], range_high: tuple[float, float]) -> None:
        (tl, sl), (tu, su) = low, high
        lo_l, hi_l = range_low
        lo_u, hi_u = range_high
        in_l = (sl == 0) & (tl >= lo_l) & (tl <= hi_l)
        in_u = (su == 0) & (tu >= lo_u) & (tu <= hi_u)
        above_l = ((sl & 4) != 0) | (tl > hi_l)
        self.informative += int(np.count_nonzero(in_u)) + int(np.count_nonzero(above_l & ~in_u))
        self.contradicting += (int(np.count_nonzero(in_u & ~above_l & (tl < lo_u - MARGIN_K)))
                               + int(np.count_nonzero(above_l & ~in_u & (tu < hi_l - MARGIN_K))))
        both = in_l & in_u
        self.both += int(np.count_nonzero(both))
        if both.any():
            d = np.clip((tu - tl)[both], _EDGES[0], _EDGES[-1] - 1e-9)
            self.histogram += np.histogram(d, bins=_EDGES)[0]

    def median(self) -> float | None:
        if self.both < MIN_PIXELS:
            return None
        k = int(np.searchsorted(np.cumsum(self.histogram), self.both / 2.0))
        return float((_EDGES[k] + _EDGES[k + 1]) / 2.0)


def _frame_time(im: Any) -> datetime | None:
    stamp = getattr(getattr(im, "frame_info", None), "time", None)
    return stamp if isinstance(stamp, datetime) else None


def cycle_mate(im: Any, frame: int, preset: int, presets: int, n: int | None = None) -> int | None:
    """The frame of ``preset`` in the same cycle as ``frame``, or None: the next one within ``presets`` frames
    (the recording's presets per cycle; a cycle runs from the lowest preset up), else the one before. The caller
    checks the clock."""
    n = int(im.num_frames) if n is None else n
    for candidate in [*range(frame + 1, frame + presets), *range(frame - 1, frame - presets, -1)]:
        if 0 <= candidate < n and preset_of(im, candidate) == preset:
            return candidate
    return None


def check_presets(im: Any, presets: Sequence[int], ranges: Mapping[int, tuple[float, float] | None], *,
                  cycles: int = CYCLES, abort: Callable[[], bool] | None = None) -> PresetCheck:
    """Compare each preset with the presets below it on ``cycles`` cycles of the open recording.

    ``ranges`` holds every preset of the recording (its cycle width). Reads the SDK's temperatures (kelvin) and
    per-pixel status at the recording's own object parameters, so the verdict does not depend on the player's
    settings; the caller restores the handle's state (``calibration.preserved_state``). Frames the shutter
    covered are skipped. A preset is left out when it disagrees with the highest preset kept below it:

    * contradictions: the upper preset reads a pixel inside its range while the lower one, 17 ms away and not
      above its own range, reads it more than MARGIN_K colder than the upper range's floor; or the lower preset
      reads it above its range while the upper one reads it more than MARGIN_K under the lower range's top. A
      flame cannot cool by hundreds of kelvin and back within 17 ms: most pixels contradicting means an offset;
    * or, where both read inside their ranges, their median difference exceeds DIFFERENCE_K (an offset in a hot
      scene, where the lower preset holds most pixels and nothing contradicts).
    """
    from .calibration import read_array, set_unit_safely

    order = by_range(presets, ranges)
    if len(order) < 2:
        return PresetCheck(tuple(order), (), (), 0)
    n = int(im.num_frames)
    width = max(len(ranges), len(order))  # frames per cycle: a joined choice may skip a preset of the cycle
    base = order[0]
    reset = getattr(im, "reset_object_parameters", None)
    if reset is not None:
        reset()  # the recording's own parameters
    # base-preset frames spread over the recording, then the hottest of a wider probe
    probe = []
    for start in np.linspace(0, n - 1, 4 * cycles).astype(int):
        for i in range(int(start), min(n, int(start) + 2 * width)):
            if preset_of(im, i) == base:
                probe.append(i)
                break
    probe = sorted(set(probe))
    set_unit_safely(im, fnv.Unit.COUNTS)
    heat = []
    for i in probe:
        if abort is not None and abort():
            raise JobCancelled("Cancelled")
        heat.append(float(np.percentile(read_array(im, i), 99.5)))
    hottest = [probe[k] for k in np.argsort(heat)[::-1][:cycles // 2]]
    spread = [probe[k] for k in np.linspace(0, len(probe) - 1, cycles - len(hottest)).astype(int)]
    chosen = sorted(set(hottest) | set(spread))
    set_unit_safely(im, fnv.Unit.TEMPERATURE_FACTORY, fnv.TempType.KELVIN)
    tallies = {(a, b): _Tally() for i, a in enumerate(order) for b in order[i + 1:]}
    limits = {p: ranges.get(p) or (-np.inf, np.inf) for p in order}
    compared = 0
    for frame in chosen:
        if abort is not None and abort():
            raise JobCancelled("Cancelled")
        kelvin = read_array(im, frame).astype(np.float64)
        if shutter_in_view(im):
            continue
        when = _frame_time(im)
        cycle = {base: (kelvin, np.array(im.status, copy=True).reshape(kelvin.shape))}
        for p in order[1:]:
            mate = cycle_mate(im, frame, p, width, n)
            if mate is None:
                continue
            values = read_array(im, mate).astype(np.float64)
            other = _frame_time(im)
            if shutter_in_view(im) or (when is not None and other is not None
                                       and abs((other - when).total_seconds()) > MAX_CYCLE_GAP_S):
                continue
            cycle[p] = (values, np.array(im.status, copy=True).reshape(values.shape))
        for (a, b), tally in tallies.items():
            if a in cycle and b in cycle:
                tally.add(cycle[a], cycle[b], limits[a], limits[b])
        compared += 1
    kept, left_out, pairs = [base], [], []
    for upper in order[1:]:
        lower = kept[-1]
        tally = tallies[(lower, upper)]
        pair = PairCheck(lower, upper, tally.informative, tally.contradicting, tally.both, tally.median())
        pairs.append(pair)
        (kept if pair.agrees else left_out).append(upper)
    return PresetCheck(tuple(kept), tuple(left_out), tuple(pairs), compared)
