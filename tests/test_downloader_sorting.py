"""Sorting keeps real object selections and the queue's visible order."""

import queue
import tempfile
import unittest

from quark_cli.downloader.app import CHECKED_MARK, DownloaderWindow, _tkinter
from quark_cli.downloader.core import Entry, sort_entries
from quark_cli.downloader.share import ShareSource
from quark_cli.downloader.worker import DOWNLOAD_ACTION, Failed, Finished, Listing, Search, SearchResults, ShareListing
from quark_cli.errors import UsageError
from tests.test_downloader_window import WindowCase, _Backend, _TK, _tk_root


class SortRulesTests(unittest.TestCase):
    def setUp(self):
        self.rows = [Entry("b", "b.bin", False, 20, "/a/b.bin"),
                     Entry("z", "Z folder", True, path="/z"),
                     Entry("u", "unknown.bin", False, None, "/c/unknown.bin"),
                     Entry("a", "A.bin", False, 0, "/z/A.bin"),
                     Entry("d", "a folder", True, path="/a")]

    def test_names_are_case_insensitive_and_folders_stay_first_in_both_directions(self):
        original = tuple(self.rows)
        self.assertEqual([e.fid for e in sort_entries(self.rows, "name")], ["d", "z", "a", "b", "u"])
        self.assertEqual([e.fid for e in sort_entries(self.rows, "name", descending=True)], ["z", "d", "u", "b", "a"])
        self.assertEqual(tuple(self.rows), original)

    def test_size_sort_uses_bytes_with_zero_and_unknown_sizes_in_both_directions(self):
        self.assertEqual([e.fid for e in sort_entries(self.rows, "size")], ["d", "z", "a", "b", "u"])
        self.assertEqual([e.fid for e in sort_entries(self.rows, "size", descending=True)], ["d", "z", "b", "a", "u"])

    def test_paths_distinguish_equal_names_and_equal_values_preserve_source_order(self):
        rows = [Entry("b", "same.bin", False, 10, "/b/same.bin"),
                Entry("a", "same.bin", False, 10, "/a/same.bin")]
        self.assertEqual([e.fid for e in sort_entries(rows, "path")], ["a", "b"])
        self.assertEqual([e.fid for e in sort_entries(rows, "path", descending=True)], ["b", "a"])
        for key in ("name", "size"):
            self.assertEqual([e.fid for e in sort_entries(rows, key, descending=True)], ["b", "a"])
        self.assertEqual(sort_entries([], "name"), [])
        with self.assertRaises(UsageError):
            sort_entries(rows, "unsupported")


class SortWindowTests(WindowCase):
    @staticmethod
    def _requests(window):
        result = []
        while True:
            try:
                result.append(window._worker._requests.get_nowait())
            except queue.Empty:
                return result

    def test_sort_keeps_checks_highlight_and_keyboard_focus_on_the_same_fids(self):
        window = self._window()
        window._adopt_rows([Entry("b", "b.bin", False, 20), Entry("a", "a.bin", False, 1)])
        window._checked = {"b"}
        window._tree.selection_ids = ("row0",)
        window._tree.focus("row0")
        window.sort_by("name")
        self.assertEqual([e.fid for e in window._entries.values()], ["a", "b"])
        self.assertEqual(window._tree.selection(), ("row1",))
        self.assertEqual(window._tree.focus(), "row1")
        self.assertEqual(window._tree.rows[1][2][0], CHECKED_MARK)
        self.assertEqual(window._tree.headings["#0"]["text"], "名称 ↑")
        window.sort_by("name")
        self.assertEqual(window._tree.focus(), "row0")
        self.assertEqual(window._tree.headings["#0"]["text"], "名称 ↓")
        self.assertEqual(window._checked, {"b"})
        self.assertEqual(self._requests(window), [])

    def test_filter_and_refresh_keep_sort_setting_and_hidden_checks(self):
        window = self._window()
        rows = (Entry("a", "a.bin", False, 1), Entry("b", "b.bin", False, 100))
        window._adopt_rows(rows)
        window._checked = {"b"}
        window.sort_by("size")
        window.sort_by("size")
        window._search_var.set("a.bin")
        self.assertEqual([e.fid for e in window._entries.values()], ["a"])
        self.assertEqual(window._checked, {"b"})
        window._search_var.set("")
        window._handle(Listing("0", rows))
        self.assertEqual([e.fid for e in window._entries.values()], ["b", "a"])
        self.assertEqual(window._checked_entries()[0].fid, "b")

    def test_search_results_and_share_lists_use_path_sort_without_requests(self):
        rows = (Entry("b", "same.bin", False, 1, "/b/same.bin"),
                Entry("a", "same.bin", False, 1, "/a/same.bin"))
        window = self._window()
        window._searching, window._pending = True, Search("0", "same")
        window._handle(SearchResults("0", "same", rows))
        window.sort_by("path")
        self.assertEqual([e.fid for e in window._entries.values()], ["a", "b"])
        source = ShareSource("https://pan.quark.cn/s/offline")
        window._share_source, window._search, window._share_trail = source, None, ["/"]
        window._handle(ShareListing(source, "/", rows))
        self.assertEqual([e.fid for e in window._entries.values()], ["a", "b"])
        window.sort_by("path")
        self.assertEqual([e.fid for e in window._entries.values()], ["b", "a"])
        self.assertEqual(self._requests(window), [])

    def test_busy_sort_cannot_change_frozen_download_order(self):
        with tempfile.TemporaryDirectory() as folder:
            window = self._window()
            window._output_dir = folder
            window._adopt_rows([Entry("a", "a.bin", False, 1), Entry("b", "b.bin", False, 20)])
            window.sort_by("size")
            window.sort_by("size")
            window._checked = {"a", "b"}
            window.start_download()
            self.assertEqual(self._requests(window)[0].fid, "b")
            window.sort_by("name")
            self.assertEqual(window._sort_key, "size")
            window._handle(Finished(DOWNLOAD_ACTION, "done"))
            self.assertEqual(self._requests(window)[0].fid, "a")
            window._handle(Failed(DOWNLOAD_ACTION, "failed"))
            for flag in ("_reading_share", "_logging_in"):
                setattr(window, flag, True)
                window.sort_by("name")
                self.assertEqual(window._sort_key, "size")
                setattr(window, flag, False)


@unittest.skipUnless(_TK, "Tk is unavailable")
class SortControlsTests(unittest.TestCase):
    def test_real_header_callbacks_keep_selection_and_toggle_size_order(self):
        root = _tk_root()
        window = DownloaderWindow(cookie=lambda: "sid=offline", backend_factory=lambda loader: _Backend())
        try:
            window._root = root
            window._build(_tkinter())
            window._adopt_rows([Entry("b", "b.bin", False, 1024), Entry("a", "a.bin", False, 1)])
            window._tree.selection_set("row0")
            window._tree.focus("row0")
            root.update_idletasks()
            callback = window._tree.heading("size", "command")
            root.tk.call(callback)
            self.assertEqual([e.fid for e in window._entries.values()], ["a", "b"])
            self.assertEqual(window._tree.heading("size", "text"), "大小 ↑")
            self.assertEqual(window._tree.selection(), ("row1",))
            self.assertEqual(window._tree.focus(), "row1")
            root.tk.call(callback)
            self.assertEqual(window._tree.heading("size", "text"), "大小 ↓")
            self.assertEqual(window._tree.focus(), "row0")
        finally:
            window._shutdown()
