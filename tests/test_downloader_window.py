"""The downloader window's rendering and event handling.

The widget-writing methods are driven with stand-in widgets, so the state
machine (listing rows, progress bar, control states, login outcome) is covered
without a display. Two Tk tests then build and close the real window.
"""

from __future__ import annotations

import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from quark_cli.auth import LoginCancelled
from quark_cli.downloader.app import (
    CHECK_COLUMN,
    CHECKED_MARK,
    CLOSE_BUSY_STATUS,
    DOWNLOAD_BUTTON_TEXT,
    IDLE_STATUS,
    KIND_CHOICES,
    LOGIN_HINT,
    NO_MATCH_RESULT_TEXT,
    OPEN_FOLDER_ACTION,
    SEARCH_HINT,
    SEARCHING_STATUS,
    SELECT_ALL_TEXT,
    STOPPING_STATUS,
    UNCHECKED_MARK,
    WINDOW_TITLE,
    DownloaderWindow,
    default_login_window,
    match_text,
)
from quark_cli.downloader.core import (
    DOWNLOAD_ACTION,
    ROOT_FID,
    SEARCH_ACTION,
    Entry,
    describe_error,
)
from quark_cli.downloader.worker import (
    Browse,
    AccountProfile,
    Download,
    Failed,
    Finished,
    Listing,
    Progress,
    Search,
    SearchResults,
    Stopped,
)
from quark_cli.errors import AuthenticationError, LocalIOError, RemoteError
from quark_cli.potplayer.login_gui import QrLoginWindow


def _tk_available() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


_TK = _tk_available()

#: The filter labels the window offers, in the order the combobox shows them.
KIND_ALL, KIND_FILES, KIND_DIRECTORIES = (label for label, _kind in KIND_CHOICES)

#: The ``kind`` each label asks for, as :meth:`DownloaderWindow._kind` reads it back.
KIND_CODE = dict(KIND_CHOICES)


def _tk_root() -> Any:
    """A real ``Tk`` root, or a skip when this process cannot create one.

    Creating a second Tcl interpreter in one process intermittently fails on
    this Anaconda build (``invalid command name "tcl_findLibrary"``); a Tk test
    that cannot start asks to be skipped rather than reporting it as a code bug.
    """
    import tkinter

    try:
        return tkinter.Tk()
    except tkinter.TclError:
        raise unittest.SkipTest("Tkinter cannot create an interpreter here") from None


def _no_cookie() -> str:
    raise AuthenticationError("请设置 QUARK_COOKIE 或 QUARK_COOKIE_FILE")


class _FakeVar:
    """A ``tk.StringVar`` stand-in that also runs the write traces of its users."""

    def __init__(self, value: str = "") -> None:
        self.value = value
        self.traces: list[Any] = []

    def set(self, value: str) -> None:
        self.value = value
        for callback in self.traces:
            callback("write", "", "write")

    def get(self) -> str:
        return self.value

    def trace_add(self, _mode: str, callback: Any) -> None:
        self.traces.append(callback)


class _FakeTree:
    """Records the rows a real ``ttk.Treeview`` would show; no Tk involved."""

    #: The geometry the fake answers ``identify_*`` with: a check column that is
    #: :data:`CHECK_WIDTH` wide, and rows that are :data:`ROW_HEIGHT` tall.
    CHECK_WIDTH = 48
    ROW_HEIGHT = 20

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, tuple]] = []
        self.selection_ids: tuple[str, ...] = ()
        self.focus_id: str = ""
        self.bindings: dict[str, Any] = {}
        self.headings: dict[str, dict[str, Any]] = {}

    def delete(self, *items: Any) -> None:
        self.rows.clear()

    def get_children(self) -> tuple[str, ...]:
        return tuple(iid for iid, _text, _values in self.rows)

    def insert(self, parent: str, index: str, *, iid: str, text: str, values: tuple) -> None:
        self.rows.append((iid, text, tuple(values)))

    def selection(self) -> tuple[str, ...]:
        return self.selection_ids

    def selection_set(self, items) -> None:
        self.selection_ids = tuple(items)

    def heading(self, column: str, **options: Any) -> None:
        self.headings.setdefault(column, {}).update(options)

    def see(self, item: str) -> None:
        self.seen_id = item

    def focus(self, item: str | None = None) -> str:
        """Read the keyboard cursor back, or move it like ``tree.focus(item)``."""
        if item is not None:
            self.focus_id = item
        return self.focus_id

    def bind(self, sequence: str, callback: Any) -> None:
        self.bindings[sequence] = callback

    def focus_set(self) -> None:
        self.has_focus = True

    def set(self, iid: str, column: str, value: Any) -> str:
        """Write one cell, the way ``tree.set`` writes the check column."""
        for index, (row_id, text, values) in enumerate(self.rows):
            if row_id != iid:
                continue
            cells = list(values)
            cells[0 if column == "check" else 1] = value
            self.rows[index] = (row_id, text, tuple(cells))
            return str(value)
        raise KeyError(iid)

    def identify_row(self, y: int) -> str:
        index = int(y) // self.ROW_HEIGHT
        if 0 <= index < len(self.rows):
            return self.rows[index][0]
        return ""

    def identify_column(self, x: int) -> str:
        if x <= 0:
            return ""
        return CHECK_COLUMN if x < self.CHECK_WIDTH else "#0"


class _FakeButton:
    def __init__(self) -> None:
        self.state = "normal"
        self.text = ""

    def configure(self, **options: Any) -> None:
        if "state" in options:
            self.state = options["state"]
        if "text" in options:
            self.text = options["text"]

    def pack(self, **_options):
        self.visible = True

    def pack_forget(self):
        self.visible = False


class _FakeCombo(_FakeButton):
    """A ``ttk.Combobox`` stand-in: it records its state and its bindings."""

    def __init__(self) -> None:
        super().__init__()
        self.bindings: dict[str, Any] = {}
        self.state = "readonly"

    def bind(self, sequence: str, callback: Any) -> None:
        self.bindings[sequence] = callback


class _FakeRoot:
    """Records the teardown a real ``Tk`` root would perform."""

    def __init__(self) -> None:
        self.destroyed = False

    def after_cancel(self, _identifier: Any) -> None:
        pass

    def destroy(self) -> None:
        self.destroyed = True


def _alive(widget: Any) -> bool:
    """Whether a real Tk widget still exists; a destroyed root answers ``False``."""
    try:
        return bool(widget.winfo_exists())
    except Exception:
        return False


class _FakeBar:
    def __init__(self) -> None:
        self.mode = "determinate"
        self.value = 0.0
        self.running = False
        self.starts: list[int] = []

    def configure(self, **options: Any) -> None:
        if "mode" in options:
            self.mode = options["mode"]
        if "value" in options:
            self.value = options["value"]

    def start(self, interval: int) -> None:
        self.running = True
        self.starts.append(interval)

    def stop(self) -> None:
        self.running = False


class _FakeLog:
    """Records the per-item lines a real ``tk.Text`` would show; no Tk involved."""

    def __init__(self) -> None:
        self.content = ""
        self.state = "disabled"

    def configure(self, **options: Any) -> None:
        if "state" in options:
            self.state = options["state"]

    def insert(self, _index: Any, text: str) -> None:
        self.content += text

    def see(self, _index: Any) -> None:
        pass


class _Backend:
    def account_nickname(self):
        return "测试账号"

    """A backend that answers immediately; the window tests never start the thread."""

    def __init__(self, rows: list[Entry] | None = None) -> None:
        self.rows = list(rows or [])

    def listing(self, fid: str) -> list[Entry]:
        return list(self.rows)

    def search(self, fid: str, query: str) -> list[Entry]:
        return [entry for entry in self.rows if query.casefold() in entry.name.casefold()]

    def download(self, request: Download, progress: Any, should_stop: Any = None) -> dict:
        return {"fid": request.fid, "path": request.target, "size": 0}


class _LoginWindow:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.runs = 0

    def run(self) -> None:
        self.runs += 1
        if self.error is not None:
            raise self.error


