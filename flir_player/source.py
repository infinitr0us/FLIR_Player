# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # The FLIR File SDK is a proprietary, user-supplied optional dependency.
    import fnv
    import fnv.file
    import fnv.reduce

    _SDK_AVAILABLE = True
except ModuleNotFoundError:  # Let this module import without the SDK present.
    fnv = None  # type: ignore[assignment]
    _SDK_AVAILABLE = False
import numpy as np

from .calibration import set_unit_safely
from .fff import describe_parameters, saved_object_parameters
from .sdktime import TimestampRepair, preset_frame_rate, recording_rate
from .models import (
    CadenceInfo,
    FramePacket,
    FrameRate,
    PresetRange,
    RoiShape,
    RoiStats,
    UnitOption,
    VideoMetadata,
)
from .processing import (
    ProcessingState,
    TemporalBuffer,
    apply_file_operation,
    apply_point_filter,
    apply_spatial_filter,
    roi_stats_app,
)


@dataclass(frozen=True, slots=True)
class _UnitSpec:
    option: UnitOption
    sdk_unit: Any
    temperature_type: Any | None = None


def _build_unit_specs() -> tuple[_UnitSpec, ...]:
    if not _SDK_AVAILABLE:
        return ()
    return (
    _UnitSpec(UnitOption("counts", "Counts", "counts"), fnv.Unit.COUNTS),
    _UnitSpec(UnitOption("object_signal", "Object Signal", ""), fnv.Unit.OBJECT_SIGNAL),
    _UnitSpec(
        UnitOption("temperature_factory_c", "Temperature (Factory, °C)", "°C"),
        fnv.Unit.TEMPERATURE_FACTORY,
        fnv.TempType.CELSIUS,
    ),
    _UnitSpec(
        UnitOption("temperature_factory_f", "Temperature (Factory, °F)", "°F"),
        fnv.Unit.TEMPERATURE_FACTORY,
        fnv.TempType.FAHRENHEIT,
    ),
    _UnitSpec(
        UnitOption("temperature_factory_k", "Temperature (Factory, K)", "K"),
        fnv.Unit.TEMPERATURE_FACTORY,
        fnv.TempType.KELVIN,
    ),
    _UnitSpec(
        UnitOption("temperature_factory_r", "Temperature (Factory, °R)", "°R"),
        fnv.Unit.TEMPERATURE_FACTORY,
        fnv.TempType.RANKINE,
    ),
    _UnitSpec(
        UnitOption("temperature_user_c", "Temperature (User, °C)", "°C"),
        fnv.Unit.TEMPERATURE_USER,
        fnv.TempType.CELSIUS,
    ),
    _UnitSpec(
        UnitOption("temperature_user_f", "Temperature (User, °F)", "°F"),
        fnv.Unit.TEMPERATURE_USER,
        fnv.TempType.FAHRENHEIT,
    ),
    _UnitSpec(
        UnitOption("temperature_user_k", "Temperature (User, K)", "K"),
        fnv.Unit.TEMPERATURE_USER,
        fnv.TempType.KELVIN,
    ),
    _UnitSpec(
        UnitOption("temperature_user_r", "Temperature (User, °R)", "°R"),
        fnv.Unit.TEMPERATURE_USER,
        fnv.TempType.RANKINE,
    ),
    _UnitSpec(
        UnitOption("radiance_factory", "Radiance (Factory)", "radiance"),
        fnv.Unit.RADIANCE_FACTORY,
    ),
    _UnitSpec(
        UnitOption("radiance_user", "Radiance (User)", "radiance"),
        fnv.Unit.RADIANCE_USER,
    ),
)

UNIT_SPECS: tuple[_UnitSpec, ...] = _build_unit_specs()
_SPECS_BY_KEY = {spec.option.key: spec for spec in UNIT_SPECS}


def _position(point: Any) -> tuple[int, int] | None:
    try:
        return (int(point["x"]), int(point["y"]))
    except (KeyError, TypeError, ValueError):
        return None


def _pretty_value(value: Any) -> str:
    """Compact rendering for SDK values that would otherwise repr() as raw blobs."""
    if isinstance(value, dict):
        return ", ".join(f"{key}: {_pretty_value(item)}" for key, item in value.items())
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_pretty_value(item) for item in value) + "]"
    return str(value)


_base_frame_rate = preset_frame_rate  # kept for callers of the old name


def _cadence_info(im: Any, span_seconds: float, stored_frames: int) -> CadenceInfo | None:
    """Compare stored frames against the capture grid the preset rate implies.

    Derived from metadata already read at open time, so it costs no extra frame
    decodes — scanning every frame's timestamp would mean a full decode pass
    (~100 s for the 19302-frame CSQ sample).
    """
    base_fps = preset_frame_rate(im)
    if base_fps <= 0 or span_seconds <= 0 or stored_frames < 2:
        return None
    expected = int(round(span_seconds * base_fps)) + 1
    if expected < stored_frames:
        # The span implies fewer slots than were stored: the preset rate does
        # not describe this recording, so there is nothing meaningful to report.
        return None
    return CadenceInfo(
        base_fps=base_fps, expected_frames=expected, stored_frames=stored_frames
    )


