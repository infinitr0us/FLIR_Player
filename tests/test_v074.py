# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""0.7.4: cell zones that follow a module running at a slant in the picture (the 1006 test-a T650 looks along
the module from above: its top face rises ~38 px from the far end to the heater end over 272 columns and is
only ~17 px tall, so equal zones from one upright box miss the cell tops at both ends)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog

from flir_player.excel_export import RoiSet, ZoneGroup, load_roi_set, save_roi_set
from flir_player.geometry import roi_coordinates
from flir_player.models import RoiShape
from flir_player.zone_dialog import ZoneSplitDialog
from flir_player.zones import ZoneSpec, pair_tcs, split_box

W, H = 640, 480
SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"
# 1006a T650 (FLIR2267.csq): module columns 160-431, top-face rows 388-404 at the far end, 358-374 at the heater end
BOX = ((160.0, 388.0), (432.0, 405.0))
FAR = (358.0, 375.0)


def _shapes(zones):
    return [RoiShape(id=k + 1, kind="rect", points=points, name=name) for k, (name, points) in enumerate(zones)]


def _cells(shape):
    ys, xs = roi_coordinates(shape, H, W)
    return sorted(set(xs.tolist())), sorted(set(ys.tolist()))


def test_t1_zones_step_along_the_slant_and_keep_the_upright_split_columns() -> None:
    flat = _shapes(split_box(ZoneSpec(box=BOX), W, H))
    slanted = _shapes(split_box(ZoneSpec(box=BOX, far=FAR), W, H))
    assert [s.name for s in slanted] == [f"Cell {k}" for k in range(1, 19)]
    tops = []
    for f, s in zip(flat, slanted):
        f_cols, _f_rows = _cells(f)
        s_cols, s_rows = _cells(s)
        assert s_cols == f_cols  # the cells along the module are the same
        assert len(s_rows) == 17  # the band keeps its height
        tops.append(s_rows[0])
    # cell 1 (heater end, right) is highest; every step towards the far end goes down by 1-3 rows
    assert tops == sorted(tops)
    steps = [b - a for a, b in zip(tops, tops[1:])]
    assert all(1 <= d <= 3 for d in steps), steps
    assert 358 <= tops[0] <= 360 and 386 <= tops[-1] <= 388
    # T2's pixel from the fit (row 382, column 349) lands in cell 6; the upright box's cell 6 misses it
    assert pair_tcs({"T2": (382, 349)}, slanted, H, W).pairs == {"T2": "Cell 6"}
    assert pair_tcs({"T2": (382, 349)}, flat, H, W).outside == ["T2"]


def test_t2_no_slant_is_the_upright_split_and_far_equal_to_the_box_is_no_slant() -> None:
    assert split_box(ZoneSpec(box=BOX, far=(388.0, 405.0))) == split_box(ZoneSpec(box=BOX))
    assert split_box(ZoneSpec(box=BOX, far=(405.0, 388.0))) == split_box(ZoneSpec(box=BOX))  # either order
    # zone 1 at the left end: the same boxes in reverse order
    right = split_box(ZoneSpec(box=BOX, far=FAR, gap=0))
    left = split_box(ZoneSpec(box=BOX, far=FAR, gap=0, start="left"))
    assert [p for _n, p in right] == [p for _n, p in left][::-1]


def test_t3_a_split_along_the_rows_steps_its_columns_and_heights_may_change() -> None:
    zones = _shapes(split_box(ZoneSpec(box=((100, 50), (110, 110)), count=6, start="top", gap=0, far=(130, 150)),
                              W, H))
    spans = [_cells(z) for z in zones]
    rows = [r for _c, r in spans]
    assert sorted(r for rr in rows for r in rr) == list(range(50, 110))  # rows split as without a slant
    lefts = [c[0] for c, _r in spans]
    widths = [len(c) for c, _r in spans]
    assert lefts == sorted(lefts) and lefts[0] < 110 and lefts[-1] > 120
    assert widths[0] < widths[-1]  # 10 columns wide at the first row, 20 at the last


@pytest.mark.parametrize(("far", "start", "message"), [
    ((370.0, 370.2), "right", "rows at the last column must cover at least one pixel"),
    ((370.0, float("nan")), "left", "rows at the last column must be numbers"),
    ((5.0, 5.0), "top", "columns at the last row must cover"),
])
def test_t4_an_empty_slant_row_is_refused_with_a_reason(far, start, message) -> None:
    with pytest.raises(ValueError, match=message):
        split_box(ZoneSpec(box=BOX, start=start, far=far))


def test_t5_a_slant_off_the_image_is_refused() -> None:
    with pytest.raises(ValueError, match="cover no pixel"):
        split_box(ZoneSpec(box=((10, 0), (100, 4)), count=3, far=(-20.0, -10.0)), W, H)


