"""The downloader's Tkinter window (``python -m quark_cli.downloader``).

Every network call and every byte of a download runs on the worker thread of
:mod:`quark_cli.downloader.worker`. The Tk thread only submits requests and
renders the events that come back on a queue, so a folder listing or a file
transfer never blocks the window.

The login panel is the shared
:class:`~quark_cli.potplayer.login_gui.QrLoginWindow`, opened as a modal child of
this window: ``Toplevel``, served by a nested event loop that ends with the
child, so closing or completing the scan returns into :meth:`DownloaderWindow.login`
while this window's own ``mainloop`` keeps running. It stores the CLI's own
Cookie (``auth.credential_path()``), so both front-ends share one login and there
is no second credential file to keep in sync.

Nothing here creates a window at import time: the widget references are plain
attributes, so the rendering methods are covered by tests with stand-in widgets.
"""

from __future__ import annotations

import contextlib
import queue
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..auth import LoginCancelled, get_cookie
from ..errors import LocalIOError, QuarkError
from ..potplayer.login_gui import QrLoginWindow
from .core import (
    DOWNLOAD_ACTION,
    ROOT_FID,
    Entry,
    Location,
    describe_error,
    display_text,
    download_plan,
)
from .worker import (
    Backend,
    Browse,
    Download,
    DriveBackend,
    Failed,
    Finished,
    Listing,
    Progress,
    Worker,
)

WINDOW_TITLE = "夸克网盘下载器"
POLL_MILLISECONDS = 120
IDLE_STATUS = "请选择一个文件或目录，然后点击「下载选中」"
LOGIN_HINT = "尚未登录：请点击「扫码登录」，用手机夸克 App 扫码"
CLOSE_BUSY_STATUS = "正在下载：请等待当前下载完成后再关闭窗口"


def _set_state(widget: Any, state: str) -> None:
    if widget is not None:
        widget.configure(state=state)


def _tkinter() -> Any:
    try:
        import tkinter
    except ImportError:
        raise LocalIOError("缺少 Tkinter，无法显示下载窗口") from None
    import tkinter.filedialog  # noqa: F401  (registers tkinter.filedialog)
    import tkinter.ttk  # noqa: F401  (registers tkinter.ttk)

    return tkinter


def default_login_window(root: Any) -> QrLoginWindow:
    """Open the shared QR window as a modal child of the downloader window."""
    import tkinter

    return QrLoginWindow(root_factory=lambda: tkinter.Toplevel(root))


