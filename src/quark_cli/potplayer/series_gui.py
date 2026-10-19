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
    """Minute/second input for one end of the shared skip control."""
    def __init__(self, tk, ttk, parent, title):
        frame = ttk.LabelFrame(parent, text=title, padding=10)
        frame.pack(side='left', fill='x', expand=True, padx=4)
        self.minutes, self.seconds = tk.StringVar(value='0'), tk.StringVar(value='0')
        self._moving = False
        self.on_change = lambda: None
        row = ttk.Frame(frame)
        row.pack(fill='x')
        ttk.Spinbox(row, from_=0, to=60, textvariable=self.minutes, width=5).pack(side='left')
        ttk.Label(row, text=' 分 ').pack(side='left')
        ttk.Spinbox(row, from_=0, to=59, textvariable=self.seconds, width=5).pack(side='left')
        ttk.Label(row, text=' 秒').pack(side='left')
        self._traces = [(variable, variable.trace_add('write', self._typed))
                        for variable in (self.minutes, self.seconds)]

    def close(self):
        for variable, trace in self._traces:
            variable.trace_remove('write', trace)
        self._traces.clear()
        self.on_change = lambda: None

    def get(self):
        minute, second = int(self.minutes.get()), int(self.seconds.get())
        if minute < 0 or not 0 <= second <= 59:
            raise UsageError('秒数须为 0–59')
        return seconds(minute * 60 + second)

    def set(self, total):
        self._moving = True
        try:
            self.minutes.set(str(total // 60))
            self.seconds.set(str(total % 60))
        finally:
            self._moving = False
        self.on_change()

    def _typed(self, *_args):
        if self._moving:
            return
        try:
            self.get()
        except (ValueError, UsageError):
            return
        self.on_change()


class SkipRangeControl:
    """One track, opposing handles, independent skip durations (not media position)."""
    def __init__(self, tk, parent, intro, outro):
        from .ui_theme import BACKGROUND, ACCENT
        self.intro, self.outro = intro, outro
        self.limit = 600
        self.active = 'intro'
        self.canvas = tk.Canvas(parent, height=116, width=480, background=BACKGROUND,
                                highlightthickness=1, highlightbackground=BACKGROUND,
                                highlightcolor=ACCENT, takefocus=True)
        self.canvas.pack(fill='x', pady=(0, 12))
        intro.on_change = outro.on_change = self.draw
        self.canvas.bind('<Configure>', lambda _event: self.draw())
        self.canvas.bind('<Button-1>', self._press)
        self.canvas.bind('<B1-Motion>', self._drag)
        self.canvas.bind('<Left>', lambda _event: self._step(-1))
        self.canvas.bind('<Right>', lambda _event: self._step(1))
        self.canvas.bind('<Home>', lambda _event: self._set(0))
        self.canvas.bind('<End>', lambda _event: self._set(self.limit))
        self.canvas.bind('<space>', self._switch)
        self.draw()

    def _geometry(self):
        return 18, max(1, (self.canvas.winfo_width() - 36) / 2)

    def positions(self):
        start, half = self._geometry()
        return (start + half * self.intro.get() / self.limit,
                start + half * (2 - self.outro.get() / self.limit))

    def draw(self):
        from .ui_theme import ACCENT, BORDER, CONTROL, TEXT, MUTED
        try:
            intro, outro = self.intro.get(), self.outro.get()
        except (ValueError, UsageError):
            return
        self.limit = max(self.limit, ((max(intro, outro) + 59) // 60) * 60)
        left, right = self.positions()
        start, half = self._geometry()
        end = start + 2 * half
        c = self.canvas
        c.delete('all')
        c.create_text(start, 20, text=f'片头  {intro//60} 分 {intro%60:02} 秒  →', fill=TEXT, anchor='w')
        c.create_text(end, 20, text=f'←  片尾  {outro//60} 分 {outro%60:02} 秒', fill=TEXT, anchor='e')
        c.create_line(start, 58, end, 58, fill=BORDER, width=4)
        c.create_line(start, 58, left, 58, fill=ACCENT, width=4)
        c.create_line(right, 58, end, 58, fill=ACCENT, width=4)
        for name, x in (('intro', left), ('outro', right)):
            c.create_rectangle(x-5, 46, x+5, 70, outline=ACCENT,
                               fill=ACCENT if name == self.active else CONTROL, width=2)
        c.create_text(start, 95, text='0 分', fill=MUTED, anchor='w')
        c.create_text(start+half, 95, text=f'每端 0–{self.limit//60} 分钟', fill=MUTED)
        c.create_text(end, 95, text='0 分', fill=MUTED, anchor='e')

    def _press(self, event):
        try:
            left, right = self.positions()
        except (ValueError, UsageError):
            return
        self.active = 'intro' if abs(event.x-left) <= abs(event.x-right) else 'outro'
        self.canvas.focus_set()
        self._drag(event)

    def _drag(self, event):
        start, half = self._geometry()
        ratio = (event.x-start)/half if self.active == 'intro' else (start+2*half-event.x)/half
        self._set(round(max(0, min(1, ratio)) * self.limit))

    def _set(self, value):
        (self.intro if self.active == 'intro' else self.outro).set(value)
        return 'break'

    def _step(self, direction):
        control = self.intro if self.active == 'intro' else self.outro
        try:
            value = control.get() + direction * (1 if self.active == 'intro' else -1)
        except (ValueError, UsageError):
            return 'break'
        return self._set(max(0, min(self.limit, value)))

    def _switch(self, _event):
        self.active = 'outro' if self.active == 'intro' else 'intro'
        self.draw()
        return 'break'


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
        self._root.geometry('580x440')
        self._root.minsize(560, 430)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill='both', expand=True)
        self._name = tk.StringVar(value='正在读取剧集设置……')
        self._notice = tk.StringVar(value='')
        ttk.Label(frame, textvariable=self._name, wraplength=450).pack(anchor='w', pady=(0, 14))
        track = ttk.Frame(frame)
        track.pack(fill='x')
        entries = ttk.Frame(frame)
        entries.pack(fill='x')
        self._intro = DurationControl(tk, ttk, entries, '跳过片头')
        self._outro = DurationControl(tk, ttk, entries, '跳过片尾')
        self._range = SkipRangeControl(tk, track, self._intro, self._outro)
        ttk.Label(frame, text='左端向右拖设置片头，右端向左拖设置片尾；也可输入分秒。\n'
                  '0 分 0 秒为关闭，每端最多 60 分钟。方向键微调，空格切换端点。\n'
                  '仅作用于当前剧集及其不同清晰度版本。\n'
                  '续播和精彩标记优先；片尾跳至结束前 1 秒，随后按播放器结束设置处理。',
                  wraplength=530, justify='left', style='Muted.TLabel').pack(anchor='w', pady=14)
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

    def _shutdown(self):
        for name in ('_intro', '_outro'):
            control = getattr(self, name, None)
            if control is not None:
                control.close()
                setattr(self, name, None)
        self._range = None
        super()._shutdown()


def run_series_settings(series, account):
    endpoint = load_endpoint()
    if endpoint is None or not series or not account:
        return EXIT_UNAVAILABLE
    client = SeriesClient(endpoint, series, account)
    return SeriesPanel(client, endpoint, series, account).run()
