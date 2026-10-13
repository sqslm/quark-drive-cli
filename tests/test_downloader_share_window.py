"""The downloader window's share view: importing a link, browsing it, downloading it.

``打开分享`` is a second source beside the account drive, and its rules are the
window's own: a strict link is imported and never echoed, the account search and
its ticks end at the import, share folders are read by their own path instead of
an account FID, and a click inside the share freezes the source and folder it
came from. The state machine is driven through the stand-in widgets of
:mod:`tests.test_downloader_window`; one real-Tk test then drives the real
分享链接/打开分享/我的网盘 controls and the real table.
"""

from __future__ import annotations

import queue
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any

from quark_cli.downloader.app import (
    CHECKED_MARK,
    IDLE_STATUS,
    SHARE_DOWNLOAD_HINT,
    SHARE_IMPORT_ACTION,
    SHARE_READING_STATUS,
    SHARE_ROOT_PATH,
    SHARE_TOP_STATUS,
    DownloaderWindow,
    match_text,
    parse_share_url,
    parse_share_input,
)
from quark_cli.downloader.core import DOWNLOAD_ACTION, ROOT_FID, Entry
from quark_cli.downloader.share import ShareSource
from quark_cli.downloader.worker import (
    SHARE_BROWSE_ACTION,
    Browse,
    BrowseShare,
    Download,
    Failed,
    Finished,
    Listing,
    Search,
    SearchResults,
    ShareListing,
    Stopped,
)
from quark_cli.errors import UsageError

try:  # ``python -m unittest discover -s tests`` imports siblings by bare name
    from test_downloader_window import (  # type: ignore[import-not-found]
        _TK,
        WindowCase,
        _Backend,
        _FakeTree,
        _tk_root,
    )
except ImportError:  # pragma: no cover - ``python -m unittest tests.test_downloader_share_window``
    from tests.test_downloader_window import (
        _TK,
        WindowCase,
        _Backend,
        _FakeTree,
        _tk_root,
    )

#: Two share links and one passcode, as a user types them. All three are made up,
#: and no test prints them: the window has to keep them out of its own text.
SHARE_LINK = "https://pan.quark.cn/s/000000000001"
OTHER_LINK = "https://pan.quark.cn/s/000000000002"
PASSCODE = "abcd"


class ShareWindowCase(WindowCase):
    """The shared window harness, plus the moves the share view needs."""

    @staticmethod
    def _requests(window: DownloaderWindow) -> list[Any]:
        """Every submitted request, in order: the worker thread never runs here."""
        submitted: list[Any] = []
        while True:
            try:
                submitted.append(window._worker._requests.get_nowait())
            except queue.Empty:
                return submitted

    def _import(self, window: DownloaderWindow, *, link: str = SHARE_LINK) -> None:
        """Type a link and its passcode, then press ``打开分享``."""
        window._share_var.set(link)
        window._passcode_var.set(PASSCODE)
        window.open_share()

    @staticmethod
    def _folder(window: DownloaderWindow, path: str, *entries: Entry) -> None:
        """Deliver one share folder listing, as the worker would."""
        window._handle(ShareListing(window._share_source, path, entries))

    @staticmethod
    def _activate(window: DownloaderWindow, row: int) -> Any:
        """Activate one row of the fake table, the way a double-click does."""
        return window._on_activate(SimpleNamespace(x=200, y=row * _FakeTree.ROW_HEIGHT + 2))

    @staticmethod
    def _texts(window: DownloaderWindow) -> list[str]:
        """The name column of every row the fake table shows."""
        return [row[1] for row in window._tree.rows]


