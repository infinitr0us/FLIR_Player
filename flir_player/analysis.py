# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Numerical-domain rules for value-based display controls."""
from __future__ import annotations

from .processing import ProcessingState


def threshold_conversion(old_key: str, new_key: str, state: ProcessingState):
    """Return affine coefficients, or None for incompatible processed domains.

    Factory/user calibrations are different domains. Subtraction yields a
    temperature difference; general file/point operations require a reset.
    """
    old, new = old_key.rsplit("_", 1), new_key.rsplit("_", 1)
    if len(old) != 2 or len(new) != 2 or old[0] != new[0] or not old[0].startswith("temperature_"):
        return None
    if state.point[0] != "none" or state.file_op not in (None, "subtract"):
        return None
    kelvin = {"c": (1.0, 273.15), "f": (5 / 9, 255.3722222222222),
              "k": (1.0, 0.0), "r": (5 / 9, 0.0)}
    a, b = kelvin[old[1]]
    c, d = kelvin[new[1]]
    difference = state.file_op == "subtract" or state.temporal[0] == "subtract"
    return a / c, 0.0 if difference else (b - d) / c


def value_domain(state: ProcessingState | None) -> tuple:
    """The kind of numbers a processing state produces within one unit.

    Spatial filters, temporal average/min/max, object parameters and
    corrections change values but keep them in the unit's own domain. Point
    filters, non-subtract file operations and differences (reference or
    sliding subtraction) produce a different domain.
    """
    state = state or ProcessingState()
    point = tuple(state.point) if state.point[0] != "none" else None
    return (point, state.file_op, state.temporal[0] == "subtract")


def value_transition(old_key: str, old_state: ProcessingState | None,
                     new_key: str, new_state: ProcessingState | None):
    """Affine (factor, offset) carrying value-based settings across a change.

    Returns None when the old numbers mean nothing in the new domain, so the
    settings must be re-seeded from the new frame.
    """
    if value_domain(old_state) != value_domain(new_state):
        return None
    if old_key == new_key:
        return (1.0, 0.0)
    return threshold_conversion(old_key, new_key, new_state or ProcessingState())
