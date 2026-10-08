# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
import json
from .jobs import CancellationToken, JobCancelled, OutputTransaction, extract_recording, unique_destination
from queue import Queue
from typing import Any

try:  # The FLIR File SDK is a proprietary, user-supplied optional dependency.
    import fnv.file
except ModuleNotFoundError:  # Let this module import without the SDK present.
    fnv = None  # type: ignore[assignment]
from PySide6.QtCore import QThread, Signal

from .compose import compose_frame
from .export import (
    MovieWriter,
    StatsCsvWriter,
    frame_burn_label,
    save_frame,
    series_frame_path,
    series_stats_path,
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
        self._packets: OrderedDict[tuple, FramePacket] = OrderedDict()
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
        key: tuple,
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

    def put(self, key: tuple, packet: FramePacket) -> None:
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
    rgb, low, high, mapping = render_frame_rgb(
        packet.data, display, packet.clip_mask, extrema=(packet.minimum, packet.maximum), return_mapping=True
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
        mapping=mapping, segmentation=display.segmentation, isotherm=display.isotherm,
    )
    return packet, rgb, composed, (low, high)


class DecoderThread(QThread):
    """Owns File SDK state and performs all decoding away from the UI thread."""

    _STATE_COMMANDS = frozenset({"unit", "params", "corrections", "reference", "filters"})

    opened = Signal(object, object, object)
    frame_ready = Signal(object)
    unit_ready = Signal(object, object)
    object_params_ready = Signal(object)
    corrections_ready = Signal(object)
    reference_ready = Signal(str)
    extract_progress = Signal(int, int)
    extract_finished = Signal(bool, str)
    export_progress = Signal(int, int)
    export_stage = Signal(str)  # what a long job is doing now, for its progress label
    export_finished = Signal(bool, str)
    # TC calibration: ok, message, the tcmatch.RunOutput (None on failure);
    # the job owns the export progress dialog like the exports.
    tc_finished = Signal(bool, str, object)
    # Bitmask exports have no progress dialog, so they report separately: a
    # queued one must never close the dialog of an export started after it.
    bitmasks_finished = Signal(bool, str)
    busy_changed = Signal(bool, str)
    failed = Signal(str)
    open_failed = Signal(int, str)  # open generation, message
    # A rejected state change: its request id, the message, and the
    # measurement/processing state still in force (None if unavailable), so
    # the GUI can put its controls back unless a newer change superseded it.
    state_failed = Signal(int, str, object)

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
        self._tokens: list[CancellationToken] = []

    def request_open(self, path: str, open_id: int = 0) -> None:
        """Open a recording; ``open_id`` tags the opened packet and failures so
        the GUI can ignore an open that a later one superseded."""
        self._clear_pending_commands()
        self._commands.put(("open", (path, int(open_id))))

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
        """Latest-wins like frame reads: each filter state replaces the whole
        point/spatial/temporal state, so a pending one that a newer state
        supersedes would only cost a full (temporal warm-up) recompute."""
        payload = (dict(state), int(index), int(request_id))
        with self._commands.not_empty:
            queue = self._commands.queue
            kept = [entry for entry in queue if entry[0] != "filters"]
            kept.append(("filters", payload))
            queue.clear()
            queue.extend(kept)
            self._commands.not_empty.notify()

    def request_extract(self, params: dict) -> None:
        token = CancellationToken()
        self._tokens.append(token)
        self._commands.put(("extract", dict(params, _token=token)))

    def request_export_sequence(self, params: dict) -> None:
        token = CancellationToken()
        self._tokens.append(token)
        self._commands.put(("export_sequence", dict(params, _token=token)))

    def request_batch_extract(self, params: dict) -> None:
        token = CancellationToken()
        self._tokens.append(token)
        self._commands.put(("batch_extract", dict(params, _token=token)))

    def request_export_excel(self, params: dict) -> None:
        token = CancellationToken()
        self._tokens.append(token)
        self._commands.put(("export_excel", dict(params, _token=token)))

    def request_tc_match(self, params: dict) -> None:
        """Find the TC pixels in the open recording and fit the emissivity (``tcmatch``)."""
        token = CancellationToken()
        self._tokens.append(token)
        self._commands.put(("tc_match", dict(params, _token=token)))

    def request_tc_workbook(self, params: dict) -> None:
        """Excel workbook of a TC calibration result (ROIs at the TC pixels, TC data filled in)."""
        token = CancellationToken()
        self._tokens.append(token)
        self._commands.put(("tc_workbook", dict(params, _token=token)))

    def request_export_bitmasks(self, folder: str, overwrite=()) -> None:
        """``overwrite``: existing bitmask files the user agreed to replace."""
        self._commands.put(("export_bitmasks", {"folder": str(folder),
                                                "replace": [str(path) for path in overwrite]}))

    def cancel_extract(self) -> None:
        """Abort the running extract/export operation."""
        self._abort = True
        for token in tuple(self._tokens):
            token.cancel()

    def shutdown(self) -> None:
        self.cancel_extract()
        self.requestInterruption()
        self._clear_pending_commands()
        self._commands.put(("stop", None))

    def run(self) -> None:
        source = FlirVideoSource()
        cache = _PacketCache(self._cache_budget, self._cache_size)
        try:
            while True:
                command, payload = self._commands.get()
                if command == "stop" or self.isInterruptionRequested():
                    break
                try:
                    if command == "open":
                        path, open_id = payload
                        self.busy_changed.emit(True, "Opening recording…")
                        source.close()
                        cache.clear()
                        metadata = source.open(path)
                        packet = source.take_first_packet()
                        if packet is None:
                            packet = source.read_frame(0, request_id=0)
                        cache.put((packet.revision, packet.unit.key, 0), replace(packet, request_id=0))
                        self.opened.emit(metadata, source.available_units,
                                         replace(packet, request_id=open_id))
                        self.object_params_ready.emit(source.read_object_parameters())
                        self.corrections_ready.emit(source.read_corrections())
                        self.busy_changed.emit(False, "")
                    elif command == "frame":
                        index, request_id, need_clip, need_metadata = payload
                        key = (source.revision, source.unit.key, index)
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
                        cache.put((packet.revision, packet.unit.key, packet.index), replace(packet, request_id=0))
                        self.object_params_ready.emit(snapshot)
                        self.frame_ready.emit(packet)
                        self.busy_changed.emit(False, "")
                    elif command == "corrections":
                        nuc, bp, index, request_id = payload
                        self.busy_changed.emit(True, "Updating corrections…")
                        state = source.set_corrections(nuc, bp)
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache.put((packet.revision, packet.unit.key, packet.index), replace(packet, request_id=0))
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
                        cache.put((packet.revision, packet.unit.key, packet.index), replace(packet, request_id=0))
                        self.reference_ready.emit(label)
                        self.frame_ready.emit(packet)
                        self.busy_changed.emit(False, "")
                    elif command == "filters":
                        state_dict, index, request_id = payload
                        self.busy_changed.emit(True, "Updating filters…")
                        source.set_processing(state_from_dict(state_dict))
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache.put((packet.revision, packet.unit.key, packet.index), replace(packet, request_id=0))
                        self.frame_ready.emit(packet)
                        self.busy_changed.emit(False, "")
                    elif command == "rois":
                        shapes, index, request_id = payload
                        source.set_rois(shapes)
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache.put((packet.revision, packet.unit.key, packet.index), replace(packet, request_id=0))
                        self.frame_ready.emit(packet)
                    elif command == "extract":
                        self._abort = payload["_token"]()
                        self.busy_changed.emit(True, "Extracting clip…")
                        ok, message = source.extract(
                            payload["dest"],
                            payload["start_frame"],
                            payload["end_frame"],
                            payload.get("decimation", 1),
                            progress=lambda cur, total: self.extract_progress.emit(cur, total),
                            abort=payload["_token"],
                        )
                        self.extract_finished.emit(ok, message)
                        self.busy_changed.emit(False, "")
                    elif command == "export_sequence":
                        self._abort = payload["_token"]()
                        self.busy_changed.emit(True, "Exporting…")
                        ok, message = self._run_export_sequence(source, payload)
                        self.export_finished.emit(ok, message)
                        self.busy_changed.emit(False, "")
                    elif command == "batch_extract":
                        self._abort = payload["_token"]()
                        self.busy_changed.emit(True, "Batch extracting…")
                        ok, message = self._run_batch_extract(payload)
                        self.export_finished.emit(ok, message)
                        self.busy_changed.emit(False, "")
                    elif command == "export_excel":
                        self._abort = payload["_token"]()
                        self.busy_changed.emit(True, "Exporting Excel workbook…")
                        ok, message = self._run_export_excel(source, payload)
                        self.export_finished.emit(ok, message)
                        self.busy_changed.emit(False, "")
                    elif command == "tc_match":
                        self._abort = payload["_token"]()
                        self.busy_changed.emit(True, "Fitting the emissivity from TCs…")
                        ok, message, output = self._run_tc_match(source, payload)
                        self.tc_finished.emit(ok, message, output)
                        self.busy_changed.emit(False, "")
                    elif command == "tc_workbook":
                        self._abort = payload["_token"]()
                        self.busy_changed.emit(True, "Exporting Excel workbook…")
                        ok, message = self._run_tc_workbook(source, payload)
                        self.export_finished.emit(ok, message)
                        self.busy_changed.emit(False, "")
                    elif command == "export_bitmasks":
                        self.busy_changed.emit(True, "Exporting ROI bitmasks…")
                        written = source.export_roi_bitmasks(
                            payload["folder"], overwrite=payload.get("replace", ()))
                        self.bitmasks_finished.emit(
                            True, f"Wrote {len(written)} bitmask file(s)"
                        )
                        self.busy_changed.emit(False, "")
                    elif command == "unit":
                        key, index, request_id = payload
                        self.busy_changed.emit(True, "Updating thermal unit…")
                        option = source.set_unit(key)
                        cache.clear()
                        packet = source.read_frame(index, request_id=request_id)
                        cache.put((packet.revision, option.key, packet.index), replace(packet, request_id=0))
                        self.unit_ready.emit(option, packet)
                        self.object_params_ready.emit(source.read_object_parameters())
                        self.busy_changed.emit(False, "")
                except Exception as exc:
                    self.busy_changed.emit(False, "")
                    message = f"{type(exc).__name__}: {exc}"
                    if command == "extract":
                        self.extract_finished.emit(False, message)
                    elif command in {"export_sequence", "batch_extract", "export_excel", "tc_workbook"}:
                        self.export_finished.emit(False, message)
                    elif command == "tc_match":
                        self.tc_finished.emit(False, message, None)
                    elif command == "export_bitmasks":
                        self.bitmasks_finished.emit(False, message)
                    elif command == "open":
                        self.open_failed.emit(payload[1], message)
                    elif command in self._STATE_COMMANDS:
                        state = None
                        if source.is_open:
                            try:
                                state = source.state_snapshot()
                            except Exception:
                                pass  # the failure itself is still reported
                        self.state_failed.emit(payload[-1], message, state)  # request id is last
                    else:
                        self.failed.emit(message)
                finally:
                    if isinstance(payload, dict) and payload.get("_token") in self._tokens:
                        self._tokens.remove(payload["_token"])
        finally:
            source.close()

    def _job_aborted(self, payload):
        token = payload.get("_token")
        return token() if token is not None else self._abort

    def _run_export_sequence(self, source: FlirVideoSource, payload: dict) -> tuple[bool, str]:
        """Stage a complete job; decimation selects outputs, not filter inputs."""
        start, end = int(payload["start_frame"]), int(payload["end_frame"])
        step = max(1, int(payload["decimation"]))
        frame_range = range(start, end + 1, step)
        total = len(frame_range)
        if start < 0 or end >= source.metadata.num_frames or total <= 0:
            return False, "Invalid or empty frame range"
        if payload.get("revision", source.revision) != source.revision:
            return False, "Analysis settings changed; start the export again"
        is_movie = payload["kind"] == "movie"
        writer = stats_writer = None
        dest = Path(payload["dest"])
        old_ring, old_index = source._temporal, source._last_frame_index
        from .processing import TemporalBuffer
        source._temporal, source._last_frame_index = TemporalBuffer(), None
        try:
            # Only outputs the export dialog confirmed replacing may be replaced.
            with OutputTransaction([source.metadata.path], lambda: self._job_aborted(payload),
                                   replace=payload.get("replace", ())) as job:
                paths = {}
                if is_movie:
                    stage = job.stage(dest)
                    writer = MovieWriter(stage, payload["fps"], payload["fmt"])
                else:
                    base = payload["base_name"]
                    if not base or Path(base).name != base or any(c in base for c in '/\\:'):
                        raise ValueError("Base name must be a filename, without a directory")
                    # Preflight the entire map before writing the first frame.
                    for index in frame_range:
                        paths[index] = job.stage(series_frame_path(dest, base, index, payload["fmt"]))
                    if payload.get("stats_csv"):
                        stage = job.stage(series_stats_path(dest, base))
                        stats_writer = StatsCsvWriter(stage, stats_csv_header([r.name for r in payload["rois"]]))
                temporal = source.processing.temporal[0] != "none"
                inputs = range(start, end + 1) if temporal else frame_range
                count = 0
                try:
                    for index in inputs:
                        job.check_cancelled()
                        if (index - start) % step:
                            source.read_frame(index, need_clip=False, need_metadata=False)
                            continue
                        packet, rgb, composed, scale = _render_export_frame(source, payload, index)
                        job.check_cancelled()
                        if writer is not None:
                            writer.append(composed)
                        else:
                            save_frame(paths[index], payload["fmt"], composed, packet.data, scale,
                                       packet=packet, source=source.metadata)
                            if stats_writer is not None:
                                stats_writer.append(stats_csv_row(packet, packet.unit.label,
                                    rois=payload["rois"], scale=scale, fmt=payload["fmt"]))
                        count += 1
                        self.export_progress.emit(count, total)
                finally:
                    try:
                        if writer is not None:
                            writer.close()
                    finally:
                        if stats_writer is not None:
                            stats_writer.close()
                job.commit()
            return True, ""
        except JobCancelled:
            return False, "Export cancelled"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            source._temporal, source._last_frame_index = old_ring, old_index

    def _run_export_excel(self, source: FlirVideoSource, payload: dict) -> tuple[bool, str]:
        """Excel workbook of raw counts; the open recording's handle is lent.

        Other recordings are opened (and closed) by the job. The lent handle
        gets its unit, scale and object parameters back; the player's own
        processing state is untouched because raw counts bypass it.
        """
        from . import __version__
        from .excel_export import SourceSpec, run_export

        if not source.is_open:
            return False, "No recording is open"
        current = Path(source.metadata.path).resolve()
        specs = []
        for entry in payload["sources"]:
            path = Path(entry["path"]).resolve()
            parameters = None
            if entry.get("current") and payload.get("parameters_from") == "player":
                parameters = source.read_object_parameters()
            specs.append(SourceSpec(path=path, rois=tuple(entry["rois"]),
                                    ignition_frame=int(entry["ignition_frame"]),
                                    label=str(entry.get("label", "")), parameters=parameters))
        try:
            message = run_export(
                payload["dest"], specs, payload["options"],
                handles={current: source.sdk_handle()},
                progress=lambda done, total: self.export_progress.emit(int(done), int(total)),
                abort=lambda: self._job_aborted(payload),
                replace=payload.get("replace", ()), tool_version=__version__)
            return True, message
        except JobCancelled:
            return False, "Export cancelled"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _lent_handle(source: FlirVideoSource, payload: dict) -> Any | None:
        """The open recording's handle for a TC job, or None to let the job open its own.

        As in the Excel export: a user-calibrated recording under a File SDK
        with the unit-change bug keeps the player's handle out of the job.
        """
        from .calibration import user_calibration_risk

        if not source.is_open:
            raise RuntimeError("No recording is open")
        if Path(payload["recording"]).resolve() != Path(source.metadata.path).resolve():
            raise RuntimeError("Another recording was opened; start again")
        im = source.sdk_handle()
        return None if im is None or user_calibration_risk(im) else im

    def _run_tc_match(self, source: FlirVideoSource, payload: dict) -> tuple[bool, str, Any]:
        """TC calibration of the open recording (``tcmatch.run_analysis``); its handle is lent."""
        from .tcmatch import run_analysis

        try:
            im = self._lent_handle(source, payload)
            parameters = source.read_object_parameters() if payload.get("parameters_from") == "player" else None
            output = run_analysis(
                payload["recording"], payload["tc_file"], payload["options"], out_dir=payload.get("out_dir"),
                cache_dir=payload.get("cache_dir"), sheet=payload.get("sheet"), im=im, parameters=parameters,
                replace=payload.get("replace", ()),
                progress=lambda done, total: self.export_progress.emit(int(done), int(total)),
                abort=lambda: self._job_aborted(payload), stage=self.export_stage.emit)
            return True, "", output
        except JobCancelled:
            return False, "Cancelled", None
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}", None

    def _run_tc_workbook(self, source: FlirVideoSource, payload: dict) -> tuple[bool, str]:
        """Workbook of a TC calibration result (``tcmatch.write_workbook``); the handle is lent."""
        from . import __version__
        from .tcmatch import write_workbook

        try:
            im = self._lent_handle(source, payload)
            key = Path(payload["recording"]).resolve()
            message = write_workbook(
                payload["dest"], payload["recording"], payload["result"], payload["table"],
                handles={key: im} if im is not None else None, parameters=payload.get("parameters"),
                progress=lambda done, total: self.export_progress.emit(int(done), int(total)),
                abort=lambda: self._job_aborted(payload), replace=payload.get("replace", ()),
                tool_version=__version__, protect=[payload["tc_file"]] if payload.get("tc_file") else ())
            return True, message
        except JobCancelled:
            return False, "Export cancelled"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def _run_batch_extract(self, payload: dict) -> tuple[bool, str]:
        """Preflight unique destinations and persist an outcome for every input."""
        folder = Path(payload["folder"])
        files = [Path(path).expanduser().resolve() for path in payload["files"]]
        folder.mkdir(parents=True, exist_ok=True)
        reserved = list(files)
        outcomes = []
        for path in files:
            dest = unique_destination(folder / f"{path.stem}_extract.ats", reserved)
            reserved.append(dest)
            outcomes.append({"source": str(path), "destination": str(dest),
                             "status": "not_started", "message": ""})
        for count, result in enumerate(outcomes, 1):
            if self._job_aborted(payload):
                result["status"] = "cancelled"
                continue
            im = None
            try:
                im = fnv.file.ImagerFile(result["source"])
                options = fnv.file.ImagerFileExtractOptions()
                options.start_frame = 0
                options.end_frame = int(im.num_frames) - 1
                options.decimation = max(1, int(payload["decimation"]))
                ok, message = extract_recording(im, result["source"], result["destination"], options,
                    progress=lambda cur, total: self.export_progress.emit(
                        (count - 1) * 1000 + int(1000 * cur / max(1, total)), len(files) * 1000),
                    abort=lambda: self._job_aborted(payload))
                result["status"] = "succeeded" if ok else (
                    "cancelled" if self._job_aborted(payload) or message == "Extraction cancelled" else "failed")
                result["message"] = message
            except Exception as exc:
                result["status"] = "failed"
                result["message"] = f"{type(exc).__name__}: {exc}"
            finally:
                if im is not None:
                    im.close()
        report = unique_destination(folder / "batch_extract_report.json", reserved)
        with OutputTransaction(files) as job:
            job.stage(report).write_text(json.dumps(outcomes, indent=2), encoding="utf-8")
            job.commit()
        succeeded = sum(r["status"] == "succeeded" for r in outcomes)
        cancelled = self._job_aborted(payload) or any(r["status"] == "cancelled" for r in outcomes)
        prefix = "Cancelled; extracted" if cancelled else "Extracted"
        details = "; ".join(f"{Path(r['source']).name}: {r['status']}" for r in outcomes if r["status"] != "succeeded")
        summary = f"{prefix} {succeeded} of {len(files)} recording(s)."
        if details:
            summary += f" {details}."
        return (not cancelled and succeeded == len(files), f"{summary} Report: {report}")

    def _clear_pending_commands(self) -> None:
        dropped = []
        with self._commands.mutex:
            for command, payload in self._commands.queue:
                if isinstance(payload, dict) and payload.get("_token") in self._tokens:
                    token = payload["_token"]
                    token.cancel()
                    self._tokens.remove(token)
                    dropped.append(command)
            self._commands.queue.clear()
        # A job dropped before it started still reports, so its progress dialog closes
        # (emitted outside the queue lock: the handlers run on the caller's thread).
        for command in dropped:
            if command == "extract":
                self.extract_finished.emit(False, "Extraction cancelled")
            elif command == "tc_match":
                self.tc_finished.emit(False, "Cancelled", None)
            elif command in {"export_sequence", "batch_extract", "export_excel", "tc_workbook"}:
                self.export_finished.emit(False, "Export cancelled")
