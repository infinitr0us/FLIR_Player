# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regressions for the 2026-09-22 comprehensive review of v0.4.1 (N01–N17)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt, QEvent
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractSlider, QColorDialog, QDialog

from conftest import wait_until
from flir_player import main_window as main_window_module
from flir_player.analysis import value_domain, value_transition
from flir_player.export import StatsCsvWriter, stats_csv_header, stats_csv_row
from flir_player.main_window import MainWindow
from flir_player.models import FramePacket, RoiShape, RoiStats, UnitOption, VideoMetadata
from flir_player.processing import ProcessingState
from flir_player.render import (
    CUSTOM_PALETTE_STOPS,
    load_custom_palettes,
    palette_names,
    register_custom_palette,
    unregister_custom_palette,
)
from flir_player.source import FlirVideoSource
from flir_player.widgets import PaletteEditorDialog, ReferenceDialog, ThermalCanvas, TransportBar

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def player(qapp):
    window = MainWindow()
    window.show()
    window.open_path(ROOT / "local/data/2.seq")
    assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
    yield window
    window.close()
    assert wait_until(qapp, lambda: not window.decoder.isRunning())


def _settle(qapp, window, revision):
    assert wait_until(qapp, lambda: not window._busy and window.current_packet.revision != revision)


def _metadata(num_frames=100, path=Path("C:/recordings/a.seq")) -> VideoMetadata:
    return VideoMetadata(path=path, width=64, height=48, num_frames=num_frames, start_time=None,
                         end_time=None, duration_seconds=num_frames / 30.0, nominal_fps=30.0)


# --- N01: value controls follow the numerical domain, not every revision ------------------


def test_n01_value_transition_rules():
    plain, spatial = ProcessingState(), ProcessingState(spatial=("average", 3))
    assert value_domain(plain) == value_domain(spatial)
    assert value_domain(plain) == value_domain(ProcessingState(temporal=("average", 5)))
    assert value_transition("counts", plain, "counts", spatial) == (1.0, 0.0)
    for changed in (ProcessingState(point=("offset", 10)), ProcessingState(file_op="subtract"),
                    ProcessingState(file_op="multiply"), ProcessingState(temporal=("subtract", 3))):
        assert value_transition("counts", plain, "counts", changed) is None
    assert value_transition("temperature_factory_c", plain, "temperature_factory_f", plain) == \
        pytest.approx((1.8, 32.0))
    assert value_transition("counts", plain, "temperature_factory_c", plain) is None


def test_n01_thresholds_survive_domain_preserving_changes(player, qapp):
    player._change_unit("temperature_factory_c")
    assert wait_until(qapp, lambda: player.current_packet.unit.key.endswith("_c") and not player._busy)
    data = player.current_packet.data
    threshold = float(np.percentile(data, 90))
    seg = (float(np.percentile(data, 10)), threshold)
    player._change_isotherm("above", 0.0, 0.0)  # first enable seeds, then the user's value
    player._change_isotherm("above", threshold, threshold)
    player._change_segmentation(True, *seg)
    player._change_range_mode("fixed")
    player._change_fixed_range(1.0, 7.0)
    for action in (lambda: player._apply_object_parameters({"emissivity": 0.9}),
                   lambda: player._change_filters({"spatial": ("average", 3)})):
        revision = player.current_packet.revision
        action()
        _settle(qapp, player, revision)
        assert player.iso_limit1 == threshold
        assert (player.seg_min, player.seg_max) == seg
        assert (player.fixed_minimum, player.fixed_maximum) == (1.0, 7.0)
    # A point filter changes the domain: re-seed like first enabling, never at the minimum.
    revision = player.current_packet.revision
    player._change_filters({"spatial": ("average", 3), "point": ("offset", 10)})
    _settle(qapp, player, revision)
    packet = player.current_packet
    assert player.iso_limit1 == pytest.approx((packet.minimum + packet.maximum) / 2)
    assert (player.seg_min, player.seg_max) == (packet.minimum, packet.maximum)
    assert (player.fixed_minimum, player.fixed_maximum) == (packet.minimum, packet.maximum)
    assert np.mean(packet.data >= player.iso_limit1) < 1.0


def test_n01_n05b_unit_change_overtaken_by_a_step_still_converts(player, qapp):
    player._change_unit("temperature_factory_c")
    assert wait_until(qapp, lambda: player.current_packet.unit.key.endswith("_c") and not player._busy)
    player._change_segmentation(True, 2.0, 5.0)
    player._change_fixed_range(2.0, 5.0)
    index = player.current_packet.index
    player._change_unit("temperature_factory_f")
    player.step_frames(1)  # supersedes the unit acknowledgement's request id
    assert wait_until(qapp, lambda: player.current_packet.unit.key.endswith("_f")
                      and player.current_packet.index == index + 1 and not player._busy)
    assert (player.seg_min, player.seg_max) == pytest.approx((35.6, 41.0))
    assert (player.fixed_minimum, player.fixed_maximum) == pytest.approx((35.6, 41.0))


# --- N02: statistics CSV columns and degenerate ROIs --------------------------------------


def _stats(roi_id, mean):
    return RoiStats(roi_id, f"Box {roi_id}", "rect", mean - 1, mean + 1, mean, 0.5, 10, mean, None, None)