class WindowCase(unittest.TestCase):
    def _window(self, *, cookie: Any = None, **options: Any) -> DownloaderWindow:
        window = DownloaderWindow(
            cookie=cookie or (lambda: "sid=test"),
            backend_factory=options.pop("backend_factory", lambda loader: _Backend()),
            **options,
        )
        window._tree = _FakeTree()
        window._status_var = _FakeVar()
        window._location_var = _FakeVar()
        window._output_var = _FakeVar()
        window._search_var = _FakeVar()
        window._kind_var = _FakeVar(KIND_ALL)
        window._count_var = _FakeVar()
        window._share_var = _FakeVar()
        window._passcode_var = _FakeVar()
        window._nickname_var = _FakeVar("未登录")
        window._scope_var = _FakeVar("全盘")
        window._scope_combo = _FakeCombo()
        window._share_var.trace_add("write", window._on_share_input_change)
        # ``_build`` ties the keyword box to the local filter; the fake widgets
        # repeat that here, so setting the variable behaves like typing. The real
        # wiring is covered by the Tk smoke tests.
        window._search_var.trace_add("write", window._on_filter_change)
        window._progress = _FakeBar()
        window._log = _FakeLog()
        window._kind_combo = _FakeCombo()
        for name in (
            "_up_button",
            "_refresh_button",
            "_login_button",
            "_output_button",
            "_open_folder_button",
            "_download_button",
            "_stop_button",
            "_search_button",
            "_return_button",
            "_share_button",
            "_drive_button",
            "_select_all_button",
        ):
            setattr(window, name, _FakeButton())
        window._keyword_entry = _FakeButton()
        window._share_entry = _FakeButton()
        window._passcode_entry = _FakeButton()
        self.addCleanup(window._worker.stop)
        return window

    @staticmethod
    def _submitted(window: DownloaderWindow) -> Any:
        try:
            return window._worker._requests.get_nowait()
        except queue.Empty:
            return window._worker._search_requests.get_nowait()[0]

    def _listing(self, window: DownloaderWindow, *entries: Entry) -> None:
        """Deliver one folder listing of the root, as the worker would."""
        window._handle(Listing(ROOT_FID, entries))

    @staticmethod
    def _results(window: DownloaderWindow, query: str, *entries: Entry, fid: str = ROOT_FID) -> None:
        window._handle(SearchResults(request_id=window._search_generation, fid=fid, query=query, entries=entries))

    @staticmethod
    def _click(window: DownloaderWindow, *, x: int = 10, y: int = 0, time: int | None = None) -> Any:
        """One press in the fake table: ``x`` picks the column, ``y`` the row."""
        event = SimpleNamespace(x=x, y=y)
        if time is not None:
            event.time = time
        return window._on_click(event)

    @staticmethod
    def _marks(window: DownloaderWindow) -> list[str]:
        """The ``选择`` box of every row the fake table shows, in table order."""
        return [row[2][0] for row in window._tree.rows]

    @staticmethod
    def _tick(window: DownloaderWindow, *fids: str) -> None:
        """Tick rows the way a click on their box does, without a fake press."""
        window._checked.update(fids)
        window._refresh_checks()
        window._sync_controls()


class CloseTests(WindowCase):
    def test_close_is_refused_while_a_download_runs_and_works_after_it(self):
        window = self._window()
        root = window._root = _FakeRoot()
        window._downloading = True

        window.close()
        self.assertFalse(root.destroyed)
        self.assertEqual(window._status_var.value, CLOSE_BUSY_STATUS)

        window._handle(Finished("下载", "已下载 f.bin：4 B"))
        self.assertFalse(window._downloading)
        window.close()
        self.assertTrue(root.destroyed)
        self.assertIsNone(window._root)

    def test_close_without_a_download_tears_the_window_down_at_once(self):
        window = self._window()
        root = window._root = _FakeRoot()
        window.close()
        self.assertTrue(root.destroyed)

    def test_a_browse_failure_does_not_end_a_running_download(self):
        # The window can submit a download from an older list while that
        # folder's browse is still in flight; when the browse then fails, the
        # transfer keeps running and the close guard has to survive the report.
        window = self._window()
        root = window._root = _FakeRoot()
        window._downloading = True
        window._handle(Progress("f.bin：1 B / 2 B · 1.0 MiB/s", 0.5))

        window._handle(Failed("浏览", "浏览失败：上游连接失败"))
        self.assertTrue(window._downloading)
        self.assertEqual(window._status_var.value, "浏览失败：上游连接失败")
        self.assertEqual(window._progress.value, 50.0)
        window.close()
        self.assertFalse(root.destroyed)
        self.assertEqual(window._status_var.value, CLOSE_BUSY_STATUS)

        window._handle(Finished("下载", "已下载 f.bin：2 B"))
        self.assertFalse(window._downloading)
        window.close()
        self.assertTrue(root.destroyed)
        self.assertIsNone(window._root)

    def test_a_browse_failure_while_idle_only_reports(self):
        window = self._window()
        root = window._root = _FakeRoot()

        window._handle(Failed("浏览", "浏览失败：上游连接失败"))
        self.assertFalse(window._downloading)
        self.assertEqual(window._status_var.value, "浏览失败：上游连接失败")
        self.assertEqual(window._refresh_button.state, "normal")
        window.close()
        self.assertTrue(root.destroyed)


class RenderingTests(WindowCase):
    def test_a_listing_replaces_the_rows_and_resets_the_status(self):
        window = self._window()
        window._handle(Listing("0", (Entry("d", "目录", True, None), Entry("f", "f.bin", False, 4))))
        self.assertEqual(
            window._tree.rows,
            [
                ("row0", "目录", (UNCHECKED_MARK, "", "目录", "—")),
                ("row1", "f.bin", (UNCHECKED_MARK, "", "文件", "4 B")),
            ],
        )
        self.assertEqual(window._status_var.value, IDLE_STATUS)
        self.assertEqual(window._count_var.value, match_text(2, 2))

    def test_progress_switches_the_bar_and_finished_fills_it(self):
        window = self._window()
        window._handle(Progress("报告.bin：500 B / 1.0 KiB · 1.0 MiB/s", None))
        self.assertEqual(window._status_var.value, "报告.bin：500 B / 1.0 KiB · 1.0 MiB/s")
        self.assertEqual((window._progress.mode, window._progress.running), ("indeterminate", True))
        self.assertEqual(window._progress.starts, [80])

        window._handle(Progress("报告.bin：1.0 KiB / 1.0 KiB · 1.0 MiB/s", 0.5))
        self.assertEqual((window._progress.mode, window._progress.running), ("determinate", False))
        self.assertEqual(window._progress.value, 50.0)

        window._handle(Finished("下载", "已下载 报告.bin：1.0 KiB"))
        self.assertEqual(window._progress.value, 100.0)
        self.assertEqual(window._status_var.value, "已下载 报告.bin：1.0 KiB")

    def test_a_failure_clears_the_bar_and_reports_the_message(self):
        window = self._window()
        window._handle(Progress("报告.bin：1 B / 2 B · 1.0 MiB/s", None))
        window._handle(Failed("下载", "下载失败：目标文件已存在；使用 --overwrite 显式覆盖"))
        self.assertEqual((window._progress.mode, window._progress.running), ("determinate", False))
        self.assertEqual(window._progress.value, 0.0)
        self.assertEqual(window._status_var.value, "下载失败：目标文件已存在；使用 --overwrite 显式覆盖")


class DownloadTests(WindowCase):
    def test_connections_are_validated_and_frozen_for_the_batch(self):
        window = self._window()
        self._listing(window, Entry("f1", "a.bin", False, 4), Entry("f2", "b.bin", False, 4))
        window._output_dir = str(self.output)
        self._tick(window, "f1", "f2")
        window._connections_var = _FakeVar("17")
        window._connections_input = _FakeButton()
        window.start_download()
        self.assertIn("1–16", window._status_var.get())
        self.assertFalse(window._downloading)
        window._connections_var.set("8")
        window.start_download()
        self.assertEqual(self._submitted(window).connections, 8)
        self.assertEqual(window._connections_input.state, "disabled")
        window._connections_var.set("2")
        window._handle(Finished(DOWNLOAD_ACTION, "done"))
        self.assertEqual(self._submitted(window).connections, 8)
        window._handle(Finished(DOWNLOAD_ACTION, "done"))
        self.assertEqual(window._connections_input.state, "normal")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)

    def test_download_needs_a_ticked_row_and_an_output_directory(self):
        window = self._window()
        window._handle(Listing("0", (Entry("f", "f.bin", False, 4),)))

        window.start_download()
        self.assertEqual(window._status_var.value, "请先在列表中勾选一个文件或目录")
        with self.assertRaises(queue.Empty):
            self._submitted(window)

        # Highlighting the row is not the selection the click takes.
        window._tree.selection_ids = ("row0",)
        window.start_download()
        self.assertEqual(window._status_var.value, "请先在列表中勾选一个文件或目录")
        with self.assertRaises(queue.Empty):
            self._submitted(window)

        self._tick(window, "f")
        window.start_download()
        self.assertEqual(window._status_var.value, "下载失败：请先选择本地输出目录")
        with self.assertRaises(queue.Empty):
            self._submitted(window)

    def test_a_ticked_folder_is_planned_under_its_name_and_blocks_the_window(self):
        window = self._window()
        window._output_dir = str(self.output)
        window._output_var.set(str(self.output))
        window._handle(Listing("0", (Entry("d", "中文 目录", True, None),)))
        self._tick(window, "d")

        window.start_download()
        self.assertEqual(
            self._submitted(window),
            Download(fid="d", target=str(self.output / "中文 目录"), recursive=True, overwrite=False, name="中文 目录"),
        )
        self.assertTrue(window._downloading)
        self.assertEqual(window._download_button.state, "disabled")
        self.assertEqual(window._up_button.state, "disabled")
        self.assertEqual(window._login_button.state, "disabled")

    def test_a_ticked_file_uses_a_single_non_recursive_download(self):
        window = self._window()
        window._output_dir = str(self.output)
        window._handle(Listing("0", (Entry("f", "报告.PDF", False, 4),)))
        self._tick(window, "f")
        window.start_download()
        self.assertEqual(
            self._submitted(window),
            Download(fid="f", target=str(self.output / "报告.PDF"), recursive=False, overwrite=False, name="报告.PDF"),
        )

    def test_navigation_and_repeat_downloads_are_refused_while_downloading(self):
        window = self._window()
        window._output_dir = str(self.output)
        window._handle(Listing("0", (Entry("f", "报告.PDF", False, 4),)))
        self._tick(window, "f")
        window.start_download()
        self._submitted(window)

        window.start_download()
        window.refresh()
        window.go_up()
        window.choose_output()
        with self.assertRaises(queue.Empty):
            self._submitted(window)

        window._handle(Finished("下载", "已下载 报告.PDF：4 B"))
        self.assertFalse(window._downloading)
        self.assertEqual(window._download_button.state, "normal")
        window.refresh()
        self.assertEqual(self._submitted(window), Browse("0"))

    def test_a_multi_selection_downloads_in_display_order_and_survives_a_failure(self):
        window = self._window()
        window._output_dir = str(self.output)
        window._handle(
            Listing(
                "0",
                (
                    Entry("f1", "a.bin", False, 4),
                    Entry("f2", "b.bin", False, 4),
                    Entry("f3", "c.bin", False, 4),
                ),
            )
        )
        # The boxes were ticked bottom-up; only the table's order reaches the worker.
        self._tick(window, "f3", "f1", "f2")

        window.start_download()
        self.assertEqual(
            self._submitted(window),
            Download(fid="f1", target=str(self.output / "a.bin"), recursive=False, overwrite=False, name="a.bin"),
        )
        # One at a time: nothing else is submitted while an item is in flight.
        with self.assertRaises(queue.Empty):
            self._submitted(window)

        conflict = "下载失败：目标文件已存在；使用 --overwrite 显式覆盖"
        window._handle(Failed(DOWNLOAD_ACTION, conflict))
        self.assertEqual(
            self._submitted(window),
            Download(fid="f2", target=str(self.output / "b.bin"), recursive=False, overwrite=False, name="b.bin"),
        )
        window._handle(Finished(DOWNLOAD_ACTION, "已下载 b.bin：4 B"))
        self.assertEqual(
            self._submitted(window),
            Download(fid="f3", target=str(self.output / "c.bin"), recursive=False, overwrite=False, name="c.bin"),
        )
        window._handle(Failed(DOWNLOAD_ACTION, conflict))

        self.assertFalse(window._downloading)
        with self.assertRaises(queue.Empty):
            self._submitted(window)
        self.assertEqual(window._status_var.value, "下载结束：共 3 项，成功 1 项，失败 2 项")
        self.assertEqual(
            window._log.content,
            f"（1/3）{conflict}\n（2/3）已下载 b.bin：4 B\n（3/3）{conflict}\n",
        )


