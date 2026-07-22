# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import numpy as np
from PySide6.QtCore import QElapsedTimer, QEvent, QMimeData, QSettings, QTimer, Qt
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QVBoxLayout,
    QWidget,
)

from .decoder import DecoderThread
from .compose import compose_frame
from .export import (
    frame_burn_label,
    save_frame,
    stats_csv_header,
    stats_csv_row,
    write_stats_csv,
)
from .export_dialogs import (
    BatchExtractDialog,
    ExportImageDialog,
    ExportMovieDialog,
    ExportSeriesDialog,
)
from .extract import ExtractDialog
from .models import FramePacket, RoiShape, UnitOption, VideoMetadata
from .plots import TemporalPlotPanel, line_profile_values, roi_values
from .render import (
    CUSTOM_PALETTE_STOPS,
    DisplayState,
    display_scale,
    format_time,
    frame_seconds,
    load_custom_palettes,
    register_custom_palette,
    render_frame_rgb,
    unregister_custom_palette,
)
from .widgets import (
    ROI_COLORS,
    AnalysisToolbar,
    BottomPanel,
    ColorScaleWidget,
    InspectorPanel,
    PaletteEditorDialog,
    ReferenceDialog,
    ThermalCanvas,
    TitleBar,
    TransportBar,
)


SUPPORTED_EXTENSIONS = {
    ".seq",
    ".ats",
    ".sfmov",
    ".csq",
    ".fff",
    ".ptw",
    ".tif",
    ".tiff",
}

ROI_KIND_LABELS = {"rect": "Box", "ellipse": "Ellipse", "line": "Line", "cursor": "Spot"}

RECENT_FILES_LIMIT = 8


def _file_dialog_filter() -> str:
    patterns = " ".join(
        f"*{ext} *{ext.upper()}" for ext in sorted(SUPPORTED_EXTENSIONS)
    )
    return f"FLIR recordings ({patterns});;All files (*.*)"