def test_n02_csv_columns_are_matched_by_roi_id():
    unit = UnitOption("counts", "Counts", "counts")
    packet = FramePacket(index=0, data=np.zeros((2, 2), np.uint16), timestamp=None, unit=unit,
                         minimum=0, maximum=0, mean=0, roi_stats=(_stats(2, 50.0), _stats(3, 70.0)))
    rois = tuple(RoiShape(i, "rect", ((0, 0), (1, 1)), f"Box {i}") for i in (1, 2, 3))
    header = stats_csv_header([shape.name for shape in rois])
    row = stats_csv_row(packet, "Counts", rois=rois)
    assert len(row) == len(header)
    cells = dict(zip(header, row))
    assert cells["Box 1 mean"] == "" and cells["Box 2 mean"] == "50" and cells["Box 3 mean"] == "70"
    assert cells["encoding"] == "RGB display"


def test_n02_degenerate_rois_are_never_created(player, qapp):
    player._add_roi("rect", ((10.0, 10.2), (50.0, 10.4)))  # rounds to zero height
    player._add_roi("ellipse", ((20.0, 20.0), (20.3, 40.0)))  # rounds to zero width
    assert player._rois == []
    player._add_roi("rect", ((60.0, 60.0), (90.0, 90.0)))
    assert wait_until(qapp, lambda: len(player.current_packet.roi_stats) == 1 and not player._busy)
    original = player._rois[0]
    player._replace_roi(RoiShape(original.id, "rect", ((60.0, 60.0), (60.2, 90.0)), original.name))
    assert player._rois == [original] and player.canvas._rois == [original]


# --- N08: the cursor readout follows the data under a still pointer ------------------------


def test_n08_probe_follows_frames_flips_and_tools(qapp):
    canvas = ThermalCanvas()
    canvas.setMinimumSize(1, 1)
    canvas.resize(200, 100)
    canvas.show()
    emitted = []
    canvas.probe_changed.connect(emitted.append)
    rgb = np.zeros((10, 20, 3), np.uint8)
    canvas.set_frame(rgb, np.arange(200, dtype=float).reshape(10, 20), "counts")
    canvas.grab()
    pos = QPointF(35, 25)  # displayed pixel (3, 2)
    canvas.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, pos, pos, Qt.MouseButton.NoButton,
                                      Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    assert emitted[-1] == (3, 2, 43.0)
    canvas.set_frame(rgb, np.arange(200, dtype=float).reshape(10, 20) * 10, "°C")
    assert emitted[-1] == (3, 2, 430.0)
    # the readout stays live in the drawing tools (2026-09-26 polish)
    canvas.set_roi_tool("rect")
    assert canvas._probe == (3, 2, 430.0)
    pos = QPointF(45, 25)  # displayed pixel (4, 2)
    canvas.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, pos, pos, Qt.MouseButton.NoButton,
                                      Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    assert emitted[-1] == (4, 2, 440.0)
    canvas.close()


# --- N10: palette editor stop selection -------------------------------------------------------


def test_n10_clicked_stop_can_be_recolored_and_removed(qapp, monkeypatch):
    dialog = PaletteEditorDialog(None, name="probe",
                                 stops=[(0.0, (0, 0, 0)), (0.5, (9, 9, 9)), (1.0, (255, 255, 255))])
    dialog.resize(420, 200)
    dialog.show()
    qapp.processEvents()
    strip = dialog.strip
    handle = QPoint(int(strip._pos_to_x(0.0)), strip._strip_rect().bottom() + 5)
    QTest.mouseClick(strip, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, handle)
    assert strip.selected_stop() == 0
    monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **k: QColor(255, 0, 0)))
    dialog._pick_color()
    assert strip.stops()[0][1] == (255, 0, 0)
    middle = QPoint(int(strip._pos_to_x(0.5)), strip._strip_rect().bottom() + 5)
    QTest.mouseClick(strip, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, middle)
    dialog.remove_button.click()
    assert [position for position, _ in strip.stops()] == [0.0, 1.0]
    dialog.close()


# --- N11: timeline clicks, drags and steps always seek -------------------------------------


def test_n11_timeline_click_and_steps_seek(qapp):
    bar = TransportBar()
    bar.resize(1200, 84)
    bar.show()
    bar.set_video(101, 10.0)
    qapp.processEvents()
    seeks = []
    bar.seek_requested.connect(seeks.append)
    slider = bar.slider
    target = QPoint(int(slider._value_to_x(80)), slider.height() // 2)
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, target)
    assert seeks and abs(seeks[-1] - 80) <= 1 and slider.value() == seeks[-1]
    # grabbing the handle off-centre drags it from where it is: no jump
    current = slider.value()
    grab = QPoint(int(slider._value_to_x(current)) + 6, slider.height() // 2)
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, grab)
    assert seeks[-1] == current
    slider.triggerAction(QAbstractSlider.SliderAction.SliderPageStepAdd)
    assert seeks[-1] == slider.sliderPosition() == slider.value()
    bar.close()


# --- N12: an external reference is not capped by the open recording ------------------------


def test_n12_reference_frame_range_follows_the_chosen_file(qapp, tmp_path):
    current = (tmp_path / "open.seq").resolve()
    current.write_bytes(b"")
    dialog = ReferenceDialog(_metadata(2685, current))
    assert dialog.frame_spin.maximum() == 2685
    dialog.path_edit.setText(str(tmp_path / "longer.csq"))
    dialog.frame_spin.setValue(19302)
    assert dialog.parameters()["frame_index"] == 19301
    dialog.path_edit.setText(str(current))
    assert dialog.frame_spin.maximum() == 2685 and dialog.frame_spin.value() == 2685
    dialog.close()


