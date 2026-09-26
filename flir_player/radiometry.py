# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""FLIR's counts → temperature conversion, reproduced outside the File SDK.

The model below reproduces File SDK ``TEMPERATURE_FACTORY`` output (5.0.1 and
2026.1.2 are bit-identical) to
float32 precision (≤ 5e-5 K) on T650sc SEQ/CSQ recordings, for emissivity,
reflected temperature, atmospheric transmission (manual and automatic) and an
external window.

    S(T)   = R / (exp(B / T) − F) − O            calibration curve, T in K
    S1     = (counts − (1 − τw) S(Tw)) / τw       window sits at the camera
    S_obj  = (S1 − (1 − ε) τ S(Tr) − (1 − τ) S(Ta)) / (ε τ)
           = K1 · counts − K2
    T_obj  = B / ln(R / (S_obj + O) + F)

R is PlanckR1 / PlanckR2 and O is PlanckO with FLIR's sign (usually negative).
Automatic transmission uses FLIR's two-band water-vapour model, whose H term
the SDK caps at ``H2O_MAX``.

Because S_obj is linear in counts and T is monotonic in S_obj, the extrema and
percentiles of a region convert exactly from the corresponding counts; its mean
temperature needs the individual pixels.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

KELVIN = 273.15
# The File SDK caps the water-vapour term of the transmission model at this
# value (measured: identical effective H for every humid case, 2026-09-25).
H2O_MAX = 38.894


