"""Suffix filtering stays local and excludes hidden checks from downloads."""

import queue
import tempfile
import unittest

from quark_cli.downloader.app import DownloaderWindow, IDLE_STATUS, _tkinter
from quark_cli.downloader.core import EXTENSION_CHOICES, Entry, filter_entries
from quark_cli.downloader.share import ShareSource
from quark_cli.downloader.worker import DOWNLOAD_ACTION, Finished, Listing, Search, SearchResults, ShareListing
from quark_cli.errors import UsageError
from tests.test_downloader_window import WindowCase, _Backend, _FakeCombo, _FakeVar, _TK, _tk_root


class ExtensionRulesTests(unittest.TestCase):
    def setUp(self):
        self.rows = [Entry("dir", "folder.mp4", True), Entry("video", "clip.MP4", False),
                     Entry("audio", "sound.flac", False), Entry("image", "image.JPG", False),
                     Entry("doc", "Report.PDF", False), Entry("zip", "archive.tar.gz", False),
                     Entry("plain", "README", False), Entry("hidden", ".env", False),
                     Entry("exe", "clip.mp4.exe", False)]

    def test_groups_match_only_files_and_full_suffixes_case_insensitively(self):
        for group, expected in (("视频", "video"), ("音频", "audio"), ("图片", "image"),
                                ("文档", "doc"), ("压缩包", "zip")):
            with self.subTest(group=group):
                self.assertEqual([entry.fid for entry in filter_entries(self.rows, extensions=group)], [expected])
        self.assertEqual([e.fid for e in filter_entries(self.rows, extensions="无扩展名")], ["plain", "hidden"])
        self.assertEqual(filter_entries(self.rows, extensions="所有格式"), self.rows)
        self.assertEqual(filter_entries(self.rows, extensions=" "), self.rows)

    def test_custom_extensions_accept_multiple_separators_leading_dot_and_compound_suffix(self):
        for text in ("MP4, pdf; tar.gz", ".mp4 .PDF .tar.gz", "mp4，pdf；tar.gz", "mp4、pdf、tar.gz"):
            with self.subTest(text=text):
                self.assertEqual([e.fid for e in filter_entries(self.rows, extensions=text)], ["video", "doc", "zip"])
        self.assertEqual([e.fid for e in filter_entries(self.rows, extensions=".tar.gz")], ["zip"])
        self.assertEqual(filter_entries(self.rows, "Report", "files", extensions="文档"), [self.rows[4]])
        self.assertEqual(filter_entries(self.rows, kind="directories", extensions="视频"), [])

    def test_invalid_tokens_fail_explicitly(self):
        for value in (None, 1, ".", "*.mp4", "mp4/path", "mp4\\path", "..mp4", "mp4.", "mp4:pdf", ",;"):
            with self.subTest(value=value), self.assertRaises(UsageError):
                filter_entries(self.rows, extensions=value)


