"""Favorite directories and recently played videos, kept in one private JSON file.

The document lives in the plugin's own directory and holds only FIDs, names,
sizes, and timestamps: never a signed URL, a Cookie, or media bytes. A single
process lock serializes every read-modify-write, so the bridge's request threads
cannot overwrite each other, and reuse of :func:`read_json`/:func:`write_json`
gives the same atomic, ACL-tight write path as the rest of the plugin state.

Schema history (older documents are upgraded in memory on read and written back
only on the next normal write, so an empty startup never rewrites the file):

* version 1: ``favorites`` plus ``recent`` rows with no resume point.
* version 2: adds ``position_ms``/``duration_ms``/``progress_at`` to each recent row.
* version 3: adds the real ``parent`` directory of every recent row and a separate
  ``marks`` list of explicitly watched videos, so a manual watched flag outlives
  the 100-entry recent cap. A mark is keyed by both FID and size: a FID whose
  object was replaced with a different size never inherits the old mark.
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
_LEGACY_VERSION = 1
_PROGRESS_VERSION = 2
_DOCUMENT_VERSION = 3
_PROGRESS_FIELDS = ("position_ms", "duration_ms", "progress_at")
_RECENT_KEYS_V1 = frozenset({"fid", "name", "size", "played_at"})
_RECENT_KEYS_V2 = _RECENT_KEYS_V1 | set(_PROGRESS_FIELDS)
_RECENT_KEYS_V3 = _RECENT_KEYS_V2 | {"parent"}
_MARK_KEYS = frozenset({"fid", "name", "size", "watched_at"})
_CORRUPT = "收藏与最近观看库损坏，已停止；不会覆盖现有文件"
_UNSUPPORTED = "收藏与最近观看库版本不受支持，已停止"

# The end-of-video rule the playback monitor also uses: a saved point is worth
# offering as "continue" only past a minimum lead-in and clear of the credits
# window (``min(30s, 2% of the duration)``). Kept here so the monitor and the
# continue list cannot disagree about what counts as unfinished.
MIN_RESUME_MS = 15_000
END_MAX_MS = 30_000
END_FRACTION = 0.02


def unfinished(position_ms: int, duration_ms: int) -> bool:
    """True when a saved point is resumable and not yet effectively finished."""
    if position_ms < MIN_RESUME_MS or duration_ms <= 0 or position_ms >= duration_ms:
        return False
    return duration_ms - position_ms > min(END_MAX_MS, int(duration_ms * END_FRACTION))


def completed(position_ms: int, duration_ms: int) -> bool:
    """True when a real saved point shows the video was played to (or past) the end.

    The complement of :func:`unfinished`'s end window: exactly the same
    ``min(30s, 2% of the duration)`` rule decides that a video was finished.
    A missing position or an unknown duration is never "completed", so an
    unplayed record cannot silently count as watched.
    """
    if position_ms <= 0 or duration_ms <= 0:
        return False
    return duration_ms - position_ms <= min(END_MAX_MS, int(duration_ms * END_FRACTION))


def progress_text(position_ms: int, duration_ms: int) -> str:
    """A short watched percentage for a saved point; empty when there is none."""
    if position_ms <= 0 or duration_ms <= 0:
        return ""
    return f"{min(100, position_ms * 100 // duration_ms)}%"


def display_name(name: str, position_ms: int, duration_ms: int, watched: bool) -> str:
    """Decorate a row title for the UI; the raw ``name`` is kept for matching."""
    if watched:
        return f"{name} · 已看"
    percent = progress_text(position_ms, duration_ms)
    return name if not percent else f"{name} · 已看 {percent}"


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


def _parent(value: Any) -> str:
    """A stored parent FID is either a valid FID or the empty "unknown" marker."""
    if value == "":
        return ""
    return _fid(value)


def _safe_parent(value: Any) -> str:
    """Sanitize a caller-supplied parent: an unusable value means "unknown"."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        return valid_fid(value)
    except RemoteError:
        return ""


def _progress_value(value: Any) -> bool:
    return type(value) is int and value >= 0


