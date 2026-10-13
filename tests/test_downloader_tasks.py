"""Task results and manual retries through the downloader's existing queue."""

import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quark_cli.downloader.app import DownloaderWindow, _tkinter
from quark_cli.downloader.core import DOWNLOAD_ACTION, DownloadTask, Entry
from quark_cli.downloader.share import ShareSource
from quark_cli.downloader.worker import Download, DriveBackend, Failed, Finished, Progress, Stopped
from tests.test_downloader_share import COOKIE, PASSCODE, SHARE_URL, ShareDownloadFixture
from tests.test_downloader_window import (
    WindowCase, _Backend, _FakeButton, _FakeVar, _LoginWindow, _TK, _tk_root,
)


class TaskTree:
    def __init__(self):
        self.rows = {}
        self.selected = ()
        self.focused = ""

    def get_children(self):
        return tuple(self.rows)

    def delete(self, *items):
        for item in items:
            del self.rows[item]
        self.selected = ()

    def insert(self, parent, index, *, iid, text, values):
        self.rows[iid] = (text, list(values))

    def selection(self):
        return self.selected

    def selection_set(self, item):
        self.selected = (item,)

    def identify_row(self, y):
        items = list(self.rows)
        return items[y // 20] if 0 <= y // 20 < len(items) else ""

    def focus(self, item):
        self.focused = item

    def set(self, iid, column, value):
        self.rows[iid][1][0 if column == "status" else 1] = value


class TaskTests(WindowCase):
    def _task_window(self, **options):
        window = self._window(**options)
        window._task_tree = TaskTree()
        window._task_summary_var = _FakeVar()
        window._task_detail_var = _FakeVar()
        window._retry_button, window._continue_button = _FakeButton(), _FakeButton()
        window._connections_var = _FakeVar("4")
        return window

    @staticmethod
    def _requests(window):
        items = []
        while True:
            try:
                items.append(window._worker._requests.get_nowait())
            except queue.Empty:
                return items

    def _begin(self, window, folder, *names):
        window._output_dir = str(folder)
        window._adopt_rows([Entry(str(i), name, False, 4) for i, name in enumerate(names)])
        window._checked = {entry.fid for entry in window._rows}
        window.start_download()

    def test_retry_then_continue_preserves_successes_original_target_and_order(self):
        with tempfile.TemporaryDirectory() as original, tempfile.TemporaryDirectory() as other:
            window = self._task_window()
            self._begin(window, original, "a.bin", "b.bin", "c.bin", "d.bin")
            self.assertEqual(self._requests(window)[0].fid, "0")
            window._handle(Failed(DOWNLOAD_ACTION, "下载失败：网络断开"))
            self.assertEqual(self._requests(window)[0].fid, "1")
            window._handle(Finished(DOWNLOAD_ACTION, "已下载 b.bin"))
            self.assertEqual(self._requests(window)[0].fid, "2")
            window.stop_download()
            window._handle(Stopped(DOWNLOAD_ACTION, "安全检查点已保留"))
            self.assertEqual([t.state for t in window._tasks], ["failed", "done", "stopped", "skipped"])
            self.assertEqual(window._task_tree.rows["3"][1][0], "未开始")
            window._output_dir = other
            window._connections_var.set("8")
            window._adopt_rows([Entry("unrelated", "other.bin", False)])
            window.retry_failed()
            request = self._requests(window)[0]
            self.assertEqual((request.fid, request.target, request.connections),
                             ("0", str(Path(original) / "a.bin"), 8))
            self.assertFalse(request.overwrite)
            window._handle(Finished(DOWNLOAD_ACTION, "已下载 a.bin"))
            window.continue_unfinished()
            self.assertEqual(self._requests(window)[0].fid, "2")
            window._handle(Finished(DOWNLOAD_ACTION, "已下载 c.bin"))
            self.assertEqual(self._requests(window)[0].fid, "3")
            window._handle(Finished(DOWNLOAD_ACTION, "已下载 d.bin"))
            self.assertEqual([t.state for t in window._tasks], ["done"] * 4)
            self.assertEqual(window._task_summary_var.get(), "共 4 项 · 已完成 4 · 失败 0")
            self.assertEqual(window._retry_button.state, "disabled")
            self.assertEqual(window._continue_button.state, "disabled")
            window.continue_unfinished()
            self.assertEqual(self._requests(window), [])

    def test_busy_requests_do_not_duplicate_work_and_progress_keeps_selected_error_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            window = self._task_window()
            self._begin(window, folder, "a.bin", "b.bin")
            self._requests(window)
            window._task_tree.selection_set("0")
            window._handle(Failed(DOWNLOAD_ACTION, "下载失败：连接中断，请检查网络"))
            self._requests(window)
            window._handle(Progress("b.bin：2 B / 4 B", 0.5))
            self.assertIn("连接中断", window._task_detail_var.get())
            self.assertEqual(window._task_tree.rows["1"][1][1], "b.bin：2 B / 4 B")
            window.retry_failed()
            window.continue_unfinished()
            self.assertEqual(self._requests(window), [])
            self.assertEqual(window._retry_button.state, "disabled")

    def test_stop_racing_with_success_only_continues_the_unstarted_item(self):
        with tempfile.TemporaryDirectory() as folder:
            window = self._task_window()
            self._begin(window, folder, "a.bin", "b.bin")
            self._requests(window)
            window.stop_download()
            window._handle(Finished(DOWNLOAD_ACTION, "已下载 a.bin"))
            window.continue_unfinished()
            self.assertEqual(self._requests(window)[0].fid, "1")
            self.assertEqual(window._tasks[0].state, "done")

    def test_deleted_original_directory_does_not_redirect_retry_or_erase_failure(self):
        window = self._task_window()
        with tempfile.TemporaryDirectory() as folder:
            self._begin(window, folder, "a.bin")
            self._requests(window)
            window._handle(Failed(DOWNLOAD_ACTION, "下载失败：网络断开"))
        with tempfile.TemporaryDirectory() as replacement:
            window._output_dir = replacement
            window.retry_failed()
        self.assertEqual(self._requests(window), [])
        self.assertEqual(window._tasks[0].state, "failed")
        self.assertIn("输出目录不存在", window._status_var.get())

    def test_planning_errors_are_results_and_new_batch_replaces_old_records(self):
        with tempfile.TemporaryDirectory() as folder:
            window = self._task_window()
            self._begin(window, folder, "unsafe?.bin")
            self.assertEqual(self._requests(window), [])
            self.assertEqual(window._tasks[0].state, "failed")
            window.retry_failed()
            self.assertEqual(self._requests(window), [])
            self._begin(window, folder, "good.bin")
            self.assertEqual([t.entry.name for t in window._tasks], ["good.bin"])
            self.assertEqual(self._requests(window)[0].name, "good.bin")

    def test_login_invalidation_keeps_results_but_refuses_old_account_fids(self):
        with tempfile.TemporaryDirectory() as folder:
            window = self._task_window(login_window_factory=lambda parent: _LoginWindow())
            self._begin(window, folder, "a.bin")
            self._requests(window)
            window._handle(Failed(DOWNLOAD_ACTION, "下载失败：登录失效"))
            window.login()
            window.retry_failed()
            self.assertFalse(any(isinstance(request, Download) for request in self._requests(window)))
            self.assertEqual(window._tasks[0].state, "failed")
            self.assertIn("重新勾选", window._status_var.get())
            self.assertEqual(window._retry_button.state, "disabled")

    def test_single_retry_leaves_other_failures_stops_and_successes_untouched(self):
        with tempfile.TemporaryDirectory() as original, tempfile.TemporaryDirectory() as other:
            window = self._task_window()
            self._begin(window, original, "a.bin", "b.bin", "c.bin", "d.bin")
            self._requests(window)
            window._handle(Failed(DOWNLOAD_ACTION, "failed"))
            self._requests(window)
            window._handle(Finished(DOWNLOAD_ACTION, "done"))
            self._requests(window)
            window.stop_download()
            window._handle(Stopped(DOWNLOAD_ACTION, "stopped"))
            window._output_dir = other
            window._task_tree.selection_set("3")
            window.retry_task()
            requests = self._requests(window)
            self.assertEqual(len(requests), 1)
            self.assertEqual((requests[0].fid, requests[0].target, requests[0].overwrite),
                             ("3", str(Path(original) / "d.bin"), False))
            window._handle(Finished(DOWNLOAD_ACTION, "done"))
            self.assertEqual([task.state for task in window._tasks], ["failed", "done", "stopped", "done"])
            self.assertEqual(self._requests(window), [])

    def test_single_retry_refuses_no_selection_completed_task_busy_and_new_login(self):
        with tempfile.TemporaryDirectory() as folder:
            window = self._task_window()
            self._begin(window, folder, "a.bin")
            self._requests(window)
            window.retry_task()
            self.assertEqual(self._requests(window), [])
            window._handle(Finished(DOWNLOAD_ACTION, "done"))
            window.retry_task()
            self.assertIn("选中一个未完成", window._status_var.get())
            window._task_tree.selection_set("0")
            window.retry_task()
            self.assertEqual(self._requests(window), [])
            window._tasks[0].state = "failed"
            window._tasks_valid = False
            window.retry_task()
            self.assertIn("登录已变化", window._status_var.get())
            self.assertEqual(self._requests(window), [])

    def test_task_menu_targets_clicked_row_and_handles_empty_area_and_busy_state(self):
        window = self._task_window()
        window._task_menu = SimpleNamespace(entryconfigure=lambda index, **kw: states.update({index: kw["state"]}),
                                            tk_popup=lambda x, y: popped.append((x, y)),
                                            grab_release=lambda: released.append(True))
        states, popped, released = {}, [], []
        window._task_output_dir = "C:/out"
        window._tasks = [DownloadTask(Entry("done", "done.bin", False), "done"),
                         DownloadTask(Entry("failed", "failed.bin", False), "failed")]
        window._render_tasks()
        event = SimpleNamespace(y=21, x_root=120, y_root=220)
        window._on_task_menu(event)
        self.assertEqual(window._task_tree.selection(), ("1",))
        self.assertEqual(window._task_tree.focused, "1")
        self.assertEqual(states, {0: "normal", 1: "normal"})
        self.assertEqual((popped, released), ([(120, 220)], [True]))
        event.y = 1
        window._on_task_menu(event)
        self.assertEqual(states[0], "disabled")
        event.y = 1000
        window._on_task_menu(event)
        self.assertEqual(len(popped), 2)
        event.y = 21
        for flag in ("_downloading", "_logging_in", "_searching", "_reading_share"):
            setattr(window, flag, True)
            window._on_task_menu(event)
            window.retry_task()
            setattr(window, flag, False)
        self.assertEqual(len(popped), 2)
        self.assertEqual(self._requests(window), [])
        window._tasks_valid = False
        window._on_task_menu(event)
        self.assertEqual(states, {0: "disabled", 1: "normal"})

    def test_open_task_folder_uses_original_location_and_completed_directory(self):
        opened = []
        window = self._task_window(folder_opener=opened.append)
        with tempfile.TemporaryDirectory() as original, tempfile.TemporaryDirectory() as other:
            window._output_dir = other
            window._task_output_dir = original
            target = Path(original) / "folder"
            target.mkdir()
            window._tasks = [DownloadTask(Entry("file", "file.bin", False), "done"),
                             DownloadTask(Entry("folder", "folder", True), "done")]
            window._render_tasks()
            window._task_tree.selection_set("0")
            window.open_task_folder()
            window._task_tree.selection_set("1")
            window.open_task_folder()
            window._tasks[1].state = "failed"
            window.open_task_folder()
            self.assertEqual(opened, [original, str(target), original])
        window.open_task_folder()
        self.assertIn("打开保存目录失败", window._status_var.get())
        self.assertEqual(len(opened), 3)

    def test_open_task_folder_refuses_busy_and_reports_opener_failure_without_raw_error(self):
        with tempfile.TemporaryDirectory() as folder:
            opener = unittest.mock.Mock(side_effect=RuntimeError("private-cookie"))
            window = self._task_window(folder_opener=opener)
            window._task_output_dir = folder
            window._tasks = [DownloadTask(Entry("file", "file.bin", False), "done")]
            window._render_tasks()
            window.open_task_folder()
            opener.assert_not_called()
            window._task_tree.selection_set("0")
            window._downloading = True
            window.open_task_folder()
            opener.assert_not_called()
            window._downloading = False
            window.open_task_folder()
            opener.assert_called_once_with(folder)
            self.assertNotIn("private-cookie", window._status_var.get())

    def test_share_retry_uses_original_source_and_cleanup_on_both_attempts(self):
        fixture = ShareDownloadFixture()
        fixture.save_error = True
        original = ShareSource(SHARE_URL, PASSCODE)
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"QUARK_CONFIG_DIR": folder}):
            backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
            window = self._task_window(cookie=lambda: COOKIE, backend_factory=lambda loader: backend)
            window._share_source = original
            window._share_trail = ["/"]
            window._output_dir = folder
            window._adopt_rows([Entry("share-note", "报告[1].txt", False, len(fixture.payload))])
            window._checked = {"share-note"}
            window.start_download()
            request = self._requests(window)[0]
            window._worker._download(request)
            while not window._worker.events.empty():
                window._handle(window._worker.events.get_nowait())
            self.assertEqual(window._tasks[0].state, "failed")
            self.assertEqual(fixture.account, [])
            self.assertEqual(fixture.recycle, [{"record_id": "keep-1", "fid": "someone-else"}])
            fixture.save_error = False
            window._share_source = ShareSource("https://pan.quark.cn/s/another", "different")
            window._share_trail = ["/another-folder"]
            window._task_tree.selection_set("0")
            window.retry_task()
            request = self._requests(window)[0]
            self.assertEqual((request.share_source, request.share_path), (original, "/"))
            window._worker._download(request)
            while not window._worker.events.empty():
                window._handle(window._worker.events.get_nowait())
            self.assertEqual(window._tasks[0].state, "done")
            self.assertEqual(Path(request.target).read_bytes(), fixture.payload)
            self.assertEqual(fixture.account, [])
            self.assertEqual(fixture.recycle, [{"record_id": "keep-1", "fid": "someone-else"}])
            self.assertEqual(len(fixture.bodies("/file/delete")), 2)
            window._task_tree.selection_set("0")
            window._show_task_detail()
            self.assertNotIn(PASSCODE, window._task_detail_var.get())
            self.assertNotIn(SHARE_URL, window._task_detail_var.get())

    def test_retry_after_share_cleanup_failure_does_not_overwrite_or_create_more_copies(self):
        fixture = ShareDownloadFixture()
        fixture.delete_error = True
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"QUARK_CONFIG_DIR": folder}):
            backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
            window = self._task_window(cookie=lambda: COOKIE, backend_factory=lambda loader: backend)
            window._share_source = ShareSource(SHARE_URL, PASSCODE)
            window._output_dir = folder
            window._adopt_rows([Entry("share-note", "报告[1].txt", False, len(fixture.payload))])
            window._checked = {"share-note"}
            window.start_download()
            first = self._requests(window)[0]
            window._worker._download(first)
            while not window._worker.events.empty():
                window._handle(window._worker.events.get_nowait())
            self.assertEqual(window._tasks[0].state, "failed")
            self.assertIn("清理", window._tasks[0].detail)
            self.assertEqual(Path(first.target).read_bytes(), fixture.payload)
            created = len(fixture.bodies("/file"))
            window.retry_failed()
            window._worker._download(self._requests(window)[0])
            while not window._worker.events.empty():
                window._handle(window._worker.events.get_nowait())
            self.assertEqual(window._tasks[0].state, "failed")
            self.assertIn("目标已存在", window._tasks[0].detail)
            self.assertEqual(len(fixture.bodies("/file")), created)
            self.assertEqual(Path(first.target).read_bytes(), fixture.payload)
            self.assertIn("清理", window._log.content)


