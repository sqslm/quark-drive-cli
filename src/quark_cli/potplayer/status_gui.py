"""Standalone Tk status window for the PotPlayer bridge (release 0.7.0).

The service owns the bridge; this module only *observes* it. It loads the
bridge's private runtime record (``plugin_state.read_runtime``), confirms that
the recorded loopback port answers ``/v1/health`` with the same instance id, and
then reuses the recorded Bearer token for a fixed set of read-only endpoints:

* ``GET  /v1/diagnostics``         one request per second while the window is open
* ``GET  /v1/diagnostics/report``  only when the operator exports a report
* ``POST /v1/playback/restart``    only when the operator asks for it
* ``GET  /v1/bookmarks``           only for the selected binding, on selection
                                   change or an explicit operator action
* ``POST /v1/bookmarks/{add,rename,remove,jump}``
                                   only when the operator acts in the bookmark
                                   window; these are local bridge calls, so no
                                   cloud login is required and the diagnostics
                                   report never contains bookmarks or notes

Nothing else is ever fetched, no host other than ``127.0.0.1`` is ever
contacted, and no Cookie, token, URL or raw upstream text is shown or logged.

Threading follows :mod:`quark_cli.potplayer.login_gui`: every Tk call stays on
the thread that created the window, all loopback HTTP and the report file write
run on one worker thread, and the two sides talk through queues polled by
``root.after``. Closing the window cancels the worker with a bounded join and
releases every Tk object on the owning thread, so no async handler can be
finalised from another thread.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.parse import quote

import httpx

from ..errors import LocalIOError
from . import plugin_state

# The bridge binds loopback only; the endpoint is always rebuilt from this
# literal plus a validated port, never from a value found in the runtime file.
HOST = "127.0.0.1"
HEALTH_PATH = "/v1/health"
SNAPSHOT_PATH = "/v1/diagnostics"
REPORT_PATH = "/v1/diagnostics/report"
RESTART_PATH = "/v1/playback/restart"
BOOKMARKS_PATH = "/v1/bookmarks"
BOOKMARKS_ADD_PATH = "/v1/bookmarks/add"
BOOKMARKS_RENAME_PATH = "/v1/bookmarks/rename"
BOOKMARKS_REMOVE_PATH = "/v1/bookmarks/remove"
BOOKMARKS_JUMP_PATH = "/v1/bookmarks/jump"

POLL_INTERVAL = 1.0
REQUEST_TIMEOUT = 3.0
EXIT_OK = 0
EXIT_UNAVAILABLE = 5

MAX_ERROR_ROWS = 20
WINDOW_WIDTH = 800
WINDOW_HEIGHT = 700
MIN_WIDTH = 640
MIN_HEIGHT = 520

# Visible rows in the per-session transfer table; more rows scroll.
TRANSFER_ROWS = 5

# The bookmark manager is a separate top-level window: the status window is
# already full, and bookmark work must never hide the diagnostics tables.
BOOKMARK_WIDTH = 560
BOOKMARK_HEIGHT = 360
BOOKMARK_MIN_WIDTH = 420
BOOKMARK_MIN_HEIGHT = 260
BOOKMARK_NOTICE_WRAP = 500

# Longest bookmark note accepted by the bridge (``bookmark_state.NOTE_LIMIT``);
# an empty note is valid and is shown as the bookmark's own timestamp.
MAX_NOTE_CHARS = 200

# Ordered Chinese UI fonts shipped with Windows; the first installed one wins.
PREFERRED_FONTS = ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "SimSun")

_MIB = 1024 * 1024
_GIB = 1024 * 1024 * 1024

_VALIDATION_LABELS = {
    "no_login": "未登录",
    "saved_unverified": "凭证已保存，待验证",
    "recently_valid": "最近调用成功",
    "expired": "登录已失效",
}

_STATE_LABELS = {
    "waiting": "等待播放开始",
    "playing": "正在播放",
    "paused": "已暂停",
    "stopped": "已停止",
    "missing": "未检测到播放器或媒体",
    "mismatch": "播放器中的文件与登记视频不一致",
    "suspended": "同一文件在多个播放器中打开，已暂停记录进度",
    "unavailable": "当前系统不支持读取播放器进度",
    "error": "读取播放器进度失败",
}

# States in which the player no longer holds the monitored file: only the other
# states offer a bookmark, a jump or "从头开始". ``suspended`` is a same-FID
# multi-instance conflict and ``mismatch`` a player showing another file, so a
# native command must not be offered for either.
_INACTIVE_STATES = frozenset(
    {"stopped", "missing", "mismatch", "suspended", "unavailable", "error"}
)

_COOLDOWN_LABELS = {
    "rate_limited": "上游限流",
    "access_restricted": "上游拒绝访问",
    "service_unavailable": "上游暂时不可用",
}

# Fixed messages for the restart request; the upstream error text is never kept.
_RESTART_ERROR_LABELS = {
    "login_required": "登录已失效，请先在播放器中重新扫码登录",
    "authentication_error": "登录已失效，请先在播放器中重新扫码登录",
    "rate_limited": "上游限流，请稍后重试",
    "access_restricted": "上游拒绝访问，请稍后重试",
    "media_changed": "媒体内容已变化，请重新打开文件",
    "network_error": "网络连接失败，请稍后重试",
    "remote_error": "上游返回错误，请稍后重试",
    "bad_request": "请求参数无效，无法从头播放",
    "local_io_error": "本地读写错误，无法从头播放",
    "internal_error": "内部错误，无法从头播放",
    "unauthorized": "本地令牌已失效，请重新打开诊断窗口",
    "not_found": "未找到对应的播放会话",
    "method_not_allowed": "本地服务接口不可用",
}
_RESTART_FAILED = "从头开始失败，请稍后重试"
_RESTART_OK = "已请求从头开始播放"

_DISCONNECTED_NOTE = "已断开：未连接到本地桥接服务（本地服务可能已退出）"
_DISCONNECT_FAILED_NOTE = "读取诊断数据失败，正在重试"
_EXPORT_UNAVAILABLE = "当前未连接本地服务，无法导出诊断报告"
_EXPORT_FAILED_REPORT = "导出失败：无法从本地服务获取诊断报告"
_EXPORT_FAILED_WRITE = "导出失败：无法写入所选文件，请检查路径和磁盘空间"

# Fixed bookmark messages: every failure is one of these, never a transport
# error, a URL, a token or any other upstream text.
_BOOKMARK_LOADING = "正在读取书签……"
_BOOKMARK_WORKING = "正在处理书签操作……"
_BOOKMARK_LOAD_FAILED = "读取书签失败，请稍后重试"
_BOOKMARK_ADD_FAILED = "添加书签失败，请稍后重试"
_BOOKMARK_RENAME_FAILED = "重命名书签失败，请稍后重试"
_BOOKMARK_REMOVE_FAILED = "删除书签失败，请稍后重试"
_BOOKMARK_JUMP_FAILED = "跳转失败，请稍后重试"
_BOOKMARK_ADDED = "已添加书签"
_BOOKMARK_RENAMED = "已重命名书签"
_BOOKMARK_REMOVED = "已删除书签"
_BOOKMARK_JUMPED = "已请求跳转"
_BOOKMARK_NOTE_TOO_LONG = "备注过长，请控制在 200 个字符以内"
_BOOKMARK_STALE = "当前选择已变更，请重新操作"


# -- formatting ---------------------------------------------------------


def _is_number(value: Any) -> bool:
    return type(value) in (int, float)


def format_bytes(value: Any) -> str:
    """Bytes as B/KiB/MiB/GiB, e.g. ``12.3 MiB``; unknown values are a dash."""
    if not _is_number(value) or value < 0:
        return "—"
    if value < 1024:
        return f"{int(value)} B"
    if value < _MIB:
        return f"{value / 1024:.1f} KiB"
    if value < _GIB:
        return f"{value / _MIB:.1f} MiB"
    return f"{value / _GIB:.2f} GiB"


def format_speed(value: Any) -> str:
    """Actual transfer rate in MiB/s (never a playback bitrate)."""
    if not _is_number(value) or value < 0:
        return "—"
    return f"{value / _MIB:.1f} MiB/s"


def format_response_ms(value: Any) -> str:
    """Last upstream response-header latency in ms; unknown values are a dash."""
    if type(value) is not int or value < 0:
        return "—"
    return f"{value} ms"


def format_duration_seconds(value: Any) -> str:
    """Seconds as HH:MM:SS."""
    if not _is_number(value) or value < 0:
        return "—"
    total = int(value)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def format_duration_ms(value: Any) -> str:
    """Milliseconds as HH:MM:SS."""
    if not _is_number(value) or value < 0:
        return "—"
    return format_duration_seconds(value / 1000)


def format_timestamp(value: Any) -> str:
    """An ISO instant from the snapshot as local HH:MM:SS, or a dash."""
    if not isinstance(value, str) or not value:
        return "—"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return "—"
    return moment.astimezone().strftime("%H:%M:%S")


def validation_label(validation: Any) -> str:
    """Chinese account state; a saved credential alone is explicitly pending."""
    return _VALIDATION_LABELS.get(validation, "未知状态")


def state_label(state: Any) -> str:
    return _STATE_LABELS.get(state, "未知状态")


def protection_text(protection: Any) -> str:
    """One fixed Chinese line for the local protection panel."""
    if not isinstance(protection, dict):
        return "不可用（无法读取本地保护状态）"
    reason = protection.get("reason")
    if reason == "local_protection_stopped":
        instruction = protection.get("instruction")
        if isinstance(instruction, str) and instruction:
            return instruction
        return "本地保护状态不可读，请检查配置目录权限"
    if not protection.get("available"):
        return "不可用（无法读取本地保护状态）"
    if protection.get("active"):
        label = _COOLDOWN_LABELS.get(reason, "上游限流")
        remaining = format_duration_seconds(protection.get("remaining_seconds"))
        return f"冷却中：{label}，剩余 {remaining}"
    return "无冷却"


# -- endpoint / client --------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    """A verified loopback endpoint: literal host, validated port, Bearer token."""

    base_url: str
    token: str
    instance: str
    port: int


def load_endpoint(read_runtime: Callable[[], Any] = plugin_state.read_runtime) -> Endpoint | None:
    """Build the loopback endpoint from the bridge's runtime record, or ``None``.

    Only ``port``/``token``/``instance`` are read; every other field (including
    any host-like value a tampered file might carry) is ignored, so the window
    can never be pointed at an arbitrary remote base URL.
    """
    entry = read_runtime()
    if not isinstance(entry, dict):
        return None
    port = entry.get("port")
    token = entry.get("token")
    instance = entry.get("instance")
    if type(port) is not int or not 1 <= port <= 65535:
        return None
    if not isinstance(token, str) or not token:
        return None
    if not isinstance(instance, str) or not instance:
        return None
    return Endpoint(f"http://{HOST}:{port}", token, instance, port)


class BridgeClient:
    """Bounded, loopback-only HTTP access with the runtime Bearer token."""

    def __init__(
        self,
        endpoint: Endpoint,
        *,
        timeout: float = REQUEST_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint
        # ``trust_env=False``: environment proxies must never rewrite loopback.
        self._client = httpx.Client(
            base_url=endpoint.base_url,
            timeout=httpx.Timeout(timeout),
            trust_env=False,
            headers={"Authorization": f"Bearer {endpoint.token}"},
            transport=transport,
        )

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()

    def healthy(self) -> bool:
        """True when the recorded instance still answers ``/v1/health``."""
        try:
            response = self._client.get(HEALTH_PATH)
            if response.status_code != 200:
                return False
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and payload.get("instance") == self._endpoint.instance
        )

    def snapshot(self) -> dict[str, Any] | None:
        return self._get_object(SNAPSHOT_PATH)

    def report(self) -> dict[str, Any] | None:
        return self._get_object(REPORT_PATH)

    def restart(self, player_pid: int, fid: str) -> tuple[bool, str]:
        """Ask the bridge to restart one binding; returns ``(ok, fixed message)``."""
        try:
            response = self._client.post(
                RESTART_PATH, json={"player_pid": player_pid, "fid": fid}
            )
            status = response.status_code
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return False, _RESTART_FAILED
        kind = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            candidate = payload["error"].get("type")
            if isinstance(candidate, str):
                kind = candidate
        if status == 200 and kind is None:
            return True, _RESTART_OK
        return False, _RESTART_ERROR_LABELS.get(kind, _RESTART_FAILED)

    def _get_object(self, path: str) -> dict[str, Any] | None:
        try:
            response = self._client.get(path)
            if response.status_code != 200:
                return None
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def bookmarks(self, fid: str) -> list[dict[str, Any]] | None:
        """One video's bookmarks by fid; ``None`` when the request failed.

        The fid is always the binding's own identifier, never a title guess, and
        an empty list is a valid answer (the video simply has no bookmark yet).
        """
        payload = self._get_object(f"{BOOKMARKS_PATH}?fid={quote(fid, safe='')}")
        items = payload.get("items") if payload is not None else None
        if not isinstance(items, list):
            return None
        return [item for item in items if isinstance(item, dict)]

    def add_bookmark(self, player_pid: int, fid: str, note: str) -> bool:
        """Bookmark the player's current position of one binding."""
        return self._post_accepted(
            BOOKMARKS_ADD_PATH, {"player_pid": player_pid, "fid": fid, "note": note}
        )

    def rename_bookmark(self, bookmark_id: Any, note: str) -> bool:
        return self._post_accepted(BOOKMARKS_RENAME_PATH, {"id": bookmark_id, "note": note})

    def remove_bookmark(self, bookmark_id: Any) -> bool:
        return self._post_accepted(BOOKMARKS_REMOVE_PATH, {"id": bookmark_id})

    def jump_bookmark(self, player_pid: int, fid: str, bookmark_id: Any) -> bool:
        return self._post_accepted(
            BOOKMARKS_JUMP_PATH, {"player_pid": player_pid, "fid": fid, "id": bookmark_id}
        )

    def _post_accepted(self, path: str, body: dict[str, Any]) -> bool:
        """One JSON POST: any 2xx means the local bridge accepted the request.

        The response body is never inspected, so no upstream text (and no error
        message) can ever reach the window; failures are a plain ``False``.
        """
        try:
            response = self._client.post(path, json=body)
        except httpx.HTTPError:
            return False
        return 200 <= response.status_code < 300


