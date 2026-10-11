"""The loopback HTTP bridge that the PotPlayer AngelScript extensions call.

The service binds ``127.0.0.1`` only, keeps one instance per user, and never
hands the player a Cookie or a signed Quark URL: control endpoints require the
runtime Bearer token, and playback URLs carry an unguessable session id.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from ..auth import LoginCancelled
from ..client import QuarkClient, default_guard
from ..errors import (
    AccessRestrictedError,
    AuthenticationError,
    LocalIOError,
    NetworkError,
    QuarkError,
    RateLimitError,
    RemoteError,
    UsageError,
)
from ..files import Files
from ..paths import is_directory
from ..request_guard import RequestGuard
from . import SERVICE_VERSION
from .bookmark_state import NOTE_LIMIT
from .diagnostics import Diagnostics
from .library import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    page,
    playlist,
    require_fid,
    search,
    select_continue,
)
from .library_state import Library, completed, display_name, progress_text, unfinished
from .native_player import NativePlayer
from .playback import POLL_INTERVAL, PlaybackMonitor
from .login_gui import QrLoginWindow
from .naming import content_type, is_video
from .plugin_state import (
    acquire_service_lock,
    clear_cookie,
    clear_runtime,
    credential_path,
    load_cookie,
    read_runtime,
    write_runtime,
)
from .stream import (
    MediaChanged,
    PlaySession,
    RangeNotSatisfiable,
    Sessions,
    content_range,
    open_content,
    resolve,
)
from .subtitles import UnknownSession, match_subtitles, read_subtitle

HOST = "127.0.0.1"
HEALTH_TIMEOUT = 2.0
START_TIMEOUT = 20.0
MAX_BODY = 64 * 1024
CHUNK_SIZE = 64 * 1024
# Quark throttles a single large media GET (measured ~0.1 MiB/s for an
# open-ended request versus ~5 MiB/s at 4 MiB), so a bigger ask is split.
MEDIA_SEGMENT = 4 * 1024 * 1024
MAX_PAGE_NUMBER = 1_000_000
EXIT_OK = 0
EXIT_ALREADY_RUNNING = 3
EXIT_UNAVAILABLE = 5
UINT32_MAX = 0xFFFFFFFF
#: How many verified video→directory associations stay in memory. They are only
#: a free lookup cache for a following ``/v1/play``; the persistent parent is
#: written with the recent record itself.
MAX_ASSOCIATIONS = 4096
#: The drive root carries no series identity, so it never earns the "目录名 /"
#: prefix: a movie stored at the root keeps its full name.
ROOT_FID = "0"

#: Watch-center wording. A series whose directory name was never verified keeps
#: this neutral, honest label instead of guessing one from a file name.
WATCH_UNNAMED = "来源目录未命名"
#: The bookmark-window request's fallback label, used only when no local recent
#: or bookmark row names the video; it deliberately carries no FID or name.
BOOKMARK_FALLBACK_NAME = "所选视频"

_CONTROL_ROUTES = frozenset({
    "/v1/auth", "/v1/auth/login", "/v1/auth/logout",
    "/v1/files", "/v1/playlist", "/v1/play", "/v1/shutdown",
    "/v1/search", "/v1/favorites", "/v1/favorites/remove",
    "/v1/recent", "/v1/recent/clear", "/v1/recent/remove", "/v1/continue",
    "/v1/series", "/v1/watch-center", "/v1/mark",
    "/v1/subtitles", "/v1/subtitle",
    "/v1/diagnostics", "/v1/diagnostics/report", "/v1/diagnostics/open",
    "/v1/playback/restart",
    "/v1/bookmarks", "/v1/bookmarks/add", "/v1/bookmarks/rename",
    "/v1/bookmarks/remove", "/v1/bookmarks/jump", "/v1/bookmarks/open",
})


def _api_error_kind(exc: BaseException) -> str:
    """Map a sanitized bridge exception to an allowlisted diagnostics kind."""
    if isinstance(exc, AuthenticationError):
        return "authentication_error"
    if isinstance(exc, RateLimitError):
        return "rate_limited"
    if isinstance(exc, AccessRestrictedError):
        return "access_restricted"
    if isinstance(exc, NetworkError):
        return "network_error"
    if isinstance(exc, MediaChanged):
        return "media_changed"
    if isinstance(exc, RemoteError):
        return "remote_error"
    if isinstance(exc, UsageError):
        return "usage_error"
    if isinstance(exc, LocalIOError):
        return "local_io_error"
    return "internal_error"


def _error_phase(kind: str) -> str:
    """Fixed phase label for a failure kind; never derived from caller text."""
    if kind in ("login_required", "authentication_error"):
        return "credential"
    if kind == "local_io_error":
        return "local"
    if kind == "bad_request":
        return "request"
    if kind in ("rate_limited", "access_restricted", "network_error", "remote_error", "media_changed"):
        return "account"
    return "unknown"


def _parse_pid(value: Any, *, label: str) -> int:
    """Validate one nonzero 32-bit player pid; booleans and floats are refused."""
    if type(value) is not int or isinstance(value, bool) or not 1 <= value <= UINT32_MAX:
        raise UsageError(f"{label} 必须是 1 到 {UINT32_MAX} 之间的整数")
    return value


def _bookmark_id(value: Any) -> str:
    """Validate one caller-supplied bookmark id before any store lookup."""
    if not isinstance(value, str) or not value:
        raise UsageError("id 必须是书签标识")
    return value


def _bookmark_note(value: Any) -> str:
    """Validate a caller-supplied note; the length bound is checked before any write."""
    if not isinstance(value, str):
        raise UsageError("note 必须是字符串")
    if len(value) > NOTE_LIMIT:
        raise UsageError(f"note 最多 {NOTE_LIMIT} 个字符")
    return value


def _mark_keys(snapshot: Mapping[str, Any]) -> set[tuple[str, int]]:
    """Manual watched marks as ``(fid, size)`` so a replaced object is distinct."""
    return {(mark["fid"], mark["size"]) for mark in snapshot["marks"]}


def _watched(entry: Mapping[str, Any], marks: set[tuple[str, int]]) -> bool:
    """A video counts as watched when manually marked or actually played to the end.

    Both the recent/continue/series views and the directory's continue target
    share this rule, so a near-end real progress and an explicit mark agree.
    """
    if (entry["fid"], entry["size"]) in marks:
        return True
    return completed(entry["position_ms"], entry["duration_ms"])


def _dir_label(entry: Mapping[str, Any], names: Mapping[str, str]) -> str:
    """The verified directory name to prefix a row with; empty for root/unknown.

    A root-level file (a movie at the drive root, whose parent is the root FID)
    and a file whose parent is unknown both stay unprefixed, so the original name
    is shown in full and no useless "我的网盘 /" prefix appears.
    """
    parent = entry["parent"]
    if not parent or parent == ROOT_FID:
        return ""
    # A name verified while browsing the directory outranks the one persisted
    # with the row, so a renamed directory shows its new name immediately.
    return names.get(parent, "") or entry["parent_name"]


def _episode_view(entry: Mapping[str, Any], watched: bool, dir_name: str = "") -> dict[str, Any]:
    """One video row shared by the recent, continue, and series views."""
    position, duration = entry["position_ms"], entry["duration_ms"]
    return {
        "fid": entry["fid"],
        "name": entry["name"],
        "is_dir": False,
        "size": entry["size"],
        "parent": entry["parent"],
        "watched": watched,
        "position_ms": position,
        "duration_ms": duration,
        "display": display_name(entry["name"], position, duration, watched, dir_name),
    }


def _favorite_items(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Favorites in stored order, each with its last known episode from recent."""
    marks = _mark_keys(snapshot)
    items = []
    for entry in snapshot["favorites"]:
        last = next((row for row in snapshot["recent"] if row["parent"] == entry["fid"]), None)
        items.append({
            "fid": entry["fid"],
            "name": entry["name"],
            "is_dir": True,
            "size": 0,
            "last_episode": None if last is None else _episode_view(last, _watched(last, marks)),
        })
    return items