class StopTests(WindowCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)

    def _tick_three(self, window: DownloaderWindow) -> None:
        window._handle(
            Listing(
                "0",
                (
                    Entry("f1", "a.bin", False, 4),
                    Entry("f2", "b.bin", False, 4),
                    Entry("f3", "c.bin", False, 4),
                ),
            )
        )
        self._tick(window, "f1", "f2", "f3")

    def test_stopping_drops_the_pending_items_and_stays_busy_until_the_item_ends(self):
        window = self._window()
        root = window._root = _FakeRoot()
        window._output_dir = str(self.output)
        self._tick_three(window)
        window.start_download()
        self.assertEqual(
            self._submitted(window),
            Download(fid="f1", target=str(self.output / "a.bin"), recursive=False, overwrite=False, name="a.bin"),
        )
        self.assertEqual(window._stop_button.state, "normal")

        window.stop_download()
        self.assertTrue(window._worker._stop.is_set())
        self.assertEqual(window._stop_button.state, "disabled")
        self.assertEqual(window._status_var.value, STOPPING_STATUS)
        # The items that had not started are gone and the window is still busy:
        # neither closing nor a second batch may start under the item that is
        # being ended at its checkpoint.
        with self.assertRaises(queue.Empty):
            self._submitted(window)
        window.close()
        self.assertFalse(root.destroyed)
        self.assertEqual(window._status_var.value, STOPPING_STATUS)
        window.start_download()
        with self.assertRaises(queue.Empty):
            self._submitted(window)

        window._handle(Stopped(DOWNLOAD_ACTION, "下载已停止：安全检查点已保留"))
        self.assertFalse(window._downloading)
        self.assertIsNone(window._batch)
        self.assertEqual(window._status_var.value, "已停止：成功 0 项，1 项已停止（检查点已保留），2 项未开始")
        self.assertEqual(
            window._log.content,
            "已请求停止：2 项未开始已取消；当前项目将在检查点结束\n（1/3）下载已停止：安全检查点已保留\n",
        )

    def test_the_same_selection_can_be_submitted_again_after_a_stop(self):
        window = self._window()
        window._root = _FakeRoot()
        window._output_dir = str(self.output)
        self._tick_three(window)
        window.start_download()
        self._submitted(window)

        window.stop_download()
        window._handle(Stopped(DOWNLOAD_ACTION, "下载已停止：安全检查点已保留"))

        window.start_download()
        self.assertFalse(window._worker._stop.is_set())
        self.assertEqual(
            self._submitted(window),
            Download(fid="f1", target=str(self.output / "a.bin"), recursive=False, overwrite=False, name="a.bin"),
        )
        for name in ("a.bin", "b.bin"):
            window._handle(Finished(DOWNLOAD_ACTION, f"已下载 {name}：4 B"))
            self._submitted(window)
        window._handle(Finished(DOWNLOAD_ACTION, "已下载 c.bin：4 B"))
        self.assertFalse(window._downloading)
        self.assertEqual(window._status_var.value, "全部下载完成：共 3 项")
        window.close()
        self.assertIsNone(window._root)


class NavigationTests(WindowCase):
    def test_activating_a_folder_enters_it_and_upper_returns(self):
        window = self._window()
        window._handle(Listing("0", (Entry("d", "目录", True, None), Entry("f", "f.bin", False, 1))))

        window._tree.selection_ids = ("row1",)
        window._on_activate(None)
        with self.assertRaises(queue.Empty):
            self._submitted(window)

        window._tree.selection_ids = ("row0",)
        window._on_activate(None)
        self.assertEqual((window._location.fid, window._location_var.value), ("d", "/目录"))
        self.assertEqual(self._submitted(window), Browse("d"))

        window.go_up()
        self.assertEqual((window._location.fid, window._location_var.value), (ROOT_FID, "/"))
        self.assertEqual(self._submitted(window), Browse(ROOT_FID))

    def test_upper_at_the_root_reports_and_asks_for_nothing(self):
        window = self._window()
        window.go_up()
        self.assertEqual(window._status_var.value, "已经在最上层目录")
        with self.assertRaises(queue.Empty):
            self._submitted(window)

    def test_a_slow_listing_for_a_folder_the_window_left_is_dropped(self):
        window = self._window()
        window._handle(Listing("0", (Entry("d", "目录", True, None),)))
        window._tree.selection_ids = ("row0",)
        window._on_activate(None)
        self.assertEqual(self._submitted(window), Browse("d"))
        self.assertEqual(window._location.fid, "d")

        # The root's answer arrives after the window already entered "目录".
        window._handle(Listing("0", (Entry("f", "根文件.bin", False, 1),)))
        self.assertEqual([row[1] for row in window._tree.rows], ["目录"])
        self.assertEqual(window._location.fid, "d")

        window._handle(Listing("d", (Entry("f", "子文件.bin", False, 2),)))
        self.assertEqual([row[1] for row in window._tree.rows], ["子文件.bin"])

    def test_activating_a_folder_during_a_download_changes_nothing(self):
        window = self._window()
        window._handle(Listing("0", (Entry("d", "目录", True, None),)))
        window._downloading = True
        window._tree.selection_ids = ("row0",)

        window._on_activate(None)
        self.assertEqual(window._location.fid, ROOT_FID)
        self.assertEqual(window._location_var.value, "")
        with self.assertRaises(queue.Empty):
            self._submitted(window)


class MatchTextTests(unittest.TestCase):
    def test_counts_and_the_no_match_hint(self):
        self.assertEqual(match_text(0, 0), "共 0 项，显示 0 项")
        self.assertEqual(match_text(10, 10), "共 10 项，显示 10 项")
        self.assertEqual(match_text(10, 3), "共 10 项，显示 3 项")
        self.assertEqual(match_text(10, 0), "无匹配项（共 10 项）")


