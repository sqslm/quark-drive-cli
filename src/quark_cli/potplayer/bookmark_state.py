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
_DOCUMENT_VERSION = 3
_KEYS = frozenset({"id", "fid", "name", "size", "position_ms", "duration_ms", "note"})
_DIRECTORY_KEYS = frozenset({'parent', 'parent_name'})
_CONTEXT_KEYS = frozenset({'series_fid', 'series_name', 'category', 'version', 'video_prefix'})
_CORRUPT = "精彩标记库损坏，已停止；不会覆盖现有文件"
_UNSUPPORTED = "精彩标记库版本不受支持，已停止"


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
    if not isinstance(raw, dict) or set(raw) not in (_KEYS, _KEYS | _DIRECTORY_KEYS, _KEYS | _DIRECTORY_KEYS | _CONTEXT_KEYS):
        raise LocalIOError(_CORRUPT)
    try:
        fid = valid_fid(raw["fid"])
    except RemoteError:
        raise LocalIOError(_CORRUPT) from None
    entry = {
        "id": _text(raw["id"]),
        "fid": fid,
        "name": _text(raw["name"]),
        "size": _count(raw["size"]),
        "position_ms": _count(raw["position_ms"]),
        "duration_ms": _count(raw["duration_ms"]),
        "note": _note(raw["note"]),
    }
    if 'parent' in raw:
        try:
            entry['parent'] = valid_fid(raw['parent'])
        except RemoteError:
            raise LocalIOError(_CORRUPT) from None
        if not isinstance(raw['parent_name'], str):
            raise LocalIOError(_CORRUPT)
        entry['parent_name'] = raw['parent_name']
    if 'series_fid' in raw:
        if not all(isinstance(raw[key], str) for key in _CONTEXT_KEYS):
            raise LocalIOError(_CORRUPT)
        entry.update({key: raw[key] for key in _CONTEXT_KEYS})
    return entry


def _checked(document: Any) -> list[dict[str, Any]]:
    """Validate the stored document instead of silently dropping its entries."""
    if not isinstance(document, dict):
        raise LocalIOError(_CORRUPT)
    if document.get("version") not in (1, 2, _DOCUMENT_VERSION):
        raise LocalIOError(_UNSUPPORTED)
    raw = document.get("bookmarks")
    if not isinstance(raw, list):
        raise LocalIOError(_CORRUPT)
    return [_entry(item) for item in raw]


def _caller_fid(value: Any) -> str:
    try:
        return valid_fid(value)
    except RemoteError:
        raise UsageError("精彩标记的文件标识无效") from None


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
        raise UsageError("精彩标记备注无效")
    if len(value) > NOTE_LIMIT:
        raise UsageError(f"精彩标记备注过长（最多 {NOTE_LIMIT} 字）")
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

    def list(self, fid: str | None = None) -> list[dict[str, Any]]:
        """One video's marks, or all marks when omitted; newest first, copied."""
        with self._lock:
            return [dict(entry) for entry in self._document() if fid is None or entry["fid"] == fid]

    # -- writes ----------------------------------------------------------

    def add(
        self,
        fid: str,
        name: str,
        size: int,
        position_ms: int,
        duration_ms: int,
        note: str,
        *,
        parent: str = '',
        parent_name: str = '',
        context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Prepend one bookmark for the exact ``fid``/``size``; return the stored row.

        The caller supplies an already-verified position/duration (the monitor
        reads them from a real native sample). Invalid input raises
        :class:`UsageError`; a broken document raises :class:`LocalIOError`.
        """
        entry = {
            "id": _caller_text(self._new_id(), "精彩标记标识无效"),
            "fid": _caller_fid(fid),
            "name": _caller_text(name, "精彩标记文件名无效"),
            "size": _caller_count(size, "精彩标记文件大小无效"),
            "position_ms": _caller_count(position_ms, "精彩标记位置无效"),
            "duration_ms": _caller_count(duration_ms, "精彩标记时长无效"),
            "note": _caller_note(note),
        }
        if parent:
            if not isinstance(parent_name, str):
                raise UsageError('精彩标记目录名称无效')
            entry.update(parent=_caller_fid(parent), parent_name=parent_name)
            if context:
                entry.update({key: context[key] for key in _CONTEXT_KEYS})
        with self._lock:
            entries = self._document()
            entries.insert(0, entry)
            self._store(entries)
        return dict(entry)

    def set_directories(self, directories: dict[str, tuple[str, str]]) -> None:
        """Retain verified directory identities independently of recent history."""
        with self._lock:
            entries = self._document()
            changed = False
            for entry in entries:
                context = directories.get(entry['fid'])
                if context is None or not context[0]:
                    continue
                parent, name = context
                parent = _caller_fid(parent)
                if not isinstance(name, str):
                    raise UsageError('精彩标记目录名称无效')
                if entry.get('parent') != parent or (name and entry.get('parent_name') != name):
                    saved_name = entry.get('parent_name', '') if entry.get('parent') == parent else ''
                    entry.update(parent=parent, parent_name=name or saved_name)
                    changed = True
            if changed:
                self._store(entries)

    def set_contexts(self, contexts: dict[str, dict[str, str]]) -> None:
        with self._lock:
            entries = self._document()
            changed = False
            for entry in entries:
                context = contexts.get(entry['fid'])
                if context and any(entry.get(key) != value for key, value in context.items()):
                    entry.update(context)
                    _entry(entry)
                    changed = True
            if changed:
                self._store(entries)

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
                raise LocalIOError("无法清空精彩标记，请检查本地状态目录") from None

    # -- document --------------------------------------------------------

    def _document(self) -> list[dict[str, Any]]:
        missing: dict[str, Any] = {}
        document = read_json(self.path, default=missing)
        if document is missing:
            return []
        return _checked(document)

    def _store(self, entries: list[dict[str, Any]]) -> None:
        write_json(self.path, {"version": _DOCUMENT_VERSION, "bookmarks": entries})


def bookmark_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group by real parent FID; never merge series by their display names."""
    groups: dict[str, dict[str, Any]] = {}
    for entry in items:
        parent = group_key(entry)
        name = entry.get('series_name') or entry.get('parent_name') or ('我的网盘' if parent == '0' else '未命名剧集' if parent else '未分类')
        group = groups.setdefault(parent, {'parent': parent, 'name': name, 'count': 0, 'category': entry.get('category', '未分类')})
        if entry.get('series_name') or entry.get('parent_name'):
            group['name'] = name
        group['count'] += 1
    return list(groups.values())


def group_key(entry):
    return entry.get('series_fid') or entry.get('parent', '')
