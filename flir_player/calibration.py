# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find and verify a recording's counts → temperature calibration via the SDK.

Two independent sources are compared with the File SDK's own temperatures:
the file's FFF CameraInfo constants (SEQ/CSQ/FFF/JPG) and a fit to SDK
temperatures read at neutral parameters. A calibration is only reported as
verified when it also reproduces the SDK under non-default emissivity,
reflected temperature, transmission and window settings.

Everything here runs on the thread that owns the ``ImagerFile`` and restores
the handle's unit, temperature scale and object parameters before returning.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

try:
    import fnv
    import fnv.file
except ModuleNotFoundError:  # importable without the SDK (pure helpers only)
    fnv = None  # type: ignore[assignment]

from .fff import CameraInfo, read_camera_info
from .radiometry import (
    Calibration,
    MeasurementParameters,
    NEUTRAL,
    RangeLimits,
    fit_planck,
    object_temperature,
)

TOLERANCE_K = 0.002  # SDK output is float32; verified models agree to ~5e-5 K
OBJECT_FIELDS = (
    "emissivity", "reflected_temp", "atmosphere_temp", "est_atmospheric_transmission",
    "distance", "relative_humidity", "ext_optics_temp", "ext_optics_transmission",
)
_CHECKS: tuple[tuple[str, MeasurementParameters], ...] = (
    ("emissivity 0.8, reflected 47 °C", MeasurementParameters(emissivity=0.8, reflected_k=320.15)),
    ("transmission 0.85 at 37 °C, emissivity 0.9",
     MeasurementParameters(emissivity=0.9, transmission=0.85, atmosphere_k=310.15)),
    ("window 0.8 at 57 °C, transmission 0.9, emissivity 0.85, reflected 37 °C",
     MeasurementParameters(emissivity=0.85, reflected_k=310.15, transmission=0.9,
                           atmosphere_k=300.15, window_transmission=0.8, window_k=330.15)),
    ("auto transmission 10 m, 50 % RH, 25 °C",
     MeasurementParameters(emissivity=0.9, transmission=None, distance_m=10.0, humidity=0.5,
                           atmosphere_k=298.15)),
)


def set_unit_safely(im: Any, unit: Any, temp_type: Any | None = None) -> None:
    """Select ``unit`` and, for temperature units, ``temp_type``.

    The scale is only assigned when it changes. On recordings with a
    ResearchIR user calibration, FileSDK 5.0.1 corrupts memory on unit and
    scale changes (a later frame read dies with an access violation, even on a
    fresh handle); 2024.7 and 2026.1 do not. No call order avoids it
    reliably, so see ``user_calibration_risk``.
    """
    im.unit = unit
    if (temp_type is not None and unit in (fnv.Unit.TEMPERATURE_FACTORY, fnv.Unit.TEMPERATURE_USER)
            and im.temp_type != temp_type):
        im.temp_type = temp_type


def sdk_version() -> str:
    """Installed FileSDK distribution version, or "" when unknown."""
    try:
        from importlib.metadata import version
        return version("FileSDK")
    except Exception:
        return ""


def user_calibration_risk(im: Any) -> bool:
    """True when switching units on this recording may crash FileSDK 5.0.1."""
    version = sdk_version()
    units = set(im.supported_units)
    has_user = bool({fnv.Unit.RADIANCE_USER, fnv.Unit.TEMPERATURE_USER} & units)
    return has_user and version.startswith("5.")


def read_parameters(im: Any) -> dict[str, float]:
    params = im.object_parameters
    return {name: float(getattr(params, name)) for name in OBJECT_FIELDS}


def write_parameters(im: Any, values: dict[str, float]) -> None:
    params = im.object_parameters
    for name in OBJECT_FIELDS:
        if name in values:
            setattr(params, name, float(values[name]))
    im.object_parameters = params


def sdk_values(params: MeasurementParameters) -> dict[str, float]:
    """SDK object-parameter fields for plain measurement parameters."""
    return {
        "emissivity": params.emissivity,
        "reflected_temp": params.reflected_k,
        "atmosphere_temp": params.atmosphere_k,
        "est_atmospheric_transmission": 0.0 if params.transmission is None else params.transmission,
        "distance": params.distance_m,
        "relative_humidity": params.humidity,
        "ext_optics_temp": params.window_k,
        "ext_optics_transmission": params.window_transmission,
    }


