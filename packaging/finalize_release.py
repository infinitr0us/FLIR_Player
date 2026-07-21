# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Finalize the release folder after a PyInstaller build.

Regenerates ``release/SHA256SUMS.txt``, ``release/BUILD_INFO.txt`` and
``release/README.txt`` from the built executable and the current environment.
Run by ``build_exe.bat``; safe to run standalone as well.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import sys
from datetime import date
from pathlib import Path

VERSION = "0.2.0"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RELEASE_DIR = PROJECT_ROOT / "release"
EXE = RELEASE_DIR / "FLIR_Thermal_Player.exe"

README_TEMPLATE = """FLIR Thermal Player {version} (Windows x64)
{underline}

Run FLIR_Thermal_Player.exe, then click the folder button to open a FLIR
SEQ, ATS, SFMOV, CSQ, FFF, PTW, or radiometric TIFF recording. You can also
drag a supported file onto the window or pass its path as the first
command-line argument. The Open button's dropdown lists recent recordings.

Features: timestamp-accurate playback with play-range markers and looping;
counts/object-signal/temperature/radiance units; measurement (object)
parameters; box/ellipse/line/spot ROIs with statistics, temporal, profile,
and histogram plots; metadata and source panels; zoom, pan, and minimap;
plateau-equalization AGC; isotherms; segmentation; image flip; 11 palettes
plus a custom palette editor; reference-frame file operation; point,
spatial, and temporal filters; still export (PNG/BMP/JPEG/TIFF 16-bit and
32-bit float) with composition options; MP4/WMV movie export; image-series
export; ROI bitmask export; clip extract and batch extract (ATS).

This is a single-file GUI build. Python, Conda, and the source checkout are
not required on the target computer. Initial startup may take a few seconds
while the bundled runtime is extracted.

Verified on {build_date} with the bundled smoke test:
  FLIR_Thermal_Player.exe 2.seq --smoke-test  (playback advances, exit 0)

The executable is unsigned, so Windows SmartScreen may show an unknown
publisher warning. For wider distribution, sign it with your organization's
Windows code-signing certificate.

The executable contains FLIR File SDK runtime components. Review the FLIR
SDK license and export-control notice before redistributing it beyond your
permitted users or organization.
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "?"


def main() -> int:
    if not EXE.is_file():
        print(f"error: {EXE} not found — run PyInstaller first", file=sys.stderr)
        return 1

    digest = sha256(EXE)
    size = EXE.stat().st_size
    build_date = date.today().isoformat()

    (RELEASE_DIR / "SHA256SUMS.txt").write_text(
        f"{digest}  FLIR_Thermal_Player.exe\n", encoding="utf-8"
    )

    info_lines = [
        "Product: FLIR Thermal Player",
        f"Version: {VERSION}",
        "Platform: Windows x64",
        f"Build date: {build_date}",
        f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        f"PyInstaller: {package_version('pyinstaller')}",
        f"PySide6: {package_version('PySide6')}",
        f"QtAwesome: {package_version('QtAwesome')}",
        f"FLIR FileSDK: {package_version('FileSDK')}",
        f"imageio: {package_version('imageio')}",
        f"imageio-ffmpeg: {package_version('imageio-ffmpeg')}",
        f"Executable size: {size:,} bytes ({size / 1048576:.1f} MiB)",
        f"SHA-256: {digest}",
    ]
    (RELEASE_DIR / "BUILD_INFO.txt").write_text(
        "\n".join(info_lines) + "\n", encoding="utf-8"
    )

    title = f"FLIR Thermal Player {VERSION} (Windows x64)"
    (RELEASE_DIR / "README.txt").write_text(
        README_TEMPLATE.format(
            version=VERSION, underline="=" * len(title), build_date=build_date
        ),
        encoding="utf-8",
    )

    print(f"Finalized release {VERSION}: {size / 1048576:.1f} MiB, SHA-256 {digest[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