class ShareLinkTests(unittest.TestCase):
    def test_a_complete_share_message_extracts_one_link_and_code(self):
        for text in (f"链接：{SHARE_LINK}\n提取码：{PASSCODE}，复制这段文字",
                     f"分享「资料」（{SHARE_LINK}?pwd={PASSCODE}）",
                     f"{SHARE_LINK} 密码: {PASSCODE}"):
            source = parse_share_input(text)
            self.assertEqual(source.passcode, PASSCODE)
            self.assertTrue(source.url.startswith(SHARE_LINK))
        self.assertEqual(parse_share_input(f"{SHARE_LINK}?pwd=auto", "edited").passcode, "edited")
        for text in (f"{SHARE_LINK} {OTHER_LINK}", f"{SHARE_LINK}?pwd=auto 提取码：other",
                     "https://[broken", "https://evil.invalid/s/a", f"{SHARE_LINK}/extra"):
            with self.subTest(text=text), self.assertRaises(UsageError):
                parse_share_input(text)

    """The link the window accepts is the library's own strict one."""

    def test_a_link_is_normalised_and_only_a_strict_one_is_taken(self):
        linked = parse_share_url(f"{SHARE_LINK}?pwd={PASSCODE}")
        self.assertEqual((linked.url, linked.passcode), (f"{SHARE_LINK}?pwd={PASSCODE}", PASSCODE))
        # A typed passcode is the one used, and surrounding space is not part of
        # the link; both are what the request then carries.
        self.assertEqual(parse_share_url(f"{SHARE_LINK}?pwd=from-link", "typed").passcode, "typed")
        self.assertEqual(parse_share_url(f"  {SHARE_LINK}  ").url, SHARE_LINK)
        # No ``pwd`` in the link and none typed leaves no passcode at all.
        self.assertIsNone(parse_share_url(SHARE_LINK).passcode)
        refused = (
            "",
            "   ",
            SHARE_LINK.replace("https://", "http://"),
            SHARE_LINK.replace("pan.quark.cn", "evil.invalid"),
            f"{SHARE_LINK}/extra",
            f"{SHARE_LINK}?pwd={PASSCODE}&extra=1",
            SHARE_LINK.replace("/s/", "/s/../"),
            "https://pan.quark.cn/s/",
        )
        for index, url in enumerate(refused):
            with self.subTest(case=index), self.assertRaises(UsageError):
                parse_share_url(url, PASSCODE)


class ShareImportTests(ShareWindowCase):
    def test_paste_fills_the_code_and_a_manual_edit_wins_when_opening(self):
        window = self._window()
        window._share_var.set(f"我分享了「资料」\n链接：{SHARE_LINK}\n提取码：{PASSCODE}")
        self.assertEqual(window._passcode_var.get(), PASSCODE)
        window._passcode_var.set("edited")
        window.open_share()
        request = self._submitted(window)
        self.assertEqual(request, BrowseShare(ShareSource(SHARE_LINK, "edited"), "/"))
        self.assertNotIn("edited", window._log.content)

    def test_importing_ends_the_account_search_and_opens_the_share_root(self):
        window = self._window()
        self._listing(window, Entry("d", "账号目录", True), Entry("f", "报告.bin", False, 4))
        window._search_var.set("报告")
        window._submit_search(ROOT_FID)
        self.assertIsInstance(self._submitted(window), Search)
        window._handle(SearchResults(ROOT_FID, "报告", (Entry("r", "报告.bin", False, 4, path="/报告.bin"),), request_id=window._search_generation))
        self._tick(window, "r")

        # A link the library refuses changes nothing: the search stays as it is.
        self._import(window, link="https://evil.invalid/s/000000000001")
        self.assertTrue(window._status_var.value.startswith(f"{SHARE_IMPORT_ACTION}失败"))
        self.assertNotIn("evil.invalid", window._status_var.value)
        self.assertNotIn(PASSCODE, window._status_var.value)
        self.assertEqual(self._texts(window), ["报告.bin"])
        self.assertEqual(window._checked, {"r"})
        self.assertIsNotNone(window._search)
        self.assertEqual(self._requests(window), [])

        self._import(window)
        source = ShareSource(SHARE_LINK, PASSCODE)
        self.assertEqual(self._requests(window), [BrowseShare(source, SHARE_ROOT_PATH)])
        # The account search is over: its rows, its keyword, its folder to
        # return to and its ticks all belong to a table that is gone.
        self.assertIsNone(window._search)
        self.assertIsNone(window._pending)
        self.assertIsNone(window._origin)
        self.assertFalse(window._searching)
        self.assertEqual(window._checked, set())
        self.assertEqual(window._tree.rows, [])
        self.assertEqual(window._count_var.value, match_text(0, 0))
        self.assertEqual(window._status_var.value, SHARE_READING_STATUS)
        self.assertEqual(window._location_var.value, f"分享 {SHARE_ROOT_PATH}")
        self.assertTrue(window._reading_share)
        # The link and its passcode reach neither the status line nor the log.
        for text in (window._status_var.value, window._location_var.value, window._log.content):
            self.assertNotIn(SHARE_LINK, text)
            self.assertNotIn(PASSCODE, text)
        # The answer to the search just left cannot fill the share table.
        window._handle(SearchResults(ROOT_FID, "报告", (Entry("r", "报告.bin", False, 4),), request_id=window._search_generation))
        self.assertEqual(window._tree.rows, [])


