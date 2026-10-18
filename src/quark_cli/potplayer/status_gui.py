"""Playback diagnostics only: transfer statistics, live sessions and diagnostic export."""
from __future__ import annotations
from typing import Any
from typing import Callable
import httpx
import json
from .gui_common import (
    MAX_ERROR_ROWS,
    REPORT_PATH,
    RESTART_PATH,
    SNAPSHOT_PATH,
    _DISCONNECTED_NOTE,
    _EXPORT_FAILED_REPORT,
    _EXPORT_FAILED_WRITE,
    _EXPORT_UNAVAILABLE,
    _RESTART_ERROR_LABELS,
    _RESTART_FAILED,
    _RESTART_OK,
    default_export_name,
    format_bytes,
    format_duration_ms,
    format_response_ms,
    format_speed,
    format_timestamp,
    match_selection,
    protection_text,
    restart_enabled,
    state_label,
    validation_label,
)
from . import plugin_state
from .gui_transport import LocalClient, LocalWorker, LocalPanel


def diagnostic_summary(snapshot):
    account = snapshot.get('account') or {}
    protection = snapshot.get('protection') or {}
    if account.get('validation') == 'expired':
        return '账号登录已失效\n请到“账号管理”重新扫码登录。'
    if not account.get('credential_present'):
        return '尚未登录网盘\n请先到“账号管理”添加或选择一个已登录的账号。'
    if protection.get('active'):
        return '网盘暂时限制了访问\n请等待下方显示的时间结束后，再重新打开视频。'
    if protection.get('available') is False:
        return '暂时无法读取插件的连接状态\n请查看下方的问题说明，修复后重新打开插件。'
    states = {row.get('state') for row in snapshot.get('playback', []) if isinstance(row, dict)}
    if 'playing' in states:
        return '视频正在播放\n下载速度为 0 也可能是已有足够缓存，不一定是故障。'
    if 'paused' in states:
        return '视频已暂停\n暂停时下载也可能停止，继续播放后会按需要补充缓存。'
    if 'waiting' in states:
        return '正在打开视频\n请稍等；如果长时间没有画面，可以关闭后重新打开该视频。'
    if states - {'stopped'}:
        return '暂时无法读取播放进度\n请查看播放器是否仍在播放；下方可以保存排查信息。'
    return '当前没有正在播放的视频\n从网盘打开视频后，这里会显示播放和下载情况。'


