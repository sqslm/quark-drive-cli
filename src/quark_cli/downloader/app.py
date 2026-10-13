"""The downloader's Tkinter window (``python -m quark_cli.downloader``).

Browsing and the file queue run on the worker thread of
:mod:`quark_cli.downloader.worker`; payload readers and nickname lookup run in
their own background threads. The Tk thread only submits requests and
renders the events that come back on a queue, so a folder listing or a file
transfer never blocks the window.

One click downloads the whole multi-selection: the entries are frozen in the
order the table shows them, served one at a time, and every outcome is appended
to the item log while the status line follows the item in flight. ``停止``
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

Searching and filtering stay local until the user asks for a request: the
keyword box and the type box re-render the rows the window already holds through
:func:`~quark_cli.downloader.core.filter_entries`, and only ``搜索`` with a
whole-drive or current-folder-and-descendants scope
submits a :class:`~quark_cli.downloader.worker.Search`. One search is in flight
at a time: an answer is applied only while its fid and keyword still match the
request the window submitted, so a late answer can never fill the table with the
rows of an older question. A search result carries the drive path of the match
and the folder chain it lives in, so double-clicking a result folder jumps
straight to that real folder and ``返回上级`` walks its real parents again;
``返回目录`` ends the search and returns to the folder the search started from.
Following a different folder — entering one, ``返回上级``, ``返回目录`` — drops
both local filters, because the keyword box is also the query that would hide
the rows of the folder just opened; ``刷新`` keeps them and re-runs the search
the table shows. A search that matched nothing says so in the count line, while
an empty folder keeps reporting its real counts. ``打开目录`` opens the
frozen save folder on the user's click, through an injectable opener, so no test
needs a desktop.

A shared link is browsed in place, never saved into the drive: ``打开分享``
extracts a strict ``https://pan.quark.cn/s/<分享ID>`` URL from a link or message
and marks the table as a share view, whose folders are read with
:class:`~quark_cli.downloader.worker.BrowseShare` and whose own folder chain is
kept apart from the account's (a share FID means nothing to the account API).
Opening it ends the account search the table may have been showing, and a folder
switch inside the share shows an empty table until the new path answers, so a
failed read can never leave rows under a location line they do not belong to.
The account search buttons are refused there, because a search would read the
account instead of the share, while ``我的网盘`` returns to the folder the drive
was showing. A share download carries its source and the shown folder along, and
one short line says that it is transferred through a temporary cloud folder that
is dropped when the click ends; the backend does that, no confirmation is asked.

The table's first column is its own ``选择`` box: a click on that column — or
the space bar on the focused row — ticks one row, which is what a download
takes, in table order. Highlighting a row still only says where navigation and
the keyboard are, and ``全选/取消全选`` ticks exactly the rows the local filters
currently show, so a hidden row is never downloaded by surprise. A click on a
box also puts the keyboard cursor on that row, so the space bar follows the
click instead of the row the cursor was on, and a double-click there is one tick
and never a step into the folder below. Ticks are kept
by fid, so a filter may hide a row and bring it back with its box still ticked,
``刷新`` drops only the fids that are gone, and leaving the folder or the view
starts from an empty tick set.

Nothing here creates a window at import time: the widget references are plain
attributes, so the rendering methods are covered by tests with stand-in widgets.
"""

from __future__ import annotations

import contextlib
import copy
import os
import queue
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..auth import LoginCancelled, get_cookie
from ..errors import LocalIOError, QuarkError, UsageError
from ..transfer import DEFAULT_DOWNLOAD_CONNECTIONS, MAX_DOWNLOAD_CONNECTIONS
from ..potplayer.login_gui import QrLoginWindow
from ..shares import parse_share_url as parse_share_link
from ..state import config_directory
from .core import (
    DOWNLOAD_ACTION,
    ROOT_FID,
    SEARCH_ACTION,
    EXTENSION_CHOICES,
    Batch,
    DownloadTask,
    Entry,
    Location,
    Outcome,
    batch_summary,
    describe_error,
    display_text,
    download_plan,
    filter_entries,
    sort_entries,
    item_status,
    output_folder,
)
from .share import ShareSource
from .preferences import Preferences, load_preferences, save_preferences

from .worker import (
    SHARE_BROWSE_ACTION,
    Backend,
    AccountProfile,
    LoadAccountProfile,
    Browse,
    BrowseShare,
    Download,
    DriveBackend,
    Failed,
    Finished,
    Listing,
    Progress,
    Search,
    SearchResults,
    ShareListing,
    Stopped,
    Worker,
)

WINDOW_TITLE = "夸克网盘下载器"
POLL_MILLISECONDS = 120
IDLE_STATUS = "请勾选文件或目录，然后点击「下载」"
LOGIN_HINT = "尚未登录：请点击「登录」，用手机夸克 App 扫码"
CLOSE_BUSY_STATUS = "正在下载：请等待当前下载完成后再关闭窗口"
STOPPING_STATUS = "正在停止：当前项目将在已保存的检查点结束，请稍候"
SEARCHING_STATUS = "正在搜索……"
SEARCH_HINT = "请输入搜索关键词"
#: The count line's text when a search matched nothing at all; an empty folder
#: still reports its real counts through :func:`match_text`.
NO_MATCH_RESULT_TEXT = "未找到匹配项"
OPEN_FOLDER_ACTION = "打开保存目录"

#: The share view's root path: the share is browsed from its own top folder, and
#: this is also the path every share download of that folder carries.
SHARE_ROOT_PATH = "/"
SHARE_IMPORT_ACTION = "导入分享"
SHARE_READING_STATUS = "正在读取分享……"
SHARE_TOP_STATUS = "已在分享最上层目录"
#: The one line a share click says about what the backend does with it: the
#: click is the authorisation, so no confirmation is asked.
SHARE_DOWNLOAD_HINT = "分享下载将临时转存，结束后自动清理；停止后需重新下载"

#: The ``选择`` box is the table's first data column, so
#: :meth:`ttk.Treeview.identify_column` names it ``#1`` (``#0`` is the name
#: column). The box is text, so no image or extra widget is needed.
CHECK_COLUMN = "#1"
CHECKED_MARK = "☑"
UNCHECKED_MARK = "□"
DOWNLOAD_BUTTON_TEXT = "下载"
SELECT_ALL_TEXT = "全选"

#: The type filter's labels and the ``kind`` each one asks
#: :func:`~quark_cli.downloader.core.filter_entries` for.
KIND_CHOICES: tuple[tuple[str, str], ...] = (
    ("全部", "all"),
    ("仅文件", "files"),
    ("仅目录", "directories"),
)


def match_text(total: int, visible: int) -> str:
    """The row counts, or the hint the local filters left an empty table with."""
    if visible == 0 and total:
        return f"无匹配项（共 {total} 项）"
    return f"共 {total} 项，显示 {visible} 项"


def parse_share_url(url: str, passcode: str | None = None) -> ShareSource:
    """Pair one typed share link with its optional passcode, or refuse it.

    The link goes through the library's own strict parser, so the window accepts
    exactly what the CLI accepts. An empty passcode stays ``None``: the link's
    own ``pwd`` parameter is then the one that is used, and neither the URL nor
    the passcode reaches the status line or the item log (the source also keeps
    both out of its ``repr``).
    """
    link = parse_share_link(url.strip(), passcode)
    return ShareSource(url=url.strip(), passcode=link.passcode or None)


