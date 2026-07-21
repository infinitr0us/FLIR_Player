# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""One-off visual verification for the UI fixes (not part of the test suite)."""

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

window = MainWindow("1.ats")
window.resize(1800, 1280)
window.show()
pump(app, 3.0)

window._add_roi("rect", ((300.0, 200.0), (700.0, 500.0)))
window._add_roi("line", ((100.0, 600.0), (1100.0, 300.0)))
window._add_roi("cursor", ((640.0, 360.0),))
pump(app, 2.0)

out = sys.argv[1] if len(sys.argv) > 1 else "artifacts/researchir-analysis/ui-fix-check.png"
window.grab().save(out, "PNG")
print("saved", out)
window.close()