class MainWindow(QMainWindow):
    """Application shell and playback controller."""

    def __init__(self, initial_path: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FLIR Thermal Player")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAcceptDrops(True)
        self.setMinimumSize(1080, 720)
        self.resize(1440, 1024)

        self.metadata: VideoMetadata | None = None
        self.current_packet: FramePacket | None = None
        self.current_palette = "Iron"
        self.range_mode = "dynamic"
        self.fixed_minimum = 0.0
        self.fixed_maximum = 1.0
        self.playback_speed = 1.0
        self.playing = False
        self._busy = False
        self._request_sequence = 0
        self._active_request_id = 0
        self._playback_waiting = False
        self._pending_playback_packet: FramePacket | None = None
        self._restart_after_seek = False
        self._focus_mode = False
        self._rois: list[RoiShape] = []
        self._roi_next_id = 1
        self._roi_name_counts: dict[str, int] = {}
        self._selected_roi_id: int | None = None
        self._extract_progress: QProgressDialog | None = None
        self._export_progress: QProgressDialog | None = None
        self.show_clipping = True
        self.show_markers = False
        self.flip_h = False
        self.flip_v = False
        self.enhancement = "linear"
        self.pe_strength = 0.5
        self.segmentation_on = False
        self.seg_min = 0.0
        self.seg_max = 1.0
        self.isotherm_mode = "off"
        self.iso_limit1 = 0.0
        self.iso_limit2 = 1.0
        self.palette_inverted = False
        self.loop_playback = bool(
            QSettings("Local", "FLIR Thermal Player").value(
                "playback/loop", False, type=bool
            )
        )
        self._play_range: tuple[int, int] | None = None
        self._wrap_pending = False
        self._filters_state: dict = {
            "point": ("none", 1.0),
            "spatial": ("none", 3),
            "temporal": ("none", 5),
        }
        self._reference_params: dict | None = None
        self._temporal: dict[int, list] = {}
        self._plot_clock = QElapsedTimer()
        self._last_plot_tab = None

        self._playback_timer = QTimer(self)
        self._playback_timer.setSingleShot(True)
        self._playback_timer.timeout.connect(self._present_pending_playback)
        self._playback_clock = QElapsedTimer()
        self._playback_anchor_timestamp: datetime | None = None
        self._playback_anchor_index = 0

        self._scrub_timer = QTimer(self)
        self._scrub_timer.setSingleShot(True)
        self._scrub_timer.setInterval(55)
        self._scrub_timer.timeout.connect(self._perform_scrub_preview)
        self._scrub_index = 0

        load_custom_palettes()
        self._build_ui()
        self._connect_ui()
        self._install_shortcuts()
        self.transport.set_loop(self.loop_playback)

        self.decoder = DecoderThread(self)
        self.decoder.opened.connect(self._on_opened)
        self.decoder.frame_ready.connect(self._on_frame_ready)
        self.decoder.unit_ready.connect(self._on_unit_ready)
        self.decoder.object_params_ready.connect(self._on_object_params_ready)
        self.decoder.corrections_ready.connect(self.inspector.set_corrections)
        self.decoder.reference_ready.connect(self._on_reference_ready)
        self.decoder.extract_progress.connect(self._on_extract_progress)
        self.decoder.extract_finished.connect(self._on_extract_finished)
        self.decoder.export_progress.connect(self._on_export_progress)
        self.decoder.export_finished.connect(self._on_export_finished)
        self.decoder.busy_changed.connect(self._set_busy)
        self.decoder.failed.connect(self._on_decode_failed)
        self.decoder.start()

        if initial_path:
            QTimer.singleShot(0, lambda: self.open_path(initial_path))

    def _build_ui(self) -> None:
        root = QFrame()
        root.setObjectName("Root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)

        self.title_bar = TitleBar()
        root_layout.addWidget(self.title_bar)

        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.stage = QWidget()
        self.stage.setObjectName("Stage")
        stage_layout = QHBoxLayout(self.stage)
        stage_layout.setContentsMargins(16, 16, 12, 16)
        stage_layout.setSpacing(12)
        self.analysis_toolbar = AnalysisToolbar()
        self.canvas = ThermalCanvas()
        self.color_scale = ColorScaleWidget()
        stage_layout.addWidget(self.analysis_toolbar)
        stage_layout.addWidget(self.canvas, 1)
        stage_layout.addWidget(self.color_scale)
        content_layout.addWidget(self.stage, 1)

        self.inspector = InspectorPanel()
        content_layout.addWidget(self.inspector)
        root_layout.addWidget(content, 1)

        self.bottom_panel = BottomPanel()
        self.bottom_panel.hide()
        root_layout.addWidget(self.bottom_panel)

        self.transport = TransportBar()
        root_layout.addWidget(self.transport)

        export_menu = QMenu(self)
        display_action = export_menu.addAction("Rendered frame (PNG)")
        image_action = export_menu.addAction("Export image…")
        array_action = export_menu.addAction("Raw values (NumPy)")
        csv_action = export_menu.addAction("Raw values (CSV)")
        bitmask_action = export_menu.addAction("ROI bitmasks…")
        export_menu.addSeparator()
        movie_action = export_menu.addAction("Export movie…")
        series_action = export_menu.addAction("Export image series…")
        export_menu.addSeparator()
        extract_action = export_menu.addAction("Extract clip (ATS)…")
        batch_action = export_menu.addAction("Batch extract…")
        display_action.triggered.connect(lambda: self._export("png"))
        image_action.triggered.connect(self._open_export_image_dialog)
        array_action.triggered.connect(lambda: self._export("npy"))
        csv_action.triggered.connect(lambda: self._export("csv"))
        bitmask_action.triggered.connect(self._export_bitmasks)
        movie_action.triggered.connect(self._open_export_movie_dialog)
        series_action.triggered.connect(self._open_export_series_dialog)
        extract_action.triggered.connect(self._open_extract_dialog)
        batch_action.triggered.connect(self._open_batch_extract_dialog)
        self.title_bar.set_export_menu(export_menu)

        self._recent_menu = QMenu(self)
        self.title_bar.set_open_menu(self._recent_menu)
        self._refresh_recent_menu()

    def _connect_ui(self) -> None:
        self.title_bar.open_requested.connect(self.open_dialog)
        self.title_bar.export_requested.connect(lambda: self._export("png"))
        self.inspector.unit_changed.connect(self._change_unit)
        self.inspector.palette_changed.connect(self._change_palette)
        self.inspector.palette_invert_toggled.connect(self._change_palette_invert)
        self.inspector.palette_edit_requested.connect(self._open_palette_editor)
        self.inspector.range_mode_changed.connect(self._change_range_mode)
        self.inspector.fixed_range_changed.connect(self._change_fixed_range)
        self.inspector.enhancement_changed.connect(self._change_enhancement)
        self.inspector.segmentation_changed.connect(self._change_segmentation)
        self.inspector.isotherm_changed.connect(self._change_isotherm)
        self.inspector.object_parameters_applied.connect(self._apply_object_parameters)
        self.inspector.object_parameters_reset.connect(self._reset_object_parameters)
        self.inspector.overlays_changed.connect(self._change_overlays)
        self.inspector.flips_changed.connect(self._change_flips)
        self.inspector.corrections_changed.connect(self._change_corrections)
        self.inspector.reference_requested.connect(self._open_reference_dialog)
        self.inspector.reference_cleared.connect(self._clear_reference)
        self.inspector.filters_changed.connect(self._change_filters)
        self.canvas.probe_changed.connect(self._update_probe)
        self.canvas.fullscreen_requested.connect(self.toggle_focus_mode)
        self.canvas.roi_drawn.connect(self._add_roi)
        self.canvas.roi_selected.connect(self._select_roi)
        self.canvas.roi_moved.connect(self._replace_roi)
        self.canvas.roi_delete_requested.connect(self._delete_roi)
        self.color_scale.isotherm_dragged.connect(self._isotherm_dragged)
        self.analysis_toolbar.tool_changed.connect(self.canvas.set_roi_tool)
        self.analysis_toolbar.delete_requested.connect(self._delete_selected_roi)
        self.analysis_toolbar.stats_toggled.connect(self._toggle_statistics)
        self.analysis_toolbar.zoom_in_requested.connect(lambda: self.canvas.zoom_step(1))
        self.analysis_toolbar.zoom_out_requested.connect(lambda: self.canvas.zoom_step(-1))
        self.analysis_toolbar.zoom_fit_requested.connect(self.canvas.set_zoom_fit)
        self.bottom_panel.close_requested.connect(lambda: self._toggle_statistics(False))
        self.bottom_panel.statistics.save_requested.connect(self._save_statistics)
        self.bottom_panel.tabs.currentChanged.connect(
            lambda _index: self._update_plots(self.current_packet)
        )
        self.bottom_panel.temporal.clear_button.clicked.connect(self._clear_temporal)
        self.bottom_panel.temporal.stat_combo.currentIndexChanged.connect(
            lambda _index: self._update_plots(self.current_packet, force=True)
        )
        self.transport.play_toggled.connect(self.toggle_playback)
        self.transport.seek_requested.connect(self.seek_to)
        self.transport.scrub_preview.connect(self._preview_scrub)
        self.transport.speed_changed.connect(self._change_speed)
        self.transport.loop_toggled.connect(self._change_loop)
        self.transport.slider.range_changed.connect(self._play_range_changed)
        self.transport.fullscreen_requested.connect(self.toggle_focus_mode)

    def _install_shortcuts(self) -> None:
        bindings = (
            (QKeySequence.StandardKey.Open, self.open_dialog),
            (QKeySequence("Ctrl+E"), lambda: self._export("png")),
            (QKeySequence("Space"), self.toggle_playback),
            (QKeySequence("Left"), lambda: self.step_frames(-1)),
            (QKeySequence("Right"), lambda: self.step_frames(1)),
            (QKeySequence("Shift+Left"), lambda: self.step_frames(-10)),
            (QKeySequence("Shift+Right"), lambda: self.step_frames(10)),
            (QKeySequence("Home"), lambda: self.seek_to(0)),
            (QKeySequence("End"), self._seek_to_end),
            (QKeySequence("F"), self.toggle_focus_mode),
            (QKeySequence("Escape"), self._escape_focus_mode),
            (QKeySequence("I"), self._mark_range_start),
            (QKeySequence("O"), self._mark_range_end),
            (QKeySequence("X"), self._clear_play_range),
            (QKeySequence("L"), self._toggle_loop_shortcut),
            (QKeySequence("+"), lambda: self.canvas.zoom_step(1)),
            (QKeySequence("="), lambda: self.canvas.zoom_step(1)),
            (QKeySequence("-"), lambda: self.canvas.zoom_step(-1)),
            (QKeySequence("0"), self.canvas.set_zoom_fit),
            (QKeySequence("1"), lambda: self.canvas.set_zoom_level(1.0)),
        )
        self._shortcuts: list[QShortcut] = []
        for sequence, callback in bindings:
            shortcut = QShortcut(sequence, self)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

    def open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open FLIR recording",
            str(Path.cwd()),
            _file_dialog_filter(),
        )
        if path:
            self.open_path(path)

    # --- recent files -----------------------------------------------------------

    def _recent_files(self) -> list[str]:
        value = QSettings("Local", "FLIR Thermal Player").value("files/recent", [])
        if isinstance(value, str):
            value = [value]
        return [str(path) for path in (value or [])]

    def _add_recent_file(self, path: Path) -> None:
        recents = self._recent_files()
        resolved = str(path)
        if resolved in recents:
            recents.remove(resolved)
        recents.insert(0, resolved)
        QSettings("Local", "FLIR Thermal Player").setValue(
            "files/recent", recents[:RECENT_FILES_LIMIT]
        )
        self._refresh_recent_menu()

    def _clear_recent_files(self) -> None:
        QSettings("Local", "FLIR Thermal Player").setValue("files/recent", [])
        self._refresh_recent_menu()

    def _refresh_recent_menu(self) -> None:
        self._recent_menu.clear()
        recents = self._recent_files()
        if not recents:
            empty = self._recent_menu.addAction("No recent recordings")
            empty.setEnabled(False)
            return
        for path in recents:
            action = self._recent_menu.addAction(Path(path).name)
            action.setToolTip(path)
            action.triggered.connect(
                lambda _checked=False, p=path: self.open_path(p)
            )
        self._recent_menu.addSeparator()
        clear = self._recent_menu.addAction("Clear Recent")
        clear.triggered.connect(self._clear_recent_files)

    def open_path(self, path: str | Path) -> None:
        resolved = Path(path).expanduser().resolve()
        if resolved.suffix.lower() not in SUPPORTED_EXTENSIONS:
            self._show_error(
                "Unsupported file",
                "Choose a FLIR SEQ, ATS, SFMOV, or CSQ recording.",
            )
            return
        if not resolved.is_file():
            self._show_error("File not found", str(resolved))
            return

        self.pause_playback(invalidate=True)
        self.metadata = None
        self.current_packet = None
        self._clear_rois()
        self._temporal.clear()
        self._toggle_statistics(False)
        self._play_range = None
        self.transport.slider.clear_play_range()
        self.title_bar.set_filename(resolved.name)
        self.transport.set_enabled(False)
        self.analysis_toolbar.set_enabled(False)
        self.inspector.unit_combo.clear()
        self.inspector.set_data_available(False)
        self.inspector.set_corrections({})  # hide correction toggles until state arrives
        self._filters_state = {"point": ("none", 1.0), "spatial": ("none", 3), "temporal": ("none", 5)}
        self._reference_params = None
        self.inspector.reset_processing()
        self.color_scale.clear()
        self.canvas.clear_frame("Opening recording…")
        self._set_busy(True, "Opening recording…")
        self.decoder.request_open(str(resolved))

    def toggle_playback(self) -> None:
        if self.current_packet is None or self.metadata is None or self._busy:
            return
        if self.playing:
            self.pause_playback(invalidate=True)
            return
        _start, end = self._playback_bounds()
        if self.current_packet.index >= end:
            self._restart_after_seek = True
            self.seek_to(self._playback_bounds()[0])
            return
        self._begin_playback()

    def _begin_playback(self) -> None:
        if self.current_packet is None:
            return
        self.playing = True
        self.transport.set_playing(True)
        self._playback_anchor_timestamp = self.current_packet.timestamp
        self._playback_anchor_index = self.current_packet.index
        self._playback_clock.start()
        self._request_next_playback_frame()

    def pause_playback(self, invalidate: bool = False) -> None:
        self.playing = False
        self.transport.set_playing(False)
        self._playback_timer.stop()
        self._playback_waiting = False
        self._pending_playback_packet = None
        self._wrap_pending = False
        if invalidate:
            self._active_request_id = self._next_request_id()

    # --- loop / play range ------------------------------------------------------

    def _change_loop(self, loop: bool) -> None:
        self.loop_playback = bool(loop)
        QSettings("Local", "FLIR Thermal Player").setValue(
            "playback/loop", self.loop_playback
        )

    def _toggle_loop_shortcut(self) -> None:
        self.transport.loop_button.toggle()

    def _play_range_changed(self, start: int, end: int) -> None:
        self._play_range = (min(start, end), max(start, end))

    def _playback_bounds(self) -> tuple[int, int]:
        last = max(0, (self.metadata.num_frames - 1) if self.metadata else 0)
        if self._play_range is not None:
            start, end = self._play_range
            return (max(0, min(start, last)), max(0, min(end, last)))
        return (0, last)

    def _mark_range_start(self) -> None:
        if self.current_packet is None or self.metadata is None:
            return
        _start, end = self._playback_bounds()
        self.transport.slider.set_play_range(self.current_packet.index, end)

    def _mark_range_end(self) -> None:
        if self.current_packet is None or self.metadata is None:
            return
        start, _end = self._playback_bounds()
        self.transport.slider.set_play_range(start, self.current_packet.index)

    def _clear_play_range(self) -> None:
        self._play_range = None
        self.transport.slider.clear_play_range()

    def _wrap_playback(self, start: int) -> None:
        """Loop: jump back to the range start and keep playing."""
        self._wrap_pending = True
        request_id = self._activate_request()
        self._playback_waiting = True
        self.decoder.request_frame(start, request_id)

    def step_frames(self, delta: int) -> None:
        if self.current_packet is None:
            return
        self.seek_to(self.current_packet.index + int(delta))

    def seek_to(self, index: int) -> None:
        if self.metadata is None:
            return
        restart = self._restart_after_seek
        self.pause_playback(invalidate=True)
        self._restart_after_seek = restart
        bounded = max(0, min(int(index), self.metadata.num_frames - 1))
        request_id = self._activate_request()
        self.decoder.request_frame(bounded, request_id)

    def _seek_to_end(self) -> None:
        if self.metadata is not None:
            self.seek_to(self.metadata.num_frames - 1)

    def _preview_scrub(self, index: int) -> None:
        if self.metadata is None:
            return
        self.pause_playback(invalidate=True)
        self._scrub_index = int(index)
        self.transport.current_time.setText(
            format_time(self.metadata.fallback_seconds_for_frame(self._scrub_index))
        )
        self._scrub_timer.start()

    def _perform_scrub_preview(self) -> None:
        self.seek_to(self._scrub_index)

    def _change_unit(self, key: str) -> None:
        if self.current_packet is None or self._busy:
            return
        self.pause_playback(invalidate=True)
        request_id = self._activate_request()
        self.decoder.request_unit(key, self.current_packet.index, request_id)
        self._reload_reference(request_id)

    def _apply_object_parameters(self, values: dict | None) -> None:
        if self.current_packet is None or self._busy:
            return
        self.pause_playback(invalidate=True)
        request_id = self._activate_request()
        self.decoder.request_object_params(values, self.current_packet.index, request_id)
        self._reload_reference(request_id)

    def _reset_object_parameters(self) -> None:
        self._apply_object_parameters(None)

    def _on_object_params_ready(self, snapshot: dict) -> None:
        self.inspector.set_object_parameters(snapshot)

    # --- ROI management --------------------------------------------------------

    def _add_roi(self, kind: str, points: tuple) -> None:
        if self.current_packet is None or kind not in ROI_KIND_LABELS:
            return
        self._roi_name_counts[kind] = self._roi_name_counts.get(kind, 0) + 1
        name = f"{ROI_KIND_LABELS[kind]} {self._roi_name_counts[kind]}"
        shape = RoiShape(
            id=self._roi_next_id,
            kind=kind,
            points=tuple((float(x), float(y)) for x, y in points),
            name=name,
        )
        self._roi_next_id += 1
        self._rois.append(shape)
        self._selected_roi_id = shape.id
        if not self.bottom_panel.isVisible():
            self._toggle_statistics(True)
        self._push_rois()

    def _select_roi(self, roi_id: int | None) -> None:
        self._selected_roi_id = roi_id
        self.canvas.set_rois(self._rois, roi_id)
        if self.range_mode == "roi":
            self._render_current_frame()

    def _replace_roi(self, shape: RoiShape) -> None:
        for index, existing in enumerate(self._rois):
            if existing.id == shape.id:
                self._rois[index] = shape
                break
        self._push_rois()

    def _delete_roi(self, roi_id: int | None) -> None:
        if roi_id is None:
            return
        self._rois = [shape for shape in self._rois if shape.id != roi_id]
        if self._selected_roi_id == roi_id:
            self._selected_roi_id = None
        self._push_rois()

    def _delete_selected_roi(self) -> None:
        self._delete_roi(self._selected_roi_id)

    def _push_rois(self) -> None:
        self.canvas.set_rois(self._rois, self._selected_roi_id)
        if self.current_packet is None:
            return
        if self.playing:
            request_id = self._active_request_id
        else:
            request_id = self._activate_request()
        self.decoder.request_rois(tuple(self._rois), self.current_packet.index, request_id)

    def _clear_rois(self) -> None:
        self._rois = []
        self._roi_next_id = 1
        self._roi_name_counts = {}
        self._selected_roi_id = None
        self.canvas.set_rois([], None)

    # --- statistics panel ------------------------------------------------------

    def _toggle_statistics(self, show: bool) -> None:
        self.bottom_panel.setVisible(show)
        self.analysis_toolbar.stats_button.blockSignals(True)
        self.analysis_toolbar.stats_button.setChecked(show)
        self.analysis_toolbar.stats_button.blockSignals(False)
        if show:
            self._update_statistics()

    def _update_statistics(self) -> None:
        packet = self.current_packet
        if packet is None or not self.bottom_panel.isVisible():
            return
        image_stats = (
            packet.minimum,
            packet.maximum,
            packet.mean,
            packet.std_dev,
            packet.num_pixels,
        )
        self.bottom_panel.statistics.set_statistics(
            packet.roi_stats, image_stats, packet.unit.suffix
        )
        self.bottom_panel.metadata.set_entries(packet.metadata_entries)
        self._update_plots(packet)

    # --- analysis plots --------------------------------------------------------

    def _update_plots(
        self, packet: FramePacket | None, force: bool = False, accumulate: bool = True
    ) -> None:
        if packet is None or self.metadata is None:
            return
        panels = self.bottom_panel
        suffix = packet.unit.suffix
        if accumulate:
            seconds = frame_seconds(packet.timestamp, self.metadata, packet.index)
            for stats in packet.roi_stats:
                self._temporal.setdefault(stats.id, []).append((seconds, stats))
            live_ids = {stats.id for stats in packet.roi_stats}
            for stale in [roi_id for roi_id in self._temporal if roi_id not in live_ids]:
                del self._temporal[stale]

        current = panels.tabs.currentWidget()
        due = not self._plot_clock.isValid() or self._plot_clock.elapsed() >= 250
        tab_changed = current is not self._last_plot_tab
        self._last_plot_tab = current
        if not (force or due or tab_changed):
            return
        self._plot_clock.restart()

        selected = next(
            (shape for shape in self._rois if shape.id == self._selected_roi_id), None
        )
        if current is panels.profile:
            if selected is not None and selected.kind == "line":
                distances, values = line_profile_values(
                    packet.data, selected.points[0], selected.points[1]
                )
                panels.profile.set_profile(distances, values, selected.name, suffix)
            else:
                panels.profile.show_message("Select a line ROI to see its profile")
        elif current is panels.histogram:
            if selected is not None:
                panels.histogram.set_values(
                    roi_values(packet.data, selected), selected.name, suffix
                )
            else:
                panels.histogram.set_values(packet.data, "Whole image", suffix)
        elif current is panels.temporal:
            stat_label = panels.temporal.statistic_label
            attr = dict(TemporalPlotPanel.STATISTICS)[stat_label]
            series = []
            for shape in self._rois:
                points = self._temporal.get(shape.id)
                if not points:
                    continue
                series.append(
                    (
                        shape.name,
                        ROI_COLORS[shape.id % len(ROI_COLORS)],
                        np.array([point[0] for point in points]),
                        np.array([float(getattr(point[1], attr)) for point in points]),
                    )
                )
            panels.temporal.set_series(series, stat_label, suffix)

    def _clear_temporal(self) -> None:
        self._temporal.clear()
        self._update_plots(self.current_packet, force=True, accumulate=False)

    # --- extract clip -----------------------------------------------------------

    def _open_extract_dialog(self) -> None:
        if self.metadata is None or self._busy:
            return
        self.pause_playback(invalidate=True)
        dialog = ExtractDialog(self.metadata, self, default_range=self._play_range)
        if dialog.exec() == ExtractDialog.DialogCode.Accepted:
            self._start_extract(dialog.parameters())

    def _start_extract(self, params: dict) -> None:
        self._extract_progress = QProgressDialog(
            "Extracting clip…", "Cancel", 0, 1, self
        )
        self._extract_progress.setWindowTitle("Extract Clip")
        self._extract_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._extract_progress.setMinimumDuration(0)
        self._extract_progress.setValue(0)
        self._extract_progress.canceled.connect(self.decoder.cancel_extract)
        self.decoder.request_extract(params)

    def _on_extract_progress(self, current: int, total: int) -> None:
        dialog = getattr(self, "_extract_progress", None)
        if dialog is None:
            return
        if dialog.maximum() != total:
            dialog.setMaximum(max(1, total))
        dialog.setValue(current)

    def _on_extract_finished(self, ok: bool, message: str) -> None:
        dialog = getattr(self, "_extract_progress", None)
        if dialog is not None:
            dialog.reset()
            dialog.deleteLater()
            self._extract_progress = None
        if ok:
            self.title_bar.export_button.setToolTip("Clip extracted")
        elif message and message != "Extraction cancelled":
            self._show_error("Extract failed", message)

    # --- export: still image / bitmasks / movie / series / batch -------------------

    def _open_export_image_dialog(self) -> None:
        if self.current_packet is None or self.metadata is None:
            return
        dialog = ExportImageDialog(self.metadata, self.current_packet.index, self)
        if dialog.exec() != ExportImageDialog.DialogCode.Accepted:
            return
        self._export_still(dialog.parameters())

    def _export_still(self, params: dict) -> None:
        packet = self.current_packet
        state = self._display_state(packet)
        rgb, low, high = render_frame_rgb(packet.data, state, packet.clip_mask)
        options = params["options"]
        label = frame_burn_label(packet, self.metadata) if options.timestamp else ""
        composed = compose_frame(
            rgb,
            options,
            palette=state.palette,
            inverted=state.inverted,
            scale=(low, high),
            suffix=packet.unit.suffix,
            rois=tuple(self._rois),
            roi_stats=packet.roi_stats,
            min_position=packet.min_position,
            max_position=packet.max_position,
            label=label,
            flips=(state.flip_h, state.flip_v),
        )
        dest = Path(params["dest"])
        try:
            save_frame(dest, params["fmt"], composed, packet.data, (low, high))
            if params["stats_sidecar"]:
                roi_names = [shape.name for shape in self._rois]
                write_stats_csv(
                    dest.with_suffix(".csv"),
                    stats_csv_header(roi_names),
                    [stats_csv_row(packet, packet.unit.label)],
                )
            self.title_bar.export_button.setToolTip(f"Saved {dest.name}")
        except Exception as exc:
            self._show_error("Export failed", f"{type(exc).__name__}: {exc}")

    def _export_bitmasks(self) -> None:
        if self.current_packet is None or self._busy:
            return
        if not self._rois:
            self.title_bar.export_button.setToolTip("Draw an ROI first")
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Choose bitmask output folder", str(self.metadata.path.parent)
        )
        if folder:
            self.decoder.request_export_bitmasks(folder)

    def _open_export_movie_dialog(self) -> None:
        if self.metadata is None or self._busy or self.current_packet is None:
            return
        self.pause_playback(invalidate=True)
        dialog = ExportMovieDialog(self.metadata, self, default_range=self._play_range)
        if dialog.exec() == ExportMovieDialog.DialogCode.Accepted:
            params = dialog.parameters()
            params["kind"] = "movie"
            self._start_export(params, "Export Movie")

    def _open_export_series_dialog(self) -> None:
        if self.metadata is None or self._busy or self.current_packet is None:
            return
        self.pause_playback(invalidate=True)
        dialog = ExportSeriesDialog(self.metadata, self, default_range=self._play_range)
        if dialog.exec() == ExportSeriesDialog.DialogCode.Accepted:
            params = dialog.parameters()
            params["kind"] = "series"
            params["dest"] = params.pop("folder")
            self._start_export(params, "Export Series")

    def _open_batch_extract_dialog(self) -> None:
        if self._busy:
            return
        self.pause_playback(invalidate=True)
        dialog = BatchExtractDialog(self)
        if dialog.exec() != BatchExtractDialog.DialogCode.Accepted:
            return
        params = dialog.parameters()
        if not params["folder"]:
            self._show_error("Batch extract", "Choose an output folder.")
            return
        self._start_export(params, "Batch Extract")

    def _export_payload(self, params: dict) -> dict:
        """Attach the current display/ROI state to a movie/series request."""
        packet = self.current_packet
        params["display"] = self._display_state(packet)
        params["selected_roi_id"] = self._selected_roi_id
        params["rois"] = tuple(self._rois)
        params["suffix"] = packet.unit.suffix
        params["unit_label"] = packet.unit.label
        return params

    def _start_export(self, params: dict, title: str) -> None:
        if params.get("kind") in {"movie", "series"}:
            params = self._export_payload(params)
        self._export_progress = QProgressDialog(
            f"{title}…", "Cancel", 0, 1, self
        )
        self._export_progress.setWindowTitle(title)
        self._export_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._export_progress.setMinimumDuration(0)
        self._export_progress.setValue(0)
        self._export_progress.canceled.connect(self.decoder.cancel_extract)
        if params.get("kind") in {"movie", "series"}:
            self.decoder.request_export_sequence(params)
        else:
            self.decoder.request_batch_extract(params)

    def _on_export_progress(self, current: int, total: int) -> None:
        dialog = getattr(self, "_export_progress", None)
        if dialog is None:
            return
        if dialog.maximum() != total:
            dialog.setMaximum(max(1, total))
        dialog.setValue(current)

    def _on_export_finished(self, ok: bool, message: str) -> None:
        dialog = getattr(self, "_export_progress", None)
        if dialog is not None:
            dialog.reset()
            dialog.deleteLater()
            self._export_progress = None
        if ok:
            self.title_bar.export_button.setToolTip(message or "Export finished")
        elif message and message != "Export cancelled":
            self._show_error("Export failed", message)

    def _save_statistics(self) -> None:
        packet = self.current_packet
        if packet is None or self.metadata is None:
            return
        default_name = (
            self.metadata.path.parent
            / f"{self.metadata.path.stem}_frame_{packet.index + 1:05d}_stats.csv"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save statistics",
            str(default_name),
            "CSV table (*.csv)",
        )
        if not path:
            return
        output = Path(path)
        if output.suffix.lower() != ".csv":
            output = output.with_suffix(".csv")
        try:
            with output.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["Source", self.metadata.filename])
                writer.writerow(["Frame", f"{packet.index + 1} / {self.metadata.num_frames}"])
                writer.writerow(["Unit", packet.unit.label])
                writer.writerow([])
                writer.writerows(self.bottom_panel.statistics.to_rows())
            self.title_bar.export_button.setToolTip(f"Saved {output.name}")
        except Exception as exc:
            self._show_error("Statistics export failed", f"{type(exc).__name__}: {exc}")

    def _change_palette(self, palette: str) -> None:
        if not palette:
            return
        self.current_palette = palette
        self._render_current_frame()

    def _change_palette_invert(self, inverted: bool) -> None:
        self.palette_inverted = bool(inverted)
        self._render_current_frame()

    def _open_palette_editor(self) -> None:
        current = self.current_palette
        existing = current in CUSTOM_PALETTE_STOPS
        if existing:
            dialog = PaletteEditorDialog(
                self, name=current, stops=CUSTOM_PALETTE_STOPS[current], existing=True
            )
        else:
            number = 1
            while f"Custom {number}" in CUSTOM_PALETTE_STOPS:
                number += 1
            dialog = PaletteEditorDialog(self, name=f"Custom {number}")
        if dialog.exec() != PaletteEditorDialog.DialogCode.Accepted:
            return
        if dialog.deleted:
            unregister_custom_palette(current)
            self.inspector.remove_palette(current)
            return
        name = dialog.palette_name()
        if not name:
            self._show_error("Invalid palette", "The palette needs a name.")
            return
        try:
            register_custom_palette(name, dialog.palette_stops())
        except ValueError as exc:
            self._show_error("Invalid palette", str(exc))
            return
        if existing and name != current:
            self.inspector.remove_palette(current)
        self.inspector.add_palette(name, select=True)

    def _change_range_mode(self, mode: str) -> None:
        if mode not in {"dynamic", "fixed", "roi"}:
            return
        self.range_mode = mode
        self.inspector.set_range_mode(mode)
        if mode == "fixed" and self.current_packet is not None:
            if self.fixed_maximum <= self.fixed_minimum:
                self.fixed_minimum = self.current_packet.minimum
                self.fixed_maximum = self.current_packet.maximum
                self.inspector.set_fixed_values(self.fixed_minimum, self.fixed_maximum)
        self._render_current_frame()

    def _display_state(self, packet: FramePacket) -> DisplayState:
        roi_minmax = None
        if self.range_mode == "roi":
            stats = next(
                (s for s in packet.roi_stats if s.id == self._selected_roi_id), None
            )
            if stats is not None:
                roi_minmax = (stats.minimum, stats.maximum)
        return DisplayState(
            palette=self.current_palette,
            inverted=self.palette_inverted,
            pe=self.pe_strength if self.enhancement == "pe" else 0.0,
            range_mode=self.range_mode,
            fixed_min=self.fixed_minimum,
            fixed_max=self.fixed_maximum,
            roi_minmax=roi_minmax,
            segmentation=(self.segmentation_on, self.seg_min, self.seg_max),
            isotherm=(self.isotherm_mode, self.iso_limit1, self.iso_limit2),
            clipping=self.show_clipping,
            flip_h=self.flip_h,
            flip_v=self.flip_v,
        )

    def _current_scale(self, packet: FramePacket) -> tuple[float, float]:
        return display_scale(packet.data, self._display_state(packet))

    def _change_overlays(self, clipping: bool, markers: bool) -> None:
        self.show_clipping = clipping
        self.show_markers = markers
        self.canvas.set_overlay_options(markers)
        self._render_current_frame()

    def _change_flips(self, flip_h: bool, flip_v: bool) -> None:
        self.flip_h = flip_h
        self.flip_v = flip_v
        self.canvas.set_flips(flip_h, flip_v)
        self._render_current_frame()

    def _change_corrections(self, nuc: bool, bp: bool) -> None:
        if self.current_packet is None or self._busy:
            return
        self.pause_playback(invalidate=True)
        request_id = self._activate_request()
        self.decoder.request_corrections(nuc, bp, self.current_packet.index, request_id)

    # --- processing pipeline (reference file operation + filters) -----------------

    def _open_reference_dialog(self) -> None:
        if self.metadata is None or self._busy or self.current_packet is None:
            return
        self.pause_playback(invalidate=True)
        dialog = ReferenceDialog(self.metadata, self)
        if dialog.exec() != ReferenceDialog.DialogCode.Accepted:
            return
        self._reference_params = dialog.parameters()
        request_id = self._activate_request()
        self.decoder.request_reference(
            self._reference_params, self.current_packet.index, request_id
        )

    def _clear_reference(self) -> None:
        if self.current_packet is None or self._busy:
            return
        self.pause_playback(invalidate=True)
        self._reference_params = None
        request_id = self._activate_request()
        self.decoder.request_reference(None, self.current_packet.index, request_id)

    def _reload_reference(self, request_id: int) -> None:
        """Re-read the reference after a unit / object-parameter change."""
        if self._reference_params is None or self.current_packet is None:
            return
        self.decoder.request_reference(
            self._reference_params, self.current_packet.index, request_id
        )

    def _change_filters(self, state: dict) -> None:
        self._filters_state = {
            "point": tuple(state.get("point", ("none", 1.0))),
            "spatial": tuple(state.get("spatial", ("none", 3))),
            "temporal": tuple(state.get("temporal", ("none", 5))),
        }
        if self.current_packet is None or self._busy:
            return
        self.pause_playback(invalidate=True)
        request_id = self._activate_request()
        self.decoder.request_filters(
            self._filters_state, self.current_packet.index, request_id
        )

    def _on_reference_ready(self, label: str) -> None:
        self.inspector.set_reference_label(label)

    def _change_fixed_range(self, minimum: float, maximum: float) -> None:
        if maximum <= minimum:
            self.inspector.set_fixed_values(self.fixed_minimum, self.fixed_maximum)
            return
        self.fixed_minimum = float(minimum)
        self.fixed_maximum = float(maximum)
        self._render_current_frame()

    def _change_enhancement(self, mode: str, strength: float) -> None:
        self.enhancement = mode if mode in {"linear", "pe"} else "linear"
        self.pe_strength = min(1.0, max(0.0, float(strength)))
        self._render_current_frame()

    def _change_segmentation(self, enabled: bool, minimum: float, maximum: float) -> None:
        self.segmentation_on = bool(enabled)
        self.seg_min = float(minimum)
        self.seg_max = float(maximum)
        if enabled and maximum <= minimum and self.current_packet is not None:
            self.seg_min = self.current_packet.minimum
            self.seg_max = self.current_packet.maximum
            self.inspector.set_segmentation_values(self.seg_min, self.seg_max)
        self._render_current_frame()

    def _change_isotherm(self, mode: str, limit1: float, limit2: float) -> None:
        was_off = self.isotherm_mode == "off"
        self.isotherm_mode = mode if mode in {"off", "above", "below", "interval"} else "off"
        self.iso_limit1 = float(limit1)
        self.iso_limit2 = float(limit2)
        if self.isotherm_mode != "off" and was_off and self.current_packet is not None:
            low, high = self._current_scale(self.current_packet)
            self.iso_limit1 = (low + high) / 2.0
            self.iso_limit2 = high
            self.inspector.set_isotherm_values(self.iso_limit1, self.iso_limit2)
        self._render_current_frame()

    def _isotherm_dragged(self, which: str, value: float) -> None:
        if which == "l2":
            self.iso_limit2 = float(value)
        else:
            self.iso_limit1 = float(value)
        self.inspector.set_isotherm_values(self.iso_limit1, self.iso_limit2)
        self._render_current_frame()

    def _change_speed(self, speed: float) -> None:
        was_playing = self.playing
        if was_playing:
            self.pause_playback(invalidate=True)
        self.playback_speed = max(0.1, float(speed))
        if was_playing:
            self._begin_playback()

    def _request_next_playback_frame(self) -> None:
        if not self.playing or self.metadata is None or self.current_packet is None:
            return
        next_index = self.current_packet.index + 1
        start, end = self._playback_bounds()
        if next_index > end:
            if self.loop_playback:
                self._wrap_playback(start)
            else:
                self.pause_playback(invalidate=False)
            return
        request_id = self._activate_request()
        self._playback_waiting = True
        self.decoder.request_frame(next_index, request_id)

    def _present_pending_playback(self) -> None:
        packet = self._pending_playback_packet
        self._pending_playback_packet = None
        if not self.playing or packet is None:
            return
        self._present_packet(packet)
        self._request_next_playback_frame()

    def _on_opened(
        self,
        metadata: VideoMetadata,
        options: tuple[UnitOption, ...],
        packet: FramePacket,
    ) -> None:
        self.metadata = metadata
        self._add_recent_file(metadata.path)
        self._active_request_id = packet.request_id
        self.title_bar.set_filename(metadata.filename)
        self.title_bar.export_button.setEnabled(True)
        self.inspector.set_available_units(options, packet.unit.key)
        self.inspector.set_data_available(True)
        self.analysis_toolbar.set_enabled(True)
        self.bottom_panel.source.set_details(metadata.source_details)
        self.transport.set_video(metadata.num_frames, metadata.duration_seconds)
        self.fixed_minimum = packet.minimum
        self.fixed_maximum = packet.maximum
        self.inspector.set_fixed_values(self.fixed_minimum, self.fixed_maximum)
        self._present_packet(packet)

    def _on_frame_ready(self, packet: FramePacket) -> None:
        if packet.request_id != self._active_request_id:
            return
        if self.playing and self._playback_waiting and self.current_packet is not None:
            self._playback_waiting = False
            wrapped = self._wrap_pending
            if wrapped:
                self._wrap_pending = False
                self._playback_anchor_timestamp = packet.timestamp
                self._playback_anchor_index = packet.index
                self._playback_clock.restart()
            target_seconds = self._playback_target_seconds(packet) / self.playback_speed
            wall_seconds = self._playback_clock.elapsed() / 1000.0
            delay = max(0.0, target_seconds - wall_seconds)
            if wrapped and self.metadata is not None:
                # keep at least one nominal frame interval between loop wraps so a
                # degenerate (single-frame) range cannot spin a tight request loop
                interval = 1.0 / max(1.0, self.metadata.nominal_fps)
                delay = max(delay, interval / self.playback_speed)
            self._pending_playback_packet = packet
            self._playback_timer.start(max(0, int(round(delay * 1000.0))))
            return
        self._present_packet(packet)
        if self._restart_after_seek:
            self._restart_after_seek = False
            QTimer.singleShot(0, self.toggle_playback)

    def _on_unit_ready(self, option: UnitOption, packet: FramePacket) -> None:
        if packet.request_id != self._active_request_id:
            return
        self.current_packet = packet
        self.fixed_minimum = packet.minimum
        self.fixed_maximum = packet.maximum
        self.inspector.set_fixed_values(self.fixed_minimum, self.fixed_maximum)
        self._present_packet(packet)

    def _present_packet(self, packet: FramePacket) -> None:
        self.current_packet = packet
        self._render_current_frame()

    def _render_current_frame(self) -> None:
        if self.current_packet is None or self.metadata is None:
            return
        packet = self.current_packet
        rgb, low, high = render_frame_rgb(
            packet.data, self._display_state(packet), packet.clip_mask
        )
        self.canvas.set_frame(rgb, packet.data, packet.unit.suffix)
        self.canvas.set_overlay_data(
            packet.min_position, packet.max_position, packet.roi_stats
        )
        self.color_scale.set_scale(
            self.current_palette,
            low,
            high,
            invert=self.palette_inverted,
            unit=packet.unit.suffix or packet.unit.label,
        )
        self.color_scale.set_isotherm(self.isotherm_mode, self.iso_limit1, self.iso_limit2)
        seconds = frame_seconds(packet.timestamp, self.metadata, packet.index)
        self.transport.set_frame(packet.index, self.metadata.num_frames, seconds)
        self.inspector.set_frame_info(
            self.metadata,
            packet.index,
            packet.unit,
            self.range_mode,
        )
        self._update_statistics()

    def _frame_interval(self, current: FramePacket, following: FramePacket) -> float:
        if current.timestamp is not None and following.timestamp is not None:
            try:
                delta = (following.timestamp - current.timestamp).total_seconds()
                if 0.001 <= delta <= 1.0:
                    return float(delta)
            except (OverflowError, TypeError, ValueError):
                pass
        if self.metadata is not None and self.metadata.nominal_fps > 0:
            return 1.0 / self.metadata.nominal_fps
        return 1.0 / 30.0

    def _playback_target_seconds(self, packet: FramePacket) -> float:
        if self._playback_anchor_timestamp is not None and packet.timestamp is not None:
            try:
                delta = (packet.timestamp - self._playback_anchor_timestamp).total_seconds()
                if delta >= 0:
                    return float(delta)
            except (OverflowError, TypeError, ValueError):
                pass
        if self.metadata is not None and self.metadata.nominal_fps > 0:
            return max(0, packet.index - self._playback_anchor_index) / self.metadata.nominal_fps
        return max(0, packet.index - self._playback_anchor_index) / 30.0

    def _update_probe(self, probe) -> None:
        suffix = self.current_packet.unit.suffix if self.current_packet is not None else ""
        self.inspector.set_probe(probe, suffix)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        if busy == self._busy:
            if busy and message and self.current_packet is None:
                self.canvas.set_message(message)
            return
        self._busy = busy
        self.inspector.set_busy(busy)
        self.title_bar.open_button.setEnabled(not busy)
        self.title_bar.export_button.setEnabled(not busy)
        self.transport.set_enabled(not busy and self.metadata is not None)
        if busy:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            if self.current_packet is None and message:
                self.canvas.set_message(message)
        else:
            QApplication.restoreOverrideCursor()

    def _on_decode_failed(self, message: str) -> None:
        self.pause_playback(invalidate=True)
        if self.current_packet is None:
            self.canvas.clear_frame("Unable to open this recording")
        self._show_error("FLIR decoding error", message)

    def _export(self, kind: str) -> None:
        if self.current_packet is None or self.metadata is None:
            return
        suffix_map = {"png": ".png", "npy": ".npy", "csv": ".csv"}
        filter_map = {
            "png": "PNG image (*.png)",
            "npy": "NumPy array (*.npy)",
            "csv": "CSV table (*.csv)",
        }
        extension = suffix_map[kind]
        default_name = (
            self.metadata.path.parent
            / f"{self.metadata.path.stem}_frame_{self.current_packet.index + 1:05d}{extension}"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export current frame",
            str(default_name),
            filter_map[kind],
        )
        if not path:
            return
        output = Path(path)
        if output.suffix.lower() != extension:
            output = output.with_suffix(extension)
        try:
            if kind == "png":
                if self.canvas.image.isNull() or not self.canvas.image.save(str(output), "PNG"):
                    raise OSError("Qt could not write the PNG image")
            elif kind == "npy":
                np.save(output, self.current_packet.data)
            else:
                np.savetxt(output, self.current_packet.data, delimiter=",", fmt="%.6f")
            self.title_bar.export_button.setToolTip(f"Saved {output.name}")
        except Exception as exc:
            self._show_error("Export failed", f"{type(exc).__name__}: {exc}")

    def toggle_focus_mode(self) -> None:
        self._focus_mode = not self._focus_mode
        if self._focus_mode:
            self.title_bar.hide()
            self.inspector.hide()
            self.transport.hide()
            self.showFullScreen()
        else:
            self.showNormal()
            self.title_bar.show()
            self.inspector.show()
            self.transport.show()

    def _escape_focus_mode(self) -> None:
        if self._focus_mode:
            self.toggle_focus_mode()

    def _activate_request(self) -> int:
        request_id = self._next_request_id()
        self._active_request_id = request_id
        return request_id

    def _next_request_id(self) -> int:
        self._request_sequence += 1
        return self._request_sequence

    @staticmethod
    def _show_error(title: str, message: str) -> None:
        QMessageBox.critical(None, title, message)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        urls = event.mimeData().urls()
        if any(Path(url.toLocalFile()).suffix.lower() in SUPPORTED_EXTENSIONS for url in urls):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                self.open_path(path)
                event.acceptProposedAction()
                return

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self.title_bar.update_maximize_icon()
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.pause_playback(invalidate=True)
        self.decoder.shutdown()
        if not self.decoder.wait(4000):
            self.decoder.requestInterruption()
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        event.accept()
