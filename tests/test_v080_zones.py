# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""0.8.0: four-sided cell zones. The engineers asked (16 October 2026) for zones that follow "the twist in the 3D
view" and the cells' different distances to the camera. On the 1006a T650 (FLIR2267.csq) the cells burn one after
another, and a map of when each pixel heats up shows their dividers leaning ~0.2 columns per row (~3 px across the
17 px top face), with cell widths equal from end to end. Zones are now quads: the band's edges run straight along
a slanted module, dividers lean by a set number of pixels, widths follow the camera's distance to each end
(perspective), and a divider can be dragged by a zone's corner (both zones beside it follow)."""
from __future__ import annotations

import json
import random
from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QDialog

from flir_player.excel_export import RoiSet, ZoneGroup, load_roi_set, map_scale, save_roi_set
from flir_player.geometry import area_extent, roi_coordinates
from flir_player.models import RoiShape
from flir_player.zone_dialog import ZoneSplitDialog
from flir_player.zones import (ZONE_KIND, ZoneSpec, corner_divider, divider_positions, move_divider, pair_tcs,
                               split_box, translate_spec)

W, H = 640, 480
SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"
# 1006a T650: columns 160-431, top face rows 395-411 at the far end (left), 359-375 at the heater end (right)
BOX = ((160.0, 395.0), (432.0, 412.0))
FAR = (359.0, 376.0)


def _pixels(kind, points, height=H, width=W):
    ys, xs = roi_coordinates(RoiShape(0, kind, tuple(points), ""), height, width)
    return set(zip(ys.tolist(), xs.tolist()))


def _shapes(zones):
    return [RoiShape(k + 1, ZONE_KIND, points, name) for k, (name, points) in enumerate(zones)]


# --- coverage ---------------------------------------------------------------------------------


def test_z1_quad_coverage_matches_boxes_and_refuses_crossed_sides() -> None:
    box = _pixels("rect", ((3, 4), (20, 15)))
    assert _pixels("quad", ((3, 4), (20, 4), (20, 15), (3, 15))) == box
    assert _pixels("quad", ((3, 15), (20, 15), (20, 4), (3, 4))) == box  # either way round
    assert _pixels("quad", ((3.4, 4.4), (19.6, 3.6), (20.4, 15.4), (2.6, 14.6))) == box  # corners round like a box's
    assert _pixels("quad", ((0, 0), (20, 20), (20, 0), (0, 20))) == set()  # sides cross
    assert _pixels("quad", ((0, 0), (10, 0), (10, 0), (0, 10))) == set()  # a repeated corner: no quad
    assert len(_pixels("quad", ((-5, -5), (10, -5), (10, 10), (-5, 10)))) == 100  # clipped to the image
    concave = _pixels("quad", ((0, 0), (20, 0), (5, 5), (0, 20)))
    assert (2, 2) in concave and (10, 10) not in concave and (1, 10) in concave and (10, 1) in concave
    assert area_extent("quad", ((3, 4), (20, 4), (20, 15), (3, 15)), H, W) == (3, 19, 4, 14, 17 * 11)
    assert area_extent("quad", ((0, 0), (20, 20), (20, 0), (0, 20)), H, W) is None


