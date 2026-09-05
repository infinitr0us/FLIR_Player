# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Application preferences, with an explicit file backend for isolated runs."""
from __future__ import annotations

import os

from PySide6.QtCore import QSettings


def app_settings() -> QSettings:
    path = os.environ.get("FLIR_SETTINGS_FILE")
    if path:
        return QSettings(path, QSettings.Format.IniFormat)
    return QSettings("Local", "FLIR Thermal Player")