@contextmanager
def preserved_state(im: Any):
    """Restore unit, temperature scale and object parameters afterwards."""
    unit = im.unit
    temp_type = im.temp_type
    params = read_parameters(im)
    try:
        yield
    finally:
        # Twice: the SDK's atmospheric_transmission readback lags one change.
        write_parameters(im, params)
        write_parameters(im, params)
        if im.temp_type != temp_type:
            # The scale can only be set meaningfully on a temperature unit.
            for temperature in (fnv.Unit.TEMPERATURE_FACTORY, fnv.Unit.TEMPERATURE_USER):
                if im.has_unit(temperature):
                    set_unit_safely(im, temperature, temp_type)
                    break
        set_unit_safely(im, unit)
        try:
            im.update_frame()
        except Exception:
            pass


def read_array(im: Any, index: int) -> np.ndarray:
    im.get_frame(int(index))
    return np.array(im.final, copy=True).reshape((int(im.height), int(im.width)))


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """Outcome of :func:`derive_calibration` for one recording."""

    calibration: Calibration | None  # None: temperatures cannot be reproduced
    status: str  # "verified" | "unavailable"
    message: str
    kind: str = "factory"  # "factory" | "user" | "none"
    fff: CameraInfo | None = None
    fff_error_k: float | None = None
    fit_error_k: float | None = None
    checks: tuple[tuple[str, float, int], ...] = ()
    file_parameters: dict = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.status == "verified" and self.calibration is not None


def _sample_frames(count: int, n: int) -> list[int]:
    return sorted({int(round(v)) for v in np.linspace(0, n - 1, count)})


def derive_calibration(im: Any, path: str | Path, *, frames: int = 8, abort=None) -> CalibrationReport:
    """Identify and verify the calibration of the open recording ``im``."""
    path = Path(path)
    fff = read_camera_info(path)
    with preserved_state(im):
        file_parameters = _file_parameters(im)
        units = set(im.supported_units)
        if fnv.Unit.TEMPERATURE_FACTORY not in units:
            if fnv.Unit.TEMPERATURE_USER in units:
                return CalibrationReport(
                    None, "unavailable",
                    "The recording uses a ResearchIR user calibration. Its temperatures cannot "
                    "be reproduced in Excel yet, so the workbook contains counts only.",
                    kind="user", fff=fff, file_parameters=file_parameters)
            return CalibrationReport(
                None, "unavailable",
                "The recording has no temperature calibration (counts only).",
                kind="none", fff=fff, file_parameters=file_parameters)
        n = int(im.num_frames)
        indices = _sample_frames(max(2, frames), n)
        write_parameters(im, sdk_values(NEUTRAL))
        set_unit_safely(im, fnv.Unit.COUNTS)
        counts = np.stack([read_array(im, i).astype(np.float64) for i in indices])
        if abort is not None and abort():
            return CalibrationReport(None, "unavailable", "Cancelled", fff=fff,
                                     file_parameters=file_parameters)
        set_unit_safely(im, fnv.Unit.TEMPERATURE_FACTORY, fnv.TempType.KELVIN)
        kelvin = np.stack([read_array(im, i).astype(np.float64) for i in indices])
        valid = _unclamped(kelvin)
        if valid.sum() < 16:
            return CalibrationReport(None, "unavailable",
                                     "Too few unclamped pixels to verify the calibration.",
                                     fff=fff, file_parameters=file_parameters)
        limits = _sdk_limits(im, fff)
        candidates: list[tuple[str, Calibration, float]] = []
        fff_error = fit_error = None
        if fff is not None:
            calibration = fff.calibration()
            fff_error = _max_error(calibration, NEUTRAL, counts[valid], kelvin[valid])
            candidates.append(("FFF CameraInfo record", calibration, fff_error))
        pairs = np.unique(np.column_stack([counts[valid], kelvin[valid]]), axis=0)
        if pairs.shape[0] >= 16 and np.ptp(pairs[:, 1]) >= 5.0:
            if pairs.shape[0] > 20000:
                pairs = pairs[np.linspace(0, pairs.shape[0] - 1, 20000).astype(int)]
            try:
                fitted = fit_planck(pairs[:, 1], pairs[:, 0])
                calibration = Calibration(
                    planck=fitted,
                    atmosphere=fff.calibration().atmosphere if fff is not None else None,
                    limits=limits, source="fitted to File SDK temperatures")
                fit_error = _max_error(calibration, NEUTRAL, counts[valid], kelvin[valid])
                candidates.append(("fit to File SDK temperatures", calibration, fit_error))
            except (ValueError, np.linalg.LinAlgError):
                pass
        accepted = [c for c in candidates if c[2] <= TOLERANCE_K]
        if not accepted:
            best = min((c[2] for c in candidates), default=None)
            detail = f" (best max error {best:.3g} K)" if best is not None else ""
            return CalibrationReport(
                None, "unavailable",
                "No calibration reproduced the File SDK's temperatures" + detail + ".",
                fff=fff, fff_error_k=fff_error, fit_error_k=fit_error,
                file_parameters=file_parameters)
        label, calibration, _error = accepted[0]
        # A frame with the widest spread exercises the most of the curve.
        widest = int(np.argmax([np.ptp(k[np.isfinite(k)]) for k in kelvin]))
        checks = _parameter_checks(im, calibration, indices[widest], counts[widest])
        worst = max((error for _label, error, _n in checks), default=np.inf)
        if not np.isfinite(worst) or worst > TOLERANCE_K:
            return CalibrationReport(
                None, "unavailable",
                f"The {label} did not reproduce the SDK under changed parameters "
                f"(max error {worst:.3g} K).",
                fff=fff, fff_error_k=fff_error, fit_error_k=fit_error, checks=tuple(checks),
                file_parameters=file_parameters)
        source = f"{label}; verified against the File SDK (max error {max(worst, _error):.1e} K)"
        calibration = Calibration(calibration.planck, calibration.atmosphere,
                                  limits if calibration.limits == RangeLimits() else calibration.limits,
                                  source)
        return CalibrationReport(calibration, "verified", source, fff=fff, fff_error_k=fff_error,
                                 fit_error_k=fit_error, checks=tuple(checks),
                                 file_parameters=file_parameters)


