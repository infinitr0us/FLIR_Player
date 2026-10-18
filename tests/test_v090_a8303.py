# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""0.9.0: A8303sc recordings (superframing ATS) with temperatures.

A fake handle follows what the 1006a A8303 file showed (local/notes/2026-10-18-a8303-answers): counts →
radiance by a quadratic; the SDK's radiance unit has the window and atmosphere taken out; temperatures follow
R / (exp(B/T) − 1) of the radiance, with the object parameters applied in radiance space and the camera's own
transmission constants; frames cycle through presets.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import openpyxl
import pytest

from flir_player.calibration import RADIANCE_SCALE, derive_calibration, sdk_values
from flir_player.excel_export import ExportOptions, SourceSpec, TcPrefill, run_export
from flir_player.models import RoiShape
from flir_player.radiometry import (
    UNIT_TRANSMISSION,
    AtmosphereConstants,
    Calibration,
    MeasurementParameters,
    PlanckConstants,
    RangeLimits,
    compensated_signal,
    fit_atmosphere,
    fit_radiance_curve,
    object_temperature,
)

A8303_ATMOSPHERE = AtmosphereConstants(X=0.0932162, alpha1=0.2014372, alpha2=0.00506231, beta1=0.00801709,
                                       beta2=0.00281940)
FILE_PARAMS = MeasurementParameters(emissivity=0.95, transmission=None, distance_m=3.3, humidity=0.3)
# per preset (1, 2): calibrated range (K), radiance curve (SDK units) and counts → radiance quadratic
PRESETS = {1: ((523.15, 873.15), PlanckConstants(19.104868, 3632.4014, 1.0, 0.0), (2.252e-10, 2.1435e-05, -0.04248)),
           2: ((773.15, 1473.15), PlanckConstants(22.873647, 3788.5186, 1.0, 0.0), (1.1e-10, 1.9e-05, -0.031))}


