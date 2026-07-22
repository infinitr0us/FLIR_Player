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