# -- worker -------------------------------------------------------------


class BridgeWorker:
    """One poll per second plus operator commands, all off the Tk thread.

    Events posted to ``events`` (read by the Tk thread):

    ``("snapshot", dict)``  a fresh diagnostics snapshot (also means connected)
    ``("failed", None)``    this poll failed but the bridge is still healthy
    ``("disconnected", None)`` the bridge itself no longer answers
    ``("exported", path)`` / ``("export_failed", msg)`` / ``("restart_done", (ok, msg))``
    ``("bookmarks", (player_pid, fid, items_or_none))``
    ``("bookmark_added" | "bookmark_renamed" | "bookmark_removed" | "bookmark_jumped",
        (player_pid, fid, ok))``

    Every bookmark event echoes the ``(player_pid, fid)`` of the request that
    caused it, so the Tk side can drop a late answer whose binding is no longer
    the selected one.
    """

    def __init__(
        self,
        client: BridgeClient,
        events: queue.Queue,
        requests: queue.Queue,
        *,
        interval: float = POLL_INTERVAL,
        stop: threading.Event | None = None,
    ) -> None:
        self._client = client
        self._events = events
        self._requests = requests
        self._interval = interval
        self._stop = stop if stop is not None else threading.Event()
        self.polls = 0

    def cancel(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        """Exactly one diagnostics GET; a failure is classified by a health probe."""
        self.polls += 1
        snapshot = self._client.snapshot()
        if snapshot is not None:
            self._events.put(("snapshot", snapshot))
            return
        if self._client.healthy():
            self._events.put(("failed", None))
        else:
            self._events.put(("disconnected", None))

    def run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            deadline = time.monotonic() + self._interval
            while not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    request = self._requests.get(timeout=remaining)
                except queue.Empty:
                    break
                self._serve(request)

    def _serve(self, request: tuple) -> None:
        kind = request[0] if request else None
        if kind == "export":
            self._export(request[1])
        elif kind == "restart":
            ok, message = self._client.restart(request[1], request[2])
            self._events.put(("restart_done", (ok, message)))
        elif kind == "bookmarks":
            _, player_pid, fid = request
            items = self._client.bookmarks(fid)
            self._events.put(("bookmarks", (player_pid, fid, items)))
        elif kind == "bookmark_add":
            _, player_pid, fid, note = request
            ok = self._client.add_bookmark(player_pid, fid, note)
            self._events.put(("bookmark_added", (player_pid, fid, ok)))
        elif kind == "bookmark_rename":
            _, player_pid, fid, bookmark_id, note = request
            ok = self._client.rename_bookmark(bookmark_id, note)
            self._events.put(("bookmark_renamed", (player_pid, fid, ok)))
        elif kind == "bookmark_remove":
            _, player_pid, fid, bookmark_id = request
            ok = self._client.remove_bookmark(bookmark_id)
            self._events.put(("bookmark_removed", (player_pid, fid, ok)))
        elif kind == "bookmark_jump":
            _, player_pid, fid, bookmark_id = request
            ok = self._client.jump_bookmark(player_pid, fid, bookmark_id)
            self._events.put(("bookmark_jumped", (player_pid, fid, ok)))

    def _export(self, path: str) -> None:
        report = self._client.report()
        if report is None:
            self._events.put(("export_failed", _EXPORT_FAILED_REPORT))
            return
        try:
            payload = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
            with open(path, "wb") as handle:
                handle.write(payload)
        except OSError:
            self._events.put(("export_failed", _EXPORT_FAILED_WRITE))
            return
        self._events.put(("exported", path))


# -- selection helpers --------------------------------------------------


def match_selection(playback: Any, selected: Any) -> dict[str, Any] | None:
    """The playback row matching ``selected`` ``(player_pid, fid)``, or ``None``."""
    if selected is None or not isinstance(playback, list):
        return None
    try:
        player_pid, fid = selected
    except (TypeError, ValueError):
        return None
    for row in playback:
        if isinstance(row, dict) and row.get("player_pid") == player_pid and row.get("fid") == fid:
            return row
    return None


def selection_target(playback: Any, selected: Any) -> tuple[int, str] | None:
    """The validated ``(player_pid, fid)`` of the selected playback row.

    ``None`` for an unmatched row or an unusable binding. Every bookmark request
    is addressed with these identifiers, so a bookmark list is never matched to
    a video by its title.
    """
    row = match_selection(playback, selected)
    if row is None:
        return None
    player_pid, fid = row.get("player_pid"), row.get("fid")
    if type(player_pid) is not int or not isinstance(fid, str) or not fid:
        return None
    return player_pid, fid


def bookmark_enabled(
    connected: bool, playback: Any, selected: Any, pending: bool = False
) -> bool:
    """A connected window with a matched binding may open that video's bookmarks."""
    if not connected or pending:
        return False
    return selection_target(playback, selected) is not None


def bookmark_action_enabled(
    connected: bool, playback: Any, selected: Any, pending: bool = False
) -> bool:
    """Only a binding that still holds the file can bookmark or jump."""
    if not bookmark_enabled(connected, playback, selected, pending):
        return False
    row = match_selection(playback, selected)
    return row.get("state") not in _INACTIVE_STATES


def bookmark_id_of(entry: Any) -> Any:
    """The bridge bookmark id of one list entry, or ``None`` when unusable."""
    if not isinstance(entry, dict):
        return None
    value = entry.get("id")
    if isinstance(value, str) and value:
        return value
    if type(value) is int:
        return value
    return None


def restart_enabled(
    connected: bool, playback: Any, selected: Any, pending: bool = False
) -> bool:
    """Only a connected window with a matched, still-active playback may restart."""
    if not connected or pending:
        return False
    row = match_selection(playback, selected)
    if row is None or row.get("state") in _INACTIVE_STATES:
        return False
    return selection_target(playback, selected) is not None


def default_export_name(moment: datetime | None = None) -> str:
    stamp = (moment or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"quark-diagnostics-{stamp}.json"


def _tkinter() -> Any:
    try:
        import tkinter
    except ImportError:
        raise LocalIOError("缺少 Tkinter，无法显示诊断窗口") from None
    return tkinter


def _widgets(root: Any) -> list[Any]:
    """Every widget under ``root`` in memory (``Tk.destroy`` keeps the links)."""
    found: list[Any] = []
    stack = [root]
    while stack:
        widget = stack.pop()
        found.append(widget)
        stack.extend(getattr(widget, "children", {}).values())
    return found


class StatusWindow:
    """The Tk window itself: one main thread for Tk, one worker for I/O."""

    def __init__(self, client: BridgeClient | None, endpoint: Endpoint | None) -> None:
        self._client = client
        self._endpoint = endpoint
        self._events: queue.Queue = queue.Queue()
        self._requests: queue.Queue = queue.Queue()
        self._worker: BridgeWorker | None = None
        self._thread: threading.Thread | None = None

        self._tk: Any = None
        self._root: Any = None
        self._filedialog: Any = None
        self._pump_id: str | None = None
        self._stopping = False

        self._connected = False
        self._snapshot: dict[str, Any] | None = None
        self._playback: list[dict[str, Any]] = []
        self._selected: tuple[Any, Any] | None = None
        self._rows: dict[str, tuple[Any, Any]] = {}
        self._restart_pending = False
        self._export_pending = False

        # Bookmark manager state: the list always belongs to ``_selected``; a
        # pending request records the binding it was issued for so a late answer
        # from another binding is dropped instead of applied.
        self._bookmarks: list[dict[str, Any]] = []
        self._bookmark_pending: tuple[Any, Any] | None = None

        self._transfer_var: Any = None
        self._transfer_tree: Any = None
        self._account_var: Any = None
        self._protection_var: Any = None
        self._notice_var: Any = None
        self._playback_tree: Any = None
        self._errors_tree: Any = None
        self._restart_button: Any = None
        self._export_button: Any = None
        self._bookmark_button: Any = None
        self._bookmark_window: Any = None
        self._bookmark_tree: Any = None
        self._bookmark_rows: dict[str, Any] = {}
        self._bookmark_header_var: Any = None
        self._bookmark_notice_var: Any = None
        self._bookmark_add_button: Any = None
        self._bookmark_jump_button: Any = None
        self._bookmark_rename_button: Any = None
        self._bookmark_remove_button: Any = None
        self._bookmark_refresh_button: Any = None
        self._simpledialog: Any = None

    # -- lifecycle -------------------------------------------------------

    def run(self) -> int:
        tk = self._tk = _tkinter()
        root = None
        try:
            try:
                root = tk.Tk()
            except tk.TclError:
                raise LocalIOError("无法创建诊断窗口，请检查桌面环境") from None
            self._root = root
            self._build(tk, root)
            if self._client is not None:
                self._worker = BridgeWorker(self._client, self._events, self._requests)
                self._thread = threading.Thread(
                    target=self._worker.run, name="quark-potplayer-status", daemon=True
                )
                self._thread.start()
            else:
                self._show_disconnected()
            self._pump()
            root.mainloop()
        finally:
            self._shutdown()
        return EXIT_OK

    def _shutdown(self) -> None:
        """Cancel the worker with a bounded join, then release Tk on this thread."""
        if self._stopping and self._root is None:
            return
        self._stopping = True
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=REQUEST_TIMEOUT + 1.0)
        client, self._client = self._client, None
        if client is not None:
            client.close()
        self._teardown()

    def _teardown(self) -> None:
        """Drop every Tk object on the thread that created the window."""
        root, self._root = self._root, None
        pump_id, self._pump_id = self._pump_id, None
        try:
            if root is not None:
                if pump_id is not None:
                    with contextlib.suppress(Exception):
                        root.after_cancel(pump_id)
                with contextlib.suppress(Exception):
                    root.destroy()
                for widget in _widgets(root):
                    with contextlib.suppress(Exception):
                        widget.master = None
                        widget.children.clear()
        finally:
            self._tk = None
            self._filedialog = None
            self._simpledialog = None
            self._transfer_tree = None
            self._playback_tree = None
            self._errors_tree = None
            self._restart_button = None
            self._export_button = None
            self._bookmark_button = None
            self._transfer_var = None
            self._account_var = None
            self._protection_var = None
            self._notice_var = None
            self._bookmark_window = None
            self._bookmark_tree = None
            self._bookmark_rows = {}
            self._bookmark_header_var = None
            self._bookmark_notice_var = None
            self._bookmark_add_button = None
            self._bookmark_jump_button = None
            self._bookmark_rename_button = None
            self._bookmark_remove_button = None
            self._bookmark_refresh_button = None

    # -- widget construction ---------------------------------------------

    def _build(self, tk: Any, root: Any) -> None:
        from tkinter import filedialog, font as tkfont, ttk

        self._filedialog = filedialog
        root.title("夸克网盘播放诊断")
        root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        root.minsize(MIN_WIDTH, MIN_HEIGHT)
        _apply_font(root, tkfont, ttk)

        self._transfer_var = tk.StringVar(value="—")
        self._account_var = tk.StringVar(value="—")
        self._protection_var = tk.StringVar(value="—")
        self._notice_var = tk.StringVar(value="正在连接本地服务……")

        outer = ttk.Frame(root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=3)
        outer.rowconfigure(4, weight=2)

        summary = ttk.LabelFrame(outer, text="传输概览", padding=8)
        summary.grid(row=0, column=0, sticky="ew")
        ttk.Label(summary, textvariable=self._transfer_var, anchor="w", justify="left").pack(
            fill="x"
        )

        transfer = ttk.LabelFrame(outer, text="各会话传输（本机逐会话统计）", padding=8)
        transfer.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        transfer.columnconfigure(0, weight=1)
        self._transfer_tree = ttk.Treeview(
            transfer,
            columns=("name", "bytes", "speed", "rtt"),
            show="headings",
            selectmode="browse",
            height=TRANSFER_ROWS,
        )
        for column, title, width in (
            ("name", "名称", 320),
            ("bytes", "本次累计传输", 140),
            ("speed", "5 秒速率", 130),
            ("rtt", "上游响应", 110),
        ):
            self._transfer_tree.heading(column, text=title)
            self._transfer_tree.column(column, width=width, anchor="w", stretch=(column == "name"))
        self._transfer_tree.grid(row=0, column=0, sticky="ew")
        transfer_scroll = ttk.Scrollbar(
            transfer, orient="vertical", command=self._transfer_tree.yview
        )
        self._transfer_tree.configure(yscrollcommand=transfer_scroll.set)
        transfer_scroll.grid(row=0, column=1, sticky="ns")

        state = ttk.LabelFrame(outer, text="账号与本地保护", padding=8)
        state.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(state, textvariable=self._account_var, anchor="w", justify="left").pack(
            fill="x"
        )
        ttk.Label(
            state, textvariable=self._protection_var, anchor="w", justify="left", wraplength=740
        ).pack(fill="x", pady=(4, 0))

        playback = ttk.LabelFrame(outer, text="播放会话（选择一行后可从头开始）", padding=8)
        playback.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        playback.columnconfigure(0, weight=1)
        playback.rowconfigure(0, weight=1)
        self._playback_tree = ttk.Treeview(
            playback,
            columns=("name", "position", "duration", "state"),
            show="headings",
            selectmode="browse",
        )
        for column, title, width in (
            ("name", "名称", 320),
            ("position", "进度", 100),
            ("duration", "时长", 100),
            ("state", "状态", 200),
        ):
            self._playback_tree.heading(column, text=title)
            self._playback_tree.column(column, width=width, anchor="w", stretch=(column == "name"))
        self._playback_tree.grid(row=0, column=0, sticky="nsew")
        playback_scroll = ttk.Scrollbar(
            playback, orient="vertical", command=self._playback_tree.yview
        )
        self._playback_tree.configure(yscrollcommand=playback_scroll.set)
        playback_scroll.grid(row=0, column=1, sticky="ns")
        self._playback_tree.bind("<<TreeviewSelect>>", self._on_select)

        errors = ttk.LabelFrame(outer, text="最近错误（最多 20 条）", padding=8)
        errors.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        errors.columnconfigure(0, weight=1)
        errors.rowconfigure(0, weight=1)
        self._errors_tree = ttk.Treeview(
            errors, columns=("time", "phase", "message"), show="headings", selectmode="browse"
        )
        for column, title, width in (
            ("time", "时间", 90),
            ("phase", "阶段", 90),
            ("message", "说明", 560),
        ):
            self._errors_tree.heading(column, text=title)
            self._errors_tree.column(column, width=width, anchor="w", stretch=(column == "message"))
        self._errors_tree.grid(row=0, column=0, sticky="nsew")
        errors_scroll = ttk.Scrollbar(errors, orient="vertical", command=self._errors_tree.yview)
        self._errors_tree.configure(yscrollcommand=errors_scroll.set)
        errors_scroll.grid(row=0, column=1, sticky="ns")

        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(actions, textvariable=self._notice_var, anchor="w", wraplength=420).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(actions, text="关闭", width=10, command=self._close).pack(side="right")
        self._restart_button = ttk.Button(
            actions, text="从头开始", width=12, command=self._restart
        )
        self._restart_button.pack(side="right", padx=6)
        self._export_button = ttk.Button(
            actions, text="导出诊断", width=12, command=self._export
        )
        self._export_button.pack(side="right")
        self._bookmark_button = ttk.Button(
            actions, text="播放书签", width=12, command=self._open_bookmarks
        )
        self._bookmark_button.pack(side="right", padx=(0, 6))

        root.protocol("WM_DELETE_WINDOW", self._close)

    # -- Tk-thread event handling ----------------------------------------

    def _pump(self) -> None:
        """Poll worker events on the Tk thread; a bound method avoids cycles."""
        root = self._root
        if root is None:
            return
        try:
            while True:
                kind, value = self._events.get_nowait()
                self._handle(kind, value)
                if self._root is None:
                    return
        except queue.Empty:
            pass
        if self._root is not None:
            self._pump_id = root.after(200, self._pump)

    def _handle(self, kind: str, value: Any) -> None:
        if kind == "snapshot":
            self._apply_snapshot(value)
        elif kind == "failed":
            self._notice_var.set(_DISCONNECT_FAILED_NOTE)
        elif kind == "disconnected":
            self._show_disconnected()
        elif kind == "exported":
            self._export_pending = False
            self._notice_var.set(f"诊断报告已导出：{value}")
            self._update_actions()
        elif kind == "export_failed":
            self._export_pending = False
            self._notice_var.set(value)
            self._update_actions()
        elif kind == "restart_done":
            self._restart_pending = False
            ok, message = value
            self._notice_var.set(message if ok else f"从头开始失败：{message}")
            self._update_actions()
        elif kind == "bookmarks":
            self._apply_bookmarks(value)
        elif kind == "bookmark_added":
            self._apply_bookmark_result(value, _BOOKMARK_ADDED, _BOOKMARK_ADD_FAILED, refresh=True)
        elif kind == "bookmark_renamed":
            self._apply_bookmark_result(
                value, _BOOKMARK_RENAMED, _BOOKMARK_RENAME_FAILED, refresh=True
            )
        elif kind == "bookmark_removed":
            self._apply_bookmark_result(
                value, _BOOKMARK_REMOVED, _BOOKMARK_REMOVE_FAILED, refresh=True
            )
        elif kind == "bookmark_jumped":
            self._apply_bookmark_result(
                value, _BOOKMARK_JUMPED, _BOOKMARK_JUMP_FAILED, refresh=False
            )

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._connected = True
        self._snapshot = snapshot
        playback = snapshot.get("playback")
        self._playback = playback if isinstance(playback, list) else []
        self._notice_var.set("已连接本地服务")
        self._refresh_transfer(snapshot)
        self._refresh_account(snapshot)
        self._refresh_playback()
        self._refresh_errors(snapshot)
        self._update_actions()

    def _show_disconnected(self) -> None:
        self._connected = False
        self._snapshot = None
        self._playback = []
        self._transfer_var.set("—")
        self._account_var.set("—")
        self._protection_var.set(_DISCONNECTED_NOTE)
        self._notice_var.set(_DISCONNECTED_NOTE)
        self._transfer_tree.delete(*self._transfer_tree.get_children())
        self._playback_tree.delete(*self._playback_tree.get_children())
        self._errors_tree.delete(*self._errors_tree.get_children())
        self._rows = {}
        self._selected = None
        if self._bookmark_live():
            self._clear_bookmarks()
            self._bookmark_notice(_DISCONNECTED_NOTE)
        self._update_actions()

    def _refresh_transfer(self, snapshot: dict[str, Any]) -> None:
        transfer = snapshot.get("transfer")
        transfer = transfer if isinstance(transfer, dict) else {}
        totals = transfer.get("totals")
        totals = totals if isinstance(totals, dict) else {}
        plays = transfer.get("plays")
        plays = plays if isinstance(plays, list) else []
        # The totals row is the bridge-wide aggregate; no selected-playback RTT
        # is guessed here because per-play rows carry no player_pid/fid.
        self._transfer_var.set(
            f"整体速度（5 秒）：{format_speed(totals.get('speed_bytes_per_second'))}"
            f"　累计传输：{format_bytes(totals.get('bytes'))}"
        )
        tree = self._transfer_tree
        tree.delete(*tree.get_children())
        for index, row in enumerate(plays):
            if not isinstance(row, dict):
                continue
            tree.insert(
                "",
                "end",
                iid=f"play{index}",
                values=(
                    row.get("name") or "—",
                    format_bytes(row.get("bytes")),
                    format_speed(row.get("speed_bytes_per_second")),
                    format_response_ms(row.get("last_elapsed_ms")),
                ),
            )

    def _refresh_account(self, snapshot: dict[str, Any]) -> None:
        account = snapshot.get("account")
        account = account if isinstance(account, dict) else {}
        credential = "已保存" if account.get("credential_present") else "未保存"
        last_success = format_timestamp(account.get("last_success_at"))
        self._account_var.set(
            f"账号状态：{validation_label(account.get('validation'))}"
            f"　凭证：{credential}　最近成功：{last_success}"
        )
        protection = snapshot.get("protection")
        self._protection_var.set(f"本地保护：{protection_text(protection)}")

    def _refresh_playback(self) -> None:
        tree = self._playback_tree
        # Read the selection before rebuilding: a programmatic selection change
        # during ``delete``/``selection_set`` may fire ``_on_select``, and the
        # row is restored afterwards so the bookmark list keeps its binding.
        previous = self._selected
        tree.delete(*tree.get_children())
        self._rows = {}
        restored = False
        for index, row in enumerate(self._playback):
            if not isinstance(row, dict):
                continue
            iid = f"row{index}"
            tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    row.get("name") or "—",
                    format_duration_ms(row.get("position_ms")),
                    format_duration_ms(row.get("duration_ms")),
                    state_label(row.get("state")),
                ),
            )
            self._rows[iid] = (row.get("player_pid"), row.get("fid"))
            if previous is not None and self._rows[iid] == previous:
                tree.selection_set(iid)
                tree.focus(iid)
                restored = True
        if previous is not None:
            self._selected = previous if restored else None
        if self._bookmark_live():
            if selection_target(self._playback, self._selected) is None:
                self._clear_bookmarks()
            else:
                self._refresh_bookmark_header()

    def _refresh_errors(self, snapshot: dict[str, Any]) -> None:
        tree = self._errors_tree
        tree.delete(*tree.get_children())
        errors = snapshot.get("errors")
        events = errors.get("events") if isinstance(errors, dict) else None
        if not isinstance(events, list):
            return
        for event in list(reversed(events))[:MAX_ERROR_ROWS]:
            if not isinstance(event, dict):
                continue
            tree.insert(
                "",
                "end",
                values=(
                    format_timestamp(event.get("at")),
                    event.get("phase") or "—",
                    event.get("message") or "—",
                ),
            )

    def _on_select(self, _event: Any = None) -> None:
        selection = self._playback_tree.selection()
        selected = self._rows.get(selection[0]) if selection else None
        if selected == self._selected:
            # Programmatic re-selection during a snapshot rebuild is not a change.
            return
        self._selected = selected
        self._update_actions()
        if self._bookmark_live():
            self._clear_bookmarks()
            self._request_bookmarks()

    def _update_actions(self) -> None:
        if self._root is None:
            return
        if restart_enabled(self._connected, self._playback, self._selected, self._restart_pending):
            self._restart_button.state(["!disabled"])
        else:
            self._restart_button.state(["disabled"])
        if self._connected and not self._export_pending:
            self._export_button.state(["!disabled"])
        else:
            self._export_button.state(["disabled"])
        busy = self._bookmark_pending is not None
        if bookmark_enabled(self._connected, self._playback, self._selected, busy):
            self._bookmark_button.state(["!disabled"])
        else:
            self._bookmark_button.state(["disabled"])

    # -- operator actions ------------------------------------------------

    def _restart(self) -> None:
        row = match_selection(self._playback, self._selected)
        if row is None:
            return
        if not restart_enabled(self._connected, self._playback, self._selected, self._restart_pending):
            return
        self._restart_pending = True
        self._notice_var.set("正在请求从头开始播放……")
        self._update_actions()
        self._requests.put(("restart", row.get("player_pid"), row.get("fid")))

    def _export(self) -> None:
        if not self._connected:
            self._notice_var.set(_EXPORT_UNAVAILABLE)
            return
        if self._export_pending:
            return
        path = self._filedialog.asksaveasfilename(
            parent=self._root,
            title="导出诊断报告",
            defaultextension=".json",
            initialfile=default_export_name(),
            filetypes=(("JSON 文件", "*.json"), ("所有文件", "*.*")),
        )
        if not path:
            return
        self._export_pending = True
        self._notice_var.set("正在导出诊断报告……")
        self._update_actions()
        self._requests.put(("export", path))

    def _close(self) -> None:
        """Tk thread only: cancel the worker, then destroy the window."""
        self._shutdown()

    # -- bookmark manager (a separate Toplevel, same Tk thread) -----------

    def _bookmark_live(self) -> bool:
        """True while the bookmark window exists (Tk thread only)."""
        window = self._bookmark_window
        if window is None:
            return False
        exists = False
        with contextlib.suppress(Exception):
            exists = bool(window.winfo_exists())
        return exists

    def _list_target(self) -> tuple[int, str] | None:
        """The selected binding when the list may address it, else ``None``."""
        if not self._bookmark_live() or self._bookmark_pending is not None:
            return None
        if not bookmark_enabled(self._connected, self._playback, self._selected):
            return None
        return selection_target(self._playback, self._selected)

    def _action_target(self) -> tuple[int, str] | None:
        """The selected binding when the player can still be asked to add/jump."""
        if not self._bookmark_live():
            return None
        if not bookmark_action_enabled(
            self._connected, self._playback, self._selected, self._bookmark_pending is not None
        ):
            return None
        return selection_target(self._playback, self._selected)

    def _selected_bookmark(self) -> Any:
        """The id of the selected bookmark row, or ``None``."""
        tree = self._bookmark_tree
        if tree is None:
            return None
        selection = tree.selection()
        return self._bookmark_rows.get(selection[0]) if selection else None

    def _bookmark_notice(self, text: str) -> None:
        if self._bookmark_notice_var is not None:
            self._bookmark_notice_var.set(text)

    def _settle_bookmark_notice(self, text: str) -> None:
        """Replace a load-in-progress message with the load's own result.

        The re-read after an operator command runs with ``announce=False`` so
        that command's own success message stays visible; it must not be
        overwritten by a later list answer.
        """
        var = self._bookmark_notice_var
        if var is not None and var.get() == _BOOKMARK_LOADING:
            var.set(text)

    def _open_bookmarks(self) -> None:
        """Open (or re-read) the bookmark manager for the selected binding."""
        if self._root is None:
            return
        if not bookmark_enabled(
            self._connected, self._playback, self._selected, self._bookmark_pending is not None
        ):
            return
        if self._bookmark_live():
            with contextlib.suppress(Exception):
                self._bookmark_window.lift()
        else:
            self._build_bookmarks()
        # Only a selection change and an explicit operator action re-read the
        # list; the per-second diagnostics poll never does.
        self._request_bookmarks()

    def _build_bookmarks(self) -> None:
        """Create the manager window; every Tk call stays on this thread."""
        from tkinter import simpledialog, ttk

        root = self._root
        self._simpledialog = simpledialog
        window = self._tk.Toplevel(root)
        self._bookmark_window = window
        window.title("播放书签")
        window.geometry(f"{BOOKMARK_WIDTH}x{BOOKMARK_HEIGHT}")
        window.minsize(BOOKMARK_MIN_WIDTH, BOOKMARK_MIN_HEIGHT)
        window.transient(root)
        window.protocol("WM_DELETE_WINDOW", self._close_bookmarks)

        self._bookmark_header_var = self._tk.StringVar(value="—")
        self._bookmark_notice_var = self._tk.StringVar(value="")
        outer = ttk.Frame(window, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        ttk.Label(
            outer, textvariable=self._bookmark_header_var, anchor="w", justify="left"
        ).grid(row=0, column=0, sticky="ew")

        table = ttk.Frame(outer)
        table.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self._bookmark_tree = ttk.Treeview(
            table, columns=("time", "note"), show="headings", selectmode="browse"
        )
        for column, title, width in (("time", "时间", 110), ("note", "备注", 360)):
            self._bookmark_tree.heading(column, text=title)
            self._bookmark_tree.column(column, width=width, anchor="w", stretch=(column == "note"))
        self._bookmark_tree.grid(row=0, column=0, sticky="nsew")
        bookmark_scroll = ttk.Scrollbar(
            table, orient="vertical", command=self._bookmark_tree.yview
        )
        self._bookmark_tree.configure(yscrollcommand=bookmark_scroll.set)
        bookmark_scroll.grid(row=0, column=1, sticky="ns")
        self._bookmark_tree.bind("<<TreeviewSelect>>", self._on_bookmark_select)

        actions = ttk.Frame(outer)
        actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._bookmark_add_button = ttk.Button(
            actions, text="添加书签", width=10, command=self._add_bookmark
        )
        self._bookmark_jump_button = ttk.Button(
            actions, text="跳转", width=8, command=self._jump_bookmark
        )
        self._bookmark_rename_button = ttk.Button(
            actions, text="重命名", width=10, command=self._rename_bookmark
        )
        self._bookmark_remove_button = ttk.Button(
            actions, text="删除", width=8, command=self._remove_bookmark
        )
        self._bookmark_refresh_button = ttk.Button(
            actions, text="刷新", width=8, command=self._request_bookmarks
        )
        for button in (
            self._bookmark_add_button,
            self._bookmark_jump_button,
            self._bookmark_rename_button,
            self._bookmark_remove_button,
            self._bookmark_refresh_button,
        ):
            button.pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="关闭", width=8, command=self._close_bookmarks).pack(side="right")
        ttk.Label(
            outer, textvariable=self._bookmark_notice_var, anchor="w", wraplength=BOOKMARK_NOTICE_WRAP
        ).grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self._refresh_bookmark_header()
        self._update_bookmark_actions()

    def _close_bookmarks(self) -> None:
        """Tk thread only: destroy the manager window and drop its state."""
        self._clear_bookmarks()
        window, self._bookmark_window = self._bookmark_window, None
        if window is not None:
            with contextlib.suppress(Exception):
                window.destroy()
        self._bookmark_tree = None
        self._bookmark_header_var = None
        self._bookmark_notice_var = None
        self._simpledialog = None
        self._bookmark_add_button = None
        self._bookmark_jump_button = None
        self._bookmark_rename_button = None
        self._bookmark_remove_button = None
        self._bookmark_refresh_button = None
        self._update_actions()

    def _clear_bookmarks(self) -> None:
        """Drop the list and pending request of a binding that is gone."""
        self._bookmarks = []
        self._bookmark_pending = None
        self._bookmark_rows = {}
        tree = self._bookmark_tree
        if tree is not None:
            with contextlib.suppress(Exception):
                tree.delete(*tree.get_children())
        self._bookmark_notice("")
        self._refresh_bookmark_header()
        self._update_bookmark_actions()

    def _request_bookmarks(self, *, announce: bool = True) -> None:
        """Ask the worker for the selected binding's bookmarks (Tk thread only).

        ``announce`` is dropped for the re-read after an operator command so the
        command's own result message stays visible.
        """
        target = self._list_target()
        if target is None:
            return
        player_pid, fid = target
        self._bookmark_pending = target
        self._bookmarks = []
        self._bookmark_rows = {}
        tree = self._bookmark_tree
        if tree is not None:
            with contextlib.suppress(Exception):
                tree.delete(*tree.get_children())
        self._refresh_bookmark_header()
        if announce:
            self._bookmark_notice(_BOOKMARK_LOADING)
        self._update_bookmark_actions()
        self._update_actions()
        self._requests.put(("bookmarks", player_pid, fid))

    def _apply_bookmarks(self, value: Any) -> None:
        """Apply a list answer, dropping any that belongs to another binding."""
        if not self._bookmark_live() or not isinstance(value, tuple) or len(value) != 3:
            return
        player_pid, fid, items = value
        if (player_pid, fid) != selection_target(self._playback, self._selected):
            return
        self._bookmark_pending = None
        if items is None:
            self._bookmarks = []
            self._render_bookmarks()
            self._bookmark_notice(_BOOKMARK_LOAD_FAILED)
            self._update_actions()
            return
        # Only addressable entries are kept: an entry without an id could not be
        # renamed, removed or jumped, and the header count is the visible one.
        self._bookmarks = [entry for entry in items if bookmark_id_of(entry) is not None]
        self._render_bookmarks()
        self._settle_bookmark_notice(f"已读取 {len(self._bookmarks)} 条书签")
        self._update_actions()

    def _apply_bookmark_result(self, value: Any, done: str, failed: str, *, refresh: bool) -> None:
        """Apply one add/rename/remove/jump answer for the binding that asked."""
        if not self._bookmark_live() or not isinstance(value, tuple) or len(value) != 3:
            return
        player_pid, fid, ok = value
        if (player_pid, fid) != self._bookmark_pending:
            return
        self._bookmark_pending = None
        if not ok:
            self._bookmark_notice(failed)
            self._update_bookmark_actions()
            self._update_actions()
            return
        self._bookmark_notice(done)
        self._update_actions()
        if refresh:
            self._request_bookmarks(announce=False)
        else:
            self._update_bookmark_actions()

    def _render_bookmarks(self) -> None:
        tree = self._bookmark_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        self._bookmark_rows = {}
        for index, entry in enumerate(self._bookmarks):
            bookmark_id = bookmark_id_of(entry)
            if bookmark_id is None:
                continue
            iid = f"mark{index}"
            position = format_duration_ms(entry.get("position_ms"))
            note = entry.get("note")
            tree.insert(
                "",
                "end",
                iid=iid,
                # An empty note is valid; the bookmark's own time point stands in
                # as its default name.
                values=(position, note if isinstance(note, str) and note else position),
            )
            self._bookmark_rows[iid] = bookmark_id
        self._refresh_bookmark_header()
        self._update_bookmark_actions()

    def _refresh_bookmark_header(self) -> None:
        if self._bookmark_header_var is None:
            return
        row = match_selection(self._playback, self._selected)
        name = (row.get("name") if row is not None else None) or "—"
        self._bookmark_header_var.set(f"当前视频：{name}　书签：{len(self._bookmarks)} 条")

    def _on_bookmark_select(self, _event: Any = None) -> None:
        self._update_bookmark_actions()

    def _update_bookmark_actions(self) -> None:
        if self._root is None or not self._bookmark_live():
            return
        busy = self._bookmark_pending is not None
        listing = bookmark_enabled(self._connected, self._playback, self._selected, busy)
        acting = bookmark_action_enabled(
            self._connected, self._playback, self._selected, busy
        )
        # Jump needs a row of the *attached* list of the selected binding: before
        # that list has arrived -- or after it was dropped -- nothing can be
        # selected, so jump stays disabled exactly like rename and remove.
        chosen = self._selected_bookmark() is not None
        _toggle(self._bookmark_add_button, acting)
        _toggle(self._bookmark_jump_button, acting and chosen)
        _toggle(self._bookmark_rename_button, listing and chosen)
        _toggle(self._bookmark_remove_button, listing and chosen)
        _toggle(self._bookmark_refresh_button, listing)

    def _ask_note(self, title: str, prompt: str, initial: str = "") -> str | None:
        """A modal note prompt on the Tk thread; ``None`` when it cannot answer.

        ``simpledialog`` re-enters ``mainloop``, so worker events (including a
        selection change) are handled while the operator types; callers must
        re-check their target afterwards.
        """
        dialog = self._simpledialog
        if dialog is None:
            return None
        try:
            answer = dialog.askstring(
                title, prompt, parent=self._bookmark_window, initialvalue=initial
            )
        except Exception:
            return None
        return None if answer is None else answer.strip()

    def _add_bookmark(self) -> None:
        """Bookmark the player's current position for the selected binding."""
        target = self._action_target()
        if target is None:
            return
        note = self._ask_note("添加书签", "请输入书签备注（可留空，最多 200 字）：")
        if note is None:
            return
        if len(note) > MAX_NOTE_CHARS:
            self._bookmark_notice(_BOOKMARK_NOTE_TOO_LONG)
            return
        if target != self._action_target():
            self._bookmark_notice(_BOOKMARK_STALE)
            return
        self._bookmark_pending = target
        self._bookmark_notice(_BOOKMARK_WORKING)
        self._update_bookmark_actions()
        self._update_actions()
        self._requests.put(("bookmark_add", target[0], target[1], note))

    def _rename_bookmark(self) -> None:
        """Rename the selected bookmark's note."""
        target = self._list_target()
        bookmark_id = self._selected_bookmark()
        if target is None or bookmark_id is None:
            return
        note = self._ask_note("重命名书签", "请输入新的备注（可留空，最多 200 字）：", self._note_of(bookmark_id))
        if note is None:
            return
        if len(note) > MAX_NOTE_CHARS:
            self._bookmark_notice(_BOOKMARK_NOTE_TOO_LONG)
            return
        if target != self._list_target() or bookmark_id != self._selected_bookmark():
            self._bookmark_notice(_BOOKMARK_STALE)
            return
        self._bookmark_pending = target
        self._bookmark_notice(_BOOKMARK_WORKING)
        self._update_bookmark_actions()
        self._update_actions()
        self._requests.put(("bookmark_rename", target[0], target[1], bookmark_id, note))

    def _remove_bookmark(self) -> None:
        """Remove the selected bookmark."""
        target = self._list_target()
        bookmark_id = self._selected_bookmark()
        if target is None or bookmark_id is None:
            return
        self._bookmark_pending = target
        self._bookmark_notice(_BOOKMARK_WORKING)
        self._update_bookmark_actions()
        self._update_actions()
        self._requests.put(("bookmark_remove", target[0], target[1], bookmark_id))

    def _jump_bookmark(self) -> None:
        """Ask the player to seek to the selected bookmark."""
        target = self._action_target()
        bookmark_id = self._selected_bookmark()
        if target is None or bookmark_id is None:
            return
        self._bookmark_pending = target
        self._bookmark_notice(_BOOKMARK_WORKING)
        self._update_bookmark_actions()
        self._update_actions()
        self._requests.put(("bookmark_jump", target[0], target[1], bookmark_id))

    def _note_of(self, bookmark_id: Any) -> str:
        """The stored note of one bookmark id, used as the rename default."""
        for entry in self._bookmarks:
            if bookmark_id_of(entry) == bookmark_id:
                note = entry.get("note")
                return note if isinstance(note, str) else ""
        return ""


