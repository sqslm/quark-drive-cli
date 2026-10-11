"""Per-file playback bookmarks kept in the plugin's own private JSON file.

Bookmarks are an independent document (``bookmark.json``) so they never touch
the favorite/recent library the service owns. Each entry keeps only the object
identity (FID, file name, size), a saved position/duration, and an optional
UTF-8 note -- never a signed URL, a Cookie, a token, or media bytes. Reuse of
:func:`~..state.read_json`/:func:`~..state.write_json` gives the same atomic,
ACL-tight write path as the rest of the plugin state, and one instance lock
serializes every read-modify-write so concurrent request threads cannot drop a
record.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..errors import LocalIOError, RemoteError, UsageError
from ..paths import valid_fid
from ..state import read_json, write_json
from .plugin_state import plugin_directory

BOOKMARK_NAME = "bookmark.json"
#: Longest accepted note in *characters* (Chinese counts one per character).
NOTE_LIMIT = 200
_DOCUMENT_VERSION = 1
_KEYS = frozenset({"id", "fid", "name", "size", "position_ms", "duration_ms", "note"})
_CORRUPT = "播放书签库损坏，已停止；不会覆盖现有文件"
_UNSUPPORTED = "播放书签库版本不受支持，已停止"


def bookmark_path() -> Path:
    return plugin_directory() / BOOKMARK_NAME


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise LocalIOError(_CORRUPT)
    return value


def _note(value: Any) -> str:
    if not isinstance(value, str) or len(value) > NOTE_LIMIT:
        raise LocalIOError(_CORRUPT)
    return value


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise LocalIOError(_CORRUPT)
    return value


def _entry(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _KEYS:
        raise LocalIOError(_CORRUPT)
    try:
        fid = valid_fid(raw["fid"])
    except RemoteError:
        raise LocalIOError(_CORRUPT) from None
    return {
        "id": _text(raw["id"]),
        "fid": fid,
        "name": _text(raw["name"]),
        "size": _count(raw["size"]),
        "position_ms": _count(raw["position_ms"]),
        "duration_ms": _count(raw["duration_ms"]),
        "note": _note(raw["note"]),
    }


def _checked(document: Any) -> list[dict[str, Any]]:
    """Validate the stored document instead of silently dropping its entries."""
    if not isinstance(document, dict):
        raise LocalIOError(_CORRUPT)
    if document.get("version") != _DOCUMENT_VERSION:
        raise LocalIOError(_UNSUPPORTED)
    raw = document.get("bookmarks")
    if not isinstance(raw, list):
        raise LocalIOError(_CORRUPT)
    return [_entry(item) for item in raw]


def _caller_fid(value: Any) -> str:
    try:
        return valid_fid(value)
    except RemoteError:
        raise UsageError("书签的文件标识无效") from None


def _caller_text(value: Any, message: str) -> str:
    if not isinstance(value, str) or not value:
        raise UsageError(message)
    return value


def _caller_count(value: Any, message: str) -> int:
    if type(value) is not int or value < 0:
        raise UsageError(message)
    return value


def _caller_note(value: Any) -> str:
    if not isinstance(value, str):
        raise UsageError("书签备注无效")
    if len(value) > NOTE_LIMIT:
        raise UsageError(f"书签备注过长（最多 {NOTE_LIMIT} 字）")
    return value


class BookmarkStore:
    """Local playback bookmarks behind one process-wide lock.

    The public surface is the GUI/service integration contract: :meth:`list`,
    :meth:`add`, :meth:`rename`, :meth:`remove`, and :meth:`clear`. Every read
    and write revalidates the document, so a hand-edited or half-written file is
    reported instead of trusted.
    """

    def __init__(self, path: Path | None = None, *, new_id: Callable[[], str] | None = None) -> None:
        self.path = bookmark_path() if path is None else path
        self._new_id = (lambda: uuid.uuid4().hex) if new_id is None else new_id
        self._lock = threading.Lock()

    # -- reads -----------------------------------------------------------

    def list(self, fid: str) -> list[dict[str, Any]]:
        """Every bookmark recorded for ``fid``, newest first (read-only copies)."""
        with self._lock:
            return [dict(entry) for entry in self._document() if entry["fid"] == fid]

    # -- writes ----------------------------------------------------------

    def add(
        self,
        fid: str,
        name: str,
        size: int,
        position_ms: int,
        duration_ms: int,
        note: str,
    ) -> dict[str, Any]:
        """Prepend one bookmark for the exact ``fid``/``size``; return the stored row.

        The caller supplies an already-verified position/duration (the monitor
        reads them from a real native sample). Invalid input raises
        :class:`UsageError`; a broken document raises :class:`LocalIOError`.
        """
        entry = {
            "id": _caller_text(self._new_id(), "书签标识无效"),
            "fid": _caller_fid(fid),
            "name": _caller_text(name, "书签文件名无效"),
            "size": _caller_count(size, "书签文件大小无效"),
            "position_ms": _caller_count(position_ms, "书签位置无效"),
            "duration_ms": _caller_count(duration_ms, "书签时长无效"),
            "note": _caller_note(note),
        }
        with self._lock:
            entries = self._document()
            entries.insert(0, entry)
            self._store(entries)
        return dict(entry)

    def rename(self, bookmark_id: str, note: str) -> bool:
        """Replace one bookmark's note; ``False`` when the id is unknown."""
        if not isinstance(bookmark_id, str) or not bookmark_id:
            return False
        note = _caller_note(note)
        with self._lock:
            entries = self._document()
            for entry in entries:
                if entry["id"] == bookmark_id:
                    if entry["note"] != note:
                        entry["note"] = note
                        self._store(entries)
                    return True
            return False

    def remove(self, bookmark_id: str) -> bool:
        """Delete one bookmark; ``False`` when the id is unknown."""
        if not isinstance(bookmark_id, str) or not bookmark_id:
            return False
        with self._lock:
            entries = self._document()
            kept = [entry for entry in entries if entry["id"] != bookmark_id]
            if len(kept) == len(entries):
                return False
            self._store(kept)
            return True

    def clear(self) -> None:
        """Drop every bookmark (logout); a missing file is already clear."""
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise LocalIOError("无法清空播放书签，请检查本地状态目录") from None

    # -- document --------------------------------------------------------

    def _document(self) -> list[dict[str, Any]]:
        missing: dict[str, Any] = {}
        document = read_json(self.path, default=missing)
        if document is missing:
            return []
        return _checked(document)

    def _store(self, entries: list[dict[str, Any]]) -> None:
        write_json(self.path, {"version": _DOCUMENT_VERSION, "bookmarks": entries})