def _checked(document: Any) -> dict[str, Any]:
    """Validate the stored document instead of silently dropping its entries."""
    if not isinstance(document, dict) or type(document.get("version")) is not int:
        raise LocalIOError(_CORRUPT)
    version = document["version"]
    if version not in (_LEGACY_VERSION, _PROGRESS_VERSION, _DOCUMENT_VERSION):
        raise LocalIOError(_UNSUPPORTED)
    expected = {"version", "favorites", "recent"}
    if version == _DOCUMENT_VERSION:
        expected.add("marks")
    if set(document) != expected:
        raise LocalIOError(_CORRUPT)
    favorites, recent = document["favorites"], document["recent"]
    marks = document["marks"] if version == _DOCUMENT_VERSION else []
    if not isinstance(favorites, list) or not isinstance(recent, list) or not isinstance(marks, list):
        raise LocalIOError(_CORRUPT)
    return {
        "version": _DOCUMENT_VERSION,
        "favorites": [_favorite(entry) for entry in favorites],
        "recent": [_recent(entry, version) for entry in recent],
        "marks": [_mark(entry) for entry in marks],
    }


def _favorite(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != {"fid", "name", "added_at"}:
        raise LocalIOError(_CORRUPT)
    if type(entry["added_at"]) is not int:
        raise LocalIOError(_CORRUPT)
    return {"fid": _fid(entry["fid"]), "name": _text(entry["name"]), "added_at": entry["added_at"]}


def _recent(entry: Any, version: int = _DOCUMENT_VERSION) -> dict[str, Any]:
    expected = {
        _LEGACY_VERSION: _RECENT_KEYS_V1,
        _PROGRESS_VERSION: _RECENT_KEYS_V2,
        _DOCUMENT_VERSION: _RECENT_KEYS_V3,
    }[version]
    if not isinstance(entry, dict) or set(entry) != expected:
        raise LocalIOError(_CORRUPT)
    if type(entry["size"]) is not int or entry["size"] < 0 or type(entry["played_at"]) is not int:
        raise LocalIOError(_CORRUPT)
    record = {
        "fid": _fid(entry["fid"]),
        "name": _text(entry["name"]),
        "size": entry["size"],
        "played_at": entry["played_at"],
    }
    for field in _PROGRESS_FIELDS:
        value = entry[field] if version >= _PROGRESS_VERSION else 0
        if not _progress_value(value):
            raise LocalIOError(_CORRUPT)
        record[field] = value
    record["parent"] = _parent(entry["parent"]) if version >= _DOCUMENT_VERSION else ""
    return record


def _mark(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != _MARK_KEYS:
        raise LocalIOError(_CORRUPT)
    if type(entry["size"]) is not int or entry["size"] < 0 or type(entry["watched_at"]) is not int:
        raise LocalIOError(_CORRUPT)
    return {
        "fid": _fid(entry["fid"]),
        "name": _text(entry["name"]),
        "size": entry["size"],
        "watched_at": entry["watched_at"],
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

    def snapshot(self) -> dict[str, Any]:
        """Read-only copies of favorites, recent records, and manual watched marks.

        The bridge derives its continue/series views from this; mutation of the
        returned rows never touches the stored document.
        """
        with self._lock:
            document = self._document()
            return {
                "favorites": [dict(entry) for entry in document["favorites"]],
                "recent": [dict(entry) for entry in document["recent"]],
                "marks": [dict(entry) for entry in document["marks"]],
            }

    def is_watched(self, fid: str, size: int) -> bool:
        """True while ``fid`` at ``size`` carries an explicit manual watched mark.

        The size is part of the identity: a FID whose object was replaced with a
        different size must not inherit the previous object's watched flag.
        """
        with self._lock:
            return any(
                mark["fid"] == fid and mark["size"] == size
                for mark in self._document()["marks"]
            )

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

    def record_recent(self, fid: str, name: str, size: int, parent: str = "") -> list[dict[str, Any]]:
        """Put an FID at the front, keep one entry per FID, and cap the list.

        A repeat play of the same ``fid`` and ``size`` keeps its saved resume
        point; a different size is a different object, so progress resets and any
        watched mark left by the replaced object is dropped. The real ``parent``
        directory is captured from a verified listing; an unknown call keeps the
        parent already on record instead of erasing it.
        """
        with self._lock:
            document = self._document()
            previous = next((entry for entry in document["recent"] if entry["fid"] == fid), None)
            keep = previous if previous is not None and previous["size"] == size else None
            recent = [entry for entry in document["recent"] if entry["fid"] != fid]
            entry = {"fid": fid, "name": name, "size": size, "played_at": self._clock()}
            for field in _PROGRESS_FIELDS:
                entry[field] = 0 if keep is None else keep[field]
            entry["parent"] = _safe_parent(parent) or (keep["parent"] if keep is not None else "")
            recent.insert(0, entry)
            del recent[RECENT_LIMIT:]
            document["recent"] = recent
            if any(mark["fid"] == fid and mark["size"] != size for mark in document["marks"]):
                document["marks"] = [
                    mark for mark in document["marks"] if mark["fid"] != fid or mark["size"] == size
                ]
            self._store(document)
            return [_public_recent(item) for item in recent]

    def progress(self, fid: str, size: int) -> dict[str, Any] | None:
        """Return the saved resume point for ``fid``/``size``, or ``None``.

        An explicitly watched video never offers a resume point, so manual state
        suppresses automatic resume until the video is marked unwatched again.
        """
        with self._lock:
            document = self._document()
            if any(mark["fid"] == fid and mark["size"] == size for mark in document["marks"]):
                return None
            for entry in document["recent"]:
                if entry["fid"] == fid and entry["size"] == size and entry["position_ms"] > 0:
                    return {field: entry[field] for field in _PROGRESS_FIELDS}
        return None

    def save_progress(self, fid: str, size: int, position_ms: int, duration_ms: int) -> bool:
        """Update an existing recent entry's resume point without reordering it.

        Never creates an entry, so a poll for a video that was not yet recorded
        cannot add a bogus watch row. An explicitly watched video is never
        written, so a real sample cannot revert a manual watched mark. ``False``
        means nothing was written; :class:`LocalIOError` propagates so a broken
        document is never masked.
        """
        if not _progress_value(position_ms) or not _progress_value(duration_ms):
            return False
        with self._lock:
            document = self._document()
            if any(mark["fid"] == fid and mark["size"] == size for mark in document["marks"]):
                return False
            for entry in document["recent"]:
                if entry["fid"] == fid:
                    if entry["size"] != size:
                        return False
                    entry["position_ms"], entry["duration_ms"] = position_ms, duration_ms
                    entry["progress_at"] = self._clock()
                    self._store(document)
                    return True
            return False

    def reset_progress(self, fid: str) -> bool:
        """Clear one entry's resume point in place; ``False`` when it is absent."""
        with self._lock:
            document = self._document()
            for entry in document["recent"]:
                if entry["fid"] == fid:
                    if any(entry[field] for field in _PROGRESS_FIELDS):
                        for field in _PROGRESS_FIELDS:
                            entry[field] = 0
                        self._store(document)
                    return True
            return False

    def mark_watched(self, fid: str, name: str, size: int) -> None:
        """Record an explicit manual watched mark for ``fid``/``size``.

        Marking the same object twice changes nothing; a mark left by a replaced
        object of the same FID but a different size is replaced, never inherited.
        """
        with self._lock:
            document = self._document()
            marks = document["marks"]
            if any(mark["fid"] == fid and mark["size"] == size for mark in marks):
                return
            document["marks"] = [mark for mark in marks if mark["fid"] != fid]
            document["marks"].insert(0, {
                "fid": _fid(fid), "name": _text(name), "size": size, "watched_at": self._clock(),
            })
            self._store(document)

    def mark_unwatched(self, fid: str) -> None:
        """Clear a manual watched mark and any saved resume point for that video.

        The live player is never touched here: only local state changes, so a
        running window keeps its own position. The next real sample may record a
        fresh resume point once the video is no longer watched.
        """
        with self._lock:
            document = self._document()
            changed = False
            marks = [mark for mark in document["marks"] if mark["fid"] != fid]
            if len(marks) != len(document["marks"]):
                document["marks"] = marks
                changed = True
            for entry in document["recent"]:
                if entry["fid"] == fid and any(entry[field] for field in _PROGRESS_FIELDS):
                    for field in _PROGRESS_FIELDS:
                        entry[field] = 0
                    changed = True
            if changed:
                self._store(document)

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
            return {"version": _DOCUMENT_VERSION, "favorites": [], "recent": [], "marks": []}
        return _checked(document)

    def _store(self, document: dict[str, Any]) -> None:
        write_json(self.path, document)
