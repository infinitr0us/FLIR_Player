# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cross-feature regressions for the September whole-package review (F01–F22)."""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import imageio.v2 as imageio
import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QFileDialog

from conftest import wait_until
from flir_player.analysis import threshold_conversion
from flir_player.compose import ExportOptions, compose_frame
from flir_player.decoder import DecoderThread
from flir_player.export import MovieWriter, save_tiff16, save_frame
from flir_player.export_dialogs import ExportImageDialog, ExportSeriesDialog
from flir_player.geometry import roi_coordinates
from flir_player.jobs import OutputTransaction, extract_recording, JobCancelled
from flir_player.main_window import MainWindow
from flir_player.models import RoiShape
from flir_player.plots import roi_values, line_profile_values
from flir_player.processing import ProcessingState, apply_file_operation, roi_stats_app
from flir_player.render import DisplayState, render_frame_rgb, legend_colors
from flir_player.settings import app_settings
from flir_player.source import FlirVideoSource
from flir_player.widgets import ObjectParametersPanel, ThermalCanvas

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def seq_source():
    source = FlirVideoSource()
    source.open(ROOT / "local/data/2.seq")
    yield source
    source.close()


@pytest.fixture
def player(qapp):
    window = MainWindow()
    window.show()
    window.open_path(ROOT / "local/data/2.seq")
    assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
    yield window
    window.close()
    assert wait_until(qapp, lambda: not window.decoder.isRunning())


@pytest.mark.parametrize("operation", ["subtract", "add", "multiply", "divide"])
def test_f02_unsigned_arithmetic_is_promoted(operation):
    data = np.array([[0, 65535, 6501]], np.uint16)
    reference = np.array([[8, 65534, 0]], np.uint16)
    with np.errstate(divide="ignore", invalid="ignore"):
        expected = {"subtract": np.subtract, "add": np.add, "multiply": np.multiply,
                    "divide": np.divide}[operation](data.astype(float), reference.astype(float))
    expected[~np.isfinite(expected)] = np.nan
    np.testing.assert_array_equal(apply_file_operation(data, reference, operation), expected)


@pytest.mark.parametrize("result", ["cancel", "failure", "unsupported", "success"])
def test_f01_extract_stages_and_preserves_existing(tmp_path, result):
    source = tmp_path / "source.ats"
    source.write_bytes(b"source")
    existing = tmp_path / "existing.ats"
    existing.write_bytes(b"sentinel")
    entered = []
    cancelled = False

    def extract(dest, options):
        nonlocal cancelled
        entered.append(dest)
        assert Path(dest) != output
        assert existing.read_bytes() == b"sentinel"
        if result != "unsupported":
            Path(dest).write_bytes(b"new recording")
        if result == "failure":
            raise OSError("injected SDK failure")
        if result == "cancel":
            cancelled = True
            assert options.progress_callback(1, 3)
            cancelled = False  # SDK success must not erase a latched abort
        return True

    im = SimpleNamespace(extract=extract)
    options = SimpleNamespace()
    output = existing
    ok, _ = extract_recording(im, source, output, options)
    assert not ok and not entered and existing.read_bytes() == b"sentinel"
    output = tmp_path / "new.ats"
    ok, message = extract_recording(im, source, output, options, abort=lambda: cancelled)
    assert ok == (result == "success"), message
    assert output.exists() == ok
    assert source.read_bytes() == b"source" and existing.read_bytes() == b"sentinel"
    assert not list(tmp_path.glob(".flir-job-*"))


def test_f01_alias_and_publish_race_are_safe(tmp_path):
    source = tmp_path / "source.ats"
    source.write_bytes(b"source")
    alias = tmp_path / "alias.ats"
    os.link(source, alias)
    with pytest.raises(ValueError, match="alias"):
        with OutputTransaction([source]) as job:
            job.stage(alias)
    first, second = tmp_path / "first.png", tmp_path / "second.png"
    with pytest.raises(FileExistsError):
        with OutputTransaction() as job:
            job.stage(first).write_bytes(b"first")
            job.stage(second).write_bytes(b"second")
            second.write_bytes(b"racing writer")
            job.commit()
    assert not first.exists() and second.read_bytes() == b"racing writer"


