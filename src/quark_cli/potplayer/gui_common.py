"""Shared GUI values, formatting and loopback endpoint validation; no page state."""

import contextlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable


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

# Association pid of an *external* bookmark target: a request names a FID that
# may have no live player at all, so the window tracks it with this sentinel in
# ``(player_pid, fid)`` tuples. It is never a real pid, so it is never sent to
# the add/jump endpoints -- those resolve the one live binding first.
EXTERNAL_PID = 0

MAX_ERROR_ROWS = 20
WINDOW_WIDTH = 800
WINDOW_HEIGHT = 700
MIN_WIDTH = 640
MIN_HEIGHT = 520

# Visible rows in the per-session transfer table; more rows scroll.
TRANSFER_ROWS = 5

# The bookmark manager is a separate top-level window: the status window is
# already full, and bookmark work must never hide the diagnostics tables.
BOOKMARK_WIDTH = 820
BOOKMARK_HEIGHT = 360
BOOKMARK_MIN_WIDTH = 640
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
    "saved_unverified": "登录信息已保存，等待连接确认",
    "recently_valid": "上次网盘连接正常",
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

# An external target may only sample/seek a binding that is actually holding and
# playing the file: ``waiting`` (bound, not started), multi-instance, mismatch
# and every inactive state can never be an unambiguous add/jump target.
_OPERABLE_STATES = frozenset({"playing", "paused"})

_COOLDOWN_LABELS = {
    "rate_limited": "网盘请求过多",
    "access_restricted": "网盘暂时拒绝访问",
    "service_unavailable": "网盘暂时不可用",
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
_BOOKMARK_LOADING = "正在读取精彩标记……"
_BOOKMARK_WORKING = "正在处理精彩标记操作……"
_BOOKMARK_LOAD_FAILED = "读取精彩标记失败，请稍后重试"
_BOOKMARK_ADD_FAILED = "添加精彩标记失败，请稍后重试"
_BOOKMARK_RENAME_FAILED = "编辑精彩标记备注失败，请稍后重试"
_BOOKMARK_REMOVE_FAILED = "删除精彩标记失败，请稍后重试"
_BOOKMARK_JUMP_FAILED = "跳转失败，请稍后重试"
_BOOKMARK_ADDED = "已添加精彩标记"
_BOOKMARK_RENAMED = "已编辑精彩标记备注"
_BOOKMARK_REMOVED = "已删除精彩标记"
_BOOKMARK_JUMPED = "已请求跳转"
_BOOKMARK_NOTE_TOO_LONG = "备注过长，请控制在 200 个字符以内"
_BOOKMARK_STALE = "当前选择已变更，请重新操作"
# Shown while the manager is bound to an external FID with no single live
# binding to add a bookmark from or jump to; the list/rename/remove stay usable.
_BOOKMARK_EXTERNAL_REASON = "添加需正在播放；选择标记后可自动打开视频并跳转"


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
        return f"{label}，请等待 {remaining} 后再试"
    return "未触发限制"


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


def bookmark_request_of(snapshot: Any) -> dict[str, Any] | None:
    """The validated external bookmark request in a snapshot, or ``None``.

    A single-video request has a non-empty id/FID; scope=all selects the global
    list with an empty FID. Missing names fall back to the FID for a single video.
    """
    if not isinstance(snapshot, dict):
        return None
    request = snapshot.get("bookmark_request")
    if not isinstance(request, dict):
        return None
    request_id = request.get("id")
    fid = request.get("fid")
    if not isinstance(request_id, str) or not request_id:
        return None
    action = {'action': 'add'} if request.get('action') == 'add' else {}
    if request.get('scope') == 'parent':
        parent = request.get('parent')
        if not isinstance(parent, str) or not parent:
            return None
        return {'id': request_id, 'fid': '', 'parent': parent, 'scope': 'parent',
                'name': request.get('name') or '当前目录', **action}
    if request.get("scope") == "all":
        return {"id": request_id, "fid": "", "name": "全部视频", "scope": "all", **action}
    if not isinstance(fid, str) or not fid:
        return None
    name = request.get("name")
    if not isinstance(name, str) or not name:
        name = fid
    return {"id": request_id, "fid": fid, "name": name, **action}


def external_action_target(playback: Any, fid: Any) -> tuple[int, str] | None:
    """The one native binding that may act for an external ``fid``, or ``None``.

    Only a single binding for exactly this FID that is actually *operable*
    (``playing`` or ``paused``) counts. With no live binding, more than one
    instance, or a waiting/suspended/mismatched one there is no unambiguous
    player to sample or seek, so add/jump stay disabled -- the choice is never
    made by name. A ``suspended`` same-FID sibling is the server's multi-instance
    conflict: it is a *second* live instance, so every add/jump for the FID is
    rejected with 409 ``media_changed`` and no binding may act (a never-streamed
    ``waiting`` sibling is not a conflict and never blocks). The pid is always a
    real, positive pid, so the :data:`EXTERNAL_PID` sentinel never reaches an add
    or jump endpoint.
    """
    if not isinstance(fid, str) or not fid or not isinstance(playback, list):
        return None
    matches: list[int] = []
    for row in playback:
        if not isinstance(row, dict) or row.get("fid") != fid:
            continue
        player_pid = row.get("player_pid")
        if type(player_pid) is not int or player_pid <= EXTERNAL_PID:
            continue
        if row.get("state") == "suspended":
            # Same-FID multi-instance conflict: the server's _suspended(fid)
            # guard refuses add/jump for the whole FID, so disable it here too.
            return None
        if row.get("state") not in _OPERABLE_STATES:
            continue
        matches.append(player_pid)
    if len(matches) != 1:
        return None
    return matches[0], fid


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