def _file_parameters(im: Any) -> dict[str, float]:
    """The recording's own object parameters (reset, then read)."""
    current = read_parameters(im)
    im.reset_object_parameters()
    defaults = read_parameters(im)
    write_parameters(im, current)
    return defaults


def _unclamped(kelvin: np.ndarray) -> np.ndarray:
    """Finite pixels that are not on the SDK's clamp plateaus."""
    finite = np.isfinite(kelvin)
    if not finite.any():
        return finite
    low, high = kelvin[finite].min(), kelvin[finite].max()
    return finite & ~np.isclose(kelvin, low, atol=1e-3) & ~np.isclose(kelvin, high, atol=1e-3)


def _max_error(calibration: Calibration, params: MeasurementParameters, counts, kelvin) -> float:
    predicted = object_temperature(calibration, params, counts)
    good = np.isfinite(predicted)
    if not good.any():
        return float("inf")
    return float(np.max(np.abs(predicted[good] - kelvin[good])))


def _parameter_checks(im, calibration, index, counts) -> list[tuple[str, float, int]]:
    results = []
    set_unit_safely(im, fnv.Unit.TEMPERATURE_FACTORY, fnv.TempType.KELVIN)
    for label, params in _CHECKS:
        write_parameters(im, sdk_values(params))
        sdk = read_array(im, index).astype(np.float64)
        predicted = object_temperature(calibration, params, counts)
        good = _unclamped(sdk) & np.isfinite(predicted)
        # The SDK clamps object temperatures; compare where it did not.
        error = float(np.max(np.abs(predicted[good] - sdk[good]))) if good.any() else float("inf")
        results.append((label, error, int(good.sum())))
    return results


def _sdk_limits(im: Any, fff: CameraInfo | None) -> RangeLimits:
    """Calibrated range from the active preset when the file has no FFF record."""
    if fff is not None:
        return fff.calibration().limits
    for preset in getattr(im.source_info, "preset_info", ()) or ():
        if getattr(preset, "available", False) and getattr(preset, "calibrated", False):
            low, high = float(preset.min_temp), float(preset.max_temp)
            if 0 < low < high:
                return RangeLimits(calibrated=(low, high))
    return RangeLimits()