@pytest.mark.parametrize("mode", ["average", "min", "max", "subtract"])
def test_f03_f04_temporal_window_and_decimated_export(seq_source, tmp_path, mode):
    source = seq_source
    raw = [source.read_frame(i).data.astype(float) for i in range(7)]
    source.set_processing(ProcessingState(temporal=(mode, 3)))
    expected = {"average": lambda a: np.mean(a, axis=0), "min": lambda a: np.min(a, axis=0),
                "max": lambda a: np.max(a, axis=0), "subtract": lambda a: a[-1] - a[0]}[mode]
    for i in [4, 0, 1, 2, 3, 4, 4, 3, 6, 0]:
        packet = source.read_frame(i, need_metadata=True)
        np.testing.assert_array_equal(packet.data, expected(raw[max(0, i-2):i+1]))
    source.read_frame(3)
    ring, index = source._temporal, source._last_frame_index
    payload = dict(kind="series", dest=str(tmp_path), base_name="temporal", fmt="tiff_float",
        start_frame=2, end_frame=6, decimation=2, stats_csv=True, rois=(),
        display=DisplayState(), options=ExportOptions(), suffix="counts", unit_label="Counts")
    ok, message = DecoderThread()._run_export_sequence(source, payload)
    assert ok, message
    assert source._temporal is ring and source._last_frame_index == index
    for i in (2, 4, 6):
        with Image.open(tmp_path / f"temporal_{i+1:05d}.tif") as image:
            np.testing.assert_array_equal(np.asarray(image), expected(raw[i-2:i+1]).astype(np.float32))
    np.testing.assert_array_equal(source.read_frame(4).data, expected(raw[2:5]))


def test_f03_decoder_warm_cache_and_metadata_refetch(player, qapp):
    player._change_filters({"temporal": ("average", 3)})
    assert wait_until(qapp, lambda: player.current_packet.processing.temporal == ("average", 3))
    for i in (3, 0, 1, 2, 3):
        old = player._active_request_id
        player.seek_to(i)
        assert wait_until(qapp, lambda: player.current_packet.index == i and player.current_packet.request_id != old)
    before = player.current_packet.data.copy()
    rid = player._activate_request()
    player.decoder.request_frame(3, rid, need_clip=True, need_metadata=True)
    assert wait_until(qapp, lambda: player.current_packet.request_id == rid)
    np.testing.assert_array_equal(before, player.current_packet.data)


def test_f10_atomic_unit_reference_and_failure_rollback(seq_source, tmp_path):
    source = seq_source
    source.load_reference(source.metadata.path, 0, "subtract")
    source.set_unit("temperature_factory_c")
    np.testing.assert_array_equal(source.read_frame(0).data, 0)
    source.apply_object_parameters({"emissivity": 0.51})
    np.testing.assert_array_equal(source.read_frame(0).data, 0)
    previous = source.read_frame(0)
    source._reference_spec = (tmp_path / "missing.seq", 0, "subtract")
    with pytest.raises(FileNotFoundError):
        source.set_unit("temperature_factory_f")
    assert source.unit == previous.unit and source.revision == previous.revision
    np.testing.assert_array_equal(source.read_frame(0).data, previous.data)


