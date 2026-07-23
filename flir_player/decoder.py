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
    StatsCsvWriter,
    frame_burn_label,
    save_frame,
    stats_csv_header,
    stats_csv_row,
)
from .models import FramePacket
from .processing import state_from_dict
from .render import display_scale, render_frame_rgb
from .source import FlirVideoSource


class _PacketCache:
    """Byte-budgeted LRU for decoded packets.

    Bounded by total payload bytes (with a secondary count cap) instead of a
    fixed frame count, so memory stays predictable across resolutions and
    unit dtypes.
    """

    def __init__(self, budget_bytes: int, max_count: int) -> None:
        self._budget = max(1, int(budget_bytes))
        self._max_count = max(1, int(max_count))
        self._packets: OrderedDict[tuple[str, int], FramePacket] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @staticmethod
    def _size(packet: FramePacket) -> int:
        size = int(packet.data.nbytes)
        if packet.clip_mask is not None:
            size += int(packet.clip_mask.nbytes)
        return size

    def get(
        self,
        key: tuple[str, int],
        need_clip: bool = False,
        need_metadata: bool = False,
    ) -> FramePacket | None:
        """Fetch a cached packet that satisfies the request's payload needs.

        ``clip_mask is None`` / empty metadata entries are valid full results
        (nothing clipped / no frame metadata), not proof of a lean packet —
        sufficiency is judged by the packet's ``clip_loaded`` /
        ``metadata_loaded`` flags. A hit is counted only when the entry
        actually satisfies the request.
        """
        packet = self._packets.get(key)
        if packet is None:
            self.misses += 1
            return None
        if (need_clip and not packet.clip_loaded) or (
            need_metadata and not packet.metadata_loaded
        ):
            self.misses += 1
            return None  # cached copy is leaner than requested
        self.hits += 1
        self._packets.move_to_end(key)
        return packet

    def put(self, key: tuple[str, int], packet: FramePacket) -> None:
        old = self._packets.pop(key, None)
        if old is not None:
            self._bytes -= self._size(old)
        self._packets[key] = packet
        self._bytes += self._size(packet)
        while len(self._packets) > 1 and (
            self._bytes > self._budget or len(self._packets) > self._max_count
        ):
            _, evicted = self._packets.popitem(last=False)
            self._bytes -= self._size(evicted)
            self.evictions += 1

    def clear(self) -> None:
        self._packets.clear()
        self._bytes = 0

    @property
    def bytes_used(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._packets)


