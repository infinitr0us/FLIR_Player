# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import csv
import os
import sys
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PySide6.QtCore import QElapsedTimer, QEvent, QMimeData, QTimer, Qt
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from .decoder import DecoderThread
from .analysis import value_transition
from .jobs import OutputTransaction, unique_destination
from .compose import compose_frame
from .export import (
    bitmask_filename,
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
    confirm_replace,
)
from .excel_dialog import ExcelExportDialog
from .excel_export import RoiSet, ZoneGroup, load_roi_set, roi_set_path, save_roi_set
from .extract import ExtractDialog
from .tc_dialog import TcCalibrationDialog, TcResultsDialog
from .tcmatch import roi_shapes, workbook_origin
from .zone_dialog import ZoneSplitDialog
from .zones import ZoneSpec, normalized_box
from .geometry import roi_coordinates
from .settings import app_settings
from .models import (
    ROI_KIND_LABELS,
    FramePacket,
    RoiShape,
    StatisticsSnapshot,
    UnitOption,
    VideoMetadata,
)
from .plots import line_profile_values, roi_values
from .processing import median_max_size
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
    EdgeResizeGrip,
    InspectorPanel,
    MessageDialog,
    PaletteEditorDialog,
    ProgressDialog,
    ReferenceDialog,
    ThermalCanvas,
    TitleBar,
    TransportBar,
    saved_button_text,
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

_CLOSING_WINDOWS = set()  # Retain Python owners until their SDK threads finish.

RECENT_FILES_LIMIT = 8

