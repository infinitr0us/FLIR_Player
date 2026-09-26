# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from .models import VideoMetadata
from .jobs import validate_destination, unique_destination
from .widgets import FramelessDialog, browse_button


class ExtractDialog(FramelessDialog):
    """Collect extract (trim/decimate) parameters — ResearchIR §4.9.1.2.

    The FLIR File SDK only produces output for ATS sources written to an
    ``.ats`` destination, so the dialog enforces the extension and warns for
    other source formats.
    """

    def __init__(
        self,
        metadata: VideoMetadata,
        parent: QWidget | None = None,
        default_range: tuple[int, int] | None = None,
    ) -> None:
        super().__init__("Extract Clip", parent)
        self.setMinimumWidth(460)
        self._metadata = metadata

        layout = self.body

        form = QFormLayout()
        form.setSpacing(8)

        source_label = QLabel(metadata.filename)
        source_label.setObjectName("InfoValue")
        form.addRow("Source", source_label)

        total = metadata.num_frames
        first, last = 1, total
        if default_range is not None:
            first = max(1, min(default_range[0] + 1, total))
            last = max(first, min(default_range[1] + 1, total))
        self.start_spin = QSpinBox()
        self.start_spin.setRange(1, total)
        self.start_spin.setValue(first)
        form.addRow("Start frame", self.start_spin)

        self.end_spin = QSpinBox()
        self.end_spin.setRange(1, total)
        self.end_spin.setValue(last)
        form.addRow("End frame", self.end_spin)

        self.decimation_spin = QSpinBox()
        self.decimation_spin.setRange(1, max(1, total))
        self.decimation_spin.setValue(1)
        self.decimation_spin.setToolTip("1 keeps every frame, N keeps every Nth frame")
        form.addRow("Keep every Nth", self.decimation_spin)

        output_row = QHBoxLayout()
        output_row.setSpacing(8)
        default_name = unique_destination(metadata.path.parent / f"{metadata.path.stem}_extract.ats", [metadata.path])
        self.output_edit = QLineEdit(str(default_name))
        output_row.addWidget(self.output_edit, 1)
        browse = browse_button("Choose output file")
        browse.clicked.connect(self._browse_output)
        output_row.addWidget(browse)
        form.addRow("Output", output_row)

        layout.addLayout(form)

        frames_label = QLabel(f"{total} frames total · numbering starts at 1")
        frames_label.setObjectName("FieldLabel")
        layout.addWidget(frames_label)

        self.warning_label = QLabel()
        self.warning_label.setObjectName("FieldLabel")
        self.warning_label.setWordWrap(True)
        if metadata.path.suffix.lower() != ".ats":
            self.warning_label.setText(
                "Note: the FLIR File SDK only supports extraction from ATS "
                "recordings; extraction from this file may produce no output."
            )
        else:
            self.warning_label.setText("Output is written in ATS format.")
        layout.addWidget(self.warning_label)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText("Extract")
        ok_button.setProperty("accent", True)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self.start_spin.valueChanged.connect(self._validate)
        self.end_spin.valueChanged.connect(self._validate)
        self.output_edit.textChanged.connect(self._validate)
        self._validate()

    def _browse_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Choose extract destination",
            self.output_edit.text(),
            "ATS recording (*.ats)",
        )
        if path:
            self.output_edit.setText(path)

    def _validate(self) -> None:
        valid = self.start_spin.value() <= self.end_spin.value()
        try:
            if not self.output_edit.text().strip():
                raise ValueError("Choose an output filename")
            dest = Path(self.parameters()["dest"])
            validate_destination(dest, [self._metadata.path])
        except (OSError, ValueError) as exc:
            valid = False
            self.warning_label.setText(str(exc))
        else:
            self.warning_label.setText("Output uses a new ATS filename; existing files are preserved."
                if self._metadata.path.suffix.lower() == ".ats" else
                "The File SDK only supports extraction from ATS recordings.")
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(valid)

    def parameters(self) -> dict:
        """Zero-based inclusive frame range + decimation + destination path."""
        dest = Path(self.output_edit.text()).expanduser()
        if dest.suffix.lower() != ".ats":
            dest = dest.with_suffix(".ats")
        return {
            "dest": str(dest),
            "start_frame": self.start_spin.value() - 1,
            "end_frame": self.end_spin.value() - 1,
            "decimation": self.decimation_spin.value(),
        }
