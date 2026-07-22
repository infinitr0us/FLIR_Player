# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # The FLIR File SDK is a proprietary, user-supplied optional dependency.
    import fnv
    import fnv.file

    _SDK_AVAILABLE = True
except ModuleNotFoundError:  # Let this module import without the SDK present.
    fnv = None  # type: ignore[assignment]
    _SDK_AVAILABLE = False
import numpy as np

from .models import FramePacket, RoiShape, RoiStats, UnitOption, VideoMetadata
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
        self._processing = ProcessingState()
        self._reference: np.ndarray | None = None
        self._reference_label = ""
        self._temporal = TemporalBuffer()
        self._last_frame_index: int | None = None

    @property
    def is_open(self) -> bool:
        return self._im is not None

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
            self._im = fnv.file.ImagerFile(str(resolved))
            if self._im.num_frames <= 0:
                raise ValueError("The recording contains no frames")

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

            first_time = self._timestamp_for_frame(0)
            last_index = int(self._im.num_frames) - 1
            last_time = self._timestamp_for_frame(last_index) if last_index else first_time
            duration = self._duration(first_time, last_time)
            nominal_fps = (last_index / duration) if last_index > 0 and duration > 0 else 30.0

            source_info = self._im.source_info
            self._metadata = VideoMetadata(
                path=resolved,
                width=int(self._im.width),
                height=int(self._im.height),
                num_frames=int(self._im.num_frames),
                start_time=first_time,
                end_time=last_time,
                duration_seconds=duration if duration > 0 else last_index / nominal_fps,
                nominal_fps=float(nominal_fps),
                camera_model=str(getattr(source_info, "camera_model", "") or ""),
                camera_serial=str(getattr(source_info, "camera_serial", "") or ""),
                source_details=_source_details(self._im),
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

        self._im.unit = spec.sdk_unit
        if spec.temperature_type is not None:
            self._im.temp_type = spec.temperature_type
        self._unit_spec = spec
        return spec.option

    def read_frame(self, index: int, request_id: int = 0) -> FramePacket:
        if self._im is None or self._unit_spec is None:
            raise RuntimeError("No FLIR recording is open")
        frame_index = max(0, min(int(index), int(self._im.num_frames) - 1))
        self._im.get_frame(frame_index)
        data = np.array(self._im.final, copy=True).reshape(
            (int(self._im.height), int(self._im.width))
        )
        data = self._process_frame(data, frame_index)
        finite = data[np.isfinite(data)]
        min_position = max_position = None
        if finite.size:
            minimum = float(np.min(finite))
            maximum = float(np.max(finite))
            mean = float(np.mean(finite))
            std_dev = float(np.std(finite))
            finite_mask = np.isfinite(data)
            low_index = np.unravel_index(
                int(np.argmin(np.where(finite_mask, data, np.inf))), data.shape
            )
            high_index = np.unravel_index(
                int(np.argmax(np.where(finite_mask, data, -np.inf))), data.shape
            )
            min_position = (int(low_index[1]), int(low_index[0]))
            max_position = (int(high_index[1]), int(high_index[0]))
        else:
            minimum = maximum = mean = std_dev = 0.0

        clip_mask = self._clip_mask(data.shape)
        metadata_entries = self._frame_metadata_entries()

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
            num_pixels=int(finite.size),
            min_position=min_position,
            max_position=max_position,
            clip_mask=clip_mask,
            metadata_entries=metadata_entries,
        )

    def _process_frame(self, data: np.ndarray, frame_index: int) -> np.ndarray:
        """Apply the processing pipeline (file-op → point → spatial → temporal)."""
        if (
            self._last_frame_index is None
            or abs(frame_index - self._last_frame_index) != 1
        ):
            self._temporal.reset()
        self._last_frame_index = frame_index
        state = self._processing
        if not state.is_active:
            return data
        if state.file_op is not None and self._reference is not None:
            data = apply_file_operation(data, self._reference, state.file_op)
        if state.point[0] != "none":
            data = apply_point_filter(data, state.point[0], state.point[1])
        if state.spatial[0] != "none":
            data = apply_spatial_filter(data, state.spatial[0], state.spatial[1])
        if state.temporal[0] != "none":
            data = self._temporal.apply(data, state.temporal[0], state.temporal[1])
        return data

    def _frame_roi_stats(self, data: np.ndarray) -> tuple[RoiStats, ...]:
        """SDK stats normally; app-side stats from the processed frame when active."""
        if self._processing.is_active:
            shapes = tuple(shape for _, shape, _ in self._roi_handles)
            return roi_stats_app(data, shapes)
        return self._roi_stats()

    def _clip_mask(self, shape: tuple[int, ...]) -> np.ndarray | None:
        """Boolean mask of pixels the SDK flags as clipped/invalid (status bit 1).

        Returns None when nothing is clipped so callers can skip overlay work.
        """
        if self._im is None:
            return None
        status = np.array(self._im.status, copy=False)
        if status.size != int(np.prod(shape)):
            return None
        clipped = (status.reshape(shape) & 2) != 0
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
        self._processing = ProcessingState()
        self._reference = None
        self._reference_label = ""
        self._temporal.reset()
        self._last_frame_index = None

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
        if merged.temporal != self._processing.temporal or not merged.is_active:
            self._temporal.reset()
        self._processing = merged

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
            if self._unit_spec is not None and im.has_unit(self._unit_spec.sdk_unit):
                im.unit = self._unit_spec.sdk_unit
                if self._unit_spec.temperature_type is not None:
                    im.temp_type = self._unit_spec.temperature_type
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
        self._processing = replace(self._processing, file_op=str(operation))
        self._temporal.reset()
        self._reference_label = f"{resolved.name} · frame {index + 1}"
        return self._reference_label

    def clear_reference(self) -> None:
        self._reference = None
        self._reference_label = ""
        self._processing = replace(self._processing, file_op=None)

    def export_roi_bitmasks(self, folder: str | Path) -> list[str]:
        """Export one PNG bitmask per app-managed ROI (§4.9.1.1, p. 61)."""
        if self._im is None:
            raise RuntimeError("No FLIR recording is open")
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for _roi_id, shape, handle in self._roi_handles:
            dest = folder / f"{shape.name.replace(' ', '_')}_bitmask.png"
            if handle.export_bitmask(str(dest)) and dest.is_file():
                written.append(str(dest))
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
        width, height = int(self._im.width), int(self._im.height)

        def clamp(point: tuple[float, float]) -> dict[str, int]:
            return {
                "x": max(0, min(int(round(point[0])), width - 1)),
                "y": max(0, min(int(round(point[1])), height - 1)),
            }

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
                    value=float(handle.center_value),
                    min_position=_position(handle.min_position),
                    max_position=_position(handle.max_position),
                )
            )
        return tuple(stats)

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

        def _callback(current: int, total: int) -> bool:
            if progress is not None:
                progress(current, total)
            return bool(abort is not None and abort())  # True aborts the SDK call

        options.progress_callback = _callback
        try:
            ok = bool(self._im.extract(str(dest_path), options))
        except Exception as exc:
            return (False, f"{type(exc).__name__}: {exc}")

        aborted = bool(abort is not None and abort())
        if aborted or not ok or not dest_path.is_file():
            try:
                dest_path.unlink(missing_ok=True)
            except OSError:
                pass
            if aborted:
                return (False, "Extraction cancelled")
            if not ok:
                return (False, "The File SDK reported the extraction as failed")
            return (
                False,
                "The File SDK produced no output for this recording "
                "(extraction is only supported from ATS sources)",
            )
        return (True, "")

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
        if nuc is not None and self.read_corrections()["has_nuc"]:
            self._im.apply_nuc = bool(nuc)
        if bp is not None and self.read_corrections()["has_bp"]:
            self._im.apply_bp = bool(bp)
        self._im.update_frame()
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

    def apply_object_parameters(self, values: dict[str, float] | None) -> dict[str, Any]:
        """Apply edited object parameters (None resets to file defaults).

        The SDK uses read-modify-write semantics; ``update_frame`` then re-reduces
        the current frame without re-reading it from disk.
        """
        if self._im is None:
            raise RuntimeError("No FLIR recording is open")
        if values is None:
            self._im.reset_object_parameters()
        else:
            params = self._im.object_parameters
            for field in OBJECT_PARAMETER_FIELDS:
                if field in values:
                    setattr(params, field, float(values[field]))
            self._im.object_parameters = params
        self._im.update_frame()
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
        return value if isinstance(value, datetime) else None

    @staticmethod
    def _duration(start: datetime | None, end: datetime | None) -> float:
        if start is None or end is None:
            return 0.0
        try:
            return max(0.0, (end - start).total_seconds())
        except (OverflowError, TypeError, ValueError):
            return 0.0
