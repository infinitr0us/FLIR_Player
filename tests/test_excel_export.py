# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Excel export against the File SDK and the sample recordings."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

openpyxl = pytest.importorskip("openpyxl")

from conftest import wait_until  # noqa: E402
from flir_player.excel_export import ExportOptions, SourceSpec, load_roi_set, roi_set_path, run_export  # noqa: E402
from flir_player.models import RoiShape  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"
ROIS = (
    RoiShape(1, "cursor", ((320.5, 240.5),), "Spot"),
    RoiShape(2, "ellipse", ((300.0, 220.0), (312.0, 232.0)), "Circle"),
    RoiShape(3, "rect", ((200.0, 150.0), (260.0, 200.0)), "Box"),
)


def _open(name: str):
    import fnv.file
    return fnv.file.ImagerFile(str(SAMPLES / name))


def test_calibration_is_verified_and_the_handle_restored() -> None:
    import fnv
    from flir_player.calibration import derive_calibration, read_parameters, set_unit_safely

    im = _open("2.seq")
    set_unit_safely(im, fnv.Unit.TEMPERATURE_FACTORY, fnv.TempType.FAHRENHEIT)
    params = read_parameters(im)
    report = derive_calibration(im, SAMPLES / "2.seq")
    assert report.verified, report.message
    assert report.fff_error_k < 1e-3 and all(error < 1e-3 for _, error, _ in report.checks)
    assert report.calibration.planck.B == 1403.5
    assert im.unit == fnv.Unit.TEMPERATURE_FACTORY and im.temp_type == fnv.TempType.FAHRENHEIT
    assert read_parameters(im) == params
    im.close()
    im = _open("1.ats")
    report = derive_calibration(im, SAMPLES / "1.ats")
    assert not report.verified and report.kind == "none"
    im.close()


def test_set_unit_safely_does_not_reassign_an_unchanged_scale() -> None:
    fnv = pytest.importorskip("fnv")
    from flir_player.calibration import set_unit_safely

    class Handle:
        unit = fnv.Unit.COUNTS
        _temp = fnv.TempType.CELSIUS
        assignments = 0

        @property
        def temp_type(self):
            return self._temp

        @temp_type.setter
        def temp_type(self, value):
            self.assignments += 1
            self._temp = value

    handle = Handle()
    set_unit_safely(handle, fnv.Unit.TEMPERATURE_USER, fnv.TempType.CELSIUS)
    assert handle.assignments == 0 and handle.unit == fnv.Unit.TEMPERATURE_USER
    set_unit_safely(handle, fnv.Unit.TEMPERATURE_USER, fnv.TempType.KELVIN)
    assert handle.assignments == 1


def test_export_reproduces_the_sdk_and_restores_a_lent_handle(tmp_path) -> None:
    from flir_player.source import FlirVideoSource

    source = FlirVideoSource()
    source.open(SAMPLES / "2.seq")
    source.set_rois(ROIS)
    source.set_unit("temperature_factory_c")
    source.apply_object_parameters({"emissivity": 0.9, "reflected_temp": 310.0})
    before = source.read_frame(40)
    from flir_player.source import OBJECT_PARAMETER_FIELDS

    def settable():
        values = source.read_object_parameters()
        return {name: values[name] for name in OBJECT_PARAMETER_FIELDS}

    params = settable()
    dest = tmp_path / "seq.xlsx"
    ticks = []
    message = run_export(dest, [SourceSpec(SAMPLES / "2.seq", ROIS, ignition_frame=600)],
                         ExportOptions(step_s=5.0, validation_rows=8, extra_stats=("median",)),
                         handles={source.metadata.path.resolve(): source.sdk_handle()},
                         progress=lambda done, total: ticks.append((done, total)))
    assert dest.exists() and "live temperatures" in message
    assert ticks and ticks[-1][0] == ticks[-1][1]
    after = source.read_frame(40)
    assert source.unit.key == "temperature_factory_c"
    assert settable() == params
    assert np.array_equal(before.data, after.data)
    source.close()

    values = openpyxl.load_workbook(dest, data_only=True)
    validation = values["Validation"]
    assert validation["D4"].value < 1e-3 and str(validation["E4"].value).startswith("PASS")
    counts = values["Counts"]
    times = [counts.cell(r, 1).value for r in range(6, 30)]
    zero = 6 + times.index(0.0)
    assert counts.cell(zero, 2).value == 601  # 1-based frame of ignition
    statuses = {values["Data"].cell(r, 4).value for r in range(6, 40)}
    assert statuses <= {"OK", "Below range", "Above range", "Saturated", None}


def test_user_calibrated_handles_are_never_borrowed(tmp_path, monkeypatch) -> None:
    import flir_player.excel_export as excel_export

    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError(f"the player's handle was used ({name})")

    # FileSDK 5.0.1 corrupts memory on unit changes of user-calibrated files
    monkeypatch.setattr(excel_export, "user_calibration_risk", lambda im: True)
    dest = tmp_path / "fresh.xlsx"
    rois = (RoiShape(1, "cursor", ((320.5, 240.5),), "Spot"),)
    run_export(dest, [SourceSpec(SAMPLES / "2.seq", rois, ignition_frame=5)], ExportOptions(step_s=20.0),
               handles={(SAMPLES / "2.seq").resolve(): Untouchable()})
    assert dest.exists()