@dataclass(frozen=True, slots=True)
class PlanckConstants:
    """Factory calibration curve of one camera, lens and range."""

    R: float  # PlanckR1 / PlanckR2
    B: float
    F: float
    O: float  # PlanckO, FLIR sign convention

    def signal(self, kelvin):
        """Calibrated signal (in counts) of a blackbody at ``kelvin``."""
        t = np.asarray(kelvin, dtype=np.float64)
        return self.R / (np.exp(self.B / t) - self.F) - self.O

    def temperature(self, signal):
        """Inverse of :meth:`signal`; NaN where the curve is undefined."""
        s = np.asarray(signal, dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            argument = self.R / (s + self.O) + self.F
            result = self.B / np.log(argument)
        return np.where((s + self.O > 0) & (argument > 1), result, np.nan)


@dataclass(frozen=True, slots=True)
class AtmosphereConstants:
    """FLIR's atmospheric transmission model (camera-specific constants)."""

    X: float = 1.9
    alpha1: float = 0.006569
    alpha2: float = 0.01262
    beta1: float = -0.002276
    beta2: float = -0.00667
    h2o_max: float = H2O_MAX

    def water_vapour(self, humidity: float, atmosphere_k: float) -> float:
        t = float(atmosphere_k) - KELVIN
        h = float(humidity) * np.exp(1.5587 + 0.06939 * t - 2.7816e-4 * t ** 2 + 6.8455e-7 * t ** 3)
        return min(h, self.h2o_max)

    def transmission(self, distance_m: float, humidity: float, atmosphere_k: float) -> float:
        """τ for a path of ``distance_m`` metres; ``humidity`` is a fraction."""
        root_d = np.sqrt(max(0.0, float(distance_m)))
        root_h = np.sqrt(self.water_vapour(humidity, atmosphere_k))
        return float(
            self.X * np.exp(-root_d * (self.alpha1 + self.beta1 * root_h))
            + (1 - self.X) * np.exp(-root_d * (self.alpha2 + self.beta2 * root_h))
        )


@dataclass(frozen=True, slots=True)
class RangeLimits:
    """Validity limits of one calibration, in kelvin (None when unknown).

    ``calibrated`` is the blackbody range the curve was fitted over (FLIR's
    warning range), ``clip`` the range the FLIR software clamps object
    temperatures to, and ``saturated`` the temperatures at the raw-signal
    limits ``raw`` (counts).
    """

    calibrated: tuple[float, float] | None = None
    clip: tuple[float, float] | None = None
    saturated: tuple[float, float] | None = None
    raw: tuple[float, float] | None = None


@dataclass(frozen=True, slots=True)
class Calibration:
    planck: PlanckConstants
    atmosphere: AtmosphereConstants | None = None
    limits: RangeLimits = RangeLimits()
    source: str = ""

    def count_limits(self) -> dict[str, tuple[float, float] | None]:
        """Counts at the calibrated range and at the saturation limits.

        These classify a measurement independently of emissivity: the camera
        either saw a signal inside its calibrated range or it did not.
        """
        limits = self.limits
        calibrated = None
        if limits.calibrated is not None:
            low, high = self.planck.signal(np.array(limits.calibrated))
            calibrated = (float(low), float(high))
        raw = limits.raw
        if raw is None and limits.saturated is not None:
            low, high = self.planck.signal(np.array(limits.saturated))
            raw = (float(low), float(high))
        return {"calibrated": calibrated, "raw": raw}


@dataclass(frozen=True, slots=True)
class MeasurementParameters:
    """Object parameters in plain SI units (temperatures in K, humidity 0–1).

    ``transmission`` None means "compute from distance, humidity and the
    atmosphere temperature" (the File SDK's ``est_atmospheric_transmission``
    of 0).
    """

    emissivity: float = 1.0
    reflected_k: float = 293.15
    atmosphere_k: float = 293.15
    transmission: float | None = 1.0
    distance_m: float = 0.0
    humidity: float = 0.5
    window_transmission: float = 1.0
    window_k: float = 293.15

    @classmethod
    def from_sdk(cls, values: dict) -> "MeasurementParameters":
        """From ``FlirVideoSource.read_object_parameters()``-style values."""
        estimate = float(values.get("est_atmospheric_transmission", 0.0))
        return cls(
            emissivity=float(values["emissivity"]),
            reflected_k=float(values["reflected_temp"]),
            atmosphere_k=float(values["atmosphere_temp"]),
            transmission=estimate if estimate > 0 else None,
            distance_m=float(values.get("distance", 0.0)),
            humidity=float(values.get("relative_humidity", 0.5)),
            window_transmission=float(values.get("ext_optics_transmission", 1.0)),
            window_k=float(values.get("ext_optics_temp", 293.15)),
        )

    def with_(self, **changes) -> "MeasurementParameters":
        return replace(self, **changes)


NEUTRAL = MeasurementParameters()  # blackbody view: ε = τ = τw = 1


def transmission_used(calibration: Calibration, params: MeasurementParameters) -> float:
    if params.transmission is not None:
        return float(params.transmission)
    if params.distance_m <= 0:
        return 1.0
    atmosphere = calibration.atmosphere or AtmosphereConstants()
    return atmosphere.transmission(params.distance_m, params.humidity, params.atmosphere_k)


def coefficients(calibration: Calibration, params: MeasurementParameters) -> tuple[float, float]:
    """(K1, K2) such that the object signal is ``K1 * counts - K2``."""
    signal = calibration.planck.signal
    eps = float(params.emissivity)
    tau = transmission_used(calibration, params)
    tau_w = float(params.window_transmission)
    if not (0 < eps <= 1 and 0 < tau <= 1 and 0 < tau_w <= 1):
        raise ValueError("Emissivity and transmissions must lie in (0, 1]")
    k1 = 1.0 / (eps * tau * tau_w)
    k2 = (
        (1 - tau_w) * float(signal(params.window_k)) / tau_w
        + (1 - eps) * tau * float(signal(params.reflected_k))
        + (1 - tau) * float(signal(params.atmosphere_k))
    ) / (eps * tau)
    return k1, k2


def object_temperature(calibration: Calibration, params: MeasurementParameters, counts, *,
                       clamp: bool = False):
    """Object temperature in K for raw ``counts`` (any array shape).

    ``clamp`` limits the result to the calibration's clip range, as the FLIR
    software does.
    """
    k1, k2 = coefficients(calibration, params)
    kelvin = calibration.planck.temperature(k1 * np.asarray(counts, dtype=np.float64) - k2)
    if clamp and calibration.limits.clip is not None:
        low, high = calibration.limits.clip
        kelvin = np.where(np.isnan(kelvin), kelvin, np.clip(kelvin, low, high))
    return kelvin


def compensated_signal(calibration: Calibration, params: MeasurementParameters, counts):
    """Signal with window and atmosphere removed, before the emissivity step.

    ``S1' = ε S(T_obj) + (1 − ε) S(T_refl)``; the linear form ``A·counts − C``
    is returned as (A, C) when ``counts`` is None.
    """
    signal = calibration.planck.signal
    tau = transmission_used(calibration, params)
    tau_w = float(params.window_transmission)
    a = 1.0 / (tau * tau_w)
    c = ((1 - tau_w) * float(signal(params.window_k)) / tau_w
         + (1 - tau) * float(signal(params.atmosphere_k))) / tau
    if counts is None:
        return a, c
    return a * np.asarray(counts, dtype=np.float64) - c


def matching_emissivity(calibration: Calibration, params: MeasurementParameters, counts, reference_k):
    """Emissivity that makes the IR reading equal ``reference_k`` (e.g. a TC).

    Values above 1 mean no physical emissivity can reconcile the two: the IR
    signal is brighter than a blackbody at the reference temperature would be
    (flames or hot gas in the line of sight, a hotter viewed area, or a
    reference that does not measure the viewed surface).
    """
    signal = calibration.planck.signal
    s1 = compensated_signal(calibration, params, counts)
    s_refl = float(signal(params.reflected_k))
    with np.errstate(divide="ignore", invalid="ignore"):
        return (s1 - s_refl) / (signal(np.asarray(reference_k, dtype=np.float64)) - s_refl)


def count_status(calibration: Calibration, low_counts, high_counts) -> np.ndarray:
    """Status codes per sample from the lowest and highest counts it covers.

    0 = inside the calibrated range, 1 = extrapolated below, 2 = extrapolated
    above, 3 = saturated (at or beyond the raw-signal limits).
    """
    low = np.asarray(low_counts, dtype=np.float64)
    high = np.asarray(high_counts, dtype=np.float64)
    status = np.zeros(np.broadcast(low, high).shape, dtype=np.int8)
    limits = calibration.count_limits()
    if limits["calibrated"] is not None:
        cal_low, cal_high = limits["calibrated"]
        status = np.where(low < cal_low, 1, status)
        status = np.where(high > cal_high, 2, status)
    if limits["raw"] is not None:
        raw_low, raw_high = limits["raw"]
        status = np.where((low <= raw_low) | (high >= raw_high), 3, status)
    return status.astype(np.int8)


STATUS_LABELS = ("OK", "Below range", "Above range", "Saturated")


def fit_planck(kelvin, counts) -> PlanckConstants:
    """Least-squares R, B, F, O from (blackbody temperature, counts) pairs.

    A coarse B grid (the model is linear in R and O for fixed B and F) seeds
    a Levenberg–Marquardt refinement. Needs a spread of temperatures; the
    caller verifies the result.
    """
    t = np.asarray(kelvin, dtype=np.float64).ravel()
    s = np.asarray(counts, dtype=np.float64).ravel()
    if t.size < 4 or np.ptp(t) <= 0:
        raise ValueError("At least four distinct temperatures are needed to fit a calibration")
    best = None
    for b in np.linspace(300.0, 6000.0, 229):
        for f in (0.5, 1.0, 1.5, 2.0):
            g = 1.0 / (np.exp(b / t) - f)
            design = np.column_stack([g, np.ones_like(g)])
            solution, *_ = np.linalg.lstsq(design, s, rcond=None)
            cost = float(np.sum((design @ solution - s) ** 2))
            if np.isfinite(cost) and (best is None or cost < best[0]):
                best = (cost, np.array([solution[0], b, f, -solution[1]]))
    if best is None:
        raise ValueError("Could not seed the calibration fit")
    p = best[1]

    def residual(q):
        return PlanckConstants(*q).signal(t) - s

    r = residual(p)
    damping = 1e-3
    for _ in range(400):
        jacobian = np.empty((t.size, 4))
        for j in range(4):
            h = max(abs(p[j]) * 1e-7, 1e-9)
            q = p.copy()
            q[j] += h
            jacobian[:, j] = (residual(q) - r) / h
        normal = jacobian.T @ jacobian
        step = np.linalg.solve(normal + damping * np.diag(np.diag(normal) + 1e-30), -(jacobian.T @ r))
        q = p + step
        rq = residual(q)
        if np.all(np.isfinite(rq)) and rq @ rq < r @ r:
            converged = np.max(np.abs(step) / np.maximum(np.abs(p), 1e-12)) < 1e-13
            p, r, damping = q, rq, damping / 3
            if converged:
                break
        else:
            damping *= 5
            if damping > 1e12:
                break
    return PlanckConstants(*(float(v) for v in p))
