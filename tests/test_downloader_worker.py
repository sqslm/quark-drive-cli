"""Offline tests for the downloader's worker thread and its library backend.

The worker is driven through its real queues; the backend is exercised against
``httpx.MockTransport`` fixtures, so no account and no network are involved.
"""

from __future__ import annotations

import hashlib
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli.downloader import worker as worker_module
from quark_cli.downloader.core import Entry
from quark_cli.downloader.worker import (
    Browse,
    Download,
    DriveBackend,
    Failed,
    Finished,
    Listing,
    Progress,
    Worker,
)
from quark_cli.errors import AuthenticationError, UsageError
from quark_cli.transfer_progress import ProgressEvent

from drive_fixture import DriveFixture, item

COOKIE = "sid=offline-cookie"


def _row(fid: str, name: str, *, directory: bool = False, size: int = 0) -> Entry:
    return Entry(fid, name, directory, None if directory else size)


class FakeBackend:
    """Scripted :class:`~quark_cli.downloader.worker.Backend` for the worker tests."""

    def __init__(self, *, listing=(), listing_error=None, result=None, error=None, gate=None):
        self.listing_rows = list(listing)
        self.listing_error = listing_error
        self.result = result
        self.error = error
        self.gate = gate
        self.downloads = []
        self._lock = threading.Lock()

    def listing(self, fid):
        with self._lock:
            if self.listing_error is not None:
                raise self.listing_error
        return list(self.listing_rows)

    def download(self, request, progress):
        self.downloads.append(request)
        if self.gate is not None:
            self.gate.wait(5.0)
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


class SingleFileFixture:
    """Serves ``file/download`` plus the storage response for one file."""

    def __init__(self, body: bytes = b"download bytes", *, name: str = "报告.PDF", fid: str = "fid-1"):
        self.body, self.name, self.fid = body, name, fid
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == "drive-pc.quark.cn":
            return httpx.Response(
                200,
                json={
                    "status": 200,
                    "code": 0,
                    "data": {"list": [{"fid": self.fid, "file_name": self.name, "size": len(self.body), "download_url": "https://cdn.quark.cn/bytes"}]},
                },
                request=request,
            )
        if request.url.host == "cdn.quark.cn":
            return httpx.Response(200, content=self.body, request=request)
        return httpx.Response(404, request=request)

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

    def test_download_of_one_file_installs_it_under_its_remote_name(self):
        fixture = SingleFileFixture()
        target = self.output / "报告.PDF"
        events: list[ProgressEvent] = []
        result = DriveBackend(lambda: COOKIE, transport=fixture.transport()).download(
            Download(fid=fixture.fid, target=str(target), recursive=False, overwrite=False, name=fixture.name),
            events.append,
        )
        self.assertEqual(target.read_bytes(), fixture.body)
        self.assertEqual(result["size"], len(fixture.body))
        self.assertTrue(events)
        self.assertEqual(events[-1].phase, "completed")
        self.assertIsNone(fixture.calls[-1].headers.get("cookie"))

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
        with patch.object(worker_module, "Transfer", lambda client: stub):
            result = DriveBackend(lambda: COOKIE, transport=fixture.transport()).download(
                Download(fid="remote-root", target=str(target), recursive=True, overwrite=False, name="下载源"),
                None,
            )
        self.assertTrue(result["recursive"])
        self.assertEqual((result["files"], result["directories"]), (1, 2))
        self.assertEqual((target / "中文 文件.txt").read_bytes(), payload)
        self.assertTrue((target / "空目录").is_dir())
        self.assertEqual([(fid, path) for fid, path, _ in stub.downloads], [("remote-file", target / "中文 文件.txt")])
        self.assertFalse(stub.downloads[0][2]["overwrite"])


if __name__ == "__main__":
    unittest.main()
