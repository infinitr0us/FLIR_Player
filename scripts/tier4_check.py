# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""One-off visual verification for Tier-4 export features (not part of the suite)."""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from flir_player.export_dialogs import ExportMovieDialog
from flir_player.main_window import MainWindow
from flir_player.style import APP_STYLESHEET, install_ui_fonts


def pump(app, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


app = QApplication(sys.argv[:1])
app.setStyle("Fusion")
install_ui_fonts()
app.setStyleSheet(APP_STYLESHEET)

window = MainWindow("2.seq")
window.resize(1800, 1280)
window.show()
pump(app, 3.0)

window._add_roi("rect", ((150.0, 120.0), (320.0, 260.0)))
window._add_roi("cursor", ((420.0, 300.0),))
pump(app, 1.5)

out_dir = sys.argv[1] if len(sys.argv) > 1 else "artifacts/researchir-analysis"

# 1. export menu expanded (grab without showMenu — offscreen showMenu hangs)
export_menu = window.title_bar.export_button.menu()
export_menu.ensurePolished()
export_menu.grab().save(f"{out_dir}/tier4-menu.png", "PNG")
print("saved tier4-menu.png")

# 2. movie export dialog
dialog = ExportMovieDialog(window.metadata, window, default_range=(100, 900))
dialog.show()
pump(app, 0.5)
dialog.grab().save(f"{out_dir}/tier4-movie-dialog.png", "PNG")
dialog.close()
print("saved tier4-movie-dialog.png")

# 3. composed still export sample (color bar + ROIs + timestamp)
from flir_player.compose import ExportOptions, compose_frame
from flir_player.export import frame_burn_label
from flir_player.render import render_frame_rgb

packet = window.current_packet
state = window._display_state(packet)
rgb, low, high = render_frame_rgb(packet.data, state, packet.clip_mask)
options = ExportOptions(
    color_bar=True, rois=True, roi_names=True, markers=True, timestamp=True
)
composed = compose_frame(
    rgb,
    options,
    palette=state.palette,
    inverted=state.inverted,
    scale=(low, high),
    suffix=packet.unit.suffix,
    rois=tuple(window._rois),
    roi_stats=packet.roi_stats,
    min_position=packet.min_position,
    max_position=packet.max_position,
    label=frame_burn_label(packet, window.metadata),
    flips=(state.flip_h, state.flip_v),
)
from PIL import Image

Image.fromarray(composed).save(f"{out_dir}/tier4-composed.png", "PNG")
print("saved tier4-composed.png")

# 4. recent files dropdown populated
window._refresh_recent_menu()
open_menu = window.title_bar.open_button.menu()
open_menu.ensurePolished()
open_menu.grab().save(f"{out_dir}/tier4-recent.png", "PNG")
print("saved tier4-recent.png")

window.close()
print("done")
