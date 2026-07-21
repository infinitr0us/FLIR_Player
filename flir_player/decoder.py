# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from queue import Queue
from typing import Any

try:  # The FLIR File SDK is a proprietary, user-supplied optional dependency.
    import fnv.file
except ModuleNotFoundError:  # Let this module import without the SDK present.
    fnv = None  # type: ignore[assignment]
from PySide6.QtCore import QThread, Signal

from .compose import compose_frame
from .export import (
    EXTENSIONS,
    MovieWriter,
    frame_burn_label,
    save_frame,
    stats_csv_header,
    stats_csv_row,
    write_stats_csv,
)
from .models import FramePacket
from .processing import state_from_dict
from .render import render_frame_rgb
from .source import FlirVideoSource


def _render_export_frame(source: FlirVideoSource, payload: dict, index: int):
    """Decode one frame and compose it exactly like the live view (WYSIWYG)."""
    packet = source.read_frame(index)
    display = payload["display"]
    if display.range_mode == "roi" and payload.get("selected_roi_id") is not None:
        stats = next(
            (s for s in packet.roi_stats if s.id == payload["selected_roi_id"]), None
        )
        display = replace(
            display,
            roi_minmax=(stats.minimum, stats.maximum) if stats is not None else None,
        )
    rgb, low, high = render_frame_rgb(packet.data, display, packet.clip_mask)
    options = payload["options"]
    label = (
        frame_burn_label(packet, source.metadata) if options.timestamp else ""
    )
    composed = compose_frame(
        rgb,
        options,
        palette=display.palette,
        inverted=display.inverted,
        scale=(low, high),
        suffix=payload["suffix"],
        rois=payload["rois"],
        roi_stats=packet.roi_stats,
        min_position=packet.min_position,
        max_position=packet.max_position,
        label=label,
        flips=(display.flip_h, display.flip_v),
    )
    return packet, rgb, composed, (low, high)