class SearchTests(WindowCase):
    def test_search_uses_the_scope_selector_and_only_results_show_return(self):
        window = self._window()
        self._listing(window, Entry("d", "目录", True))
        self.assertFalse(window._return_button.visible)
        window._tree.selection_ids = ("row0",)
        window._on_activate(None)
        self._submitted(window)
        window._scope_var.set("当前目录及子目录")
        window._search_var.set("目标")
        window.search()
        self.assertEqual(self._submitted(window), Search("d", "目标", request_id=window._search_generation, recursive=(window._pending or window._search).recursive))
        self.assertTrue(window._return_button.visible)
        window._handle(SearchResults("d", "目标", (), request_id=window._search_generation))
        window.leave_search()
        self.assertFalse(window._return_button.visible)

    """Submitting a search, and what its answer does to the table."""

    def test_the_subdirectories_button_searches_the_folder_shown(self):
        window = self._window()
        self._listing(window, Entry("d", "目录", True, None))
        window._search_var.set("  报告  ")

        window.search_subdirectories()
        self.assertEqual(self._submitted(window), Search(fid=ROOT_FID, query="报告", request_id=window._search_generation, recursive=(window._pending or window._search).recursive))
        self.assertTrue(window._searching)
        self.assertEqual(window._status_var.value, SEARCHING_STATUS)
        # One request at a time: nothing else is submitted while it is in flight.
        with self.assertRaises(queue.Empty):
            self._submitted(window)

    def test_the_plain_button_searches_the_whole_drive_from_wherever_it_is(self):
        window = self._window()
        self._listing(window, Entry("d", "目录", True, None))
        window._tree.selection_ids = ("row0",)
        window._on_activate(None)
        self._submitted(window)
        self.assertEqual(window._location.fid, "d")
        window._search_var.set("报告")

        window.search()
        self.assertEqual(self._submitted(window), Search(fid=ROOT_FID, query="报告", request_id=window._search_generation, recursive=(window._pending or window._search).recursive))

        window._handle(Failed(SEARCH_ACTION, "搜索失败：上游连接失败"))
        window.search_subdirectories()
        self.assertEqual(self._submitted(window), Search(fid="d", query="报告", request_id=window._search_generation, recursive=(window._pending or window._search).recursive))

    def test_a_blank_keyword_only_hints_and_submits_nothing(self):
        window = self._window()
        window._search_var.set("   ")

        window.search_subdirectories()
        self.assertEqual(window._status_var.value, SEARCH_HINT)
        self.assertFalse(window._searching)
        self.assertIsNone(window._origin)
        with self.assertRaises(queue.Empty):
            self._submitted(window)

    def test_results_replace_the_table_and_show_the_drive_path(self):
        window = self._window()
        self._listing(window, Entry("f", "旧.bin", False, 4))
        window._search_var.set("报告")
        window.search_subdirectories()
        self._submitted(window)

        window._handle(
            SearchResults(request_id=window._search_generation,
                fid=ROOT_FID,
                query="报告",
                entries=(
                    Entry("m", "报告.bin", False, 4, "/我的网盘/文档/报告.bin", (("0", "我的网盘"), ("d", "文档"))),
                    Entry("d2", "报告目录", True, None, "/我的网盘/报告目录", (("0", "我的网盘"), ("d2", "报告目录"))),
                ),
            )
        )

        self.assertEqual(
            window._tree.rows,
            [
                ("row0", "报告.bin", (UNCHECKED_MARK, "/我的网盘/文档/报告.bin", "文件", "4 B")),
                ("row1", "报告目录", (UNCHECKED_MARK, "/我的网盘/报告目录", "目录", "—")),
            ],
        )
        self.assertEqual(window._status_var.value, IDLE_STATUS)
        self.assertEqual(window._count_var.value, match_text(2, 2))
        self.assertFalse(window._searching)
        self.assertEqual(window._search, Search(fid=ROOT_FID, query="报告", request_id=window._search_generation, recursive=(window._pending or window._search).recursive))
        # A search session is running, so it can be left again; the buttons that
        # ask for a request are usable now that none is in flight.
        self.assertEqual(window._return_button.state, "normal")
        self.assertEqual(window._search_button.state, "normal")
        self.assertEqual(window._keyword_entry.state, "normal")

    def test_an_empty_result_leaves_an_empty_table_and_asks_nothing_new(self):
        window = self._window()
        # An empty folder keeps reporting its own counts...
        self._listing(window)
        self.assertEqual(window._count_var.value, match_text(0, 0))

        window._search_var.set("报告")
        window.search_subdirectories()
        self._submitted(window)

        # ...while a search that matched nothing says so outright.
        window._handle(SearchResults(request_id=window._search_generation, fid=ROOT_FID, query="报告", entries=()))
        self.assertEqual(window._tree.rows, [])
        self.assertEqual(window._count_var.value, NO_MATCH_RESULT_TEXT)
        self.assertEqual(window._status_var.value, IDLE_STATUS)
        with self.assertRaises(queue.Empty):
            self._submitted(window)

    def test_a_failed_search_reports_leaves_the_list_and_the_folder_usable(self):
        window = self._window()
        # The keyword box also filters the rows the window holds, so a row that
        # survives it is what tells the failure apart from a clobbered table.
        self._listing(window, Entry("f", "报告a.bin", False, 4))
        window._search_var.set("报告")
        window.search_subdirectories()
        self._submitted(window)

        window._handle(Failed(SEARCH_ACTION, "搜索失败：上游连接失败"))

        self.assertEqual(window._status_var.value, "搜索失败：上游连接失败")
        self.assertFalse(window._searching)
        self.assertEqual([row[1] for row in window._tree.rows], ["报告a.bin"])
        self.assertEqual(window._search_button.state, "normal")
        # Nothing of the search reached the table, so there is no search session
        # to return from; the window simply refreshes the folder it shows.
        self.assertIsNone(window._origin)
        self.assertEqual(window._return_button.state, "disabled")
        window.refresh()
        self.assertEqual(self._submitted(window), Browse(ROOT_FID))

    def test_an_answer_to_an_older_question_is_dropped(self):
        window = self._window()
        self._listing(window, Entry("f", "报告旧.bin", False, 4))
        window._search_var.set("报告")
        window.search_subdirectories()
        self._submitted(window)

        # Another keyword, another folder: neither answer belongs to the request
        # the window is waiting for.
        window._handle(SearchResults(request_id=window._search_generation, fid=ROOT_FID, query="别的词", entries=(Entry("x", "别的.bin", False, 4),)))
        window._handle(SearchResults(request_id=window._search_generation, fid="d", query="报告", entries=(Entry("y", "别处.bin", False, 4),)))
        self.assertTrue(window._searching)
        self.assertEqual([row[1] for row in window._tree.rows], ["报告旧.bin"])

        window._handle(
            SearchResults(request_id=window._search_generation,
                fid=ROOT_FID,
                query="报告",
                entries=(Entry("m", "报告.bin", False, 4, "/我的网盘/报告.bin", (("0", "我的网盘"),)),),
            )
        )
        self.assertFalse(window._searching)
        self.assertEqual([row[1] for row in window._tree.rows], ["报告.bin"])

    def test_a_late_listing_cannot_replace_the_search_results(self):
        window = self._window()
        window._search_var.set("报告")
        window.search_subdirectories()
        self._submitted(window)
        window._handle(
            SearchResults(request_id=window._search_generation,
                fid=ROOT_FID,
                query="报告",
                entries=(Entry("m", "报告.bin", False, 4, "/我的网盘/报告.bin", (("0", "我的网盘"),)),),
            )
        )

        # The folder's own listing arrives after the search was answered: the
        # table shows the search, so the folder rows are stale and dropped.
        self._listing(window, Entry("f", "旧.bin", False, 4))
        self.assertEqual([row[1] for row in window._tree.rows], ["报告.bin"])
        self.assertEqual(window._status_var.value, IDLE_STATUS)

    def test_a_listing_that_arrives_while_the_search_runs_is_ignored(self):
        window = self._window()
        self._listing(window, Entry("f", "报告旧.bin", False, 4))
        window._search_var.set("报告")
        window.search_subdirectories()
        self._submitted(window)

        # The listing of the folder the search started from arrives while that
        # search is still unanswered: it must neither fill the table nor end the
        # search state, or the window would sit un-busy on rows the user never
        # asked for and a second search could be submitted over this one.
        window._handle(Listing(ROOT_FID, (Entry("x", "别的.bin", False, 4),)))
        self.assertTrue(window._searching)
        self.assertEqual(window._status_var.value, SEARCHING_STATUS)
        self.assertEqual([row[1] for row in window._tree.rows], ["报告旧.bin"])
        self.assertEqual(window._search_button.state, "normal")

        window._handle(
            SearchResults(request_id=window._search_generation,
                fid=ROOT_FID,
                query="报告",
                entries=(Entry("m", "报告.bin", False, 4, "/我的网盘/报告.bin", (("0", "我的网盘"),)),),
            )
        )
        self.assertFalse(window._searching)
        self.assertEqual([row[1] for row in window._tree.rows], ["报告.bin"])

    def test_refreshing_search_results_runs_the_same_search_again(self):
        window = self._window()
        window._search_var.set("报告")
        window.search_subdirectories()
        self._submitted(window)
        window._handle(
            SearchResults(request_id=window._search_generation,
                fid=ROOT_FID,
                query="报告",
                entries=(Entry("m", "报告.bin", False, 4, "/我的网盘/报告.bin", (("0", "我的网盘"),)),),
            )
        )

        window.refresh()
        self.assertEqual(self._submitted(window), Search(fid=ROOT_FID, query="报告", request_id=window._search_generation, recursive=(window._pending or window._search).recursive))
        self.assertEqual(window._status_var.value, SEARCHING_STATUS)
        with self.assertRaises(queue.Empty):
            self._submitted(window)

        window._handle(
            SearchResults(request_id=window._search_generation,
                fid=ROOT_FID,
                query="报告",
                entries=(Entry("m2", "报告2.bin", False, 4, "/我的网盘/报告2.bin", (("0", "我的网盘"),)),),
            )
        )
        self.assertEqual([row[1] for row in window._tree.rows], ["报告2.bin"])

    def test_search_keeps_controls_available_but_refuses_download(self):
        window = self._window()
        self._listing(window, Entry("d", "报告目录", True, None))
        with tempfile.TemporaryDirectory() as folder:
            window._output_dir = folder
            window._search_var.set("报告")
            window.search_subdirectories()
            first = self._submitted(window)
            self.assertEqual(window._search_button.state, "normal")
            self.assertEqual(window._scope_combo.state, "readonly")
            self.assertEqual(window._return_button.state, "normal")
            self.assertEqual(window._keyword_entry.state, "normal")
            self.assertEqual(window._download_button.state, "disabled")
            window.start_download()
            with self.assertRaises(queue.Empty):
                self._submitted(window)
            window.search_subdirectories()
            second = self._submitted(window)
            self.assertNotEqual(first.request_id, second.request_id)
            window.cancel_search()
            self.assertFalse(window._searching)

    def test_a_download_freezes_the_search_and_the_filters(self):
        window = self._window()
        with tempfile.TemporaryDirectory() as folder:
            window._output_dir = folder
            window._search_var.set("报告")
            window.search_subdirectories()
            self._submitted(window)
            window._handle(
                SearchResults(request_id=window._search_generation,
                    fid=ROOT_FID,
                    query="报告",
                    entries=(
                        Entry("m", "报告.bin", False, 4, "/我的网盘/报告.bin", (("0", "我的网盘"),)),
                        Entry("n", "报告2.bin", False, 4, "/我的网盘/报告2.bin", (("0", "我的网盘"),)),
                    ),
                )
            )
            self._tick(window, "m", "n")
            window.start_download()
            self.assertEqual(
                self._submitted(window),
                Download(
                    fid="m",
                    target=str(Path(folder) / "报告.bin"),
                    recursive=False,
                    overwrite=False,
                    name="报告.bin",
                ),
            )
            self.assertEqual(window._search_button.state, "disabled")
            self.assertEqual(window._keyword_entry.state, "disabled")
            self.assertEqual(window._kind_combo.state, "disabled")
            self.assertEqual(window._return_button.state, "disabled")
            self.assertEqual(window._open_folder_button.state, "disabled")

            window.search()
            window.search_subdirectories()
            window.leave_search()
            window._search_var.set("别的词")
            window._kind_var.set(KIND_DIRECTORIES)
            window._on_filter_change()
            with self.assertRaises(queue.Empty):
                self._submitted(window)
            self.assertEqual([row[1] for row in window._tree.rows], ["报告.bin", "报告2.bin"])
            self.assertIsNotNone(window._batch)
            self.assertEqual(window._batch.total, 2)

    def test_a_folder_result_opens_its_real_folder_and_returns_to_the_search_folder(self):
        window = self._window()
        window._search_var.set("报告")
        window.search_subdirectories()
        self._submitted(window)
        window._handle(
            SearchResults(request_id=window._search_generation,
                fid=ROOT_FID,
                query="报告",
                entries=(
                    Entry(
                        "d2",
                        "报告目录",
                        True,
                        None,
                        "/我的网盘/文档/报告目录",
                        (("0", "我的网盘"), ("d", "文档"), ("d2", "报告目录")),
                    ),
                ),
            )
        )
        window._tree.selection_ids = ("row0",)

        window._on_activate(None)
        self.assertEqual(
            (window._location.fid, window._location_var.value), ("d2", "/文档/报告目录")
        )
        self.assertEqual(self._submitted(window), Browse("d2"))

        window._handle(Listing("d2", (Entry("f", "x.bin", False, 4),)))
        self.assertEqual([row[1] for row in window._tree.rows], ["x.bin"])

        # 返回上级 walks the real parents of the folder that was entered, not the
        # search's own chain.
        window.go_up()
        self.assertEqual((window._location.fid, window._location_var.value), ("d", "/文档"))
        self.assertEqual(self._submitted(window), Browse("d"))

        # 返回目录 ends the search and shows the folder it started from.
        window.leave_search()
        self.assertEqual((window._location.fid, window._location_var.value), (ROOT_FID, "/"))
        self.assertEqual(self._submitted(window), Browse(ROOT_FID))
        self.assertEqual(window._return_button.state, "disabled")
        window.refresh()
        self.assertEqual(self._submitted(window), Browse(ROOT_FID))

    def test_two_results_with_one_name_download_to_the_same_target_in_order(self):
        window = self._window()
        with tempfile.TemporaryDirectory() as folder:
            window._output_dir = folder
            window._search_var.set("报告")
            window.search_subdirectories()
            self._submitted(window)
            window._handle(
                SearchResults(request_id=window._search_generation,
                    fid=ROOT_FID,
                    query="报告",
                    entries=(
                        Entry("m1", "报告.bin", False, 4, "/我的网盘/A/报告.bin", (("0", "我的网盘"), ("a", "A"))),
                        Entry("m2", "报告.bin", False, 4, "/我的网盘/B/报告.bin", (("0", "我的网盘"), ("b", "B"))),
                    ),
                )
            )
            self._tick(window, "m2", "m1")

            window.start_download()
            # The name stays the name the library knows: both matches are asked
            # for the same local target, in table order, and the library's
            # existing refusal decides the second one.
            self.assertEqual(
                self._submitted(window),
                Download(
                    fid="m1",
                    target=str(Path(folder) / "报告.bin"),
                    recursive=False,
                    overwrite=False,
                    name="报告.bin",
                ),
            )
            window._handle(Failed(DOWNLOAD_ACTION, "下载失败：目标文件已存在；使用 --overwrite 显式覆盖"))
            self.assertEqual(
                self._submitted(window),
                Download(
                    fid="m2",
                    target=str(Path(folder) / "报告.bin"),
                    recursive=False,
                    overwrite=False,
                    name="报告.bin",
                ),
            )
            window._handle(Finished(DOWNLOAD_ACTION, "已下载 报告.bin：4 B"))
            self.assertFalse(window._downloading)
            self.assertEqual(window._status_var.value, "下载结束：共 2 项，成功 1 项，失败 1 项")


