"""Disk-backed cache keyed on each source's own change signal.

No database: this only ever holds the current and next week, so a JSON file
per kitchen is enough. The change key (ETag, PDF URL set, listing hash) is what
decides whether the expensive load() runs - not a wall-clock TTL.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path

CACHE_DIR = Path(os.environ.get("KITCHEN_CACHE_DIR", Path.home() / ".cache" / "kitchen-menus"))


def _path(name: str) -> Path:
    return CACHE_DIR / f"{name}.json"


def read(name: str) -> dict | None:
    try:
        with _path(name).open(encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write(name: str, change_key: str | None, payload: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "change_key": change_key,
        "stored_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "payload": payload,
    }
    # Atomic replace so a crash mid-write cannot leave a truncated cache file.
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, default=str)
        os.replace(tmp, _path(name))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_blob(name: str) -> bytes | None:
    try:
        return (CACHE_DIR / name).read_bytes()
    except FileNotFoundError:
        return None


def write_blob(name: str, data: bytes) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / name).write_bytes(data)