def test_t6_the_dialog_follows_the_box_until_the_slant_is_set(qapp) -> None:
    seen = []
    dialog = ZoneSplitDialog(ZoneSpec(box=BOX), W, H, tc_pixels={"T2": (382, 349)})
    dialog.preview.connect(seen.append)
    dialog.show()
    qapp.processEvents()
    assert dialog.far_label.text() == "Rows at the last column"
    assert (dialog.f0.value(), dialog.f1.value()) == (388, 404)
    dialog.y0.setValue(390)  # moving the box's rows moves the untouched slant row with them
    assert (dialog.f0.value(), dialog.f1.value()) == (390, 404) and dialog.spec().far is None
    dialog.y0.setValue(388)
    assert "T2 is in no ROI" in dialog.tc_label.text()
    dialog.f0.setValue(358)
    dialog.f1.setValue(374)
    assert dialog.spec().far == FAR
    assert seen[-1] == split_box(ZoneSpec(box=BOX, far=FAR), W, H)
    assert "T2 → Cell 6" in dialog.tc_label.text()
    info = dialog.info_label.text()
    assert "18 zones of 14-15 × 17 pixels" in info
    assert "Rows 388-404 at column 160, 358-374 at column 431" in info and "step along the slant" in info
    dialog.y1.setValue(406)  # set by hand: the slant row stays where it was
    assert dialog.spec().far == FAR
    # turning the split to run along the rows: the slant row now gives columns and follows the box again
    dialog.start_combo.setCurrentIndex(dialog.start_combo.findData("top"))
    assert dialog.far_label.text() == "Columns at the last row"
    assert (dialog.f0.value(), dialog.f1.value()) == (160, 431) and dialog.spec().far is None
    dialog.close()


def test_t7_resplitting_shows_the_saved_slant(qapp) -> None:
    dialog = ZoneSplitDialog(ZoneSpec(box=BOX, far=FAR), W, H, regroup=True)
    dialog.show()
    qapp.processEvents()
    assert (dialog.f0.value(), dialog.f1.value()) == (358, 374)
    dialog.y0.setValue(387)  # a saved slant is the user's: it does not follow the box
    assert dialog.spec().far == FAR
    dialog.close()


def test_t8_roi_sets_keep_the_slant_and_older_sets_have_none(tmp_path) -> None:
    spec = ZoneSpec(box=BOX, count=3, gap=0, far=FAR)
    zones = [RoiShape(k + 1, "rect", points, name) for k, (name, points) in enumerate(split_box(spec))]
    path = tmp_path / "a.rois.json"
    save_roi_set(path, RoiSet(rois=tuple(zones), zone_groups=(ZoneGroup(rois=(0, 1, 2), spec=spec, name="Box 1"),)))
    assert load_roi_set(path).zone_groups[0].spec == spec
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["zone_groups"][0]["far"] == [358.0, 375.0]
    flat = ZoneSpec(box=BOX, count=3, gap=0)
    save_roi_set(path, RoiSet(rois=tuple(zones), zone_groups=(ZoneGroup(rois=(0, 1, 2), spec=flat),)))
    assert "far" not in json.loads(path.read_text(encoding="utf-8"))["zone_groups"][0]  # as older players wrote
    assert load_roi_set(path).zone_groups[0].spec.far is None
    for bad in ([375.0], [375.0, 375.0], ["a", 1], 3, [1.0, float("inf")]):
        payload["zone_groups"][0]["far"] = bad
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="Zone group 1"):
            load_roi_set(path)


@pytest.mark.skipif(not (SAMPLES / "2.seq").exists(), reason="needs local/data/2.seq")
def test_t9_split_with_a_slant_resplit_join_and_reload_in_the_player(qapp, tmp_path, monkeypatch) -> None:
    from conftest import wait_until

    from flir_player import main_window as mw
    from flir_player.main_window import MainWindow

    window = MainWindow()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy, timeout=20)
        drawn = ((40.0, 200.0), (220.0, 212.0))
        window._add_roi("rect", drawn)

        def accept(settings):
            def fake_exec(self):
                self.show()
                for key, value in settings.items():
                    getattr(self, key).setValue(value)
                return QDialog.DialogCode.Accepted
            return fake_exec

        monkeypatch.setattr(ZoneSplitDialog, "exec", accept({"count_spin": 9, "f0": 170, "f1": 181}))
        window._split_zones()
        spec = window._zone_groups[0]["spec"]
        assert spec.far == (170.0, 182.0) and spec.box == drawn
        tops = [roi.points[0][1] for roi in window._rois]
        assert tops[0] < tops[-1]  # cell 1 at the right end sits higher
        # a re-split starts from the saved slant
        window._select_roi(window._rois[2].id)
        opened = {}

        def look(self):
            opened["far"] = (self.f0.value(), self.f1.value())
            return QDialog.DialogCode.Rejected
        monkeypatch.setattr(ZoneSplitDialog, "exec", look)
        window._split_zones()
        assert opened["far"] == (170, 181)
        # the ROI set keeps it
        path = tmp_path / "set.rois.json"
        monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(path), "")))
        monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(path), "")))
        window._save_roi_set()
        window._load_roi_set()
        assert window._zone_groups[0]["spec"].far == (170.0, 182.0)
        # joining back gives the box as drawn; a new box starts without a slant
        window._select_roi(window._rois[0].id)
        monkeypatch.setattr(ZoneSplitDialog, "exec", lambda self: ZoneSplitDialog.JOIN)
        window._split_zones()
        assert [roi.points for roi in window._rois] == [drawn]
        monkeypatch.setattr(ZoneSplitDialog, "exec", look)
        window._split_zones()
        assert opened["far"] == (200, 211)
    finally:
        window.close()