class FilterTests(WindowCase):
    """The keyword box and the type box re-render what the window already holds."""

    ROWS = (
        Entry("d", "文档", True, None),
        Entry("f1", "报告.PDF", False, 4),
        Entry("f2", "Report.txt", False, 8),
    )

    def _labels(self, window: DownloaderWindow) -> list[str]:
        return [row[1] for row in window._tree.rows]

    def test_the_keyword_filters_case_insensitively_without_a_request(self):
        window = self._window()
        self._listing(window, *self.ROWS)
        self.assertEqual(self._labels(window), ["文档", "报告.PDF", "Report.txt"])

        window._search_var.set("report")
        self.assertEqual(self._labels(window), ["Report.txt"])
        self.assertEqual(window._count_var.value, match_text(3, 1))
        with self.assertRaises(queue.Empty):
            self._submitted(window)

    def test_the_type_box_and_the_keyword_combine_and_relax_again(self):
        window = self._window()
        self._listing(window, *self.ROWS)

        window._search_var.set("报告")
        self.assertEqual(self._labels(window), ["报告.PDF"])
        window._kind_var.set(KIND_DIRECTORIES)
        window._on_filter_change()
        self.assertEqual(self._labels(window), [])
        self.assertEqual(window._count_var.value, match_text(3, 0))

        window._kind_var.set(KIND_FILES)
        window._on_filter_change()
        self.assertEqual(self._labels(window), ["报告.PDF"])

        window._search_var.set("")
        self.assertEqual(self._labels(window), ["报告.PDF", "Report.txt"])

        window._kind_var.set(KIND_ALL)
        window._on_filter_change()
        self.assertEqual(self._labels(window), ["文档", "报告.PDF", "Report.txt"])
        self.assertEqual(window._count_var.value, match_text(3, 3))

    def test_the_filters_apply_to_search_results_and_restore_all_of_them(self):
        window = self._window()
        window._search_var.set("报告")
        window.search_subdirectories()
        self._submitted(window)
        window._handle(
            SearchResults(request_id=window._search_generation,
                fid=ROOT_FID,
                query="报告",
                entries=(
                    Entry("m", "报告.bin", False, 4, "/我的网盘/报告.bin", (("0", "我的网盘"),)),
                    Entry("d", "报告目录", True, None, "/我的网盘/报告目录", (("0", "我的网盘"), ("d", "报告目录"))),
                ),
            )
        )
        self.assertEqual(self._labels(window), ["报告.bin", "报告目录"])

        window._kind_var.set(KIND_DIRECTORIES)
        window._on_filter_change()
        self.assertEqual(self._labels(window), ["报告目录"])
        self.assertEqual(window._count_var.value, match_text(2, 1))

        window._kind_var.set(KIND_ALL)
        window._on_filter_change()
        self.assertEqual(self._labels(window), ["报告.bin", "报告目录"])
        with self.assertRaises(queue.Empty):
            self._submitted(window)

    def test_following_a_folder_drops_the_local_filters_and_a_refresh_keeps_them(self):
        window = self._window()
        self._listing(
            window, Entry("d", "报告目录", True, None), Entry("f", "报告.bin", False, 4)
        )

        window._search_var.set("报告")
        window._kind_var.set(KIND_DIRECTORIES)
        window._on_filter_change()
        self.assertEqual([row[1] for row in window._tree.rows], ["报告目录"])

        # Refreshing the folder keeps what the user filtered by.
        window.refresh()
        self.assertEqual(self._submitted(window), Browse(ROOT_FID))
        self.assertEqual((window._keyword(), window._kind()), ("报告", KIND_CODE[KIND_DIRECTORIES]))

        # Entering a folder drops both filters: the folder opened shows in full.
        window._tree.selection_ids = ("row0",)
        window._on_activate(None)
        self.assertEqual(self._submitted(window), Browse("d"))
        self.assertEqual((window._keyword(), window._kind()), ("", KIND_CODE[KIND_ALL]))
        window._handle(
            Listing("d", (Entry("x", "x.bin", False, 4), Entry("y", "报告y.bin", False, 8)))
        )
        self.assertEqual([row[1] for row in window._tree.rows], ["x.bin", "报告y.bin"])

        # 返回上级 drops them too, and shows the parent in full.
        window._search_var.set("x")
        window.go_up()
        self.assertEqual(self._submitted(window), Browse(ROOT_FID))
        self.assertEqual((window._keyword(), window._kind()), ("", KIND_CODE[KIND_ALL]))
        self._listing(window, Entry("d2", "目录", True, None), Entry("f2", "f.bin", False, 4))
        self.assertEqual([row[1] for row in window._tree.rows], ["目录", "f.bin"])

        # 返回目录 drops them as well, so the folder the search started from is
        # shown in full again.
        window._search_var.set("f")
        window._kind_var.set(KIND_FILES)
        window._on_filter_change()
        window.search_subdirectories()
        self.assertEqual(self._submitted(window), Search(fid=ROOT_FID, query="f", request_id=window._search_generation, recursive=(window._pending or window._search).recursive))
        self._results(
            window,
            "f",
            Entry("f", "f.bin", False, 4, "/我的网盘/f.bin", (("0", "我的网盘"),)),
        )
        self.assertEqual([row[1] for row in window._tree.rows], ["f.bin"])

        window.leave_search()
        self.assertEqual(self._submitted(window), Browse(ROOT_FID))
        self.assertEqual((window._keyword(), window._kind()), ("", KIND_CODE[KIND_ALL]))
        self._listing(window, Entry("d3", "目录", True, None), Entry("f3", "f.bin", False, 4))
        self.assertEqual([row[1] for row in window._tree.rows], ["目录", "f.bin"])


