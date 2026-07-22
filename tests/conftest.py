# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import importlib.util
import inspect
import os
import re
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from flir_player.style import APP_STYLESHEET, install_ui_fonts


# ---------------------------------------------------------------------------
# Sample-data / SDK availability guards
#
# A large part of the suite needs the proprietary FLIR File SDK (the ``fnv``
# package) and real recordings placed in ``local/data/`` (``1.ats`` and
# ``2.seq``). Neither is distributed with this project. When either is missing
# the affected tests are skipped instead of erroring, so a bare clone (and CI)
# still runs the pure-logic tests. When both are present — as on a developer
# machine with its own recordings — nothing is skipped and the full suite runs.
# ---------------------------------------------------------------------------
_SAMPLES = Path(__file__).resolve().parents[1] / "local" / "data"
_SDK_AVAILABLE = importlib.util.find_spec("fnv") is not None
_SEQ_AVAILABLE = (_SAMPLES / "2.seq").exists()
_ATS_AVAILABLE = (_SAMPLES / "1.ats").exists()


def _relevant_sources(item: pytest.Item) -> str:
    """Source text of a test plus the same-module helpers and fixtures it uses.

    Data dependencies are detected by looking for the sample-file names, which
    may live in the test body, in a module-level helper it calls (e.g.
    ``_open_seq_source``), or in a fixture it requests.
    """
    func = getattr(item, "function", None)
    if func is None:
        return ""
    parts: list[str] = []
    try:
        parts.append(inspect.getsource(func))
    except (OSError, TypeError):
        return ""
    module = inspect.getmodule(func)
    body = parts[0]
    if module is not None:
        for name, obj in vars(module).items():
            if (
                inspect.isfunction(obj)
                and obj is not func
                and getattr(obj, "__module__", None) == module.__name__
                and re.search(rf"\b{re.escape(name)}\s*\(", body)
            ):
                try:
                    parts.append(inspect.getsource(obj))
                except (OSError, TypeError):
                    pass
    info = getattr(item, "_fixtureinfo", None)
    if info is not None:
        for defs in getattr(info, "name2fixturedefs", {}).values():
            for fixture_def in defs:
                fixture_func = getattr(fixture_def, "func", None)
                if fixture_func is None:
                    continue
                try:
                    parts.append(inspect.getsource(fixture_func))
                except (OSError, TypeError):
                    pass
    return "\n".join(parts)


def pytest_collection_modifyitems(config, items):  # noqa: D401
    for item in items:
        source = _relevant_sources(item)
        needs_seq = "2.seq" in source
        needs_ats = "1.ats" in source
        if not (needs_seq or needs_ats):
            continue
        if not _SDK_AVAILABLE:
            item.add_marker(
                pytest.mark.skip(reason="FLIR File SDK (the fnv package) is not installed")
            )
        elif needs_seq and not _SEQ_AVAILABLE:
            item.add_marker(
                pytest.mark.skip(reason="sample recording '2.seq' is not present in local/data/")
            )
        elif needs_ats and not _ATS_AVAILABLE:
            item.add_marker(
                pytest.mark.skip(reason="sample recording '1.ats' is not present in local/data/")
            )


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    install_ui_fonts()
    app.setStyleSheet(APP_STYLESHEET)
    return app


def wait_until(app: QApplication, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return bool(predicate())