def _recent_items(snapshot: Mapping[str, Any], names: Mapping[str, str]) -> list[dict[str, Any]]:
    marks = _mark_keys(snapshot)
    items = []
    for entry in snapshot["recent"]:
        view = _episode_view(entry, _watched(entry, marks), _dir_label(entry, names))
        view["played_at"] = entry["played_at"]
        items.append(view)
    return items


def _continue_items(snapshot: Mapping[str, Any], names: Mapping[str, str]) -> list[dict[str, Any]]:
    """Resumable, unfinished, not-yet-watched videos, newest progress first."""
    marks = _mark_keys(snapshot)
    items = [
        _episode_view(entry, False, _dir_label(entry, names))
        for entry in sorted(snapshot["recent"], key=lambda row: row["progress_at"], reverse=True)
        if not _watched(entry, marks) and unfinished(entry["position_ms"], entry["duration_ms"])
    ]
    return items


def _watch_display(title: str, watched: bool, position_ms: int, duration_ms: int) -> str:
    """Title plus the latest episode's own progress; never a directory total.

    The watch center never lists a directory's episodes, so it can show a saved
    point but must never imply how many episodes exist or that a series is
    finished. ``已看`` (no percentage) only means that one episode is watched.
    """
    if watched:
        return f"{title} · 已看"
    percent = progress_text(position_ms, duration_ms)
    return title if not percent else f"{title} · 已看{percent}"


def _watch_center_items(snapshot: Mapping[str, Any], names: Mapping[str, str]) -> list[dict[str, Any]]:
    """Home-screen groups: one per verified directory, one per root/unknown video.

    History is grouped by the verified ``parent`` FID stored with each recent
    row, never by a name, so two directories that share a title stay apart and a
    renamed one keeps its identity. A row whose parent is the drive root or
    unknown has no series identity and stands alone, keyed by its own video FID.
    Groups are ordered by the latest episode's ``max(played_at, progress_at)``;
    the recent list is already newest-played first, so equal stamps keep a
    stable, newest-first order.
    """
    marks = _mark_keys(snapshot)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for entry in snapshot["recent"]:
        parent = entry["parent"]
        series = bool(parent) and parent != ROOT_FID
        kind = "series" if series else "video"
        key = parent if series else entry["fid"]
        group_key = (kind, key)
        bucket = groups.get(group_key)
        if bucket is None:
            bucket = groups[group_key] = {
                "kind": kind,
                "key": key,
                "parent": parent,
                "rows": [],
            }
            order.append(group_key)
        bucket["rows"].append(entry)
    items: list[dict[str, Any]] = []
    for group_key in order:
        bucket = groups[group_key]
        rows = bucket["rows"]
        # ``max`` keeps the first maximal row, and recent is newest-played first,
        # so a tie names the most recently watched episode.
        latest = max(rows, key=lambda row: max(row["played_at"], row["progress_at"]))
        watched = _watched(latest, marks)
        if bucket["kind"] == "series":
            name = names.get(bucket["parent"], "")
            if not name:
                name = next((row["parent_name"] for row in rows if row["parent_name"]), "")
            if not name:
                name = WATCH_UNNAMED
            title = f"{name} · 上次：{latest['name']}"
        else:
            name = latest["name"]
            title = name
        items.append({
            "kind": bucket["kind"],
            "key": bucket["key"],
            "parent": bucket["parent"],
            "name": name,
            "display": _watch_display(
                title, watched, latest["position_ms"], latest["duration_ms"]
            ),
            "latest": _episode_view(latest, watched),
            "unfinished_count": sum(
                1 for row in rows
                if not _watched(row, marks) and unfinished(row["position_ms"], row["duration_ms"])
            ),
            "_stamp": max(latest["played_at"], latest["progress_at"]),
        })
    items.sort(key=lambda item: item["_stamp"], reverse=True)
    for item in items:
        del item["_stamp"]
    return items