class ShareBrowsingTests(ShareWindowCase):
    def test_only_the_share_folder_the_window_shows_may_fill_the_table(self):
        window = self._window()
        self._import(window)
        source = window._share_source
        self._submitted(window)
        rows = (Entry("s1", "分享文件.bin", False, 4, path="/分享文件.bin"),)

        # Another share, or another folder of this one, is not what the table
        # asked for: both are stale answers and the read stays in flight.
        window._handle(ShareListing(ShareSource(OTHER_LINK, PASSCODE), SHARE_ROOT_PATH, rows))
        window._handle(ShareListing(source, "/别的目录", rows))
        self.assertEqual(window._tree.rows, [])
        self.assertTrue(window._reading_share)

        window._handle(ShareListing(source, SHARE_ROOT_PATH, rows))
        self.assertEqual(self._texts(window), ["分享文件.bin"])
        self.assertEqual(window._status_var.value, IDLE_STATUS)
        self.assertFalse(window._reading_share)

        # An account listing and a late search answer are dropped in a share
        # view, so no share row can ever sit under an account location line.
        window._handle(Listing(ROOT_FID, (Entry("a1", "账号文件.bin", False, 4),)))
        window._handle(SearchResults(ROOT_FID, "文件", (Entry("a2", "搜索文件.bin", False, 4),), request_id=window._search_generation))
        self.assertEqual(self._texts(window), ["分享文件.bin"])

        window.open_drive()
        self._submitted(window)
        window._handle(ShareListing(source, SHARE_ROOT_PATH, rows))
        self.assertEqual(window._tree.rows, [])

    def test_a_share_folder_is_read_by_its_own_path_not_an_account_fid(self):
        window = self._window()
        self._import(window)
        source = window._share_source
        self._submitted(window)
        self._folder(window, SHARE_ROOT_PATH, Entry("s1", "资料", True, path="/资料"), Entry("s2", "a.bin", False, 4, path="/a.bin"))

        self._activate(window, 1)  # a file row is not a folder
        self.assertEqual(self._requests(window), [])
        self._activate(window, 0)
        self.assertEqual(self._requests(window), [BrowseShare(source, "/资料")])
        self.assertEqual(window._share_trail, [SHARE_ROOT_PATH, "/资料"])
        self.assertEqual(window._tree.rows, [])
        self.assertEqual(window._location_var.value, "分享 /资料")
        self.assertTrue(window._reading_share)

        # 刷新 re-reads the path shown, and the ticks of the rows that are still
        # there survive it; the rows the new listing no longer holds lose theirs.
        self._folder(window, "/资料", Entry("s3", "b.bin", False, 4, path="/资料/b.bin"))
        self._tick(window, "s3")
        window.refresh()
        self.assertEqual(self._requests(window), [BrowseShare(source, "/资料")])
        self._folder(window, "/资料", Entry("s3", "b.bin", False, 4, path="/资料/b.bin"))
        self.assertEqual(self._marks(window), [CHECKED_MARK])
        self._folder(window, "/资料", Entry("s4", "c.bin", False, 4, path="/资料/c.bin"))
        self.assertEqual(window._checked, set())

        window.go_up()
        self.assertEqual(self._requests(window), [BrowseShare(source, SHARE_ROOT_PATH)])
        self.assertEqual(window._location_var.value, f"分享 {SHARE_ROOT_PATH}")
        # A share row without a path of its own cannot be entered, and 返回上级
        # at the share's own root only says so.
        self._folder(window, SHARE_ROOT_PATH, Entry("s5", "无路径目录", True))
        self._activate(window, 0)
        self.assertEqual(self._requests(window), [])
        self.assertEqual(window._share_trail, [SHARE_ROOT_PATH])
        window.go_up()
        self.assertEqual(window._status_var.value, SHARE_TOP_STATUS)
        self.assertEqual(self._requests(window), [])

    def test_a_failed_share_read_leaves_an_empty_table_and_a_recoverable_view(self):
        window = self._window()
        self._import(window)
        source = window._share_source
        self._submitted(window)
        self._folder(window, SHARE_ROOT_PATH, Entry("s1", "资料", True, path="/资料"))
        self._activate(window, 0)
        self._submitted(window)

        # The read fails after the path changed: the rows of "/" are gone and
        # nothing is shown under the location line of the folder that failed.
        message = "读取分享失败：分享已过期或不存在"
        window._handle(Failed(SHARE_BROWSE_ACTION, message))
        self.assertFalse(window._reading_share)
        self.assertEqual(window._tree.rows, [])
        self.assertEqual(window._status_var.value, message)
        self.assertEqual(window._location_var.value, "分享 /资料")
        # Both ways out of a failed read are open: retry it, or go back to the
        # drive the window was showing before the import.
        for button in (window._up_button, window._refresh_button, window._share_button, window._drive_button):
            self.assertEqual(button.state, "normal")
        self.assertEqual(window._download_button.state, "disabled")
        window.refresh()
        self.assertEqual(self._requests(window), [BrowseShare(source, "/资料")])
        self._folder(window, "/资料", Entry("s2", "b.bin", False, 4, path="/资料/b.bin"))
        self.assertEqual(self._texts(window), ["b.bin"])
        window.open_drive()
        self.assertIsNone(window._share_source)
        self.assertEqual(self._requests(window), [Browse(ROOT_FID)])
        self.assertEqual(window._status_var.value, "正在读取目录……")


