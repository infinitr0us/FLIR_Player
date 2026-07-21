# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

from conftest import wait_until

from PySide6.QtWidgets import QFileDialog

from flir_player.main_window import MainWindow
from flir_player.widgets import MetadataPanel, MetadataPickerDialog


ROOT = Path(__file__).resolve().parents[1]


def test_statistics_panel_tracks_rois_and_saves_csv(qapp, tmp_path, monkeypatch) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(ROOT / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)
        panel = window.bottom_panel.statistics
        assert not window.bottom_panel.isVisible()

        # first ROI auto-opens the statistics panel
        window._add_roi("rect", ((100.0, 100.0), (150.0, 140.0)))
        assert window.bottom_panel.isVisible()
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 1)
        headers = [
            panel.table.horizontalHeaderItem(column).text()
            for column in range(panel.table.columnCount())
        ]
        assert headers == ["Image", "Box 1"]
        assert panel.table.rowCount() == len(panel.METRICS)

        # whole-image column matches the packet statistics
        mean_row = panel.METRICS.index("Mean")
        image_cell = panel.table.item(mean_row, 0).text()
        assert image_cell.startswith(f"{window.current_packet.mean:.0f}")

        # spot ROI adds a Value row entry
        window._add_roi("cursor", ((320.0, 240.0),))
        assert wait_until(qapp, lambda: len(window.current_packet.roi_stats) == 2)
        value_row = panel.METRICS.index("Value")
        spot_cell = panel.table.item(value_row, 2).text()
        expected = float(window.current_packet.data[240, 320])
        assert spot_cell.startswith(f"{expected:.0f}")
        assert panel.table.item(value_row, 1).text() == "—"  # box has no cursor value

        # pause freezes the table
        panel.pause_toggle.setChecked(True)
        before = [panel.table.item(0, c).text() for c in range(panel.table.columnCount())]
        panel.set_statistics((), None, "counts")
        after = [panel.table.item(0, c).text() for c in range(panel.table.columnCount())]
        assert before == after
        panel.pause_toggle.setChecked(False)
        window._update_statistics()  # repopulate from the current packet

        # image column can be hidden
        panel.image_toggle.setChecked(False)
        headers = [
            panel.table.horizontalHeaderItem(column).text()
            for column in range(panel.table.columnCount())
        ]
        assert headers == ["Box 1", "Spot 1"]
        panel.image_toggle.setChecked(True)

        # save to CSV
        save_path = tmp_path / "stats.csv"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            lambda *args, **kwargs: (str(save_path), "CSV table (*.csv)"),
        )
        window._save_statistics()
        content = save_path.read_text(encoding="utf-8")
        assert "Box 1" in content and "Mean" in content
        assert "2.seq" in content
    finally:
        window.close()
        qapp.processEvents()


def test_source_and_metadata_tabs(qapp) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(ROOT / "2.seq")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)

        # source tab is populated once per file
        source_table = window.bottom_panel.source.table
        assert source_table.rowCount() > 5
        keys = [source_table.item(row, 0).text() for row in range(source_table.rowCount())]
        assert "Camera model" in keys

        # metadata tab follows the current frame
        window._toggle_statistics(True)
        window.seek_to(10)
        assert wait_until(qapp, lambda: window.current_packet.index == 10)
        metadata_table = window.bottom_panel.metadata.table
        entries = {
            metadata_table.item(row, 0).text(): metadata_table.item(row, 1).text()
            for row in range(metadata_table.rowCount())
        }
        assert entries.get("FrameNumber") == "10"
    finally:
        window.close()
        qapp.processEvents()


def test_metadata_panel_hides_chosen_entries(qapp) -> None:
    panel = MetadataPanel()
    panel.set_entries((("Time", "12:00"), ("Preset", "0"), ("FrameNumber", "7")))
    assert panel.table.rowCount() == 3
    panel._hidden = {"Preset"}
    panel._render()
    assert panel.table.rowCount() == 2
    names = [panel.table.item(row, 0).text() for row in range(panel.table.rowCount())]
    assert "Preset" not in names

    dialog = MetadataPickerDialog(["Time", "Preset"], hidden=set())
    dialog._boxes["Time"].setChecked(False)
    assert dialog.hidden_names() == {"Time"}
    dialog.close()