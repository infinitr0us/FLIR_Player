# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cell zones in the player: the split dialog, re-splitting and joining, ROI sets with zone groups,
compact labels, and the Excel dialog's thermocouple and area-mean options."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtWidgets import QDialog

from flir_player.excel_export import RoiSet, ZoneGroup, load_roi_set, save_roi_set
from flir_player.models import RoiShape
from flir_player.zone_dialog import ZoneSplitDialog
from flir_player.zones import ZoneSpec, split_box

SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"
BOX = ((245.0, 251.0), (405.0, 259.0))


def test_split_dialog_previews_zones_and_refuses_impossible_settings(qapp) -> None:
    seen = []
    dialog = ZoneSplitDialog(ZoneSpec(box=BOX), 640, 480, tc_pixels={"T1": (256, 380), "Far": (10, 10)})
    dialog.preview.connect(seen.append)
    dialog.show()
    qapp.processEvents()
    assert len(seen[-1]) == 18 and seen[-1][0][0] == "Cell 1"
    assert seen[-1] == split_box(ZoneSpec(box=BOX), 640, 480)
    assert "18 zones of 7-8 × 8 pixels" in dialog.info_label.text()
    assert "T1 → Cell 3" in dialog.tc_label.text() and "Far is in no ROI" in dialog.tc_label.text()
    ok = dialog.button_box.button(dialog.button_box.StandardButton.Ok)
    dialog.count_spin.setValue(100)  # 100 zones with 1 px gaps do not fit 160 px
    assert seen[-1] == [] and not ok.isEnabled() and "at least" in dialog.info_label.text()
    dialog.count_spin.setValue(9)
    dialog.start_combo.setCurrentIndex(1)  # left end
    dialog.prefix_edit.setText("Zone")
    assert ok.isEnabled() and len(seen[-1]) == 9 and seen[-1][0][0] == "Zone 1"
    spec = dialog.spec()
    assert (spec.count, spec.start, spec.gap, spec.prefix, spec.box) == (9, "left", 1, "Zone", BOX)
    # the box edges are inclusive pixel columns and rows
    assert (dialog.x0.value(), dialog.x1.value(), dialog.y0.value(), dialog.y1.value()) == (245, 404, 251, 258)
    dialog.close()


def test_split_dialog_offers_join_only_for_a_split(qapp) -> None:
    plain = ZoneSplitDialog(ZoneSpec(box=BOX), 640, 480)
    again = ZoneSplitDialog(ZoneSpec(box=BOX), 640, 480, regroup=True)
    texts = lambda d: [b.text() for b in d.button_box.buttons()]  # noqa: E731
    assert "Join Back into One Box" not in texts(plain) and "Join Back into One Box" in texts(again)


