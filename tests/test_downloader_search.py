"""Search must deliver early, stay cancellable, and never sweep the drive for global queries."""
import threading
import time
import unittest

import httpx

from quark_cli.downloader.core import Entry
from quark_cli.downloader.search import SearchCancelled, stream
from quark_cli.downloader.worker import DriveBackend, Search, SearchEnded, SearchResults, Worker
from quark_cli.files import Files
from quark_cli.client import QuarkClient
from quark_cli.errors import RemoteError
from tests.test_downloader_window import WindowCase, _tk_root
from quark_cli.downloader.app import _tkinter
from tests.test_downloader_worker import COOKIE, DriveFixture, item


def response(rows, total=None):
    data = {"list": rows}
    if total is not None:
        data["_total"] = total
    return httpx.Response(200, json={"status": 200, "code": 0, "data": data})


class SearchSourceTests(unittest.TestCase):
    def test_first_page_arrives_before_second_and_only_search_is_requested(self):
        release = threading.Event()
        calls = []
        rows = [item(str(i), f"知否{i}.bin", size=i) for i in range(101)]
        def source(request):
            calls.append(request.url.path)
            self.assertTrue(request.url.path.endswith("file/search"))
            self.assertEqual(request.url.params["_size"], "100")
            self.assertEqual(request.url.params["_is_hl"], "0")
            if request.url.params["_page"] == "2":
                if not release.wait(3):
                    raise AssertionError("second page was not released")
                return response(rows[100:], 101)
            return response(rows[:100], 101)
        worker = Worker(DriveBackend(lambda: COOKIE, transport=httpx.MockTransport(source)))
        worker.start()
        try:
            worker.submit(Search("0", "知否", 17, False))
            first = worker.events.get(timeout=2)
            self.assertIsInstance(first, SearchResults)
            self.assertFalse(first.complete)
            self.assertEqual(first.request_id, 17)
            self.assertEqual(len(first.entries), 100)
            self.assertFalse(first.entries[0].location)
            release.set()
            self.assertEqual(len(worker.events.get(timeout=2).entries), 1)
            self.assertTrue(worker.events.get(timeout=2).complete)
            self.assertEqual(len(calls), 2)
        finally:
            release.set()
            worker.stop()

    def test_cancellation_after_page_does_not_send_next_page(self):
        cancelled = threading.Event()
        calls = []
        def source(request):
            calls.append(request.url.path)
            return response([item(str(i), "知否.bin") for i in range(100)], 200)
        backend = DriveBackend(lambda: COOKIE, transport=httpx.MockTransport(source))
        with self.assertRaises(SearchCancelled):
            backend.search_stream(Search("0", "知否", 1, False), lambda *_: cancelled.set(), cancelled.is_set)
        self.assertEqual(len(calls), 1)

    def test_duplicate_page_is_refused_after_preserving_first_page(self):
        rows = [item(str(i), "知否.bin") for i in range(100)]
        received = []
        backend = DriveBackend(lambda: COOKIE, transport=httpx.MockTransport(lambda _: response(rows, 200)))
        with self.assertRaises(RemoteError):
            backend.search_stream(Search("0", "知否", 1, False), lambda batch, _: received.extend(batch), lambda: False)
        self.assertEqual(len(received), 100)

    def test_subtree_stream_keeps_exact_scope_and_real_paths(self):
        fixture = DriveFixture([item("a", "A", directory=True), item("b", "B", directory=True),
            item("sub", "子目录", "a", directory=True), item("f", "知否.txt", "sub"), item("other", "知否.txt", "b")])
        backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
        batches = []
        backend.search_stream(Search("a", "知否", 1, True), lambda rows, n: batches.append((rows, n)), lambda: False)
        found = [row for rows, _ in batches for row in rows]
        self.assertEqual([(r.fid, r.path) for r in found], [("f", "/A/子目录/知否.txt")])
        self.assertEqual(batches[-1][1], 2)

    def test_locate_keeps_same_name_objects_separate(self):
        fixture = DriveFixture([item("a", "A", directory=True), item("b", "B", directory=True),
            item("f", "知否.txt", "a"), item("g", "知否.txt", "b")])
        backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
        self.assertEqual(backend.locate(Entry("f", "知否.txt", False)).path, "/A/知否.txt")
        self.assertEqual(backend.locate(Entry("g", "知否.txt", False)).path, "/B/知否.txt")

    def test_malformed_names_are_not_published_as_safe_paths(self):
        with QuarkClient(COOKIE, transport=httpx.MockTransport(lambda _: response([item("f", "../bad")], 1))) as client:
            # Global search does not invent a path; existing download planning still rejects the name.
            found = []
            stream(Files(client), "0", "bad", False, lambda rows, _: found.extend(rows), lambda: False)
            self.assertEqual(found[0].path, "")