def test_f08_canonical_geometry_matches_sdk_masks(seq_source, tmp_path):
    source = seq_source
    shapes = []
    for kind in ("rect", "ellipse", "line"):
        for points in (((10, 10), (60, 50)), ((60, 50), (10, 10)), ((0, 0), (9, 7)),
                       ((0, 4), (2, 0)), ((0, 0), (4, 2)), ((0, 0), (639, 479)),
                       ((-5, -3), (8, 5))):
            shapes.append(RoiShape(len(shapes)+1, kind, points, f"ROI {len(shapes)+1}"))
    shapes.append(RoiShape(100, "cursor", ((639, 479),), "Spot"))
    source.set_rois(tuple(shapes))
    plain = source.read_frame(10)
    for _, shape, handle in source._roi_handles:
        path = tmp_path / "sdk.png"
        assert handle.export_bitmask(str(path))
        with Image.open(path) as image:
            sdk = np.asarray(image).any(axis=2)
        ys, xs = roi_coordinates(shape, *plain.data.shape)
        mask = np.zeros(plain.data.shape, bool)
        mask[ys, xs] = True
        if shape.kind != "line" or shape.points != ((0, 0), (639, 479)):
            np.testing.assert_array_equal(mask, sdk, err_msg=str(shape))
        else:
            # SDK has two different sample coordinates on this long diagonal.
            # It is deliberately excluded from the SDK statistics fast path.
            assert mask.sum() == sdk.sum()
        stats = next(s for s in plain.roi_stats if s.id == shape.id)
        assert stats.mean == pytest.approx(float(plain.data[ys, xs].mean()), abs=1e-8)
        assert len(set(zip(ys.tolist(), xs.tolist()))) == ys.size
        assert roi_values(plain.data, shape).size == sdk.sum()
    source.set_processing(ProcessingState(point=("gain", 1)))
    processed = source.read_frame(10)
    for a, b in zip(plain.roi_stats, processed.roi_stats):
        assert a.num_pixels == b.num_pixels
        assert a.mean == pytest.approx(b.mean, abs=1e-8)
        assert a.value == pytest.approx(b.value, abs=1e-8)
    folder = tmp_path / "masks"
    paths = source.export_roi_bitmasks(folder)
    for shape, path in zip(shapes, paths):
        with Image.open(path) as image:
            assert np.count_nonzero(image) == roi_values(plain.data, shape).size


