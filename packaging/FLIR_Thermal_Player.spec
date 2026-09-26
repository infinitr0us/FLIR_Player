# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata


PROJECT_ROOT = Path(SPEC).resolve().parent.parent
ONE_FILE = os.environ.get("FLIR_PACKAGE_MODE", "onefile").lower() == "onefile"

datas = []
binaries = []
hiddenimports = []
for package in ("fnv", "qtawesome", "imageio_ffmpeg"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# imageio/imageio-ffmpeg read their distribution metadata at import time
for distribution in ("imageio", "imageio-ffmpeg"):
    datas += copy_metadata(distribution)

# SVG glyphs referenced by the stylesheet (checkbox tick, combo chevron)
datas.append((str(PROJECT_ROOT / "flir_player" / "icons"), "flir_player/icons"))

conda_ffi = Path(sys.prefix) / "Library" / "bin" / "ffi.dll"
if conda_ffi.is_file():
    binaries.append((str(conda_ffi), "."))

hiddenimports += [
    "matplotlib.backends.backend_agg",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    # imageio resolves its ffmpeg plugin lazily at writer creation time
    "imageio.plugins.ffmpeg",
]

a = Analysis(
    [str(PROJECT_ROOT / "flir_player_app.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={"matplotlib": {"backends": ["Agg"]}},
    runtime_hooks=[],
    excludes=[
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
        "tkinter",
    ],
    noarchive=False,
    optimize=1,
)

# PyInstaller sees Anaconda's base ICU 58 on PATH and tries to bundle it as
# Qt6Core's unversioned `icuuc.dll`. That legacy DLL lacks the unversioned ICU
# exports used by the current PySide6 wheel and shadows Windows' compatible
# system ICU. Excluding the two legacy files is required for QtCore to load.
legacy_conda_icu = {"icuuc.dll", "icudt58.dll"}
a.binaries = [
    entry
    for entry in a.binaries
    if Path(entry[0]).name.lower() not in legacy_conda_icu
]


# The Conda environment's Visual C++ runtime (14.27) is too old for FLIR File
# SDK 2026.1: its fnvreduce.dll fails to initialize (error 1114, then the
# process dies with 0xC06D007E) against msvcp140 < 14.40, Microsoft's 2024
# std::mutex change. Bundle the newest redistributable copies found instead
# (Windows' own, or PySide6's); newer runtimes stay compatible with older code.
def _file_version(path):
    import ctypes
    from ctypes import wintypes

    version_dll = ctypes.windll.version
    size = version_dll.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return (0, 0, 0, 0)
    data = ctypes.create_string_buffer(size)
    if not version_dll.GetFileVersionInfoW(str(path), 0, size, data):
        return (0, 0, 0, 0)
    info, length = ctypes.c_void_p(), wintypes.UINT()
    if not version_dll.VerQueryValueW(data, "\\", ctypes.byref(info), ctypes.byref(length)):
        return (0, 0, 0, 0)
    fixed = ctypes.cast(info, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
    return (fixed[2] >> 16, fixed[2] & 0xFFFF, fixed[3] >> 16, fixed[3] & 0xFFFF)


import PySide6  # noqa: E402

vc_runtime = {"msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll",
              "vcruntime140_1.dll", "concrt140.dll"}
vc_sources = [Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32", Path(PySide6.__file__).parent]
vc_report = []
binaries = []
for dest, src, kind in a.binaries:
    name = Path(dest).name.lower()
    if name in vc_runtime and Path(dest).parent == Path("."):
        candidates = [Path(src)] + [folder / Path(dest).name for folder in vc_sources]
        best = max((c for c in candidates if c.is_file()), key=_file_version)
        vc_report.append(f"{Path(dest).name} {'.'.join(map(str, _file_version(best)))} from {best.parent}")
        src = str(best)
    binaries.append((dest, src, kind))
a.binaries = binaries
print("Bundled Visual C++ runtime:\n  " + "\n  ".join(vc_report))

pyz = PYZ(a.pure)
common = dict(
    name="FLIR_Thermal_Player",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=os.environ.get("FLIR_PACKAGE_CONSOLE", "0") == "1",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "assets" / "flir_player.ico"),
    version=str(PROJECT_ROOT / "packaging" / "version_info.txt"),
)

if ONE_FILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        **common,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        **common,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="FLIR_Thermal_Player",
    )