def test_counts_only_recording_exports_counts(tmp_path) -> None:
    dest = tmp_path / "ats.xlsx"
    rois = (RoiShape(1, "cursor", ((100.5, 100.5),), "Spot"),)
    message = run_export(dest, [SourceSpec(SAMPLES / "1.ats", rois, ignition_frame=10)],
                         ExportOptions(step_s=2.0))
    assert "0/1 recording(s) with live temperatures" in message
    book = openpyxl.load_workbook(dest)
    assert "Validation" not in book.sheetnames
    assert book["Data"].cell(6, 4).value in ("Counts only", "Saturated")
    assert isinstance(book["Counts"].cell(6, 4).value, (int, float))


def test_clip_mask_marks_high_saturation() -> None:
    import fnv
    from flir_player.source import FlirVideoSource

    im = _open("3.csq")
    im.unit = fnv.Unit.TEMPERATURE_FACTORY
    n = int(im.num_frames)
    for index in np.linspace(n // 8, n - 1, 40).astype(int):  # a frame with saturated pixels
        im.get_frame(int(index))
        status = np.array(im.status, copy=True).reshape((int(im.height), int(im.width)))
        if ((status & 4) != 0).any():
            break
    im.close()
    high = (status & 4) != 0
    assert high.any(), "the sample should contain saturated pixels"
    source = FlirVideoSource()
    source.open(SAMPLES / "3.csq")
    source.set_unit("temperature_factory_c")
    packet = source.read_frame(int(index))
    source.close()
    assert packet.clip_mask is not None and np.all(packet.clip_mask[high])
    assert int(packet.clip_mask.sum()) == int(((status & 6) != 0).sum())


def test_dialog_parameters_and_estimate(qapp) -> None:
    from flir_player.excel_dialog import ExcelExportDialog
    from flir_player.source import FlirVideoSource

    source = FlirVideoSource()
    metadata = source.open(SAMPLES / "2.seq")
    source.close()
    dialog = ExcelExportDialog(metadata, ROIS, current_frame=500, ignition_frame=None)
    assert dialog.ignition_spin.value() == 501
    dialog.start_check.setChecked(True)
    dialog.start_spin.setValue(-10.0)
    dialog.median_check.setChecked(True)
    dialog.sheet_checks["charts"].setChecked(False)
    params = dialog.parameters()
    assert params["kind"] == "excel" and params["dest"].endswith(".xlsx")
    options = params["options"]
    assert options.start_s == -10.0 and options.end_s is None and options.extra_stats == ("median",)
    assert "charts" not in options.sheets and "tc" in options.sheets
    assert params["sources"][0]["ignition_frame"] == 500 and params["sources"][0]["current"]
    assert "rows" in dialog.estimate_label.text()
    dialog.close()


def test_player_exports_a_workbook_and_saves_the_roi_set(qapp, tmp_path, monkeypatch) -> None:
    import shutil

    from flir_player.excel_dialog import ExcelExportDialog
    from flir_player.main_window import MainWindow
    from PySide6.QtWidgets import QFileDialog

    recording = tmp_path / "2.seq"
    try:
        (tmp_path / "2.seq").hardlink_to(SAMPLES / "2.seq")
    except OSError:
        shutil.copy2(SAMPLES / "2.seq", recording)
    window = MainWindow()
    window.show()
    finished = []
    window.decoder.export_finished.connect(lambda ok, message: finished.append((ok, message)))
    try:
        window.open_path(recording)
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy, 30)
        window._add_roi("cursor", ((320.5, 240.5),))
        window._add_roi("ellipse", ((300.0, 220.0), (312.0, 232.0)))
        window.seek_to(700)
        assert wait_until(qapp, lambda: window.current_packet.index == 700)
        dest = tmp_path / "player.xlsx"

        def fake_exec(dialog):
            dialog.path_edit.setText(str(dest))
            dialog.step_spin.setValue(10.0)
            return ExcelExportDialog.DialogCode.Accepted

        monkeypatch.setattr(ExcelExportDialog, "exec", fake_exec)
        window._open_excel_dialog()
        assert wait_until(qapp, lambda: bool(finished), 120), "export did not finish"
        assert finished[0][0], finished[0][1]
        assert dest.exists()
        sidecar = roi_set_path(recording)
        roi_set = load_roi_set(sidecar)
        assert roi_set.ignition_frame == 700 and len(roi_set.rois) == 2
        # the player keeps working on the lent handle afterwards
        window.seek_to(10)
        assert wait_until(qapp, lambda: window.current_packet.index == 10 and not window._busy)
        # loading the ROI set replaces the ROIs
        window._clear_rois()
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(sidecar), ""))
        window._load_roi_set()
        assert [shape.name for shape in window._rois] == [shape.name for shape in roi_set.rois]
    finally:
        window.close()
        qapp.processEvents()
