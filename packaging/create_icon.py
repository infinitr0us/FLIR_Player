# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image, ImageDraw
from PySide6.QtWidgets import QApplication

from flir_player.style import install_ui_fonts
from flir_player.widgets import awesome_icon


def main() -> int:
    assets = PROJECT_ROOT / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    glyph_path = assets / "flir_player_glyph.png"
    png_path = assets / "flir_player.png"
    ico_path = assets / "flir_player.ico"

    app = QApplication.instance() or QApplication([])
    install_ui_fonts()
    pixmap = awesome_icon("fa6s.diamond", "#f6a600").pixmap(320, 320)
    if not pixmap.save(str(glyph_path), "PNG"):
        return 2

    canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((16, 16, 496, 496), radius=104, fill=(24, 28, 32, 255))
    glyph = Image.open(glyph_path).convert("RGBA")
    canvas.alpha_composite(glyph, ((512 - glyph.width) // 2, (512 - glyph.height) // 2))
    canvas.save(png_path)
    canvas.save(
        ico_path,
        format="ICO",
        sizes=((16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)),
    )
    glyph_path.unlink(missing_ok=True)
    print(ico_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