@unittest.skipUnless(_TK, "Tk is unavailable")
class RealTaskControlsTests(unittest.TestCase):
    def test_real_context_menu_selects_clicked_task_and_invokes_single_retry(self):
        root = _tk_root()
        opened = []
        window = DownloaderWindow(cookie=lambda: "sid=test", backend_factory=lambda loader: _Backend(),
                                  folder_opener=opened.append)
        try:
            window._root = root
            window._build(_tkinter())
            with tempfile.TemporaryDirectory() as folder:
                window._output_dir = folder
                window._adopt_rows([Entry("a", "a.bin", False), Entry("b", "b.bin", False)])
                window._checked = {"a", "b"}
                window.start_download()
                window._handle(Finished(DOWNLOAD_ACTION, "done"))
                window._handle(Failed(DOWNLOAD_ACTION, "failed"))
                root.update()
                bounds = window._task_tree.bbox("1")
                self.assertTrue(bounds)
                with patch.object(window._task_menu, "tk_popup") as popup:
                    window._on_task_menu(SimpleNamespace(y=bounds[1] + 3, x_root=100, y_root=100))
                popup.assert_called_once_with(100, 100)
                self.assertEqual(window._task_tree.selection(), ("1",))
                self.assertEqual(str(window._task_menu.entrycget(0, "state")), "normal")
                window._task_menu.invoke(1)
                self.assertEqual(opened, [folder])
                window._task_menu.invoke(0)
                self.assertEqual(window._batch.current.fid, "b")
                window._handle(Finished(DOWNLOAD_ACTION, "done"))
        finally:
            window._shutdown()

    def test_real_tab_buttons_retry_failure_and_keep_completed_row(self):
        root = _tk_root()
        window = DownloaderWindow(cookie=lambda: "sid=test", backend_factory=lambda loader: _Backend(),
                                  root_factory=lambda: root)
        try:
            window._root = root
            window._build(_tkinter())
            with tempfile.TemporaryDirectory() as folder:
                window._output_dir = folder
                window._adopt_rows([Entry("a", "a.bin", False), Entry("b", "b.bin", False)])
                window._checked = {"a", "b"}
                window._sync_controls()
                window._download_button.invoke()
                self.assertEqual(window._notebook.tab(window._notebook.select(), "text"), "下载任务")
                window._handle(Finished(DOWNLOAD_ACTION, "已下载 a.bin"))
                window._handle(Failed(DOWNLOAD_ACTION, "下载失败：网络断开"))
                root.update_idletasks()
                self.assertEqual(window._task_tree.set("0", "status"), "已完成")
                self.assertEqual(str(window._retry_button["state"]), "normal")
                window._retry_button.invoke()
                self.assertEqual(window._batch.current.fid, "b")
                self.assertEqual(str(window._continue_button["state"]), "disabled")
                window._handle(Finished(DOWNLOAD_ACTION, "已下载 b.bin"))
                self.assertEqual(window._task_tree.set("1", "status"), "已完成")
                self.assertEqual(str(window._retry_button["state"]), "disabled")
        finally:
            window._shutdown()
