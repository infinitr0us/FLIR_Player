# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow
from .style import APP_STYLESHEET, install_ui_fonts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Modern FLIR thermal video player")
    parser.add_argument("recording", nargs="?", help="Optional SEQ, ATS, SFMOV, or CSQ file")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-output", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("FLIR Thermal Player")
    app.setOrganizationName("Local")
    app.setStyle("Fusion")
    install_ui_fonts()
    app.setStyleSheet(APP_STYLESHEET)
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)

    window = MainWindow(args.recording)
    window.show()

    if args.smoke_test:
        if not args.recording:
            parser = build_parser()
            parser.error("--smoke-test requires a recording")
        state = {"finished": False}

        def finish(code: int) -> None:
            if state["finished"]:
                return
            state["finished"] = True
            if code == 0 and args.smoke_output is not None:
                args.smoke_output.parent.mkdir(parents=True, exist_ok=True)
                if not window.grab().save(str(args.smoke_output), "PNG"):
                    code = 4
            # A smoke-test exit obeys the same worker lifetime as an ordinary
            # close, including timeout/error paths during a noninterruptible SDK call.
            app.setQuitOnLastWindowClosed(False)
            if window.decoder.isRunning():
                window.decoder.finished.connect(lambda: app.exit(code))
                window.close()
            else:
                window.close()
                app.exit(code)

        def exercise_playback(*_args) -> None:
            def start() -> None:
                if window.current_packet is None or window._busy:
                    finish(3)
                    return
                initial_index = window.current_packet.index
                window.toggle_playback()

                def verify() -> None:
                    advanced = (
                        window.current_packet is not None
                        and window.current_packet.index > initial_index
                    )
                    finish(0 if advanced else 5)

                QTimer.singleShot(1200, verify)

            QTimer.singleShot(120, start)

        window.decoder.opened.connect(exercise_playback)
        window.decoder.failed.connect(lambda _message: finish(2))
        QTimer.singleShot(20000, lambda: finish(3))

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