# --- N13: custom palette names ----------------------------------------------------------------


def test_n13_builtin_names_are_reserved_and_renames_retire_the_old_palette(player, monkeypatch):
    with pytest.raises(ValueError, match="built-in"):
        register_custom_palette("Iron", [(0.0, (0, 0, 0)), (1.0, (9, 9, 9))])
    assert palette_names().count("Iron") == 1
    stops = [(0.0, (0, 0, 0)), (1.0, (200, 10, 10))]
    register_custom_palette("Review Original", stops)
    player.inspector.add_palette("Review Original", select=True)

    class Renaming:
        DialogCode = QDialog.DialogCode
        deleted = False

        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def palette_name(self):
            return "Review Renamed"

        def palette_stops(self):
            return stops

    monkeypatch.setattr(main_window_module, "PaletteEditorDialog", Renaming)
    try:
        player._open_palette_editor()
        assert "Review Original" not in CUSTOM_PALETTE_STOPS
        assert player.inspector.palette_combo.findText("Review Original") < 0
        assert player.current_palette == "Review Renamed"
        CUSTOM_PALETTE_STOPS.clear()
        load_custom_palettes()  # what the next start sees
        assert "Review Original" not in CUSTOM_PALETTE_STOPS
        assert "Review Renamed" in CUSTOM_PALETTE_STOPS
    finally:
        for name in ("Review Original", "Review Renamed"):
            unregister_custom_palette(name)


# --- N14 / N16: numerical and I/O robustness ----------------------------------------------------


def test_n14_whole_image_statistics_reduce_in_float64():
    source = FlirVideoSource()
    source.open(ROOT / "local/data/2.seq")
    try:
        source.set_unit("temperature_factory_f")
        source.set_processing(ProcessingState(point=("exp", 1.0)))
        packet = source.read_frame(0)
        finite = packet.data[np.isfinite(packet.data)].astype(np.float64)
        assert np.isfinite(packet.std_dev)
        assert packet.std_dev == pytest.approx(float(finite.std()), rel=1e-9)
        assert packet.mean == pytest.approx(float(finite.mean()), rel=1e-9)
    finally:
        source.close()


def test_n16_stats_writer_closes_its_file_when_the_header_fails(tmp_path, monkeypatch):
    class Failing:
        def writerow(self, _row):
            raise OSError("injected write failure")

    import flir_player.export as export_module
    monkeypatch.setattr(export_module.csv, "writer", lambda _handle: Failing())
    path = tmp_path / "stats.csv"
    with pytest.raises(OSError, match="injected"):
        StatsCsvWriter(path, ["frame"])
    path.unlink()  # Windows refuses this while a handle is still open


# --- N03 / N09: publication without hard links, explicit replacement ------------------------


def _no_hard_links(monkeypatch):
    import errno
    from flir_player import jobs

    def fat_link(_source, destination, *args, **kwargs):  # CreateHardLinkW on FAT/exFAT
        raise OSError(errno.EINVAL, "Incorrect function", str(destination), 1)
    monkeypatch.setattr(jobs.os, "link", fat_link)


def test_n03_publication_works_without_hard_links(tmp_path, monkeypatch):
    from flir_player.jobs import OutputTransaction
    _no_hard_links(monkeypatch)
    existing = tmp_path / "existing.csv"
    existing.write_bytes(b"old")
    with OutputTransaction() as job:
        job.stage(tmp_path / "new.csv").write_bytes(b"new")
        with pytest.raises(FileExistsError):
            job.stage(existing)
        job.stage(existing, replace=True).write_bytes(b"replacement")
        job.commit()
    assert (tmp_path / "new.csv").read_bytes() == b"new"
    assert existing.read_bytes() == b"replacement"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["existing.csv", "new.csv"]


@pytest.mark.parametrize("links", [True, False])
def test_n09_failed_job_restores_replaced_files(tmp_path, monkeypatch, links):
    from flir_player import jobs
    if not links:
        _no_hard_links(monkeypatch)
    existing = tmp_path / "frame.png"
    existing.write_bytes(b"previous export")
    real_publish = jobs._publish_new

    def failing_publish(stage, dest):
        if dest.name == "late.csv":
            raise OSError("injected publication failure")
        real_publish(stage, dest)
    monkeypatch.setattr(jobs, "_publish_new", failing_publish)
    with pytest.raises(OSError, match="injected"):
        with jobs.OutputTransaction() as job:
            job.stage(existing, replace=True).write_bytes(b"new export")
            job.stage(tmp_path / "early.csv").write_bytes(b"early")
            job.stage(tmp_path / "late.csv").write_bytes(b"late")
            job.commit()
    assert existing.read_bytes() == b"previous export"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["frame.png"]