def _render_export_frame(source: FlirVideoSource, payload: dict, index: int):
    """Decode one frame and compose it exactly like the live view (WYSIWYG).

    Raw TIFF series bypass RGB render/composition entirely: save_frame writes
    radiometric data for those formats and would discard the composed RGB.
    """
    raw_tiff = payload["fmt"] in ("tiff16", "tiff_float")
    packet = source.read_frame(
        index, need_clip=not raw_tiff, need_metadata=not raw_tiff
    )
    display = payload["display"]
    if display.range_mode == "roi" and payload.get("selected_roi_id") is not None:
        stats = next(
            (s for s in packet.roi_stats if s.id == payload["selected_roi_id"]), None
        )
        display = replace(
            display,
            roi_minmax=(stats.minimum, stats.maximum) if stats is not None else None,
        )
    if raw_tiff:
        if payload["fmt"] == "tiff16":
            scale = display_scale(
                packet.data, display, extrema=(packet.minimum, packet.maximum)
            )
        else:
            scale = (0.0, 0.0)
        return packet, None, None, scale
    rgb, low, high = render_frame_rgb(
        packet.data, display, packet.clip_mask, extrema=(packet.minimum, packet.maximum)
    )
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

    def __init__(
        self,
        parent=None,
        cache_size: int = 8,
        cache_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        super().__init__(parent)
        self._commands: Queue[tuple[str, Any]] = Queue()
        self._cache_size = max(1, int(cache_size))
        self._cache_budget = max(1, int(cache_bytes))
        self._abort = False

    def request_open(self, path: str) -> None:
        self._clear_pending_commands()
        self._commands.put(("open", path))

    def request_frame(
        self,
        index: int,
        request_id: int,
        need_clip: bool = True,
        need_metadata: bool = True,
    ) -> None:
        """Latest-wins frame read: superseded pending frame reads are dropped.

        Frame reads are idempotent and the GUI discards stale request IDs, so
        coalescing pending "frame" commands cannot reorder anything; state
        changes keep strict FIFO order.
        """
        payload = (int(index), int(request_id), bool(need_clip), bool(need_metadata))
        with self._commands.not_empty:
            queue = self._commands.queue
            kept = [entry for entry in queue if entry[0] != "frame"]
            kept.append(("frame", payload))
            queue.clear()
            queue.extend(kept)
            self._commands.not_empty.notify()

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
        cache = _PacketCache(self._cache_budget, self._cache_size)
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
                        packet = source.take_first_packet()
                        if packet is None:
                            packet = source.read_frame(0, request_id=0)
                        cache.put((packet.unit.key, 0), packet)
                        self.opened.emit(metadata, source.available_units, packet)
                        self.object_params_ready.emit(source.read_object_parameters())
                        self.corrections_ready.emit(source.read_corrections())
                        self.busy_changed.emit(False, "")
                    elif command == "frame":
                        index, request_id, need_clip, need_metadata = payload
                        key = (source.unit.key, index)
                        packet = cache.get(
                            key, need_clip=need_clip, need_metadata=need_metadata
                        )
                        if packet is None:
                            packet = source.read_frame(
                                index,
                                request_id=request_id,
                                need_clip=need_clip,
                                need_metadata=need_metadata,
                            )
                            cache.put(key, replace(packet, request_id=0))
                        self.frame_ready.emit(replace(packet, request_id=request_id))
                    elif command == "params":
                        values, index, request_id = payload
                        self.busy_changed.emit(True, "Applying object parameters…")
                        snapshot = source.apply_object_parameters(values)
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache.put((packet.unit.key, packet.index), replace(packet, request_id=0))
                        self.object_params_ready.emit(snapshot)
                        self.frame_ready.emit(packet)
                        self.busy_changed.emit(False, "")
                    elif command == "corrections":
                        nuc, bp, index, request_id = payload
                        self.busy_changed.emit(True, "Updating corrections…")
                        state = source.set_corrections(nuc, bp)
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache.put((packet.unit.key, packet.index), replace(packet, request_id=0))
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
                        cache.put((packet.unit.key, packet.index), replace(packet, request_id=0))
                        self.reference_ready.emit(label)
                        self.frame_ready.emit(packet)
                        self.busy_changed.emit(False, "")
                    elif command == "filters":
                        state_dict, index, request_id = payload
                        self.busy_changed.emit(True, "Updating filters…")
                        source.set_processing(state_from_dict(state_dict))
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache.put((packet.unit.key, packet.index), replace(packet, request_id=0))
                        self.frame_ready.emit(packet)
                        self.busy_changed.emit(False, "")
                    elif command == "rois":
                        shapes, index, request_id = payload
                        source.set_rois(shapes)
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache.put((packet.unit.key, packet.index), replace(packet, request_id=0))
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
                        cache.put((option.key, packet.index), replace(packet, request_id=0))
                        self.unit_ready.emit(option, packet)
                        self.busy_changed.emit(False, "")
                except Exception as exc:
                    self.busy_changed.emit(False, "")
                    self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            source.close()

    def _run_export_sequence(self, source: FlirVideoSource, payload: dict) -> tuple[bool, str]:
        """Movie or numbered-series export loop (§4.9.1.1, p. 59–60).

        Bookkeeping is O(1): the frame range is iterated lazily and the stats
        CSV is streamed one row per frame instead of being accumulated.
        """
        start = payload["start_frame"]
        end = payload["end_frame"]
        step = max(1, payload["decimation"])
        frame_range = range(start, end + 1, step)
        total = len(frame_range)
        if total <= 0:
            return (False, "The frame range is empty")
        is_movie = payload["kind"] == "movie"
        writer = None
        stats_writer = None
        dest_path = Path(payload["dest"])
        try:
            if is_movie:
                writer = MovieWriter(dest_path, payload["fps"], payload["fmt"])
            else:
                dest_path.mkdir(parents=True, exist_ok=True)
                if payload["stats_csv"]:
                    roi_names = [shape.name for shape in payload["rois"]]
                    stats_writer = StatsCsvWriter(
                        dest_path / f"{payload['base_name']}_stats.csv",
                        stats_csv_header(roi_names),
                    )
            for count, index in enumerate(frame_range, 1):
                if self._abort:
                    if writer is not None:
                        writer.close()
                        dest_path.unlink(missing_ok=True)
                    if stats_writer is not None:
                        stats_writer.close()
                        stats_writer.path.unlink(missing_ok=True)
                    return (False, "Export cancelled")
                packet, rgb, composed, scale = _render_export_frame(source, payload, index)
                if writer is not None:
                    writer.append(composed)
                else:
                    frame_path = dest_path / (
                        f"{payload['base_name']}_{index + 1:05d}{EXTENSIONS[payload['fmt']]}"
                    )
                    save_frame(frame_path, payload["fmt"], composed, packet.data, scale)
                    if stats_writer is not None:
                        stats_writer.append(stats_csv_row(packet, payload["unit_label"]))
                self.export_progress.emit(count, total)
            if writer is not None:
                writer.close()
                writer = None
            if stats_writer is not None:
                stats_writer.close()
                stats_writer = None
            return (True, "")
        except Exception as exc:
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
            if stats_writer is not None:
                try:
                    stats_writer.close()
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