def _cadence_rows(cadence: CadenceInfo | None, average_fps: float,
                  rate: FrameRate | None = None) -> tuple[tuple[str, str], ...]:
    """Source-panel rows describing the recording's frame rate and evenness."""
    rows: list[tuple[str, str]] = []
    if rate is not None and rate.corrected:
        rows.append(("Frame rate", f"{rate.fps:.2f} fps (camera rate)"))
        rows.append(("Timestamps", f"Run {rate.clock_error * 100:.1f} % slow ({rate.clock_fps:.2f} fps "
                                   f"implied); times use the frame number"))
        return tuple(rows)
    if average_fps > 0:
        if cadence is not None and not cadence.is_even:
            rows.append(
                (
                    "Frame rate",
                    f"{average_fps:.2f} fps average · {cadence.base_fps:.2f} Hz camera rate",
                )
            )
        else:
            rows.append(("Frame rate", f"{average_fps:.2f} fps"))
    if rate is not None and rate.suggested_fps > 0:
        rows.append(("Timestamps", f"Imply {rate.clock_fps:.2f} fps, "
                                   f"{(rate.clock_fps / rate.suggested_fps - 1) * 100:.1f} % above "
                                   f"{rate.suggested_fps:g} Hz: if the camera ran at {rate.suggested_fps:g} Hz, "
                                   "its clock runs slow (times follow the timestamps)"))
    if cadence is None:
        return tuple(rows)
    if cadence.is_even:
        rows.append(("Frame cadence", "Even"))
    else:
        rows.append(
            (
                "Frame cadence",
                f"Uneven — {cadence.stored_frames} of {cadence.expected_frames} frames stored "
                f"({cadence.kept_fraction * 100:.1f} %), {cadence.missing_frames} missing",
            )
        )
    return tuple(rows)


def _superframing_presets(im: Any) -> tuple[PresetRange, ...]:
    """The presets a superframing recording cycles through (empty for one preset)."""
    info = getattr(im, "source_info", None)
    presets = []
    for index, preset in enumerate(getattr(info, "preset_info", ()) or ()):
        frames = int(getattr(preset, "num_frames", 0) or 0)
        if not getattr(preset, "available", False) or frames <= 0:
            continue
        calibrated = bool(getattr(preset, "calibrated", False))
        presets.append(PresetRange(index=index, frames=frames,
                                   min_k=float(preset.min_temp) if calibrated else None,
                                   max_k=float(preset.max_temp) if calibrated else None))
    return tuple(presets) if len(presets) >= 2 else ()


def _source_details(im: Any) -> tuple[tuple[str, str], ...]:
    """Static per-recording property rows for the Source Information panel (§4.8.3)."""
    info = im.source_info
    rows: list[tuple[str, str]] = []

    def add(label: str, value: Any, suffix: str = "") -> None:
        if value is None:
            return
        text = str(value).strip()
        if text and text != "0":
            rows.append((label, f"{text}{suffix}"))

    add("Camera", getattr(info, "camera", ""))
    add("Camera model", getattr(info, "camera_model", ""))
    add("Camera serial", getattr(info, "camera_serial", ""))
    add("Camera part number", getattr(info, "camera_part_number", ""))
    add("Lens", getattr(info, "lens", ""))
    add("Lens part number", getattr(info, "lens_part_number", ""))
    add("Lens serial", getattr(info, "lens_serial", ""))
    add("Filter", getattr(info, "filter", ""))
    add("Filter serial", getattr(info, "filter_serial", ""))
    if getattr(info, "fov_valid", False):
        rows.append(
            (
                "IFOV (H × V)",
                f"{float(getattr(info, 'ihfov', 0.0)):.2f} × "
                f"{float(getattr(info, 'ivfov', 0.0)):.2f} µrad",
            )
        )
    if getattr(info, "bandpass_start_valid", False) and getattr(
        info, "bandpass_end_valid", False
    ):
        rows.append(
            (
                "Bandpass",
                f"{float(getattr(info, 'bandpass_start', 0.0)):.2f} – "
                f"{float(getattr(info, 'bandpass_end', 0.0)):.2f} µm",
            )
        )
    if getattr(info, "gps_valid", False):
        rows.append(
            (
                "GPS",
                f"{float(getattr(info, 'gps_latitude', 0.0)):.5f}, "
                f"{float(getattr(info, 'gps_longitude', 0.0)):.5f}",
            )
        )
    rows.append(("Image size", f"{int(im.width)} × {int(im.height)}"))
    add("Pixel type", _pretty_value(getattr(info, "pixel_type", "")))
    ad_bits = getattr(info, "ad_bits", 0)
    if ad_bits:
        rows.append(("A/D bits", str(ad_bits)))
    if getattr(info, "superframing", False):
        rows.append(("Superframing", "Yes"))
    rows.append(("Presets", str(int(getattr(info, "num_presets", 1)))))

    for index, preset in enumerate(getattr(info, "preset_info", ()) or ()):
        if not getattr(preset, "available", True):
            continue
        parts: list[str] = []
        if getattr(preset, "frame_rate_valid", False):
            parts.append(f"{float(preset.frame_rate):.3g} Hz")
        if getattr(preset, "int_time_valid", False):
            parts.append(f"IT {float(preset.int_time):.3g} ms")
        if getattr(preset, "calibrated", False):
            parts.append(
                f"cal {float(preset.min_temp):.0f}–{float(preset.max_temp):.0f} K"
            )
        frames = int(getattr(preset, "num_frames", 0))
        if frames:
            parts.append(f"{frames} frames")
        if parts:
            rows.append((f"Preset {index}", " · ".join(parts)))
    return tuple(rows)

