# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from flir_player.main_window import MainWindow
from flir_player.style import APP_STYLESHEET, install_ui_fonts


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Capture a loaded FLIR player window")
    result.add_argument("recording", type=Path)
    result.add_argument("output", type=Path)
    result.add_argument("--width", type=int, default=1440)
    result.add_argument("--height", type=int, default=1024)
    result.add_argument("--settle-ms", type=int, default=700)
    result.add_argument("--timeout-ms", type=int, default=15000)
    return result


def main() -> int:
    args = parser().parse_args()
    app = QApplication([])
    app.setStyle("Fusion")
    install_ui_fonts()
    app.setStyleSheet(APP_STYLESHEET)
    window = MainWindow()
    window.resize(args.width, args.height)
    window.show()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    state = {"captured": False, "exit_code": 1}

    def capture() -> None:
        if state["captured"]:
            return
        state["captured"] = True
        state["exit_code"] = 0 if window.grab().save(str(args.output), "PNG") else 2
        window.close()
        app.quit()

    window.decoder.opened.connect(lambda *_: QTimer.singleShot(args.settle_ms, capture))
    QTimer.singleShot(args.timeout_ms, capture)
    window.open_path(args.recording)
    app.exec()
    return int(state["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