def test_n09_dialogs_ask_before_replacing_and_default_to_new_names(qapp, tmp_path, monkeypatch):
    from flir_player.export_dialogs import ExportImageDialog, ExportMovieDialog, ExportSeriesDialog
    from flir_player.widgets import MessageDialog
    recording = (tmp_path / "rec.seq").resolve()
    metadata = _metadata(30, recording)
    (tmp_path / "rec_frame_00001.png").write_bytes(b"x")
    (tmp_path / "rec.mp4").write_bytes(b"x")
    answers = []
    monkeypatch.setattr(MessageDialog, "question", staticmethod(lambda *a, **k: answers.pop(0)))
    image = ExportImageDialog(metadata, 0)
    movie = ExportMovieDialog(metadata)
    assert Path(image.path_edit.text()).name == "rec_frame_00001 (2).png"
    assert Path(movie.path_edit.text()).name == "rec (2).mp4"
    image.path_edit.setText(str(tmp_path / "rec_frame_00001.png"))
    answers.append(False)
    image.accept()
    assert image.result() != image.DialogCode.Accepted and not image.parameters()["replace"]
    answers.append(True)
    image.accept()
    assert image.result() == image.DialogCode.Accepted and image.parameters()["replace"]
    series = ExportSeriesDialog(metadata)
    series.folder_edit.setText(str(tmp_path))
    series.name_edit.setText("REC")  # Windows names are case-insensitive
    series.start_spin.setValue(1)
    series.end_spin.setValue(3)
    (tmp_path / "rec_00002.png").write_bytes(b"x")
    answers.append(True)
    series.accept()
    assert series.result() == series.DialogCode.Accepted and series.parameters()["replace"]
    assert not answers
    for dialog in (image, movie, series):
        dialog.close()


def test_n09_bitmasks_can_be_replaced_after_confirmation(player, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    from flir_player.widgets import MessageDialog
    player._add_roi("rect", ((10, 10), (50, 50)))
    assert wait_until(qapp, lambda: player.current_packet.roi_stats and not player._busy)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(tmp_path)))
    monkeypatch.setattr(MessageDialog, "question", staticmethod(lambda *a, **k: True))
    results = []
    player.decoder.export_finished.connect(lambda ok, message: results.append((ok, message)))
    for expected in (1, 2):
        player._export_bitmasks()
        assert wait_until(qapp, lambda: len(results) == expected)
    assert results == [(True, "Wrote 1 bitmask file(s)")] * 2
    assert [p.name for p in tmp_path.iterdir()] == ["Box_1_bitmask.png"]


# --- N04: one pixel-coordinate convention for canvas, geometry, flips and export -------------

W4, H4 = 20, 10


def _encoded_canvas(qapp, size, flip_h, flip_v):
    """Canvas whose drawn colours encode the raw pixel shown (R = 10·x, G = 20·y)."""
    raw = np.arange(W4 * H4, dtype=float).reshape(H4, W4)
    rgb = np.zeros((H4, W4, 3), np.uint8)
    rgb[..., 0] = np.arange(W4)[None, :] * 10
    rgb[..., 1] = np.arange(H4)[:, None] * 20
    shown = rgb[:, ::-1] if flip_h else rgb  # the window flips the RGB it hands the canvas
    shown = shown[::-1] if flip_v else shown
    canvas = ThermalCanvas()
    canvas.setMinimumSize(1, 1)
    canvas.resize(*size)
    canvas.show()
    canvas.set_flips(flip_h, flip_v)
    canvas.set_frame(np.ascontiguousarray(shown), raw, "counts")
    image = canvas.grab().toImage()
    drawn = {}
    for y in range(image.height()):
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            drawn[(x, y)] = (round(colour.red() / 10), round(colour.green() / 20))
    return canvas, raw, drawn


@pytest.mark.parametrize("size", [(200, 100), (150, 75)])
@pytest.mark.parametrize("flip_h,flip_v", [(False, False), (True, False), (False, True), (True, True)])
def test_n04_probe_spot_and_box_measure_the_drawn_pixels(qapp, size, flip_h, flip_v):
    from flir_player.geometry import roi_coordinates
    canvas, raw, drawn = _encoded_canvas(qapp, size, flip_h, flip_v)
    # every screen pixel, including those on pixel boundaries
    for (x, y), pixel in drawn.items():
        assert canvas._pixel_at(QPointF(x, y)) == pixel, (x, y)
    cells = {}
    for (x, y), pixel in drawn.items():
        cells.setdefault(pixel, []).append((x + 0.5, y + 0.5))
    centre = {pixel: tuple(np.mean(points, axis=0)) for pixel, points in cells.items()}
    # the probe reads, and is drawn on, the pixel under the pointer
    pos = QPointF(37, 22)
    px, py, value = canvas._probe_at(pos)
    assert (px, py) == drawn[(37, 22)] and value == raw[py, px]
    marker = canvas._image_to_widget(px + 0.5, py + 0.5)
    assert abs(marker.x() - centre[(px, py)][0]) < 1 and abs(marker.y() - centre[(px, py)][1]) < 1
    # a spot measures the pixel it is placed on
    emitted = []
    canvas.roi_drawn.connect(lambda kind, points: emitted.append((kind, points)))
    canvas.set_roi_tool("cursor")
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(37, 22))
    ys, xs = roi_coordinates(RoiShape(1, *emitted[-1], "Spot"), H4, W4)
    assert (int(xs[0]), int(ys[0])) == drawn[(37, 22)]
    # a box contains exactly the pixels whose drawn centres lie inside its outline
    canvas.set_roi_tool("rect")
    start, end = QPoint(22, 13), QPoint(68, 47)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    canvas.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(end), QPointF(end), Qt.MouseButton.LeftButton,
                                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    ys, xs = roi_coordinates(RoiShape(2, *emitted[-1], "Box"), H4, W4)
    inside = {pixel for pixel, (cx, cy) in centre.items() if 22 < cx < 68 and 13 < cy < 47}
    assert set(zip(xs.tolist(), ys.tolist())) == inside
    canvas.close()


