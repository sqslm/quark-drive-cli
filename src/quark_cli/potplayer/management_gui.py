"""Window shell: hosts independent pages, owns no login or highlight state."""
from __future__ import annotations

from typing import Any, Callable

from ..errors import LocalIOError
from . import plugin_state
from .accounts_gui import AccountClient, AccountsPanel
from .highlights_gui import HighlightsClient, HighlightsPanel
from .status_gui import BridgeClient, StatusWindow
from .gui_common import (
    EXIT_OK, EXIT_UNAVAILABLE, WINDOW_WIDTH, WINDOW_HEIGHT, MIN_WIDTH, MIN_HEIGHT,
    _apply_font, _tkinter, _widgets, load_endpoint,
)


class ManagementWindow:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.pages = []
        self.tabs = None
        self.root = None

    def run(self):
        from tkinter import ttk, font
        tk = _tkinter()
        root = self.root = tk.Tk()
        root.title('夸克网盘插件管理')
        root.geometry(f'{WINDOW_WIDTH}x{WINDOW_HEIGHT}')
        root.minsize(MIN_WIDTH, MIN_HEIGHT)
        _apply_font(root, font, ttk)
        root.protocol('WM_DELETE_WINDOW', root.quit)
        tabs = self.tabs = ttk.Notebook(root)
        tabs.pack(fill='both', expand=True)
        try:
            for title, page_type, client_type in (
                ('播放诊断', StatusWindow, BridgeClient),
                ('账号管理', AccountsPanel, AccountClient),
                ('精彩标记', HighlightsPanel, HighlightsClient),
            ):
                frame = ttk.Frame(tabs)
                tabs.add(frame, text=title)
                client = client_type(self.endpoint) if self.endpoint is not None else None
                page = page_type(client, self.endpoint)
                self.pages.append(page)
                page.mount(tk, root, frame)
            def activate(_event):
                if tabs.index(tabs.select()) == 1:
                    self.pages[1].activate()
            tabs.bind('<<NotebookTabChanged>>', activate)
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