class DecoderThread(QThread):
    """Owns File SDK state and performs all decoding away from the UI thread."""

    opened = Signal(object, object, object)
    frame_ready = Signal(object)
    unit_ready = Signal(object, object)
    object_params_ready = Signal(object)
    corrections_ready = Signal(object)
    reference_ready = Signal(str)
    extract_progress = Signal(int, int)
    extract_finished = Signal(bool, str)
    export_progress = Signal(int, int)
    export_finished = Signal(bool, str)
    busy_changed = Signal(bool, str)
    failed = Signal(str)

    def __init__(self, parent=None, cache_size: int = 8) -> None:
        super().__init__(parent)
        self._commands: Queue[tuple[str, Any]] = Queue()
        self._cache_size = max(1, int(cache_size))
        self._abort = False

    def request_open(self, path: str) -> None:
        self._clear_pending_commands()
        self._commands.put(("open", path))

    def request_frame(self, index: int, request_id: int) -> None:
        self._commands.put(("frame", (int(index), int(request_id))))

    def request_unit(self, key: str, index: int, request_id: int) -> None:
        self._commands.put(("unit", (key, int(index), int(request_id))))

    def request_object_params(
        self, values: dict[str, float] | None, index: int, request_id: int
    ) -> None:
        self._commands.put(("params", (values, int(index), int(request_id))))

    def request_rois(self, shapes: tuple, index: int, request_id: int) -> None:
        self._commands.put(("rois", (tuple(shapes), int(index), int(request_id))))

    def request_corrections(self, nuc: bool, bp: bool, index: int, request_id: int) -> None:
        self._commands.put(("corrections", (bool(nuc), bool(bp), int(index), int(request_id))))

    def request_reference(self, params: dict | None, index: int, request_id: int) -> None:
        payload = None if params is None else dict(params)
        self._commands.put(("reference", (payload, int(index), int(request_id))))

    def request_filters(self, state: dict, index: int, request_id: int) -> None:
        self._commands.put(("filters", (dict(state), int(index), int(request_id))))

    def request_extract(self, params: dict) -> None:
        self._commands.put(("extract", dict(params)))

    def request_export_sequence(self, params: dict) -> None:
        self._commands.put(("export_sequence", dict(params)))

    def request_batch_extract(self, params: dict) -> None:
        self._commands.put(("batch_extract", dict(params)))

    def request_export_bitmasks(self, folder: str) -> None:
        self._commands.put(("export_bitmasks", {"folder": str(folder)}))

    def cancel_extract(self) -> None:
        """Abort the running extract/export operation."""
        self._abort = True

    def shutdown(self) -> None:
        self._clear_pending_commands()
        self._commands.put(("stop", None))

    def run(self) -> None:
        source = FlirVideoSource()
        cache: OrderedDict[tuple[str, int], FramePacket] = OrderedDict()
        try:
            while True:
                command, payload = self._commands.get()
                if command == "stop":
                    break
                try:
                    if command == "open":
                        self.busy_changed.emit(True, "Opening recording…")
                        source.close()
                        cache.clear()
                        metadata = source.open(payload)
                        packet = source.read_frame(0, request_id=0)
                        cache[(packet.unit.key, 0)] = packet
                        self.opened.emit(metadata, source.available_units, packet)
                        self.object_params_ready.emit(source.read_object_parameters())
                        self.corrections_ready.emit(source.read_corrections())
                        self.busy_changed.emit(False, "")
                    elif command == "frame":
                        index, request_id = payload
                        key = (source.unit.key, index)
                        packet = cache.get(key)
                        if packet is None:
                            packet = source.read_frame(index, request_id=request_id)
                            cache[key] = replace(packet, request_id=0)
                            cache.move_to_end(key)
                            while len(cache) > self._cache_size:
                                cache.popitem(last=False)
                        self.frame_ready.emit(replace(packet, request_id=request_id))
                    elif command == "params":
                        values, index, request_id = payload
                        self.busy_changed.emit(True, "Applying object parameters…")
                        snapshot = source.apply_object_parameters(values)
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache[(packet.unit.key, packet.index)] = replace(packet, request_id=0)
                        self.object_params_ready.emit(snapshot)
                        self.frame_ready.emit(packet)
                        self.busy_changed.emit(False, "")
                    elif command == "corrections":
                        nuc, bp, index, request_id = payload
                        self.busy_changed.emit(True, "Updating corrections…")
                        state = source.set_corrections(nuc, bp)
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache[(packet.unit.key, packet.index)] = replace(packet, request_id=0)
                        self.corrections_ready.emit(state)
                        self.frame_ready.emit(packet)
                        self.busy_changed.emit(False, "")
                    elif command == "reference":
                        params, index, request_id = payload
                        self.busy_changed.emit(True, "Loading reference frame…")
                        if params is None:
                            source.clear_reference()
                            label = ""
                        else:
                            label = source.load_reference(
                                params["path"], params["frame_index"], params["op"]
                            )
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache[(packet.unit.key, packet.index)] = replace(packet, request_id=0)
                        self.reference_ready.emit(label)
                        self.frame_ready.emit(packet)
                        self.busy_changed.emit(False, "")
                    elif command == "filters":
                        state_dict, index, request_id = payload
                        self.busy_changed.emit(True, "Updating filters…")
                        source.set_processing(state_from_dict(state_dict))
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache[(packet.unit.key, packet.index)] = replace(packet, request_id=0)
                        self.frame_ready.emit(packet)
                        self.busy_changed.emit(False, "")
                    elif command == "rois":
                        shapes, index, request_id = payload
                        source.set_rois(shapes)
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache[(packet.unit.key, packet.index)] = replace(packet, request_id=0)
                        self.frame_ready.emit(packet)
                    elif command == "extract":
                        self._abort = False
                        self.busy_changed.emit(True, "Extracting clip…")
                        ok, message = source.extract(
                            payload["dest"],
                            payload["start_frame"],
                            payload["end_frame"],
                            payload.get("decimation", 1),
                            progress=lambda cur, total: self.extract_progress.emit(cur, total),
                            abort=lambda: self._abort,
                        )
                        self.extract_finished.emit(ok, message)
                        self.busy_changed.emit(False, "")
                    elif command == "export_sequence":
                        self._abort = False
                        self.busy_changed.emit(True, "Exporting…")
                        ok, message = self._run_export_sequence(source, payload)
                        self.export_finished.emit(ok, message)
                        self.busy_changed.emit(False, "")
                    elif command == "batch_extract":
                        self._abort = False
                        self.busy_changed.emit(True, "Batch extracting…")
                        ok, message = self._run_batch_extract(payload)
                        self.export_finished.emit(ok, message)
                        self.busy_changed.emit(False, "")
                    elif command == "export_bitmasks":
                        self.busy_changed.emit(True, "Exporting ROI bitmasks…")
                        written = source.export_roi_bitmasks(payload["folder"])
                        self.export_finished.emit(
                            True, f"Wrote {len(written)} bitmask file(s)"
                        )
                        self.busy_changed.emit(False, "")
                    elif command == "unit":
                        key, index, request_id = payload
                        self.busy_changed.emit(True, "Updating thermal unit…")
                        option = source.set_unit(key)
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache[(option.key, packet.index)] = replace(packet, request_id=0)
                        self.unit_ready.emit(option, packet)
                        self.busy_changed.emit(False, "")
                except Exception as exc:
                    self.busy_changed.emit(False, "")
                    self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            source.close()

    def _run_export_sequence(self, source: FlirVideoSource, payload: dict) -> tuple[bool, str]:
        """Movie or numbered-series export loop (§4.9.1.1, p. 59–60)."""
        start = payload["start_frame"]
        end = payload["end_frame"]
        step = max(1, payload["decimation"])
        indices = list(range(start, end + 1, step))
        if not indices:
            return (False, "The frame range is empty")
        total = len(indices)
        is_movie = payload["kind"] == "movie"
        writer = None
        stats_rows: list[list] = []
        dest_path = Path(payload["dest"])
        try:
            if is_movie:
                writer = MovieWriter(dest_path, payload["fps"], payload["fmt"])
            else:
                dest_path.mkdir(parents=True, exist_ok=True)
            for count, index in enumerate(indices, 1):
                if self._abort:
                    if writer is not None:
                        writer.close()
                        dest_path.unlink(missing_ok=True)
                    return (False, "Export cancelled")
                packet, rgb, composed, scale = _render_export_frame(source, payload, index)
                if writer is not None:
                    writer.append(composed)
                else:
                    frame_path = dest_path / (
                        f"{payload['base_name']}_{index + 1:05d}{EXTENSIONS[payload['fmt']]}"
                    )
                    save_frame(frame_path, payload["fmt"], composed, packet.data, scale)
                    if payload["stats_csv"]:
                        stats_rows.append(stats_csv_row(packet, payload["unit_label"]))
                self.export_progress.emit(count, total)
            if writer is not None:
                writer.close()
                writer = None
            if stats_rows:
                roi_names = [shape.name for shape in payload["rois"]]
                write_stats_csv(
                    dest_path / f"{payload['base_name']}_stats.csv",
                    stats_csv_header(roi_names),
                    stats_rows,
                )
            return (True, "")
        except Exception as exc:
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
            return (False, f"{type(exc).__name__}: {exc}")

    def _run_batch_extract(self, payload: dict) -> tuple[bool, str]:
        """Extract a list of ATS recordings via temporary SDK handles (§4.9.1.3)."""
        folder = Path(payload["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        files = payload["files"]
        produced: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        for count, path in enumerate(files, 1):
            if self._abort:
                return (False, f"Cancelled after {count - 1} of {len(files)} files")
            source_path = Path(path)
            dest = folder / f"{source_path.stem}_extract.ats"
            try:
                im = fnv.file.ImagerFile(str(source_path))
                try:
                    options = fnv.file.ImagerFileExtractOptions()
                    options.start_frame = 0
                    options.end_frame = int(im.num_frames) - 1
                    options.decimation = max(1, int(payload["decimation"]))
                    ok = bool(im.extract(str(dest), options))
                finally:
                    im.close()
                if ok and dest.is_file():
                    produced.append(source_path.name)
                else:
                    skipped.append(source_path.name)
                    dest.unlink(missing_ok=True)
            except Exception as exc:
                failed.append(f"{source_path.name} ({type(exc).__name__})")
                dest.unlink(missing_ok=True)
            self.export_progress.emit(count, len(files))
        parts = [f"Extracted {len(produced)} of {len(files)} recording(s)"]
        if skipped:
            parts.append("no output (ATS sources only): " + ", ".join(skipped))
        if failed:
            parts.append("failed: " + ", ".join(failed))
        return (not failed, "; ".join(parts))

    def _clear_pending_commands(self) -> None:
        with self._commands.mutex:
            self._commands.queue.clear()