def test_roi_sets_keep_zone_groups(tmp_path) -> None:
    zones = [RoiShape(k + 2, "rect", points, name)
             for k, (name, points) in enumerate(split_box(ZoneSpec(box=BOX, count=3, gap=0)))]
    spot = RoiShape(1, "cursor", ((10.5, 10.5),), "Spot")
    group = ZoneGroup(rois=(1, 2, 3), spec=ZoneSpec(box=BOX, count=3, gap=0), name="Top slice")
    path = tmp_path / "a.rois.json"
    save_roi_set(path, RoiSet(rois=(spot, *zones), zone_groups=(group,)))
    loaded = load_roi_set(path)
    assert loaded.zone_groups == (group,)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["zone_groups"][0]["rois"] = [1, 9]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Zone group 1"):
        load_roi_set(path)
    del payload["zone_groups"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_roi_set(path).zone_groups == ()  # older sets have none


@pytest.mark.skipif(not (SAMPLES / "2.seq").exists(), reason="needs local/data/2.seq")
def test_split_resplit_join_and_reload_in_the_player(qapp, tmp_path, monkeypatch) -> None:
    from conftest import wait_until

    from flir_player import main_window as mw
    from flir_player.main_window import MainWindow

    window = MainWindow()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy, timeout=20)
        window._add_roi("cursor", ((5.5, 5.5),))
        window._add_roi("rect", ((40.0, 200.0), (220.0, 212.0)))
        box_name = window._rois[-1].name
        previews = []

        def accept(settings):
            def fake_exec(self):
                self.show()
                for key, value in settings.items():
                    getattr(self, key).setValue(value) if key != "prefix" else self.prefix_edit.setText(value)
                previews.append(len(window.canvas._rois))
                return QDialog.DialogCode.Accepted
            return fake_exec

        monkeypatch.setattr(ZoneSplitDialog, "exec", accept({"count_spin": 18}))
        window._split_zones()
        names = [roi.name for roi in window._rois]
        assert names[0].startswith("Spot") and names[1:] == [f"Cell {k}" for k in range(1, 19)]
        assert previews == [19]  # the spot plus 18 zones were on the canvas while the dialog was open
        assert len(window._zone_groups) == 1 and window._zone_groups[0]["name"] == box_name
        # selecting a zone and splitting again re-splits the whole group
        window._select_roi(window._rois[5].id)
        monkeypatch.setattr(ZoneSplitDialog, "exec", accept({"count_spin": 9, "prefix": "Zone"}))
        window._split_zones()
        assert [roi.name for roi in window._rois][1:] == [f"Zone {k}" for k in range(1, 10)]
        assert len(window._zone_groups) == 1
        # saving and loading the ROI set keeps the group
        path = tmp_path / "set.rois.json"
        monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(path), "")))
        monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(path), "")))
        window._save_roi_set()
        window._load_roi_set()
        assert [roi.name for roi in window._rois][1:] == [f"Zone {k}" for k in range(1, 10)]
        assert len(window._zone_groups) == 1 and len(window._zone_groups[0]["ids"]) == 9
        # join back into the box
        window._select_roi(window._rois[3].id)
        monkeypatch.setattr(ZoneSplitDialog, "exec", lambda self: ZoneSplitDialog.JOIN)
        window._split_zones()
        assert [roi.name for roi in window._rois][1:] == [box_name]
        assert window._rois[1].points == ((40.0, 200.0), (220.0, 212.0)) and window._zone_groups == []
        # a cancelled split leaves everything as it was
        monkeypatch.setattr(ZoneSplitDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
        before = list(window._rois)
        window._split_zones()
        assert window._rois == before and window.canvas._rois == before
        # nothing to split: a spot
        window._select_roi(window._rois[0].id)
        window._split_zones()
        assert window._rois == before
    finally:
        window.close()


def test_narrow_numbered_labels_shrink_to_their_number(qapp) -> None:
    from PySide6.QtGui import QFont, QFontMetrics

    from flir_player.widgets import ThermalCanvas

    canvas = ThermalCanvas()
    canvas.resize(640, 480)
    metrics = QFontMetrics(QFont("Segoe UI", 10))
    canvas._image_to_widget = lambda x, y: type("P", (), {"x": lambda self: x * scale, "y": lambda self: y})()
    zone = RoiShape(1, "rect", ((0.0, 0.0), (9.0, 8.0)), "Cell 12")
    other = RoiShape(2, "rect", ((0.0, 0.0), (9.0, 8.0)), "T1 3×3")
    scale = 1.0
    assert canvas._fitting_label(zone, metrics, False) is None  # 9 px: no room even for "12"
    assert canvas._fitting_label(zone, metrics, True) == "Cell 12"  # the selected one keeps its name
    assert canvas._fitting_label(other, metrics, False) == "T1 3×3"  # no trailing number: unchanged
    scale = 3.0
    assert canvas._fitting_label(zone, metrics, False) == "12"
    scale = 12.0
    assert canvas._fitting_label(zone, metrics, False) == "Cell 12"


def test_exported_images_and_roi_maps_follow_the_same_rule() -> None:
    from PIL import Image, ImageDraw

    from flir_player.compose import _fitting_label
    from flir_player.excel_export import map_scale

    draw = ImageDraw.Draw(Image.new("RGB", (100, 100)))
    zone = RoiShape(1, "rect", ((0.0, 0.0), (9.0, 8.0)), "Cell 12")
    assert _fitting_label(draw, zone, (0, 0), (9, 8)) is None
    assert _fitting_label(draw, zone, (0, 0), (27, 24)) == "12"
    assert _fitting_label(draw, RoiShape(2, "rect", ((0, 0), (9, 8)), "Hot spot"), (0, 0), (9, 8)) == "Hot spot"
    zones = [RoiShape(k + 1, "rect", p, n) for k, (n, p) in enumerate(split_box(ZoneSpec(box=BOX), 640, 480))]
    assert map_scale(zones, 640) == 3
    assert map_scale([RoiShape(1, "rect", ((0, 0), (100, 50)), "Big")], 640) == 1
    assert map_scale(zones, 1280) == 1  # never wider than 2000 px


def test_excel_dialog_fills_tc_compare_from_the_fit_for_zones(qapp) -> None:
    from dataclasses import replace

    from test_tcmatch import SPOT_A, SPOT_B, make_scene

    from flir_player import tcmatch
    from flir_player.excel_dialog import ExcelExportDialog
    from flir_player.models import VideoMetadata
    from flir_player.tcmatch import MatchOptions, RunOutput

    samples, table = make_scene()
    result, _l, _s = tcmatch.analyse(samples, table, MatchOptions(spot=3))
    run = RunOutput(result=result, table=table)
    metadata = VideoMetadata(path=Path("C:/data/scene.seq"), width=48, height=40, num_frames=30_000,
                             start_time=None, end_time=None, duration_seconds=1000.0, nominal_fps=30.0)
    zones = [RoiShape(k + 1, "rect", ((float(c), 0.0), (float(c + 4), 40.0)), f"Cell {k + 1}")
             for k, c in enumerate(range(0, 48, 4))]  # 12 vertical zones
    dialog = ExcelExportDialog(metadata, tuple(zones), current_frame=0, ignition_frame=4110, tc_run=run)
    assert dialog.means_combo.currentData() == "signal"  # more than 8 areas
    assert dialog.tc_check.isEnabled() and dialog.tc_check.isChecked()
    a_zone = f"Cell {SPOT_A[1] // 4 + 1}"
    b_zone = f"Cell {SPOT_B[1] // 4 + 1}"
    assert f"TC A → {a_zone}" in dialog.tc_label.text() and f"TC B → {b_zone}" in dialog.tc_label.text()
    options = dialog.options()
    assert options.area_means == "signal"
    assert dict(options.tc_prefill.roi_tc) == {a_zone: "TC A", b_zone: "TC B"}
    assert options.tc_prefill.offset_s == pytest.approx(result.lag_s - 4110 / result.fps)
    # unticking the TC sheet, or the clock time base, turns the option off with a reason
    dialog.sheet_checks["tc"].setChecked(False)
    assert not dialog.tc_check.isEnabled() and "TC Compare" in dialog.tc_label.text()
    assert dialog.options().tc_prefill is None
    dialog.sheet_checks["tc"].setChecked(True)
    dialog.time_base_combo.setCurrentIndex(1)
    assert not dialog.tc_check.isEnabled() and "time base" in dialog.tc_label.text()
    dialog.time_base_combo.setCurrentIndex(0)
    dialog.tc_check.setChecked(False)
    assert dialog.options().tc_prefill is None and "paste" in dialog.tc_label.text()
    # no fit, or a superframing fit, cannot fill it
    plain = ExcelExportDialog(metadata, tuple(zones[:3]), current_frame=0)
    assert not plain.tc_check.isEnabled() and plain.means_combo.currentData() == "pixels"
    preset = ExcelExportDialog(metadata, tuple(zones), current_frame=0,
                               tc_run=RunOutput(result=replace(result, preset=2), table=table))
    assert not preset.tc_check.isEnabled() and "superframing" in preset.tc_label.text()
    assert np.isfinite(options.tc_prefill.offset_s)


def test_z1_the_split_dialog_refuses_names_other_rois_carry(qapp) -> None:
    seen = []
    dialog = ZoneSplitDialog(ZoneSpec(box=BOX), 640, 480, taken={"Cell 3", "Box 1"})
    dialog.preview.connect(seen.append)
    dialog.show()
    qapp.processEvents()
    ok = dialog.button_box.button(dialog.button_box.StandardButton.Ok)
    assert seen[-1] == [] and not ok.isEnabled() and "Cell 3" in dialog.info_label.text()
    dialog.prefix_edit.setText("Top")
    assert ok.isEnabled() and seen[-1][0][0] == "Top 1"
    dialog.close()


def test_z1_the_excel_dialog_explains_repeated_names(qapp) -> None:
    from test_tcmatch import SPOT_A, SPOT_B, make_scene

    from flir_player import tcmatch
    from flir_player.excel_dialog import ExcelExportDialog
    from flir_player.models import VideoMetadata
    from flir_player.tcmatch import MatchOptions, RunOutput

    samples, table = make_scene()
    result, _l, _s = tcmatch.analyse(samples, table, MatchOptions(spot=3))
    metadata = VideoMetadata(path=Path("C:/data/scene.seq"), width=48, height=40, num_frames=30_000,
                             start_time=None, end_time=None, duration_seconds=1000.0, nominal_fps=30.0)
    rois = (RoiShape(1, "rect", ((SPOT_A[1] - 1.0, SPOT_A[0] - 1.0), (SPOT_A[1] + 2.0, SPOT_A[0] + 2.0)), "Cell"),
            RoiShape(2, "rect", ((SPOT_B[1] - 1.0, SPOT_B[0] - 1.0), (SPOT_B[1] + 2.0, SPOT_B[0] + 2.0)), "Cell"))
    dialog = ExcelExportDialog(metadata, rois, current_frame=0, tc_run=RunOutput(result=result, table=table))
    assert not dialog.tc_check.isEnabled() and "More than one ROI is named Cell" in dialog.tc_label.text()
    assert dialog.options().tc_prefill is None


def test_z8_the_excel_dialog_checks_added_recordings_for_repeated_names(qapp) -> None:
    from test_tcmatch import SPOT_A, SPOT_B, make_scene

    from flir_player import tcmatch
    from flir_player.excel_dialog import ExcelExportDialog
    from flir_player.models import VideoMetadata
    from flir_player.tcmatch import MatchOptions, RunOutput

    samples, table = make_scene()
    result, _l, _s = tcmatch.analyse(samples, table, MatchOptions(spot=3))
    metadata = VideoMetadata(path=Path("C:/data/scene.seq"), width=48, height=40, num_frames=30_000,
                             start_time=None, end_time=None, duration_seconds=1000.0, nominal_fps=30.0)
    rois = (RoiShape(1, "rect", ((SPOT_A[1] - 1.0, SPOT_A[0] - 1.0), (SPOT_A[1] + 2.0, SPOT_A[0] + 2.0)), "Cell 3"),
            RoiShape(2, "rect", ((SPOT_B[1] - 1.0, SPOT_B[0] - 1.0), (SPOT_B[1] + 2.0, SPOT_B[0] + 2.0)), "Cell 6"))
    dialog = ExcelExportDialog(metadata, rois, current_frame=0, tc_run=RunOutput(result=result, table=table))
    assert dialog.tc_check.isEnabled()
    other = (RoiShape(1, "rect", ((1.0, 1.0), (4.0, 4.0)), "Cell 3"), RoiShape(2, "rect", ((6.0, 1.0), (9.0, 4.0)), "Cell 3"))
    dialog._others.append({"path": Path("C:/data/b.csq"), "rois": other, "ignition": 0})
    dialog._update()
    assert not dialog.tc_check.isEnabled() and "b.csq has more than one ROI named Cell 3" in dialog.tc_label.text()
    assert dialog.options().tc_prefill is None
