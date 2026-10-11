"""The downloader's Tkinter window (``python -m quark_cli.downloader``).

Every network call and every byte of a download runs on the worker thread of
:mod:`quark_cli.downloader.worker`. The Tk thread only submits requests and
renders the events that come back on a queue, so a folder listing or a file
transfer never blocks the window.

One click downloads the whole multi-selection: the entries are frozen in the
order the table shows them, served one at a time, and every outcome is appended
to the item log while the status line follows the item in flight. ``停止下载``
drops the items that have not started and asks the worker to end the one in
flight at the checkpoint the library already persisted, so the same selection
can be submitted again to continue.

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
    Batch,
    Entry,
    Location,
    Outcome,
    batch_summary,
    describe_error,
    display_text,
    download_plan,
    item_status,
    output_folder,
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
    Stopped,
    Worker,
)

WINDOW_TITLE = "夸克网盘下载器"
POLL_MILLISECONDS = 120
IDLE_STATUS = "请选择一个或多个文件或目录，然后点击「下载选中」"
LOGIN_HINT = "尚未登录：请点击「扫码登录」，用手机夸克 App 扫码"
CLOSE_BUSY_STATUS = "正在下载：请等待当前下载完成后再关闭窗口"
STOPPING_STATUS = "正在停止：当前项目将在已保存的检查点结束，请稍候"


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
    """Browse the drive and download the selected files or folder trees.

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
        self._batch: Batch | None = None
        self._downloading = False
        self._stop_requested = False
        self._logging_in = False
        self._indeterminate = False
        self._root: Any = None
        self._tree: Any = None
        self._log: Any = None
        self._progress: Any = None
        self._status_var: Any = None
        self._location_var: Any = None
        self._output_var: Any = None
        self._up_button: Any = None
        self._refresh_button: Any = None
        self._login_button: Any = None
        self._output_button: Any = None
        self._download_button: Any = None
        self._stop_button: Any = None
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
        """Close the window (Tk thread only), refused while a batch is running.

        A stopped batch is not over: the worker still owes the outcome of the
        item it is ending at its checkpoint, and the process must not exit under
        a transfer that is still writing to the library's part file. The window
        therefore stays up until that outcome arrives, then the usual close works.
        """
        if self._downloading:
            self._set_status(STOPPING_STATUS if self._stop_requested else CLOSE_BUSY_STATUS)
            return
        self._shutdown()

    def _build(self, tk: Any) -> None:
        root = self._root
        root.title(WINDOW_TITLE)
        root.geometry("860x620")
        root.minsize(700, 520)

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

        tree = self._tree = tk.ttk.Treeview(root, columns=("type", "size"), show="tree headings", selectmode="extended")
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

        # One line per finished item: the status line only ever describes the item
        # in flight, so this is where per-item successes and failures accumulate.
        self._log = tk.Text(root, height=5, wrap="word", state="disabled")
        self._log.pack(fill="x", padx=12, pady=(0, 4))

        actions = tk.Frame(root)
        actions.pack(fill="x", padx=12, pady=(4, 4))
        self._output_button = tk.Button(actions, text="选择保存目录", width=12, command=self.choose_output)
        self._output_button.pack(side="left", padx=(0, 6))
        self._output_var = tk.StringVar(value="未选择本地保存目录")
        tk.Label(actions, textvariable=self._output_var, anchor="w").pack(side="left", fill="x", expand=True, padx=6)
        self._download_button = tk.Button(actions, text="下载选中", width=10, command=self.start_download)
        self._download_button.pack(side="right")
        self._stop_button = tk.Button(
            actions, text="停止下载", width=10, command=self.stop_download, state="disabled"
        )
        self._stop_button.pack(side="right", padx=(0, 6))

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
        self._log = None
        self._progress = None
        self._status_var = None
        self._location_var = None
        self._output_var = None
        self._up_button = None
        self._refresh_button = None
        self._login_button = None
        self._output_button = None
        self._download_button = None
        self._stop_button = None
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
            self._set_status(self._item_line(event.text))
            self._show_progress(event.fraction)
        elif isinstance(event, Finished):
            self._settle(Outcome.DONE, event.summary)
            self._stop_progress(100.0)
            self._start_next()
        elif isinstance(event, Stopped):
            # The library ended the item at a checkpoint it had already
            # persisted; the rest of the selection was cleared when the stop was
            # asked for, so the batch closes here unless an item is still owed.
            self._settle(Outcome.STOPPED, event.message)
            self._stop_progress(0.0)
            self._start_next()
        elif isinstance(event, Failed):
            # A failed listing says nothing about a transfer that is already
            # running: the window may have submitted the download from an older
            # list while the browse was still in flight, so ending the download
            # state here would unlock :meth:`close` under a live download. Only
            # the download's own failure ends it; a browse failure just reports.
            if event.action == DOWNLOAD_ACTION:
                self._settle(Outcome.FAILED, event.message)
                self._stop_progress(0.0)
                self._start_next()
            else:
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
        """Freeze the selection and the output folder, then serve it in list order.

        Planning stays per item: an entry whose name the library refuses fails
        alone in the log and the rest of the selection continues.
        """
        if self._downloading:
            return
        entries = self._selected_entries()
        if not entries:
            self._set_status("请先在列表中选择一个文件或目录")
            return
        try:
            folder = output_folder(self._output_dir)
        except (QuarkError, OSError) as error:
            self._set_status(describe_error(error, DOWNLOAD_ACTION))
            return
        self._batch = Batch(str(folder), entries)
        self._stop_requested = False
        self._downloading = True
        self._indeterminate = False
        # The stop flag belongs to the batch that was running; it is dropped
        # here, before this batch's first item is submitted to the worker.
        self._worker.clear_stop()
        self._sync_controls()
        self._start_next()

    def stop_download(self) -> None:
        """Stop the batch: drop what has not started, end the item in flight safely.

        The worker confirms with :class:`~quark_cli.downloader.worker.Stopped`
        (or with the item's own outcome, if it finished first). Until then the
        window stays busy, so closing and a new submission are both refused and
        the process never exits under a transfer that is still writing.
        """
        if not self._downloading or self._stop_requested:
            return
        self._stop_requested = True
        cancelled = self._batch.clear() if self._batch is not None else 0
        self._worker.request_stop()
        pending = f"{cancelled} 项未开始已取消；" if cancelled else ""
        self._append_log(f"已请求停止：{pending}当前项目将在检查点结束")
        self._set_status(STOPPING_STATUS)
        self._sync_controls()

    def _start_next(self) -> None:
        """Submit the next selected item, log the unplannable ones, or end the batch."""
        batch = self._batch
        if batch is None:
            # No batch is running, so nothing may keep the window busy: the
            # outcome was already reported by :meth:`_settle`.
            self._finish_batch()
            return
        while True:
            item = None if self._stop_requested else batch.take()
            if item is None:
                self._finish_batch()
                return
            try:
                plan = download_plan(batch.output_dir, item)
            except (QuarkError, OSError) as error:
                self._settle(Outcome.FAILED, describe_error(error, DOWNLOAD_ACTION))
                continue
            self._stop_progress(0.0)
            self._set_status(self._item_line(f"正在下载 {item.label}……"))
            self._worker.submit(
                Download(
                    fid=plan.fid,
                    target=str(plan.target),
                    recursive=plan.recursive,
                    # Downloads never replace an existing file: the library refuses it
                    # and the refusal is shown as a readable conflict message.
                    overwrite=False,
                    name=item.name,
                )
            )
            return

    def _finish_batch(self) -> None:
        """Close the click: report what became of every selected entry."""
        batch = self._batch
        self._batch = None
        self._stop_requested = False
        self._downloading = False
        if batch is not None:
            self._set_status(
                batch_summary(
                    succeeded=batch.succeeded,
                    failed=batch.failed,
                    stopped=batch.stopped,
                    skipped=batch.skipped,
                )
            )
        self._sync_controls()

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

    def _item_line(self, text: str) -> str:
        """One line about the item in flight: its place in the click, then the text."""
        batch = self._batch
        if batch is None:
            return text
        return item_status(batch.position, batch.total, text)

    def _append_log(self, text: str) -> None:
        """Record one finished item; the log is the per-item outcome list."""
        log = self._log
        if log is None:
            return
        log.configure(state="normal")
        log.insert("end", display_text(text) + "\n")
        log.see("end")
        log.configure(state="disabled")

    def _settle(self, outcome: Outcome, message: str) -> None:
        """Log the outcome of the item in flight and count it in the batch."""
        batch = self._batch
        if batch is None:
            self._set_status(message)
            return
        self._append_log(item_status(batch.position, batch.total, message))
        batch.settle(outcome)

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
        _set_state(self._stop_button, "normal" if self._downloading and not self._stop_requested else "disabled")
        ready = not busy and bool(self._output_dir) and bool(self._selected_entries())
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

    def _selected_entries(self) -> list[Entry]:
        """Every selected row, in the order the table shows them.

        The rows keep their listing order in :attr:`_entries`, so a batch is
        planned and downloaded in display order no matter in which order the
        rows were clicked.
        """
        if self._tree is None:
            return []
        chosen = set(self._tree.selection())
        if not chosen:
            return []
        return [entry for iid, entry in self._entries.items() if iid in chosen]

    def _selected(self) -> Entry | None:
        chosen = self._selected_entries()
        return chosen[0] if chosen else None

    def _activated_row(self, event: Any) -> str | None:
        """The row a double-click landed on, or ``None`` when it cannot be told.

        A multi-selection can span several rows, so entering the folder of the
        *first* selected row would follow a different row than the one clicked.
        """
        identify = getattr(self._tree, "identify_row", None)
        y = getattr(event, "y", None)
        if identify is None or not isinstance(y, int):
            return None
        iid = identify(y)
        return iid if isinstance(iid, str) and iid else None

    def _on_selection(self, _event: Any) -> None:
        self._sync_controls()

    def _on_activate(self, event: Any) -> None:
        # The tree stays clickable during a download, so entering a folder has
        # to be refused here too; otherwise the location line and the table
        # would disagree, because ``_open`` refuses to browse while busy.
        if self._downloading:
            return
        row = self._activated_row(event)
        entry = self._entries.get(row) if row is not None else self._selected()
        if entry is None or not entry.directory:
            return
        self._location.enter(entry.fid, entry.name)
        self._show_location()
        self._open(entry.fid)


def run_window() -> int:
    """Open the download window and block until it is closed."""
    DownloaderWindow().run()
    return 0
