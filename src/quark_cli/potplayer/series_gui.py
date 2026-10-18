"""Small, independent per-series settings window; no diagnostics dependency."""
from urllib.parse import urlencode
from ..errors import UsageError
from .series_settings import seconds
from .gui_transport import LocalClient, LocalWorker, LocalPanel
from .gui_common import load_endpoint, EXIT_UNAVAILABLE


class SeriesClient(LocalClient):
    def __init__(self, endpoint, series, account, **kwargs):
        super().__init__(endpoint, **kwargs)
        self.series, self.bound_account = series, account

    def _account_header(self):
        return {'X-Quark-Account': self.bound_account}

    def snapshot(self):
        return self._get_object('/v1/series-settings?' + urlencode({'series': self.series}))


class SeriesWorker(LocalWorker):
    def _command(self, request):
        if request and request[0] == 'save':
            self._emit('saved', self._client._post_accepted('/v1/series-settings', request[1]))


class SeriesPanel(LocalPanel):
    title = '片头片尾设置'
    worker_type = SeriesWorker

    def __init__(self, client, endpoint, series, account):
        super().__init__(client, endpoint)
        self.series, self._account_id = series, account
        self._loaded = False
        self._pending = False

    def _build(self, tk, root):
        from tkinter import ttk
        self._root.geometry('500x300')
        self._root.minsize(460, 280)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill='both', expand=True)
        self._name = tk.StringVar(value='正在读取剧集设置……')
        self._notice = tk.StringVar(value='')
        ttk.Label(frame, textvariable=self._name, wraplength=450).pack(anchor='w', pady=(0, 14))
        self._intro, self._outro = tk.StringVar(value='0'), tk.StringVar(value='0')
        for text, variable in (('跳过片头（秒）', self._intro), ('跳过片尾（秒）', self._outro)):
            row = ttk.Frame(frame)
            row.pack(fill='x', pady=5)
            ttk.Label(row, text=text, width=20).pack(side='left')
            ttk.Entry(row, textvariable=variable, width=12).pack(side='left')
        ttk.Label(frame, text='填 0 关闭，范围 0–3600 秒；仅作用于当前剧集及其不同清晰度版本。\n'
                  '续播和精彩标记优先；片尾跳至结束前 1 秒，随后按播放器结束设置处理。',
                  wraplength=450, justify='left').pack(anchor='w', pady=10)
        actions = ttk.Frame(frame)
        actions.pack(fill='x')
        self._save_button = ttk.Button(actions, text='保存设置', command=self.save, state='disabled')
        self._save_button.pack(side='left')
        ttk.Button(actions, text='关闭', command=self._close).pack(side='right')
        ttk.Label(frame, textvariable=self._notice, wraplength=450).pack(anchor='w', pady=8)

    def save(self):
        try:
            intro, outro = seconds(int(self._intro.get())), seconds(int(self._outro.get()))
        except (ValueError, UsageError):
            self._notice.set('请输入 0–3600 的整数秒，填 0 关闭')
            return
        self._pending = True
        self._save_button.configure(state='disabled')
        self._notice.set('正在保存……')
        self._queue_command(('save', {'series': self.series, 'intro_seconds': intro, 'outro_seconds': outro}))

    def _handle(self, kind, value):
        if kind == 'scoped':
            scope, event, payload = value
            if scope == self._account_id:
                self._handle(event, payload)
        elif kind == 'snapshot':
            self._connected = True
            self._name.set(value['name'])
            if not self._loaded:
                self._intro.set(str(value['intro_seconds']))
                self._outro.set(str(value['outro_seconds']))
                self._loaded = True
            if not self._pending:
                self._save_button.configure(state='normal')
        elif kind == 'saved':
            self._pending = False
            self._notice.set('已保存，播放时自动应用' if value else '保存失败；若已切换账号，请关闭窗口后重新打开')
            self._save_button.configure(state='normal' if self._connected else 'disabled')
        elif kind in ('failed', 'disconnected'):
            self._show_disconnected()

    def _show_disconnected(self):
        self._connected = False
        self._save_button.configure(state='disabled')
        self._notice.set('无法读取设置；若已切换账号，请关闭窗口后重新打开')


def run_series_settings(series, account):
    endpoint = load_endpoint()
    if endpoint is None or not series or not account:
        return EXIT_UNAVAILABLE
    client = SeriesClient(endpoint, series, account)
    return SeriesPanel(client, endpoint, series, account).run()