class IncrementalWindowTests(WindowCase):
    def test_same_keyword_older_completion_and_failure_are_ignored(self):
        window = self._window()
        window._search_var.set("知否")
        window.search()
        old = self._submitted(window)
        window.search()
        current = self._submitted(window)
        window._handle(SearchResults("0", "知否", (Entry("old", "知否-old", False),), old.request_id))
        window._handle(SearchEnded(old.request_id, "old error"))
        self.assertTrue(window._searching)
        self.assertEqual(window._rows, [])
        window._handle(SearchResults("0", "知否", (Entry("new", "知否-new", False),), current.request_id, False))
        self.assertEqual([r.fid for r in window._rows], ["new"])
        window._handle(SearchResults("0", "知否", (), current.request_id))
        self.assertFalse(window._searching)

    def test_failure_before_results_keeps_old_list_and_partial_failure_keeps_hits(self):
        window = self._window()
        self._listing(window, Entry("original", "知否-original", False))
        window._search_var.set("知否")
        window.search()
        request = self._submitted(window)
        window._handle(SearchEnded(request.request_id, "搜索失败"))
        self.assertEqual(window._rows[0].fid, "original")
        window.search()
        request = self._submitted(window)
        window._handle(SearchResults("0", "知否", (Entry("hit", "知否-hit", False),), request.request_id, False))
        window._handle(SearchEnded(request.request_id, "搜索失败"))
        self.assertEqual(window._rows[0].fid, "hit")
        self.assertIn("不完整", window._status_var.get())

    def test_cancel_releases_download_and_invalidates_late_page(self):
        window = self._window()
        window._search_var.set("知否")
        window.search()
        request = self._submitted(window)
        window._handle(SearchResults("0", "知否", (Entry("hit", "知否-hit", False),), request.request_id, False))
        window.cancel_search()
        window._handle(SearchResults("0", "知否", (Entry("late", "知否-late", False),), request.request_id, False))
        self.assertEqual([r.fid for r in window._rows], ["hit"])
        self.assertFalse(window._searching)

    def test_real_tk_large_render_yields_and_cancel_button_is_enabled(self):
        root = _tk_root()
        window = self._window()
        window._root = root
        window._build(_tkinter())
        try:
            window._search_var.set("知否")
            window.search()
            request = self._submitted(window)
            rows = tuple(Entry(str(i), f"知否{i}.bin", False) for i in range(10000))
            window._handle(SearchResults("0", "知否", rows, request.request_id, False))
            self.assertLess(len(window._tree.get_children()), len(rows))
            self.assertEqual(str(window._cancel_search_button["state"]), "normal")
            heartbeat = []
            root.after(0, lambda: heartbeat.append(time.monotonic()))
            start = time.monotonic()
            while not heartbeat and time.monotonic() - start < 1:
                root.update()
            self.assertTrue(heartbeat)
            self.assertLess(heartbeat[0] - start, .2)
            window._cancel_search_button.invoke()
            self.assertFalse(window._searching)
        finally:
            window._shutdown()
