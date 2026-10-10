# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cell zones: splitting a box, pairing TC pixels with ROIs, short labels."""
from __future__ import annotations

import numpy as np
import pytest

from flir_player.geometry import roi_coordinates
from flir_player.models import RoiShape
from flir_player.zones import ZoneSpec, normalized_box, pair_tcs, short_label, split_box

W, H = 640, 480


def _shapes(zones):
    return [RoiShape(id=k + 1, kind="rect", points=points, name=name) for k, (name, points) in enumerate(zones)]


def _columns(shape):
    _ys, xs = roi_coordinates(shape, H, W)
    return sorted(set(xs.tolist()))


def test_eighteen_zones_from_the_right_end_partition_the_box_with_one_pixel_gaps() -> None:
    # the 0903 T650 module: columns 245-404, top slice rows 251-258
    zones = split_box(ZoneSpec(box=((245, 251), (405, 259)), count=18, start="right", gap=1), W, H)
    shapes = _shapes(zones)
    assert [s.name for s in shapes] == [f"Cell {k}" for k in range(1, 19)]
    columns = [_columns(s) for s in shapes]
    # cell 1 holds the rightmost column, cell 18 the leftmost; rows follow the box
    assert columns[0][-1] == 404 and columns[-1][0] == 245
    for shape in shapes:
        ys, _xs = roi_coordinates(shape, H, W)
        assert set(ys.tolist()) == set(range(251, 259))
    # neighbours never share a column, and exactly one column is left out at each boundary
    for right_zone, left_zone in zip(columns, columns[1:]):
        assert left_zone[-1] < right_zone[0]
        assert right_zone[0] - left_zone[-1] == 2
    used = sum(len(c) for c in columns)
    assert used == 160 - 17
    # the 0903 TC pixels land in cells 3, 6 and 9
    pairing = pair_tcs({"T1": (256, 380), "S2": (255, 353), "S3": (256, 332)}, shapes, H, W)
    assert pairing.pairs == {"T1": "Cell 3", "S2": "Cell 6", "S3": "Cell 9"}


def test_no_gap_partitions_every_column_and_ends_are_mirrored() -> None:
    right = _shapes(split_box(ZoneSpec(box=((100, 10), (190, 20)), count=9, start="right", gap=0), W, H))
    left = _shapes(split_box(ZoneSpec(box=((100, 10), (190, 20)), count=9, start="left", gap=0), W, H))
    cols_r = [_columns(s) for s in right]
    cols_l = [_columns(s) for s in left]
    assert sorted(c for cols in cols_r for c in cols) == list(range(100, 190))
    assert cols_r == cols_l[::-1]
    assert all(len(c) == 10 for c in cols_r)


def test_vertical_split_from_the_top_and_bottom() -> None:
    top = _shapes(split_box(ZoneSpec(box=((50, 100), (60, 160)), count=6, start="top", gap=0), W, H))
    bottom = _shapes(split_box(ZoneSpec(box=((50, 100), (60, 160)), count=6, start="bottom", gap=0), W, H))
    rows_top = [sorted(set(roi_coordinates(s, H, W)[0].tolist())) for s in top]
    assert rows_top[0] == list(range(100, 110)) and rows_top[-1] == list(range(150, 160))
    rows_bottom = [sorted(set(roi_coordinates(s, H, W)[0].tolist())) for s in bottom]
    assert rows_bottom == rows_top[::-1]


def test_corners_in_any_order_and_fractional_corners_follow_box_rounding() -> None:
    a = split_box(ZoneSpec(box=((405.3, 258.6), (244.8, 251.2)), count=18))
    b = split_box(ZoneSpec(box=((245, 251), (405, 259)), count=18))
    assert a == b
    assert normalized_box(((10.4, 7.6), (3.5, 2.2))) == ((4.0, 2.0), (10.0, 8.0))  # round half to even


@pytest.mark.parametrize("spec, message", [
    (ZoneSpec(box=((0, 0), (20, 5)), count=18, gap=1), "at least 35 px"),
    (ZoneSpec(box=((0, 0), (20, 5)), count=0), "whole number"),
    (ZoneSpec(box=((0, 0), (20, 5)), start="middle"), "Zone 1 must be"),
    (ZoneSpec(box=((0, 0), (20, 5)), count=2, gap=-1), "not negative"),
    (ZoneSpec(box=((0, 0), (20, 5)), count=2, gap=0.5), "whole number of pixels"),
    (ZoneSpec(box=((0, 0), (0, 5)), count=2), "covers no pixel"),
])
def test_invalid_splits_are_refused_with_a_reason(spec, message) -> None:
    with pytest.raises(ValueError, match=message):
        split_box(spec, W, H)


def test_a_zone_outside_the_image_is_refused() -> None:
    with pytest.raises(ValueError, match="cover no pixel"):
        split_box(ZoneSpec(box=((630, 10), (700, 20)), count=7, gap=0), W, H)


def test_prefix_is_tidied_and_single_zone_keeps_the_box() -> None:
    zones = split_box(ZoneSpec(box=((5, 5), (25, 9)), count=1, prefix="  Module   side "))
    assert zones == [("Module side 1", ((5.0, 5.0), (25.0, 9.0)))]
    assert split_box(ZoneSpec(box=((5, 5), (25, 9)), count=2, prefix=" "))[0][0] == "Zone 1"


def test_pairing_prefers_the_smallest_roi_and_reports_shared_and_outside() -> None:
    search = RoiShape(1, "rect", ((200, 200), (460, 310)), "Search")
    zones = _shapes(split_box(ZoneSpec(box=((245, 251), (405, 259)), count=18), W, H))
    line = RoiShape(99, "line", ((300, 255), (400, 255)), "Profile")
    pixels = {"S2": (255, 353), "T2": (255, 354), "T1": (256, 380), "Far": (100, 100), "Low": (290, 300)}
    pairing = pair_tcs(pixels, [search, line, *zones], H, W)
    # S2 and T2 share cell 6 (the first wins); the search box only takes what no zone holds
    assert pairing.pairs == {"S2": "Cell 6", "T1": "Cell 3", "Low": "Search"}
    assert pairing.shared == {"Cell 6": ["S2", "T2"]}
    assert pairing.outside == ["Far"]
    text = pairing.describe()
    assert "S2 → Cell 6" in text and "Far is in no ROI" in text and "Cell 6 holds S2, T2 (paired with S2)" in text


def test_spot_on_the_pixel_wins_and_empty_pairing_text() -> None:
    zone = RoiShape(1, "rect", ((350, 250), (360, 260)), "Cell 6")
    spot = RoiShape(2, "cursor", ((353.5, 255.5),), "S2 spot")
    assert pair_tcs({"S2": (255, 353)}, [zone, spot], H, W).pairs == {"S2": "S2 spot"}
    assert pair_tcs({}, [zone], H, W).describe() == "No TC pixel lies in an ROI"


@pytest.mark.parametrize("name, short", [("Cell 12", "12"), ("Box 3", "3"), ("T1 3×3", None), ("Cell", None),
                                          ("Zone 1a", None), ("7", None)])
def test_short_label(name, short) -> None:
    assert short_label(name) == short


def test_zone_pixels_have_equal_size_within_one_column() -> None:
    zones = _shapes(split_box(ZoneSpec(box=((245, 251), (405, 259)), count=18, gap=1), W, H))
    widths = np.array([len(_columns(s)) for s in zones])
    assert widths.max() - widths.min() <= 1