class _A8303Like:
    """A superframing ATS handle: frame i has preset 1 + i % 2, 2 frames per second."""

    width = height = 8
    num_frames = 120

    def __init__(self, scene=None, clock_gap: float = 0.0):
        import fnv
        self.fnv = fnv
        self.unit, self.temp_type = fnv.Unit.COUNTS, fnv.TempType.KELVIN
        self.supported_units = [fnv.Unit.COUNTS, fnv.Unit.RADIANCE_FACTORY, fnv.Unit.TEMPERATURE_FACTORY]
        self.reset_object_parameters()
        info = [NS(available=False, calibrated=False, num_frames=0, min_temp=0.0, max_temp=0.0)]
        for preset, ((low, high), _curve, _quad) in PRESETS.items():
            info.append(NS(available=True, calibrated=True, min_temp=low, max_temp=high, frame_rate_valid=True,
                           frame_rate=54.2, num_frames=self.num_frames // 2))
        self.source_info = NS(camera_model="A8303sc", preset_info=info)
        # each pixel's scene temperature (K) per frame: a module heating up, a little different per pixel
        self.scene = scene if scene is not None else (
            lambda i: np.linspace(560.0, 840.0, 64).reshape(8, 8) + 30.0 * np.sin(i / 7.0))
        self.clock_gap = clock_gap  # seconds the clock jumps after frame 60 (dropped frames)
        self.get_frame(0)

    def get_preset(self, index):
        return 1 + index % 2

    def reset_object_parameters(self):
        self.object_parameters = NS(**sdk_values(FILE_PARAMS), atmospheric_transmission=1.0)

    def _curve(self, preset):
        _range, curve, _quad = PRESETS[preset]
        return Calibration(planck=curve, atmosphere=A8303_ATMOSPHERE)

    def counts_for(self, index):
        _range, curve, (a2, a1, a0) = PRESETS[self.get_preset(index)]
        radiance = curve.signal(self.scene(index))  # what reaches the camera (window and air left out)
        return (-a1 + np.sqrt(a1 * a1 - 4 * a2 * (a0 - radiance))) / (2 * a2)

    def get_frame(self, index):
        fnv = self.fnv
        self.index = index
        seconds = index / 2.0 + (self.clock_gap if index >= 60 else 0.0)
        self.frame_info = NS(time=datetime(2026, 10, 6, 10, 47, 59) + timedelta(seconds=seconds))
        preset = self.get_preset(index)
        counts = self.counts_for(index)
        a2, a1, a0 = PRESETS[preset][2]
        radiance = a2 * counts * counts + a1 * counts + a0
        params = MeasurementParameters.from_sdk(vars(self.object_parameters))
        cal = self._curve(preset)
        if self.unit == fnv.Unit.COUNTS:
            self.final = counts
        elif self.unit == fnv.Unit.RADIANCE_FACTORY:
            self.final = compensated_signal(cal, params, radiance)
        else:
            self.final = object_temperature(cal, params, radiance)
        self.status = np.zeros((8, 8), np.int8)

    def update_frame(self):
        self.get_frame(self.index)

    def has_unit(self, unit):
        return unit in self.supported_units

    def close(self):
        pass


@pytest.fixture
def ats(tmp_path) -> Path:
    path = tmp_path / "1006_A8303sc.ats"
    path.write_bytes(b"synthetic, no FFF record")
    return path


# --- radiometry -------------------------------------------------------------------------------------


def test_the_atmosphere_fit_recovers_a_cameras_constants() -> None:
    grid = [(d, h, t) for d in (1.0, 3.0, 10.0, 30.0, 100.0) for h in (0.0, 0.5, 0.9) for t in (283.15, 303.15)]
    for constants in (A8303_ATMOSPHERE, AtmosphereConstants()):
        tau = [constants.transmission(*point) for point in grid]
        fitted, error = fit_atmosphere(*zip(*grid), tau)
        assert error < 1e-7
        for d, h, t in ((3.3, 0.3, 293.15), (50.0, 0.7, 298.15), (0.5, 0.0, 280.0)):
            assert fitted.transmission(d, h, t) == pytest.approx(constants.transmission(d, h, t), abs=1e-6)
    unit, error = fit_atmosphere(*zip(*grid), [1.0] * len(grid))
    assert unit == UNIT_TRANSMISSION and error == 0.0


def test_the_radiance_curve_has_two_constants() -> None:
    curve = PlanckConstants(1.9e6, 3632.4, 1.0, 0.0)
    kelvin = np.linspace(520.0, 900.0, 200)
    fitted = fit_radiance_curve(kelvin, curve.signal(kelvin))
    assert (fitted.F, fitted.O) == (1.0, 0.0)
    assert fitted.R == pytest.approx(curve.R, rel=1e-9) and fitted.B == pytest.approx(curve.B, rel=1e-9)
    # exact far below the fitted temperatures (the reflected and air terms are evaluated there)
    assert float(fitted.signal(293.15)) == pytest.approx(float(curve.signal(293.15)), rel=1e-7)


# --- calibration -------------------------------------------------------------------------------------


def test_the_radiance_route_verifies_an_a8303_preset(ats) -> None:
    pytest.importorskip("fnv")
    im = _A8303Like()
    frames = list(range(0, 120, 2))[::6]  # preset 1
    report = derive_calibration(im, ats, indices=frames, limits=RangeLimits(calibrated=PRESETS[1][0]))
    assert report.verified, report.message
    assert report.signal == "radiance" and "radiance" in report.message
    assert max(error for _label, error, _n in report.checks) < 1e-3
    assert any("own parameters" in label for label, _e, _n in report.checks)
    counts = im.counts_for(0)
    a2, a1, a0 = PRESETS[1][2]
    assert np.allclose(report.to_signal(counts), (a2 * counts ** 2 + a1 * counts + a0) * RADIANCE_SCALE, rtol=1e-6)
    tau = report.calibration.atmosphere.transmission(3.3, 0.3, 293.15)
    assert tau == pytest.approx(A8303_ATMOSPHERE.transmission(3.3, 0.3, 293.15), abs=1e-6)
    # temperatures at the recording's own parameters are the SDK's
    im.reset_object_parameters()
    im.unit = im.fnv.Unit.TEMPERATURE_FACTORY
    im.get_frame(4)
    ours = object_temperature(report.calibration, FILE_PARAMS, report.to_signal(im.counts_for(4)))
    assert np.max(np.abs(ours - im.final)) < 1e-3
    # without a range the counts route is tried first and the report says why it failed too
    assert derive_calibration(_A8303Like(), ats, indices=frames).signal == "radiance"
    assert derive_calibration(_A8303Like(), ats, indices=frames, signal="counts").signal == "counts"


# --- export --------------------------------------------------------------------------------------------


def _export(tmp_path, ats, im, *, preset=None, prefill=None, ignition=10, options=None) -> openpyxl.Workbook:
    out = tmp_path / f"a8303_{preset}.xlsx"
    spec = SourceSpec(ats, (RoiShape(1, "cursor", ((3.5, 3.5),), "Cell 1"),
                            RoiShape(2, "rect", ((1.0, 1.0), (4.0, 4.0)), "Cell 2")),
                      ignition_frame=ignition, preset=preset)
    options = options or ExportOptions(step_s=1.0, tc_prefill=prefill)
    message = run_export(out, [spec], options, handles={ats.resolve(): im}, replace=[out])
    assert "1/1 recording(s) with live temperatures" in message
    return openpyxl.load_workbook(out, data_only=True)


def test_the_export_gives_an_a8303_preset_temperatures(tmp_path, ats) -> None:
    pytest.importorskip("fnv")
    book = _export(tmp_path, ats, _A8303Like(), preset=2)
    frames = [row[1] for row in book["Counts"].iter_rows(min_row=6, values_only=True) if row[1] is not None]
    assert frames and all(1 + (frame - 1) % 2 == 2 for frame in frames)  # preset 2 only
    largest = next(row for row in book["Validation"].iter_rows(values_only=True)
                   if row[0] == "Largest |difference| over checked samples (K)")
    assert largest[3] < 1e-3 and largest[4].startswith("PASS")
    headers = list(book["Counts"].iter_rows(min_row=5, max_row=5, values_only=True))[0]
    assert "Value (signal)" in headers and "Mean (signal)" in headers
    source = {row[0]: row[1] for row in book["Source"].iter_rows(values_only=True) if row[0]}
    assert source["Preset"].startswith("2 (500 to 1200 °C)") and "quadratic of the counts" in source["Signal"]
    assert list(book["Settings"].iter_rows(min_row=5, max_row=5, values_only=True))[0][1] == "Leave them blank (#N/A)"


def test_out_of_range_rows_start_blank(tmp_path, ats) -> None:
    pytest.importorskip("fnv")
    cold = _A8303Like(scene=lambda i: np.full((8, 8), 450.0 if i < 60 else 700.0) + np.arange(64).reshape(8, 8))
    book = _export(tmp_path, ats, cold, preset=1, ignition=0)
    rows = [row for row in book["Data"].iter_rows(min_row=6, values_only=True) if row[0] is not None]
    early = [row for row in rows if row[0] < 25]
    late = [row for row in rows if row[0] > 35]
    assert early and all(row[2] == "#N/A" for row in early)  # 177 °C, below preset 1's 250 °C
    assert late and all(isinstance(row[2], float) for row in late)


def test_a_frame_timed_tc_sheet_moves_onto_the_clock(tmp_path, ats) -> None:
    """The dialog places the TC readings by frame number / rate; the recording's clock gains 4 s after
    frame 60 (dropped frames), so at ignition frame 80 the axis is 4 s later than 80 / 2 fps."""
    pytest.importorskip("fnv")
    prefill = TcPrefill(times=(0.0, 1.0, 2.0), columns=((20.0, 21.0, 22.0),), names=("T1",),
                        roi_tc=(("Cell 1", "T1"),), offset_s=-30.0, window=(-30.0, -28.0),
                        roi_use=(("Cell 1", ((-30.0, -29.0),)),), frame=80, fps=2.0)
    book = _export(tmp_path, ats, _A8303Like(clock_gap=4.0), preset=1, prefill=prefill, ignition=80)
    offset = next(row[1] for row in book["TC Compare"].iter_rows(values_only=True)
                  if row[0] == "Logger time offset (s)")
    assert offset == pytest.approx(-30.0 + 80 / 2.0 - (80 / 2.0 + 4.0))


# --- player ----------------------------------------------------------------------------------------------


def test_the_player_reads_one_preset(ats, monkeypatch) -> None:
    pytest.importorskip("fnv")
    from flir_player import source as source_module
    from flir_player.processing import ProcessingState

    fake = _A8303Like()
    monkeypatch.setattr(source_module.fnv.file, "ImagerFile", lambda _path: fake)
    fake.rois = []
    fake.can_change_object_parameters = True
    fake.has_nuc = fake.has_bp = False
    src = source_module.FlirVideoSource()
    metadata = src.open(ats)
    assert [p.index for p in metadata.presets] == [1, 2] and src.preset == 1
    assert [src.read_frame(i).index for i in (0, 1, 2, 3, 119)] == [0, 2, 2, 4, 118]
    src.set_preset(2)
    assert [src.read_frame(i).index for i in (0, 1, 118)] == [1, 1, 119]
    assert src.preset_frames(0, 7) == [1, 3, 5, 7]
    src.set_processing(ProcessingState(temporal=("mean", 3)))
    assert src._earlier(9, 2) == [5, 7] and src._following(9) == 11
    src.set_preset(None)
    assert src.read_frame(1).index == 1
    with pytest.raises(ValueError, match="Preset 3"):
        src.set_preset(3)
