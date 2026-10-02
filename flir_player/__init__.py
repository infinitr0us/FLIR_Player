# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Modern FLIR thermal video player."""

import ctypes
import importlib.util
import os
import sys
from pathlib import Path


def _preload_file_sdk() -> None:
    """Load the FLIR File SDK's native DLLs from ``fnv/_lib`` by full path.

    FileSDK 2026.1's extension modules delay-load these DLLs but cannot find
    them on their own: the first ``ImagerFile`` then kills the process with
    0xC06D007E (DELAYLOAD_DLL_NOT_FOUND), in a plain wheel install as well as
    in the packaged executable. Once they are loaded, the delay-load resolves
    to them. Harmless for older SDKs and a no-op without the SDK.

    It must run before ``fnv`` is imported: loading the DLLs afterwards makes
    FileSDK 2026.1 crash, so an earlier import (outside this package) is left
    alone. Import ``flir_player`` first in scripts that use both.
    """
    log = []  # FLIR_SDK_DEBUG=<file> records what happened, for support
    if os.name != "nt" or "fnv" in sys.modules:
        log.append(f"skipped (os={os.name}, fnv imported={'fnv' in sys.modules})")
        return _debug(log)
    try:
        spec = importlib.util.find_spec("fnv")
    except (ImportError, ValueError) as exc:
        log.append(f"find_spec failed: {exc!r}")
        return _debug(log)
    locations = list(getattr(spec, "submodule_search_locations", None) or ()) if spec else []
    if spec is not None and spec.origin:
        locations.append(str(Path(spec.origin).parent))
    library = next((Path(p) / "_lib" for p in locations if (Path(p) / "_lib").is_dir()), None)
    log.append(f"spec origin={getattr(spec, 'origin', None)!r} locations={locations} library={library}")
    if library is None:
        return _debug(log)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LoadLibraryExW.restype = ctypes.c_void_p
    kernel32.LoadLibraryExW.argtypes = (ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32)
    load_with_altered_search_path = 0x00000008  # resolve each DLL's own imports next to it
    for name in ("CharLS.dll", "zlib1.dll", "minizip.dll", "fnv.dll", "fnvreduce.dll", "fnvfile.dll"):
        dll = library / name
        if dll.is_file():
            handle = kernel32.LoadLibraryExW(str(dll), None, load_with_altered_search_path)
            log.append(f"{name}: {'loaded' if handle else f'error {ctypes.get_last_error()}'}")
    _debug(log)


def _debug(lines) -> None:
    target = os.environ.get("FLIR_SDK_DEBUG")
    if target:
        try:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write("\n".join(["[FLIR File SDK preload]", *lines, ""]))
        except OSError:
            pass


_preload_file_sdk()

from .models import FramePacket, UnitOption, VideoMetadata  # noqa: E402

__all__ = ["FramePacket", "UnitOption", "VideoMetadata"]
__version__ = "0.5.2"
