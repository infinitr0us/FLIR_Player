# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""FLIR's counts → temperature model, SDK-independent parts."""
from __future__ import annotations

import numpy as np
import pytest

from flir_player.radiometry import (
    H2O_MAX,
    AtmosphereConstants,
    Calibration,
    MeasurementParameters,
    PlanckConstants,
    RangeLimits,
    coefficients,
    count_status,
    fit_planck,
    matching_emissivity,
    object_temperature,
    transmission_used,
)

# T650sc s/n 55908418 (FFF CameraInfo of the sample recordings)
T650 = Calibration(
    planck=PlanckConstants(R=15391.119141 / 0.126859, B=1403.5, F=1.6, O=-5781.0),
    atmosphere=AtmosphereConstants(),
    limits=RangeLimits(calibrated=(373.15, 923.15), clip=(273.15, 933.15),
                       saturated=(213.15, 943.15), raw=(5949.0, 48672.0)),
)


def forward_counts(cal: Calibration, kelvin, p: MeasurementParameters):
    """Measured counts for an object at ``kelvin`` (window at the camera)."""
    s = cal.planck.signal
    tau = transmission_used(cal, p)
    object_side = (p.emissivity * tau * s(kelvin) + (1 - p.emissivity) * tau * s(p.reflected_k)
                   + (1 - tau) * s(p.atmosphere_k))
    return p.window_transmission * object_side + (1 - p.window_transmission) * s(p.window_k)


def test_exiftool_convention_smoke_value() -> None:
    # SC660 example constants (Thermimage docs): raw 19852 at blackbody settings → 32.632 °C
    sc660 = Calibration(PlanckConstants(R=21106.77 / 0.012545258, B=1501.0, F=1.0, O=-7340.0))
    kelvin = object_temperature(sc660, MeasurementParameters(), 19852.0)
    assert float(kelvin) - 273.15 == pytest.approx(32.632, abs=1e-3)


def test_round_trip_with_every_parameter() -> None:
    temperatures = np.linspace(280.0, 900.0, 60)
    params = MeasurementParameters(emissivity=0.7, reflected_k=330.0, atmosphere_k=300.0,
                                   transmission=0.93, window_transmission=0.85, window_k=310.0)
    counts = forward_counts(T650, temperatures, params)
    assert np.max(np.abs(object_temperature(T650, params, counts) - temperatures)) < 1e-9


def test_object_signal_is_linear_in_counts() -> None:
    params = MeasurementParameters(emissivity=0.8, reflected_k=320.0, transmission=0.9)
    k1, k2 = coefficients(T650, params)
    counts = np.array([7000.0, 12000.0, 30000.0])
    expected = T650.planck.temperature(k1 * counts - k2)
    assert np.allclose(object_temperature(T650, params, counts), expected)
    # extrema convert exactly: T is monotonic in counts
    assert np.all(np.diff(object_temperature(T650, params, counts)) > 0)


def test_invalid_signal_is_nan_and_parameters_are_checked() -> None:
    assert np.isnan(object_temperature(T650, MeasurementParameters(), 5000.0))  # below the offset
    with pytest.raises(ValueError):
        coefficients(T650, MeasurementParameters(emissivity=0.0))


def test_clamp_follows_the_clip_range() -> None:
    params = MeasurementParameters()
    hot = forward_counts(T650, 1000.0, params)
    cold = forward_counts(T650, 250.0, params)
    assert float(object_temperature(T650, params, hot, clamp=True)) == pytest.approx(933.15)
    assert float(object_temperature(T650, params, cold, clamp=True)) == pytest.approx(273.15)


def test_automatic_transmission_matches_the_sdk_including_the_water_cap() -> None:
    # File SDK 5.0.1 values measured on FLIR2229.csq (sdk_math_probe5)
    atm = T650.atmosphere
    assert atm.transmission(10.0, 0.5, 298.15) == pytest.approx(0.9780569672584534, abs=2e-7)
    assert atm.transmission(100.0, 0.5, 278.15) == pytest.approx(0.95863563068729, abs=2e-7)
    # above the cap the SDK's water term saturates (RH 0.9 at 45 °C)
    assert atm.water_vapour(0.9, 318.15) == H2O_MAX
    assert atm.transmission(100.0, 0.9, 318.15) == pytest.approx(0.8480337, abs=2e-6)
    auto = MeasurementParameters(transmission=None, distance_m=0.0)
    assert transmission_used(T650, auto) == 1.0


def test_matching_emissivity_recovers_the_truth() -> None:
    truth = MeasurementParameters(emissivity=0.83, reflected_k=305.0, transmission=0.97)
    kelvin = np.array([320.0, 450.0, 700.0])
    counts = forward_counts(T650, kelvin, truth)
    guess = truth.with_(emissivity=0.95)  # emissivity itself does not enter the result
    assert np.allclose(matching_emissivity(T650, guess, counts, kelvin), 0.83)
    # a reference colder than the apparent reading needs ε > 1: not an emissivity problem
    assert float(matching_emissivity(T650, guess, counts[2], 400.0)) > 1.0


def test_count_status_is_independent_of_emissivity() -> None:
    limits = T650.count_limits()
    low, high = limits["calibrated"]
    status = count_status(T650, np.array([low - 10, low + 10, high - 10, 20000, 7000]),
                          np.array([low + 20, high - 20, high + 10, 48672, 7500]))
    assert status.tolist() == [1, 0, 2, 3, 1]


def test_fit_recovers_a_calibration_curve() -> None:
    kelvin = np.linspace(290.0, 900.0, 80)
    counts = T650.planck.signal(kelvin)
    fitted = fit_planck(kelvin, counts)
    assert np.max(np.abs(fitted.temperature(counts) - kelvin)) < 1e-3
    with pytest.raises(ValueError):
        fit_planck([300.0, 300.0, 300.0, 300.0], [1.0, 2.0, 3.0, 4.0])
