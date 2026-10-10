"""Standalone Tk status window for the PotPlayer bridge (release 0.5.0).

The service owns the bridge; this module only *observes* it. It loads the
bridge's private runtime record (``plugin_state.read_runtime``), confirms that
the recorded loopback port answers ``/v1/health`` with the same instance id, and
then reuses the recorded Bearer token for a fixed set of read-only endpoints:

* ``GET  /v1/diagnostics``         one request per second while the window is open
* ``GET  /v1/diagnostics/report``  only when the operator exports a report
* ``POST /v1/playback/restart``    only when the operator asks for it

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

# States in which the player still holds the file: only these offer "从头开始".
_INACTIVE_STATES = frozenset({"stopped", "missing", "unavailable", "error"})

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


# -- worker -------------------------------------------------------------


class BridgeWorker:
    """One poll per second plus operator commands, all off the Tk thread.

    Events posted to ``events`` (read by the Tk thread):

    ``("snapshot", dict)``  a fresh diagnostics snapshot (also means connected)
    ``("failed", None)``    this poll failed but the bridge is still healthy
    ``("disconnected", None)`` the bridge itself no longer answers
    ``("exported", path)`` / ``("export_failed", msg)`` / ``("restart_done", (ok, msg))``
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


def restart_enabled(
    connected: bool, playback: Any, selected: Any, pending: bool = False
) -> bool:
    """Only a connected window with a matched, still-active playback may restart."""
    if not connected or pending:
        return False
    row = match_selection(playback, selected)
    if row is None or row.get("state") in _INACTIVE_STATES:
        return False
    player_pid, fid = row.get("player_pid"), row.get("fid")
    return type(player_pid) is int and isinstance(fid, str) and bool(fid)


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

        self._transfer_var: Any = None
        self._transfer_tree: Any = None
        self._account_var: Any = None
        self._protection_var: Any = None
        self._notice_var: Any = None
        self._playback_tree: Any = None
        self._errors_tree: Any = None
        self._restart_button: Any = None
        self._export_button: Any = None

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
            self._transfer_tree = None
            self._playback_tree = None
            self._errors_tree = None
            self._restart_button = None
            self._export_button = None
            self._transfer_var = None
            self._account_var = None
            self._protection_var = None
            self._notice_var = None

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
        tree.delete(*tree.get_children())
        self._rows = {}
        previous = self._selected
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
        if previous is not None and not restored:
            self._selected = None

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
        self._selected = self._rows.get(selection[0]) if selection else None
        self._update_actions()

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
