# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Output ownership: preflight, sibling staging and no-overwrite publication."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from threading import Event


class JobCancelled(RuntimeError):
    pass


class CancellationToken:
    def __init__(self):
        self._event = Event()

    def cancel(self):
        self._event.set()

    def __call__(self):
        return self._event.is_set()


def aliases(path: Path, other: Path) -> bool:
    if path.resolve() == other.resolve():
        return True
    try:
        return path.samefile(other)
    except (OSError, ValueError):
        return False


def validate_destination(path: Path, sources=()) -> None:
    if any(aliases(path, Path(source)) for source in sources):
        raise ValueError("An output must not be the source recording or an alias of any input")
    if path.exists():
        raise FileExistsError(f"Output already exists; choose a new name: {path}")


def unique_destination(path: Path, reserved=()) -> Path:
    candidate = path
    number = 2
    while candidate.exists() or any(aliases(candidate, Path(p)) for p in reserved):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        number += 1
    return candidate


class OutputTransaction:
    """Keep existing files intact, including if cancellation races SDK success.

    All outputs are staged before publication. Hard-link creation publishes a
    complete file atomically and fails if another process created its name.
    A failed multi-file publication rolls back only links still owned by us.
    """
    def __init__(self, sources=(), abort=None):
        self.sources = tuple(Path(p) for p in sources)
        self.abort = abort or (lambda: False)
        self._directories = {}
        self._entries = []
        self._destinations = set()
        self._published = []
        self._complete = False

    def __enter__(self):
        return self

    def check_cancelled(self):
        if self.abort():
            raise JobCancelled("Operation cancelled")

    def stage(self, destination) -> Path:
        self.check_cancelled()
        dest = Path(destination).expanduser().resolve()
        validate_destination(dest, self.sources)
        if dest in self._destinations:
            raise ValueError(f"Duplicate output: {dest}")
        self._destinations.add(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.parent not in self._directories:
            self._directories[dest.parent] = tempfile.TemporaryDirectory(prefix=".flir-job-", dir=dest.parent)
        stage = Path(self._directories[dest.parent].name) / f"{len(self._entries):08d}{dest.suffix}"
        self._entries.append((stage, dest))
        return stage

    def commit(self):
        for stage, dest in self._entries:
            self.check_cancelled()
            if not stage.is_file() or stage.stat().st_size == 0:
                raise OSError(f"No complete output was produced for {dest.name}")
            validate_destination(dest, self.sources)
        for stage, dest in self._entries:
            self.check_cancelled()
            os.link(stage, dest)
            self._published.append((stage, dest))
        self.check_cancelled()
        self._complete = True

    def __exit__(self, *_):
        try:
            if not self._complete:
                for stage, dest in reversed(self._published):
                    if aliases(stage, dest):
                        dest.unlink(missing_ok=True)
        finally:
            for directory in self._directories.values():
                directory.cleanup()


def extract_recording(im, source, dest, options, *, progress=None, abort=None):
    """SDK extraction with latched cancellation and job-owned cleanup."""
    aborted = False

    def callback(current, total):
        nonlocal aborted
        if progress is not None:
            progress(current, total)
        aborted = aborted or bool(abort and abort())
        return aborted

    options.progress_callback = callback
    try:
        with OutputTransaction([source], lambda: aborted or bool(abort and abort())) as job:
            stage = job.stage(dest)
            ok = bool(im.extract(str(stage), options))
            job.check_cancelled()
            if not ok:
                raise OSError("The File SDK reported the extraction as failed")
            if not stage.is_file() or not stage.stat().st_size:
                raise OSError("The File SDK produced no output (extraction requires an ATS source)")
            job.commit()
        return True, ""
    except JobCancelled:
        return False, "Extraction cancelled"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
