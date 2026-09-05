# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Modern FLIR thermal video player."""

from .models import FramePacket, UnitOption, VideoMetadata

__all__ = ["FramePacket", "UnitOption", "VideoMetadata"]
__version__ = "0.4.1"