def test_z2_leaning_neighbours_share_no_pixel_and_leave_exactly_the_gap() -> None:
    random.seed(80)
    for _trial in range(300):
        tops = sorted(random.sample(range(5, 300), 6))
        lean = [random.randint(-9, 9) for _ in tops]
        bottoms = [t + l for t, l in zip(tops, lean)]
        if any(b >= c for b, c in zip(bottoms, bottoms[1:])):
            continue
        y0, y1 = random.randint(0, 50), random.randint(60, 120)
        tiles = [_pixels("quad", ((tops[i], y0), (tops[i + 1], y0), (bottoms[i + 1], y1), (bottoms[i], y1)))
                 for i in range(5)]
        band = _pixels("quad", ((tops[0], y0), (tops[-1], y0), (bottoms[-1], y1), (bottoms[0], y1)))
        assert sum(len(t) for t in tiles) == len(set().union(*tiles)) == len(band)
    # with a gap, each inner divider leaves out exactly `gap` pixels on every row of the band
    for gap in (1, 2, 3):
        zones = _shapes(split_box(ZoneSpec(box=((100, 100), (300, 120)), count=10, gap=gap, lean=5)))
        rows = {}
        for shape in zones:
            ys, _xs = roi_coordinates(shape, H, W)
            for y in ys.tolist():
                rows[y] = rows.get(y, 0) + 1
        assert set(rows.values()) == {200 - 9 * gap - 2 * (gap // 2)}, gap


# --- the split -----------------------------------------------------------------------------


def test_z3_dividers_lean_about_the_band_middle_and_keep_t2_in_cell_6() -> None:
    upright = split_box(ZoneSpec(box=BOX, far=FAR), W, H)
    leaning = split_box(ZoneSpec(box=BOX, far=FAR, lean=3), W, H)
    for (_n, up), (_m, lean) in zip(upright, leaning):
        assert lean[3][0] - lean[0][0] in (2.0, 3.0, 4.0) and lean[2][0] - lean[1][0] in (2.0, 3.0, 4.0)
        # the middle line of each divider stays where the upright split has it, to half a pixel and rounding
        assert abs((lean[0][0] + lean[3][0]) / 2 - up[0][0]) <= 1.0
    assert pair_tcs({"T2": (382, 349)}, _shapes(leaning), H, W).pairs == {"T2": "Cell 6"}
    negative = split_box(ZoneSpec(box=BOX, far=FAR, lean=-4), W, H)
    assert all(p[3][0] - p[0][0] == -4.0 for _n, p in negative)
    # a split along the rows leans its dividers' right ends down the column
    column = split_box(ZoneSpec(box=((100, 50), (120, 250)), count=8, start="top", gap=0, lean=6))
    assert all(p[3][1] - p[0][1] == 6.0 for _n, p in column)
    assert column[0][1][0] == (100.0, 47.0)  # zone 1 at the top: its first corner 3 rows above the box


def test_z4_camera_distances_narrow_the_zones_towards_the_far_end() -> None:
    flat = split_box(ZoneSpec(box=BOX, far=FAR))
    same = split_box(ZoneSpec(box=BOX, far=FAR, distances=(3.0, 3.0)))
    assert same == flat  # equal distances: equal widths
    zones = split_box(ZoneSpec(box=BOX, far=FAR, distances=(3.0, 3.6)))
    widths = [p[1][0] - p[0][0] for _n, p in zones]
    assert all(b <= a + 1 for a, b in zip(widths, widths[1:])) and widths[0] > widths[-1] + 3  # whole pixels
    # a straight row receding in depth: widths fall as 1 / (1 + k u)^2, ends (1 + k)^2 = 1.44 apart
    assert 1.3 <= widths[0] / widths[-1] <= 1.6
    assert zones[0][1][1][0] == 432.0 and zones[-1][1][0][0] == 160.0  # the box's ends stay
    mirrored = split_box(ZoneSpec(box=BOX, far=FAR, start="left", distances=(3.0, 3.6)))
    assert [p[1][0] - p[0][0] for _n, p in mirrored][0] == max(widths)  # zone 1 is the near, wide one again
    with pytest.raises(ValueError, match="positive"):
        split_box(ZoneSpec(box=BOX, distances=(0.0, 3.0)))
    with pytest.raises(ValueError, match="lean"):
        split_box(ZoneSpec(box=BOX, lean=500))


def test_z5_a_divider_moved_by_its_corner_moves_both_zones_beside_it() -> None:
    spec = ZoneSpec(box=BOX, far=FAR, lean=3)
    before = dict(split_box(spec, W, H))
    divider, edge, offset = corner_divider(spec, 6, 0)  # zone 6's corner nearer the far end, on the top edge
    assert edge == 0 and offset == Fraction(-1, 2)
    moved = move_divider(spec, divider, edge, 330 + offset)
    after = dict(split_box(moved, W, H))
    assert after["Cell 6"][0][0] == 330.0 and after["Cell 7"][1][0] == 329.0  # the 1 px gap stays between them
    assert after["Cell 6"][3] == before["Cell 6"][3]  # the divider's other end stays
    assert all(after[name] == before[name] for name in before if name not in ("Cell 6", "Cell 7"))
    # zone 7's high-side corner names the same divider
    assert corner_divider(spec, 7, 1)[:2] == (divider, edge)
    # moving it back where the split put it restores the zones exactly
    back = move_divider(moved, divider, edge, divider_positions(spec)[divider][edge])
    assert dict(split_box(back, W, H)) == before
    # a divider dragged past its neighbour is refused, naming the zone
    with pytest.raises(ValueError, match="Cell 7 would be less than a pixel wide"):
        split_box(move_divider(spec, divider, edge, 300), W, H)
    # the outer dividers lose half a gap (rounded down), as the box's ends always did
    outer, edge0, offset0 = corner_divider(spec, 1, 1)
    assert outer == 18 and offset0 == 0
    gap3 = ZoneSpec(box=BOX, gap=3)
    assert corner_divider(gap3, 1, 1)[2] == 1 and corner_divider(gap3, 1, 0)[2] == Fraction(-3, 2)


def test_z6_moving_a_split_moves_its_slant_and_moved_dividers_too() -> None:
    spec = move_divider(ZoneSpec(box=BOX, far=FAR, lean=2), 12, 1, 340)
    shifted = translate_spec(spec, 5, -3)
    for (_n, a), (_m, b) in zip(split_box(spec), split_box(shifted)):
        assert all((bx - ax, by - ay) == (5.0, -3.0) for (ax, ay), (bx, by) in zip(a, b))
    column = move_divider(ZoneSpec(box=((100, 50), (120, 250)), count=8, start="top", far=(104, 126)), 3, 0, 120)
    for (_n, a), (_m, b) in zip(split_box(column), split_box(translate_spec(column, -2, 7))):
        assert all((bx - ax, by - ay) == (-2.0, 7.0) for (ax, ay), (bx, by) in zip(a, b))


def test_z7_edges_on_exact_pixel_boundaries_no_longer_slip_a_pixel() -> None:
    # 26 zones over rows 3-228: zone boundary 13 is exactly row 116 (floats put it at 115.999... -> 115)
    zones = dict(split_box(ZoneSpec(box=((100, 3), (110, 229)), count=26, start="top", gap=1)))
    assert zones["Cell 13"][2][1] == 116.0 and zones["Cell 14"][0][1] == 117.0


# --- saved sets -----------------------------------------------------------------------------------


def test_s1_roi_sets_keep_the_lean_distances_and_moved_dividers(tmp_path) -> None:
    spec = move_divider(ZoneSpec(box=BOX, far=FAR, lean=3, distances=(3.0, 3.4), prefix="Cell"), 7, 0, 268.5)
    zones = _shapes(split_box(spec, W, H))
    path = tmp_path / "zones.rois.json"
    save_roi_set(path, RoiSet(rois=tuple(zones), zone_groups=(ZoneGroup(rois=tuple(range(18)), spec=spec),)))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["rois"][0]["kind"] == "quad" and len(payload["rois"][0]["points"]) == 4
    assert payload["zone_groups"][0]["lean"] == 3 and payload["zone_groups"][0]["distances"] == [3.0, 3.4]
    loaded = load_roi_set(path)
    assert loaded.zone_groups[0].spec == spec and loaded.rois == tuple(zones)
    # a plain split writes none of the new keys (0.7.x can read the group)
    plain = tmp_path / "plain.rois.json"
    save_roi_set(plain, RoiSet(rois=(), zone_groups=(ZoneGroup(rois=(0,), spec=ZoneSpec(box=BOX)),)))
    group = json.loads(plain.read_text(encoding="utf-8"))["zone_groups"]
    assert not {"lean", "distances", "moved"} & set(group[0]) if group else True
    for key, bad in (("lean", 2.5), ("lean", True), ("lean", 999), ("distances", [3.0]), ("distances", [0, 3]),
                     ("distances", ["3", 4]), ("moved", [[1, 2.0]]), ("moved", [[99, 1.0, 2.0]]),
                     ("moved", [[1, 2.0, 3.0], [1, 4.0, 5.0]]), ("moved", [[True, 1.0, 2.0]])):
        broken = json.loads(path.read_text(encoding="utf-8"))
        broken["zone_groups"][0][key] = bad
        path.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(ValueError, match="Zone group 1"):
            load_roi_set(path)
    broken = json.loads(plain.read_text(encoding="utf-8"))
    broken["rois"] = [{"name": "Cell 1", "kind": "quad", "points": [[0, 0], [1, 0], [1, 1]]}]
    plain.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(ValueError, match="ROI 1"):
        load_roi_set(plain)


def test_s2_the_roi_map_scale_sees_narrow_quads() -> None:
    zones = _shapes(split_box(ZoneSpec(box=BOX, far=FAR), W, H))
    assert map_scale(zones, W) == 2  # 14-15 px zones: twice as large for room for their numbers


# --- the dialog ------------------------------------------------------------------------------


def test_d1_dialog_sets_lean_and_distances_and_keeps_moved_dividers_only_for_the_same_count(qapp) -> None:
    seen = []
    dialog = ZoneSplitDialog(ZoneSpec(box=BOX, far=FAR), W, H, tc_pixels={"T2": (382, 349)})
    dialog.preview.connect(seen.append)
    dialog.show()
    qapp.processEvents()
    assert not dialog.keep_moved.isVisible()
    dialog.lean_spin.setValue(3)
    assert dialog.spec().lean == 3 and seen[-1] == split_box(ZoneSpec(box=BOX, far=FAR, lean=3), W, H)
    assert "Dividers lean 3 px" in dialog.info_label.text() and "T2 → Cell 6" in dialog.tc_label.text()
    dialog.near_distance.setValue(3.0)  # one distance only: refused
    ok = dialog.button_box.button(dialog.button_box.StandardButton.Ok)
    assert not ok.isEnabled() and "both ends" in dialog.info_label.text() and seen[-1] == []
    dialog.far_distance.setValue(3.6)
    assert ok.isEnabled() and dialog.spec().distances == (3.0, 3.6) and "narrow" in dialog.info_label.text()
    dialog.near_distance.setValue(0.0)
    dialog.far_distance.setValue(0.0)
    assert dialog.spec().distances is None
    dialog.close()
    moved = move_divider(ZoneSpec(box=BOX, far=FAR, lean=2, distances=(3.0, 3.3)), 9, 1, 300)
    again = ZoneSplitDialog(moved, W, H, regroup=True)
    again.show()
    qapp.processEvents()
    assert again.keep_moved.isVisible() and again.keep_moved.isChecked()
    assert again.lean_spin.value() == 2 and (again.near_distance.value(), again.far_distance.value()) == (3.0, 3.3)
    assert again.spec() == moved and "1 divider moved by hand" in again.info_label.text()
    again.count_spin.setValue(17)
    assert not again.keep_moved.isEnabled() and again.spec().moved == ()
    again.count_spin.setValue(18)
    assert again.spec().moved == moved.moved
    again.keep_moved.setChecked(False)
    assert again.spec().moved == ()
    again.close()


# --- the player -------------------------------------------------------------------------------


def _mouse(type_, pos, button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton):
    return QMouseEvent(type_, QPointF(pos), button, buttons, Qt.KeyboardModifier.NoModifier)


@pytest.mark.skipif(not (SAMPLES / "2.seq").exists(), reason="needs local/data/2.seq")
def test_m1_zones_are_quads_and_drag_by_divider_or_as_a_whole(qapp, tmp_path, monkeypatch) -> None:
    from conftest import wait_until

    from flir_player import main_window as mw
    from flir_player.main_window import MainWindow

    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy, timeout=20)
        window._add_roi("rect", ((40.0, 200.0), (220.0, 216.0)))

        def accept(self):
            self.show()
            self.count_spin.setValue(9)
            self.lean_spin.setValue(4)
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(ZoneSplitDialog, "exec", accept)
        window._split_zones()
        zones = list(window._rois)
        assert [z.kind for z in zones] == ["quad"] * 9 and window._zone_groups[0]["spec"].lean == 4
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 9)  # measured by the app
        stats = {s.id: s for s in window.current_packet.roi_stats}
        assert all(stats[z.id].num_pixels == len(roi_coordinates(z, window.metadata.height,
                                                                 window.metadata.width)[0]) for z in zones)
        # a corner drag through the canvas moves one divider: zones 4 and 5 change, the others stay
        canvas = window.canvas
        canvas.repaint()
        window._select_roi(zones[3].id)
        corner = zones[3].points[0]  # zone 4's corner on the far side, top edge
        grab = canvas._image_to_widget(*corner)
        canvas.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, grab))
        target = canvas._image_to_widget(corner[0] - 3, corner[1] + 2)  # across the band: ignored
        canvas.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, target))
        assert set(canvas._drag_overrides) == {z.id for z in zones}  # shown live
        canvas.mouseReleaseEvent(_mouse(QMouseEvent.Type.MouseButtonRelease, target, buttons=Qt.MouseButton.NoButton))
        after = {z.id: z for z in window._rois}
        changed = [z.name for z in zones if after[z.id].points != z.points]
        assert changed == ["Cell 4", "Cell 5"]
        assert after[zones[3].id].points[0] == (corner[0] - 3, corner[1])  # moved along the row only
        spec = window._zone_groups[0]["spec"]
        assert len(spec.moved) == 1 and dict(split_box(spec))["Cell 4"] == after[zones[3].id].points
        # dragging a zone's body moves the whole split by whole pixels
        middle = canvas._image_to_widget(*[sum(c) / 4 for c in zip(*after[zones[6].id].points)])
        canvas.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, middle))
        canvas.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, middle + QPointF(0, -40)))
        canvas.mouseReleaseEvent(_mouse(QMouseEvent.Type.MouseButtonRelease, middle + QPointF(0, -40),
                                        buttons=Qt.MouseButton.NoButton))
        shifted = {z.id: z for z in window._rois}
        dy = shifted[zones[0].id].points[0][1] - after[zones[0].id].points[0][1]
        assert dy < 0 and all(all(b == (a[0], a[1] + dy) for a, b in zip(after[i].points, shifted[i].points))
                              for i in after)
        assert window._zone_groups[0]["spec"] == translate_spec(spec, 0, int(dy))
        # a corner dragged onto the next divider keeps the last good state
        state = window._zone_drag(shifted[zones[3].id], "c0", (shifted[zones[3].id].points[1][0] + 5, 0), (0, 0))
        assert state == shifted or all(state[i].points == shifted[i].points for i in state)
        # the moved divider survives saving and loading the ROI set
        path = tmp_path / "set.rois.json"
        monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(path), "")))
        monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(path), "")))
        window._save_roi_set()
        window._load_roi_set()
        assert [z.points for z in window._rois] == [shifted[z.id].points for z in zones]
        assert window._zone_groups[0]["spec"] == translate_spec(spec, 0, int(dy))
        # boxes (and 0.7.x box zones) still drag on their own
        window._add_roi("rect", ((300.0, 100.0), (340.0, 130.0)))
        box = window._rois[-1]
        assert window._zone_drag(box, "body", (10, 10), (0, 0)) is None
    finally:
        window.close()
        qapp.processEvents()