@pytest.mark.parametrize("flip_h,flip_v", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("handle", ["nw", "ne", "sw", "se"])
def test_f09_resize_preserves_opposite_corner(qapp, flip_h, flip_v, handle):
    canvas = ThermalCanvas()
    canvas.set_flips(flip_h, flip_v)
    raw_handle = handle.translate(str.maketrans("we", "ew")) if flip_h else handle
    raw_handle = raw_handle.translate(str.maketrans("ns", "sn")) if flip_v else raw_handle
    anchors = {"nw": (100, 100), "se": (50, 50), "ne": (50, 100), "sw": (100, 50)}
    for points in [((100, 100), (50, 50)), ((50, 100), (100, 50)), ((50, 50), (100, 100))]:
        shape = RoiShape(1, "rect", points, "Box")
        canvas._drag = dict(orig=shape, handle=handle)
        assert canvas._dragged_shape((45, 45)).points == (anchors[raw_handle], (45, 45))


def test_f12_only_edited_parameter_is_emitted(qapp):
    panel = ObjectParametersPanel()
    snapshot = dict(emissivity=.987, reflected_temp=293.14999, atmosphere_temp=295.14999,
        est_atmospheric_transmission=.987, distance=12.34, relative_humidity=.4559,
        ext_optics_temp=293.14999, ext_optics_transmission=.996, can_change=True)
    panel.set_parameters(snapshot)
    changes = []
    panel.applied.connect(changes.append)
    panel._spins["distance"].editingFinished.emit()  # merely leaving a rounded field
    assert changes == []
    panel._spins["emissivity"].setValue(.51)
    panel._spins["emissivity"].editingFinished.emit()
    assert changes == [{"emissivity": .51}]
    assert panel._snapshot == snapshot
    panel.set_parameters(dict(snapshot, can_change=False))
    assert not panel.isEnabled()


def test_f05_f11_f18_histories_thresholds_and_paused_selection(player, qapp):
    player._add_roi("rect", ((10, 10), (50, 50)))
    assert wait_until(qapp, lambda: len(player.current_packet.roi_stats) == 1)
    player._add_roi("rect", ((60, 60), (90, 90)))
    assert wait_until(qapp, lambda: len(player.current_packet.roi_stats) == 2)
    player.bottom_panel.tabs.setCurrentWidget(player.bottom_panel.histogram)
    player._select_roi(1)
    assert player.bottom_panel.histogram.canvas.ax.get_title() == "Box 1"
    for i in (10, 20):
        player.seek_to(i)
        assert wait_until(qapp, lambda: player.current_packet.index == i)
    assert len(player._temporal[1]) >= 3
    player._change_unit("temperature_factory_c")
    assert wait_until(qapp, lambda: player.current_packet.unit.key.endswith("_c") and not player._busy)
    assert list(player._temporal[1]) == [20]
    player._change_segmentation(True, 20, 100)
    player._change_isotherm("interval", 30, 80)
    player._change_isotherm("interval", 30, 80)  # set limits after first-enable seeding
    player._change_unit("temperature_factory_f")
    assert wait_until(qapp, lambda: player.current_packet.unit.key.endswith("_f") and not player._busy)
    assert (player.seg_min, player.seg_max) == pytest.approx((68, 212))
    assert (player.iso_limit1, player.iso_limit2) == pytest.approx((86, 176))
    player._change_filters({"point": ("offset", 10)})
    assert wait_until(qapp, lambda: player.current_packet.processing.point == ("offset", 10))
    assert list(player._temporal[1]) == [20]
    assert (player.seg_min, player.seg_max) == (player.current_packet.minimum, player.current_packet.maximum)


def test_f11_temperature_difference_has_no_offset():
    state = ProcessingState(file_op="subtract")
    assert threshold_conversion("temperature_factory_c", "temperature_factory_f", state) == pytest.approx((1.8, 0))
    assert threshold_conversion("counts", "temperature_factory_c", state) is None
    assert threshold_conversion("temperature_factory_c", "temperature_user_c", state) is None


def test_f13_paused_statistics_snapshot_survives_dialog(player, qapp, tmp_path, monkeypatch):
    player._toggle_statistics(True)
    panel = player.bottom_panel.statistics
    player.bottom_panel.tabs.setCurrentWidget(panel)
    player.seek_to(20)
    assert wait_until(qapp, lambda: player.current_packet.index == 20)
    snapshot, rows = panel.snapshot, panel.to_rows()
    panel.pause_toggle.setChecked(True)
    player.seek_to(50)
    assert wait_until(qapp, lambda: player.current_packet.index == 50)
    dest = tmp_path / "stats.csv"
    def dialog(*args):
        player._present_packet(replace(player.current_packet, index=51))
        return str(dest), ""
    monkeypatch.setattr(QFileDialog, "getSaveFileName", dialog)
    player._save_statistics()
    with dest.open(encoding="utf-8") as handle:
        saved = list(csv.reader(handle))
    assert saved[1] == ["Frame", f"21 / {player.metadata.num_frames}"]
    assert panel.snapshot is snapshot and panel.to_rows() == rows
    assert saved[-6:] == [[str(v) for v in row] for row in rows[-6:]]


def test_f17_quick_export_keeps_pre_dialog_frame(player, tmp_path, monkeypatch):
    packet = player.current_packet
    dest = tmp_path / "frame.npy"
    def dialog(*args):
        player._present_packet(replace(packet, index=1, data=packet.data + 1))
        return str(dest), ""
    monkeypatch.setattr(QFileDialog, "getSaveFileName", dialog)
    player._export("npy")
    np.testing.assert_array_equal(np.load(dest), packet.data)


def test_f17_still_dialog_freezes_data_and_sidecar(player, tmp_path, monkeypatch):
    packet = player.current_packet
    dest = tmp_path / "still.tif"
    def dialog_exec(dialog):
        player._present_packet(replace(packet, index=8, data=packet.data+8))
        return dialog.DialogCode.Accepted
    monkeypatch.setattr(ExportImageDialog, "exec", dialog_exec)
    monkeypatch.setattr(ExportImageDialog, "parameters", lambda _: dict(dest=str(dest),
        fmt="tiff16", options=ExportOptions(), stats_sidecar=True))
    player._open_export_image_dialog()
    with Image.open(dest) as image:
        np.testing.assert_array_equal(np.asarray(image), packet.data)
        assert json.loads(image.tag_v2[270])["frame"] == packet.index+1
    with dest.with_suffix(".csv").open() as handle:
        rows = list(csv.reader(handle))
    assert rows[1][0] == str(packet.index+1)


def test_f11_new_source_resets_thresholds(player, qapp):
    player._change_segmentation(True, 100, 200)
    player._change_isotherm("interval", 100, 200)
    player._change_isotherm("interval", 100, 200)
    player.open_path(ROOT / "local/data/2.seq")
    assert wait_until(qapp, lambda: player.current_packet is not None and not player._busy)
    assert (player.seg_min, player.seg_max) == (player.current_packet.minimum, player.current_packet.maximum)
    packet = player.current_packet  # re-seeded like first enabling (N01), never at the minimum
    assert (player.iso_limit1, player.iso_limit2) == ((packet.minimum + packet.maximum) / 2, packet.maximum)


def test_f14_numeric_tiff_mapping_and_controls(seq_source, qapp, tmp_path):
    packet = seq_source.read_frame(0)
    dest = tmp_path / "raw.tif"
    save_frame(dest, "tiff16", None, packet.data, (packet.minimum, packet.maximum),
               packet=packet, source=seq_source.metadata)
    with Image.open(dest) as image:
        np.testing.assert_array_equal(np.asarray(image), packet.data)
        description = json.loads(image.tag_v2[270])
        assert description["encoding"] == "raw_counts_uint16" and description["unit"] == "counts"
        assert description["analysis"]["object_parameters"]
    data = np.linspace(-5, 20, 99).reshape(9, 11)
    dest = tmp_path / "mapped.tif"
    save_tiff16(dest, data, -5, 20)
    with Image.open(dest) as image:
        desc = json.loads(image.tag_v2[270])
        decoded = np.asarray(image) * desc["scale"] + desc["offset"]
    np.testing.assert_allclose(decoded, data, atol=25/65535/2)
    for dialog in (ExportImageDialog(seq_source.metadata, 0), ExportSeriesDialog(seq_source.metadata)):
        for fmt in ("tiff16", "tiff_float"):
            dialog.format_combo.setCurrentIndex(dialog.format_combo.findData(fmt))
            assert not dialog.composition.isEnabled()
        dialog.close()


@pytest.mark.parametrize("invert", [False, True])
def test_f15_pe_legend_uses_frame_transfer(invert):
    data = np.zeros((480, 64), np.float32)
    data[0, :2] = 50, 100
    state = DisplayState(palette="Grayscale", pe=.5, inverted=invert, clipping=False)
    rgb, low, high, mapping = render_frame_rgb(data, state, return_mapping=True)
    legend = legend_colors(state.palette, invert, mapping, (low, high))
    np.testing.assert_array_equal(rgb[0, 0], legend[127])
    composed = compose_frame(rgb, ExportOptions(rois=False), palette=state.palette,
        inverted=invert, scale=(low, high), mapping=mapping)
    # Read the interior of the actual exported legend at its midpoint.
    np.testing.assert_array_equal(composed[240, 64+20], legend[127])


def test_f16_f21_range_start_and_visible_kernel(player, qapp):
    player._play_range_changed(100, 120)
    player.toggle_playback()
    assert wait_until(qapp, lambda: player.current_packet.index >= 100)
    assert player.current_packet.index <= 120
    player.pause_playback(invalidate=True)
    player.inspector.spatial_spin.setValue(4)
    assert player.inspector.spatial_spin.value() == 5


def test_f19_settings_are_explicit_ini_and_do_not_touch_sentinel(tmp_path):
    sentinel = QSettings(str(tmp_path / "sentinel.ini"), QSettings.Format.IniFormat)
    sentinel.setValue("palettes/custom", "precious saved palette")
    sentinel.sync()
    before = Path(sentinel.fileName()).read_bytes()
    settings = app_settings()
    assert settings.format() == QSettings.Format.IniFormat
    assert settings.fileName() != sentinel.fileName()
    settings.setValue("review/probe", 1)
    settings.sync()
    assert Path(sentinel.fileName()).read_bytes() == before


@pytest.mark.parametrize("fmt", ["mp4", "wmv"])
@pytest.mark.parametrize("shape", [(480, 744), (61, 63)])
def test_f22_movie_geometry_is_preserved(tmp_path, fmt, shape):
    dest = tmp_path / f"geometry.{fmt}"
    pixels = np.full((*shape, 3), 128, np.uint8)
    pixels[:, -3:] = 240
    writer = MovieWriter(dest, 10, fmt)
    writer.append(pixels)
    writer.close()
    reader = imageio.get_reader(dest)
    try:
        decoded = reader.get_data(0)
    finally:
        reader.close()
    assert decoded.shape[:2] == tuple(n+n%2 for n in shape)
    assert decoded[:, -2:].mean() > 210  # original border survives padding


@pytest.mark.parametrize("cancel_at", [None, 1, 2, 3])
def test_f06_f20_batch_preflight_and_in_file_cancellation(tmp_path, monkeypatch, cancel_at):
    import flir_player.decoder as decoder_module
    decoder = DecoderThread()
    files = []
    for folder in ("a", "b", "c"):
        path = tmp_path / folder / "trial.ats"
        path.parent.mkdir()
        path.write_bytes(folder.encode())
        files.append(str(path))
    out = tmp_path / "outputs"
    out.mkdir()
    sentinel = out / "trial_extract.ats"
    sentinel.write_bytes(b"existing")
    entered = []

    class Imager:
        num_frames = 3
        def __init__(self, path):
            self.path = path
        def close(self):
            pass
        def extract(self, dest, options):
            entered.append(self.path)
            Path(dest).write_bytes(Path(self.path).read_bytes())
            if cancel_at == len(entered):
                decoder.cancel_extract()
            options.progress_callback(1, 3)
            return True

    monkeypatch.setattr(decoder_module, "fnv", SimpleNamespace(file=SimpleNamespace(
        ImagerFile=Imager, ImagerFileExtractOptions=SimpleNamespace)))
    ok, message = decoder._run_batch_extract(dict(files=files, folder=str(out), decimation=1))
    results = json.loads((out / "batch_extract_report.json").read_text())
    assert ok == (cancel_at is None), message
    assert len({r["destination"] for r in results}) == 3
    expected_success = 3 if cancel_at is None else cancel_at-1
    assert sum(r["status"] == "succeeded" for r in results) == expected_success
    assert sentinel.read_bytes() == b"existing"
    for result in results:
        dest = Path(result["destination"])
        assert dest.exists() == (result["status"] == "succeeded")
    assert not list(out.glob(".flir-job-*"))


def test_f20_cancellation_is_latched_before_job_start():
    decoder = DecoderThread()
    decoder.request_extract({"dest": "unused.ats"})
    decoder.cancel_extract()
    decoder.request_extract({"dest": "next.ats"})
    first = decoder._commands.get()[1]["_token"]
    second = decoder._commands.get()[1]["_token"]
    assert first() and not second()


@pytest.mark.parametrize("failure", ["exception", "cancel"])
def test_export_failure_leaves_no_partial_outputs(seq_source, tmp_path, monkeypatch, failure):
    import flir_player.decoder as decoder_module
    decoder = DecoderThread()
    original = decoder_module.save_frame
    calls = []
    def save(*args, **kwargs):
        calls.append(args[0])
        original(*args, **kwargs)
        if len(calls) == 2:
            if failure == "exception":
                raise OSError("disk failure")
            decoder.cancel_extract()
    monkeypatch.setattr(decoder_module, "save_frame", save)
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_bytes(b"keep")
    ok, message = decoder._run_export_sequence(seq_source, dict(kind="series", dest=str(tmp_path),
        base_name="frames", fmt="tiff_float", start_frame=0, end_frame=3, decimation=1,
        stats_csv=True, rois=(), display=DisplayState(), options=ExportOptions(), suffix="counts"))
    assert not ok, message
    assert list(tmp_path.iterdir()) == [sentinel]


@pytest.mark.parametrize("operation", ["open", "extract", "filter", "movie"])
def test_f07_close_waits_for_worker_subprocess(tmp_path, operation):
    # Native QThread teardown must be tested outside the pytest process.
    # The real SDK sample is local/data/2.seq; bounded delays reproduce a call
    # that outlasts the former four-second close timeout.
    script = tmp_path / "shutdown.py"
    script.write_text(textwrap.dedent('''
        import os, sys, time, threading
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        os.environ['FLIR_SETTINGS_FILE'] = str(Path(__file__).with_suffix('.ini'))
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from flir_player.main_window import MainWindow
        from flir_player.source import FlirVideoSource
        from flir_player.decoder import DecoderThread
        mode = sys.argv[2]
        entered = threading.Event()
        def delayed(original):
            def call(*args, **kwargs):
                entered.set()
                time.sleep(4.5)
                return original(*args, **kwargs)
            return call
        if mode == 'open':
            FlirVideoSource.open = delayed(FlirVideoSource.open)
        elif mode == 'extract':
            FlirVideoSource.extract = delayed(FlirVideoSource.extract)
        elif mode == 'filter':
            FlirVideoSource.set_processing = delayed(FlirVideoSource.set_processing)
        else:
            DecoderThread._run_export_sequence = delayed(DecoderThread._run_export_sequence)
        app = QApplication([])
        window = MainWindow()
        window._show_error = lambda *args: None
        window.show()
        window.open_path(Path(sys.argv[1]) / 'local/data/2.seq')
        started = False
        closed = False
        ticks = 0
        start = time.monotonic()
        def poll():
            global started, closed, ticks
            ticks += 1
            if mode != 'open' and not started and window.current_packet is not None and not window._busy:
                started = True
                if mode == 'extract':
                    window.decoder.request_extract(dict(dest=str(Path(__file__).with_suffix('.ats')), start_frame=0,end_frame=2))
                elif mode == 'filter':
                    window._change_filters({'spatial': ('median', 3)})
                else:
                    from flir_player.compose import ExportOptions
                    params = dict(kind='movie', dest=str(Path(__file__).with_suffix('.mp4')),
                        start_frame=0, end_frame=4, decimation=1, fmt='mp4', fps=10, options=ExportOptions())
                    window.decoder.request_export_sequence(window._export_payload(params))
            if entered.is_set() and not closed:
                closed = True
                window.close()
        timer = QTimer()
        timer.timeout.connect(poll)
        timer.start(10)
        QTimer.singleShot(15000, lambda: os._exit(3))
        app.exec()
        assert not window.decoder.isRunning()
        assert time.monotonic() - start >= 4.5
        assert ticks > 100, ticks
        print('normal exit; worker stopped; GUI timer remained responsive', flush=True)
    '''), encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), str(ROOT), operation],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "normal exit; worker stopped" in result.stdout