class ExtensionWindowTests(WindowCase):
    def _extension_window(self):
        window = self._window()
        window._extension_var = _FakeVar("所有格式")
        window._extension_combo = _FakeCombo()
        window._extension_var.trace_add("write", window._on_filter_change)
        return window

    @staticmethod
    def _requests(window):
        result = []
        while True:
            try:
                result.append(window._worker._requests.get_nowait())
            except queue.Empty:
                return result

    def test_hidden_checks_survive_filter_and_only_visible_matches_download_in_sorted_order(self):
        with tempfile.TemporaryDirectory() as folder:
            window = self._extension_window()
            window._output_dir = folder
            rows = (Entry("pdf", "a.pdf", False, 10), Entry("mp4", "b.MP4", False, 20),
                    Entry("mkv", "c.mkv", False, 5))
            window._adopt_rows(rows)
            window._checked = {"pdf", "mp4", "mkv"}
            window.sort_by("size")
            window._extension_var.set("视频")
            self.assertEqual(window._checked, {"pdf", "mp4", "mkv"})
            self.assertEqual([e.fid for e in window._checked_entries()], ["mkv", "mp4"])
            self.assertEqual(self._requests(window), [])
            window.start_download()
            self.assertEqual(self._requests(window)[0].fid, "mkv")
            self.assertEqual(window._extension_combo.state, "disabled")
            window._handle(Finished(DOWNLOAD_ACTION, "done"))
            self.assertEqual(self._requests(window)[0].fid, "mp4")
            window._handle(Finished(DOWNLOAD_ACTION, "done"))
            window._extension_var.set("所有格式")
            self.assertEqual(len(window._checked_entries()), 3)

    def test_invalid_filter_clears_visible_matches_blocks_stale_download_and_recovers(self):
        with tempfile.TemporaryDirectory() as folder:
            window = self._extension_window()
            window._output_dir = folder
            rows = (Entry("v", "clip.mp4", False),)
            window._adopt_rows(rows)
            window._checked = {"v"}
            window._extension_var.set("*.mp4")
            self.assertEqual(window._entries, {})
            self.assertEqual(window._download_button.state, "disabled")
            window._handle(Listing("0", rows))
            self.assertIn("扩展名格式无效", window._status_var.get())
            window.start_download()
            self.assertEqual(self._requests(window), [])
            window._extension_var.set("mp4")
            self.assertEqual(window._checked_entries()[0].fid, "v")
            self.assertEqual(window._extension_error, "")
            window._extension_var.set(".")
            window._extension_var.set("mp4")
            self.assertEqual(window._status_var.get(), IDLE_STATUS)

    def test_search_share_refresh_and_navigation_follow_existing_filter_rules(self):
        rows = (Entry("v", "clip.mp4", False), Entry("p", "clip.pdf", False))
        window = self._extension_window()
        window._extension_var.set("视频")
        window._searching, window._pending = True, Search("0", "clip")
        window._handle(SearchResults("0", "clip", rows))
        self.assertEqual(list(e.fid for e in window._entries.values()), ["v"])
        window._share_source, window._share_trail, window._search = ShareSource("https://pan.quark.cn/s/test"), ["/"], None
        window._handle(ShareListing(window._share_source, "/", rows))
        self.assertEqual(list(e.fid for e in window._entries.values()), ["v"])
        window._handle(ShareListing(window._share_source, "/", rows))
        self.assertEqual(window._extension_var.get(), "视频")
        window._clear_filters()
        self.assertEqual(window._extension_var.get(), "所有格式")
        self.assertEqual(len(window._entries), 2)
        self.assertEqual(self._requests(window), [])

    def test_busy_filters_do_not_rewrite_the_current_batch_view(self):
        for flag in ("_downloading", "_logging_in", "_reading_share"):
            window = self._extension_window()
            window._adopt_rows((Entry("v", "clip.mp4", False), Entry("p", "clip.pdf", False)))
            setattr(window, flag, True)
            window._sync_controls()
            window._extension_var.set("视频")
            self.assertEqual(len(window._entries), 2)
            self.assertEqual(window._extension_combo.state, "disabled")


@unittest.skipUnless(_TK, "Tk is unavailable")
class ExtensionControlsTests(unittest.TestCase):
    def test_real_editable_combo_filters_without_network_and_clears_on_navigation_reset(self):
        root = _tk_root()
        window = DownloaderWindow(cookie=lambda: "sid=offline", backend_factory=lambda loader: _Backend())
        try:
            window._root = root
            window._build(_tkinter())
            window._adopt_rows((Entry("v", "clip.mp4", False), Entry("p", "clip.pdf", False)))
            self.assertEqual(tuple(window._extension_combo["values"]), EXTENSION_CHOICES)
            window._extension_combo.set("视频")
            self.assertEqual([e.fid for e in window._entries.values()], ["v"])
            window._extension_combo.set("pdf, .mp4")
            self.assertEqual(len(window._entries), 2)
            window._clear_filters()
            self.assertEqual(window._extension_combo.get(), "所有格式")
            window._downloading = True
            window._sync_controls()
            self.assertEqual(str(window._extension_combo["state"]), "disabled")
            root.update()
            self.assertGreater(window._tree.winfo_height(), 150)
        finally:
            window._shutdown()