# Bounded temporal history per ROI (~11 minutes at 30 fps): compact
# (seconds, mean, min, max, std) tuples; oldest half dropped when exceeded.
TEMPORAL_POINT_CAP = 20000
_TEMPORAL_STAT_INDEX = {"Mean": 1, "Min": 2, "Max": 3, "Std Dev": 4}


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
        self._open_request_id = 0  # generation of the latest open; older ones are stale
        self._state_request_id = 0  # latest measurement/processing change sent
        self._playback_outstanding = False  # a playback frame request is in flight
        self._ready: deque[FramePacket] = deque()  # due-order presentation queue
        self._latest_decoded: FramePacket | None = None  # newest playback arrival
        self._restart_after_seek = False
        # playback counters, printed ~1/s to stderr when FLIR_PERF_DEBUG is set
        self._perf_debug = bool(os.environ.get("FLIR_PERF_DEBUG"))
        self._perf = {"presented": 0, "dropped": 0, "skipped": 0}
        self._perf_clock = QElapsedTimer()
        self._focus_mode = False
        self._rois: list[RoiShape] = []
        self._roi_next_id = 1
        self._roi_name_counts: dict[str, int] = {}
        self._selected_roi_id: int | None = None
        self._zone_groups: list[dict] = []  # zones split from one box: ids, ZoneSpec, the box's name
        self._zone_defaults = ZoneSpec(box=((0.0, 0.0), (1.0, 1.0)))  # count, end, gap, names last used
        self._extract_progress: ProgressDialog | None = None
        self._export_progress: ProgressDialog | None = None
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
            app_settings().value(
                "playback/loop", False, type=bool
            )
        )
        # Pace presentation on frame index x nominal rate instead of on each
        # frame's recorded timestamp. Off by default: timestamp pacing is true
        # to capture, and recordings with dropped frames should look uneven
        # unless the user asks otherwise.
        self.constant_rate = bool(
            app_settings().value(
                "playback/constant_rate", False, type=bool
            )
        )
        self._play_range: tuple[int, int] | None = None
        self._wrap_pending = False
        self._end_pending = False  # decode reached range end; terminal packet still queued
        self._filters_state: dict = {
            "point": ("none", 1.0),
            "spatial": ("none", 3),
            "temporal": ("none", 5),
        }
        self._reference_params: dict | None = None
        self._object_params: dict | None = None  # latest measurement-parameter snapshot
        self._ignition_frames: dict[str, int] = {}  # recording path → ignition frame (0-based)
        # recording path → the last TC fit's request (dialog settings) and output (tcmatch.RunOutput)
        self._tc_runs: dict[str, dict] = {}
        self._temporal: dict[int, dict[int, tuple[float, object]]] = {}
        self._plot_clock = QElapsedTimer()
        self._stats_clock = QElapsedTimer()
        self._last_plot_tab = None

        self._playback_timer = QTimer(self)
        self._playback_timer.setSingleShot(True)
        self._playback_timer.timeout.connect(self._present_due)
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
        # Start with the image focused, not the first button in tab order (the
        # Open button would sit in its amber focus ring from launch on).
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)
        self.transport.set_loop(self.loop_playback)
        self.transport.set_constant_rate(self.constant_rate)

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
        self.decoder.export_stage.connect(self._on_export_stage)
        self.decoder.tc_finished.connect(self._on_tc_finished)
        self.decoder.bitmasks_finished.connect(self._on_bitmasks_finished)
        self.decoder.busy_changed.connect(self._set_busy)
        self.decoder.failed.connect(self._on_decode_failed)
        self.decoder.open_failed.connect(self._on_open_failed)
        self.decoder.state_failed.connect(self._on_state_failed)
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
        self.transport.slider.hover_text = self._timeline_hover_text

        # The frameless window has no native border to resize from.
        self._resize_grips = [
            EdgeResizeGrip(self, edges)
            for edges in EdgeResizeGrip.ALL_EDGES
        ]

        export_menu = QMenu(self)
        display_action = export_menu.addAction("Rendered frame (PNG)")
        image_action = export_menu.addAction("Export image…")
        array_action = export_menu.addAction("Raw values (NumPy)")
        csv_action = export_menu.addAction("Raw values (CSV)")
        bitmask_action = export_menu.addAction("ROI bitmasks…")
        export_menu.addSeparator()
        excel_action = export_menu.addAction("Excel workbook (live temperatures)…")
        excel_action.setToolTip("Raw counts at the ROIs with emissivity-adjustable temperature formulas")
        save_rois_action = export_menu.addAction("Save ROI set…")
        load_rois_action = export_menu.addAction("Load ROI set…")
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
        excel_action.triggered.connect(self._open_excel_dialog)
        save_rois_action.triggered.connect(self._save_roi_set)
        load_rois_action.triggered.connect(self._load_roi_set)
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
        self.inspector.tc_fit_requested.connect(self._open_tc_dialog)
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
        self.analysis_toolbar.split_requested.connect(self._split_zones)
        self.analysis_toolbar.stats_toggled.connect(self._toggle_statistics)
        self.analysis_toolbar.zoom_in_requested.connect(lambda: self.canvas.zoom_step(1))
        self.analysis_toolbar.zoom_out_requested.connect(lambda: self.canvas.zoom_step(-1))
        self.analysis_toolbar.zoom_fit_requested.connect(self.canvas.set_zoom_fit)
        self.bottom_panel.close_requested.connect(lambda: self._toggle_statistics(False))
        self.bottom_panel.statistics.save_requested.connect(self._save_statistics)
        self.bottom_panel.tabs.currentChanged.connect(self._on_tab_changed)
        self.bottom_panel.temporal.clear_button.clicked.connect(self._clear_temporal)
        self.bottom_panel.temporal.stat_combo.currentIndexChanged.connect(
            lambda _index: self._update_plots(self.current_packet, force=True)
        )
        self.transport.play_toggled.connect(self.toggle_playback)
        self.transport.seek_requested.connect(self.seek_to)
        self.transport.scrub_preview.connect(self._preview_scrub)
        self.transport.speed_changed.connect(self._change_speed)
        self.transport.loop_toggled.connect(self._change_loop)
        self.transport.constant_rate_toggled.connect(self._change_constant_rate)
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
            (QKeySequence("R"), self._toggle_constant_rate_shortcut),
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
        value = app_settings().value("files/recent", [])
        if isinstance(value, str):
            value = [value]
        return [str(path) for path in (value or [])]

    def _add_recent_file(self, path: Path) -> None:
        recents = self._recent_files()
        resolved = str(path)
        if resolved in recents:
            recents.remove(resolved)
        recents.insert(0, resolved)
        app_settings().setValue(
            "files/recent", recents[:RECENT_FILES_LIMIT]
        )
        self._refresh_recent_menu()

    def _clear_recent_files(self) -> None:
        app_settings().setValue("files/recent", [])
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
                "Choose a FLIR SEQ, ATS, SFMOV, CSQ, FFF, PTW or radiometric TIFF recording.",
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
        self._open_request_id = self._activate_request()
        self.decoder.request_open(str(resolved), self._open_request_id)

    def toggle_playback(self) -> None:
        if self.current_packet is None or self.metadata is None or self._busy:
            return
        if self.playing:
            self.pause_playback(invalidate=True)
            return
        start, end = self._playback_bounds()
        if self.current_packet.index < start or self.current_packet.index >= end:
            if start < end:
                self._restart_after_seek = True
                self.seek_to(start)
            elif self.current_packet.index != start:
                self.seek_to(start)
            # degenerate (single-frame) range: nothing to play; seeking to the
            # start would re-trigger this branch and spin a tight request loop
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
        self._perf_clock.start()
        self._latest_decoded = self.current_packet
        self._request_next_playback_frame(self.current_packet)

    def pause_playback(self, invalidate: bool = False) -> None:
        self.playing = False
        self.transport.set_playing(False)
        self._playback_timer.stop()
        self._playback_outstanding = False
        self._latest_decoded = None
        self._ready.clear()
        self._wrap_pending = False
        self._end_pending = False
        if invalidate:
            self._active_request_id = self._next_request_id()

    # --- loop / play range ------------------------------------------------------

    def _change_loop(self, loop: bool) -> None:
        self.loop_playback = bool(loop)
        app_settings().setValue(
            "playback/loop", self.loop_playback
        )

    def _toggle_loop_shortcut(self) -> None:
        self.transport.loop_button.toggle()

    def _change_constant_rate(self, constant: bool) -> None:
        """Switch between timestamp pacing and even index pacing.

        Both modes measure dues against the same media clock, but from
        different origins, so a live switch re-anchors (as a speed change does)
        rather than reinterpreting an anchor taken under the other mode.
        """
        constant = bool(constant)
        if constant == self.constant_rate:
            return
        was_playing = self.playing
        if was_playing:
            self.pause_playback(invalidate=True)
        self.constant_rate = constant
        app_settings().setValue(
            "playback/constant_rate", self.constant_rate
        )
        if was_playing:
            self._begin_playback()

    def _toggle_constant_rate_shortcut(self) -> None:
        self.transport.constant_rate_button.toggle()

    def _play_range_changed(self, start: int, end: int) -> None:
        previous = self._playback_bounds()
        self._play_range = (min(start, end), max(start, end))
        if self.playing:
            if start <= previous[0] and end >= previous[1]:
                if self._end_pending:
                    self._end_pending = False
                    self._maybe_request_next()
            else:
                self.pause_playback(invalidate=True)
                self.toggle_playback()

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
        self._playback_outstanding = True
        self.decoder.request_frame(
            start,
            request_id,
            need_clip=self.show_clipping,
            need_metadata=self._metadata_tab_active(),
        )

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
        self.decoder.request_frame(
            bounded,
            request_id,
            need_clip=self.show_clipping,
            need_metadata=self._metadata_tab_active(),
        )

    def _seek_to_end(self) -> None:
        if self.metadata is not None:
            self.seek_to(self.metadata.num_frames - 1)

    def _timeline_hover_text(self, index: int) -> str:
        if self.metadata is None:
            return ""
        # the same nominal-rate time the scrub preview shows while dragging
        seconds = self.metadata.fallback_seconds_for_frame(index)
        return f"Frame {index + 1}  ·  {format_time(seconds)}"

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
            if self.current_packet is not None:  # keep the combo on the unit in force
                self.inspector.select_unit(self.current_packet.unit.key)
            return
        self.pause_playback(invalidate=True)
        request_id = self._activate_state_request()
        self.decoder.request_unit(key, self.current_packet.index, request_id)

    def _apply_object_parameters(self, values: dict | None) -> None:
        if self.current_packet is None or self._busy:
            return
        self.pause_playback(invalidate=True)
        request_id = self._activate_state_request()
        self.decoder.request_object_params(values, self.current_packet.index, request_id)

    def _reset_object_parameters(self) -> None:
        self._apply_object_parameters(None)

    def _on_object_params_ready(self, snapshot: dict) -> None:
        self._object_params = dict(snapshot)
        self.inspector.set_object_parameters(snapshot)

    # --- ROI management --------------------------------------------------------

    def _covers_pixels(self, kind: str, points) -> bool:
        """False for boxes/ellipses that round to zero width or height.

        The worker drops such shapes, so keeping them would leave the canvas,
        the statistics and every exported CSV disagreeing about the ROI set.
        """
        if self.metadata is None:
            return False
        probe = RoiShape(0, kind, tuple((float(x), float(y)) for x, y in points), "")
        ys, _xs = roi_coordinates(probe, self.metadata.height, self.metadata.width)
        return ys.size > 0

    def _add_roi(self, kind: str, points: tuple) -> None:
        if self.current_packet is None or kind not in ROI_KIND_LABELS:
            return
        if not self._covers_pixels(kind, points):
            return  # like a too-short drag: nothing measurable was drawn
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
        self._update_plots(self.current_packet, force=True, accumulate=False)

    def _replace_roi(self, shape: RoiShape) -> None:
        if not self._covers_pixels(shape.kind, shape.points):
            self.canvas.set_rois(self._rois, self._selected_roi_id)  # snap back
            return
        for index, existing in enumerate(self._rois):
            if existing.id == shape.id:
                self._rois[index] = shape
                self._temporal.pop(shape.id, None)
                self.bottom_panel.temporal.setToolTip("History cleared for the edited ROI")
                self.bottom_panel.temporal.history_note.setText("Edited ROI history cleared")
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
        self.pause_playback(invalidate=True)
        request_id = self._activate_request()
        self.decoder.request_rois(tuple(self._rois), self.current_packet.index, request_id)

    def _clear_rois(self) -> None:
        self._rois = []
        self._tc_roi_ids: set[int] = set()  # ROIs from "Fit Emissivity from TCs" (ids restart)
        self._zone_groups = []
        self._roi_next_id = 1
        self._roi_name_counts = {}
        self._selected_roi_id = None
        self.canvas.set_rois([], None)

    # --- cell zones ----------------------------------------------------------------

    def _zone_group_of(self, roi_id: int | None) -> dict | None:
        """The split this ROI came from, if any of its zones still exist."""
        present = {shape.id for shape in self._rois}
        self._zone_groups = [g for g in self._zone_groups if present & set(g["ids"])]
        return next((g for g in self._zone_groups if roi_id in g["ids"]), None)

    def _tc_pixels(self) -> dict[str, tuple[int, int]]:
        """TC → (row, col) from this session's TC fit of the open recording."""
        if self.metadata is None:
            return {}
        output = self._tc_runs.get(str(self.metadata.path), {}).get("output")
        if output is None:
            return {}
        return {ch.name: tuple(ch.match.pixel) for ch in output.result.channels
                if ch.match.found and ch.match.pixel is not None}

    def _split_zones(self) -> None:
        """Split the selected box into equal zones, or re-split / join the zones of an earlier split."""
        if self.metadata is None or self.current_packet is None or self._busy:
            return
        selected = next((shape for shape in self._rois if shape.id == self._selected_roi_id), None)
        group = self._zone_group_of(self._selected_roi_id)
        if group is not None:
            spec, replaced, box_name = group["spec"], set(group["ids"]), group["name"]
        elif selected is not None and selected.kind == "rect":
            d = self._zone_defaults
            spec = ZoneSpec(box=normalized_box(selected.points), count=d.count, start=d.start, gap=d.gap,
                            prefix=d.prefix)
            replaced, box_name = {selected.id}, selected.name
        else:
            self._notify("Select a box ROI to split into zones (or a zone to re-split)")
            return
        self.pause_playback(invalidate=True)
        width, height = self.metadata.width, self.metadata.height
        others = [shape for shape in self._rois if shape.id not in replaced]
        base = max([shape.id for shape in self._rois] + [self._roi_next_id]) + 1

        def preview(zones) -> None:
            shapes = [RoiShape(base + k, "rect", points, name) for k, (name, points) in enumerate(zones)]
            self.canvas.set_rois(others + shapes, None)

        dialog = ZoneSplitDialog(spec, width, height, tc_pixels=self._tc_pixels(), regroup=group is not None,
                                 parent=self)
        dialog.preview.connect(preview)
        code = dialog.exec()
        if code == ZoneSplitDialog.DialogCode.Accepted and dialog.zones():
            new_spec = dialog.spec()
            self._zone_defaults = new_spec
            zones = [RoiShape(self._roi_next_id + k, "rect", points, name)
                     for k, (name, points) in enumerate(dialog.zones())]
            self._roi_next_id += len(zones)
            self._replace_with(replaced, zones)
            if group is not None:
                self._zone_groups.remove(group)
            self._zone_groups.append({"ids": [z.id for z in zones], "spec": new_spec, "name": box_name})
            self._notify(f"Split into {len(zones)} zones: {zones[0].name} to {zones[-1].name}")
        elif code == ZoneSplitDialog.JOIN and group is not None:
            box = RoiShape(self._roi_next_id, "rect", tuple(group["spec"].box), box_name)
            self._roi_next_id += 1
            self._replace_with(replaced, [box])
            self._zone_groups.remove(group)
            self._selected_roi_id = box.id
            self._notify(f"Joined the zones back into {box_name}")
        self._push_rois()  # also restores the canvas after a cancel

    def _replace_with(self, ids: set[int], shapes: list[RoiShape]) -> None:
        """Put ``shapes`` where the first of ``ids`` was in the ROI list and drop the others."""
        position = next((k for k, shape in enumerate(self._rois) if shape.id in ids), len(self._rois))
        kept = [shape for shape in self._rois if shape.id not in ids]
        before = sum(1 for shape in self._rois[:position] if shape.id not in ids)
        self._rois = kept[:before] + list(shapes) + kept[before:]
        if self._selected_roi_id in ids:
            self._selected_roi_id = None
        if not self.bottom_panel.isVisible():
            self._toggle_statistics(True)

    # --- statistics panel ------------------------------------------------------

    def _toggle_statistics(self, show: bool) -> None:
        self.bottom_panel.setVisible(show)
        self.analysis_toolbar.stats_button.blockSignals(True)
        self.analysis_toolbar.stats_button.setChecked(show)
        self.analysis_toolbar.stats_button.blockSignals(False)
        if show:
            self._update_statistics(force=True)
            # Reopening the panel is not a tab change, so the tab-changed
            # hook never fires — fetch any payload the visible tab needs.
            self._ensure_current_payload()

    def _update_statistics(self, force: bool = False) -> None:
        packet = self.current_packet
        if packet is None or not self.bottom_panel.isVisible():
            return
        # Tables rebuild Qt items cell by cell, so only the visible tab is
        # refreshed, throttled to ~5 Hz during playback (immediate on pause,
        # seek, or when a tab becomes visible).
        due = (
            not self.playing
            or not self._stats_clock.isValid()
            or self._stats_clock.elapsed() >= 200
        )
        if force or due:
            self._stats_clock.restart()
            current = self.bottom_panel.tabs.currentWidget()
            if current is self.bottom_panel.statistics:
                image_stats = (
                    packet.minimum,
                    packet.maximum,
                    packet.mean,
                    packet.std_dev,
                    packet.num_pixels,
                )
                self.bottom_panel.statistics.set_statistics(
                    packet.roi_stats, image_stats, packet.unit.suffix,
                    StatisticsSnapshot(packet, self.metadata, tuple(self._rois)),
                )
            elif current is self.bottom_panel.metadata:
                self.bottom_panel.metadata.set_entries(packet.metadata_entries)
        self._update_plots(packet)

    def _metadata_tab_active(self) -> bool:
        return (
            self.bottom_panel.isVisible()
            and self.bottom_panel.tabs.currentWidget() is self.bottom_panel.metadata
        )

    def _on_tab_changed(self, _index: int) -> None:
        self._update_statistics(force=True)
        self._ensure_current_payload()

    def _ensure_current_payload(self) -> None:
        """Refetch the current frame when the visible tab needs a payload the
        packet was decoded without (metadata is skipped for lean interactive
        decodes while the Metadata tab is hidden)."""
        if (
            self._metadata_tab_active()
            and not self.playing
            and self.current_packet is not None
            and not self.current_packet.metadata_loaded
        ):
            request_id = self._activate_request()
            self.decoder.request_frame(
                self.current_packet.index,
                request_id,
                need_clip=self.show_clipping,
                need_metadata=True,
            )

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
                # Keyed by frame index (dedupe); compact tuples, bounded count.
                history = self._temporal.setdefault(stats.id, {})
                if len(history) > TEMPORAL_POINT_CAP:
                    # drop the oldest ~half (insertion order) to bound memory
                    trim = len(history) - TEMPORAL_POINT_CAP // 2
                    for old_key in list(history)[:trim]:
                        del history[old_key]
                history[packet.index] = (
                    seconds,
                    stats.mean,
                    stats.minimum,
                    stats.maximum,
                    stats.std_dev,
                )
            live_ids = {stats.id for stats in packet.roi_stats}
            for stale in [roi_id for roi_id in self._temporal if roi_id not in live_ids]:
                del self._temporal[stale]

        current = panels.tabs.currentWidget()
        due = not self.playing or not self._plot_clock.isValid() or self._plot_clock.elapsed() >= 250
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
            attr_index = _TEMPORAL_STAT_INDEX[stat_label]
            series = []
            for shape in self._rois:
                points = self._temporal.get(shape.id)
                if not points:
                    continue
                ordered = [points[key] for key in sorted(points)]
                series.append(
                    (
                        shape.name,
                        ROI_COLORS[shape.id % len(ROI_COLORS)],
                        np.fromiter(
                            (point[0] for point in ordered), float, len(ordered)
                        ),
                        np.fromiter(
                            (point[attr_index] for point in ordered),
                            float,
                            len(ordered),
                        ),
                    )
                )
            panels.temporal.set_series(series, stat_label, suffix)

    def _clear_temporal(self) -> None:
        self._temporal.clear()
        self.bottom_panel.temporal.history_note.setText("History cleared")
        self._update_plots(self.current_packet, force=True, accumulate=False)

    # --- extract clip -----------------------------------------------------------

    def _open_extract_dialog(self) -> None:
        if self.metadata is None or self._busy:
            return
        self.pause_playback(invalidate=True)
        dialog = ExtractDialog(self.metadata, self, default_range=self._play_range)
        if dialog.exec() == ExtractDialog.DialogCode.Accepted:
            self._start_extract(dialog.parameters())

    def _progress_dialog(self, title: str, label: str) -> ProgressDialog:
        return ProgressDialog(title, label, self)

    def _start_extract(self, params: dict) -> None:
        self._extract_dest = Path(params["dest"]).name
        self._extract_progress = self._progress_dialog("Extract Clip", "Extracting clip…")
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
            self._notify(message or f"Extracted {getattr(self, '_extract_dest', 'the clip')}")
        elif message and message != "Extraction cancelled":
            self._show_error("Extract failed", message)

    # --- export: still image / bitmasks / movie / series / batch -------------------

    def _open_export_image_dialog(self) -> None:
        if self.current_packet is None or self.metadata is None:
            return
        self.pause_playback(invalidate=True)
        snapshot = (self.current_packet, self.metadata, self._display_state(self.current_packet), tuple(self._rois))
        dialog = ExportImageDialog(snapshot[1], snapshot[0].index, self)
        if dialog.exec() != ExportImageDialog.DialogCode.Accepted:
            return
        self._export_still(dialog.parameters(), snapshot)

    def _export_still(self, params: dict, snapshot=None) -> None:
        packet, metadata, state, rois = snapshot or (
            self.current_packet, self.metadata, self._display_state(self.current_packet), tuple(self._rois))
        rgb, low, high, mapping = render_frame_rgb(
            packet.data, state, packet.clip_mask, extrema=(packet.minimum, packet.maximum), return_mapping=True
        )
        options = params["options"]
        label = frame_burn_label(packet, metadata) if options.timestamp else ""
        composed = compose_frame(
            rgb,
            options,
            palette=state.palette,
            inverted=state.inverted,
            scale=(low, high),
            suffix=packet.unit.suffix,
            rois=rois,
            roi_stats=packet.roi_stats,
            min_position=packet.min_position,
            max_position=packet.max_position,
            label=label,
            flips=(state.flip_h, state.flip_v),
            mapping=mapping, segmentation=state.segmentation, isotherm=state.isotherm,
        )
        dest = Path(params["dest"])
        try:
            # Only the files the dialog confirmed may be replaced.
            with OutputTransaction([metadata.path], replace=params.get("replace", ())) as job:
                frame_path = job.stage(dest)
                csv_path = job.stage(dest.with_suffix(".csv")) if params["stats_sidecar"] else None
                save_frame(frame_path, params["fmt"], composed, packet.data, (low, high), packet=packet, source=metadata)
                if csv_path is not None:
                    write_stats_csv(csv_path, stats_csv_header([shape.name for shape in rois]),
                        [stats_csv_row(packet, packet.unit.label, rois=rois, scale=(low, high),
                                       fmt=params["fmt"])])
                job.commit()
            self._notify(f"Saved {dest.name}")
        except Exception as exc:
            self._show_error("Export failed", f"{type(exc).__name__}: {exc}")

    def _export_bitmasks(self) -> None:
        if self.current_packet is None or self._busy:
            return
        if not self._rois:
            self._notify("Draw an ROI first: bitmasks are written one per ROI")
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Choose bitmask output folder", str(self.metadata.path.parent)
        )
        if not folder:
            return
        replace = confirm_replace(
            self, [Path(folder) / bitmask_filename(shape.name) for shape in self._rois])
        if replace is not None:
            self.decoder.request_export_bitmasks(folder, overwrite=replace)  # confirmed paths

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

    # --- Excel workbook and ROI sets ---------------------------------------------

    def _ignition_frame(self) -> int | None:
        """Ignition frame of the open recording: this session's, else its ROI set's."""
        if self.metadata is None:
            return None
        key = str(self.metadata.path)
        if key in self._ignition_frames:
            return self._ignition_frames[key]
        sidecar = roi_set_path(self.metadata.path)
        if sidecar.exists():
            try:
                frame = load_roi_set(sidecar).ignition_frame
            except (OSError, ValueError, KeyError):
                frame = None
            if frame is not None and 0 <= frame < self.metadata.num_frames:
                return frame
        return None

    def _roi_set(self) -> RoiSet:
        metadata = self.metadata
        self._zone_group_of(None)  # forget splits whose zones are all gone
        position = {shape.id: k for k, shape in enumerate(self._rois)}
        groups = tuple(ZoneGroup(rois=tuple(position[i] for i in g["ids"] if i in position), spec=g["spec"],
                                 name=g["name"]) for g in self._zone_groups)
        return RoiSet(rois=tuple(self._rois), ignition_frame=self._ignition_frame(),
                      recording=metadata.path.name, size=(metadata.width, metadata.height), zone_groups=groups)

    def _open_excel_dialog(self) -> None:
        if self.metadata is None or self.current_packet is None or self._busy:
            return
        self.pause_playback(invalidate=True)
        dialog = ExcelExportDialog(self.metadata, tuple(self._rois), self.current_packet.index,
                                   ignition_frame=self._ignition_frame(),
                                   tc_run=self._tc_runs.get(str(self.metadata.path), {}).get("output"), parent=self)
        if dialog.exec() != ExcelExportDialog.DialogCode.Accepted:
            return
        params = dialog.parameters()
        self._ignition_frames[str(self.metadata.path)] = params["sources"][0]["ignition_frame"]
        if params["save_sidecar"] and self._rois:
            try:
                save_roi_set(roi_set_path(self.metadata.path), self._roi_set())
            except OSError as exc:
                self._show_error("ROI set", f"Could not save the ROI set next to the recording: {exc}")
        self._start_export(params, "Export Excel Workbook")

    def _save_roi_set(self) -> None:
        if self.metadata is None:
            return
        if not self._rois:
            self._notify("Draw an ROI first: there is no ROI set to save")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save ROI set", str(roi_set_path(self.metadata.path)),
            "ROI sets (*.rois.json);;All files (*.*)")
        if not path:
            return
        if self.current_packet is not None and self._ignition_frame() is None:
            # Remember where the user is: usually the moment of ignition.
            self._ignition_frames[str(self.metadata.path)] = self.current_packet.index
        try:
            save_roi_set(path, self._roi_set())
            self._notify(f"Saved {Path(path).name}")
        except OSError as exc:
            self._show_error("Save ROI set", f"{type(exc).__name__}: {exc}")

    def _load_roi_set(self) -> None:
        if self.metadata is None or self.current_packet is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load ROI set", str(self.metadata.path.parent),
            "ROI sets (*.rois.json *.json);;All files (*.*)")
        if not path:
            return
        try:
            roi_set = load_roi_set(path)
        except (OSError, ValueError, KeyError) as exc:
            self._show_error("Load ROI set", f"{type(exc).__name__}: {exc}")
            return
        self._clear_rois()
        ids: dict[int, int] = {}  # position in the set → new id
        for k, shape in enumerate(roi_set.rois):
            if not self._covers_pixels(shape.kind, shape.points):
                continue
            self._rois.append(RoiShape(id=self._roi_next_id, kind=shape.kind, points=shape.points,
                                       name=shape.name))
            ids[k] = self._roi_next_id
            self._roi_next_id += 1
            self._roi_name_counts[shape.kind] = self._roi_name_counts.get(shape.kind, 0) + 1
        kept = list(self._rois)
        for group in roi_set.zone_groups:
            members = [ids[k] for k in group.rois if k in ids]
            if members:
                self._zone_groups.append({"ids": members, "spec": group.spec, "name": group.name})
        if roi_set.ignition_frame is not None and roi_set.recording == self.metadata.path.name                 and 0 <= roi_set.ignition_frame < self.metadata.num_frames:
            self._ignition_frames[str(self.metadata.path)] = roi_set.ignition_frame
        skipped = len(roi_set.rois) - len(kept)
        note = f" ({skipped} outside this image skipped)" if skipped else ""
        self._notify(f"Loaded {len(kept)} ROI(s){note}")
        self._push_rois()

    def _export_payload(self, params: dict) -> dict:
        """Attach the current display/ROI state to a movie/series request."""
        packet = self.current_packet
        params["display"] = self._display_state(packet)
        params["selected_roi_id"] = self._selected_roi_id
        params["rois"] = tuple(self._rois)
        params["suffix"] = packet.unit.suffix
        params["unit_label"] = packet.unit.label
        params["revision"] = packet.revision
        return params

    def _start_export(self, params: dict, title: str) -> None:
        if params.get("kind") in {"movie", "series"}:
            params = self._export_payload(params)
        # what to confirm when the job itself reports nothing
        self._export_done_text = {
            "movie": f"Saved {Path(str(params.get('dest', ''))).name}",
            "series": f"Saved the image series in {Path(str(params.get('dest', ''))).name}",
        }.get(params.get("kind"), f"{title} finished")
        self._export_progress = self._progress_dialog(title, f"{title}…")
        self._export_progress.canceled.connect(self.decoder.cancel_extract)
        if params.get("kind") == "excel":
            self.decoder.request_export_excel(params)
        elif params.get("kind") in {"movie", "series"}:
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
            self._notify(message or getattr(self, "_export_done_text", "Export finished"))
        elif message and message != "Export cancelled":
            self._show_error("Export failed", message)

    def _on_export_stage(self, text: str) -> None:
        dialog = getattr(self, "_export_progress", None)
        if dialog is None or dialog.wasCanceled():
            return  # "Cancelling…" stays until the job stops
        dialog.setLabelText(f"{text}…")
        dialog.setMaximum(0)  # busy until the step reports progress
        dialog.setValue(0)

    # --- emissivity from thermocouples ---------------------------------------------

    def _open_tc_dialog(self) -> None:
        if self.metadata is None or self.current_packet is None or self._busy:
            return
        self.pause_playback(invalidate=True)
        if not any(key.startswith("temperature_factory") for key in getattr(self, "_unit_keys", ())):
            MessageDialog.information(
                self, "Fit Emissivity from TCs",
                "This recording has no factory temperature calibration (counts only, or a ResearchIR user "
                "calibration), so its emissivity cannot be fitted from TCs.")
            return
        run = self._tc_runs.get(str(self.metadata.path), {})
        dialog = TcCalibrationDialog(self.metadata, tuple(self._rois), self.current_packet.index,
                                     selected_roi_id=self._selected_roi_id, ignition_frame=self._ignition_frame(),
                                     previous=run.get("request"), has_results="output" in run, parent=self)
        code = dialog.exec()
        if code == TcCalibrationDialog.LAST_RESULTS:
            self._show_tc_results()
            return
        if code != TcCalibrationDialog.DialogCode.Accepted:
            return
        params = dialog.parameters()
        # the request is remembered at once (the next dialog starts from it); results only on success
        self._tc_runs.setdefault(str(self.metadata.path), {})["request"] = params
        self._tc_pending = params
        self._export_progress = self._progress_dialog("Fit Emissivity from TCs", "Starting…")
        self._export_progress.canceled.connect(self.decoder.cancel_extract)
        self.decoder.request_tc_match(params)

    def _on_tc_finished(self, ok: bool, message: str, output) -> None:
        dialog = getattr(self, "_export_progress", None)
        if dialog is not None:
            dialog.reset()
            dialog.deleteLater()
            self._export_progress = None
        if not ok:
            if message and message != "Cancelled":
                self._show_error("Fit emissivity from TCs", message)
            return
        request = getattr(self, "_tc_pending", None) or {}
        if self.metadata is None or Path(request.get("recording", "")).resolve() != self.metadata.path.resolve():
            return  # another recording was opened meanwhile
        run = self._tc_runs.setdefault(str(self.metadata.path), {})
        run["output"] = output
        run["out_dir"] = (getattr(self, "_tc_pending", None) or {}).get("out_dir")
        self._show_tc_results()

    def _show_tc_results(self) -> None:
        if self.metadata is None:
            return
        run = self._tc_runs.get(str(self.metadata.path), {})
        output = run.get("output")
        if output is None:
            return
        out_dir = run.get("out_dir")
        dialog = TcResultsDialog(output, Path(out_dir) if out_dir else None, parent=self)
        dialog.add_rois_requested.connect(lambda: self._add_tc_rois(output))
        dialog.emissivity_requested.connect(self._set_tc_emissivity)
        if dialog.exec() == TcResultsDialog.WORKBOOK:
            self._save_tc_workbook(output, Path(out_dir) if out_dir else self.metadata.path.parent)

    def _set_tc_emissivity(self, eps: float) -> None:
        if self.current_packet is None or self._busy or not (self._object_params or {}).get("can_change", True):
            self._notify("The emissivity of this recording cannot be changed now")
            return
        self._apply_object_parameters({"emissivity": float(eps)})
        self._notify(f"Emissivity set to {eps:.2f} (Measurement panel)")

    def _add_tc_rois(self, output) -> None:
        """A box at each TC pixel, the block the fit used (replacing earlier ones of the same names);
        ignition = logger time 0. The workbook also gets single-pixel spots."""
        if self.metadata is None or self.current_packet is None:
            return
        shapes = [shape for shape in roi_shapes(output.result)
                  if shape.kind == "rect" and self._covers_pixels(shape.kind, shape.points)]
        names = {shape.name for shape in shapes}
        earlier = getattr(self, "_tc_roi_ids", set())  # added by an earlier fit (labels can change)
        self._rois = [shape for shape in self._rois if shape.name not in names and shape.id not in earlier]
        if self._selected_roi_id not in {shape.id for shape in self._rois}:
            self._selected_roi_id = None
        self._tc_roi_ids = set()
        for shape in shapes:
            self._rois.append(RoiShape(id=self._roi_next_id, kind=shape.kind, points=shape.points, name=shape.name))
            self._tc_roi_ids.add(self._roi_next_id)
            self._roi_next_id += 1
        frame, _offset = workbook_origin(output.result)
        self._ignition_frames[str(self.metadata.path)] = frame
        if not self.bottom_panel.isVisible():
            self._toggle_statistics(True)
        self._push_rois()
        self._notify(f"Added {len(shapes)} ROI(s) at the TC pixels; ignition (t = 0) is frame {frame + 1}")

    def _save_tc_workbook(self, output, folder: Path) -> None:
        if self.metadata is None:
            return
        default = folder / f"{self.metadata.path.stem}_vs_TC.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "Save Excel workbook", str(default), "Excel workbook (*.xlsx)")
        if not path:
            return
        dest = Path(path) if Path(path).suffix.lower() == ".xlsx" else Path(path).with_suffix(".xlsx")
        replace = confirm_replace(self, [dest], [path])
        if replace is None:
            return
        self._export_done_text = f"Saved {dest.name}"
        self._export_progress = self._progress_dialog("Export Excel Workbook", "Export Excel Workbook…")
        self._export_progress.canceled.connect(self.decoder.cancel_extract)
        self.decoder.request_tc_workbook({
            "recording": str(self.metadata.path), "dest": str(dest), "result": output.result,
            "table": output.table, "parameters": output.parameters, "replace": [str(p) for p in replace],
            "tc_file": str(output.tc_file) if output.tc_file is not None else None})

    def _on_bitmasks_finished(self, ok: bool, message: str) -> None:
        if ok:
            self._notify(message)
        else:
            self._show_error("Export failed", message)

    def _save_statistics(self) -> None:
        snapshot = self.bottom_panel.statistics.snapshot
        if snapshot is None:
            return
        packet, metadata = snapshot.packet, snapshot.metadata
        rows = self.bottom_panel.statistics.to_rows()
        default_name = unique_destination(
            metadata.path.parent
            / f"{metadata.path.stem}_frame_{packet.index + 1:05d}_stats.csv"
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
        replace = confirm_replace(self, [output], confirmed=[path])  # Save dialog asked already
        if replace is None:
            return
        try:
            with OutputTransaction([metadata.path], replace=replace) as job:
                with job.stage(output).open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["Source", str(metadata.path)])
                    writer.writerow(["Frame", f"{packet.index + 1} / {metadata.num_frames}"])
                    writer.writerow(["Unit", packet.unit.label])
                    writer.writerow(["Timestamp", packet.timestamp.isoformat() if packet.timestamp else ""])
                    writer.writerow(["Analysis revision", packet.revision])
                    writer.writerow(["ROIs", repr(snapshot.rois)])
                    writer.writerow([])
                    writer.writerows(rows)
                job.commit()
            self._notify(f"Saved {output.name}")
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
        if name in CUSTOM_PALETTE_STOPS and not (existing and name == current):
            self._show_error("Invalid palette", f"A custom palette named “{name}” already exists.")
            return
        try:
            register_custom_palette(name, dialog.palette_stops())
        except ValueError as exc:
            self._show_error("Invalid palette", str(exc))
            return
        if existing and name != current:  # a rename retires the old entry everywhere
            unregister_custom_palette(current)
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
        return display_scale(
            packet.data,
            self._display_state(packet),
            extrema=(packet.minimum, packet.maximum),
        )

    def _change_overlays(self, clipping: bool, markers: bool) -> None:
        clipping_changed = clipping != self.show_clipping
        self.show_clipping = clipping
        self.show_markers = markers
        self.canvas.set_overlay_options(markers)
        if clipping and clipping_changed and self.current_packet is not None:
            if self.current_packet.clip_mask is None and not self.playing:
                # This packet was decoded while clipping was off and has no
                # mask; refetch the current frame with one.
                request_id = self._activate_request()
                self.decoder.request_frame(
                    self.current_packet.index,
                    request_id,
                    need_clip=True,
                    need_metadata=self._metadata_tab_active(),
                )
                return
            # playing: the next decoded frame carries the mask again
        if clipping_changed:
            self._render_current_frame()
        # markers are painted by the canvas; toggling them alone needs no
        # RGB rerender.

    def _change_flips(self, flip_h: bool, flip_v: bool) -> None:
        self.flip_h = flip_h
        self.flip_v = flip_v
        self.canvas.set_flips(flip_h, flip_v)
        self._render_current_frame()

    def _change_corrections(self, nuc: bool, bp: bool) -> None:
        if self.current_packet is None or self._busy:
            return
        self.pause_playback(invalidate=True)
        request_id = self._activate_state_request()
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
        request_id = self._activate_state_request()
        self.decoder.request_reference(
            self._reference_params, self.current_packet.index, request_id
        )

    def _clear_reference(self) -> None:
        if self.current_packet is None or self._busy:
            return
        self.pause_playback(invalidate=True)
        self._reference_params = None
        request_id = self._activate_state_request()
        self.decoder.request_reference(None, self.current_packet.index, request_id)

    def _change_filters(self, state: dict) -> None:
        if self.current_packet is not None and self._busy:
            self.inspector.set_processing(self._filters_state)  # not sent: undo the edit
            return
        self._filters_state = {
            "point": tuple(state.get("point", ("none", 1.0))),
            "spatial": tuple(state.get("spatial", ("none", 3))),
            "temporal": tuple(state.get("temporal", ("none", 5))),
        }
        if self.current_packet is None:
            return
        self.pause_playback(invalidate=True)
        request_id = self._activate_state_request()
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

    def _request_next_playback_frame(self, after: FramePacket) -> None:
        """Request the next frame to decode (issued on arrival, not after
        presentation, so decode overlaps the presentation wait)."""
        if not self.playing or self.metadata is None:
            return
        start, end = self._playback_bounds()
        next_index = max(start, self._skip_ahead_index(after, end))
        if next_index > end:
            # Decode reached the range end. Do not pause/wrap here: the
            # terminal packet is still queued and must be presented first
            # (otherwise the range's last frame is never shown).
            self._end_pending = True
            self._finish_range_end_if_done()
            return
        request_id = self._activate_request()
        self._playback_outstanding = True
        self.decoder.request_frame(
            next_index,
            request_id,
            need_clip=self.show_clipping,
            need_metadata=self._metadata_tab_active(),
        )

    def _finish_range_end_if_done(self) -> bool:
        """Pause/wrap once *presentation* has reached the decoded range end.

        Waits while the terminal packet is still queued or not yet presented;
        resolves immediately when the end frame is already on screen (e.g.
        resuming playback exactly at the range end). Loop wraps are issued
        only here, so the wrapped start arrives to an empty queue and the
        anchor reset never mixes laps. Returns True when playback was paused
        or a wrap was issued.
        """
        if not self._end_pending:
            return False
        start, end = self._playback_bounds()
        if any(packet.index >= end for packet in self._ready):
            return False
        if self.current_packet is None or self.current_packet.index < end:
            return False
        self._end_pending = False
        if self.loop_playback:
            self._wrap_playback(start)
        else:
            self.pause_playback()
        return True

    def _skip_ahead_index(self, after: FramePacket, end: int) -> int:
        """Next decode index: +1 normally; media-clock catch-up when late.

        Decode skipping is disabled while a temporal filter is active — those
        accumulate state from every decoded frame, so only presentation may
        drop. A single jump is capped at one second of media time.
        """
        next_index = after.index + 1
        if next_index > end or self._wrap_pending:
            return next_index
        temporal_active = self._filters_state.get("temporal", ("none", 5))[0] != "none"
        fps = self.metadata.nominal_fps if self.metadata and self.metadata.nominal_fps > 0 else 30.0
        if temporal_active or self.playback_speed <= 0:
            return next_index
        now = self._playback_clock.elapsed() / 1000.0
        target = self._playback_target_seconds(after) / self.playback_speed
        if now - target <= 1.0 / fps / self.playback_speed:
            return next_index
        frames_behind = int((now * self.playback_speed - self._playback_target_seconds(after)) * fps)
        expected = after.index + max(frames_behind, 1)
        expected = min(expected, after.index + max(1, int(fps)))
        skipped = max(0, min(expected, end) - after.index - 1)
        self._perf["skipped"] += skipped
        return min(expected, end)

    def _maybe_request_next(self) -> None:
        """Issue the next decode request, bounded by a media-time horizon.

        Decode-ahead overlaps the presentation wait, but is capped at ~2 frame
        intervals ahead of the media clock; otherwise the decode chain would
        free-run at SDK speed and presentation dues would race into the
        future. When playback falls behind, the horizon is negative and the
        request fires immediately (catch-up; `_skip_ahead_index` may jump).
        """
        if not self.playing or self.metadata is None or self._playback_outstanding:
            return
        if self._end_pending:
            return  # nothing left to decode past the range end
        latest = self._latest_decoded
        if latest is None:
            return
        fps = self.metadata.nominal_fps if self.metadata.nominal_fps > 0 else 30.0
        horizon = 1.5 / fps / self.playback_speed
        due = self._playback_target_seconds(latest) / self.playback_speed
        if due - self._playback_clock.elapsed() / 1000.0 > horizon:
            return
        self._request_next_playback_frame(latest)

    def _arm_presentation_timer(self) -> None:
        if not self._ready:
            return
        packet = self._ready[0]
        target = self._playback_target_seconds(packet) / self.playback_speed
        delay = max(0.0, target - self._playback_clock.elapsed() / 1000.0)
        self._playback_timer.start(max(0, int(round(delay * 1000.0))))

    def _present_due(self) -> None:
        """Present the newest due packet; drop superseded ones."""
        if not self.playing or not self._ready:
            return
        now = self._playback_clock.elapsed() / 1000.0
        newest_due = -1
        for position, packet in enumerate(self._ready):
            due = self._playback_target_seconds(packet) / self.playback_speed
            if due <= now + 0.0005:
                newest_due = position
        if newest_due < 0:
            self._maybe_request_next()
            self._arm_presentation_timer()
            return
        for _ in range(newest_due):
            self._ready.popleft()
            self._perf["dropped"] += 1
        packet = self._ready.popleft()
        lateness = now - self._playback_target_seconds(packet) / self.playback_speed
        self._present_packet(packet)
        self._perf["presented"] += 1
        if self._perf_debug and self._perf_clock.elapsed() >= 1000:
            elapsed = self._perf_clock.elapsed() / 1000.0
            print(
                f"[perf] presented {self._perf['presented'] / elapsed:.1f} fps, "
                f"dropped {self._perf['dropped']}, skipped {self._perf['skipped']}, "
                f"lateness {lateness * 1000.0:.1f} ms",
                file=sys.stderr,
            )
            self._perf = {"presented": 0, "dropped": 0, "skipped": 0}
            self._perf_clock.restart()
        if self._finish_range_end_if_done():
            return
        self._maybe_request_next()
        if self._ready:
            self._arm_presentation_timer()

    def _on_opened(
        self,
        metadata: VideoMetadata,
        options: tuple[UnitOption, ...],
        packet: FramePacket,
    ) -> None:
        if packet.request_id != self._open_request_id:
            return  # a later open superseded this one; the worker has moved on
        self.metadata = metadata
        self._add_recent_file(metadata.path)
        self._active_request_id = packet.request_id
        self.title_bar.set_filename(metadata.filename)
        self.title_bar.export_button.setEnabled(True)
        self.inspector.set_available_units(options, packet.unit.key)
        self._unit_keys = frozenset(option.key for option in options)
        self.inspector.set_data_available(True)
        self.analysis_toolbar.set_enabled(True)
        self.bottom_panel.source.set_details(metadata.source_details)
        self.inspector.set_saved_parameters(metadata.saved_parameters, metadata.saved_by)
        if metadata.saved_parameters is not None:
            who = "ResearchIR" if metadata.saved_by == "ResearchIR" else "software"
            self._notify(f"Opened with the camera's object parameters. The file's saved {who} "
                         f"override is not applied: Measurement → {saved_button_text(metadata.saved_by)}.")
        self.transport.set_cadence(metadata.cadence)
        self.transport.set_video(metadata.num_frames, metadata.duration_seconds)
        self.inspector.set_median_size_cap(
            median_max_size(metadata.width * metadata.height)
        )
        self._sync_value_controls(packet, None)  # a new source re-seeds them
        self._present_packet(packet)

    def _on_frame_ready(self, packet: FramePacket) -> None:
        if packet.request_id != self._active_request_id:
            return
        if self.playing and self.current_packet is not None:
            if self._wrap_pending:
                self._wrap_pending = False
                self._anchor_after_wrap(packet)
            if len(self._ready) >= 4:
                # genuine overload only: the 1.5-interval decode horizon keeps
                # the queue at ~2 packets in steady state, so reaching the cap
                # means presentation is behind and superseded packets may go
                self._ready.popleft()
                self._perf["dropped"] += 1
            self._ready.append(packet)
            if not self._playback_timer.isActive():
                self._arm_presentation_timer()
            if self._playback_outstanding:
                self._playback_outstanding = False
            self._latest_decoded = packet
            self._maybe_request_next()
            return
        self._present_packet(packet)
        if self._restart_after_seek:
            self._restart_after_seek = False
            QTimer.singleShot(0, self.toggle_playback)

    def _anchor_after_wrap(self, packet: FramePacket) -> None:
        """Anchor the wrapped lap to the running media clock.

        The new lap's first frame is due one nominal interval after the
        previous lap's terminal frame, so the cadence continues across the
        wrap and no anti-spin hold (or stale-lap queue) is needed. The clock
        is NOT restarted: dues are media seconds in one continuous space.
        """
        fps = (
            self.metadata.nominal_fps
            if self.metadata and self.metadata.nominal_fps > 0
            else 30.0
        )
        base = 0.0
        if self.current_packet is not None:
            base = self._playback_target_seconds(self.current_packet) + 1.0 / fps
        if packet.timestamp is not None:
            self._playback_anchor_timestamp = packet.timestamp - timedelta(seconds=base)
        else:
            self._playback_anchor_timestamp = None
        self._playback_anchor_index = packet.index - base * fps

    def _on_unit_ready(self, option: UnitOption, packet: FramePacket) -> None:
        if packet.request_id != self._active_request_id:
            return  # value controls follow the next presented packet instead
        self._present_packet(packet)

    def _present_packet(self, packet: FramePacket) -> None:
        previous = self.current_packet
        if previous is not None and packet.revision != previous.revision:
            self._temporal.clear()
            self.bottom_panel.temporal.setToolTip("History cleared: analysis settings changed")
            self.bottom_panel.temporal.history_note.setText("History cleared: settings changed")
            # Driven by the presented data, not by acknowledgements, so a unit
            # change overtaken by a seek still converts the value controls.
            transition = value_transition(previous.unit.key, previous.processing,
                                          packet.unit.key, packet.processing)
            if transition != (1.0, 0.0):
                self._sync_value_controls(packet, transition)
        self.current_packet = packet
        self._render_current_frame()

    def _sync_value_controls(self, packet: FramePacket, transition) -> None:
        """Carry fixed range and thresholds into a new numerical domain.

        ``transition`` is an affine (factor, offset) from ``value_transition``;
        None re-seeds from the frame the way first enabling each control does
        (an isotherm at the frame minimum would paint the whole image).
        """
        if transition is None:
            low, high = packet.minimum, packet.maximum
            self.fixed_minimum, self.fixed_maximum = low, high
            self.seg_min, self.seg_max = low, high
            self.iso_limit1, self.iso_limit2 = (low + high) / 2.0, high
        else:
            factor, offset = transition
            self.fixed_minimum, self.fixed_maximum = (
                factor * self.fixed_minimum + offset, factor * self.fixed_maximum + offset)
            self.seg_min, self.seg_max = (factor * self.seg_min + offset, factor * self.seg_max + offset)
            self.iso_limit1, self.iso_limit2 = (
                factor * self.iso_limit1 + offset, factor * self.iso_limit2 + offset)
        self.inspector.set_value_precision(packet.minimum, packet.maximum)
        self.inspector.set_fixed_values(self.fixed_minimum, self.fixed_maximum)
        self.inspector.set_segmentation_values(self.seg_min, self.seg_max)
        self.inspector.set_isotherm_values(self.iso_limit1, self.iso_limit2)

    def _render_current_frame(self) -> None:
        if self.current_packet is None or self.metadata is None:
            return
        packet = self.current_packet
        rgb, low, high, mapping = render_frame_rgb(
            packet.data,
            self._display_state(packet),
            packet.clip_mask,
            extrema=(packet.minimum, packet.maximum),
            return_mapping=True,
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
            mapping=mapping, segmentation=self._display_state(packet).segmentation,
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
        frame_timed = self.metadata is not None and self.metadata.frame_timed
        if current.timestamp is not None and following.timestamp is not None and not frame_timed:
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
        if (
            not self.constant_rate
            and not (self.metadata is not None and self.metadata.frame_timed)
            and self._playback_anchor_timestamp is not None
            and packet.timestamp is not None
        ):
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
        if getattr(self, "_closing", False):
            return
        self.pause_playback(invalidate=True)
        if self.current_packet is None:
            self.canvas.clear_frame("Unable to open this recording")
        self._show_error("FLIR decoding error", message)

    def _on_open_failed(self, open_id: int, message: str) -> None:
        if open_id != self._open_request_id:
            return  # failure of an open the user has already replaced
        self._on_decode_failed(message)

    def _on_state_failed(self, request_id: int, message: str, state: dict | None) -> None:
        """Report a rejected state change and put the controls back.

        A rejection that a newer change has already superseded leaves the
        controls alone: they describe that newer change, whose own result (or
        rejection) follows. Otherwise the controls go back to the state in
        force and the frame is fetched again under it: an earlier change may
        have succeeded while its frame was superseded by the rejected one.
        """
        if getattr(self, "_closing", False):
            return
        self._show_error("FLIR decoding error", message)
        if state is None or request_id != self._state_request_id:
            return
        self._restore_state(state)
        if self.current_packet is not None:
            refresh = self._activate_request()
            self.decoder.request_frame(
                self.current_packet.index,
                refresh,
                need_clip=self.show_clipping,
                need_metadata=self._metadata_tab_active(),
            )

    def _restore_state(self, state: dict) -> None:
        """Show the measurement/processing state in force in the controls."""
        self.inspector.select_unit(state["unit"])
        self.inspector.set_object_parameters(state["object_parameters"])
        self.inspector.set_corrections(state["corrections"])
        self._reference_params = state["reference"]
        self.inspector.set_reference_label(state["reference_label"])
        self._filters_state = {key: tuple(value) for key, value in state["processing"].items()}
        self.inspector.set_processing(self._filters_state)

    def _export(self, kind: str) -> None:
        if self.current_packet is None or self.metadata is None:
            return
        self.pause_playback(invalidate=True)
        packet, metadata, image = self.current_packet, self.metadata, self.canvas.image.copy()
        suffix_map = {"png": ".png", "npy": ".npy", "csv": ".csv"}
        filter_map = {
            "png": "PNG image (*.png)",
            "npy": "NumPy array (*.npy)",
            "csv": "CSV table (*.csv)",
        }
        extension = suffix_map[kind]
        default_name = unique_destination(
            metadata.path.parent
            / f"{metadata.path.stem}_frame_{packet.index + 1:05d}{extension}"
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
        replace = confirm_replace(self, [output], confirmed=[path])  # Save dialog asked already
        if replace is None:
            return
        try:
            with OutputTransaction([metadata.path], replace=replace) as job:
                stage = job.stage(output)
                if kind == "png":
                    if image.isNull() or not image.save(str(stage), "PNG"):
                        raise OSError("Qt could not write the PNG image")
                elif kind == "npy":
                    np.save(stage, packet.data)
                else:
                    # Round-trip exact: %.6f kept only ~4 digits of radiance.
                    kind_code = packet.data.dtype.kind
                    fmt = "%d" if kind_code in "iu" else (
                        "%.17g" if packet.data.dtype == np.float64 else "%.9g")
                    np.savetxt(stage, packet.data, delimiter=",", fmt=fmt)
                job.commit()
            self._notify(f"Saved {output.name}")
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

    def _activate_state_request(self) -> int:
        """Request id for a unit/parameter/correction/reference/filter change;
        a rejection only resets the controls if no newer change was sent."""
        request_id = self._activate_request()
        self._state_request_id = request_id
        return request_id

    def _next_request_id(self) -> int:
        self._request_sequence += 1
        return self._request_sequence

    def _show_error(self, title: str, message: str) -> None:
        # Owned by the player window: centred over it, and never by a job's
        # progress dialog, which the job's completion deletes (taking an
        # unread message with it).
        if getattr(self, "_closing", False):
            return  # quitting: a job failing on its way out needs no modal dialog
        MessageDialog.critical(self, title, message)

    def _notify(self, text: str) -> None:
        """Visible confirmation of a finished action (saved file, loaded ROIs)."""
        self.canvas.show_notice(text)

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
            self._place_resize_grips()
        super().changeEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_resize_grips()

    def _place_resize_grips(self) -> None:
        resizable = not (self.isMaximized() or self.isFullScreen())
        for grip in getattr(self, "_resize_grips", ()):
            grip.place()
            grip.setVisible(resizable)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.pause_playback(invalidate=True)
        if self.decoder.isRunning():
            event.ignore()
            if not getattr(self, "_closing", False):
                self._closing = True
                _CLOSING_WINDOWS.add(self)
                self.setEnabled(False)
                self.title_bar.set_filename("Closing — waiting for the current operation…")
                self.decoder.finished.connect(self.close)
                self.decoder.shutdown()
            return
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        _CLOSING_WINDOWS.discard(self)
        event.accept()