# --- Codex review of 0.7.4 (run 20261014-161126-review-v074-slant) -------------------------------------------


def test_r1_a_band_of_constant_height_keeps_it_at_exact_half_ties() -> None:
    # cell 13 spans columns 147-157: its rows are exactly 19.5-36.5, rounded half up to 20-36 (floats gave 19-36)
    zones = dict(split_box(ZoneSpec(box=((0, 50), (220, 67)), count=18, gap=1, start="left", far=(6, 23))))
    assert zones["Cell 13"] == ((147.0, 20.0), (158.0, 37.0))
    assert {b[1] - a[1] for a, b in zones.values()} == {17.0}
    shapes = _shapes(split_box(ZoneSpec(box=((0, 50), (220, 67)), count=18, gap=1, start="left", far=(6, 23))))
    assert "Cell 13" not in pair_tcs({"X": (19, 150)}, shapes, H, W).pairs.values()
    # negative rows: a one-row band stays one row in every zone
    thin = split_box(ZoneSpec(box=((0, -64), (5, -63)), count=4, gap=0, start="left", far=(-19, -18)))
    assert all(b[1] - a[1] == 1.0 for _n, (a, b) in thin)


def test_r2_the_slant_rounds_like_the_box_corners() -> None:
    # far equal to the box's own fractional rows is no slant, whatever the rounding of .5
    box = ((0.0, 0.5), (4.0, 10.5))
    flat = split_box(ZoneSpec(box=box, count=2, gap=0, start="left"))
    assert split_box(ZoneSpec(box=box, count=2, gap=0, start="left", far=(0.5, 10.5))) == flat
    assert split_box(ZoneSpec(box=BOX, far=(357.6, 374.6))) == split_box(ZoneSpec(box=BOX, far=FAR))


def test_r3_the_dialog_rounds_a_saved_fractional_slant(qapp) -> None:
    dialog = ZoneSplitDialog(ZoneSpec(box=BOX, far=(357.6, 374.6)), W, H, regroup=True)
    dialog.show()
    qapp.processEvents()
    assert (dialog.f0.value(), dialog.f1.value()) == (358, 374) and dialog.spec().far == FAR
    dialog.close()
    broken = ZoneSplitDialog(ZoneSpec(box=BOX, far=(380.0, 380.0)), W, H, regroup=True)  # covers no row
    broken.show()
    qapp.processEvents()
    assert (broken.f0.value(), broken.f1.value()) == (388, 404) and broken.spec().far is None
    broken.close()


@pytest.mark.parametrize(("box", "far", "start"), [
    (((0.0, 10.6), (40.0, 20.6)), (10.6, 20.6), "left"),
    (((10.6, 0.0), (20.6, 40.0)), (10.6, 20.6), "top"),
])
def test_r5_a_saved_fractional_box_with_an_equal_slant_reopens_flat(qapp, box, far, start) -> None:
    spec = ZoneSpec(box=box, count=2, gap=0, start=start, far=far)
    dialog = ZoneSplitDialog(spec, W, H, regroup=True)
    dialog.show()
    qapp.processEvents()
    assert dialog.spec().far is None
    assert dialog.zones() == split_box(ZoneSpec(box=box, count=2, gap=0, start=start), W, H)
    dialog.close()


@pytest.mark.parametrize("bad", ["12", {"358": None, "375": "x"}, [False, True], [358, None], "358,375"])
def test_r4_a_slant_that_is_not_two_numbers_is_refused(tmp_path, bad) -> None:
    spec = ZoneSpec(box=BOX, count=3, gap=0, far=FAR)
    zones = [RoiShape(k + 1, "rect", points, name) for k, (name, points) in enumerate(split_box(spec))]
    path = tmp_path / "a.rois.json"
    save_roi_set(path, RoiSet(rois=tuple(zones), zone_groups=(ZoneGroup(rois=(0, 1, 2), spec=spec),)))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["zone_groups"][0]["far"] = bad
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Zone group 1"):
        load_roi_set(path)
