# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Export dialogs (ResearchIR §4.9.1.1–3): image, series, movie, batch extract."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .compose import ExportOptions
from .export import EXTENSIONS, MOVIE_FORMATS, STILL_FORMATS
from .models import VideoMetadata
from .widgets import ChevronComboBox, FramelessDialog


class _CompositionBox(QFrame):
    """Shared composition-option checkboxes (§4.9.1.1, p. 61)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("RangePanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)
        title = QLabel("Composition")
        title.setObjectName("FieldLabel")
        layout.addWidget(title)
        self.color_bar_check = QCheckBox("Color bar")
        self.color_bar_check.setChecked(True)
        self.rois_check = QCheckBox("ROIs")
        self.rois_check.setChecked(True)
        self.roi_names_check = QCheckBox("ROI names")
        self.roi_names_check.setChecked(True)
        self.markers_check = QCheckBox("Min/max markers")
        self.timestamp_check = QCheckBox("Timestamp burn-in")
        self.border_check = QCheckBox("Border")
        for check in (
            self.color_bar_check,
            self.rois_check,
            self.roi_names_check,
            self.markers_check,
            self.timestamp_check,
            self.border_check,
        ):
            layout.addWidget(check)
        self.rois_check.toggled.connect(
            lambda checked: self.roi_names_check.setEnabled(checked)
        )

    def options(self) -> ExportOptions:
        return ExportOptions(
            color_bar=self.color_bar_check.isChecked(),
            rois=self.rois_check.isChecked(),
            roi_names=self.roi_names_check.isChecked(),
            markers=self.markers_check.isChecked(),
            timestamp=self.timestamp_check.isChecked(),
            border=self.border_check.isChecked(),
        )


def _path_row(browse_tooltip: str) -> tuple[QHBoxLayout, QLineEdit, QToolButton]:
    row = QHBoxLayout()
    row.setSpacing(8)
    edit = QLineEdit()
    row.addWidget(edit, 1)
    browse = QToolButton()
    browse.setText("…")
    browse.setToolTip(browse_tooltip)
    row.addWidget(browse)
    return row, edit, browse


class ExportImageDialog(FramelessDialog):
    """Still-image export with format and composition options (§4.9.1.1)."""

    def __init__(self, metadata: VideoMetadata, frame_index: int, parent=None) -> None:
        super().__init__("Export Image", parent)
        self.setMinimumWidth(480)
        layout = self.body

        form = QFormLayout()
        form.setSpacing(8)
        self.format_combo = ChevronComboBox()
        for key, label in STILL_FORMATS.items():
            self.format_combo.addItem(label.split(" (")[0], key)
        self.format_combo.currentIndexChanged.connect(self._sync_extension)
        form.addRow("Format", self.format_combo)

        default = (
            metadata.path.parent
            / f"{metadata.path.stem}_frame_{frame_index + 1:05d}{EXTENSIONS['png']}"
        )
        row, self.path_edit, browse = _path_row("Choose output file")
        self.path_edit.setText(str(default))
        browse.clicked.connect(self._browse)
        form.addRow("Output", row)
        layout.addLayout(form)

        self.composition = _CompositionBox()
        layout.addWidget(self.composition)

        self.sidecar_check = QCheckBox("Write statistics sidecar (CSV)")
        layout.addWidget(self.sidecar_check)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = button_box.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText("Export")
        ok_button.setProperty("accent", True)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _fmt(self) -> str:
        return str(self.format_combo.currentData())

    def _sync_extension(self) -> None:
        self.path_edit.setText(
            str(Path(self.path_edit.text()).with_suffix(EXTENSIONS[self._fmt()]))
        )
        self.composition.setEnabled(self._fmt() not in {"tiff16", "tiff_float"})
        self.composition.setToolTip("Numeric TIFFs contain unflipped data without RGB composition")

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Choose output file", self.path_edit.text(), STILL_FORMATS[self._fmt()]
        )
        if path:
            self.path_edit.setText(path)

    def parameters(self) -> dict:
        dest = Path(self.path_edit.text()).expanduser()
        if dest.suffix.lower() != EXTENSIONS[self._fmt()]:
            dest = dest.with_suffix(EXTENSIONS[self._fmt()])
        return {
            "dest": str(dest),
            "fmt": self._fmt(),
            "options": self.composition.options(),
            "stats_sidecar": self.sidecar_check.isChecked(),
        }


class ExportSeriesDialog(FramelessDialog):
    """Export a numbered series of frames with a skip pattern (§4.9.1.1, p. 60)."""

    def __init__(
        self,
        metadata: VideoMetadata,
        parent=None,
        default_range: tuple[int, int] | None = None,
    ) -> None:
        super().__init__("Export Image Series", parent)
        self.setMinimumWidth(480)
        total = metadata.num_frames
        first, last = 1, total
        if default_range is not None:
            first = max(1, min(default_range[0] + 1, total))
            last = max(first, min(default_range[1] + 1, total))

        layout = self.body
        form = QFormLayout()
        form.setSpacing(8)

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
        self.decimation_spin.setToolTip("1 keeps every frame, N keeps every Nth frame")
        form.addRow("Keep every Nth", self.decimation_spin)

        self.format_combo = ChevronComboBox()
        for key, label in STILL_FORMATS.items():
            self.format_combo.addItem(label.split(" (")[0], key)
        form.addRow("Format", self.format_combo)

        row, self.folder_edit, browse = _path_row("Choose output folder")
        self.folder_edit.setText(str(metadata.path.parent))
        browse.clicked.connect(self._browse_folder)
        form.addRow("Folder", row)
        self.name_edit = QLineEdit(f"{metadata.path.stem}")
        form.addRow("Base name", self.name_edit)
        layout.addLayout(form)

        self.composition = _CompositionBox()
        layout.addWidget(self.composition)
        self.stats_check = QCheckBox("Write per-frame statistics (CSV)")
        self.format_combo.currentIndexChanged.connect(lambda: self.composition.setEnabled(
            str(self.format_combo.currentData()) not in {"tiff16", "tiff_float"}))
        self.composition.setToolTip("Numeric TIFFs contain unflipped data without RGB composition")
        layout.addWidget(self.stats_check)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText("Export")
        ok_button.setProperty("accent", True)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self.start_spin.valueChanged.connect(self._validate)
        self.end_spin.valueChanged.connect(self._validate)
        self._validate()

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose output folder", self.folder_edit.text()
        )
        if folder:
            self.folder_edit.setText(folder)

    def _validate(self) -> None:
        valid = self.start_spin.value() <= self.end_spin.value()
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(valid)

    def parameters(self) -> dict:
        base = self.name_edit.text().strip() or "frames"
        return {
            "folder": self.folder_edit.text().strip(),
            "base_name": base,
            "start_frame": self.start_spin.value() - 1,
            "end_frame": self.end_spin.value() - 1,
            "decimation": max(1, self.decimation_spin.value()),
            "fmt": str(self.format_combo.currentData()),
            "options": self.composition.options(),
            "stats_csv": self.stats_check.isChecked(),
        }


class ExportMovieDialog(FramelessDialog):
    """Movie export to MP4/WMV (§4.9.1.1, p. 59)."""

    def __init__(
        self,
        metadata: VideoMetadata,
        parent=None,
        default_range: tuple[int, int] | None = None,
    ) -> None:
        super().__init__("Export Movie", parent)
        self.setMinimumWidth(480)
        total = metadata.num_frames
        first, last = 1, total
        if default_range is not None:
            first = max(1, min(default_range[0] + 1, total))
            last = max(first, min(default_range[1] + 1, total))

        layout = self.body
        form = QFormLayout()
        form.setSpacing(8)

        self.format_combo = ChevronComboBox()
        for key, label in MOVIE_FORMATS.items():
            self.format_combo.addItem(label.split(" (")[0], key)
        self.format_combo.currentIndexChanged.connect(self._sync_extension)
        form.addRow("Format", self.format_combo)

        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setRange(1.0, 240.0)
        self.fps_spin.setDecimals(2)
        self.fps_spin.setValue(round(metadata.nominal_fps, 2) or 30.0)
        form.addRow("Frame rate", self.fps_spin)

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
        self.decimation_spin.setToolTip("1 keeps every frame, N keeps every Nth frame")
        form.addRow("Keep every Nth", self.decimation_spin)

        default = metadata.path.parent / f"{metadata.path.stem}.mp4"
        row, self.path_edit, browse = _path_row("Choose output file")
        self.path_edit.setText(str(default))
        browse.clicked.connect(self._browse)
        form.addRow("Output", row)
        layout.addLayout(form)

        self.composition = _CompositionBox()
        layout.addWidget(self.composition)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText("Export")
        ok_button.setProperty("accent", True)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self.start_spin.valueChanged.connect(self._validate)
        self.end_spin.valueChanged.connect(self._validate)
        self._validate()

    def _fmt(self) -> str:
        return str(self.format_combo.currentData())

    def _sync_extension(self) -> None:
        self.path_edit.setText(
            str(Path(self.path_edit.text()).with_suffix(f".{self._fmt()}"))
        )

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Choose output file", self.path_edit.text(), MOVIE_FORMATS[self._fmt()]
        )
        if path:
            self.path_edit.setText(path)

    def _validate(self) -> None:
        valid = self.start_spin.value() <= self.end_spin.value()
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(valid)

    def parameters(self) -> dict:
        dest = Path(self.path_edit.text()).expanduser()
        if dest.suffix.lower() != f".{self._fmt()}":
            dest = dest.with_suffix(f".{self._fmt()}")
        return {
            "dest": str(dest),
            "fmt": self._fmt(),
            "fps": float(self.fps_spin.value()),
            "start_frame": self.start_spin.value() - 1,
            "end_frame": self.end_spin.value() - 1,
            "decimation": max(1, self.decimation_spin.value()),
            "options": self.composition.options(),
        }


class BatchExtractDialog(FramelessDialog):
    """Trim a list of ATS recordings in one go (§4.9.1.3, p. 62)."""

    def __init__(self, parent=None) -> None:
        super().__init__("Batch Extract", parent)
        self.setMinimumWidth(520)

        layout = self.body

        files_label = QLabel("Recordings (ATS only — the File SDK extracts no other format)")
        files_label.setObjectName("FieldLabel")
        files_label.setWordWrap(True)
        layout.addWidget(files_label)

        self.file_list = QListWidget()
        layout.addWidget(self.file_list, 1)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(8)
        add_button = QPushButton("Add Files…")
        add_button.clicked.connect(self._add_files)
        self.remove_button = QPushButton("Remove Selected")
        self.remove_button.clicked.connect(self._remove_selected)
        buttons_row.addWidget(add_button)
        buttons_row.addWidget(self.remove_button)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)

        form = QFormLayout()
        form.setSpacing(8)
        row, self.folder_edit, browse = _path_row("Choose output folder")
        browse.clicked.connect(self._browse_folder)
        form.addRow("Output folder", row)
        self.decimation_spin = QSpinBox()
        self.decimation_spin.setRange(1, 100000)
        self.decimation_spin.setToolTip("1 keeps every frame, N keeps every Nth frame")
        form.addRow("Keep every Nth", self.decimation_spin)
        layout.addLayout(form)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText("Extract")
        ok_button.setProperty("accent", True)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)
        self._add_size_grip()
        self.file_list.model().rowsInserted.connect(self._validate)
        self.file_list.model().rowsRemoved.connect(self._validate)
        self._validate()

    def _add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add recordings",
            str(Path.cwd()),
            "ATS recordings (*.ats *.ATS);;All files (*.*)",
        )
        existing = self.files()
        for path in paths:
            if path not in existing:
                self.file_list.addItem(path)

    def _remove_selected(self) -> None:
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose output folder", self.folder_edit.text()
        )
        if folder:
            self.folder_edit.setText(folder)

    def _validate(self) -> None:
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self.file_list.count() > 0
        )

    def files(self) -> list[str]:
        return [self.file_list.item(i).text() for i in range(self.file_list.count())]

    def parameters(self) -> dict:
        return {
            "files": self.files(),
            "folder": self.folder_edit.text().strip(),
            "decimation": max(1, self.decimation_spin.value()),
        }
