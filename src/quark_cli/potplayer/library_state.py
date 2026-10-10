"""Favorite directories and recently played videos, kept in one private JSON file.

The document lives in the plugin's own directory and holds only FIDs, names,
sizes, and timestamps: never a signed URL, a Cookie, or media bytes. A single
process lock serializes every read-modify-write, so the bridge's request threads
cannot overwrite each other, and reuse of :func:`read_json`/:func:`write_json`
gives the same atomic, ACL-tight write path as the rest of the plugin state.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..errors import LocalIOError, RemoteError
from ..paths import valid_fid
from ..state import read_json, write_json
from .plugin_state import plugin_directory

LIBRARY_NAME = "library.json"
RECENT_LIMIT = 100
_DOCUMENT_VERSION = 1
_CORRUPT = "收藏与最近观看库损坏，已停止；不会覆盖现有文件"


def library_path() -> Path:
    return plugin_directory() / LIBRARY_NAME


def _fid(value: Any) -> str:
    try:
        return valid_fid(value)
    except RemoteError:
        raise LocalIOError(_CORRUPT) from None


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise LocalIOError(_CORRUPT)
    return value


def _checked(document: dict[str, Any]) -> dict[str, Any]:
    """Validate the stored document instead of silently dropping its entries."""
    if set(document) != {"version", "favorites", "recent"} or type(document["version"]) is not int:
        raise LocalIOError(_CORRUPT)
    if document["version"] != _DOCUMENT_VERSION:
        raise LocalIOError("收藏与最近观看库版本不受支持，已停止")
    favorites, recent = document["favorites"], document["recent"]
    if not isinstance(favorites, list) or not isinstance(recent, list):
        raise LocalIOError(_CORRUPT)
    return {
        "version": _DOCUMENT_VERSION,
        "favorites": [_favorite(entry) for entry in favorites],
        "recent": [_recent(entry) for entry in recent],
    }


def _favorite(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != {"fid", "name", "added_at"}:
        raise LocalIOError(_CORRUPT)
    if type(entry["added_at"]) is not int:
        raise LocalIOError(_CORRUPT)
    return {"fid": _fid(entry["fid"]), "name": _text(entry["name"]), "added_at": entry["added_at"]}


def _recent(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != {"fid", "name", "size", "played_at"}:
        raise LocalIOError(_CORRUPT)
    if type(entry["size"]) is not int or entry["size"] < 0 or type(entry["played_at"]) is not int:
        raise LocalIOError(_CORRUPT)
    return {
        "fid": _fid(entry["fid"]),
        "name": _text(entry["name"]),
        "size": entry["size"],
        "played_at": entry["played_at"],
    }


def _public_favorite(entry: dict[str, Any]) -> dict[str, Any]:
    return {"fid": entry["fid"], "name": entry["name"], "is_dir": True, "size": 0}


def _public_recent(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "fid": entry["fid"],
        "name": entry["name"],
        "is_dir": False,
        "size": entry["size"],
        "played_at": entry["played_at"],
    }


class Library:
    """Favorite directories and recent videos behind one process-wide lock."""

    def __init__(self, path: Path | None = None, *, clock: Callable[[], int] | None = None) -> None:
        self.path = library_path() if path is None else path
        self._clock = (lambda: int(time.time())) if clock is None else clock
        self._lock = threading.Lock()

    # -- reads -----------------------------------------------------------

    def favorites(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_public_favorite(entry) for entry in self._document()["favorites"]]

    def recent(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_public_recent(entry) for entry in self._document()["recent"]]

    # -- writes ----------------------------------------------------------

    def add_favorite(self, fid: str, name: str) -> list[dict[str, Any]]:
        """Prepend a new favorite; an already-favorite FID stays where it is."""
        with self._lock:
            document = self._document()
            favorites = document["favorites"]
            if all(entry["fid"] != fid for entry in favorites):
                favorites.insert(0, {"fid": fid, "name": name, "added_at": self._clock()})
                self._store(document)
            return [_public_favorite(entry) for entry in favorites]

    def remove_favorite(self, fid: str) -> list[dict[str, Any]]:
        """Drop an FID if present; removing an absent FID changes nothing."""
        with self._lock:
            document = self._document()
            favorites = [entry for entry in document["favorites"] if entry["fid"] != fid]
            if len(favorites) != len(document["favorites"]):
                document["favorites"] = favorites
                self._store(document)
            return [_public_favorite(entry) for entry in favorites]

    def record_recent(self, fid: str, name: str, size: int) -> list[dict[str, Any]]:
        """Put an FID at the front, keep one entry per FID, and cap the list."""
        with self._lock:
            document = self._document()
            recent = [entry for entry in document["recent"] if entry["fid"] != fid]
            recent.insert(0, {"fid": fid, "name": name, "size": size, "played_at": self._clock()})
            del recent[RECENT_LIMIT:]
            document["recent"] = recent
            self._store(document)
            return [_public_recent(entry) for entry in recent]

    def clear_recent(self) -> None:
        with self._lock:
            document = self._document()
            if document["recent"]:
                document["recent"] = []
                self._store(document)

    def clear(self) -> None:
        """Explicit logout drops both lists; a missing file is already clear."""
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise LocalIOError("无法清空收藏与最近观看，请检查本地状态目录") from None

    # -- document --------------------------------------------------------

    def _document(self) -> dict[str, Any]:
        missing: dict[str, Any] = {}
        document = read_json(self.path, default=missing)
        if document is missing:
            return {"version": _DOCUMENT_VERSION, "favorites": [], "recent": []}
        return _checked(document)

    def _store(self, document: dict[str, Any]) -> None:
        write_json(self.path, document)
