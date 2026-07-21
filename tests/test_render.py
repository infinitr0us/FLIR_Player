# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import numpy as np

from flir_player.render import display_range, format_time, format_value, render_rgb


def test_format_time_handles_minutes_and_hours() -> None:
    assert format_time(0) == "00:00.000"
    assert format_time(89.539) == "01:29.539"
    assert format_time(3661.2) == "1:01:01.200"


def test_display_range_handles_constant_and_reversed_values() -> None:
    data = np.full((2, 3), 7.0)
    low, high = display_range(data)
    assert low < 7.0 < high
    assert display_range(data, 20.0, 10.0) == (10.0, 20.0)


def test_render_rgb_is_contiguous_and_palette_sensitive() -> None:
    data = np.array([[0.0, 0.5, 1.0]], dtype=np.float32)
    iron = render_rgb(data, "Iron", 0.0, 1.0)
    gray = render_rgb(data, "Grayscale", 0.0, 1.0)
    assert iron.shape == (1, 3, 3)
    assert iron.dtype == np.uint8
    assert iron.flags.c_contiguous
    assert not np.array_equal(iron, gray)


def test_value_formatting_respects_unit() -> None:
    assert format_value(6500.4, "counts") == "6500 counts"
    assert format_value(25.125, "°C") == "25.12 °C"
    assert format_value(77.125, "°F") == "77.12 °F"
    assert format_value(518.67, "°R") == "518.67 °R"
    assert format_value(6500.4, "") == "6500"