def _series_latest(snapshot: Mapping[str, Any], views: Mapping[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The most recently watched episode that is still in this listing, or ``None``.

    Only a recent record whose file is in the listing at the *same size* counts,
    so a stale record from a replaced object never names a "last watched"
    episode. The most recent ``max(played_at, progress_at)`` wins; the recent
    list is newest-played first, so a tie keeps the newest one.
    """
    best: dict[str, Any] | None = None
    best_stamp = -1
    for record in snapshot["recent"]:
        view = views.get(record["fid"])
        if view is None or view["size"] != record["size"]:
            continue
        stamp = max(record["played_at"], record["progress_at"])
        if stamp > best_stamp:
            best_stamp, best = stamp, view
    return best


def _series_next(
    videos: list[dict[str, Any]], views: Mapping[str, dict[str, Any]], latest: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The episode immediately after ``latest`` in natural order, or ``None``.

    Only the listing's own order is used, so a moved or renamed episode cannot
    make a stale FID association, and the last episode never wraps to the first.
    With no latest episode the first unwatched one is offered, else ``None``.
    """
    if latest is not None:
        position = next(
            (index for index, video in enumerate(videos) if video["fid"] == latest["fid"]), None
        )
        if position is not None and position + 1 < len(videos):
            return views[videos[position + 1]["fid"]]
        return None
    return next(
        (views[video["fid"]] for video in videos if not views[video["fid"]]["watched"]), None
    )


def _series_payload(snapshot: Mapping[str, Any], parent: str, videos: list[dict[str, Any]]) -> dict[str, Any]:
    """Continue target, last-watched pair, and per-episode status for one listing.

    The trusted listing is the only source of a video's name, size, and parent:
    a saved recent record contributes only its resume point, so a stale stored
    name or an old unknown parent can never label or re-associate an episode.
    No extra directory is ever scanned; ``latest`` and ``next`` are derived from
    this same listing.
    """
    marks = _mark_keys(snapshot)
    saved = {entry["fid"]: entry for entry in snapshot["recent"]}
    episodes = []
    views: dict[str, dict[str, Any]] = {}
    for video in videos:
        record = saved.get(video["fid"])
        if record is not None and record["size"] != video["size"]:
            # A different size is a different object: its saved point is stale.
            record = None
        position = record["position_ms"] if record is not None else 0
        duration = record["duration_ms"] if record is not None else 0
        entry = {
            "fid": video["fid"],
            "name": video["name"],
            "size": video["size"],
            "parent": parent,
            "position_ms": position,
            "duration_ms": duration,
        }
        watched = _watched(entry, marks)
        view = _episode_view(entry, watched)
        view["progress_at"] = record["progress_at"] if record is not None else 0
        view["unfinished"] = not watched and unfinished(position, duration)
        episodes.append(view)
        views[video["fid"]] = view
    target = select_continue(episodes)
    latest = _series_latest(snapshot, views)
    return {
        "parent": parent,
        "total": len(episodes),
        "watched": sum(1 for episode in episodes if episode["watched"]),
        # A listing with no videos (empty or non-video-only) has nothing to
        # finish: it must not be reported as entirely watched.
        "complete": target is None and bool(episodes),
        "continue": None if target is None else {
            "fid": target["fid"],
            "name": target["name"],
            "position_ms": target["position_ms"],
            "duration_ms": target["duration_ms"],
            "display": target["display"],
        },
        "latest": latest,
        "next": _series_next(videos, views, latest),
        "episodes": [
            {key: episode[key] for key in
             ("fid", "name", "size", "watched", "position_ms", "duration_ms", "display")}
            for episode in episodes
        ],
    }


class _ObservedClient(QuarkClient):
    """Bridge-only observer: reports each ordinary API call's result, sends nothing extra.

    The override wraps the single ``request`` funnel, so a successful management
    call marks the account recently valid and an upstream authentication failure
    marks it expired. Rate-limit/restriction errors carry no expiry signal, and a
    missing local Cookie never reaches here (``Service.client`` raises first), so
    it can never be mistaken for an upstream account failure.
    """

    def __init__(self, cookie: str, *, observer: Diagnostics, **kwargs: Any) -> None:
        super().__init__(cookie, **kwargs)
        self._observer = observer

    def request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        try:
            payload = super().request(method, endpoint, **kwargs)
        except QuarkError as exc:
            self._observer.api_result(False, _api_error_kind(exc))
            raise
        self._observer.api_result(True)
        return payload



def _failure(kind: str, message: str) -> dict[str, Any]:
    return {"error": {"type": kind, "message": message}}


def _status_for(exc: BaseException) -> tuple[int, str]:
    if isinstance(exc, AuthenticationError):
        return 401, "login_required"
    # Typed cooldown errors are never credential failures: the plugin must not
    # clear its Cookie, library or login state for them.
    if isinstance(exc, RateLimitError):
        return 429, "rate_limited"
    if isinstance(exc, AccessRestrictedError):
        return 403, "access_restricted"
    if isinstance(exc, MediaChanged):
        return 409, "media_changed"
    if isinstance(exc, NetworkError):
        return 504, "network_error"
    if isinstance(exc, RemoteError):
        return 502, "remote_error"
    if isinstance(exc, UsageError):
        return 400, "bad_request"
    if isinstance(exc, LocalIOError):
        return 500, "local_io_error"
    return 500, "internal_error"


def _first(query: Mapping[str, list[str]], key: str, default: str | None) -> str | None:
    values = query.get(key)
    if not values or values[0] == "":
        return default
    return values[0]


def _int_param(query: Mapping[str, list[str]], key: str, default: int, minimum: int, maximum: int) -> int:
    raw = _first(query, key, None)
    if raw is None:
        return default
    if not raw.isdigit():
        raise UsageError(f"{key} 必须是整数")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise UsageError(f"{key} 允许范围是 {minimum} 到 {maximum}")
    return value


def _content_range_header(value: str | None) -> tuple[int, int, int] | None:
    if not value or not value.casefold().startswith("bytes "):
        return None
    span, separator, total = value[6:].strip().partition("/")
    if not separator:
        return None
    start, dash, end = span.partition("-")
    if not dash or not start.isdigit() or not end.isdigit() or not total.isdigit():
        return None
    return int(start), int(end), int(total)


def _upstream_plan(
    response: httpx.Response,
    session: PlaySession,
    byte_range: tuple[int, int] | None,
) -> tuple[int, int, str | None]:
    """Validate what the origin sent against the session before any header goes out."""
    if response.status_code not in (200, 206):
        raise RemoteError(f"下载源返回了异常状态 {response.status_code}，已中止播放")
    encoding = response.headers.get("content-encoding")
    if encoding and encoding.casefold() != "identity":
        raise RemoteError("下载源启用了压缩编码，已中止播放")
    announced = response.headers.get("content-length")
    announced_length = int(announced) if announced is not None and announced.isdigit() else None
    if response.status_code == 206:
        parsed = _content_range_header(response.headers.get("content-range"))
        if parsed is None:
            raise MediaChanged("夸克未返回可校验的范围信息，已中止播放")
        start, end, total = parsed
        if start > end or end >= total:
            raise RemoteError("下载源返回的范围信息无效，已中止播放")
        if total != session.size or (byte_range is not None and (start, end) != byte_range):
            raise MediaChanged("文件大小或版本已变化，已中止播放")
        length = end - start + 1
        if announced_length is not None and announced_length != length:
            raise RemoteError("下载源返回的长度与范围不一致，已中止播放")
        return 206, length, f"bytes {start}-{end}/{total}"
    if byte_range is not None:
        raise RemoteError("下载源忽略了分段请求，已中止播放")
    if announced_length is not None and announced_length != session.size:
        raise MediaChanged("文件大小或版本已变化，已中止播放")
    return 200, session.size, None


class Service:
    """Owns the instance token, the Quark session, login state, and play sessions."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        cookie: str | None = None,
        login_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        guard: RequestGuard | None = None,
        native_factory: Callable[[], Any] | None = None,
        status_launcher: Callable[[], Any] | None = None,
        monitor_poll_interval: float = POLL_INTERVAL,
        monitor_start: bool = True,
    ) -> None:
        self.instance = secrets.token_hex(8)
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self.server: ThreadingHTTPServer | None = None
        self.sessions = Sessions(clock)
        self.library = Library()
        self.diagnostics = Diagnostics()
        # The native receiver is created lazily on the monitor's own worker, so
        # no HTTP request thread ever touches it.
        self.monitor = PlaybackMonitor(
            self.library,
            native_factory=native_factory if native_factory is not None else NativePlayer,
            clock=clock,
            poll_interval=monitor_poll_interval,
            start=monitor_start,
        )
        self._transport = transport
        self._cookie = cookie
        # One guard is shared by the bridge, its Quark client and the QR login it
        # starts, so a cooldown survives client rebuilds within this process.
        self._guard = guard if guard is not None else default_guard(transport, None)
        self._login_factory = login_factory or (
            lambda: QrLoginWindow(transport=transport, cookie_path=credential_path(), guard=self._guard)
        )
        # Verified video→directory associations and directory names seen in a
        # listing: used only to stamp a following /v1/play with its real parent
        # and that parent's name. Bounded and memory only; the durable parent and
        # name live on the recent record. Names are never guessed from a window
        # title and never fetched with an extra request.
        self._parents: dict[str, str] = {}
        self._dir_names: dict[str, str] = {}
        self._parent_lock = threading.Lock()
        self._client: QuarkClient | None = None
        self._client_cookie: str | None = None
        self._client_lock = threading.Lock()
        self._login_lock = threading.Lock()
        self._login_thread: threading.Thread | None = None
        self._login_window: QrLoginWindow | None = None
        self._status_launcher = status_launcher
        self._status_process: Any = None
        self._status_lock = threading.Lock()
        # One-shot bookmark-window target handed to the status GUI, plus the lock
        # that serializes setting/clearing it with the two diagnostics opens.
        self._bookmark_lock = threading.Lock()
        self._bookmark_request: dict[str, str] | None = None
        # Serializes a stream's one-time history record against
        # ``/v1/recent/remove``, so a record that already started can never
        # re-create the row the removal just deleted.
        self._history_lock = threading.Lock()
        self.login_state = "idle"
        self.login_message: str | None = None
        self.qr_png: str | None = None

    # -- Quark session ---------------------------------------------------

    def authorized(self, header: str | None) -> bool:
        if not header:
            return False
        scheme, _, value = header.partition(" ")
        return scheme.casefold() == "bearer" and hmac.compare_digest(value.strip(), self.token)

    def client(self) -> QuarkClient:
        """Reuse the Quark session, rebuilding it when the saved credential changed."""
        cookie = self._cookie or load_cookie()
        if not cookie:
            raise AuthenticationError("插件尚未登录夸克网盘，请先扫码登录")
        with self._client_lock:
            if self._client is None or self._client_cookie != cookie:
                if self._client is not None:
                    self._client.close()
                # A genuinely new credential: any previously observed validity
                # (including a stale-cookie expiry) no longer applies.
                self.diagnostics.credential_changed()
                self._client = _ObservedClient(
                    cookie, transport=self._transport, guard=self._guard, observer=self.diagnostics
                )
                self._client_cookie = cookie
            return self._client

    def files(self) -> Files:
        return Files(self.client())

    def note_directory(self, parent: str, items: Any) -> None:
        """Remember the real directory of every video and every sub-directory's name.

        The caller passes an already-fetched listing, so both facts cost no extra
        network request; only a real directory FID and file name from server
        metadata are ever recorded, never a window title or a guess.
        """
        with self._parent_lock:
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                name, fid = item.get("name"), item.get("fid")
                if not isinstance(fid, str) or not fid or not isinstance(name, str) or not name:
                    continue
                if item.get("is_dir"):
                    self._dir_names.pop(fid, None)
                    self._dir_names[fid] = name
                    while len(self._dir_names) > MAX_ASSOCIATIONS:
                        self._dir_names.pop(next(iter(self._dir_names)))
                    continue
                if not parent or not is_video(name):
                    continue
                self._parents.pop(fid, None)
                self._parents[fid] = parent
                while len(self._parents) > MAX_ASSOCIATIONS:
                    self._parents.pop(next(iter(self._parents)))

    def parent_of(self, fid: str) -> str:
        """The last verified directory of ``fid``, or ``""`` when unknown."""
        with self._parent_lock:
            return self._parents.get(fid, "")

    def directory_names(self) -> dict[str, str]:
        """Directory FID→name from listings already read plus favorites; no request.

        Favorites are local state whose name was verified by ``file/info`` when the
        directory was added, so they complete a known parent of an older record
        without probing the drive again.
        """
        with self._parent_lock:
            names = dict(self._dir_names)
        for entry in self.library.favorites():
            names.setdefault(entry["fid"], entry["name"])
        return names

    def parent_name_of(self, fid: str) -> str:
        """The verified name of ``fid``'s directory, or ``""`` when unknown.

        A root or unknown parent yields no name, so a top-level movie keeps its
        full original name instead of gaining a "我的网盘 /" prefix.
        """
        parent = self.parent_of(fid)
        if not parent or parent == ROOT_FID:
            return ""
        return self.directory_names().get(parent, "")

    def require_login(self) -> None:
        """Reject a control call whose data belongs to a signed-in plugin."""
        if not self.authenticated():
            raise AuthenticationError("插件尚未登录夸克网盘，请先扫码登录")

    def drop_client(self) -> None:
        with self._client_lock:
            client, self._client = self._client, None
            self._client_cookie = None
        if client is not None:
            client.close()

    def close(self) -> None:
        """Stop the bridge: flush/close the monitor, then the QR window and client."""
        self.monitor.close()
        with self._login_lock:
            window, self._login_window = self._login_window, None
        if isinstance(window, QrLoginWindow):
            window.request_close()
        self.drop_client()

    def authenticated(self) -> bool:
        if self._cookie:
            return True
        try:
            return load_cookie() is not None
        except LocalIOError:
            return False

    # -- login -----------------------------------------------------------

    def _login_payload_locked(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated(),
            "login_state": self.login_state,
            "login_message": self.login_message,
            "qr_png": self.qr_png,
        }

    def login_payload(self) -> dict[str, Any]:
        with self._login_lock:
            return self._login_payload_locked()

    def start_login(self) -> dict[str, Any]:
        """Start one QR login; a repeated call while it runs reports the same state."""
        # Refuse before any GUI is created: a shared cooldown means the account
        # or upstream is currently rejecting requests, not that login is broken.
        self._guard.check_active_cooldown()
        with self._login_lock:
            if self._login_thread is not None and self._login_thread.is_alive():
                return self._login_payload_locked()
            self.login_state = "waiting"
            self.login_message = None
            self.qr_png = None
            thread = threading.Thread(target=self._run_login, name="quark-potplayer-login", daemon=True)
            self._login_thread = thread
        thread.start()
        return self.login_payload()

    def _run_login(self) -> None:
        state, message = "failed", None
        try:
            login = self._login_factory()
            with self._login_lock:
                self._login_window = login
            login.run(on_qr_ready=self._qr_ready)
        except LoginCancelled:
            state = "idle"
        except QuarkError as exc:
            message = str(exc) or "扫码登录失败"
        except OSError:
            message = "扫码登录过程失败"
        else:
            state = "success"
        with self._login_lock:
            self.login_state = state
            self.login_message = message
            self.qr_png = None
            self._login_thread = None
            self._login_window = None
        if state == "success":
            self.drop_client()

    def _qr_ready(self, path: Any) -> None:
        with self._login_lock:
            self.qr_png = str(path)

    def invalidate_credential(self) -> None:
        """Forget a plugin credential the account API proved stale.

        Covers both the in-process injected Cookie and the saved plugin file, so
        ``authenticated`` turns false and the extension re-opens the QR login.
        Only the bridge's own credential is removed; the ``quark`` CLI Cookie is
        never touched.
        """
        self._cookie = None
        self.drop_client()
        try:
            clear_cookie()
        except LocalIOError:
            pass

    def logout(self) -> dict[str, Any]:
        """Clear only the bridge's own credential; the CLI Cookie stays untouched.

        An explicit logout also drops the favorites, recent lists and playback
        bookmarks; a merely expired credential (``invalidate_credential``) keeps
        all of them. Monitor bindings are suppressed before the library is
        cleared, so a poll that was already in flight cannot resurrect the
        progress (or a late bookmark capture) logout just removed.
        """
        self.invalidate_credential()
        self.monitor.clear()
        self.monitor.bookmarks.clear()
        self.library.clear()
        with self._login_lock:
            self.login_state = "idle"
            self.login_message = None
            self.qr_png = None
        self.diagnostics.credential_changed()
        return {"authenticated": False}

    # -- diagnostics window ----------------------------------------------

    def open_status_window(self) -> str:
        """Reuse a live diagnostics window, else spawn one; never blocks on it.

        The child is an independent GUI process (``--status-window``) that reads
        the private runtime record itself; no token, URL or credential is placed
        on its command line. Closing the window does not stop media or the bridge.
        """
        with self._status_lock:
            process = self._status_process
            if process is not None and process.poll() is None:
                return "already_open"
            launcher = self._status_launcher or _spawn_status_window
            try:
                self._status_process = launcher()
            except OSError:
                raise LocalIOError("无法打开播放状态与诊断窗口") from None
            return "opened"

    def open_plain_status_window(self) -> str:
        """Open/reuse the status window and drop any stale bookmark target.

        A plain entry means the operator just wants the diagnostics window, so
        the pending bookmark request is cleared under the lock shared with
        :meth:`request_bookmark_window`; the GUI then never re-opens the video an
        earlier request pointed at.
        """
        with self._bookmark_lock:
            self._bookmark_request = None
            return self.open_status_window()

    def bookmark_request(self) -> dict[str, str] | None:
        """A copy of the pending bookmark-window request, or ``None``."""
        with self._bookmark_lock:
            request = self._bookmark_request
            return None if request is None else dict(request)

    def request_bookmark_window(self, fid: str) -> str:
        """Retain one bookmark-window target, then open/reuse the status window.

        The name is read from local state only (the recent row, else the local
        bookmark document, else a FID-neutral label): no cloud request is made.
        The request is stored before the window is spawned, so the GUI can pick
        it up; a spawn failure clears it again, leaving no stale target behind.
        """
        request = {"id": secrets.token_hex(16), "fid": fid, "name": self._bookmark_target_name(fid)}
        with self._bookmark_lock:
            self._bookmark_request = request
            try:
                return self.open_status_window()
            except LocalIOError:
                self._bookmark_request = None
                raise

    def _bookmark_target_name(self, fid: str) -> str:
        """The best local name for ``fid``: recent row, then bookmark, else neutral."""
        for row in self.library.snapshot()["recent"]:
            if row["fid"] == fid and row["name"]:
                return row["name"]
        for entry in self.monitor.bookmarks.list(fid):
            if entry["name"]:
                return entry["name"]
        return BOOKMARK_FALLBACK_NAME

    # -- history ---------------------------------------------------------

    def record_played(self, session: PlaySession) -> None:
        """Write the one history row a started stream owns, under the history lock.

        ``Sessions.remove_history`` marks a session whose history was deleted; the
        lock is shared with :meth:`remove_history`, so a late first chunk or a
        repeated range GET of that session can never re-create the row, while a
        record that already started either finishes and is deleted afterwards or
        sees the mark and never writes. A brand-new session (a new explicit
        ``/v1/play``) is unmarked and records normally. A corrupt library raises
        ``LocalIOError`` here; it is left to propagate.
        """
        with self._history_lock:
            if session.history_removed:
                return
            self.library.record_recent(
                session.fid, session.name, session.size,
                self.parent_of(session.fid), self.parent_name_of(session.fid),
            )
            self.monitor.stream_started(session.session)

    def remove_history(self, fid: str) -> None:
        """Forget one video's recent row, blocking any live stream from restoring it.

        The sessions of ``fid`` are marked, the monitor suppresses exactly that
        FID's bindings (draining any in-flight write), and only then is the row
        deleted -- all under the history lock, so an already-running record is
        deleted while a later one never starts. Marking the sessions inside the
        same critical section (rather than before it) closes the window in which
        a concurrent new session could begin an unmarked record. Other videos'
        bindings, favorites, marks and bookmarks are untouched, and no player is
        seeked or stopped.
        """
        with self._history_lock:
            self.sessions.remove_history(fid)
            self.monitor.forget_fid(fid)
            self.library.remove_recent(fid)

    def new_session(self, media: Any) -> PlaySession:
        """Create one play session under the history lock.

        Serializing creation with :meth:`remove_history` means no stream for a
        FID can begin between that removal's session marking and its row
        deletion, so an in-flight record can never resurrect the removed row. A
        session created after the removal is a genuinely new explicit
        ``/v1/play`` and records normally.
        """
        with self._history_lock:
            return self.sessions.create(media)

    # -- lifecycle -------------------------------------------------------

    def build_server(self) -> ThreadingHTTPServer:
        server = _Server((HOST, 0), _Handler, self)
        self.server = server
        self.port = int(server.server_address[1])
        return server

    def stop(self) -> None:
        server = self.server
        if server is None:
            return
        threading.Thread(target=server.shutdown, name="quark-potplayer-stop", daemon=True).start()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], service: Service) -> None:
        self.service = service
        super().__init__(address, handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"quark-potplayer/{SERVICE_VERSION}"
    sys_version = ""

    @property
    def service(self) -> Service:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        """Stay silent: request lines can contain session ids and file names."""

    def do_GET(self) -> None:  # noqa: N802 - http.server dispatch name
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    # -- routing ---------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        raw = self._consume_body()
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            return self._json(400, _failure("bad_request", "请求地址无效"))
        route, query = parsed.path, parse_qs(parsed.query, keep_blank_values=True)
        try:
            if route == "/v1/health":
                if method not in ("GET", "HEAD"):
                    return self._json(405, _failure("method_not_allowed", "该接口只接受 GET 或 HEAD"))
                return self._json(200, {
                    "status": "ok",
                    "version": SERVICE_VERSION,
                    "instance": self.service.instance,
                })
            if route.startswith("/v1/stream/"):
                if method not in ("GET", "HEAD"):
                    return self._json(405, _failure("method_not_allowed", "该接口只接受 GET 或 HEAD"))
                return self._stream(route)
            if not self.service.authorized(self.headers.get("Authorization")):
                return self._json(401, _failure("unauthorized", "本地访问令牌无效"))
            return self._control(route, method, query, raw)
        except QuarkError as exc:
            self._failure_response(exc)
        except httpx.HTTPError:
            self._failure_response(RemoteError("上游传输失败"))

    def _control(self, route: str, method: str, query: Mapping[str, list[str]], raw: bytes | None) -> None:
        service = self.service
        if route == "/v1/auth":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            return self._json(200, service.login_payload())
        if route == "/v1/auth/login":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            return self._json(202, service.start_login())
        if route == "/v1/auth/logout":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            return self._json(200, service.logout())
        if route == "/v1/files":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            parent = require_fid(_first(query, "parent", "0"), label="parent", allow_root=True)
            number = _int_param(query, "page", 1, 1, MAX_PAGE_NUMBER)
            size = _int_param(query, "size", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
            payload = page(service.files(), parent, number, size)
            service.note_directory(parent, payload["items"])
            return self._json(200, payload)
        if route == "/v1/playlist":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            parent_value = _first(query, "parent", None)
            start_value = _first(query, "start", None)
            parent = require_fid(parent_value, label="parent", allow_root=True) if parent_value is not None else None
            start = require_fid(start_value, label="start") if start_value is not None else None
            resolved, items = playlist(service.files(), parent, start)
            service.note_directory(resolved, items)
            return self._json(200, {"parent": resolved, "items": items})
        if route == "/v1/play":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            body = self._body(raw)
            fid = require_fid(body.get("fid"), label="fid")
            # Validate the optional pid before any Quark request: a bad pid must
            # never trigger an upstream call or a media session.
            pid_value = body.get("player_pid")
            player_pid = None if pid_value is None else _parse_pid(pid_value, label="player_pid")
            media = resolve(service.files(), service.client(), fid)
            session = service.new_session(media)
            service.diagnostics.media_started(session.session, session.name)
            relative = f"/v1/stream/{session.session}/{quote(session.name, safe='')}"
            if player_pid is not None:
                # Bind the native pid to the exact loopback address the player is
                # handed. A storage/native failure here must never block playback.
                try:
                    service.monitor.bind(session, player_pid, f"http://{HOST}:{service.port}{relative}")
                except QuarkError:
                    pass
            return self._json(200, {"url": relative, "title": session.name})
        if route == "/v1/diagnostics":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            payload = service.diagnostics.snapshot(
                credential_present=service.authenticated(), guard=service._guard
            )
            payload["playback"] = service.monitor.snapshot()
            payload["bookmark_request"] = service.bookmark_request()
            return self._json(200, payload)
        if route == "/v1/diagnostics/report":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            return self._json(200, service.diagnostics.report(
                credential_present=service.authenticated(), guard=service._guard
            ))
        if route == "/v1/diagnostics/open":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            return self._json(202, {"status": service.open_plain_status_window()})
        if route == "/v1/playback/restart":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            body = self._body(raw)
            player_pid = _parse_pid(body.get("player_pid"), label="player_pid")
            fid = require_fid(body.get("fid"), label="fid")
            try:
                restarted = service.monitor.restart(player_pid, fid)
            except UsageError:
                restarted = False
            if not restarted:
                # Explicit JSON conflict; this is never a credential failure.
                return self._json(409, _failure("media_changed", "未找到可从头开始的播放绑定"))
            return self._json(200, {"status": "restarted"})
        if route == "/v1/shutdown":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            self._json(200, {"status": "stopping"})
            self.wfile.flush()
            service.stop()
            return
        if route == "/v1/search":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            service.require_login()
            keyword = _first(query, "q", None)
            if keyword is None or not keyword.strip():
                raise UsageError("q 不能为空")
            number = _int_param(query, "page", 1, 1, MAX_PAGE_NUMBER)
            size = _int_param(query, "size", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
            return self._json(200, search(service.files(), keyword.strip(), number, size))
        if route == "/v1/favorites":
            if method in ("GET", "HEAD"):
                service.require_login()
                return self._json(200, {"items": _favorite_items(service.library.snapshot())})
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET 或 POST"))
            service.require_login()
            fid = require_fid(self._body(raw).get("fid"), label="fid")
            info = service.files().info(fid)
            if not is_directory(info):
                raise UsageError("fid 不是网盘目录")
            name = info.get("file_name")
            if not isinstance(name, str) or not name:
                raise RemoteError("目录详情缺少有效名称")
            service.library.add_favorite(fid, name)
            return self._json(200, {"items": _favorite_items(service.library.snapshot())})
        if route == "/v1/favorites/remove":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            service.require_login()
            fid = require_fid(self._body(raw).get("fid"), label="fid")
            service.library.remove_favorite(fid)
            return self._json(200, {"items": _favorite_items(service.library.snapshot())})
        if route == "/v1/recent":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            service.require_login()
            return self._json(200, {"items": _recent_items(service.library.snapshot(), service.directory_names())})
        if route == "/v1/continue":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            service.require_login()
            return self._json(200, {"items": _continue_items(service.library.snapshot(), service.directory_names())})
        if route == "/v1/watch-center":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            # Local-only: the watch center is built from the private document and
            # already-verified directory names; it needs no login and no request.
            snapshot = service.library.snapshot()
            return self._json(200, {"items": _watch_center_items(snapshot, service.directory_names())})
        if route == "/v1/series":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            service.require_login()
            parent = require_fid(_first(query, "parent", None), label="parent", allow_root=True)
            resolved, videos = playlist(service.files(), parent, None)
            service.note_directory(resolved, videos)
            return self._json(200, _series_payload(service.library.snapshot(), resolved, videos))
        if route == "/v1/mark":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            service.require_login()
            body = self._body(raw)
            fid = require_fid(body.get("fid"), label="fid")
            parent = require_fid(body.get("parent"), label="parent", allow_root=True)
            watched = body.get("watched")
            if type(watched) is not bool:
                raise UsageError("watched 必须是布尔值")
            # The directory listing is the only authority for the video's real
            # type and size; no client-supplied name or size is trusted.
            resolved, videos = playlist(service.files(), parent, None)
            match = next((video for video in videos if video["fid"] == fid), None)
            if match is None:
                raise UsageError("fid 不是该目录中的视频文件")
            if watched:
                service.library.mark_watched(fid, match["name"], match["size"])
            else:
                service.library.mark_unwatched(fid)
            return self._json(200, {"fid": fid, "watched": watched})
        if route == "/v1/recent/clear":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            service.require_login()
            service.monitor.clear()
            service.library.clear_recent()
            return self._json(200, {"items": []})
        if route == "/v1/recent/remove":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            # Local-only: deleting one row needs no login and no cloud request.
            fid = require_fid(self._body(raw).get("fid"), label="fid")
            service.remove_history(fid)
            return self._json(200, {"status": "removed"})
        if route == "/v1/subtitles":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            service.require_login()
            try:
                payload = match_subtitles(
                    service.files(), service.sessions,
                    _first(query, "fid", None), _first(query, "session", None),
                )
            except UnknownSession:
                return self._json(404, _failure("not_found", "播放会话不存在或已失效"))
            return self._json(200, payload)
        if route == "/v1/subtitle":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            service.require_login()
            fid = require_fid(_first(query, "fid", None), label="fid")
            return self._text(200, read_subtitle(service.files(), service.client(), fid))
        if route == "/v1/bookmarks":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            # Local-only: the plugin's own bookmark document needs no Quark login.
            fid = require_fid(_first(query, "fid", None), label="fid")
            return self._json(200, {"items": service.monitor.bookmarks.list(fid)})
        if route == "/v1/bookmarks/add":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            body = self._body(raw)
            player_pid = _parse_pid(body.get("player_pid"), label="player_pid")
            fid = require_fid(body.get("fid"), label="fid")
            note = _bookmark_note(body.get("note"))
            # The stored timestamp, file name and size come from a fresh native
            # sample taken on the monitor's worker thread: no client field and no
            # cloud request is trusted or made here.
            bookmark = service.monitor.add_bookmark(player_pid, fid, note)
            if bookmark is None:
                return self._json(409, _failure("media_changed", "未找到可保存的播放位置"))
            return self._json(200, {"bookmark": bookmark})
        if route == "/v1/bookmarks/rename":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            body = self._body(raw)
            bookmark_id = _bookmark_id(body.get("id"))
            note = _bookmark_note(body.get("note"))
            if not service.monitor.bookmarks.rename(bookmark_id, note):
                return self._json(404, _failure("not_found", "书签不存在"))
            return self._json(200, {"status": "renamed"})
        if route == "/v1/bookmarks/remove":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            bookmark_id = _bookmark_id(self._body(raw).get("id"))
            if not service.monitor.bookmarks.remove(bookmark_id):
                return self._json(404, _failure("not_found", "书签不存在"))
            return self._json(200, {"status": "removed"})
        if route == "/v1/bookmarks/open":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            # Local-only: the target name is read from local state, no login and
            # no cloud request are needed to open the bookmark window.
            fid = require_fid(self._body(raw).get("fid"), label="fid")
            return self._json(202, {"status": service.request_bookmark_window(fid)})
        if route == "/v1/bookmarks/jump":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            body = self._body(raw)
            player_pid = _parse_pid(body.get("player_pid"), label="player_pid")
            fid = require_fid(body.get("fid"), label="fid")
            bookmark_id = _bookmark_id(body.get("id"))
            # The seek only ever runs on the monitor's worker and only against
            # the already-bound player: no cloud request is made.
            if not service.monitor.jump_bookmark(player_pid, fid, bookmark_id):
                return self._json(409, _failure("media_changed", "未能跳转到该播放位置"))
            return self._json(200, {"status": "jumped"})
        if route in _CONTROL_ROUTES:
            return self._json(405, _failure("method_not_allowed", "请求方法不受支持"))
        return self._json(404, _failure("not_found", "未知接口"))

    # -- playback --------------------------------------------------------

    def _stream(self, route: str) -> None:
        parts = route.split("/")
        if len(parts) < 4 or not parts[3]:
            return self._json(400, _failure("bad_request", "播放地址无效"))
        session = self.service.sessions.get(parts[3])
        if session is None:
            return self._json(404, _failure("not_found", "播放会话不存在或已失效"))
        try:
            byte_range = content_range(self.headers.get("Range"), session.size)
        except RangeNotSatisfiable:
            return self._json(
                416,
                _failure("range_not_satisfiable", "请求范围超出文件大小"),
                extra={"Content-Range": f"bytes */{session.size}"},
            )
        if self.command == "HEAD":
            length = session.size if byte_range is None else byte_range[1] - byte_range[0] + 1
            self._stream_headers(session, byte_range, length)
            return
        self._stream_body(session, byte_range)

    def _stream_body(self, session: PlaySession, byte_range: tuple[int, int] | None) -> None:
        service = self.service
        diagnostics = service.diagnostics

        def refetch() -> None:
            media = resolve(service.files(), service.client(), session.fid)
            if media.size != session.size:
                service.sessions.forget(session.session)
                raise MediaChanged("文件大小或版本已变化，已中止播放")
            service.sessions.refresh(session.session, media)

        def on_origin(status: int, elapsed_ms: int) -> None:
            # One fixed, URL-free observation per real origin response/redirect.
            diagnostics.origin_response(session.session, status, elapsed_ms)

        # The player keeps one GET open for the whole episode, so a half-watched
        # title still counts as played. Record the moment the first chunk leaves
        # the socket; one flag shared by every segment keeps it to one write, and
        # a later disconnect or truncation never erases the entry.
        recorded = False

        def record_once() -> None:
            nonlocal recorded
            if not recorded:
                recorded = True
                self._record_played(session)
                service.monitor.stream_started(session.session)

        def on_chunk(count: int) -> None:
            # Counted only after a successful socket write; every chunk, including
            # a repeated seek over the same range, counts again.
            diagnostics.transferred(session.session, count)

        sent = False
        start, end = byte_range if byte_range is not None else (0, session.size - 1)
        try:
            if end - start + 1 > MEDIA_SEGMENT:
                # One large ask is throttled by the origin; take the same bytes
                # in serial bounded ranges and forward each as it arrives.
                position = start
                while position <= end:
                    stop = min(position + MEDIA_SEGMENT - 1, end)
                    with open_content(
                        service.client(), session, (position, stop), refetch, on_origin=on_origin
                    ) as response:
                        _, length, _ = _upstream_plan(response, session, (position, stop))
                        self._pin_version(session, response)
                        if not sent:
                            self._stream_headers(session, byte_range, end - start + 1)
                            sent = True
                        if not self._forward(response, length, on_started=record_once, on_chunk=on_chunk):
                            return
                    position = stop + 1
                return
            with open_content(
                service.client(), session, byte_range, refetch, on_origin=on_origin
            ) as response:
                status, length, header = _upstream_plan(response, session, byte_range)
                self._pin_version(session, response)
                self._stream_headers(session, byte_range if status == 206 else None, length, status=status, header=header)
                sent = True
                self._forward(response, length, on_started=record_once, on_chunk=on_chunk)
        except (QuarkError, httpx.HTTPError) as exc:
            if sent:
                # The body is already in flight; only an aborted connection is
                # honest here, but the failure is still logged as a fixed event.
                self.close_connection = True
                self._record_failure(exc, phase="origin")
            else:
                self._failure_response(exc, phase="origin")

    def _record_played(self, session: PlaySession) -> None:
        """One disk write per started media response, never per chunk or segment.

        A corrupt library raises ``LocalIOError`` here; it is left to propagate
        so the original ``QuarkError`` path aborts the stream instead of the
        library being silently reset, and ``/v1/recent``/``/v1/favorites`` keep
        reporting the stored document as broken. The service owns the write so
        ``/v1/recent/remove`` can serialize against it.
        """
        self.service.record_played(session)

    def _record_failure(self, exc: BaseException, *, phase: str | None = None) -> None:
        """Log one fixed, sanitized failure event; never a raw exception or URL."""
        status, kind = _status_for(exc)
        if kind == "local_io_error":
            phase = "local"
        elif phase is None:
            phase = _error_phase(kind)
        self.service.diagnostics.error(phase, kind, status)

    def _pin_version(self, session: PlaySession, response: httpx.Response) -> None:
        """Bind the first strong ETag; any later different strong ETag aborts."""
        etag = response.headers.get("etag")
        if etag and not etag.startswith("W/") and not self.service.sessions.pin_etag(session.session, etag):
            raise MediaChanged("文件版本已变化，已中止播放")

    def _stream_headers(
        self,
        session: PlaySession,
        byte_range: tuple[int, int] | None,
        length: int,
        *,
        status: int | None = None,
        header: str | None = None,
    ) -> None:
        if status is None:
            status = 206 if byte_range is not None else 200
        self.send_response(status)
        self.send_header("Content-Type", content_type(session.name))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if header is not None:
            self.send_header("Content-Range", header)
        elif byte_range is not None:
            self.send_header("Content-Range", f"bytes {byte_range[0]}-{byte_range[1]}/{session.size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _forward(
        self,
        response: httpx.Response,
        length: int,
        *,
        on_started: Callable[[], None] | None = None,
        on_chunk: Callable[[int], None] | None = None,
    ) -> bool:
        """Copy exactly ``length`` bytes; ``False`` means the body stopped short.

        ``on_started`` runs once, right after the first non-empty chunk is
        written to the socket, so callers can react before the body completes.
        ``on_chunk`` runs after every successful socket write with the number of
        bytes written, so callers can count real transfer.

        A write to the client socket failing is a normal player disconnect (a
        seek or close), so nothing is logged for it; a failed origin read or an
        origin body that ends early is logged as one fixed event instead.
        """
        written = 0
        try:
            for chunk in response.iter_raw(CHUNK_SIZE):
                if not chunk:
                    continue
                remaining = length - written
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                self.wfile.write(chunk)
                written += len(chunk)
                if on_chunk is not None:
                    on_chunk(len(chunk))
                if on_started is not None:
                    on_started()
                    on_started = None
                if written >= length:
                    break
        except httpx.HTTPError:
            self.close_connection = True
            self.service.diagnostics.error("origin", "network_error")
            return False
        except OSError:
            # The player closed the connection (seek/close): not a failure.
            self.close_connection = True
            return False
        if written < length:
            # The origin ended early; the announced Content-Length can no longer hold.
            self.close_connection = True
            self.service.diagnostics.error("origin", "remote_error")
            return False
        return True

    # -- primitives ------------------------------------------------------

    def _consume_body(self) -> bytes | None:
        """Read the announced body once, bounded, so keep-alive stays clean.

        Control requests (``auth/login``, ``logout``, ``shutdown``) ignore their
        body, but leaving it unread corrupts the next request on the same
        connection. A body over ``MAX_BODY`` is refused without draining and the
        connection is closed instead, since the remainder cannot be trusted.
        """
        header = self.headers.get("Content-Length")
        if header is None:
            return None
        if not header.isdigit():
            self.close_connection = True
            return None
        length = int(header)
        if length <= 0:
            return b""
        if length > MAX_BODY:
            self.close_connection = True
            return None
        return self.rfile.read(length)

    def _body(self, raw: bytes | None) -> dict[str, Any]:
        if raw is None:
            raise UsageError("请求体长度无效")
        if not raw:
            raise UsageError("请求体不能为空")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise UsageError("请求体必须是 JSON 对象") from None
        if not isinstance(payload, dict):
            raise UsageError("请求体必须是 JSON 对象")
        return payload

    def _json(self, status: int, payload: Mapping[str, Any], extra: Mapping[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _failure_response(self, exc: BaseException, *, phase: str | None = None) -> None:
        status, kind = _status_for(exc)
        if kind == "login_required":
            # The account API (not the origin) rejected the Cookie: the plugin's
            # own credential is stale, so drop it and ask the player to re-scan.
            self.service.invalidate_credential()
            message = "插件未登录或登录已失效，请在播放器中重新扫码登录"
        else:
            message = str(exc) or "服务内部错误"
        # One fixed, sanitized event per terminal failure; no raw exception text.
        self._record_failure(exc, phase=phase)
        retry_after = getattr(exc, "retry_after", None)
        extra = None
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) and retry_after > 0:
            extra = {"Retry-After": str(math.ceil(retry_after))}
        try:
            self._json(status, _failure(kind, message), extra=extra)
        except OSError:
            self.close_connection = True


def run_service(*, transport: httpx.BaseTransport | None = None, cookie: str | None = None) -> int:
    """Serve in this process; returns ``EXIT_ALREADY_RUNNING`` when one is live."""
    with ExitStack() as stack:
        lock = acquire_service_lock()
        if lock is None:
            return EXIT_ALREADY_RUNNING
        stack.push(lock)
        service = Service(transport=transport, cookie=cookie)
        server = service.build_server()
        write_runtime({
            "version": SERVICE_VERSION,
            "instance": service.instance,
            "port": service.port,
            "token": service.token,
            "pid": os.getpid(),
            "started_at": int(time.time()),
        })
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            clear_runtime(service.instance)
            service.close()
    return EXIT_OK


def _healthy(port: int, instance: str) -> bool:
    try:
        response = httpx.get(f"http://{HOST}:{port}/v1/health", timeout=HEALTH_TIMEOUT)
        if response.status_code != 200:
            return False
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok" and payload.get("instance") == instance


def _healthy_runtime() -> dict[str, Any] | None:
    entry = read_runtime()
    if not entry:
        return None
    port, token, instance = entry.get("port"), entry.get("token"), entry.get("instance")
    valid = (
        isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535
        and isinstance(token, str) and token
        and isinstance(instance, str) and instance
    )
    if not valid or not _healthy(port, instance):
        return None
    return entry


def _detached_options() -> dict[str, Any]:
    """Spawn options for a background process with no visible console on Windows."""
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        options["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    return options


def _spawn_service(executable: str) -> None:
    try:
        subprocess.Popen([executable, "-m", "quark_cli.potplayer", "--serve"], **_detached_options())
    except OSError:
        raise LocalIOError("无法启动夸克网盘本地服务进程") from None


def _spawn_status_window() -> Any:
    """Start the standalone GUI in its own process; it reads the private runtime itself.

    No token, credential or URL is placed on the command line. The frozen entry
    drops the ``-m quark_cli.potplayer`` selector, and ``--status-window`` only
    opens the window: it never starts the service or a login.
    """
    return subprocess.Popen(
        [sys.executable or "python", "-m", "quark_cli.potplayer", "--status-window"], **_detached_options()
    )


def ensure_service(
    *,
    executable: str | None = None,
    timeout: float = START_TIMEOUT,
    spawn: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    """Return the live instance's ``{base_url, token}``, starting it when needed."""
    entry = _healthy_runtime()
    if entry is None:
        (spawn or _spawn_service)(executable or sys.executable or "python")
        deadline = time.monotonic() + timeout
        while entry is None:
            if time.monotonic() >= deadline:
                raise LocalIOError("夸克网盘本地服务未能在限定时间内就绪")
            sleep(0.1)
            entry = _healthy_runtime()
    return {"base_url": f"http://{HOST}:{entry['port']}", "token": entry["token"]}


def stop_service() -> int:
    """Ask the recorded instance to stop; a missing instance counts as stopped."""
    entry = read_runtime()
    if not entry:
        return EXIT_OK
    port, token = entry.get("port"), entry.get("token")
    if not isinstance(port, int) or not isinstance(token, str):
        return EXIT_UNAVAILABLE
    try:
        response = httpx.post(
            f"http://{HOST}:{port}/v1/shutdown",
            headers={"Authorization": f"Bearer {token}"},
            timeout=HEALTH_TIMEOUT,
        )
    except httpx.HTTPError:
        return EXIT_UNAVAILABLE
    return EXIT_OK if response.status_code == 200 else EXIT_UNAVAILABLE