class CheckboxTests(WindowCase):
    """The ``选择`` box is the selection; the highlight and the cursor are not."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)

    def _two_rows(self) -> DownloaderWindow:
        """A window that shows two files, with a save folder already chosen."""
        window = self._window()
        window._output_dir = str(self.output)
        self._listing(window, Entry("f1", "a.bin", False, 4), Entry("f2", "b.bin", False, 4))
        return window

    @staticmethod
    def _second_row() -> int:
        """A ``y`` inside the second row of the fake table."""
        return _FakeTree.ROW_HEIGHT + 1

    def test_a_plain_click_ticks_that_row_and_a_second_one_untickes_it(self):
        window = self._two_rows()
        window._tree.selection_ids = ("row1",)

        # One click, no modifier key: the box of the row under the pointer, and
        # the highlight the table already had is left alone.
        self.assertEqual(self._click(window, y=1), "break")
        self.assertEqual(self._marks(window), [CHECKED_MARK, UNCHECKED_MARK])
        self.assertEqual(window._checked, {"f1"})
        self.assertEqual(window._tree.selection_ids, ("row1",))

        self._click(window, y=1, time=2000)
        self.assertEqual(self._marks(window), [UNCHECKED_MARK, UNCHECKED_MARK])
        self.assertEqual(window._checked, set())

    def test_a_click_outside_the_box_only_highlights_and_queues_nothing(self):
        window = self._two_rows()

        # The name column keeps the table's own selection behaviour...
        self.assertIsNone(self._click(window, x=_FakeTree.CHECK_WIDTH + 5, y=1))
        self.assertEqual(window._checked, set())
        self.assertEqual(self._marks(window), [UNCHECKED_MARK, UNCHECKED_MARK])
        # ...and the row that is highlighted is not what the click takes.
        window._tree.selection_ids = ("row0",)
        window.start_download()
        self.assertEqual(window._status_var.value, "请先在列表中勾选一个文件或目录")
        with self.assertRaises(queue.Empty):
            self._submitted(window)

    def test_the_header_and_the_space_below_the_rows_tick_nothing(self):
        window = self._two_rows()
        self.assertEqual(self._click(window, y=-1), "break")
        self.assertEqual(self._click(window, y=500), "break")
        self.assertIsNone(self._click(window, x=0, y=1))
        self.assertEqual(window._checked, set())

    def test_a_click_on_the_box_leaves_the_keyboard_cursor_on_that_row(self):
        window = self._two_rows()
        window._tree.focus_id = "row0"

        self._click(window, y=self._second_row())
        self.assertEqual(window._tree.focus(), "row1")
        # The space bar now ticks the row the click was on, not the old cursor.
        window._on_space(None)
        self.assertEqual(self._marks(window), [UNCHECKED_MARK, UNCHECKED_MARK])
        self.assertEqual(window._checked, set())

    def test_the_space_bar_ticks_the_focused_row_only(self):
        window = self._two_rows()
        window._on_space(None)
        self.assertEqual(window._checked, set())

        window._tree.focus_id = "row1"
        window._on_space(None)
        self.assertEqual(self._marks(window), [UNCHECKED_MARK, CHECKED_MARK])
        self.assertEqual(window._checked, {"f2"})

        # A cursor on a row the table no longer holds ticks nothing.
        window._tree.focus_id = "row9"
        window._on_space(None)
        self.assertEqual(window._checked, {"f2"})

    def test_a_double_click_on_the_box_ticks_once_and_opens_nothing(self):
        window = self._window()
        self._listing(window, Entry("d", "目录", True, None), Entry("f", "f.bin", False, 4))

        # Tk dispatches the second press to the double-click binding instead
        # of the single-click binding.
        self._click(window, y=1, time=3000)
        window._on_activate(SimpleNamespace(x=10, y=1, time=3100))

        self.assertEqual(self._marks(window), [CHECKED_MARK, UNCHECKED_MARK])
        self.assertEqual(window._checked, {"d"})
        self.assertEqual((window._location.fid, window._location_var.value), (ROOT_FID, ""))
        with self.assertRaises(queue.Empty):
            self._submitted(window)

    def test_select_all_ticks_the_visible_rows_and_the_click_takes_those(self):
        window = self._two_rows()
        window._search_var.set("a.bin")

        window.toggle_all()
        self.assertEqual(window._checked, {"f1"})
        self.assertEqual(self._marks(window), [CHECKED_MARK])
        with self.assertRaises(queue.Empty):
            self._submitted(window)

        window._search_var.set("")
        self.assertEqual(self._marks(window), [CHECKED_MARK, UNCHECKED_MARK])
        self.assertEqual(window._count_var.value, f"{match_text(2, 2)}，已选 1 项")
        window.start_download()
        self.assertEqual(
            self._submitted(window),
            Download(fid="f1", target=str(self.output / "a.bin"), recursive=False, overwrite=False, name="a.bin"),
        )

        # A tick on a row the filter hides is not part of the click either.
        window._handle(Finished(DOWNLOAD_ACTION, "已下载 a.bin：4 B"))
        window._search_var.set("b.bin")
        window.start_download()
        self.assertEqual(window._status_var.value, "请先在列表中勾选一个文件或目录")
        with self.assertRaises(queue.Empty):
            self._submitted(window)

    def test_select_all_untickes_when_every_visible_row_is_ticked(self):
        window = self._two_rows()
        window.toggle_all()
        self.assertEqual(window._checked, {"f1", "f2"})
        self.assertEqual(self._marks(window), [CHECKED_MARK, CHECKED_MARK])

        window.toggle_all()
        self.assertEqual(window._checked, set())
        self.assertEqual(self._marks(window), [UNCHECKED_MARK, UNCHECKED_MARK])

    def test_a_tick_survives_a_filter_and_a_refresh_and_goes_with_its_row(self):
        window = self._two_rows()
        self._click(window, y=1)
        self.assertEqual(window._checked, {"f1"})

        # A filter hides the row and brings it back with its box still ticked.
        window._search_var.set("b.bin")
        self.assertEqual(self._marks(window), [UNCHECKED_MARK])
        window._search_var.set("")
        self.assertEqual(self._marks(window), [CHECKED_MARK, UNCHECKED_MARK])

        # 刷新 re-reads the folder: every row that is still there keeps its box.
        window.refresh()
        self.assertEqual(self._submitted(window), Browse(ROOT_FID))
        self._listing(window, Entry("f1", "a.bin", False, 4), Entry("f2", "b.bin", False, 4))
        self.assertEqual(self._marks(window), [CHECKED_MARK, UNCHECKED_MARK])

        # The row is gone: its fid goes with it, and the file that takes its
        # table position does not inherit the tick.
        self._listing(window, Entry("f9", "新.bin", False, 4), Entry("f2", "b.bin", False, 4))
        self.assertEqual(window._checked, set())
        self.assertEqual(self._marks(window), [UNCHECKED_MARK, UNCHECKED_MARK])
        window.start_download()
        self.assertEqual(window._status_var.value, "请先在列表中勾选一个文件或目录")
        with self.assertRaises(queue.Empty):
            self._submitted(window)

    def test_leaving_the_folder_starts_from_an_empty_tick_set(self):
        window = self._two_rows()
        self._click(window, y=1)
        self.assertEqual(window._checked, {"f1"})

        # 返回上级 at the root does not leave the folder, so the tick stays.
        window.go_up()
        self.assertEqual(window._checked, {"f1"})

        self._listing(
            window,
            Entry("d", "目录", True, None),
            Entry("f1", "a.bin", False, 4),
            Entry("f2", "b.bin", False, 4),
        )
        self.assertEqual(window._checked, {"f1"})
        window._tree.selection_ids = ("row0",)
        window._on_activate(None)
        self.assertEqual(self._submitted(window), Browse("d"))
        self.assertEqual(window._checked, set())

    def test_a_running_download_freezes_the_boxes_and_select_all(self):
        window = self._two_rows()
        self._click(window, y=1)
        window.start_download()
        self._submitted(window)

        self.assertEqual(self._click(window, y=self._second_row()), "break")
        self.assertEqual(window._on_space(None), "break")
        window.toggle_all()
        self.assertEqual(window._checked, {"f1"})
        self.assertEqual(self._marks(window), [CHECKED_MARK, UNCHECKED_MARK])
        self.assertEqual(window._select_all_button.state, "disabled")


class OpenFolderTests(WindowCase):
    """``打开保存目录``: only on a click, only a folder that still exists."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)

    def _window(self, **options: Any) -> DownloaderWindow:
        self.opened: list[str] = []
        options.setdefault("folder_opener", self.opened.append)
        return super()._window(**options)

    def test_a_valid_folder_is_opened_without_touching_the_status(self):
        window = self._window()
        window._sync_controls()
        self.assertEqual(window._open_folder_button.state, "disabled")

        window._output_dir = str(self.output)
        window._sync_controls()
        self.assertEqual(window._open_folder_button.state, "normal")

        window.open_output_folder()
        self.assertEqual(self.opened, [str(self.output)])
        self.assertEqual(window._status_var.value, "")

    def test_it_is_unavailable_for_a_folder_that_disappeared_or_was_never_chosen(self):
        window = self._window()
        window.open_output_folder()
        self.assertEqual(self.opened, [])

        window._output_dir = str(self.output / "missing")
        window._sync_controls()
        window.open_output_folder()
        self.assertEqual(self.opened, [])
        self.assertEqual(
            window._status_var.value,
            describe_error(LocalIOError("本地输出目录不存在或不是目录"), OPEN_FOLDER_ACTION),
        )

    def test_a_download_freezes_it_until_the_batch_is_over(self):
        window = self._window()
        window._output_dir = str(self.output)
        window._sync_controls()
        window._downloading = True
        window._sync_controls()
        self.assertEqual(window._open_folder_button.state, "disabled")

        window.open_output_folder()
        self.assertEqual(self.opened, [])

        window._downloading = False
        window._sync_controls()
        self.assertEqual(window._open_folder_button.state, "normal")

    def test_a_failing_opener_becomes_one_safe_line(self):
        window = self._window()
        window._output_dir = str(self.output)

        def boom(_path: str) -> None:
            raise OSError(123, "The system cannot find the drive specified", str(self.output))

        window._folder_opener = boom
        window.open_output_folder()
        self.assertEqual(
            window._status_var.value,
            describe_error(OSError(123, "x"), OPEN_FOLDER_ACTION),
        )
        self.assertNotIn(str(self.output), window._status_var.value)

        def crash(_path: str) -> None:
            raise RuntimeError("boom")

        window._folder_opener = crash
        window.open_output_folder()
        self.assertEqual(window._status_var.value, f"{OPEN_FOLDER_ACTION}失败：发生未预期的错误")


