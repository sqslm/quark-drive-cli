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


class DurationControl:
    """Minute/second entry and an integer-second slider sharing one value."""
    def __init__(self, tk, ttk, parent, title):
        frame = ttk.LabelFrame(parent, text=title, padding=10)
        frame.pack(fill='x', pady=6)
        self.minutes, self.seconds = tk.StringVar(value='0'), tk.StringVar(value='0')
        self._moving = False
        row = ttk.Frame(frame)
        row.pack(fill='x')
        ttk.Spinbox(row, from_=0, to=60, textvariable=self.minutes, width=5).pack(side='left')
        ttk.Label(row, text=' 分 ').pack(side='left')
        ttk.Spinbox(row, from_=0, to=59, textvariable=self.seconds, width=5).pack(side='left')
        ttk.Label(row, text=' 秒').pack(side='left')
        self._limit = tk.StringVar(value='10 分')
        self.slider = ttk.Scale(frame, from_=0, to=600, command=self._slide)
        self.slider.pack(fill='x', pady=(8, 0))
        ends = ttk.Frame(frame)
        ends.pack(fill='x')
        ttk.Label(ends, text='0 分').pack(side='left')
        ttk.Label(ends, textvariable=self._limit).pack(side='right')
        self.minutes.trace_add('write', self._typed)
        self.seconds.trace_add('write', self._typed)

    def get(self):
        minute, second = int(self.minutes.get()), int(self.seconds.get())
        if minute < 0 or not 0 <= second <= 59:
            raise UsageError('秒数须为 0–59')
        return seconds(minute * 60 + second)

    def _position(self, total):
        if total > float(self.slider.cget('to')):
            limit = min(3600, ((total + 59) // 60) * 60)
            self.slider.configure(to=limit)
            self._limit.set(f'{limit // 60} 分')
        self.slider.set(total)

    def set(self, total):
        self._moving = True
        try:
            self.minutes.set(str(total // 60))
            self.seconds.set(str(total % 60))
            self._position(total)
        finally:
            self._moving = False

    def _slide(self, value):
        if not self._moving:
            self.set(round(float(value)))

    def _typed(self, *_args):
        if self._moving:
            return
        try:
            total = self.get()
        except (ValueError, UsageError):
            return
        self._moving = True
        try:
            self._position(total)
        finally:
            self._moving = False


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
        self._root.geometry('540x480')
        self._root.minsize(500, 470)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill='both', expand=True)
        self._name = tk.StringVar(value='正在读取剧集设置……')
        self._notice = tk.StringVar(value='')
        ttk.Label(frame, textvariable=self._name, wraplength=450).pack(anchor='w', pady=(0, 14))
        self._intro = DurationControl(tk, ttk, frame, '跳过片头')
        self._outro = DurationControl(tk, ttk, frame, '跳过片尾')
        ttk.Label(frame, text='拖动滑块或输入分钟和秒数；0 分 0 秒关闭，最多 60 分钟。\n'
                  '仅作用于当前剧集及其不同清晰度版本。\n'
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
            intro, outro = self._intro.get(), self._outro.get()
        except (ValueError, UsageError):
            self._notice.set('请输入 0–60 分钟和 0–59 秒，合计最多 60 分钟')
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
                self._intro.set(value['intro_seconds'])
                self._outro.set(value['outro_seconds'])
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