class BridgeClient(LocalClient):
    def snapshot(self) -> dict[str, Any] | None:
        return self._get_object(SNAPSHOT_PATH)

    def report(self) -> dict[str, Any] | None:
        return self._get_object(REPORT_PATH)

    def restart(self, player_pid: int, fid: str) -> tuple[bool, str]:
        """Ask the bridge to restart one binding; returns ``(ok, fixed message)``."""
        try:
            response = self._client.post(
                RESTART_PATH, json={"player_pid": player_pid, "fid": fid}, headers=self._account_header()
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


class BridgeWorker(LocalWorker):
    def _command(self, request):
        kind = request[0] if request else None
        if kind == 'export':
            self._export(request[1])
        elif kind == 'restart':
            self._emit('restart_done', self._client.restart(request[1], request[2]))
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


class StatusWindow(LocalPanel):
    worker_type = BridgeWorker
    title = '播放诊断'
    def __init__(self, client, endpoint):
        super().__init__(client, endpoint)
        self._playback, self._rows = [], {}
        self._selected = None
        self._restart_pending = self._export_pending = self._rebuilding = False
        self._filedialog = None
        self._transfer_var = self._account_var = self._protection_var = self._notice_var = None
        self._summary_var = None
        self._transfer_tree = self._playback_tree = self._errors_tree = None
        self._restart_button = self._export_button = None
    def _build(self, tk: Any, root: Any) -> None:
        from tkinter import filedialog, ttk

        self._filedialog = filedialog
        self._transfer_var = tk.StringVar(value="—")
        self._summary_var = tk.StringVar(value='正在检查播放情况……')
        self._account_var = tk.StringVar(value="—")
        self._protection_var = tk.StringVar(value="—")
        self._notice_var = tk.StringVar(value="正在连接本地服务……")

        outer = ttk.Frame(root, padding=10)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        outer.rowconfigure(4, weight=1)

        summary = ttk.LabelFrame(outer, text="当前情况", padding=8)
        summary.grid(row=0, column=0, sticky="ew")
        ttk.Label(summary, textvariable=self._summary_var, anchor='w', justify='left',
                  wraplength=730).pack(fill='x', pady=(0, 6))
        ttk.Label(summary, textvariable=self._transfer_var, anchor="w", justify="left").pack(
            fill="x"
        )

        transfer = ttk.LabelFrame(outer, text="本次打开过的视频 · 下载情况", padding=8)
        transfer.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        transfer.columnconfigure(0, weight=1)
        transfer.rowconfigure(0, weight=1)
        self._transfer_tree = ttk.Treeview(
            transfer,
            columns=("name", "bytes", "speed", "rtt"),
            show="headings",
            selectmode="browse",
            height=3,
        )
        for column, title, width in (
            ("name", "视频", 320),
            ("bytes", "已下载", 140),
            ("speed", "下载速度", 130),
            ("rtt", "网盘响应时间", 110),
        ):
            self._transfer_tree.heading(column, text=title)
            self._transfer_tree.column(column, width=width, anchor="w", stretch=(column == "name"))
        self._transfer_tree.grid(row=0, column=0, sticky="nsew")
        transfer_scroll = ttk.Scrollbar(
            transfer, orient="vertical", command=self._transfer_tree.yview
        )
        self._transfer_tree.configure(yscrollcommand=transfer_scroll.set)
        transfer_scroll.grid(row=0, column=1, sticky="ns")

        state = ttk.LabelFrame(outer, text="网盘连接", padding=8)
        state.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(state, textvariable=self._account_var, anchor="w", justify="left").pack(
            fill="x"
        )
        ttk.Label(
            state, textvariable=self._protection_var, anchor="w", justify="left", wraplength=740
        ).pack(fill="x", pady=(4, 0))

        playback = ttk.LabelFrame(outer, text="正在播放的视频 · 选中后可从头播放", padding=8)
        playback.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        outer.rowconfigure(1, weight=2)
        playback.columnconfigure(0, weight=1)
        playback.rowconfigure(0, weight=1)
        self._playback_tree = ttk.Treeview(
            playback,
            columns=("name", "position", "duration", "state"),
            show="headings",
            selectmode="browse",
            height=3,
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

        errors = ttk.LabelFrame(outer, text="最近遇到的问题", padding=8)
        errors.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        errors.columnconfigure(0, weight=1)
        errors.rowconfigure(0, weight=1)
        self._errors_tree = ttk.Treeview(
            errors, columns=("time", "phase", "message"), show="headings", selectmode="browse", height=3
        )
        for column, title, width in (
            ("time", "时间", 90),
            ("phase", "问题位置", 90),
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
            actions, text="从头播放", width=12, command=self._restart
        )
        self._restart_button.pack(side="right", padx=6)
        self._export_button = ttk.Button(
            actions, text="保存排查信息", width=12, command=self._export
        )
        self._export_button.pack(side="right")
    def _handle(self, kind, value):
        if kind == 'scoped':
            scope, event, payload = value
            if scope == self._account_id:
                self._handle(event, payload)
        elif kind == 'snapshot':
            self._apply_snapshot(value)
        elif kind in ('failed', 'disconnected'):
            self._show_disconnected()
        elif kind == 'exported':
            self._export_pending = False
            self._notice_var.set(f'诊断报告已导出：{value}')
            self._update_actions()
        elif kind == 'export_failed':
            self._export_pending = False
            self._notice_var.set(value)
            self._update_actions()
        elif kind == 'restart_done':
            self._restart_pending = False
            self._notice_var.set(value[1])
            self._update_actions()

    def _apply_snapshot(self, snapshot):
        self._connected = True
        account_id = snapshot.get('account_id') or ''
        if self._account_id != account_id:
            self._selected = None
        self._account_id = account_id
        self._snapshot = snapshot
        self._summary_var.set(diagnostic_summary(snapshot))
        self._playback = snapshot.get('playback', [])
        self._refresh_transfer(snapshot)
        self._refresh_account(snapshot)
        self._refresh_playback()
        self._refresh_errors(snapshot)
        self._notice_var.set('已连接本地服务')
        self._update_actions()

    def _show_disconnected(self):
        self._connected = False
        self._notice_var.set(_DISCONNECTED_NOTE)
        self._summary_var.set('暂时无法连接插件\n请关闭此窗口，从 PotPlayer 重新打开“插件管理”。')
        self._playback = []
        self._refresh_playback()
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
            f"下载速度：{format_speed(totals.get('speed_bytes_per_second'))}"
            f"　本次已下载：{format_bytes(totals.get('bytes'))}"
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
        credential = "已保存登录信息" if account.get("credential_present") else "请到账号管理登录"
        last_success = format_timestamp(account.get("last_success_at"))
        self._account_var.set(
            f"{validation_label(account.get('validation'))}　{credential}"
            f"　上次连接成功：{last_success}"
        )
        protection = snapshot.get("protection")
        self._protection_var.set(f"请求限制：{protection_text(protection)}")

    def _refresh_errors(self, snapshot: dict[str, Any]) -> None:
        tree = self._errors_tree
        tree.delete(*tree.get_children())
        errors = snapshot.get("errors")
        events = errors.get("events") if isinstance(errors, dict) else None
        if not isinstance(events, list) or not events:
            tree.insert('', 'end', values=('—', '—', '暂无问题记录；如果播放异常，可保存排查信息。'))
            return
        for event in list(reversed(events))[:MAX_ERROR_ROWS]:
            if not isinstance(event, dict):
                continue
            tree.insert(
                "",
                "end",
                values=(
                    format_timestamp(event.get("at")),
                    {'源站': '视频下载', '凭证': '登录信息', '本地': '电脑文件', '请求': '连接网盘'}.get(
                        event.get('phase'), event.get('phase') or '—'),
                    event.get("message") or "—",
                ),
            )

    def _restart(self) -> None:
        row = match_selection(self._playback, self._selected)
        if row is None:
            return
        if not restart_enabled(self._connected, self._playback, self._selected, self._restart_pending):
            return
        self._restart_pending = True
        self._notice_var.set("正在请求从头开始播放……")
        self._update_actions()
        self._queue_command(("restart", row.get("player_pid"), row.get("fid")))

    def _export(self) -> None:
        if not self._connected:
            self._notice_var.set(_EXPORT_UNAVAILABLE)
            return
        if self._export_pending:
            return
        path = self._filedialog.asksaveasfilename(
            parent=self._root,
            title="保存排查信息 · 播放有问题时可发给开发者",
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

    def _refresh_playback(self) -> None:
        tree = self._playback_tree
        # Read the selection before rebuilding: a programmatic selection change
        # during ``delete``/``selection_set`` may fire ``_on_select``, and the
        # row is restored afterwards so playback actions keep their binding.
        previous = self._selected
        self._rebuilding = True
        try:
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
        finally:
            self._rebuilding = False
    def _on_select(self, _event=None):
        if not self._rebuilding:
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

def run_status_window(runtime_reader: Callable[[], Any] = plugin_state.read_runtime) -> int:
    from .management_gui import run_management_window
    return run_management_window(runtime_reader)
