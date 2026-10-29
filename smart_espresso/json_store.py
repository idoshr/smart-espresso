"""Atomic JSON files that survive a bad write and a bad file on disk.

A power cut is how an espresso machine usually turns off. The temp file is
fsynced before it replaces the destination, and the directory is fsynced
after that, so the name points at a complete file. The previous good file
is kept beside it as ``<path>.bak``. A file that cannot be parsed is renamed
to ``<path>.corrupt-<time>`` and is never written over.
"""

import json
import os
import shutil
import tempfile
import time
from typing import Optional


def quarantine(path: str) -> str:
    """Rename ``path`` aside so a later save cannot replace it."""
    stamp = time.time_ns()
    dest = f"{path}.corrupt-{stamp}"
    suffix = 0
    while os.path.exists(dest):
        suffix += 1
        dest = f"{path}.corrupt-{stamp}-{suffix}"
    os.replace(path, dest)
    return dest


def load_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_with_backup(path: str, parse):
    """
    Load ``path``, or its ``.bak`` when the main file cannot be parsed.

    ``parse`` turns the decoded JSON into the caller's value and raises
    ``ValueError`` or ``TypeError`` when the shape is wrong. Returns None
    when nothing usable is on disk. An unreadable file is quarantined.
    """
    if not path:
        return None

    if os.path.exists(path):
        try:
            return parse(load_json(path))
        except (OSError, ValueError, TypeError) as error:
            print(f"Ignoring unreadable state {path}: {error}")
            try:
                kept = quarantine(path)
                print(f"Preserved unreadable state as {kept}")
            except OSError as move_error:
                print(f"Could not preserve unreadable state {path}: {move_error}")
                return None

    backup = path + ".bak"
    if not os.path.exists(backup):
        return None
    try:
        value = parse(load_json(backup))
    except (OSError, ValueError, TypeError) as error:
        print(f"Ignoring unreadable backup {backup}: {error}")
        try:
            quarantine(backup)
        except OSError as move_error:
            print(f"Could not preserve unreadable backup {backup}: {move_error}")
        return None
    print(f"Restored {path} from {backup}")
    return value


def atomic_write_json(path: str, payload) -> None:
    """
    Replace ``path`` with ``payload``.

    The previous file, when there is one, is linked to ``path.bak`` first.
    If the replace fails, ``path`` is left as it was.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path: Optional[str]
    handle, temp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(payload, file, separators=(",", ":"))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        _keep_backup(path)
        os.replace(temp_path, path)
        temp_path = None
        _fsync_directory(directory)
    except OSError:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise


def _keep_backup(path: str) -> None:
    if not os.path.exists(path):
        return
    backup = path + ".bak"
    try:
        try:
            os.unlink(backup)
        except FileNotFoundError:
            pass
        os.link(path, backup)
    except OSError:
        try:
            shutil.copyfile(path, backup)
            _fsync_file(backup)
        except OSError as error:
            print(f"Could not keep a backup of {path}: {error}")


def _fsync_file(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(directory: str) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
