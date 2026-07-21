# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""One-off visual verification for Tier-3 features (not part of the test suite)."""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

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

# ROIs for context
window._add_roi("rect", ((150.0, 120.0), (320.0, 260.0)))
window._add_roi("line", ((60.0, 400.0), (600.0, 220.0)))
pump(app, 1.5)

# Tier-3 display features: isotherm Above + PE AGC at 60 %
window.inspector.isotherm_combo.setCurrentIndex(1)
window.inspector.enhancement_combo.setCurrentIndex(1)
window._change_enhancement("pe", 0.6)
pump(app, 1.0)

# play range + loop indicator on the timeline
window.transport.slider.set_play_range(200, 2400)
from PySide6.QtCore import QSettings

_settings = QSettings("Local", "FLIR Thermal Player")
_loop_before = _settings.value("playback/loop", False, type=bool)
window.transport.loop_button.setChecked(True)

# zoom to 100 % (minimap + indicator appear)
window.canvas.set_zoom_level(1.0)
pump(app, 1.0)

out_dir = sys.argv[1] if len(sys.argv) > 1 else "artifacts/researchir-analysis"
window.grab().save(f"{out_dir}/tier3-main.png", "PNG")
print("saved tier3-main.png")

# palette invert + flip H for the second shot
window._change_palette_invert(True)
window._change_flips(True, False)
pump(app, 0.6)
window.grab().save(f"{out_dir}/tier3-invert-flip.png", "PNG")
print("saved tier3-invert-flip.png")
window._change_palette_invert(False)
window._change_flips(False, False)

# inspector close-up with segmentation + processing section populated
window.inspector.segmentation_check.setChecked(True)
window._change_segmentation(True, 31.0, 33.0)
window._reference_params = {"path": "2.seq", "frame_index": 0, "op": "subtract"}
window.decoder.request_reference(
    window._reference_params, window.current_packet.index, window._activate_request()
)
pump(app, 2.0)
window.inspector.grab().save(f"{out_dir}/tier3-inspector.png", "PNG")
print("saved tier3-inspector.png")

# color bar close-up with isotherm band + draggable handle
window.color_scale.grab().save(f"{out_dir}/tier3-colorbar.png", "PNG")
print("saved tier3-colorbar.png")

_settings.setValue("playback/loop", _loop_before)  # restore persisted setting
window.close()
print("done")