# --- the 0.8.0 review (Codex, 16 October 2026) -----------------------------------------------------


def test_r1_a_lean_much_steeper_than_the_band_is_refused_rather_than_overlapping() -> None:
    spec = ZoneSpec(box=((30, 30), (77, 34)), count=3, start="left", gap=1, far=(18, 23), lean=25)
    with pytest.raises(ValueError, match="would share pixels"):
        split_box(spec, 120, 100)
    # the 1006a geometry with any sensible lean keeps every pixel in one zone
    for lean in range(-8, 9):
        zones = _shapes(split_box(ZoneSpec(box=BOX, far=FAR, lean=lean), W, H))
        pixels = [p for z in zones for p in zip(*[a.tolist() for a in roi_coordinates(z, H, W)])]
        assert len(pixels) == len(set(pixels)), lean


def test_r2_moving_one_divider_end_keeps_the_other_end_exactly() -> None:
    spec = ZoneSpec(box=((10, 10), (79, 30)), count=18, start="left", gap=1, lean=3, distances=(3.0, 3.3))
    before = dict(split_box(spec))
    divider, edge, offset = corner_divider(spec, 4, 1)  # Cell 4's upper corner on its far side
    assert before["Cell 4"][1][0] == 24.0
    after = dict(split_box(move_divider(spec, divider, edge, 25 + offset)))
    assert after["Cell 4"][1][0] == 25.0 and after["Cell 4"][2] == before["Cell 4"][2]  # the lower end stays
    assert after["Cell 5"][3] == before["Cell 5"][3]
    # repeated moves of other dividers never drift the ones already moved
    spec2 = move_divider(spec, divider, edge, 25 + offset)
    for d in (2, 7, 11):
        spec2 = move_divider(spec2, d, 1, divider_positions(spec2)[d][1] + 1)
    assert dict(split_box(spec2))["Cell 4"][1:3] == after["Cell 4"][1:3]