def parse_share_input(text: str, passcode: str | None = None) -> ShareSource:
    """Extract one Quark URL and code from a pasted link or share message."""
    candidates = re.findall(r'https?://[^\s<>"\'，。；、（）【】「」]+', text, re.IGNORECASE)
    try:
        urls = [url.rstrip(".,;:!?)]}") for url in candidates if urlsplit(url).hostname == "pan.quark.cn"]
    except ValueError:
        raise UsageError("分享链接格式无效") from None
    if len(urls) != 1:
        raise UsageError("请输入单个夸克分享链接或分享文案")
    linked = parse_share_url(urls[0])
    codes = set(re.findall(r"(?:提取码|提取碼|密码)\s*[:：]?\s*([A-Za-z0-9]+)", text))
    if linked.passcode:
        codes.add(linked.passcode)
    if not passcode and len(codes) > 1:
        raise UsageError("分享文案中的提取码不一致，请手动填写正确提取码")
    return parse_share_url(urls[0], passcode or next(iter(codes), None))


def default_folder_opener(path: str) -> None:
    """Open one existing folder with the desktop's own file manager.

    The path is handed to the shell-directory API as it is: no command line is
    assembled from it, so a folder name cannot change how it is interpreted.
    """
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise LocalIOError("当前系统不支持打开保存目录")
    startfile(path)


def _set_state(widget: Any, state: str) -> None:
    if widget is not None:
        widget.configure(state=state)