@pytest.mark.parametrize("flip_h", [False, True])
def test_n04_export_overlays_mark_the_same_pixel(flip_h):
    from flir_player.compose import ExportOptions, compose_frame
    rgb = np.zeros((48, 64, 3), np.uint8)
    spot = RoiShape(1, "cursor", ((10.5, 20.5),), "Spot")  # pixel (10, 20)
    composed = compose_frame(rgb, ExportOptions(color_bar=False, rois=True, roi_names=False),
                             rois=(spot,), flips=(flip_h, False))
    column = 64 - 1 - 10 if flip_h else 10
    assert tuple(composed[20, column]) == (76, 194, 255)  # ROI_COLORS[1]
    marked = compose_frame(rgb, ExportOptions(color_bar=False, rois=False, markers=True),
                           max_position=(10, 20), flips=(flip_h, False))
    assert tuple(marked[20, column]) == (255, 122, 144)


# --- N05: rejected or superseded state transitions ------------------------------------------


def test_n05a_rejected_changes_put_the_controls_back(player, qapp, monkeypatch):
    from flir_player import source as source_module
    shown = []
    monkeypatch.setattr(MainWindow, "_show_error", staticmethod(lambda _title, message: shown.append(message)))
    inspector = player.inspector
    player._change_unit("temperature_factory_c")  # object parameters are editable here
    assert wait_until(qapp, lambda: player.current_packet.unit.key.endswith("_c") and not player._busy)

    def reject(*_args, **_kwargs):
        raise ValueError("injected rejection")
    monkeypatch.setattr(source_module.FlirVideoSource, "apply_object_parameters", reject)
    emissivity = inspector.params_panel._spins["emissivity"]
    before = emissivity.value()
    emissivity.setValue(0.5)
    emissivity.editingFinished.emit()
    assert wait_until(qapp, lambda: len(shown) == 1 and emissivity.value() == before)
    monkeypatch.setattr(source_module.FlirVideoSource, "set_processing", reject)
    inspector.spatial_combo.setCurrentIndex(inspector.spatial_combo.findData("average"))
    assert wait_until(qapp, lambda: len(shown) == 2 and inspector.spatial_combo.currentData() == "none")
    assert player._filters_state["spatial"][0] == "none"
    # a unit change whose reference reload fails (e.g. the reference lacks the unit)
    params = {"path": str(ROOT / "local/data/2.seq"), "frame_index": 0, "op": "subtract"}
    player._reference_params = params
    rid = player._activate_request()
    player.decoder.request_reference(params, player.current_packet.index, rid)
    assert wait_until(qapp, lambda: player.current_packet.processing.file_op == "subtract" and not player._busy)
    monkeypatch.setattr(source_module.FlirVideoSource, "load_reference", reject)
    inspector.unit_combo.setCurrentIndex(inspector.unit_combo.findData("counts"))
    assert wait_until(qapp, lambda: len(shown) == 3
                      and inspector.unit_combo.currentData() == "temperature_factory_c")
    assert player.current_packet.unit.key == "temperature_factory_c"
    assert player._reference_params == params


def test_n05c_a_superseded_open_cannot_populate_the_window(qapp, monkeypatch):
    import threading
    import time
    from flir_player import source as source_module
    shown = []
    monkeypatch.setattr(MainWindow, "_show_error", staticmethod(lambda _title, message: shown.append(message)))
    original = source_module.FlirVideoSource.open
    entered = threading.Event()

    def open_slowly_or_fail(self, path):
        if Path(path).name == "2.seq":
            entered.set()
            time.sleep(1.0)  # still opening when the user picks another file
            return original(self, path)
        raise ValueError("injected open failure")
    monkeypatch.setattr(source_module.FlirVideoSource, "open", open_slowly_or_fail)
    window = MainWindow()
    window.show()
    try:
        window.open_path(ROOT / "local/data/2.seq")
        assert entered.wait(10)
        window.open_path(ROOT / "local/data/3.csq")
        assert wait_until(qapp, lambda: shown and not window._busy)
        wait_until(qapp, lambda: False, timeout=0.3)
        assert shown == ["ValueError: injected open failure"]
        assert window.metadata is None and window.current_packet is None
    finally:
        window.close()
        assert wait_until(qapp, lambda: not window.decoder.isRunning())


# --- N06: temporal warm-ups reuse work; filter edits do not queue up -------------------------


