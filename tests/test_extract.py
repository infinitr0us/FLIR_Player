# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import wait_until

fnv_file = pytest.importorskip("fnv.file", reason="FLIR File SDK (the fnv package) is not installed")
from PySide6.QtWidgets import QDialog, QDialogButtonBox

from flir_player.extract import ExtractDialog
from flir_player.main_window import MainWindow
from flir_player.models import VideoMetadata
from flir_player.source import FlirVideoSource


SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"


def test_extract_ats_range_and_decimation(tmp_path) -> None:
    source = FlirVideoSource()
    try:
        source.open(SAMPLES / "1.ats")
        dest = tmp_path / "clip.ats"
        ok, message = source.extract(dest, start_frame=0, end_frame=9)
        assert ok, message
        assert dest.is_file()
        extracted = fnv_file.ImagerFile(str(dest))
        assert extracted.num_frames == 10
        assert (extracted.width, extracted.height) == (1280, 720)
        extracted.close()

        every_second = tmp_path / "clip2.ats"
        ok, message = source.extract(every_second, start_frame=0, end_frame=9, decimation=2)
        assert ok, message
        extracted = fnv_file.ImagerFile(str(every_second))
        assert extracted.num_frames == 5
        extracted.close()
    finally:
        source.close()


def test_extract_seq_reports_unsupported(tmp_path) -> None:
    source = FlirVideoSource()
    try:
        source.open(SAMPLES / "2.seq")
        dest = tmp_path / "seq_clip.ats"
        ok, message = source.extract(dest, start_frame=0, end_frame=4)
        assert not ok
        assert "ATS" in message
        assert not dest.exists()  # silent SDK no-op is cleaned up and reported
    finally:
        source.close()


def test_extract_abort_cleans_up(tmp_path) -> None:
    source = FlirVideoSource()
    try:
        source.open(SAMPLES / "1.ats")
        dest = tmp_path / "aborted.ats"
        calls = []

        def progress(current: int, total: int) -> None:
            calls.append(current)

        ok, message = source.extract(
            dest, start_frame=0, end_frame=99, progress=progress, abort=lambda: True
        )
        assert not ok
        assert message == "Extraction cancelled"
        assert calls == [1]  # aborted right after the first progress callback
        assert not dest.exists()
    finally:
        source.close()


def _dialog_metadata(path: str, frames: int = 100) -> VideoMetadata:
    return VideoMetadata(
        path=Path(path),
        width=640,
        height=480,
        num_frames=frames,
        start_time=None,
        end_time=None,
        duration_seconds=frames / 30.0,
        nominal_fps=30.0,
    )


def test_extract_dialog_validation_and_parameters(qapp) -> None:
    dialog = ExtractDialog(_dialog_metadata("recording.seq"))
    try:
        assert "ATS" in dialog.warning_label.text()  # warns about non-ATS source
        dialog.start_spin.setValue(10)
        dialog.end_spin.setValue(20)
        dialog.decimation_spin.setValue(2)
        dialog.output_edit.setText("out\\clip")
        params = dialog.parameters()
        assert params["start_frame"] == 9
        assert params["end_frame"] == 19
        assert params["decimation"] == 2
        assert params["dest"].endswith(".ats")

        ok_button = dialog.button_box.button(QDialogButtonBox.StandardButton.Ok)
        dialog.end_spin.setValue(5)  # before start
        assert not ok_button.isEnabled()
    finally:
        dialog.close()


def test_main_window_extracts_clip(qapp, tmp_path, monkeypatch) -> None:
    window = MainWindow()
    window.show()
    try:
        window.open_path(SAMPLES / "1.ats")
        assert wait_until(qapp, lambda: window.current_packet is not None and not window._busy)

        dest = tmp_path / "ui_clip.ats"
        monkeypatch.setattr(
            ExtractDialog, "exec", lambda self: QDialog.DialogCode.Accepted
        )
        monkeypatch.setattr(
            ExtractDialog,
            "parameters",
            lambda self: {
                "dest": str(dest),
                "start_frame": 0,
                "end_frame": 4,
                "decimation": 1,
            },
        )
        window._open_extract_dialog()
        assert window._extract_progress is not None  # dialog shown synchronously
        assert wait_until(qapp, lambda: window._extract_progress is None)  # finished
        assert dest.is_file()

        extracted = fnv_file.ImagerFile(str(dest))
        assert extracted.num_frames == 5
        extracted.close()
    finally:
        window.close()
        qapp.processEvents()