def _release_exception_frames(error: BaseException) -> None:
    """Handled login errors must not keep Tk owners alive until background GC."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        pending.extend(cause for cause in (current.__cause__, current.__context__) if cause is not None)
        current.__traceback__ = None


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
    and saved Cookie. ``folder_opener`` opens the save folder for ``打开保存目录``
    and defaults to :func:`default_folder_opener`, so tests never need a desktop.
    """

    def __init__(
        self,
        *,
        cookie: Callable[[], str] | None = None,
        backend_factory: Callable[[Callable[[], str]], Backend] | None = None,
        login_window_factory: Callable[[Any], Any] | None = None,
        root_factory: Callable[[], Any] | None = None,
        transport: Any = None,
        folder_opener: Callable[[str], None] | None = None,
        preferences_path: Path | None = None,
    ) -> None:
        self._cookie = get_cookie if cookie is None else cookie
        factory = backend_factory or (lambda loader: DriveBackend(loader, transport=transport))
        self._worker = Worker(factory(self._cookie))
        self._login_window_factory = login_window_factory or default_login_window
        self._root_factory = root_factory
        self._folder_opener = folder_opener or default_folder_opener
        self._preferences_path = preferences_path
        self._preferences = Preferences()
        self._preferences_save_allowed = False
        self._preferences_warning = ""
        self._location = Location()
        # The share view: ``None`` means the table follows the account drive.
        # The share's own folder chain is kept apart from ``_location``, because
        # a share FID means nothing to the account's browse API; the last path
        # of the chain is both what the table shows and what the next
        # ``BrowseShare`` asks for.
        self._share_source: ShareSource | None = None
        self._share_trail: list[str] = [SHARE_ROOT_PATH]
        #: Whether a share read is in flight: it freezes navigation, the view
        #: switch and the click exactly like a search does.
        self._reading_share = False
        #: The share source and parent path a running batch was frozen with, so
        #: a later view change can never mix a share item with an account item.
        self._batch_share: tuple[ShareSource, str] | None = None
        #: The fids whose ``选择`` box is ticked. Keyed by fid rather than by row
        #: position, so filtering, re-ordering or refreshing can never move a
        #: tick onto another row.
        self._checked: set[str] = set()
        # The rows the window holds, before the local filters: re-rendering
        # always starts from this complete list, never from an already filtered
        # one, so clearing a filter restores every row.
        self._rows: list[Entry] = []
        self._sort_key: str | None = None
        self._sort_descending = False
        # The rows the table shows right now, keyed by the iid it shows them
        # under. It is empty until the first listing arrives, and the controls
        # are drawn from it from the very first :meth:`_sync_controls`.
        self._entries: dict[str, Entry] = {}
        # The search whose rows the table shows, and the search that is actually
        # in flight; they are equal while the table shows what was asked for.
        self._search: Search | None = None
        self._pending: Search | None = None
        # The folder the search started from, kept so ``返回目录`` can restore it
        # even after the user walked away into a result folder.
        self._origin: Location | None = None
        self._searching = False
        self._output_dir = ""
        self._batch: Batch | None = None
        self._batch_connections = DEFAULT_DOWNLOAD_CONNECTIONS
        self._batch_resume_available = True
        self._tasks: list[DownloadTask] = []
        self._active_task_ids: list[int] = []
        self._task_output_dir = ""
        self._task_share: tuple[ShareSource, str] | None = None
        self._tasks_valid = True
        self._notebook: Any = None
        self._task_tab: Any = None
        self._task_tree: Any = None
        self._task_menu: Any = None
        self._task_summary_var: Any = None
        self._task_detail_var: Any = None
        self._retry_button: Any = None
        self._continue_button: Any = None
        self._connections_var: Any = None
        self._connections_input: Any = None
        self._downloading = False
        self._stop_requested = False
        self._logging_in = False
        self._profile_generation = 0
        self._profile_pending = False
        self._nickname_var: Any = None
        self._scope_var: Any = None
        self._scope_combo: Any = None
        self._indeterminate = False
        self._root: Any = None
        self._tree: Any = None
        self._log: Any = None
        self._progress: Any = None
        self._status_var: Any = None
        self._location_var: Any = None
        self._output_var: Any = None
        self._search_var: Any = None
        self._kind_var: Any = None
        self._count_var: Any = None
        self._keyword_entry: Any = None
        self._kind_combo: Any = None
        self._extension_var: Any = None
        self._extension_combo: Any = None
        self._extension_error = ""
        self._share_var: Any = None
        self._passcode_var: Any = None
        self._share_entry: Any = None
        self._passcode_entry: Any = None
        self._share_button: Any = None
        self._drive_button: Any = None
        self._select_all_button: Any = None
        self._up_button: Any = None
        self._refresh_button: Any = None
        self._search_button: Any = None
        self._return_button: Any = None
        self._login_button: Any = None
        self._output_button: Any = None
        self._open_folder_button: Any = None
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
                self._load_preferences()
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
        if not self._save_preferences():
            import tkinter.messagebox as messagebox

            messagebox.showwarning("下载器设置", self._preferences_warning + "\n本次设置未保存。", parent=self._root)
        self._shutdown()

    def _load_preferences(self) -> None:
        try:
            if self._preferences_path is None:
                self._preferences_path = config_directory() / "downloader" / "preferences.json"
            self._preferences = load_preferences(self._preferences_path)
            self._preferences_save_allowed = True
            if self._preferences.output_dir:
                if Path(self._preferences.output_dir).is_dir():
                    self._output_dir = self._preferences.output_dir
                else:
                    self._preferences_warning = "记住的保存目录已不可用，请重新选择"
        except (QuarkError, OSError) as error:
            self._preferences = Preferences()
            self._preferences_save_allowed = False
            self._preferences_warning = describe_error(error, "读取下载器设置") + "；使用默认设置，原设置未改写"

    def _save_preferences(self, *, connections: int | None = None) -> bool:
        if not self._preferences_save_allowed or self._preferences_path is None:
            return True
        if connections is None:
            try:
                connections = self._download_connections()
            except UsageError:
                # A half-edited spinbox must not replace the last valid value.
                connections = self._preferences.connections
        width, height = self._preferences.width, self._preferences.height
        if self._root is not None and self._root.winfo_width() > 1:
            width, height = self._root.winfo_width(), self._root.winfo_height()
        preferences = Preferences(self._output_dir, connections, width, height)
        try:
            save_preferences(self._preferences_path, preferences)
        except (QuarkError, OSError) as error:
            self._preferences_warning = describe_error(error, "保存下载器设置")
            self._append_log(self._preferences_warning)
            self._set_status(self._preferences_warning)
            return False
        self._preferences = preferences
        return True

    def _remember_connections(self, *_event: Any) -> None:
        if self._frozen():
            return
        try:
            connections = self._download_connections()
        except UsageError as error:
            self._set_status(str(error))
            return
        self._save_preferences(connections=connections)

    def _build(self, tk: Any) -> None:
        root = self._root
        root.title(WINDOW_TITLE)
        screen_width = root.winfo_screenwidth()
        screen_height = max(1, root.winfo_screenheight() - 80)
        root.geometry(f"{min(max(760, self._preferences.width), screen_width)}x"
                      f"{min(max(540, self._preferences.height), screen_height)}")
        root.minsize(min(760, screen_width), min(540, screen_height))
        ttk = tk.ttk

        toolbar = ttk.Frame(root)
        toolbar.pack(fill="x", padx=12, pady=(10, 6))
        self._up_button = ttk.Button(toolbar, text="上级", width=6, command=self.go_up)
        self._up_button.pack(side="left", padx=(0, 4))
        self._refresh_button = ttk.Button(toolbar, text="刷新", width=6, command=self.refresh)
        self._refresh_button.pack(side="left", padx=(0, 8))
        self._login_button = ttk.Button(toolbar, text="登录", width=6, command=self.login)
        self._login_button.pack(side="right")
        self._nickname_var = tk.StringVar(value="未登录")
        ttk.Label(toolbar, textvariable=self._nickname_var).pack(side="right", padx=8)
        self._return_button = ttk.Button(toolbar, text="返回目录", width=8, command=self.leave_search)
        self._location_var = tk.StringVar(value=self._location.title)
        ttk.Label(toolbar, textvariable=self._location_var, anchor="w").pack(side="left", fill="x", expand=True)

        shared = ttk.Frame(root)
        shared.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Label(shared, text="分享").pack(side="left", padx=(0, 4))
        self._share_var = tk.StringVar()
        self._passcode_var = tk.StringVar()
        self._share_var.trace_add("write", self._on_share_input_change)
        self._share_entry = ttk.Entry(shared, textvariable=self._share_var, width=32)
        self._share_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Label(shared, text="提取码（可选）").pack(side="left")
        self._passcode_entry = ttk.Entry(shared, textvariable=self._passcode_var, width=7, show="*")
        self._passcode_entry.pack(side="left", padx=(4, 6))
        self._share_button = ttk.Button(shared, text="打开分享", width=8, command=self.open_share)
        self._share_button.pack(side="left", padx=(0, 4))
        self._drive_button = ttk.Button(shared, text="我的网盘", width=8, command=self.open_drive)
        self._drive_button.pack(side="left")

        find = ttk.Frame(root)
        find.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Label(find, text="名称").pack(side="left", padx=(0, 4))
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", self._on_filter_change)
        self._keyword_entry = ttk.Entry(find, textvariable=self._search_var, width=18)
        self._keyword_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._scope_var = tk.StringVar(value="全盘")
        self._scope_combo = ttk.Combobox(find, textvariable=self._scope_var, width=16,
                                         values=("全盘", "当前目录及子目录"), state="readonly")
        self._scope_combo.pack(side="left", padx=(0, 4))
        self._search_button = ttk.Button(find, text="搜索", width=6, command=self.search)
        self._search_button.pack(side="left", padx=(0, 6))
        self._kind_var = tk.StringVar(value=KIND_CHOICES[0][0])
        self._kind_combo = ttk.Combobox(find, textvariable=self._kind_var,
                                        values=[label for label, _ in KIND_CHOICES], width=7, state="readonly")
        self._kind_combo.pack(side="left", padx=(0, 6))
        self._kind_combo.bind("<<ComboboxSelected>>", self._on_filter_change)
        self._select_all_button = ttk.Button(find, text=SELECT_ALL_TEXT, width=8, command=self.toggle_all)
        self._select_all_button.pack(side="left", padx=(0, 6))
        self._count_var = tk.StringVar(value=match_text(0, 0))
        ttk.Label(find, textvariable=self._count_var, anchor="e").pack(side="right")

        formats = ttk.Frame(root)
        formats.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Label(formats, text="扩展名").pack(side="left", padx=(0, 6))
        self._extension_var = tk.StringVar(value=EXTENSION_CHOICES[0])
        self._extension_combo = ttk.Combobox(formats, textvariable=self._extension_var,
                                            values=EXTENSION_CHOICES, width=24, state="normal")
        self._extension_combo.pack(side="left", padx=(0, 8))
        ttk.Label(formats, text="也可输入 mp4, mkv 或 tar.gz").pack(side="left")
        self._extension_var.trace_add("write", self._on_filter_change)

        self._notebook = ttk.Notebook(root)
        self._notebook.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        listing = ttk.Frame(self._notebook)
        self._notebook.add(listing, text="文件")
        tree = ttk.Treeview(listing, columns=("check", "path", "type", "size"), show="tree headings", selectmode="extended")
        tree.heading("#0", text="名称", command=lambda: self.sort_by("name"))
        tree.column("#0", width=260, minwidth=160)
        for key, text, width in (("check", "选择", 48), ("path", "路径", 300),
                                  ("type", "类型", 70), ("size", "大小", 95)):
            tree.heading(key, text=text)
            tree.column(key, width=width, minwidth=width if key != "path" else 80,
                        stretch=key == "path", anchor="center" if key in ("check", "type") else "w")
        scroll = ttk.Scrollbar(listing, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        tree.bind("<Button-1>", self._on_click)
        tree.bind("<Double-1>", self._on_activate)
        tree.bind("<space>", self._on_space)
        tree.bind("<<TreeviewSelect>>", self._on_selection)
        self._tree = tree
        tree.heading("size", command=lambda: self.sort_by("size"))
        tree.heading("path", command=lambda: self.sort_by("path"))
        self._task_tab = ttk.Frame(self._notebook)
        self._notebook.add(self._task_tab, text="下载任务")
        task_actions = ttk.Frame(self._task_tab)
        task_actions.pack(fill="x", padx=6, pady=6)
        self._retry_button = ttk.Button(task_actions, text="重试失败", command=self.retry_failed)
        self._retry_button.pack(side="left", padx=(0, 4))
        self._continue_button = ttk.Button(task_actions, text="继续未完成", command=self.continue_unfinished)
        self._continue_button.pack(side="left", padx=(0, 8))
        self._task_summary_var = tk.StringVar(value="暂无下载任务")
        ttk.Label(task_actions, textvariable=self._task_summary_var).pack(side="left")
        task_listing = ttk.Frame(self._task_tab)
        task_listing.pack(fill="both", expand=True, padx=6)
        self._task_tree = ttk.Treeview(task_listing, columns=("status", "detail"), show="tree headings",
                                      selectmode="browse")
        self._task_tree.heading("#0", text="名称")
        self._task_tree.column("#0", width=230, minwidth=140)
        self._task_tree.heading("status", text="状态")
        self._task_tree.column("status", width=80, minwidth=80, stretch=False, anchor="center")
        self._task_tree.heading("detail", text="结果 / 进度")
        self._task_tree.column("detail", width=430, minwidth=180)
        task_scroll = ttk.Scrollbar(task_listing, orient="vertical", command=self._task_tree.yview)
        self._task_tree.configure(yscrollcommand=task_scroll.set)
        task_scroll.pack(side="right", fill="y")
        self._task_tree.pack(side="left", fill="both", expand=True)
        self._task_tree.bind("<<TreeviewSelect>>", self._show_task_detail)
        self._task_menu = tk.Menu(self._task_tree, tearoff=False)
        self._task_menu.add_command(label="重试此项", command=self.retry_task)
        self._task_menu.add_command(label="打开保存目录", command=self.open_task_folder)
        self._task_tree.bind("<Button-3>", self._on_task_menu)
        self._task_detail_var = tk.StringVar(value="选择任务查看完整结果；右键可重试此项或打开保存目录")
        ttk.Label(self._task_tab, textvariable=self._task_detail_var, wraplength=790,
                  justify="left", anchor="w").pack(fill="x", padx=6, pady=6)
        self._progress = ttk.Progressbar(root, mode="determinate", maximum=100.0)
        self._progress.pack(fill="x", padx=12, pady=(0, 4))
        self._log = tk.Text(root, height=4, wrap="word", state="disabled")
        self._log.pack(fill="x", padx=12, pady=(0, 6))

        destination = ttk.Frame(root)
        destination.pack(fill="x", padx=12, pady=(0, 6))
        self._open_folder_button = ttk.Button(destination, text="打开目录", width=8,
                                               command=self.open_output_folder, state="disabled")
        self._open_folder_button.pack(side="right")
        self._output_button = ttk.Button(destination, text="更改", width=6, command=self.choose_output)
        self._output_button.pack(side="right", padx=(0, 4))
        ttk.Label(destination, text="保存到").pack(side="left", padx=(0, 6))
        self._output_var = tk.StringVar(value=display_text(self._output_dir) or "未选择本地保存目录")
        ttk.Label(destination, textvariable=self._output_var, anchor="w").pack(side="left", fill="x", expand=True)
        actions = ttk.Frame(root)
        actions.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Label(actions, text="线程数").pack(side="left", padx=(0, 4))
        self._connections_var = tk.StringVar(value=str(self._preferences.connections))
        self._connections_input = ttk.Spinbox(actions, from_=1, to=MAX_DOWNLOAD_CONNECTIONS,
                                               width=3, textvariable=self._connections_var,
                                               command=self._remember_connections)
        self._connections_input.pack(side="left")
        self._connections_input.bind("<FocusOut>", self._remember_connections)
        self._connections_input.bind("<Return>", self._remember_connections)
        self._download_button = ttk.Button(actions, text=DOWNLOAD_BUTTON_TEXT, width=12, command=self.start_download)
        self._download_button.pack(side="right")
        self._stop_button = ttk.Button(actions, text="停止", width=8, command=self.stop_download, state="disabled")
        self._stop_button.pack(side="right", padx=(0, 4))
        self._status_var = tk.StringVar(value=IDLE_STATUS)
        ttk.Label(root, textvariable=self._status_var, anchor="w", wraplength=810, justify="left").pack(
            fill="x", padx=12, pady=(0, 10))
        root.protocol("WM_DELETE_WINDOW", self.close)

    def _initialise(self) -> None:
        self._sync_controls()
        if self._credential_available():
            self._profile_pending = True
            self._open(ROOT_FID)
        else:
            self._set_status(LOGIN_HINT)
        if self._preferences_warning:
            self._append_log(self._preferences_warning)
            self._set_status(self._preferences_warning)

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
        self._notebook = None
        self._task_tab = None
        self._task_tree = None
        self._task_menu = None
        self._task_summary_var = None
        self._task_detail_var = None
        self._retry_button = None
        self._continue_button = None
        self._connections_var = None
        self._connections_input = None
        self._nickname_var = None
        self._scope_var = None
        self._scope_combo = None
        self._log = None
        self._progress = None
        self._status_var = None
        self._location_var = None
        self._output_var = None
        self._search_var = None
        self._kind_var = None
        self._count_var = None
        self._keyword_entry = None
        self._kind_combo = None
        self._extension_var = None
        self._extension_combo = None
        self._share_var = None
        self._passcode_var = None
        self._share_entry = None
        self._passcode_entry = None
        self._share_button = None
        self._drive_button = None
        self._select_all_button = None
        self._up_button = None
        self._refresh_button = None
        self._search_button = None
        self._return_button = None
        self._login_button = None
        self._output_button = None
        self._open_folder_button = None
        self._download_button = None
        self._stop_button = None
        self._preferences_save_allowed = False
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
        if isinstance(event, AccountProfile):
            if event.generation != self._profile_generation:
                return
            if self._nickname_var is not None:
                name = display_text(event.nickname) if event.nickname else "昵称暂不可用"
                self._nickname_var.set(name if len(name) <= 24 else name[:24] + "…")
            if event.error:
                self._append_log(event.error)
        elif isinstance(event, SearchResults):
            # A search reads the account drive, so its answer never belongs to
            # the table while that table shows a share.
            if self._share_source is not None:
                return
            # One search is in flight at a time; an answer to an older question
            # (another fid, another keyword) is dropped instead of filling the
            # table with rows the window did not ask for.
            pending = self._pending
            if not self._searching or pending is None:
                return
            if event.fid != pending.fid or event.query != pending.query:
                return
            self._searching = False
            self._pending = None
            self._search = pending
            self._adopt_rows(event.entries)
            self._set_status(self._extension_error or IDLE_STATUS)
            self._sync_controls()
        elif isinstance(event, ShareListing):
            # Only the share folder the window is actually showing may fill the
            # table: a listing of another share, or of another path, or one that
            # arrived after the view was left, is a stale answer and is dropped.
            if (
                self._share_source is None
                or event.source != self._share_source
                or event.path != self._share_trail[-1]
            ):
                return
            self._reading_share = False
            self._adopt_rows(event.entries)
            self._set_status(self._extension_error or IDLE_STATUS)
            self._sync_controls()
        elif isinstance(event, Listing):
            # Navigating again before this answer arrived makes it stale: only
            # the folder the window is actually showing may fill the table, or
            # the rows would contradict the location line. A search in flight or
            # on screen speaks for the folder it started from too, so no folder
            # listing may fill the table while either holds: it would both
            # replace the rows and end the search state before its own answer.
            # A share view is never filled by an account listing either.
            if (
                self._searching
                or self._search is not None
                or self._share_source is not None
                or event.fid != self._location.fid
            ):
                return
            self._render_listing(event)
            self._set_status(self._extension_error or IDLE_STATUS)
            self._sync_controls()
            if self._profile_pending:
                self._profile_pending = False
                self._request_profile()
        elif isinstance(event, Progress):
            self._set_status(self._item_line(event.text))
            self._show_progress(event.fraction)
            self._update_task("running", event.text)
        elif isinstance(event, Finished):
            self._settle(Outcome.DONE, event.summary)
            self._stop_progress(100.0)
            self._start_next()
        elif isinstance(event, Stopped):
            self._batch_resume_available = event.resume_available
            # The library ended the item at a checkpoint it had already
            # persisted; the rest of the selection was cleared when the stop was
            # asked for, so the batch closes here unless an item is still owed.
            self._settle(Outcome.STOPPED, event.message)
            self._stop_progress(0.0)
            self._start_next()
        elif isinstance(event, Failed):
            # A failed listing or search says nothing about a transfer that is
            # already running: the window may have submitted the download from an
            # older list while the request was still in flight, so ending the
            # download state here would unlock :meth:`close` under a live
            # download. Only the download's own failure ends it; anything else
            # just reports and leaves the table as it is.
            if event.action == DOWNLOAD_ACTION:
                self._settle(Outcome.FAILED, event.message)
                self._stop_progress(0.0)
                self._start_next()
                return
            if event.action == SEARCH_ACTION:
                self._searching = False
                self._pending = None
                if self._search is None:
                    # Nothing of this search reached the table, so there is no
                    # search session left to return from either.
                    self._origin = None
            if event.action == SHARE_BROWSE_ACTION:
                # The share read is over (nothing was shown); the user may go
                # back to the drive or import the link again, and the failure
                # says nothing about a download that is already running.
                self._reading_share = False
            self._set_status(event.message)
            self._sync_controls()

    # -- actions ---------------------------------------------------------

    def go_up(self) -> None:
        if self._busy():
            return
        if self._share_source is not None:
            # The share's own chain, never a share fid: the account's browse API
            # knows nothing about it.
            if len(self._share_trail) <= 1:
                self._set_status(SHARE_TOP_STATUS)
                return
            self._share_trail.pop()
            self._clear_checks()
            self._clear_rows()
            self._clear_filters()
            self._show_location()
            self._open_share_path()
            return
        if not self._location.up():
            self._set_status("已经在最上层目录")
            return
        # Walking out of a result folder ends the search listing: the table
        # follows the real folder again from here on.
        self._search = None
        self._clear_checks()
        self._clear_filters()
        self._show_location()
        self._open(self._location.fid)

    def refresh(self) -> None:
        """Re-read the folder shown, or re-run the search the table shows.

        A share folder is re-read with the same chain, so the ticks survive for
        every item that is still there; nothing else is cleared.
        """
        if self._share_source is not None:
            self._open_share_path()
            return
        request = self._search
        if request is None:
            self._open(self._location.fid)
            return
        if self._busy():
            return
        self._pending = request
        self._searching = True
        self._set_status(SEARCHING_STATUS)
        self._sync_controls()
        self._worker.submit(request)

    def open_share(self) -> None:
        """Read the typed share link and show the share's own top folder.

        Browsing only: nothing is saved into the drive. The link is validated by
        the library's own strict parser first, so a refused link changes nothing.
        A share is another source than the account, so the search session of the
        account view ends here: its rows, its folder to return to and the answer
        it may still be waiting for all describe a table that is now gone.
        """
        if self._busy():
            return
        url = self._share_var.get() if self._share_var is not None else ""
        passcode = self._passcode_var.get() if self._passcode_var is not None else ""
        try:
            source = parse_share_input(url, passcode if passcode else None)
        except QuarkError as error:
            self._set_status(describe_error(error, SHARE_IMPORT_ACTION))
            return
        self._reset_search()
        self._share_source = source
        self._share_trail = [SHARE_ROOT_PATH]
        self._checked.clear()
        self._clear_rows()
        self._clear_filters()
        self._show_location()
        self._open_share_path()

    def open_drive(self) -> None:
        """Leave the share view and browse the account folder that was shown."""
        if self._busy() or self._share_source is None:
            return
        self._share_source = None
        self._share_trail = [SHARE_ROOT_PATH]
        self._reset_search()
        self._checked.clear()
        self._clear_rows()
        self._clear_filters()
        self._show_location()
        self._open(self._location.fid)

    def _open_share_path(self) -> None:
        """(Re-)read the share folder the trail currently points at."""
        source = self._share_source
        if source is None or self._busy():
            return
        self._reading_share = True
        self._set_status(SHARE_READING_STATUS)
        self._sync_controls()
        self._worker.submit(BrowseShare(source, self._share_trail[-1]))

    def search(self) -> None:
        """Search the whole drive for the keyword in the filter box."""
        scope = self._scope_var.get() if self._scope_var is not None else "全盘"
        self._submit_search(ROOT_FID if scope == "全盘" else self._location.fid)

    def _on_share_input_change(self, *_event: Any) -> None:
        if self._busy() or self._share_var is None or self._passcode_var is None:
            return
        try:
            source = parse_share_input(self._share_var.get())
        except (UsageError, ValueError):
            # Incomplete typing is left intact; opening reports invalid input.
            return
        if source.passcode:
            self._passcode_var.set(source.passcode)

    def _request_profile(self) -> None:
        self._profile_generation += 1
        if self._nickname_var is not None:
            self._nickname_var.set("读取昵称…")
        self._worker.submit(LoadAccountProfile(self._profile_generation))

    def search_subdirectories(self) -> None:
        """Search the folder shown and every folder below it (the root is all)."""
        self._submit_search(self._location.fid)

    def leave_search(self) -> None:
        """Leave the search: show the folder the search started from.

        A share view has no search session of the account's to leave, so this
        is refused there as well as disabled in the controls.
        """
        if self._busy() or self._share_source is not None or self._origin is None:
            return
        origin, self._origin = self._origin, None
        self._search = None
        self._rows = []
        self._checked.clear()
        self._render_rows(())
        self._show_counts(0)
        self._clear_filters()
        self._sync_controls()
        self._location = origin
        self._show_location()
        self._open(self._location.fid)

    def _submit_search(self, fid: str) -> None:
        """Ask for one search of ``fid``; a keyword that is blank asks for none."""
        if self._busy():
            return
        if self._share_source is not None:
            # A drive search would read the account, not the share, so it is
            # refused here as well as disabled in the controls.
            return
        query = self._keyword()
        if not query:
            self._set_status(SEARCH_HINT)
            return
        # A search never moves the location, so the snapshot is the folder the
        # window shows; it survives walking into a result folder, which is what
        # ``返回目录`` restores.
        self._origin = copy.deepcopy(self._location)
        self._pending = Search(fid=fid, query=query)
        self._searching = True
        self._clear_checks()
        self._set_status(SEARCHING_STATUS)
        self._sync_controls()
        self._worker.submit(self._pending)

    def choose_output(self) -> None:
        if self._frozen():
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
        self._save_preferences()
        self._sync_controls()

    def open_output_folder(self) -> None:
        """Open the frozen save folder in the file manager, on the user's click.

        The folder is validated again here, so a save folder that was moved or
        deleted since it was chosen becomes a readable line instead of an error
        from the desktop's own API. Opening a folder says nothing about the
        download in progress, so a successful open leaves the progress line alone.
        """
        if self._frozen() or not self._output_dir:
            return
        try:
            folder = output_folder(self._output_dir)
        except (QuarkError, OSError) as error:
            self._set_status(describe_error(error, OPEN_FOLDER_ACTION))
            return
        try:
            self._folder_opener(str(folder))
        except Exception as error:
            self._set_status(describe_error(error, OPEN_FOLDER_ACTION))

    def start_download(self) -> None:
        """Freeze the ticked rows and the output folder, then serve them in list order.

        The ticked rows are the frozen selection, and the share they came from
        is frozen with them, so a later view change can never mix a share item
        with an account item. Planning stays per item: an entry whose name the
        library refuses fails alone in the log and the rest of the click
        continues.
        """
        if self._busy():
            return
        entries = self._checked_entries()
        if not entries:
            self._set_status("请先在列表中勾选一个文件或目录")
            return
        try:
            folder = output_folder(self._output_dir)
            connections = self._download_connections()
        except (QuarkError, OSError) as error:
            self._set_status(describe_error(error, DOWNLOAD_ACTION))
            return
        source = self._share_source
        self._tasks = [DownloadTask(entry) for entry in entries]
        self._task_output_dir = str(folder)
        self._task_share = None if source is None else (source, self._share_trail[-1])
        self._tasks_valid = True
        self._begin_batch(list(range(len(self._tasks))), connections)

    def _download_connections(self) -> int:
        value = self._connections_var.get() if self._connections_var is not None else DEFAULT_DOWNLOAD_CONNECTIONS
        try:
            connections = int(value)
        except (TypeError, ValueError):
            raise UsageError("线程数必须是 1–16 的整数") from None
        if not 1 <= connections <= MAX_DOWNLOAD_CONNECTIONS:
            raise UsageError("线程数必须是 1–16 的整数")
        return connections

    def retry_failed(self) -> None:
        self._retry_tasks(failed_only=True)

    def continue_unfinished(self) -> None:
        self._retry_tasks(failed_only=False)

    def retry_task(self) -> None:
        self._retry_tasks(failed_only=False, selected_only=True)

    def _retry_tasks(self, *, failed_only: bool, selected_only: bool = False) -> None:
        if self._busy():
            return
        if not self._tasks_valid:
            self._set_status("登录已变化，请重新勾选要下载的项目")
            return
        states = ("failed",) if failed_only else ("failed", "stopped", "skipped")
        selected = self._task_tree.selection() if selected_only and self._task_tree is not None else ()
        task_ids = [index for index, task in enumerate(self._tasks) if task.state in states
                    and (not selected_only or str(index) in selected)]
        if not task_ids:
            self._set_status("请选中一个未完成的任务" if selected_only else
                             "没有需要重试的失败任务" if failed_only else "没有未完成的任务")
            return
        try:
            output_folder(self._task_output_dir)
            connections = self._download_connections()
        except (QuarkError, OSError) as error:
            self._set_status(describe_error(error, DOWNLOAD_ACTION))
            return
        for index in task_ids:
            self._tasks[index].state = "waiting"
            self._tasks[index].detail = ""
        self._begin_batch(task_ids, connections)

    def open_task_folder(self) -> None:
        if self._busy() or self._task_tree is None:
            return
        selected = self._task_tree.selection()
        if not selected:
            self._set_status("请先选中一个任务")
            return
        task = self._tasks[int(selected[0])]
        try:
            folder = output_folder(self._task_output_dir)
            if task.entry.directory and task.state == "done":
                folder = output_folder(str(download_plan(self._task_output_dir, task.entry).target))
            self._folder_opener(str(folder))
        except Exception as error:
            self._set_status(describe_error(error, OPEN_FOLDER_ACTION))

    def _on_task_menu(self, event: Any) -> str:
        if self._busy():
            return "break"
        iid = self._task_tree.identify_row(event.y)
        if not iid:
            return "break"
        self._task_tree.selection_set(iid)
        self._task_tree.focus(iid)
        self._show_task_detail()
        task = self._tasks[int(iid)]
        ready = self._tasks_valid and task.state in ("failed", "stopped", "skipped")
        self._task_menu.entryconfigure(0, state="normal" if ready else "disabled")
        self._task_menu.entryconfigure(1, state="normal" if self._task_output_dir else "disabled")
        try:
            self._task_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._task_menu.grab_release()
        return "break"

    def _begin_batch(self, task_ids: list[int], connections: int) -> None:
        self._save_preferences(connections=connections)
        self._active_task_ids = task_ids
        self._batch = Batch(self._task_output_dir, (self._tasks[index].entry for index in task_ids))
        self._batch_connections = connections
        self._batch_resume_available = True
        self._batch_share = self._task_share
        self._stop_requested = False
        self._downloading = True
        self._indeterminate = False
        if self._batch_share is not None:
            # The click is the authorisation for the temporary transfer the
            # backend performs, so this is a line of information, not a question.
            self._append_log(SHARE_DOWNLOAD_HINT)
        # The stop flag belongs to the batch that was running; it is dropped
        # here, before this batch's first item is submitted to the worker.
        self._worker.clear_stop()
        self._render_tasks()
        if self._notebook is not None:
            self._notebook.select(self._task_tab)
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
        for index in self._active_task_ids:
            task = self._tasks[index]
            if task.state == "waiting":
                task.state, task.detail = "skipped", "停止批次时尚未开始"
        self._render_tasks()
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
            self._update_task("running", "正在准备下载……")
            shared = self._batch_share
            self._worker.submit(
                Download(
                    fid=plan.fid,
                    target=str(plan.target),
                    recursive=plan.recursive,
                    # Downloads never replace an existing file: the library refuses it
                    # and the refusal is shown as a readable conflict message.
                    overwrite=False,
                    name=item.name,
                    # Only a share item carries its source and the folder it was
                    # shown in; an account download keeps the defaults.
                    share_source=None if shared is None else shared[0],
                    share_path=SHARE_ROOT_PATH if shared is None else shared[1],
                    connections=self._batch_connections,
                )
            )
            return

    def _finish_batch(self) -> None:
        """Close the click: report what became of every ticked entry."""
        batch = self._batch
        resume_available = self._batch_share is None and self._batch_resume_available
        self._batch = None
        self._batch_share = None
        self._stop_requested = False
        self._downloading = False
        self._render_tasks()
        if batch is not None:
            self._set_status(
                batch_summary(
                    succeeded=batch.succeeded,
                    failed=batch.failed,
                    stopped=batch.stopped,
                    skipped=batch.skipped,
                    resume_available=resume_available,
                )
            )
        self._sync_controls()

    def login(self) -> None:
        if self._busy() or self._share_source is not None:
            # A successful login browses the account folder; that would contradict
            # a share view, whose controls are the ones for leaving it.
            return
        self._logging_in = True
        self._profile_generation += 1
        if self._nickname_var is not None:
            self._nickname_var.set("登录中…")
        self._sync_controls()
        succeeded = False
        try:
            self._login_window_factory(self._root).run()
            succeeded = True
        except LoginCancelled as error:
            self._set_status("已取消扫码登录")
            _release_exception_frames(error)
        except Exception as error:
            # ``LoginCancelled`` is checked first; every other failure (a typed
            # QuarkError, an OSError, or anything unexpected) becomes one
            # readable line instead of a traceback in a windowed program.
            self._set_status(describe_error(error, "扫码登录"))
            # A login provider may retain its error. Its traceback can retain
            # Tk widgets; release those frames on the Tk thread, not later GC.
            _release_exception_frames(error)
        finally:
            self._logging_in = False
            self._sync_controls()
        if succeeded:
            # A retained FID belongs to the account that supplied it. Successful
            # login may switch accounts; old results stay readable, without retries.
            if self._tasks:
                self._tasks_valid = False
                self._render_tasks()
                self._sync_controls()
            self._open(self._location.fid)
        if self._credential_available():
            self._request_profile()
        elif self._nickname_var is not None:
            self._nickname_var.set("未登录")

    # -- rendering -------------------------------------------------------

    def _render_listing(self, listing: Listing) -> None:
        self._adopt_rows(listing.entries)

    def _adopt_rows(self, entries: Iterable[Entry]) -> None:
        """Show a new listing and drop the ticks of the rows it no longer holds.

        The boxes are keyed by fid, so a row that came back with the new listing
        keeps its tick and a new row that took the same table position starts
        unticked; only a fid the listing is missing is dropped. The local filters
        are applied afterwards, never to the list itself.
        """
        self._rows = list(entries)
        self._checked &= {entry.fid for entry in self._rows}
        self._apply_filter()

    def _clear_rows(self) -> None:
        """Show an empty table: the rows of the view being left are gone.

        A folder switch asks the worker for the rows that replace them, and the
        table must not show the rows of the path that was left until that answer
        arrives — a failed read would otherwise leave rows under a location line
        they do not belong to.
        """
        self._rows = []
        self._render_rows(())
        self._show_counts(0)

    def _reset_search(self) -> None:
        """Drop the account search session: switching the view ends it."""
        self._search = None
        self._pending = None
        self._searching = False
        self._origin = None

    def _apply_filter(self) -> None:
        """Re-render the complete list the window holds through the local filters.

        Filtering always starts from :attr:`_rows`, never from the rows already
        on screen, so relaxing a filter brings back every row it had hidden.
        """
        try:
            rows = filter_entries(self._rows, query=self._keyword(), kind=self._kind(),
                                  extensions=self._extension_var.get() if self._extension_var is not None else "")
        except UsageError as error:
            self._render_rows(())
            self._show_counts(0)
            self._extension_error = str(error)
            self._set_status(self._extension_error)
            return
        if self._extension_error and self._status_var is not None and self._status_var.get() == self._extension_error:
            self._set_status(IDLE_STATUS)
        self._extension_error = ""
        if self._sort_key is not None:
            rows = sort_entries(rows, self._sort_key, descending=self._sort_descending)
        self._render_rows(rows)
        self._show_counts(len(rows))

    def sort_by(self, key: str) -> None:
        """Toggle one local column without moving checks or focus to another FID."""
        if self._busy():
            return
        if key not in ("name", "size", "path"):
            raise UsageError("排序字段必须是 name、size 或 path")
        tree = self._tree
        selected = {self._entries[iid].fid for iid in tree.selection() if iid in self._entries}
        focused = self._entries.get(tree.focus())
        self._sort_descending = not self._sort_descending if self._sort_key == key else False
        self._sort_key = key
        self._apply_filter()
        tree.selection_set([iid for iid, entry in self._entries.items() if entry.fid in selected])
        if focused is not None:
            for iid, entry in self._entries.items():
                if entry.fid == focused.fid:
                    tree.focus(iid)
                    tree.see(iid)
                    break
        for column, field, label in (("#0", "name", "名称"), ("size", "size", "大小"), ("path", "path", "路径")):
            arrow = " ↓" if self._sort_descending else " ↑"
            tree.heading(column, text=label + (arrow if field == key else ""))
        self._sync_controls()

    def _render_rows(self, rows: Iterable[Entry]) -> None:
        """Show exactly these rows; the table order is the download order."""
        tree = self._tree
        tree.delete(*tree.get_children())
        self._entries = {}
        for index, entry in enumerate(rows):
            iid = f"row{index}"
            self._entries[iid] = entry
            tree.insert(
                "",
                "end",
                iid=iid,
                text=entry.label,
                values=(
                    self._check_mark(entry.fid),
                    display_text(entry.path),
                    entry.type_label,
                    entry.size_label,
                ),
            )

    def _check_mark(self, fid: str) -> str:
        """The ``选择`` box of one row: the tick is keyed by fid, not by row."""
        return CHECKED_MARK if fid in self._checked else UNCHECKED_MARK

    def _refresh_checks(self) -> None:
        """Redraw the ticks the window already holds, and the lines that count them."""
        tree = self._tree
        if tree is None:
            return
        for iid, entry in self._entries.items():
            tree.set(iid, "check", self._check_mark(entry.fid))
        self._show_counts(len(self._entries))

    def _clear_checks(self) -> None:
        """Drop the ticks of the view being left; a folder switch starts empty."""
        if not self._checked:
            return
        self._checked.clear()
        self._refresh_checks()

    def _checked_entries(self) -> list[Entry]:
        """Every ticked row the filters currently show, in table order.

        Hidden rows are not in :attr:`_entries`, so a row a filter hides is
        never part of the click.
        """
        return [entry for entry in self._entries.values() if entry.fid in self._checked]

    def toggle_all(self) -> None:
        """Tick every visible row, or untick them if they all are already."""
        if self._busy():
            return
        visible = [entry.fid for entry in self._entries.values()]
        if visible and all(fid in self._checked for fid in visible):
            self._checked.difference_update(visible)
        else:
            self._checked.update(visible)
        self._refresh_checks()
        self._sync_controls()

    def _show_counts(self, visible: int) -> None:
        if self._count_var is None:
            return
        if self._search is not None and not self._rows:
            # A search that matched nothing says so instead of reporting the
            # zero counts an empty folder reports.
            self._count_var.set(NO_MATCH_RESULT_TEXT)
            return
        text = match_text(len(self._rows), visible)
        chosen = len(self._checked_entries())
        if chosen:
            text = f"{text}，已选 {chosen} 项"
        self._count_var.set(text)

    def _keyword(self) -> str:
        return self._search_var.get().strip() if self._search_var is not None else ""

    def _kind(self) -> str:
        label = self._kind_var.get() if self._kind_var is not None else KIND_CHOICES[0][0]
        for text, kind in KIND_CHOICES:
            if text == label:
                return kind
        return KIND_CHOICES[0][1]

    def _on_filter_change(self, *_event: Any) -> None:
        """A filter changed: re-render the rows already held, without a request."""
        if self._busy():
            return
        self._apply_filter()
        self._sync_controls()

    def _clear_filters(self) -> None:
        """Drop both local filters so a folder just opened is shown in full.

        The keyword box is also the query of the search buttons, so a filter left
        over from elsewhere would hide the rows of the folder that follows. The
        write trace of the keyword box re-renders the rows the window holds;
        :meth:`refresh` deliberately does not clear anything.
        """
        if self._kind_var is not None:
            self._kind_var.set(KIND_CHOICES[0][0])
        if self._extension_var is not None:
            self._extension_var.set(EXTENSION_CHOICES[0])
        if self._search_var is not None:
            self._search_var.set("")

    def _show_location(self) -> None:
        if self._location_var is None:
            return
        # The share's path is prefixed so the table never looks like the account
        # drive; the account's own breadcrumb is left untouched behind it.
        self._location_var.set(
            f"分享 {self._share_trail[-1]}" if self._share_source is not None else self._location.title
        )

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
        self._update_task(outcome.value, message)
        batch.settle(outcome)
        self._render_tasks()

    def _update_task(self, state: str, detail: str) -> None:
        batch = self._batch
        if batch is None or batch.current is None or batch.position > len(self._active_task_ids):
            return
        index = self._active_task_ids[batch.position - 1]
        task = self._tasks[index]
        task.state, task.detail = state, detail
        if self._task_tree is not None:
            self._task_tree.set(str(index), "status", task.state_label)
            self._task_tree.set(str(index), "detail", display_text(detail))
            self._show_task_detail()

    def _render_tasks(self) -> None:
        tree = self._task_tree
        if tree is not None:
            selected = tree.selection()
            tree.delete(*tree.get_children())
            for index, task in enumerate(self._tasks):
                tree.insert("", "end", iid=str(index), text=task.entry.label,
                            values=(task.state_label, display_text(task.detail)))
            if selected and int(selected[0]) < len(self._tasks):
                tree.selection_set(selected[0])
            self._show_task_detail()
        if self._task_summary_var is not None:
            done = sum(task.state == "done" for task in self._tasks)
            failed = sum(task.state == "failed" for task in self._tasks)
            text = f"共 {len(self._tasks)} 项 · 已完成 {done} · 失败 {failed}" if self._tasks else "暂无下载任务"
            if self._tasks and not self._tasks_valid:
                text += " · 登录已变化"
            self._task_summary_var.set(text)

    def _show_task_detail(self, _event: Any = None) -> None:
        if self._task_tree is None or self._task_detail_var is None:
            return
        selected = self._task_tree.selection()
        if not selected:
            self._task_detail_var.set("选择任务查看完整结果；右键可重试此项或打开保存目录")
            return
        task = self._tasks[int(selected[0])]
        source = "分享" if self._task_share is not None else "我的网盘"
        self._task_detail_var.set(display_text(
            f"{source} · 保存到 {self._task_output_dir}\n{task.entry.name}：{task.detail or task.state_label}"))

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
        """One rule per control: what a request in flight may still change."""
        busy = self._busy()
        frozen = self._frozen()
        share = self._share_source is not None
        free = "disabled" if busy else "normal"
        for button in (
            self._up_button,
            self._refresh_button,
            self._share_button,
            self._drive_button,
        ):
            _set_state(button, free)
        _set_state(
            self._return_button,
            "normal" if self._origin is not None and not busy else "disabled",
        )
        # A drive search would read the account instead of the share, so both
        # buttons are out of reach while the table shows a share.
        _set_state(self._search_button, "disabled" if busy or share else "normal")
        _set_state(self._scope_combo, "disabled" if busy or share else "readonly")
        if self._return_button is not None:
            if self._origin is not None and not share:
                self._return_button.pack(side="left", before=self._login_button, padx=(0, 4))
            else:
                self._return_button.pack_forget()
        _set_state(self._keyword_entry, free)
        _set_state(self._kind_combo, "disabled" if busy else "readonly")
        _set_state(self._extension_combo, "disabled" if busy else "normal")
        _set_state(self._share_entry, free)
        _set_state(self._passcode_entry, free)
        _set_state(self._login_button, "disabled" if busy or share else "normal")
        _set_state(self._output_button, "disabled" if frozen else "normal")
        _set_state(
            self._open_folder_button,
            "disabled" if frozen or not self._output_dir else "normal",
        )
        _set_state(self._stop_button, "normal" if self._downloading and not self._stop_requested else "disabled")
        _set_state(self._select_all_button, "disabled" if busy or not self._entries else "normal")
        if self._select_all_button is not None:
            selected_all = bool(self._entries) and all(entry.fid in self._checked for entry in self._entries.values())
            self._select_all_button.configure(text="取消全选" if selected_all else SELECT_ALL_TEXT)
        _set_state(self._connections_input, "disabled" if busy else "normal")
        retry_ready = not busy and self._tasks_valid
        _set_state(self._retry_button, "normal" if retry_ready and any(
            task.state == "failed" for task in self._tasks) else "disabled")
        _set_state(self._continue_button, "normal" if retry_ready and any(
            task.state in ("failed", "stopped", "skipped") for task in self._tasks) else "disabled")
        chosen = len(self._checked_entries())
        ready = not busy and bool(self._output_dir) and chosen > 0
        _set_state(self._download_button, "normal" if ready else "disabled")
        self._show_download_label(chosen)

    def _show_download_label(self, chosen: int) -> None:
        """The click's own label says how many of the shown rows it would take."""
        button = self._download_button
        if button is None:
            return
        text = DOWNLOAD_BUTTON_TEXT if not chosen else f"{DOWNLOAD_BUTTON_TEXT}（{chosen}）"
        button.configure(text=text)

    # -- helpers ---------------------------------------------------------

    def _busy(self) -> bool:
        """Whether a request in flight freezes the list, the filters and the click."""
        return self._downloading or self._searching or self._logging_in or self._reading_share

    def _frozen(self) -> bool:
        """Whether the save folder may not change: a batch or a login holds it."""
        return self._downloading or self._logging_in

    def _credential_available(self) -> bool:
        try:
            self._cookie()
        except QuarkError:
            return False
        return True

    def _open(self, fid: str) -> None:
        """Browse a real folder; the table stops showing search results."""
        if self._busy():
            return
        self._search = None
        self._set_status("正在读取目录……")
        self._worker.submit(Browse(fid))

    def _highlighted_row(self) -> Entry | None:
        """The highlighted row, the fallback for an activate with no row in it."""
        tree = self._tree
        if tree is None:
            return None
        for iid in tree.selection():
            entry = self._entries.get(iid)
            if entry is not None:
                return entry
        return None

    def _row_at(self, event: Any) -> str:
        """The row a click landed on, or ``""`` for the header and empty space."""
        identify = getattr(self._tree, "identify_row", None)
        y = getattr(event, "y", None)
        if identify is None or not isinstance(y, int):
            return ""
        iid = identify(y)
        return iid if isinstance(iid, str) else ""

    def _column_at(self, event: Any) -> str:
        """The table column a click landed on, or ``""`` when it cannot be told."""
        identify = getattr(self._tree, "identify_column", None)
        x = getattr(event, "x", None)
        if identify is None or not isinstance(x, int):
            return ""
        column = identify(x)
        return column if isinstance(column, str) else ""

    def _toggle(self, fid: str) -> None:
        """Flip one row's tick and redraw what counts it."""
        if fid in self._checked:
            self._checked.discard(fid)
        else:
            self._checked.add(fid)
        self._refresh_checks()
        self._sync_controls()

    def _on_click(self, event: Any) -> str | None:
        """A click on the ``选择`` column ticks that row and nothing else.

        Every other column keeps the table's normal behaviour, so highlighting a
        row for navigation is untouched; the header and empty space below the
        rows are ignored. A request in flight freezes the boxes and 全选, so no
        tick can move under a batch that is already frozen. Tk dispatches the
        second press to the double-click binding, which only handles navigation.
        The returned ``break`` suppresses the
        class binding that would move the highlight and the keyboard cursor, so
        the cursor is put on the clicked row here: the space bar then ticks the
        row the click just ticked.
        """
        if self._column_at(event) != CHECK_COLUMN:
            return None
        if self._busy():
            return "break"
        iid = self._row_at(event)
        entry = self._entries.get(iid)
        if entry is None:
            return "break"
        self._toggle(entry.fid)
        self._tree.focus_set()
        self._tree.focus(iid)
        return "break"

    def _on_space(self, _event: Any) -> str:
        """The space bar ticks the focused row, like a click on its box."""
        if self._busy():
            return "break"
        focus = getattr(self._tree, "focus", None)
        iid = focus() if focus is not None else ""
        entry = self._entries.get(iid) if isinstance(iid, str) else None
        if entry is not None:
            self._toggle(entry.fid)
        return "break"

    def _on_selection(self, _event: Any) -> None:
        self._sync_controls()

    def _on_activate(self, event: Any) -> None:
        # The tree stays clickable during a request in flight, so entering a
        # folder has to be refused here too; otherwise the location line and the
        # table would disagree, because ``_open`` refuses to browse while busy.
        if self._busy():
            return
        if self._column_at(event) == CHECK_COLUMN:
            # That column only ever ticks a row; a double-click there must not
            # also walk into the folder under it.
            return
        if event is None:
            # No row to read (a keyboard-driven activate), so the highlighted row
            # is the one meant.
            entry = self._highlighted_row()
        else:
            entry = self._entries.get(self._row_at(event))
        if entry is None or not entry.directory:
            return
        if self._share_source is not None:
            # Inside a share every row knows its own full path, so the share's
            # chain is extended with it and the next read asks for that path.
            if not entry.path:
                return
            self._share_trail.append(entry.path)
            self._clear_checks()
            self._clear_rows()
            self._clear_filters()
            self._show_location()
            self._open_share_path()
            return
        if entry.location:
            # A search result knows the folder chain it lives in, so the window
            # moves straight to that real folder instead of guessing a parent.
            self._location.replace(entry.location)
        else:
            self._location.enter(entry.fid, entry.name)
        # The folder just entered is shown in full: a keyword left over from the
        # table that was left behind would hide its rows.
        self._clear_checks()
        self._clear_filters()
        self._show_location()
        self._open(entry.fid)


def run_window() -> int:
    """Open the download window and block until it is closed."""
    DownloaderWindow().run()
    return 0