class StartupTests(WindowCase):
    def test_an_existing_login_browses_the_root(self):
        window = self._window()
        window._initialise()
        self.assertEqual(window._status_var.value, "正在读取目录……")
        self.assertEqual(self._submitted(window), Browse(ROOT_FID))
        self.assertEqual(window._login_button.state, "normal")

    def test_no_credential_shows_the_login_hint_and_asks_for_nothing(self):
        window = self._window(cookie=_no_cookie)
        window._initialise()
        self.assertEqual(window._status_var.value, LOGIN_HINT)
        with self.assertRaises(queue.Empty):
            self._submitted(window)


class LoginTests(WindowCase):
    def test_old_profile_results_are_ignored_and_failure_preserves_download_state(self):
        window = self._window()
        window._profile_generation = 2
        window._nickname_var.set("读取昵称…")
        window._handle(AccountProfile(1, "旧账号"))
        self.assertEqual(window._nickname_var.get(), "读取昵称…")
        window._handle(AccountProfile(2, "新账号"))
        self.assertEqual(window._nickname_var.get(), "新账号")
        window._downloading = True
        window._status_var.set("正在下载")
        window._handle(AccountProfile(2, error="昵称读取失败"))
        self.assertEqual(window._nickname_var.get(), "昵称暂不可用")
        self.assertEqual(window._status_var.get(), "正在下载")
        self.assertTrue(window._downloading)
        self.assertIn("昵称读取失败", window._log.content)

    def test_a_successful_login_browses_the_folder_that_is_shown(self):
        login = _LoginWindow()
        window = self._window(login_window_factory=lambda root: login)
        window._initialise()
        self._submitted(window)

        window.login()
        self.assertEqual(login.runs, 1)
        self.assertEqual(window._status_var.value, "正在读取目录……")
        self.assertEqual(self._submitted(window), Browse(ROOT_FID))
        self.assertFalse(window._logging_in)
        self.assertEqual(window._login_button.state, "normal")

    def test_cancelling_and_failing_keep_the_window_usable(self):
        login = _LoginWindow(LoginCancelled("登录已取消"))
        window = self._window(login_window_factory=lambda root: login)

        window.login()
        self.assertEqual(window._status_var.value, "已取消扫码登录")
        self.assertFalse(window._logging_in)
        with self.assertRaises(queue.Empty):
            self._submitted(window)

        login.error = RemoteError("上游连接失败")
        window.login()
        self.assertEqual(window._status_var.value, "扫码登录失败：上游连接失败")
        self.assertEqual(window._login_button.state, "normal")

        login.error = RuntimeError("boom")
        window.login()
        self.assertEqual(window._status_var.value, "扫码登录失败：发生未预期的错误")

    def test_the_login_entry_defaults_to_the_shared_qr_window(self):
        window = DownloaderWindow(cookie=lambda: "sid=test", backend_factory=lambda loader: _Backend())
        self.addCleanup(window._worker.stop)
        self.assertIs(window._login_window_factory, default_login_window)


