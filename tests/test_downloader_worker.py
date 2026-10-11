"""Offline tests for the downloader's worker thread and its library backend.

The worker is driven through its real queues; the backend is exercised against
``httpx.MockTransport`` fixtures, so no account and no network are involved.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli.downloader import worker as worker_module
from quark_cli.downloader.core import DOWNLOAD_ACTION, Entry
from quark_cli.downloader.worker import (
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
from quark_cli.errors import AuthenticationError, UsageError
from quark_cli.transfer import DownloadStopped
from quark_cli.transfer_progress import ProgressEvent

from drive_fixture import DriveFixture, item

COOKIE = "sid=offline-cookie"


def _row(fid: str, name: str, *, directory: bool = False, size: int = 0) -> Entry:
    return Entry(fid, name, directory, None if directory else size)


class FakeBackend:
    """Scripted :class:`~quark_cli.downloader.worker.Backend` for the worker tests.

    It honors ``should_stop`` the way the library does: a stop asked for before
    the first byte ends the request at the checkpoint the library would have
    persisted, without a progress event and without publishing anything.
    """

    def __init__(self, *, listing=(), listing_error=None, result=None, error=None, gate=None):
        self.listing_rows = list(listing)
        self.listing_error = listing_error
        self.result = result
        self.error = error
        self.gate = gate
        self.downloads = []
        self.stops = []
        self._lock = threading.Lock()

    def listing(self, fid):
        with self._lock:
            if self.listing_error is not None:
                raise self.listing_error
        return list(self.listing_rows)

    def download(self, request, progress, should_stop=None):
        self.downloads.append(request)
        self.stops.append(should_stop)
        if self.gate is not None:
            self.gate.wait(5.0)
        if should_stop is not None and should_stop():
            raise DownloadStopped("下载已停止：已保留安全检查点，再次提交可继续")
        if progress is not None:
            progress(ProgressEvent("download", "completed", request.name, 10, 10, 10, 1048576.0))
        if self.error is not None:
            raise self.error
        return self.result


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.workers: list[Worker] = []
        self.addCleanup(self._stop_workers)

    def _stop_workers(self):
        for worker in self.workers:
            worker.stop()

    def _worker(self, backend: FakeBackend) -> Worker:
        worker = Worker(backend)
        self.workers.append(worker)
        worker.start()
        return worker

    @staticmethod
    def _download_request() -> Download:
        return Download(fid="fid-1", target="out.bin", recursive=False, overwrite=False, name="报告.PDF")

    def test_browse_returns_one_listing_event(self):
        rows = [_row("d", "目录", directory=True), _row("f", "f.bin", size=4)]
        worker = self._worker(FakeBackend(listing=rows))
        worker.submit(Browse("0"))
        event = worker.events.get(timeout=5)
        self.assertIsInstance(event, Listing)
        self.assertEqual((event.fid, event.entries), ("0", tuple(rows)))

    def test_browse_failure_is_one_line_and_the_worker_keeps_serving(self):
        backend = FakeBackend(listing_error=RuntimeError("boom secret=private"))
        worker = self._worker(backend)
        worker.submit(Browse("0"))
        failed = worker.events.get(timeout=5)
        self.assertIsInstance(failed, Failed)
        self.assertEqual(failed.action, "浏览")
        self.assertEqual(failed.message, "浏览失败：发生未预期的错误")
        self.assertNotIn("private", failed.message)

        backend.listing_error = None
        backend.listing_rows = [_row("f", "f.bin", size=1)]
        worker.submit(Browse("0"))
        self.assertIsInstance(worker.events.get(timeout=5), Listing)

    def test_download_reports_progress_then_the_completion_line(self):
        backend = FakeBackend(result={"fid": "fid-1", "path": "out.bin", "size": 10})
        worker = self._worker(backend)
        worker.submit(self._download_request())

        progress = worker.events.get(timeout=5)
        self.assertIsInstance(progress, Progress)
        self.assertEqual(progress.text, "报告.PDF：10 B / 10 B · 1.0 MiB/s")
        self.assertEqual(progress.fraction, 1.0)

        finished = worker.events.get(timeout=5)
        self.assertIsInstance(finished, Finished)
        self.assertEqual(finished.summary, "已下载 报告.PDF：10 B")
        self.assertEqual(len(backend.downloads), 1)
        self.assertFalse(backend.downloads[0].overwrite)

    def test_download_failure_reports_the_library_conflict(self):
        backend = FakeBackend(error=UsageError("目标文件已存在；使用 --overwrite 显式覆盖"))
        worker = self._worker(backend)
        worker.submit(self._download_request())
        worker.events.get(timeout=5)  # progress
        failed = worker.events.get(timeout=5)
        self.assertIsInstance(failed, Failed)
        self.assertEqual(failed.message, "下载失败：目标文件已存在；使用 --overwrite 显式覆盖")

    def test_a_stop_request_ends_the_download_as_a_stop_and_the_next_batch_runs(self):
        backend = FakeBackend(result={"fid": "fid-1", "path": "out.bin", "size": 10})
        worker = self._worker(backend)

        worker.request_stop()
        worker.submit(self._download_request())
        stopped = worker.events.get(timeout=5)
        self.assertIsInstance(stopped, Stopped)
        self.assertEqual(stopped.action, DOWNLOAD_ACTION)
        self.assertIn("检查点", stopped.message)
        self.assertFalse(backend.downloads[0].overwrite)

        # The stop belongs to the batch it was asked for; the next batch clears
        # it before its first item, so the same request can be submitted again.
        worker.clear_stop()
        self.assertFalse(backend.stops[0](), "the hook is live, not a frozen copy")
        worker.submit(self._download_request())
        events = [worker.events.get(timeout=5) for _ in range(2)]
        self.assertIsInstance(events[0], Progress)
        self.assertIsInstance(events[1], Finished)
        self.assertEqual(events[1].summary, "已下载 报告.PDF：10 B")

    def test_the_library_is_handed_the_live_stop_hook_of_every_download(self):
        backend = FakeBackend(result={"fid": "fid-1", "path": "out.bin", "size": 10})
        worker = self._worker(backend)
        worker.submit(self._download_request())
        self.assertIsInstance(worker.events.get(timeout=5), Progress)

        hook = backend.stops[0]
        self.assertIsNotNone(hook, "a download without a stop hook could not be stopped")
        self.assertFalse(hook())
        worker.request_stop()
        self.assertTrue(hook(), "the hook read at the checkpoint must see the stop request")

    def test_a_missing_credential_becomes_the_cli_guidance_line(self):
        worker = self._worker(DriveBackend(lambda: ""))
        worker.submit(Browse("0"))
        failed = worker.events.get(timeout=5)
        self.assertEqual(failed.message, "浏览失败：请设置 QUARK_COOKIE 或 QUARK_COOKIE_FILE")

    def test_a_running_download_is_never_overtaken_by_a_browse(self):
        gate = threading.Event()
        backend = FakeBackend(result={"fid": "fid-1", "path": "out.bin", "size": 10}, gate=gate)
        worker = self._worker(backend)
        worker.submit(self._download_request())

        deadline = time.monotonic() + 5
        while not backend.downloads and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(backend.downloads, "download never started")

        worker.submit(Browse("0"))
        with self.assertRaises(queue.Empty):
            worker.events.get_nowait()

        gate.set()
        events = [worker.events.get(timeout=5) for _ in range(3)]
        self.assertIsInstance(events[0], Progress)
        self.assertIsInstance(events[1], Finished)
        self.assertIsInstance(events[2], Listing)

    def test_stop_joins_the_daemon_thread_and_is_idempotent(self):
        worker = Worker(FakeBackend())
        worker.stop()  # never started
        worker.start()
        thread = worker._thread
        self.assertTrue(thread.daemon)
        worker.stop()
        self.assertFalse(thread.is_alive())
        self.assertIsNone(worker._thread)
        worker.stop()

    def test_start_is_idempotent(self):
        worker = self._worker(FakeBackend(listing=[_row("f", "f.bin", size=1)]))
        first = worker._thread
        worker.start()
        self.assertIs(worker._thread, first)


class _Pieces(httpx.SyncByteStream):
    """Yields one response body in pieces, so a stop can land mid-body."""

    def __init__(self, body: bytes, chunk: int) -> None:
        self.body, self.chunk = body, chunk

    def __iter__(self):
        for start in range(0, len(self.body), self.chunk):
            yield self.body[start:start + self.chunk]


class MediaFixture:
    """The real download path for one file, over ``httpx.MockTransport``.

    ``file/download`` hands out the storage URL, ``file/info`` the remote identity
    a resume ledger binds to, and the storage host answers with a strong ETag and
    real ``Range`` support, exactly like the service the library talks to.
    ``chunk`` sizes the response body pieces, so a test can stop a transfer at a
    known checkpoint.
    """

    def __init__(self, payload: bytes = b"download bytes", *, name: str = "报告.PDF", fid: str = "fid-1",
                 chunk: int | None = None, etag: str | None = '"stable-version"'):
        self.payload, self.name, self.fid = payload, name, fid
        self.chunk, self.etag = chunk or max(1, len(payload)), etag
        self.calls: list[httpx.Request] = []
        self.body_requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == "drive-pc.quark.cn":
            if request.url.path.endswith("/file/info"):
                return httpx.Response(
                    200,
                    json={
                        "status": 200,
                        "code": 0,
                        "data": {
                            "fid": self.fid,
                            "file_name": self.name,
                            "pdir_fid": "0",
                            "file_type": 1,
                            "size": len(self.payload),
                            "updated_at": 123,
                        },
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "status": 200,
                    "code": 0,
                    "data": {"list": [{"fid": self.fid, "file_name": self.name, "size": len(self.payload),
                                       "download_url": "https://cdn.quark.cn/bytes"}]},
                },
                request=request,
            )
        if request.url.host != "cdn.quark.cn":
            return httpx.Response(404, request=request)
        self.body_requests.append(request)
        headers = {"etag": self.etag} if self.etag is not None else {}
        total = len(self.payload)
        range_header = request.headers.get("range")
        if range_header is not None and request.headers.get("if-range", self.etag) == self.etag:
            match = re.fullmatch(r"bytes=([0-9]+)-([0-9]*)", range_header)
            start = int(match.group(1)) if match is not None else total
            if start >= total:
                return httpx.Response(
                    416, headers={**headers, "content-range": f"bytes */{total}"}, request=request
                )
            end = int(match.group(2)) if match is not None and match.group(2) else total - 1
            return httpx.Response(
                206,
                stream=_Pieces(self.payload[start:end + 1], self.chunk),
                headers={**headers, "content-range": f"bytes {start}-{end}/{total}"},
                request=request,
            )
        return httpx.Response(200, stream=_Pieces(self.payload, self.chunk), headers=headers, request=request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class StubTransfer:
    """Records the per-file downloads a real :class:`RecursiveTransfer` performs."""

    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads
        self.downloads: list[tuple[str, Path, dict]] = []

    def download(self, fid, output, **options):
        output = Path(output)
        self.downloads.append((fid, output, options))
        output.write_bytes(self.payloads[fid])
        callback = options.get("progress")
        if callback is not None:
            size = len(self.payloads[fid])
            callback(ProgressEvent("download", "completed", output.name, size, size, size, 1.0))
        return {"fid": fid, "path": str(output), "size": output.stat().st_size}


class DriveBackendTests(unittest.TestCase):
    def setUp(self):
        self._config = tempfile.TemporaryDirectory()
        self.addCleanup(self._config.cleanup)
        self._environment = patch.dict(os.environ, {"QUARK_CONFIG_DIR": self._config.name})
        self._environment.start()
        self.addCleanup(self._environment.stop)
        self._output = tempfile.TemporaryDirectory()
        self.addCleanup(self._output.cleanup)
        self.output = Path(self._output.name)

    def test_listing_reads_the_library_pages_and_sorts_the_rows(self):
        backend = DriveBackend(lambda: COOKIE, transport=DriveFixture().transport())
        rows = backend.listing("0")
        self.assertEqual([row.name for row in rows], ["范围", "范围外"])
        self.assertTrue(all(row.directory for row in rows))
        self.assertEqual(
            [(row.name, row.directory, row.size_label) for row in backend.listing("child")],
            [("报告.PDF", False, "4 B")],
        )

    def test_listing_without_a_credential_reports_the_cli_guidance(self):
        backend = DriveBackend(lambda: "", transport=DriveFixture().transport())
        with self.assertRaises(AuthenticationError) as caught:
            backend.listing("0")
        self.assertIn("QUARK_COOKIE", str(caught.exception))

    def _request(self, fid: str, target: Path, *, recursive: bool = False, name: str) -> Download:
        return Download(fid=fid, target=str(target), recursive=recursive, overwrite=False, name=name)

    def _state_files(self, pattern: str) -> list[Path]:
        """The library's durable ledger files under ``QUARK_CONFIG_DIR``."""
        return sorted((Path(self._config.name) / "transfers").glob(pattern))

    def _local_parts(self) -> list[Path]:
        """The library's part file, which it keeps beside the output file."""
        return sorted(self.output.glob(".quark-*.part"))

    def _storage_requests(self, fixture: MediaFixture) -> list[httpx.Request]:
        """The requests that carried the payload, i.e. the ones a CDN sees."""
        return [request for request in fixture.calls if request.url.host == "cdn.quark.cn"]

    def test_download_of_one_file_installs_it_under_its_remote_name(self):
        fixture = MediaFixture()
        target = self.output / "报告.PDF"
        events: list[ProgressEvent] = []
        result = DriveBackend(lambda: COOKIE, transport=fixture.transport()).download(
            self._request(fixture.fid, target, name=fixture.name),
            events.append,
        )
        self.assertEqual(target.read_bytes(), fixture.payload)
        self.assertEqual(result["size"], len(fixture.payload))
        # The window's downloads always run with a checkpoint, so an untouched
        # re-selection can be resumed rather than restarted.
        self.assertEqual(result["resumed"], False)
        self.assertEqual(result["sha256"], hashlib.sha256(fixture.payload).hexdigest())
        self.assertTrue(events)
        self.assertEqual(events[-1].phase, "completed")
        # The payload host never sees the session credential; the private
        # file/info and file/download calls this API needs still do.
        for storage in self._storage_requests(fixture):
            self.assertIsNone(storage.headers.get("cookie"))
        ledger = json.loads(self._state_files("download-*.json")[0].read_text("utf-8"))
        self.assertEqual(ledger["phase"], "completed")
        self.assertEqual(self._local_parts(), [], "a published download keeps no part file")

    def test_a_stop_ends_a_real_transfer_at_its_checkpoint_and_the_next_attempt_continues(self):
        # The payload is a whole number of stream chunks, so the library confirms
        # and persists a chunk before it is asked about stopping: the stop then
        # lands on a durable checkpoint instead of inside a chunk.
        payload = b"0123456789abcdef" * (2 * 1024 * 1024 // 16)
        fixture = MediaFixture(payload, chunk=8192)
        target = self.output / "报告.PDF"
        request = self._request(fixture.fid, target, name=fixture.name)
        stop = threading.Event()
        transferred: list[int] = []

        def on_progress(event: ProgressEvent) -> None:
            if event.transferred:
                transferred.append(event.transferred)
                stop.set()

        backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
        with self.assertRaises(DownloadStopped):
            backend.download(request, on_progress, stop.is_set)

        ledger = json.loads(self._state_files("download-*.json")[0].read_text("utf-8"))
        confirmed = ledger["offset"]
        self.assertTrue(transferred, "the stop must be asked for from a progress event")
        self.assertGreaterEqual(confirmed, transferred[0], "the stop may not cut a confirmed chunk")
        self.assertLessEqual(confirmed, len(payload) // 2, "the stop must land mid-file, not past it")
        self.assertFalse(target.exists(), "a stopped transfer must not publish a partial target")
        parts = self._local_parts()
        self.assertEqual(len(parts), 1)
        # The part holds exactly the bytes the ledger has confirmed, so the next
        # attempt can trust the checkpoint it reads back.
        self.assertEqual(parts[0].stat().st_size, confirmed)
        self.assertEqual(parts[0].read_bytes(), payload[:confirmed])
        self.assertEqual((ledger["phase"], ledger["etag"]), ("streaming", fixture.etag))
        self.assertEqual(ledger["prefix_sha256"], hashlib.sha256(payload[:confirmed]).hexdigest())
        self.assertEqual(fixture.body_requests[0].headers.get("range"), None)

        # The same selection and the same output folder continue from there.
        resumed = DriveBackend(lambda: COOKIE, transport=fixture.transport()).download(request, None)
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(target.read_bytes(), payload)
        second = fixture.body_requests[1]
        self.assertEqual(second.headers.get("range"), f"bytes={confirmed}-")
        self.assertEqual(second.headers.get("if-range"), fixture.etag)
        self.assertIsNone(second.headers.get("cookie"), "a resumed payload request stays credential-free")
        ledger = json.loads(self._state_files("download-*.json")[0].read_text("utf-8"))
        self.assertEqual(ledger["phase"], "completed")
        self.assertEqual(self._local_parts(), [])

    def test_a_directory_request_uses_the_recursive_download_path(self):
        payload = "公开测试".encode()
        fixture = DriveFixture(
            [
                item("remote-root", "下载源", "0", directory=True),
                item("remote-empty", "空目录", "remote-root", directory=True),
                item("remote-file", "中文 文件.txt", "remote-root", size=len(payload), updated_at=123, md5=hashlib.md5(payload).hexdigest()),
            ]
        )
        stub = StubTransfer({"remote-file": payload})
        target = self.output / "下载源"
        stop = threading.Event()
        with patch.object(worker_module, "Transfer", lambda client: stub):
            result = DriveBackend(lambda: COOKIE, transport=fixture.transport()).download(
                self._request("remote-root", target, recursive=True, name="下载源"),
                None,
                stop.is_set,
            )
        self.assertTrue(result["recursive"])
        self.assertEqual((result["files"], result["directories"]), (1, 2))
        self.assertEqual((target / "中文 文件.txt").read_bytes(), payload)
        self.assertTrue((target / "空目录").is_dir())
        self.assertEqual([(fid, path) for fid, path, _ in stub.downloads], [("remote-file", target / "中文 文件.txt")])
        # A tree download is submitted with the same resumable, stoppable rules
        # as a single file: one hook per file, forwarded by the tree walk.
        options = stub.downloads[0][2]
        self.assertFalse(options["overwrite"])
        self.assertTrue(options["resume"])
        # A bound method is a fresh object on every read, so identity says
        # nothing here: what matters is that the live stop event itself was
        # forwarded, so setting it ends the file in flight.
        self.assertEqual(options["should_stop"], stop.is_set)
        self.assertIs(options["should_stop"].__self__, stop)
        stop.set()
        self.assertTrue(options["should_stop"](), "the forwarded hook must see the stop request")


if __name__ == "__main__":
    unittest.main()