def test_r3_absurd_coordinates_cover_nothing_instead_of_overflowing() -> None:
    big = 10 ** 10
    assert _pixels("quad", ((-big, -big), (big, -big), (big, big), (-big, big)), 10, 10) == set()
    assert area_extent("quad", ((-big, -big), (big, -big), (big, big), (-big, big)), 10, 10) is None


def test_r4_label_room_of_a_zone_leaning_more_than_it_is_wide() -> None:
    from flir_player.geometry import quad_label_width

    assert quad_label_width(((0, 0), (10, 0), (40, 4), (30, 4))) == 10  # not the 30 px of its lean
    assert quad_label_width(((0, 0), (10, 0), (10, 40), (0, 40))) == 10
    assert quad_label_width(((0, 0), (0, 10), (40, 10), (40, 0))) == 40  # a zone of a split along the rows


@pytest.mark.skipif(not (SAMPLES / "2.seq").exists(), reason="needs local/data/2.seq")
def test_r5_zones_left_after_a_delete_and_reload_keep_their_own_places(qapp, tmp_path, monkeypatch) -> None:
    from conftest import wait_until

    from flir_player import main_window as mw
    from flir_player.main_window import MainWindow

    window = MainWindow()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy, timeout=20)
        window._add_roi("rect", ((20.0, 20.0), (110.0, 60.0)))

        def accept(self):
            self.show()
            self.count_spin.setValue(3)
            self.start_combo.setCurrentIndex(self.start_combo.findData("left"))
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(ZoneSplitDialog, "exec", accept)
        window._split_zones()
        before = {roi.name: roi.points for roi in window._rois}
        window._delete_roi(next(roi.id for roi in window._rois if roi.name == "Cell 1"))
        path = tmp_path / "set.rois.json"
        monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(path), "")))
        monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(path), "")))
        window._save_roi_set()
        window._load_roi_set()
        cell2 = next(roi for roi in window._rois if roi.name == "Cell 2")
        moved = window._zone_drag(cell2, "body", (11.0, 0.0), (10.0, 0.0))
        shapes = {shape.name: shape.points for shape in moved.values()}
        assert set(shapes) == {"Cell 2", "Cell 3"}
        for name in shapes:
            assert shapes[name] == tuple((x + 1, y) for x, y in before[name])
    finally:
        window.close()
        qapp.processEvents()