class ShareControlTests(ShareWindowCase):
    def test_a_share_read_freezes_navigation_and_the_account_search_is_out_of_reach(self):
        window = self._window()
        self._import(window)
        source = window._share_source
        self._submitted(window)
        for button in (window._up_button, window._refresh_button, window._share_button, window._drive_button, window._login_button, window._select_all_button):
            self.assertEqual(button.state, "disabled")
        # A drive search would read the account instead of the share.
        for button in (window._search_button, window._scope_combo):
            self.assertEqual(button.state, "disabled")
        window.go_up()
        window.refresh()
        window.open_drive()
        window.leave_search()
        window.login()
        window.search()
        window.search_subdirectories()
        self.assertEqual(self._requests(window), [])
        self.assertEqual(window._share_trail, [SHARE_ROOT_PATH])

        self._folder(window, SHARE_ROOT_PATH, Entry("s1", "资料", True, path="/资料"))
        self.assertEqual(window._up_button.state, "normal")
        self.assertEqual(window._search_button.state, "disabled")
        self.assertEqual(window._login_button.state, "disabled")
        self._activate(window, 0)
        self.assertEqual(self._requests(window), [BrowseShare(source, "/资料")])
        self._folder(window, "/资料", Entry("s2", "b.bin", False, 4, path="/资料/b.bin"))

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        window._output_dir = temporary.name
        self._tick(window, "s2")
        window.start_download()
        self.assertTrue(window._downloading)
        request = self._submitted(window)
        self.assertEqual((request.share_source, request.share_path), (source, "/资料"))
        # A batch in flight freezes the source as well: a new link, a folder
        # click, 返回上级 and a click on a box may not move the view or the
        # selection out from under it.
        self._import(window, link=OTHER_LINK)
        self._activate(window, 0)
        window.go_up()
        self._click(window)
        self.assertEqual(self._requests(window), [])
        self.assertEqual(self._marks(window), [CHECKED_MARK])
        self.assertEqual(window._share_trail, [SHARE_ROOT_PATH, "/资料"])
        self.assertEqual(window._location_var.value, "分享 /资料")
        self.assertEqual(window._share_source, source)
        self.assertEqual(window._share_entry.state, "disabled")
        self.assertEqual(window._passcode_entry.state, "disabled")
        window._handle(Finished(DOWNLOAD_ACTION, "已下载 b.bin：4 B"))
        self.assertFalse(window._downloading)

    def test_a_share_click_keeps_its_source_and_stops_without_a_checkpoint(self):
        window = self._window()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        window._output_dir = temporary.name
        self._import(window)
        source = window._share_source
        self._submitted(window)
        self._folder(window, SHARE_ROOT_PATH, Entry("s1", "a.bin", False, 4, path="/a.bin"), Entry("s2", "b.bin", False, 4, path="/b.bin"))
        self._tick(window, "s1", "s2")
        window.start_download()
        first = self._submitted(window)
        window._handle(Finished(DOWNLOAD_ACTION, "已下载 a.bin：4 B"))
        second = self._submitted(window)
        # The click serves the table order and every item carries the source and
        # the shown share folder, never an account FID or an account path.
        self.assertEqual((first.fid, second.fid), ("s1", "s2"))
        for request in (first, second):
            self.assertIsInstance(request, Download)
            self.assertEqual((request.share_source, request.share_path), (source, SHARE_ROOT_PATH))
            self.assertFalse(request.overwrite)
        self.assertEqual(window._batch_share, (source, SHARE_ROOT_PATH))
        self.assertIn(SHARE_DOWNLOAD_HINT, window._log.content)

        window.stop_download()
        self.assertIn("当前项目将在检查点结束", window._log.content)
        window._handle(Stopped(DOWNLOAD_ACTION, "分享下载已停止：临时内容已清理，需重新下载"))
        # A share download has no checkpoint left to continue from: the closing
        # line must not claim one was kept.
        self.assertEqual(window._status_var.value, "已停止：成功 1 项，1 项已停止（需重新下载）")
        self.assertNotIn("检查点已保留", window._status_var.value)
        self.assertFalse(window._downloading)
        self.assertIsNone(window._batch)
        self.assertIsNone(window._batch_share)


