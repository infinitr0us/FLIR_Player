# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

from conftest import wait_until

from flir_player.main_window import MainWindow
from flir_player.source import FlirVideoSource


ROOT = Path(__file__).resolve().parents[1]


def test_corrections_state_and_safe_toggle() -> None:
    source = FlirVideoSource()
    try:
        source.open(ROOT / "2.seq")
        state = source.read_corrections()
        # this recording carries no embedded corrections (apply flags are inert defaults)
        assert state["has_nuc"] is False
        assert state["has_bp"] is False
        # toggling on a file without embedded corrections must not change has_* flags
        updated = source.set_corrections(nuc=True, bp=True)
        assert updated["has_nuc"] is False
        assert updated["has_bp"] is False
    finally:
        source.close()


def test_corrections_toggles_hidden_without_embedded_data(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(ROOT / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        assert not window.inspector.corrections_row.isVisible()
        assert not window.inspector.nuc_check.isVisible()
        assert not window.inspector.bp_check.isVisible()
    finally:
        window.close()
        qapp.processEvents()