OBJECT_PARAMETER_FIELDS: tuple[str, ...] = (
    "emissivity",
    "reflected_temp",
    "atmosphere_temp",
    "est_atmospheric_transmission",
    "distance",
    "relative_humidity",
    "ext_optics_temp",
    "ext_optics_transmission",
)


class FlirVideoSource:
    """Thin, thread-confined wrapper around the FLIR File SDK."""

    def __init__(self) -> None:
        self._im: Any | None = None
        self._metadata: VideoMetadata | None = None
        self._available_units: tuple[UnitOption, ...] = ()
        self._unit_spec: _UnitSpec | None = None
        self._roi_handles: list[tuple[int, RoiShape, Any]] = []
        self._image_roi: Any | None = None  # built-in whole-image ROI handle
        self._first_packet: FramePacket | None = None  # decoded during open()
        self._processing = ProcessingState()
        self._reference: np.ndarray | None = None
        self._reference_label = ""
        self._temporal = TemporalBuffer()
        self._last_frame_index: int | None = None
        self._reference_spec: tuple[Path, int, str] | None = None
        self.revision = 0
        # Temporal-filter inputs (file op → point → spatial) by (revision,
        # index), so warm-ups after a seek replay cached work, not the pipeline.
        self._pretemporal: OrderedDict[tuple[int, int], np.ndarray] = OrderedDict()
        self._pretemporal_bytes = 0
        self._provenance: tuple[int, tuple] | None = None  # (revision, provenance)
        self._clock = TimestampRepair(None)  # ATS year repair (see sdktime)

    def _invalidate_processing(self) -> None:
        self.revision += 1
        self._temporal.reset()
        self._last_frame_index = None
        self._pretemporal.clear()
        self._pretemporal_bytes = 0
        self._provenance = None

    def _reload_reference(self) -> None:
        if self._reference_spec is not None:
            self.load_reference(*self._reference_spec)

    @property
    def is_open(self) -> bool:
        return self._im is not None

    def sdk_handle(self) -> Any | None:
        """The open ImagerFile, for jobs on this (the owning) thread only.

        Borrowers must restore its unit, scale and object parameters
        (``calibration.preserved_state``).
        """
        return self._im

    @property
    def metadata(self) -> VideoMetadata:
        if self._metadata is None:
            raise RuntimeError("No FLIR recording is open")
        return self._metadata

    @property
    def available_units(self) -> tuple[UnitOption, ...]:
        return self._available_units

    @property
    def unit(self) -> UnitOption:
        if self._unit_spec is None:
            raise RuntimeError("No FLIR unit is selected")
        return self._unit_spec.option

    def open(self, path: str | Path) -> VideoMetadata:
        self.close()
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Recording not found: {resolved}")

        try:
            self._clock = TimestampRepair(resolved)
            self._im = fnv.file.ImagerFile(str(resolved))
            if self._im.num_frames <= 0:
                raise ValueError("The recording contains no frames")
            saved_parameters, saved_by = self._undo_saved_override(resolved)

            supported_sdk_units = set(self._im.supported_units)
            self._available_units = tuple(
                spec.option for spec in UNIT_SPECS if spec.sdk_unit in supported_sdk_units
            )
            if not self._available_units:
                raise ValueError("The recording exposes no supported thermal units")

            preferred_key = (
                "counts"
                if any(option.key == "counts" for option in self._available_units)
                else self._available_units[0].key
            )
            self.set_unit(preferred_key)
            self._image_roi = self._find_builtin_image_roi()

            first_packet = self.read_frame(0, request_id=0)
            first_time = first_packet.timestamp
            last_index = int(self._im.num_frames) - 1
            last_time = self._timestamp_for_frame(last_index) if last_index else first_time
            self._first_packet = first_packet
            duration = self._duration(first_time, last_time)
            rate = recording_rate(self._im, self._clock)
            nominal_fps = rate.fps

            cadence = _cadence_info(self._im, duration, int(self._im.num_frames))
            source_info = self._im.source_info
            saved_rows = ()
            if saved_parameters is not None:
                saved_rows = (("Saved settings", f"{saved_by} override, not applied: "
                               + describe_parameters(saved_parameters, self.read_object_parameters())),)
            self._metadata = VideoMetadata(
                path=resolved,
                width=int(self._im.width),
                height=int(self._im.height),
                num_frames=int(self._im.num_frames),
                start_time=first_time,
                end_time=last_time,
                duration_seconds=(last_index / nominal_fps if rate.corrected or duration <= 0
                                  else duration),
                nominal_fps=float(nominal_fps),
                camera_model=str(getattr(source_info, "camera_model", "") or ""),
                camera_serial=str(getattr(source_info, "camera_serial", "") or ""),
                source_details=_cadence_rows(cadence, float(nominal_fps), rate)
                + ((("Recording date", "From the file's modification time (the ATS clock has no year)"),)
                   if self._clock.repaired else ())
                + saved_rows
                + _source_details(self._im),
                cadence=cadence,
                rate=rate,
                saved_parameters=saved_parameters,
                saved_by=saved_by,
                presets=_superframing_presets(self._im),
            )
            return self._metadata
        except Exception:
            self.close()
            raise

    def set_unit(self, key: str) -> UnitOption:
        if self._im is None:
            raise RuntimeError("No FLIR recording is open")
        spec = _SPECS_BY_KEY.get(key)
        if spec is None:
            raise KeyError(f"Unknown unit: {key}")
        if not self._im.has_unit(spec.sdk_unit):
            raise ValueError(f"The current recording does not support {spec.option.label}")

        previous = self._unit_spec
        try:
            set_unit_safely(self._im, spec.sdk_unit, spec.temperature_type)
            self._unit_spec = spec
            self._reload_reference()
        except Exception:
            self._unit_spec = previous
            if previous is not None:
                set_unit_safely(self._im, previous.sdk_unit, previous.temperature_type)
            raise
        self._invalidate_processing()
        return spec.option

    def read_frame(
        self,
        index: int,
        request_id: int = 0,
        need_clip: bool = True,
        need_metadata: bool = True,
    ) -> FramePacket:
        if self._im is None or self._unit_spec is None:
            raise RuntimeError("No FLIR recording is open")
        frame_index = max(0, min(int(index), int(self._im.num_frames) - 1))
        try:
            return self._read_frame(frame_index, request_id, need_clip, need_metadata)
        except BaseException:
            # A failure part-way (e.g. MemoryError in a large median) leaves the
            # temporal ring and frame index unreliable: a retry must not treat
            # the previous frame's window as this frame's. Rebuild from scratch.
            self._temporal.reset()
            self._last_frame_index = None
            raise

    def _read_frame(
        self, frame_index: int, request_id: int, need_clip: bool, need_metadata: bool
    ) -> FramePacket:
        # Define every result by source indices, independent of request order,
        # packet-cache hits, exports, reverse stepping and payload refreshes.
        temporal, depth = self._processing.temporal
        if temporal != "none":
            depth = max(2, int(depth))
            if frame_index == self._last_frame_index and len(self._temporal):
                # The frame just produced again (payload refetch, ROI edit):
                # its window is already in the ring, so nothing is replayed.
                self._im.get_frame(frame_index)
                return self._packet_from_current(
                    frame_index, request_id, need_clip, need_metadata,
                    processed=self._temporal.result(temporal),
                )
            if frame_index != (
                self._last_frame_index + 1 if self._last_frame_index is not None else -1
            ):
                self._temporal.reset()
                for warm_index in range(max(0, frame_index - depth + 1), frame_index):
                    self._temporal.push(self._pretemporal_frame(warm_index), depth)
                    self._last_frame_index = warm_index
        self._im.get_frame(frame_index)
        return self._packet_from_current(
            frame_index, request_id, need_clip, need_metadata
        )

    _PRETEMPORAL_BUDGET = 256 * 1024 * 1024

    def _pretemporal_frame(self, frame_index: int, raw: np.ndarray | None = None) -> np.ndarray:
        """Frame ``frame_index`` through every stage before the temporal filter.

        Cached per processing revision (arrays are never modified afterwards),
        bounded by a byte budget and about two temporal windows.
        """
        key = (self.revision, frame_index)
        cached = self._pretemporal.get(key)
        if cached is not None:
            self._pretemporal.move_to_end(key)
            return cached
        if raw is None:
            self._im.get_frame(frame_index)
            raw = np.array(self._im.final, copy=True).reshape(
                (int(self._im.height), int(self._im.width))
            )
        data = self._pretemporal_stages(raw)
        self._pretemporal[key] = data
        self._pretemporal_bytes += data.nbytes
        limit = 2 * max(2, int(self._processing.temporal[1]))
        while len(self._pretemporal) > 1 and (
            self._pretemporal_bytes > self._PRETEMPORAL_BUDGET or len(self._pretemporal) > limit
        ):
            _, evicted = self._pretemporal.popitem(last=False)
            self._pretemporal_bytes -= evicted.nbytes
        return data

    def _packet_from_current(
        self,
        frame_index: int,
        request_id: int,
        need_clip: bool = True,
        need_metadata: bool = True,
        processed: np.ndarray | None = None,
    ) -> FramePacket:
        """Build a FramePacket for the frame the SDK is currently positioned on.

        ``need_clip``/``need_metadata`` skip SDK work the caller will not
        consume (the status-bit scan and the frame-header string conversions);
        interactive requests pass False when those views are hidden, exports
        always keep the defaults.
        """
        if processed is None:
            data = np.array(self._im.final, copy=True).reshape(
                (int(self._im.height), int(self._im.width))
            )
            data = self._process_frame(data, frame_index)
        else:
            data = processed
        (
            minimum,
            maximum,
            mean,
            std_dev,
            num_pixels,
            min_position,
            max_position,
        ) = self._whole_image_stats(data)

        clip_mask = self._clip_mask(data.shape) if need_clip else None
        metadata_entries = self._frame_metadata_entries() if need_metadata else ()

        return FramePacket(
            index=frame_index,
            data=data,
            timestamp=self._safe_frame_time(),
            unit=self._unit_spec.option,
            minimum=minimum,
            maximum=maximum,
            mean=mean,
            request_id=request_id,
            roi_stats=self._frame_roi_stats(data),
            std_dev=std_dev,
            num_pixels=num_pixels,
            min_position=min_position,
            max_position=max_position,
            clip_mask=clip_mask,
            metadata_entries=metadata_entries,
            clip_loaded=need_clip,
            metadata_loaded=need_metadata,
            revision=self.revision,
            processing=self._processing,
            analysis=self._analysis_provenance(),
        )

    def _analysis_provenance(self) -> tuple:
        # Small immutable values, never SDK handles, accompany numerical exports.
        # Everything here changes only with the revision, so read the SDK once.
        if self._provenance is not None and self._provenance[0] == self.revision:
            return self._provenance[1]
        reference = self._reference_spec
        provenance = (
            ("reference", (str(reference[0]), reference[1], reference[2]) if reference else None),
            ("object_parameters", tuple(self.read_object_parameters().items())),
            ("corrections", tuple(self.read_corrections().items())))
        self._provenance = (self.revision, provenance)
        return provenance

    def _whole_image_stats(self, data: np.ndarray):
        """Whole-image statistics for one (possibly processed) frame.

        When the processing pipeline is inactive, the SDK's built-in whole-image
        ``Image`` ROI already carries min/max/mean/std-dev/pixel count/extrema
        positions for the current frame — reading them costs microseconds, while
        the NumPy reduction costs several milliseconds per frame. The SDK only
        sees pre-processing data, so the app-side reduction is kept for the
        processing-active case.
        """
        if not self._processing.is_active and self._image_roi is not None:
            try:
                roi = self._image_roi
                return (
                    float(roi.min_value),
                    float(roi.max_value),
                    float(roi.mean),
                    float(roi.std_dev),
                    int(roi.num_pixels),
                    _position(roi.min_position),
                    _position(roi.max_position),
                )
            except Exception:
                pass  # fall through to the NumPy reduction below
        finite_mask = np.isfinite(data)
        num_pixels = int(finite_mask.sum())
        if not num_pixels:
            return (0.0, 0.0, 0.0, 0.0, 0, None, None)
        finite = data[finite_mask]
        low_index = np.unravel_index(
            int(np.argmin(np.where(finite_mask, data, np.inf))), data.shape
        )
        high_index = np.unravel_index(
            int(np.argmax(np.where(finite_mask, data, -np.inf))), data.shape
        )
        return (
            float(np.min(finite)),
            float(np.max(finite)),
            float(np.mean(finite, dtype=np.float64)),
            float(np.std(finite, dtype=np.float64)),
            num_pixels,
            (int(low_index[1]), int(low_index[0])),
            (int(high_index[1]), int(high_index[0])),
        )

    def _process_frame(self, data: np.ndarray, frame_index: int) -> np.ndarray:
        """Apply the processing pipeline (file-op → point → spatial → temporal)."""
        state = self._processing
        if not state.is_active:
            result = data
        elif state.temporal[0] != "none":
            result = self._temporal.apply(
                self._pretemporal_frame(frame_index, raw=data), state.temporal[0], state.temporal[1]
            )
        else:
            result = self._pretemporal_stages(data)
        self._last_frame_index = frame_index  # only once the frame is fully processed
        return result

    def _pretemporal_stages(self, data: np.ndarray) -> np.ndarray:
        state = self._processing
        if state.file_op is not None and self._reference is not None:
            data = apply_file_operation(data, self._reference, state.file_op)
        if state.point[0] != "none":
            data = apply_point_filter(data, state.point[0], state.point[1])
        if state.spatial[0] != "none":
            data = apply_spatial_filter(data, state.spatial[0], state.spatial[1])
        return data

    def _frame_roi_stats(self, data: np.ndarray) -> tuple[RoiStats, ...]:
        """Use SDK fast paths only for coverage with established parity.

        Long SDK diagonal lines can differ by a pixel from integer nearest
        sampling. Always measure lines with our canonical geometry, including
        when processing is disabled, so enabling identity filters changes nothing.
        """
        if self._processing.is_active:
            shapes = tuple(shape for _, shape, _ in self._roi_handles)
            return roi_stats_app(data, shapes)
        sdk_stats = self._roi_stats()
        lines = {s.id: s for s in roi_stats_app(data, tuple(
            shape for _, shape, _ in self._roi_handles if shape.kind == "line"))}
        return tuple(lines.get(stats.id, stats) for stats in sdk_stats)

    def _clip_mask(self, shape: tuple[int, ...]) -> np.ndarray | None:
        """Boolean mask of pixels the SDK clamped to its clip range.

        In temperature units the SDK status is 1 outside the calibrated range
        (extrapolated), 2 when clamped at the low clip limit and 4 when clamped
        at the high limit, which includes saturated pixels. Returns None when
        nothing is clipped so callers can skip overlay work.
        """
        if self._im is None:
            return None
        status = np.array(self._im.status, copy=False)
        if status.size != int(np.prod(shape)):
            return None
        clipped = (status.reshape(shape) & (2 | 4)) != 0
        return clipped if clipped.any() else None

    def _frame_metadata_entries(self) -> tuple[tuple[str, str], ...]:
        if self._im is None:
            return ()
        entries: list[tuple[str, str]] = []
        try:
            for entry in self._im.frame_info:
                entries.append((str(entry["name"]), str(entry["value"])))
        except Exception:
            pass
        return tuple(entries)

    def close(self) -> None:
        if self._im is not None:
            try:
                self._im.close()
            except Exception:
                pass
        self._im = None
        self._metadata = None
        self._available_units = ()
        self._unit_spec = None
        self._roi_handles = []
        self._image_roi = None
        self._first_packet = None
        self._processing = ProcessingState()
        self._reference = None
        self._reference_label = ""
        self._reference_spec = None
        self._temporal.reset()
        self._last_frame_index = None
        self._pretemporal.clear()
        self._pretemporal_bytes = 0
        self._provenance = None

    # --- processing pipeline (§4.8.6, §4.9.5.3) ------------------------------------

    @property
    def reference_label(self) -> str:
        return self._reference_label

    @property
    def processing(self) -> ProcessingState:
        return self._processing

    def set_processing(self, state: ProcessingState) -> None:
        """Update point/spatial/temporal stages; file-op is reference-owned."""
        merged = replace(state, file_op=self._processing.file_op)
        from .processing import median_max_size
        size = max(3, int(merged.spatial[1]) | 1)
        if merged.spatial[0] == "median" and self._im is not None:
            size = min(size, median_max_size(int(self._im.height) * int(self._im.width)))
        merged = replace(merged, spatial=(merged.spatial[0], size))
        self._processing = merged
        self._invalidate_processing()

    def load_reference(self, path: str | Path, frame_index: int, operation: str) -> str:
        """Load a reference frame for file operations (§4.9.5.3).

        Reads through the current handle when the reference is the open file
        (the caller re-reads the current frame afterwards), otherwise through a
        temporary ImagerFile so all SDK access stays on the decoder thread.
        """
        if self._im is None:
            raise RuntimeError("No FLIR recording is open")
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Recording not found: {resolved}")

        same_file = self._metadata is not None and resolved == self._metadata.path
        im = self._im if same_file else fnv.file.ImagerFile(str(resolved))
        try:
            if self._unit_spec is not None:
                if not im.has_unit(self._unit_spec.sdk_unit):
                    raise ValueError("The reference does not support the selected unit")
                set_unit_safely(im, self._unit_spec.sdk_unit, self._unit_spec.temperature_type)
            if not same_file:
                # Reference pixels use the same measurement parameters and
                # available correction switches as the current recording.
                if im.can_change_object_parameters:
                    params = im.object_parameters
                    current = self.read_object_parameters()
                    for field in OBJECT_PARAMETER_FIELDS:
                        setattr(params, field, current[field])
                    im.object_parameters = params
                for key in ("nuc", "bp"):
                    if getattr(im, f"has_{key}", False):
                        setattr(im, f"apply_{key}", getattr(self._im, f"apply_{key}", False))
            index = max(0, min(int(frame_index), int(im.num_frames) - 1))
            im.get_frame(index)
            reference = np.array(im.final, copy=True).reshape(
                (int(im.height), int(im.width))
            )
        finally:
            if not same_file:
                try:
                    im.close()
                except Exception:
                    pass

        expected = (int(self._im.height), int(self._im.width))
        if reference.shape != expected:
            raise ValueError(
                f"Reference size {reference.shape[1]}×{reference.shape[0]} does not "
                f"match the current recording ({expected[1]}×{expected[0]})"
            )
        self._reference = reference
        self._reference_spec = (resolved, index, str(operation))
        self._processing = replace(self._processing, file_op=str(operation))
        self._invalidate_processing()
        self._reference_label = f"{resolved.name} · frame {index + 1}"
        return self._reference_label

    def clear_reference(self) -> None:
        self._reference = None
        self._reference_label = ""
        self._reference_spec = None
        self._processing = replace(self._processing, file_op=None)
        self._invalidate_processing()

    def export_roi_bitmasks(self, folder: str | Path, overwrite=()) -> list[str]:
        """Export one PNG bitmask per app-managed ROI (§4.9.1.1, p. 61).

        ``overwrite`` lists existing bitmask files the user agreed to replace.
        """
        if self._im is None:
            raise RuntimeError("No FLIR recording is open")
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        from .export import bitmask_filename
        from .geometry import roi_coordinates
        from .jobs import OutputTransaction
        written: list[str] = []
        with OutputTransaction([self.metadata.path], replace=overwrite) as job:
            entries = []
            for _roi_id, shape, handle in self._roi_handles:
                dest = folder / bitmask_filename(shape.name)
                entries.append((shape, job.stage(dest)))
                written.append(str(dest))
            for shape, stage in entries:
                mask = np.zeros((self.metadata.height, self.metadata.width), np.uint8)
                ys, xs = roi_coordinates(shape, *mask.shape)
                mask[ys, xs] = 255
                Image.fromarray(mask).save(stage, "PNG")
            job.commit()
        return written

    def set_rois(self, shapes: tuple[RoiShape, ...]) -> None:
        """Replace the app-managed ROI set and recompute their statistics.

        Only ROIs added through this method are removed again; ROIs stored in
        the recording itself (e.g. the built-in whole-image ROI) are left alone,
        and nothing is written back to the file on disk.
        """
        if self._im is None:
            raise RuntimeError("No FLIR recording is open")
        for _, _, handle in self._roi_handles:
            try:
                self._im.rois.remove(handle)
            except Exception:
                pass
        self._roi_handles = []
        for shape in shapes:
            handle = self._add_roi(shape)
            if handle is not None:
                self._roi_handles.append((shape.id, shape, handle))
        self._im.update_frame()

    def _add_roi(self, shape: RoiShape) -> Any | None:
        from .geometry import pixel_index
        width, height = int(self._im.width), int(self._im.height)

        def clamp(point: tuple[float, float]) -> dict[str, int]:
            # The same integer pixels as the app geometry, so SDK statistics
            # and app-side statistics measure one pixel set.
            x, y = pixel_index(shape.kind, point[0], point[1], width, height)
            return {"x": x, "y": y}

        if shape.kind == "cursor" and len(shape.points) == 1:
            return self._im.rois.add_cursor(clamp(shape.points[0]))
        if shape.kind == "line" and len(shape.points) == 2:
            return self._im.rois.add_line(clamp(shape.points[0]), clamp(shape.points[1]))
        if shape.kind in {"rect", "ellipse"} and len(shape.points) == 2:
            first, second = clamp(shape.points[0]), clamp(shape.points[1])
            box = {
                "left": min(first["x"], second["x"]),
                "top": min(first["y"], second["y"]),
                "right": max(first["x"], second["x"]),
                "bottom": max(first["y"], second["y"]),
            }
            if box["right"] == box["left"] or box["bottom"] == box["top"]:
                return None
            if shape.kind == "rect":
                return self._im.rois.add_rect(box)
            return self._im.rois.add_ellipse(box)
        return None

    def _roi_stats(self) -> tuple[RoiStats, ...]:
        stats: list[RoiStats] = []
        for roi_id, shape, handle in self._roi_handles:
            stats.append(
                RoiStats(
                    id=roi_id,
                    name=shape.name,
                    kind=shape.kind,
                    minimum=float(handle.min_value),
                    maximum=float(handle.max_value),
                    mean=float(handle.mean),
                    std_dev=float(handle.std_dev),
                    num_pixels=int(handle.num_pixels),
                    value=float(handle.center_value if shape.kind == "cursor" else handle.mean),
                    min_position=_position(handle.min_position),
                    max_position=_position(handle.max_position),
                )
            )
        return tuple(stats)

    def _find_builtin_image_roi(self) -> Any | None:
        """Handle of the recording's built-in whole-image ROI, if present.

        Matched by name and type — never assumed to sit at index zero. Returns
        None when the recording has none (the NumPy reduction is then used).
        """
        if self._im is None:
            return None
        try:
            image_type = fnv.reduce.RoiType.IMAGE
            for roi in self._im.rois:
                if getattr(roi, "name", "") == "Image" and roi.type == image_type:
                    return roi
        except Exception:
            pass
        return None

    def state_snapshot(self) -> dict:
        """Plain-value measurement and processing state currently in force."""
        spec = self._reference_spec
        return {
            "unit": self.unit.key,
            "object_parameters": self.read_object_parameters(),
            "corrections": self.read_corrections(),
            "reference": None if spec is None else {
                "path": str(spec[0]), "frame_index": spec[1], "op": spec[2]},
            "reference_label": self._reference_label,
            "processing": {
                "point": tuple(self._processing.point),
                "spatial": tuple(self._processing.spatial),
                "temporal": tuple(self._processing.temporal),
            },
        }

    def take_first_packet(self) -> FramePacket | None:
        """Hand out the frame-0 packet decoded during open() (consumed once)."""
        packet, self._first_packet = self._first_packet, None
        return packet

    def extract(
        self,
        dest: str | Path,
        start_frame: int,
        end_frame: int,
        decimation: int = 1,
        progress: Any | None = None,
        abort: Any | None = None,
    ) -> tuple[bool, str]:
        """Extract a frame range into a new ATS file (ResearchIR §4.9.1.2).

        Returns (success, message). The SDK silently no-ops for unsupported
        source/destination combinations, so the output file is always verified
        to exist afterwards; partial output is removed on abort/failure.
        """
        if self._im is None:
            raise RuntimeError("No FLIR recording is open")
        dest_path = Path(dest)
        options = fnv.file.ImagerFileExtractOptions()
        options.start_frame = max(0, int(start_frame))
        options.end_frame = int(end_frame)
        options.decimation = max(1, int(decimation))

        from .jobs import extract_recording
        return extract_recording(self._im, self.metadata.path, dest_path, options,
                                 progress=progress, abort=abort)

    def read_corrections(self) -> dict[str, bool]:
        """Availability and apply-state of embedded NUC / bad-pixel corrections."""
        if self._im is None:
            raise RuntimeError("No FLIR recording is open")
        return {
            "has_nuc": bool(getattr(self._im, "has_nuc", False)),
            "apply_nuc": bool(getattr(self._im, "apply_nuc", False)),
            "has_bp": bool(getattr(self._im, "has_bp", False)),
            "apply_bp": bool(getattr(self._im, "apply_bp", False)),
        }

    def set_corrections(self, nuc: bool | None, bp: bool | None) -> dict[str, bool]:
        """Toggle embedded corrections; files without them are left untouched."""
        if self._im is None:
            raise RuntimeError("No FLIR recording is open")
        previous = self.read_corrections()
        try:
            if nuc is not None and previous["has_nuc"]:
                self._im.apply_nuc = bool(nuc)
            if bp is not None and previous["has_bp"]:
                self._im.apply_bp = bool(bp)
            self._im.update_frame()
            self._reload_reference()
        except Exception:
            for key in ("nuc", "bp"):
                if previous[f"has_{key}"]:
                    setattr(self._im, f"apply_{key}", previous[f"apply_{key}"])
            self._im.update_frame()
            raise
        self._invalidate_processing()
        return self.read_corrections()

    def read_object_parameters(self) -> dict[str, Any]:
        """Snapshot the current measurement object parameters as plain values."""
        if self._im is None:
            raise RuntimeError("No FLIR recording is open")
        params = self._im.object_parameters
        snapshot = {
            field: float(getattr(params, field)) for field in OBJECT_PARAMETER_FIELDS
        }
        # Read-only, computed by the SDK from the estimate above; informational.
        snapshot["atmospheric_transmission"] = float(params.atmospheric_transmission)
        snapshot["can_change"] = bool(self._im.can_change_object_parameters)
        return snapshot

    def _undo_saved_override(self, path: Path) -> tuple[dict[str, float] | None, str]:
        """Open with the camera's object parameters, not a saved software override.

        A ResearchIR workspace saved in the file can override the object
        parameters every frame records (the 0922 A700 test: ε 1, 3 m, τ 1
        over the camera's ε 0.95, 1 m), and the SDK applies it on open. The
        player starts from the camera's values instead, as "Reset" and the
        Excel export do. Whatever the SDK applied is returned when it differs,
        so it can be offered, with its origin for the labels.
        """
        opened = self.read_object_parameters()
        self._im.reset_object_parameters()
        camera = self.read_object_parameters()
        saved = {key: opened[key] for key in OBJECT_PARAMETER_FIELDS}
        if all(math.isclose(value, camera[key], rel_tol=1e-5, abs_tol=1e-5) for key, value in saved.items()):
            return None, ""
        return saved, "ResearchIR" if saved_object_parameters(path) is not None else "FLIR software"

    def apply_object_parameters(self, values: dict[str, float] | None) -> dict[str, Any]:
        """Apply edited object parameters (None resets to file defaults).

        The SDK uses read-modify-write semantics; ``update_frame`` then re-reduces
        the current frame without re-reading it from disk.
        """
        if self._im is None:
            raise RuntimeError("No FLIR recording is open")
        previous = self._im.object_parameters
        try:
            if values is None:
                self._im.reset_object_parameters()
            else:
                params = self._im.object_parameters
                for field in OBJECT_PARAMETER_FIELDS:
                    if field in values:
                        setattr(params, field, float(values[field]))
                self._im.object_parameters = params
            self._im.update_frame()
            self._reload_reference()
        except Exception:
            self._im.object_parameters = previous
            self._im.update_frame()
            raise
        self._invalidate_processing()
        return self.read_object_parameters()

    def _timestamp_for_frame(self, index: int) -> datetime | None:
        if self._im is None:
            return None
        self._im.get_frame(index)
        return self._safe_frame_time()

    def _safe_frame_time(self) -> datetime | None:
        if self._im is None:
            return None
        value = getattr(self._im.frame_info, "time", None)
        return self._clock(value) if isinstance(value, datetime) else None

    @staticmethod
    def _duration(start: datetime | None, end: datetime | None) -> float:
        if start is None or end is None:
            return 0.0
        try:
            return max(0.0, (end - start).total_seconds())
        except (OverflowError, TypeError, ValueError):
            return 0.0