@pytest.mark.skipif(not (SAMPLES / "2.seq").exists(), reason="needs local/data/2.seq")
def test_r6_a_cancelled_preview_never_comes_back(qapp, monkeypatch) -> None:
    from conftest import wait_until

    from flir_player.main_window import MainWindow

    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy, timeout=20)
        window._add_roi("rect", ((40.0, 200.0), (220.0, 216.0)))

        def accept(self):
            self.show()
            self.count_spin.setValue(9)
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(ZoneSplitDialog, "exec", accept)
        window._split_zones()
        zones = list(window._rois)
        canvas = window.canvas
        canvas.repaint()
        window._select_roi(zones[3].id)
        corner = zones[3].points[0]
        grab = canvas._image_to_widget(*corner)
        canvas.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, grab))
        canvas.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, canvas._image_to_widget(corner[0] - 3, corner[1])))
        assert canvas._drag_overrides
        canvas.cancel_gesture()  # Esc
        assert [z.points for z in window._rois] == [z.points for z in zones]
        # a second drag straight onto the next divider (invalid) must not bring the cancelled one back
        canvas.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, grab))
        far = canvas._image_to_widget(zones[3].points[1][0] + 4, corner[1])
        canvas.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, far))
        canvas.mouseReleaseEvent(_mouse(QMouseEvent.Type.MouseButtonRelease, far, buttons=Qt.MouseButton.NoButton))
        assert [z.points for z in window._rois] == [z.points for z in zones]
        assert window._zone_groups[0]["spec"].moved == ()
    finally:
        window.close()
        qapp.processEvents()