@unittest.skipUnless(_TK, "Tkinter with a display is required")
class WindowSmokeTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict("os.environ", {"QUARK_CONFIG_DIR": directory.name})
        environment.start()
        self.addCleanup(environment.stop)

    def test_handled_login_errors_release_tk_before_background_collection(self):
        import subprocess
        import sys
        import textwrap

        script = textwrap.dedent('''
            import gc, threading, time, sys
            from quark_cli.downloader.app import DownloaderWindow, _tkinter
            from quark_cli.auth import LoginCancelled
            from quark_cli.errors import LocalIOError
            class Backend:
                def account_nickname(self): return 'test'
            class Login:
                def __init__(self, parent):
                    self.parent = parent
                    self.error = (LoginCancelled if sys.argv[1] == 'cancel' else LocalIOError)('offline login failure')
                def run(self):
                    try: raise ValueError('inner failure')
                    except ValueError as error: raise self.error from error
            def exercise():
                tk = _tkinter(); root = tk.Tk()
                window = DownloaderWindow(cookie=lambda:'sid=offline', backend_factory=lambda c:Backend(),
                                          root_factory=lambda:root, login_window_factory=Login)
                window._root = root; window._build(tk); window.login(); time.sleep(.03); window.close()
            exercise()
            thread = threading.Thread(target=gc.collect); thread.start(); thread.join()
            print('released')
        ''')
        for kind in ("cancel", "failure"):
            with self.subTest(kind=kind):
                result = subprocess.run([sys.executable, "-c", script, kind], capture_output=True,
                                        text=True, encoding="utf-8", timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("released", result.stdout)

    def test_the_window_browses_the_root_and_closes(self):
        rows = [Entry("d", "目录", True, None), Entry("f", "f.bin", False, 4)]
        root = _tk_root()
        window = DownloaderWindow(
            cookie=lambda: "sid=test",
            backend_factory=lambda loader: _Backend(rows),
            root_factory=lambda: root,
        )
        observed: dict = {}
        deadline = time.monotonic() + 10

        def snapshot_and_close() -> None:
            observed["title"] = root.title()
            observed["status"] = window._status_var.get()
            observed["rows"] = [window._tree.item(iid, "text") for iid in window._tree.get_children()]
            window.close()

        def watch() -> None:
            if window._tree.get_children() or time.monotonic() > deadline:
                snapshot_and_close()
                return
            root.after(50, watch)

        root.after(20, watch)
        window.run()

        self.assertEqual(observed.get("rows"), ["目录", "f.bin"])
        self.assertEqual(observed.get("title"), WINDOW_TITLE)
        self.assertEqual(observed.get("status"), IDLE_STATUS)

    def test_the_window_without_a_credential_shows_the_login_hint(self):
        root = _tk_root()
        window = DownloaderWindow(
            cookie=_no_cookie,
            backend_factory=lambda loader: _Backend(),
            root_factory=lambda: root,
        )
        observed: dict = {}

        def snapshot_and_close() -> None:
            observed["status"] = window._status_var.get()
            observed["rows"] = list(window._tree.get_children())
            observed["login_state"] = str(window._login_button["state"])
            window.close()

        root.after(100, snapshot_and_close)
        window.run()

        self.assertEqual(observed.get("status"), LOGIN_HINT)
        self.assertEqual(observed.get("rows"), [])
        self.assertEqual(observed.get("login_state"), "normal")

    def test_login_returns_when_the_qr_child_ends_and_this_window_survives(self):
        import tkinter

        class _FinishedScan:
            """The scan completes slowly enough that the modal loop is running."""

            def __init__(self, cancel: Any) -> None:
                self._cancel = cancel

            def run(self, on_qr_ready=None) -> None:
                time.sleep(0.3)

        class _ClosedScan:
            """The scan blocks until the user closes the QR window."""

            def __init__(self, cancel: Any) -> None:
                self._cancel = cancel

            def run(self, on_qr_ready=None) -> None:
                if not self._cancel.wait(15):
                    raise AssertionError("the QR window was never closed")
                raise LoginCancelled("登录已取消")

        cases = (
            ("confirmed", _FinishedScan, None, "正在读取目录……"),
            ("closed", _ClosedScan, 250, "已取消扫码登录"),
        )
        for label, scan, close_after, expected in cases:
            with self.subTest(scan=label):
                children: list = []

                def make_login(parent: Any) -> QrLoginWindow:
                    widget = tkinter.Toplevel(parent)
                    children.append(widget)
                    login = QrLoginWindow(root_factory=lambda: widget, login_factory=scan)
                    if close_after is not None:
                        parent.after(close_after, login.close)
                    return login

                root = _tk_root()
                window = DownloaderWindow(
                    cookie=lambda: "sid=test",
                    backend_factory=lambda loader: _Backend([Entry("f", "f.bin", False, 4)]),
                    root_factory=lambda: root,
                    login_window_factory=make_login,
                )
                observed: dict = {}

                def start_login() -> None:
                    started = time.monotonic()
                    window.login()
                    observed["elapsed"] = time.monotonic() - started
                    observed["alive"] = _alive(root)
                    observed["child_gone"] = not _alive(children[0])
                    observed["status"] = (
                        window._status_var.get() if window._status_var is not None else None
                    )
                    window.close()

                def watchdog() -> None:
                    # Without the nested loop, ``login`` would sit in the child's
                    # ``mainloop`` until this window is destroyed: fail, not hang.
                    observed["timed_out"] = True
                    window.close()

                root.after(50, start_login)
                root.after(8000, watchdog)
                window.run()

                self.assertFalse(observed.get("timed_out"), "login never returned while this window stayed open")
                self.assertLess(observed.get("elapsed", 99.0), 5.0)
                self.assertTrue(observed.get("alive"))
                self.assertTrue(observed.get("child_gone"))
                self.assertEqual(observed.get("status"), expected)

    def test_close_during_a_download_is_refused_until_it_finishes(self):
        gate = threading.Event()

        class _SlowDownloadBackend(_Backend):
            def download(self, request: Download, progress: Any, should_stop: Any = None) -> dict:
                if not gate.wait(15):
                    raise AssertionError("the test never released the download")
                return {"fid": request.fid, "path": request.target, "size": 4}

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = _tk_root()
        window = DownloaderWindow(
            cookie=lambda: "sid=test",
            backend_factory=lambda loader: _SlowDownloadBackend([Entry("f", "f.bin", False, 4)]),
            root_factory=lambda: root,
        )
        window._output_dir = temporary.name
        observed: dict = {}

        def begin() -> None:
            children = window._tree.get_children()
            if not children:
                root.after(50, begin)
                return
            root.update_idletasks()
            box = window._tree.bbox(children[0], "check")
            if not box:
                root.after(50, begin)
                return
            window._on_click(SimpleNamespace(x=box[0] + box[2] // 2, y=box[1] + box[3] // 2))
            self.assertTrue(window._checked)
            window.start_download()
            self.assertTrue(window._downloading)
            window.close()
            observed["alive"] = _alive(root)
            observed["status"] = window._status_var.get()
            gate.set()
            root.after(50, finish)

        def finish() -> None:
            if window._downloading:
                root.after(50, finish)
                return
            observed["done_status"] = window._status_var.get()
            root.after_cancel(watchdog)
            window.close()

        root.after(50, begin)
        watchdog = root.after(5000, root.quit)
        window.run()

        self.assertTrue(observed.get("alive"), "the window closed under a running download")
        self.assertEqual(observed.get("status"), CLOSE_BUSY_STATUS)
        # The item's own line goes to the per-item log; the status line closes
        # the click, so it reports the batch, not the single item.
        self.assertEqual(observed.get("done_status"), "全部下载完成：共 1 项")

    def test_the_login_window_is_a_modal_child_of_the_downloader_window(self):
        import tkinter

        root = _tk_root()
        try:
            login = default_login_window(root)
            self.assertIsInstance(login, QrLoginWindow)
            child = login._root_factory()
            self.assertIsInstance(child, tkinter.Toplevel)
            self.assertIs(child.master, root)
            child.destroy()
        finally:
            root.destroy()

    def test_the_window_offers_a_multi_selection_and_a_stop_control(self):
        root = _tk_root()
        window = DownloaderWindow(
            cookie=lambda: "sid=test",
            backend_factory=lambda loader: _Backend(),
            root_factory=lambda: root,
        )
        observed: dict = {}

        def snapshot_and_close() -> None:
            if window._profile_pending:
                root.after(50, snapshot_and_close)
                return
            observed["selectmode"] = str(window._tree.cget("selectmode"))
            observed["check_heading"] = window._tree.heading("check", "text")
            observed["select_all"] = window._select_all_button.cget("text")
            observed["download"] = window._download_button.cget("text")
            observed["share"] = window._share_button.cget("text")
            observed["drive"] = window._drive_button.cget("text")
            observed["passcode_mask"] = window._passcode_entry.cget("show")
            observed["connections"] = window._connections_var.get()
            observed["connections_max"] = window._connections_input.cget("to")
            observed["nickname"] = window._nickname_var.get()
            observed["stop_text"] = str(window._stop_button.cget("text"))
            observed["stop_state"] = str(window._stop_button.cget("state"))
            window.close()

        root.after(100, snapshot_and_close)
        window.run()

        self.assertEqual(observed.get("selectmode"), "extended")
        self.assertEqual(observed.get("check_heading"), "选择")
        self.assertEqual(observed.get("select_all"), SELECT_ALL_TEXT)
        self.assertEqual(observed.get("download"), DOWNLOAD_BUTTON_TEXT)
        self.assertEqual(observed.get("share"), "打开分享")
        self.assertEqual(observed.get("drive"), "我的网盘")
        self.assertEqual(observed.get("passcode_mask"), "*")
        self.assertEqual(observed.get("connections"), "4")
        self.assertEqual(int(observed.get("connections_max")), 16)
        self.assertIn(observed.get("nickname"), ("测试账号", "读取昵称…"))
        self.assertEqual(observed.get("stop_text"), "停止")
        self.assertEqual(observed.get("stop_state"), "disabled")

    def test_the_keyword_box_filters_the_real_table_and_the_save_folder_opens(self):
        import tempfile

        rows = [Entry("f1", "报告.bin", False, 4), Entry("f2", "其他.bin", False, 8)]
        opened: list[str] = []
        root = _tk_root()
        window = DownloaderWindow(
            cookie=lambda: "sid=test",
            backend_factory=lambda loader: _Backend(rows),
            root_factory=lambda: root,
            folder_opener=opened.append,
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        observed: dict = {}

        def snapshot_and_close() -> None:
            children = window._tree.get_children()
            if not children:
                root.after(50, snapshot_and_close)
                return
            observed["path_heading"] = str(window._tree.heading("path", "text"))
            observed["search_text"] = str(window._search_button.cget("text"))
            observed["scopes"] = list(window._scope_combo.cget("values"))
            observed["return_visible"] = bool(window._return_button.winfo_manager())
            observed["return_text"] = str(window._return_button.cget("text"))
            observed["kinds"] = list(window._kind_combo.cget("values"))
            observed["open_state"] = str(window._open_folder_button.cget("state"))

            # Typing in the keyword box filters the rows already in the table.
            window._keyword_entry.insert(0, "报告")
            observed["filtered"] = [window._tree.item(iid, "text") for iid in window._tree.get_children()]
            observed["counts"] = window._count_var.get()

            window._output_dir = temporary.name
            window._sync_controls()
            observed["open_state_after"] = str(window._open_folder_button.cget("state"))
            window.open_output_folder()
            window.close()

        root.after(50, snapshot_and_close)
        window.run()

        self.assertEqual(observed.get("path_heading"), "路径")
        self.assertEqual(observed.get("search_text"), "搜索")
        self.assertEqual(observed.get("scopes"), ["全盘", "当前目录及子目录"])
        self.assertFalse(observed.get("return_visible"))
        self.assertEqual(observed.get("return_text"), "返回目录")
        self.assertEqual(observed.get("kinds"), [KIND_ALL, KIND_FILES, KIND_DIRECTORIES])
        self.assertEqual(observed.get("open_state"), "disabled")
        self.assertEqual(observed.get("filtered"), ["报告.bin"])
        self.assertEqual(observed.get("counts"), match_text(2, 1))
        self.assertEqual(observed.get("open_state_after"), "normal")
        self.assertEqual(opened, [temporary.name])


if __name__ == "__main__":
    unittest.main()
