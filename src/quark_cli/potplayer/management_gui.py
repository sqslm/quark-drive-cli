"""Window shell: hosts independent pages, owns no login or highlight state."""
from __future__ import annotations

from typing import Any, Callable

from ..errors import LocalIOError
from . import plugin_state
from .accounts_gui import AccountClient, AccountsPanel
from .highlights_gui import HighlightsClient, HighlightsPanel
from .installation_gui import InstallationPanel
from .status_gui import BridgeClient, StatusWindow
from .window_ui import configure_window
from .gui_common import (
    EXIT_OK, EXIT_UNAVAILABLE, WINDOW_WIDTH, WINDOW_HEIGHT, MIN_WIDTH, MIN_HEIGHT,
    _tkinter, _widgets, load_endpoint,
)


class ManagementWindow:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.pages = []
        self.tabs = None
        self.root = None

    def run(self):
        from tkinter import ttk
        tk = _tkinter()
        root = self.root = tk.Tk()
        root.title('夸克网盘插件管理')
        root.geometry(f'{WINDOW_WIDTH}x{WINDOW_HEIGHT}')
        root.minsize(MIN_WIDTH, MIN_HEIGHT)
        configure_window(root)
        root.protocol('WM_DELETE_WINDOW', root.quit)
        ttk.Label(root, text='夸克网盘 · PotPlayer 第三方插件', style='Muted.TLabel',
                  padding=(16, 5)).pack(side='bottom', fill='x')
        tabs = self.tabs = ttk.Notebook(root)
        tabs.pack(fill='both', expand=True)
        try:
            for title, page_type, client_type in (
                ('账号管理', AccountsPanel, AccountClient),
                ('精彩标记', HighlightsPanel, HighlightsClient),
                ('播放诊断', StatusWindow, BridgeClient),
                ('安装管理', InstallationPanel, None),
            ):
                frame = ttk.Frame(tabs)
                tabs.add(frame, text=title)
                client = client_type(self.endpoint) if self.endpoint is not None and client_type is not None else None
                page = page_type(client, self.endpoint)
                if isinstance(page, HighlightsPanel):
                    page.consume_requests = False
                self.pages.append(page)
                page.mount(tk, root, frame)
            def activate(_event):
                if tabs.index(tabs.select()) == 0:
                    self.pages[0].activate()
            tabs.bind('<<NotebookTabChanged>>', activate)
            tabs.select(0)
            self.pages[0].activate()
            root.mainloop()
        finally:
            for page in self.pages:
                page._shutdown()
            root.destroy()
            for widget in _widgets(root):
                widget.master = None
                widget.children.clear()
            self.pages.clear()
            self.root = self.tabs = None
        return EXIT_OK


def run_management_window(runtime_reader: Callable[[], Any] = plugin_state.read_runtime) -> int:
    endpoint = load_endpoint(runtime_reader)
    if endpoint is not None:
        client = BridgeClient(endpoint)
        try:
            if not client.healthy():
                endpoint = None
        finally:
            client.close()
    try:
        return ManagementWindow(endpoint).run()
    except LocalIOError:
        return EXIT_UNAVAILABLE