def _toggle(widget: Any, enabled: bool) -> None:
    """Enable or disable one ttk button; no-op for a missing widget."""
    if widget is not None:
        widget.state(["!disabled"] if enabled else ["disabled"])


def _apply_font(root: Any, tkfont: Any, ttk: Any) -> None:
    """Use a readable Chinese font when one of the preferred families exists."""
    try:
        families = set(tkfont.families(root))
    except Exception:
        return
    for family in PREFERRED_FONTS:
        if family not in families:
            continue
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            with contextlib.suppress(Exception):
                tkfont.nametofont(name, root=root).configure(family=family, size=10)
        with contextlib.suppress(Exception):
            style = ttk.Style(root)
            style.configure("Treeview", font=(family, 10), rowheight=22)
            style.configure("Treeview.Heading", font=(family, 10))
        return


def run_status_window(runtime_reader: Callable[[], Any] = plugin_state.read_runtime) -> int:
    """Open the standalone diagnostics window; returns a process exit code.

    The runtime record is loaded and its loopback health re-verified before the
    window appears; when no live instance is recorded the window still opens, in
    a fixed disconnected state, so the operator can see why nothing is shown.
    """
    endpoint = load_endpoint(runtime_reader)
    client: BridgeClient | None = None
    if endpoint is not None:
        client = BridgeClient(endpoint)
        if client.healthy():
            pass
        else:
            client.close()
            client = None
    try:
        return StatusWindow(client, endpoint).run()
    except LocalIOError:
        if client is not None:
            client.close()
        return EXIT_UNAVAILABLE