class DownloaderWindow:
    """Browse the drive and download one file or one folder tree.

    ``cookie`` supplies the current Cookie on every request, so a login performed
    while the window is open is picked up without any session bookkeeping; by
    default it is :func:`quark_cli.auth.get_cookie` with the CLI's own precedence
    and saved Cookie.
    """

    def __init__(
        self,
        *,
        cookie: Callable[[], str] | None = None,
        backend_factory: Callable[[Callable[[], str]], Backend] | None = None,
        login_window_factory: Callable[[Any], Any] | None = None,
        root_factory: Callable[[], Any] | None = None,
        transport: Any = None,
    ) -> None:
        self._cookie = get_cookie if cookie is None else cookie
        factory = backend_factory or (lambda loader: DriveBackend(loader, transport=transport))
        self._worker = Worker(factory(self._cookie))
        self._login_window_factory = login_window_factory or default_login_window
        self._root_factory = root_factory
        self._location = Location()
        self._entries: dict[str, Entry] = {}
        self._output_dir = ""
        self._downloading = False
        self._logging_in = False
        self._indeterminate = False
        self._root: Any = None
        self._tree: Any = None
        self._progress: Any = None
        self._status_var: Any = None
        self._location_var: Any = None
        self._output_var: Any = None
        self._up_button: Any = None
        self._refresh_button: Any = None
        self._login_button: Any = None
        self._output_button: Any = None
        self._download_button: Any = None
        self._pump_id: str | None = None

    # -- lifecycle -------------------------------------------------------

    def run(self) -> None:
        """Create the window, serve events until it closes, then tear it down."""
        tk = _tkinter()
        try:
            try:
                self._root = (self._root_factory or tk.Tk)()
                self._build(tk)
            except tk.TclError:
                raise LocalIOError("无法创建下载窗口，请检查桌面环境") from None
            self._worker.start()
            self._initialise()
            self._pump()
            self._root.mainloop()
        finally:
            self._shutdown()

    def close(self) -> None:
        """Close the window (Tk thread only), refused while a download runs.

        A download is not cancellable and must not be cut off by the process
        exiting underneath it: the window stays up with a hint until the
        transfer ends, then the usual close works.
        """
        if self._downloading:
            self._set_status(CLOSE_BUSY_STATUS)
            return
        self._shutdown()

    def _build(self, tk: Any) -> None:
        root = self._root
        root.title(WINDOW_TITLE)
        root.geometry("820x560")
        root.minsize(660, 420)

        toolbar = tk.Frame(root)
        toolbar.pack(fill="x", padx=12, pady=(12, 6))
        self._up_button = tk.Button(toolbar, text="返回上级", width=10, command=self.go_up)
        self._refresh_button = tk.Button(toolbar, text="刷新", width=8, command=self.refresh)
        self._login_button = tk.Button(toolbar, text="扫码登录", width=10, command=self.login)
        for button in (self._up_button, self._refresh_button, self._login_button):
            button.pack(side="left", padx=(0, 6))
        self._location_var = tk.StringVar(value=self._location.title)
        tk.Label(toolbar, textvariable=self._location_var, anchor="w").pack(
            side="left", fill="x", expand=True, padx=(6, 0)
        )

        tree = self._tree = tk.ttk.Treeview(root, columns=("type", "size"), show="tree headings", selectmode="browse")
        tree.heading("#0", text="名称")
        tree.heading("type", text="类型")
        tree.heading("size", text="大小")
        tree.column("#0", width=440, anchor="w")
        tree.column("type", width=80, anchor="center", stretch=False)
        tree.column("size", width=120, anchor="e", stretch=False)
        tree.pack(fill="both", expand=True, padx=12, pady=6)
        tree.bind("<Double-1>", self._on_activate)
        tree.bind("<<TreeviewSelect>>", self._on_selection)

        bar = tk.Frame(root)
        bar.pack(fill="x", padx=12, pady=(0, 4))
        self._progress = tk.ttk.Progressbar(bar, mode="determinate", maximum=100.0)
        self._progress.pack(fill="x")

        actions = tk.Frame(root)
        actions.pack(fill="x", padx=12, pady=(4, 4))
        self._output_button = tk.Button(actions, text="选择保存目录", width=12, command=self.choose_output)
        self._output_button.pack(side="left", padx=(0, 6))
        self._output_var = tk.StringVar(value="未选择本地保存目录")
        tk.Label(actions, textvariable=self._output_var, anchor="w").pack(side="left", fill="x", expand=True, padx=6)
        self._download_button = tk.Button(actions, text="下载选中", width=10, command=self.start_download)
        self._download_button.pack(side="right")

        self._status_var = tk.StringVar(value=IDLE_STATUS)
        tk.Label(root, textvariable=self._status_var, anchor="w", wraplength=780, justify="left").pack(
            fill="x", padx=12, pady=(0, 12)
        )
        root.protocol("WM_DELETE_WINDOW", self.close)

    def _initialise(self) -> None:
        self._sync_controls()
        if self._credential_available():
            self._open(ROOT_FID)
        else:
            self._set_status(LOGIN_HINT)

    def _shutdown(self) -> None:
        """Release the Tk objects on the thread that created them, bounded.

        The window disappears before the worker is joined. :meth:`close` refuses
        to get here while a download is running, so in the normal flow the
        worker is idle between requests and the join returns at once; the bound
        only covers an in-flight request that a destroyed root forced out of the
        event loop (a session logoff, say). Only the daemon thread would outlive
        the process then, and the library's atomic publish keeps the target file
        itself complete either way.
        """
        root, self._root = self._root, None
        pump_id, self._pump_id = self._pump_id, None
        if root is not None:
            with contextlib.suppress(Exception):
                if pump_id is not None:
                    root.after_cancel(pump_id)
            with contextlib.suppress(Exception):
                root.destroy()
        self._tree = None
        self._progress = None
        self._status_var = None
        self._location_var = None
        self._output_var = None
        self._up_button = None
        self._refresh_button = None
        self._login_button = None
        self._output_button = None
        self._download_button = None
        self._worker.stop()

    # -- event loop ------------------------------------------------------

    def _pump(self) -> None:
        if self._root is None:
            return
        self._drain()
        self._pump_id = self._root.after(POLL_MILLISECONDS, self._pump)

    def _drain(self) -> None:
        """Apply every event the worker has produced so far (Tk thread only)."""
        while True:
            try:
                event = self._worker.events.get_nowait()
            except queue.Empty:
                return
            self._handle(event)

    def _handle(self, event: Any) -> None:
        if isinstance(event, Listing):
            # Navigating again before this answer arrived makes it stale: only
            # the folder the window is actually showing may fill the table, or
            # the rows would contradict the location line.
            if event.fid != self._location.fid:
                return
            self._render_listing(event)
            self._set_status(IDLE_STATUS)
        elif isinstance(event, Progress):
            self._set_status(event.text)
            self._show_progress(event.fraction)
        elif isinstance(event, Finished):
            self._downloading = False
            self._stop_progress(100.0)
            self._set_status(event.summary)
            self._sync_controls()
        elif isinstance(event, Failed):
            # A failed listing says nothing about a transfer that is already
            # running: the window may have submitted the download from an older
            # list while the browse was still in flight, so ending the download
            # state here would unlock :meth:`close` under a live download. Only
            # the download's own failure ends it; a browse failure just reports.
            if event.action == DOWNLOAD_ACTION:
                self._downloading = False
                self._stop_progress(0.0)
            self._set_status(event.message)
            self._sync_controls()

    # -- actions ---------------------------------------------------------

    def go_up(self) -> None:
        if self._downloading:
            return
        if not self._location.up():
            self._set_status("已经在最上层目录")
            return
        self._show_location()
        self._open(self._location.fid)

    def refresh(self) -> None:
        self._open(self._location.fid)

    def choose_output(self) -> None:
        if self._downloading:
            return
        import tkinter.filedialog as filedialog

        chosen = filedialog.askdirectory(
            title="选择下载保存目录",
            initialdir=self._output_dir or str(Path.home()),
            mustexist=True,
        )
        if not chosen:
            return
        self._output_dir = chosen
        self._output_var.set(display_text(chosen))
        self._sync_controls()

    def start_download(self) -> None:
        if self._downloading:
            return
        entry = self._selected()
        if entry is None:
            self._set_status("请先在列表中选择一个文件或目录")
            return
        try:
            plan = download_plan(self._output_dir, entry)
        except (QuarkError, OSError) as error:
            self._set_status(describe_error(error, DOWNLOAD_ACTION))
            return
        self._downloading = True
        self._indeterminate = False
        self._stop_progress(0.0)
        self._set_status(f"正在下载 {entry.label}……")
        self._sync_controls()
        self._worker.submit(
            Download(
                fid=plan.fid,
                target=str(plan.target),
                recursive=plan.recursive,
                # Downloads never replace an existing file: the library refuses it
                # and the refusal is shown as a readable conflict message.
                overwrite=False,
                name=entry.name,
            )
        )

    def login(self) -> None:
        if self._downloading or self._logging_in:
            return
        self._logging_in = True
        self._sync_controls()
        succeeded = False
        try:
            self._login_window_factory(self._root).run()
            succeeded = True
        except LoginCancelled:
            self._set_status("已取消扫码登录")
        except Exception as error:
            # ``LoginCancelled`` is checked first; every other failure (a typed
            # QuarkError, an OSError, or anything unexpected) becomes one
            # readable line instead of a traceback in a windowed program.
            self._set_status(describe_error(error, "扫码登录"))
        finally:
            self._logging_in = False
            self._sync_controls()
        if succeeded:
            self._open(self._location.fid)

    # -- rendering -------------------------------------------------------

    def _render_listing(self, listing: Listing) -> None:
        tree = self._tree
        tree.delete(*tree.get_children())
        self._entries = {}
        for index, entry in enumerate(listing.entries):
            iid = f"row{index}"
            self._entries[iid] = entry
            tree.insert("", "end", iid=iid, text=entry.label, values=(entry.type_label, entry.size_label))

    def _show_location(self) -> None:
        if self._location_var is not None:
            self._location_var.set(self._location.title)

    def _set_status(self, text: str) -> None:
        if self._status_var is not None:
            self._status_var.set(display_text(text))

    def _show_progress(self, fraction: float | None) -> None:
        bar = self._progress
        if bar is None:
            return
        if fraction is None:
            if not self._indeterminate:
                self._indeterminate = True
                bar.configure(mode="indeterminate")
                bar.start(80)
            return
        if self._indeterminate:
            self._indeterminate = False
            bar.stop()
            bar.configure(mode="determinate")
        bar.configure(value=fraction * 100.0)

    def _stop_progress(self, value: float) -> None:
        bar = self._progress
        if bar is None:
            return
        if self._indeterminate:
            self._indeterminate = False
            bar.stop()
            bar.configure(mode="determinate")
        bar.configure(value=value)

    def _sync_controls(self) -> None:
        busy = self._downloading or self._logging_in
        state = "disabled" if busy else "normal"
        for button in (self._up_button, self._refresh_button):
            _set_state(button, state)
        _set_state(self._login_button, "disabled" if self._downloading else "normal")
        _set_state(self._output_button, state)
        ready = not busy and bool(self._output_dir) and self._selected() is not None
        _set_state(self._download_button, "normal" if ready else "disabled")

    # -- helpers ---------------------------------------------------------

    def _credential_available(self) -> bool:
        try:
            self._cookie()
        except QuarkError:
            return False
        return True

    def _open(self, fid: str) -> None:
        if self._downloading:
            return
        self._set_status("正在读取目录……")
        self._worker.submit(Browse(fid))

    def _selected(self) -> Entry | None:
        if self._tree is None:
            return None
        selection = self._tree.selection()
        if not selection:
            return None
        return self._entries.get(selection[0])

    def _on_selection(self, _event: Any) -> None:
        self._sync_controls()

    def _on_activate(self, _event: Any) -> None:
        # The tree stays clickable during a download, so entering a folder has
        # to be refused here too; otherwise the location line and the table
        # would disagree, because ``_open`` refuses to browse while busy.
        if self._downloading:
            return
        entry = self._selected()
        if entry is None or not entry.directory:
            return
        self._location.enter(entry.fid, entry.name)
        self._show_location()
        self._open(entry.fid)


def run_window() -> int:
    """Open the download window and block until it is closed."""
    DownloaderWindow().run()
    return 0
