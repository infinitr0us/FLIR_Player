# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Export writers: still images, TIFF, movie files, stats sidecars (§4.9.1.1).

All helpers are UI-thread-agnostic and safe to call from the decoder thread.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
from PIL import Image

from .models import FramePacket, VideoMetadata
from .render import format_time, frame_seconds

STILL_FORMATS: dict[str, str] = {
    "png": "PNG image (*.png)",
    "bmp": "BMP image (*.bmp)",
    "jpeg": "JPEG image (*.jpg)",
    "tiff16": "TIFF 16-bit numeric (Counts or mapped values) (*.tif)",
    "tiff_float": "TIFF 32-bit float numeric (*.tif)",
}

EXTENSIONS: dict[str, str] = {
    "png": ".png",
    "bmp": ".bmp",
    "jpeg": ".jpg",
    "tiff16": ".tif",
    "tiff_float": ".tif",
}

MOVIE_FORMATS: dict[str, str] = {
    "mp4": "MP4 video (*.mp4)",
    "wmv": "WMV video (*.wmv)",
}


def save_still(path: Path, rgb: np.ndarray, fmt: str) -> None:
    """Save a composed RGB image as PNG/BMP/JPEG."""
    image = Image.fromarray(rgb, mode="RGB")
    if fmt == "jpeg":
        image.save(str(path), "JPEG", quality=92)
    elif fmt == "bmp":
        image.save(str(path), "BMP")
    else:
        image.save(str(path), "PNG")


def numerical_encoding(data, low, high, fmt="tiff16") -> dict:
    if fmt == "tiff_float":
        return {"encoding": "float32", "scale": 1.0, "offset": 0.0}
    if data.dtype == np.uint16:
        return {"encoding": "raw_counts_uint16", "scale": 1.0, "offset": 0.0}
    finite = data[np.isfinite(data)]
    if finite.size and low == 0.0 and high == 0.0:
        low, high = float(finite.min()), float(finite.max())
    span = high - low
    if span <= 0 or not np.isfinite(span):
        span = 1.0
    return {"encoding": "display_range_uint16", "scale": span / 65535.0,
            "offset": low, "low": low, "high": low + span,
            "clipping": "outside range clipped; non-finite values stored as zero"}


def save_tiff16(path: Path, data: np.ndarray, low: float, high: float, metadata=None) -> None:
    """Raw uint16 Counts, otherwise explicitly mapped data with a TIFF mapping."""
    encoding = numerical_encoding(data, low, high)
    if encoding["encoding"] == "raw_counts_uint16":
        values = data
    else:
        normalized = (data.astype(np.float64) - encoding["offset"]) / encoding["scale"]
        normalized = np.where(np.isfinite(data), normalized, 0)
        values = np.clip(normalized + 0.5, 0, 65535).astype(np.uint16)
    Image.fromarray(values, mode="I;16").save(str(path), "TIFF",
        tiffinfo={270: json.dumps(dict(metadata or {}, **encoding))})


def save_tiff_float(path: Path, data: np.ndarray, metadata=None) -> None:
    """32-bit float TIFF in the current unit (§4.9.1.1)."""
    Image.fromarray(data.astype(np.float32), mode="F").save(str(path), "TIFF",
        tiffinfo={270: json.dumps(dict(metadata or {}, encoding="float32", scale=1.0, offset=0.0))})


def save_frame(
    path: Path,
    fmt: str,
    rgb: np.ndarray,
    data: np.ndarray,
    scale: tuple[float, float],
    *, packet: FramePacket | None = None, source: VideoMetadata | None = None,
) -> None:
    """Dispatch helper shared by the still dialog and the series export."""
    metadata = {"unit": packet.unit.key if packet else "",
                "frame": packet.index + 1 if packet else None,
                "timestamp": packet.timestamp.isoformat() if packet and packet.timestamp else None,
                "revision": packet.revision if packet else None,
                "processing": asdict(packet.processing) if packet and packet.processing else None,
                "analysis": dict(packet.analysis) if packet else {},
                "source": str(source.path) if source else ""}
    if fmt == "tiff16":
        save_tiff16(path, data, *scale, metadata=metadata)
    elif fmt == "tiff_float":
        save_tiff_float(path, data, metadata=metadata)
    else:
        save_still(path, rgb, fmt)


class MovieWriter:
    """Thin wrapper over imageio-ffmpeg for MP4 (H.264) / WMV output."""

    def __init__(self, path: Path, fps: float, fmt: str) -> None:
        self.path = Path(path)
        fps = max(1.0, float(fps))
        if fmt == "wmv":
            self._writer = imageio.get_writer(
                str(self.path), fps=fps, codec="wmv2", pixelformat="yuv420p", macro_block_size=None
            )
        else:
            self._writer = imageio.get_writer(
                str(self.path),
                fps=fps,
                codec="libx264",
                pixelformat="yuv420p",
                macro_block_size=None,
                ffmpeg_params=["-crf", "20"],
            )

    def append(self, rgb: np.ndarray) -> None:
        # Preserve every source pixel; pad an odd edge without resampling.
        height, width = rgb.shape[:2]
        self._writer.append_data(
            np.ascontiguousarray(np.pad(rgb, ((0, height % 2), (0, width % 2), (0, 0)), mode="edge"))
        )

    def close(self) -> None:
        self._writer.close()


def stats_csv_header(roi_names: list[str]) -> list[str]:
    header = [
        "frame",
        "timestamp",
        "unit",
        "min",
        "max",
        "mean",
        "std_dev",
        "pixels",
    ]
    for name in roi_names:
        header += [f"{name} min", f"{name} max", f"{name} mean", f"{name} std_dev"]
    return header + ["encoding", "scale", "offset", "processing", "analysis_revision"]


def stats_csv_row(packet: FramePacket, unit_label: str, *, scale=(0.0, 0.0), fmt="") -> list:
    row = [
        packet.index + 1,
        packet.timestamp.isoformat(sep=" ") if packet.timestamp else "",
        unit_label,
        f"{packet.minimum:.6g}",
        f"{packet.maximum:.6g}",
        f"{packet.mean:.6g}",
        f"{packet.std_dev:.6g}",
        packet.num_pixels,
    ]
    for stats in packet.roi_stats:
        row += [
            f"{stats.minimum:.6g}",
            f"{stats.maximum:.6g}",
            f"{stats.mean:.6g}",
            f"{stats.std_dev:.6g}",
        ]
    encoding = numerical_encoding(packet.data, *scale, fmt) if fmt in {"tiff16", "tiff_float"} else {}
    return row + [encoding.get("encoding", "RGB display"), encoding.get("scale", ""),
                  encoding.get("offset", ""), repr(packet.processing), packet.revision]


def write_stats_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


class StatsCsvWriter:
    """Streaming stats CSV for long series exports: open once, one row per
    frame, constant bookkeeping memory (rows are not accumulated in RAM)."""

    def __init__(self, path: Path, header: list[str]) -> None:
        self.path = Path(path)
        self._handle = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._handle)
        self._writer.writerow(header)

    def append(self, row: list) -> None:
        self._writer.writerow(row)

    def close(self) -> None:
        self._handle.close()


def frame_burn_label(packet: FramePacket, metadata: VideoMetadata) -> str:
    """Timestamp burn-in text for composed exports (§4.9.1.1, p. 61)."""
    seconds = frame_seconds(packet.timestamp, metadata, packet.index)
    stamp = (
        packet.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        if packet.timestamp is not None
        else ""
    )
    label = f"Frame {packet.index + 1} · {format_time(seconds)}"
    return f"{label} · {stamp}" if stamp else label
