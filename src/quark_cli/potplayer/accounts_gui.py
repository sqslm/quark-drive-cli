"""Account list, QR login, switching and capacity; independent of playback diagnostics."""
from __future__ import annotations
import httpx
from .gui_common import (
    _DISCONNECTED_NOTE,
    format_bytes,
)
from .gui_transport import LocalClient, LocalWorker, LocalPanel


class AccountClient(LocalClient):
    def snapshot(self):
        try:
            response = self._client.get('/v1/accounts')
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None
    def account_action(self, action: str, key: str | None):
        path = '/v1/auth/login' if action == 'login' else '/v1/accounts/' + action
        try:
            response = self._client.post(path, json={'id': key}, timeout=30.0,
                                         headers=self._account_header())
            return 200 <= response.status_code < 300
        except httpx.HTTPError:
            return False


class AccountWorker(LocalWorker):
    def _command(self, request):
        if request and request[0] == 'account':
            ok = self._client.account_action(request[1], request[2])
            self._emit('account_done', (request[1], ok))
            self._client.account_id = None
            self.tick()

class AccountsPanel(LocalPanel):
    worker_type = AccountWorker
    def __init__(self, client, endpoint):
        super().__init__(client, endpoint)
        self._accounts = []
        self._account_pending = self._login_waiting = self._account_tab_seen = False
        self._accounts_tree = self._accounts_notice = self._current_account_var = None
        self._account_buttons = {}
        self._failed = False

    def _build(self, tk, root):
        from tkinter import ttk
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)
        self._build_accounts(tk, ttk, frame)

    def _handle(self, kind, value):
        if kind == 'scoped':
            scope, event, payload = value
            if scope == self._account_id:
                self._handle(event, payload)
        elif kind == 'account_done':
            self._account_pending = False
            self._accounts_notice.set('账号操作已完成' if value[1] else '账号操作失败，请检查账号文件或登录状态')
            self._update_account_buttons()
        elif kind == 'snapshot':
            self._connected = True
            error = value.get('error')
            self._failed = isinstance(error, dict)
            if self._failed:
                self._accounts_notice.set('账号模块不可用：' + error.get('message', '请检查账号记录')
                                          + '；播放诊断与精彩标记仍可独立使用。')
                self._update_account_buttons()
                return
            account_id = value.get('active') or ''
            if account_id != self._account_id:
                self._account_pending = False
            self._account_id = account_id
            self._refresh_accounts({'accounts': value, 'login_state': value.get('login_state')})
        elif kind in ('failed', 'disconnected'):
            self._show_disconnected()

    def _show_disconnected(self):
        self._connected = False
        self._accounts_notice.set(_DISCONNECTED_NOTE)
        self._update_account_buttons()

    def activate(self):
        if not self._account_tab_seen and self._connected and self._account_id and not self._failed:
            self._account_tab_seen = True
            self._account_command('refresh')

    def _shutdown(self):
        super()._shutdown()
        self._account_buttons.clear()
    def _build_accounts(self, tk, ttk, frame):
        self._current_account_var = tk.StringVar(value='当前账号：未登录')
        self._accounts_notice = tk.StringVar(value='切换会保留其他账号登录；退出只清除所选账号凭证。')
        ttk.Label(frame, textvariable=self._current_account_var, font=('Microsoft YaHei UI', 13)).pack(anchor='w', pady=(0, 12))
        self._accounts_tree = ttk.Treeview(frame, columns=('nickname', 'state', 'remaining', 'total'),
                                          show='headings', selectmode='browse')
        for column, title, width in (('nickname', '账号昵称', 220), ('state', '登录状态', 110),
                                     ('remaining', '剩余容量', 150), ('total', '总容量', 150)):
            self._accounts_tree.heading(column, text=title)
            self._accounts_tree.column(column, width=width, stretch=True)
        self._accounts_tree.pack(fill='both', expand=True)
        self._accounts_tree.bind('<<TreeviewSelect>>', lambda _event: self._update_account_buttons())
        actions = ttk.Frame(frame)
        actions.pack(fill='x', pady=12)
        for action, title in (('login', '扫码添加账号'), ('switch', '切换到所选账号'),
                              ('logout', '退出所选账号'), ('refresh', '刷新当前昵称与容量')):
            button = ttk.Button(actions, text=title, command=lambda action=action: self._account_command(action))
            button.pack(side='left', padx=(0, 8))
            self._account_buttons[action] = button
        ttk.Label(frame, textvariable=self._accounts_notice, wraplength=720).pack(anchor='w')
        ttk.Label(frame, text='容量为最近一次查询结果。切换会中断旧播放，请从网盘根目录重新打开视频。\n'
                              '最近观看、进度、收藏和精彩标记按账号保存；退出账号保留本机记录。',
                  wraplength=720, justify='left').pack(anchor='w', pady=(12, 0))
        self._update_account_buttons()

    def _refresh_accounts(self, snapshot):
        payload = snapshot.get('accounts')
        if not isinstance(payload, dict) or self._accounts_tree is None:
            return
        self._accounts = payload.get('items', [])
        self._login_waiting = snapshot.get('login_state') == 'waiting'
        selected = self._accounts_tree.selection()
        self._accounts_tree.delete(*self._accounts_tree.get_children())
        current_name = '未登录'
        for row in self._accounts:
            key = row['id']
            capacity = row.get('capacity')
            capacity = capacity if isinstance(capacity, dict) else {}
            total, used = capacity.get('total_bytes'), capacity.get('used_bytes')
            remaining = max(0, total - used) if type(total) is int and type(used) is int else None
            state = '当前账号' if row['current'] else ('已登录' if row['logged_in'] else '已退出 / 已失效')
            self._accounts_tree.insert('', 'end', iid=key,
                                       values=(row['nickname'], state, format_bytes(remaining), format_bytes(total)))
            if row['current']:
                current_name = row['nickname']
        self._current_account_var.set('当前账号：' + current_name)
        target = next((key for key in selected if self._accounts_tree.exists(key)), None) or payload.get('active')
        if target:
            self._accounts_tree.selection_set(target)
        if self._login_waiting:
            self._accounts_notice.set('等待扫码确认；关闭扫码窗口可取消，原账号保持登录。')
        elif not self._account_pending and self._accounts_notice.get().startswith('等待扫码'):
            self._accounts_notice.set('扫码已结束；账号列表已更新。')
        self._update_account_buttons()

    def _update_account_buttons(self):
        if self._accounts_tree is None:
            return
        selection = self._accounts_tree.selection()
        row = next((row for row in self._accounts if selection and row['id'] == selection[0]), None)
        ready = self._connected and not self._failed and not self._account_pending and not self._login_waiting
        for action, button in self._account_buttons.items():
            enabled = ready
            if action == 'switch':
                enabled = enabled and row is not None and row['logged_in'] and not row['current']
            elif action == 'logout':
                enabled = enabled and row is not None and row['logged_in']
            elif action == 'refresh':
                enabled = enabled and bool(self._account_id)
            button.state(['!disabled' if enabled else 'disabled'])

    def _account_command(self, action):
        if not self._connected or self._failed or self._account_pending or self._login_waiting:
            return
        selection = self._accounts_tree.selection()
        key = selection[0] if selection else None
        if action in ('switch', 'logout') and not key:
            return
        self._account_pending = True
        self._accounts_notice.set('正在处理账号操作……')
        # Commands retain the account that was visible when issued.
        self._queue_command(('account', action, key))
        self._update_account_buttons()