def test_r7_the_overlap_check_looks_only_at_pixels_that_can_be_measured() -> None:
    # one zone cannot overlap anything; a huge box is checked only inside the image (no 16 TB array)
    huge = ZoneSpec(box=((-1_000_000, -1_000_000), (1_000_000, 1_000_000)), count=1, start="left", gap=0)
    assert len(split_box(huge, 10, 10)) == 1
    two = ZoneSpec(box=((0, -1_000_000), (10, 1_000_000)), count=2, start="left", gap=0, lean=1)
    assert len(split_box(two, 10, 10)) == 2


def test_r8_a_requested_divider_position_lands_on_the_grid_that_survives_saving() -> None:
    spec = ZoneSpec(box=((10, 10), (14, 25)), count=2, start="left", gap=0, lean=3, distances=(0.1, 0.3))
    before = dict(split_box(spec))
    same = move_divider(spec, 1, 0, divider_positions(spec)[1][0])  # the position it has: nothing moves
    assert dict(split_box(same)) == before


@pytest.mark.skipif(not (SAMPLES / "2.seq").exists(), reason="needs local/data/2.seq")
def test_r9_a_new_roi_list_during_a_drag_ends_it_without_moving_a_zone_alone(qapp, monkeypatch) -> None:
    from conftest import wait_until

    from flir_player.main_window import MainWindow

    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy, timeout=20)
        window._add_roi("rect", ((20.0, 20.0), (110.0, 60.0)))

        def accept(self):
            self.show()
            self.count_spin.setValue(3)
            self.start_combo.setCurrentIndex(self.start_combo.findData("left"))
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(ZoneSplitDialog, "exec", accept)
        window._split_zones()
        zones = list(window._rois)
        canvas = window.canvas
        canvas.repaint()
        window._select_roi(zones[0].id)
        corner = zones[0].points[1]  # Cell 1's top corner towards Cell 2
        grab = canvas._image_to_widget(*corner)
        canvas.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, grab))
        moved_to = canvas._image_to_widget(corner[0] + 5, corner[1])
        canvas.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, moved_to))
        canvas.set_rois(zones, zones[0].id)  # the window replaces the list mid-drag
        canvas.mouseReleaseEvent(_mouse(QMouseEvent.Type.MouseButtonRelease, moved_to, buttons=Qt.MouseButton.NoButton))
        assert [z.points for z in window._rois] == [z.points for z in zones]
        assert window._zone_groups[0]["spec"].moved == ()
    finally:
        window.close()
        qapp.processEvents()