def test_n06_rereads_and_warm_ups_reuse_processed_frames(monkeypatch):
    from flir_player import source as source_module
    calls = []
    original = source_module.apply_spatial_filter

    def counting(data, name, size):
        calls.append(1)
        return original(data, name, size)
    monkeypatch.setattr(source_module, "apply_spatial_filter", counting)
    state = ProcessingState(spatial=("average", 5), temporal=("average", 5))
    source = FlirVideoSource()
    source.open(ROOT / "local/data/2.seq")
    fresh = FlirVideoSource()
    fresh.open(ROOT / "local/data/2.seq")
    try:
        source.set_processing(state)
        first = source.read_frame(10).data
        assert len(calls) == 5  # four warm-up frames and the frame itself
        calls.clear()
        again = source.read_frame(10, need_metadata=True)  # metadata refetch
        source.set_rois((RoiShape(1, "rect", ((10, 10), (40, 40)), "Box 1"),))
        with_roi = source.read_frame(10)  # ROI edit re-serves the same frame
        assert calls == [] and len(with_roi.roi_stats) == 1
        np.testing.assert_array_equal(again.data, first)
        np.testing.assert_array_equal(with_roi.data, first)
        source.read_frame(11)
        back = source.read_frame(9).data  # seek: frames 6..8 come from the cache
        assert len(calls) == 1 + 1  # frame 11, then only frame 5 for the new window
        fresh.set_processing(state)
        np.testing.assert_array_equal(back, fresh.read_frame(9).data)
    finally:
        source.close()
        fresh.close()


def test_n06_pending_filter_states_coalesce():
    from flir_player.decoder import DecoderThread
    decoder = DecoderThread()  # not started: inspect the command queue only
    decoder.request_filters({"spatial": ("average", 3)}, 0, 1)
    decoder.request_unit("temperature_factory_c", 0, 2)
    decoder.request_filters({"spatial": ("average", 5)}, 0, 3)
    decoder.request_filters({"spatial": ("average", 7)}, 0, 4)
    queued = list(decoder._commands.queue)
    assert [command for command, _ in queued] == ["unit", "filters"]
    assert queued[-1][1] == ({"spatial": ("average", 7)}, 0, 4)


def test_n06_filter_spins_apply_when_committed(qapp):
    from flir_player.widgets import InspectorPanel
    inspector = InspectorPanel()
    for spin in (inspector.point_spin, inspector.spatial_spin, inspector.temporal_spin):
        assert not spin.keyboardTracking()
    inspector.close()


# --- N07: value precision follows the unit's range -------------------------------------------


def test_n07_formatting_follows_the_value_range():
    from flir_player.render import format_spread, format_tick, format_value, span_decimals
    low, high = 0.0029018, 0.0034786  # radiance on the sample SEQ
    ticks = [format_tick(low + f * (high - low), low, high) for f in (0, .2, .4, .6, .8, 1)]
    assert len(set(ticks)) == 6 and ticks[0] == "0.002902"
    assert format_tick(-1e-9, -1.0, 1.0) == "0.00"
    assert span_decimals(204) == 0 and span_decimals(9.0) == 2
    assert format_value(0.0029018489, "radiance") == "0.0029018 radiance"
    assert format_value(97.9383, "") == "97.94"
    assert format_spread(0.0000412, "radiance") == "0.0000412 radiance"
    assert format_spread(4.023, "counts") == "4.02 counts"