class _ShareBackend(_Backend):
    """A drive whose share root holds one folder, and that folder one file."""

    def __init__(self) -> None:
        super().__init__([Entry("a1", "账号.bin", False, 4)])

    def share_listing(self, source: ShareSource, path: str) -> list[Entry]:
        if path == SHARE_ROOT_PATH:
            return [Entry("s1", "分享资料", True, path="/分享资料")]
        return [Entry("s2", "深层.bin", False, 8, path=f"{path}/深层.bin")]


@unittest.skipUnless(_TK, "Tkinter with a display is required")
class ShareWindowSmokeTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict("os.environ", {"QUARK_CONFIG_DIR": directory.name})
        environment.start()
        self.addCleanup(environment.stop)

    def test_the_real_window_imports_a_share_walks_it_and_returns_to_the_drive(self):
        root = _tk_root()
        window = DownloaderWindow(
            cookie=lambda: "sid=test",
            backend_factory=lambda loader: _ShareBackend(),
            root_factory=lambda: root,
        )
        observed: dict[str, Any] = {}

        def rows() -> list[str]:
            return [window._tree.item(iid, "text") for iid in window._tree.get_children()]

        def automate() -> None:
            if not rows():
                root.after(50, automate)
                return
            observed["drive_rows"] = rows()
            observed["masked"] = str(window._passcode_entry.cget("show"))
            window._share_entry.insert(0, SHARE_LINK)
            window._passcode_entry.insert(0, PASSCODE)
            window.open_share()
            root.after(50, watch_share)

        def watch_share() -> None:
            if not rows():
                root.after(50, watch_share)
                return
            observed["share_rows"] = rows()
            observed["share_location"] = window._location_var.get()
            observed["share_buttons"] = [
                str(window._share_button.cget("state")),
                str(window._drive_button.cget("state")),
            ]
            observed["log"] = window._log.get("1.0", "end")
            # The keyboard activate is the same path a double-click takes.
            window._tree.selection_set(window._tree.get_children()[0])
            window._on_activate(None)
            root.after(50, watch_folder)

        def watch_folder() -> None:
            if rows() != ["深层.bin"]:
                root.after(50, watch_folder)
                return
            observed["folder_rows"] = rows()
            observed["folder_location"] = window._location_var.get()
            window.open_drive()
            root.after(50, watch_drive)

        def watch_drive() -> None:
            if window._share_source is not None or not rows() or window._status_var.get() != IDLE_STATUS:
                root.after(50, watch_drive)
                return
            observed["back_rows"] = rows()
            observed["back_location"] = window._location_var.get()
            window.close()

        def watchdog() -> None:
            observed["timed_out"] = True
            window.close()

        root.after(50, automate)
        root.after(15000, watchdog)
        window.run()

        self.assertFalse(observed.get("timed_out"), "the share view never finished its walk")
        self.assertEqual(observed.get("drive_rows"), ["账号.bin"])
        self.assertEqual(observed.get("masked"), "*")
        self.assertEqual(observed.get("share_rows"), ["分享资料"])
        self.assertEqual(observed.get("share_location"), f"分享 {SHARE_ROOT_PATH}")
        self.assertEqual(observed.get("share_buttons"), ["normal", "normal"])
        self.assertEqual(observed.get("folder_rows"), ["深层.bin"])
        self.assertEqual(observed.get("folder_location"), "分享 /分享资料")
        self.assertEqual(observed.get("back_rows"), ["账号.bin"])
        self.assertEqual(observed.get("back_location"), "/")
        for text in (observed.get("log", ""), observed.get("share_location", "")):
            self.assertNotIn(SHARE_LINK, text)
            self.assertNotIn(PASSCODE, text)


if __name__ == "__main__":
    unittest.main()
