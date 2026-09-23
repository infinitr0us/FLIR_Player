# SPDX-FileCopyrightText: 2026 Yuchuan Li <Yuchuan.Li@outlook.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Output ownership: preflight, sibling staging and verified publication."""
from __future__ import annotations

import contextlib
import os
import shutil
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


def validate_destination(path: Path, sources=(), *, replace: bool = False) -> None:
    """Refuse source aliases always, and existing files unless replacing them
    was explicitly confirmed by the user."""
    if any(aliases(path, Path(source)) for source in sources):
        raise ValueError("An output must not be the source recording or an alias of any input")
    if path.is_dir():
        raise IsADirectoryError(f"Output is a folder: {path}")
    if path.exists() and not replace:
        raise FileExistsError(f"Output already exists; choose a new name: {path}")


def _path_key(path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def unique_destination(path: Path, reserved=()) -> Path:
    candidate = path
    number = 2
    while candidate.exists() or any(aliases(candidate, Path(p)) for p in reserved):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        number += 1
    return candidate


def _identity(path: Path):
    """What makes a file recognisably the one this job published.

    Survives hard-linking and same-volume renames. On Windows the file index
    of a FAT entry changes with a rename, so size and both timestamps (the
    creation time stays with the file) identify it there instead.
    """
    try:
        status = os.stat(path)
    except OSError:
        return None
    if os.name == "nt":
        return (status.st_dev, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)


def _publish_new(stage: Path, dest: Path) -> None:
    """Publish a complete staged file under a name that must not exist yet.

    A hard link publishes atomically and fails if the name appeared meanwhile.
    Without hard links (FAT32/exFAT drives, some network shares) Windows
    renames instead: MoveFileEx without REPLACE_EXISTING is equally atomic and
    never overwrites. Elsewhere the name is reserved with an exclusive create
    and the staged file moved over that placeholder.
    """
    try:
        os.link(stage, dest)
        return
    except FileExistsError:
        raise
    except OSError as link_error:
        if os.name == "nt":
            try:
                os.rename(stage, dest)
                return
            except FileExistsError:
                raise
            except OSError as error:
                raise error from link_error
        try:
            placeholder = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            raise
        except OSError as error:
            raise error from link_error
    reserved = _identity(dest)
    os.close(placeholder)
    try:
        os.replace(stage, dest)
    except BaseException:
        if _identity(dest) == reserved:  # still our empty placeholder
            with contextlib.suppress(OSError):
                os.unlink(dest)
        raise


class OutputTransaction:
    """Keep existing files intact, including if cancellation races SDK success.

    All outputs are staged next to their destination before publication. New
    names are published atomically and never overwrite a file that appeared
    meanwhile. An existing file the user confirmed replacing (``replace``) is
    replaced atomically, and its previous content is kept until the whole job
    has published so a failed job can restore it. A failed multi-file
    publication rolls back only files that are still ours, and never loses a
    previous version it could not put back.
    """
    def __init__(self, sources=(), abort=None, replace=()):
        """``replace`` lists the existing files the user confirmed replacing;
        any other destination that exists (or appears) is refused."""
        self.sources = tuple(Path(p) for p in sources)
        self.abort = abort or (lambda: False)
        self._replaceable = {_path_key(path) for path in replace}
        self._directories: dict[Path, Path] = {}  # destination folder → staging folder
        self._entries = []  # (stage, destination, replace)
        self._destinations = set()
        self._published = []  # (destination, identity, backup of the replaced file)
        self._complete = False

    def __enter__(self):
        return self

    def check_cancelled(self):
        if self.abort():
            raise JobCancelled("Operation cancelled")

    def _directory(self, parent: Path) -> Path:
        # Plain folders, removed explicitly: a TemporaryDirectory finalizer
        # could delete a backup that a failed rollback still has to keep.
        if parent not in self._directories:
            self._directories[parent] = Path(tempfile.mkdtemp(prefix=".flir-job-", dir=parent))
        return self._directories[parent]

    def stage(self, destination, *, replace: bool = False) -> Path:
        self.check_cancelled()
        dest = Path(destination).expanduser().resolve()
        replace = replace or _path_key(dest) in self._replaceable
        validate_destination(dest, self.sources, replace=replace)
        if dest in self._destinations:
            raise ValueError(f"Duplicate output: {dest}")
        self._destinations.add(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        stage = self._directory(dest.parent) / f"{len(self._entries):08d}{dest.suffix}"
        self._entries.append((stage, dest, bool(replace)))
        return stage

    def commit(self):
        for stage, dest, replace in self._entries:
            self.check_cancelled()
            if not stage.is_file() or stage.stat().st_size == 0:
                raise OSError(f"No complete output was produced for {dest.name}")
            validate_destination(dest, self.sources, replace=replace)
        for number, (stage, dest, replace) in enumerate(self._entries):
            self.check_cancelled()
            # Taken before publication: a file that replaces ours afterwards
            # can never be mistaken for it (and deleted) during rollback.
            identity = _identity(stage)
            backup = None
            if replace and dest.exists():
                backup = self._directory(dest.parent) / f"{number:08d}.previous"
                try:
                    os.link(dest, backup)
                except OSError:
                    shutil.copy2(dest, backup)
                os.replace(stage, dest)
            else:
                _publish_new(stage, dest)
            self._published.append((dest, identity, backup))
        self.check_cancelled()
        self._complete = True

    def __exit__(self, *_):
        kept: list[tuple[Path, Path]] = []  # (destination, where its previous version is)
        keep_folders: set[Path] = set()
        try:
            if not self._complete:
                for dest, identity, backup in reversed(self._published):
                    if identity is None or _identity(dest) != identity:
                        # Changed by someone else since: not ours to touch, but
                        # the user's previous version must still survive.
                        if backup is not None:
                            kept.append((dest, self._keep_backup(backup, dest, keep_folders)))
                        continue
                    try:
                        if backup is not None:
                            os.replace(backup, dest)
                        else:
                            dest.unlink(missing_ok=True)
                    except OSError:
                        if backup is None:
                            continue  # our new file stays; nothing of the user's is lost
                        kept.append((dest, self._keep_backup(backup, dest, keep_folders)))
        finally:
            for folder in self._directories.values():
                if folder not in keep_folders:
                    shutil.rmtree(folder, ignore_errors=True)
        if kept:
            where = "; ".join(f"{dest.name} → {path}" for dest, path in kept)
            raise OSError(f"Could not restore replaced output(s); previous version kept: {where}")

    @staticmethod
    def _keep_backup(backup: Path, dest: Path, keep_folders: set[Path]) -> Path:
        """Move a backup that could not be restored next to its destination.

        If even that fails, its staging folder is left in place instead of
        being cleaned up, so the user's previous file is never deleted.
        """
        target = unique_destination(dest.with_name(f"{dest.stem} (previous){dest.suffix}"))
        try:
            os.rename(backup, target)
            return target
        except OSError:
            keep_folders.add(backup.parent)
            return backup


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