def test_n07_radiance_controls_and_raw_csv_keep_precision(player, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    player._change_unit("radiance_factory")
    assert wait_until(qapp, lambda: player.current_packet.unit.key == "radiance_factory" and not player._busy)
    packet = player.current_packet
    inspector = player.inspector
    assert inspector.minimum_spin.value() == pytest.approx(packet.minimum, abs=1e-7)
    assert inspector.maximum_spin.value() == pytest.approx(packet.maximum, abs=1e-7)
    middle = (packet.minimum + packet.maximum) / 2
    inspector.maximum_spin.setValue(middle)
    inspector._fixed_values_edited()
    assert player.fixed_maximum == pytest.approx(middle, abs=1e-7)
    dest = tmp_path / "raw.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(dest), "")))
    player._export("csv")
    loaded = np.loadtxt(dest, delimiter=",", dtype=np.float64)
    np.testing.assert_array_equal(loaded.astype(packet.data.dtype), packet.data)


def test_n07_emissivity_takes_three_decimals(qapp):
    from flir_player.widgets import ObjectParametersPanel
    panel = ObjectParametersPanel()
    panel.set_parameters(dict(emissivity=.95, reflected_temp=293.15, atmosphere_temp=293.15,
                              est_atmospheric_transmission=0.0, distance=1.0, relative_humidity=.5,
                              ext_optics_temp=293.15, ext_optics_transmission=1.0, can_change=True))
    changes = []
    panel.applied.connect(changes.append)
    panel._spins["emissivity"].setValue(0.955)
    panel._spins["emissivity"].editingFinished.emit()
    assert changes == [{"emissivity": pytest.approx(0.955)}]


# --- N15 / N17 -----------------------------------------------------------------------------------


def test_n15_rolling_average_recovers_after_a_huge_frame_leaves():
    from flir_player.processing import TemporalBuffer
    ring = TemporalBuffer()
    for value in (1e20, 1.0, 1.0):
        result = ring.apply(np.full((2, 2), value), "average", 2)
    np.testing.assert_array_equal(result, 1.0)


def test_n17_integer_histograms_use_integer_aligned_bins():
    from flir_player.plots import histogram_bins
    values = np.arange(6445, 6650, dtype=np.uint16)  # one of each Counts value
    counts, edges = np.histogram(values, bins=histogram_bins(values, 128))
    assert np.all(edges % 1 == 0.5)
    assert set(counts[:-1].tolist()) == {2}  # no comb: every full bin holds two integers
    floats = values.astype(np.float32)
    assert histogram_bins(floats, 128) == 128


# --- Follow-ups from the cross-model review of these fixes -----------------------------------


def test_n09_unrestorable_replacement_keeps_the_previous_version(tmp_path, monkeypatch):
    from flir_player import jobs
    existing = tmp_path / "frame.png"
    existing.write_bytes(b"previous export")
    real_replace = jobs.os.replace

    def locked_restore(source, destination):
        if str(source).endswith(".previous"):  # rollback: destination held open elsewhere
            raise PermissionError(13, "The process cannot access the file", str(destination))
        real_replace(source, destination)

    def failing_publish(stage, dest):
        raise OSError("injected publication failure")
    monkeypatch.setattr(jobs.os, "replace", locked_restore)
    monkeypatch.setattr(jobs, "_publish_new", failing_publish)
    with pytest.raises(OSError, match="previous version kept") as raised:
        with jobs.OutputTransaction() as job:
            job.stage(existing, replace=True).write_bytes(b"new export")
            job.stage(tmp_path / "late.csv").write_bytes(b"late")
            job.commit()
    kept = tmp_path / "frame (previous).png"
    assert kept.read_bytes() == b"previous export" and "frame (previous).png" in str(raised.value)
    assert existing.read_bytes() == b"new export"


def test_n09_rollback_never_deletes_a_file_that_replaced_ours(tmp_path, monkeypatch):
    import os
    from flir_player import jobs
    first = tmp_path / "first.png"
    real_publish = jobs._publish_new

    def racing_publish(stage, dest):
        if dest.name == "second.png":  # someone replaces our first output, then we fail
            other = tmp_path / "other.tmp"
            other.write_bytes(b"another program's file")
            os.replace(other, first)
            raise OSError("injected publication failure")
        real_publish(stage, dest)
    monkeypatch.setattr(jobs, "_publish_new", racing_publish)
    with pytest.raises(OSError, match="injected"):
        with jobs.OutputTransaction() as job:
            job.stage(first).write_bytes(b"ours")
            job.stage(tmp_path / "second.png").write_bytes(b"ours too")
            job.commit()
    assert first.read_bytes() == b"another program's file"


def test_n06_a_failed_frame_is_not_served_from_the_previous_window(monkeypatch):
    from flir_player import source as source_module
    state = ProcessingState(spatial=("average", 3), temporal=("average", 3))
    source = FlirVideoSource()
    source.open(ROOT / "local/data/2.seq")
    cold = FlirVideoSource()
    cold.open(ROOT / "local/data/2.seq")
    original = source_module.apply_spatial_filter
    try:
        source.set_processing(state)
        source.read_frame(10)
        failures = [MemoryError("injected")]

        def fail_once(data, name, size):
            if failures:
                raise failures.pop()
            return original(data, name, size)
        monkeypatch.setattr(source_module, "apply_spatial_filter", fail_once)
        with pytest.raises(MemoryError):
            source.read_frame(11)
        retried = source.read_frame(11).data
        cold.set_processing(state)
        np.testing.assert_array_equal(retried, cold.read_frame(11).data)
    finally:
        source.close()
        cold.close()


def test_n05_a_superseded_rejection_leaves_newer_controls_alone(player, qapp, monkeypatch):
    shown = []
    monkeypatch.setattr(MainWindow, "_show_error", staticmethod(lambda _title, message: shown.append(message)))
    player._change_filters({"point": ("offset", 10)})
    older = player._state_request_id
    player._change_filters({"point": ("offset", 20)})
    assert wait_until(qapp, lambda: player.current_packet.processing.point == ("offset", 20)
                      and not player._busy)
    player.inspector.set_processing(player._filters_state)  # the controls show offset 20
    stale = {"unit": "counts", "object_parameters": {}, "corrections": {}, "reference": None,
             "reference_label": "", "processing": {"point": ("none", 1.0), "spatial": ("none", 3),
                                                   "temporal": ("none", 5)}}
    player._on_state_failed(older, "offset 10 rejected", stale)
    assert shown == ["offset 10 rejected"]
    assert player._filters_state["point"] == ("offset", 20)
    assert player.inspector.point_combo.currentData() == "offset"


_DPI_SCRIPT = r'''
import sys
import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication
app = QApplication([])
from flir_player.widgets import ThermalCanvas
width, height = int(sys.argv[1]), int(sys.argv[2])
W, H = 20, 10
raw = np.arange(W * H, dtype=float).reshape(H, W)
rgb = np.zeros((H, W, 3), np.uint8)
rgb[..., 0] = np.arange(W)[None, :] * 10
rgb[..., 1] = np.arange(H)[:, None] * 20
bad = compared = 0
for flip_h, flip_v in ((False, False), (True, True)):
    shown = rgb[:, ::-1] if flip_h else rgb
    shown = shown[::-1] if flip_v else shown
    canvas = ThermalCanvas()
    canvas.setMinimumSize(1, 1)
    canvas.resize(width, height)
    canvas.show()
    canvas.set_flips(flip_h, flip_v)
    canvas.set_frame(np.ascontiguousarray(shown), raw, "counts")
    pixmap = canvas.grab()
    image, ratio = pixmap.toImage(), pixmap.devicePixelRatio()
    for y in range(image.height()):
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            if colour.blue():
                continue  # letterbox background, not image
            drawn = (round(colour.red() / 10), round(colour.green() / 20))
            compared += 1
            if canvas._pixel_at(QPointF(x / ratio, y / ratio)) != drawn:
                bad += 1
print(ratio, image.width(), image.height(), compared, bad)
'''


@pytest.mark.parametrize("scale,size", [("1.25", (160, 80)), ("1.25", (161, 81)), ("1.25", (173, 70)),
                                        ("1.5", (140, 70)), ("1.5", (151, 97)), ("1.75", (120, 60))])
def test_n04_pixel_picking_matches_the_drawn_pixel_at_fractional_display_scaling(tmp_path, scale, size):
    import os
    import subprocess
    import sys
    script = tmp_path / "dpi_probe.py"
    script.write_text(_DPI_SCRIPT, encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=scale,
               PYTHONPATH=str(ROOT))
    result = subprocess.run([sys.executable, str(script), *map(str, size)], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    ratio, device_width, device_height, compared, bad = result.stdout.split()[-5:]
    assert float(ratio) == pytest.approx(float(scale))
    assert abs(int(device_width) - size[0] * float(scale)) < 1 and int(bad) == 0
    assert int(compared) > int(device_width) * int(device_height)  # both flip passes, mostly image


def test_n04_a_full_frame_box_measures_every_pixel(tmp_path):
    from PIL import Image
    from flir_player.geometry import roi_coordinates
    source = FlirVideoSource()
    source.open(ROOT / "local/data/2.seq")
    try:
        width, height = source.metadata.width, source.metadata.height
        shapes = (RoiShape(1, "rect", ((0.0, 0.0), (float(width), float(height))), "Frame"),
                  RoiShape(2, "ellipse", ((0.0, 0.0), (float(width), float(height))), "Oval"))
        source.set_rois(shapes)
        plain = source.read_frame(0)
        for (_, shape, handle), stats in zip(source._roi_handles, plain.roi_stats):
            ys, xs = roi_coordinates(shape, height, width)
            assert stats.num_pixels == ys.size  # SDK statistics, no processing
            path = tmp_path / f"{shape.name}.png"
            assert handle.export_bitmask(str(path))
            with Image.open(path) as image:
                sdk = np.asarray(image).any(axis=2)
            mask = np.zeros((height, width), bool)
            mask[ys, xs] = True
            np.testing.assert_array_equal(mask, sdk)
        assert plain.roi_stats[0].num_pixels == width * height
    finally:
        source.close()


def test_n09_a_changed_replacement_still_keeps_the_users_previous_version(tmp_path, monkeypatch):
    import os
    from flir_player import jobs
    existing = tmp_path / "frame.png"
    existing.write_bytes(b"previous export")
    real_publish = jobs._publish_new

    def racing_publish(stage, dest):  # another program rewrites our replacement, then we fail
        other = tmp_path / "other.tmp"
        other.write_bytes(b"another program's file")
        os.replace(other, existing)
        raise OSError("injected publication failure")
    monkeypatch.setattr(jobs, "_publish_new", racing_publish)
    with pytest.raises(OSError, match="previous version kept"):
        with jobs.OutputTransaction(replace=[existing]) as job:
            job.stage(existing).write_bytes(b"new export")
            job.stage(tmp_path / "late.csv").write_bytes(b"late")
            job.commit()
    assert existing.read_bytes() == b"another program's file"
    assert (tmp_path / "frame (previous).png").read_bytes() == b"previous export"


def test_n09_only_confirmed_files_are_replaced(tmp_path):
    from flir_player import jobs
    confirmed, appeared = tmp_path / "f_00001.png", tmp_path / "f_00002.png"
    confirmed.write_bytes(b"confirmed old")
    with pytest.raises(FileExistsError):
        with jobs.OutputTransaction(replace=[confirmed]) as job:
            job.stage(confirmed).write_bytes(b"new 1")
            job.stage(appeared).write_bytes(b"new 2")
            appeared.write_bytes(b"created while rendering")  # never confirmed
            job.commit()
    assert confirmed.read_bytes() == b"confirmed old"
    assert appeared.read_bytes() == b"created while rendering"


def test_n05_a_rejection_after_a_superseded_success_refreshes_the_frame(player, qapp, monkeypatch):
    import threading
    import time
    from flir_player import source as source_module
    shown = []
    monkeypatch.setattr(MainWindow, "_show_error", staticmethod(lambda _title, message: shown.append(message)))
    original = source_module.FlirVideoSource.set_processing
    started = threading.Event()

    def slow_then_reject(self, state):
        if state.point == ("offset", 20.0):
            raise ValueError("offset 20 rejected")
        started.set()
        time.sleep(0.4)  # offset 10 is still being applied when offset 20 is sent
        original(self, state)
    monkeypatch.setattr(source_module.FlirVideoSource, "set_processing", slow_then_reject)
    player._change_filters({"point": ("offset", 10)})
    assert started.wait(10)
    player._change_filters({"point": ("offset", 20)})  # supersedes offset 10's frame
    assert wait_until(qapp, lambda: shown and not player._busy
                      and player.current_packet.processing.point == ("offset", 10.0))
    assert player._filters_state["point"] == ("offset", 10.0)
    assert player.inspector.point_combo.currentData() == "offset"


def test_n15_per_pixel_cancellation_is_recomputed():
    from flir_player.processing import TemporalBuffer
    ring = TemporalBuffer()
    for frame in ([[1e20, 1e20]], [[1.0, 1e20]], [[1.0, 1e20]]):
        result = ring.apply(np.array(frame), "average", 2)
    np.testing.assert_array_equal(result, [[1.0, 1e20]])
