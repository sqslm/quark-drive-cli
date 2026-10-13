"""Bounded range reads, contiguous checkpoints and real stop/resume."""
import hashlib
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli.client import QuarkClient
from quark_cli.errors import LocalIOError, RemoteError
from quark_cli.transfer import DownloadStopped, Transfer
from quark_cli.transfer_state import TransferLedger


class RangeSource:
    def __init__(self, body=None, delay=0.01):
        self.body = body or bytes(range(256)) * 256
        self.delay = delay
        self.requests = []
        self.active = self.peak = 0
        self.lock = threading.Lock()
        self.ignore = self.no_etag = False
        self.bad_start = None

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "drive-pc.quark.cn":
            if request.url.path.endswith("/info"):
                data = {"fid": "f", "file_name": "test.bin", "pdir_fid": "0",
                        "file_type": 1, "size": len(self.body), "updated_at": 123}
            else:
                data = {"list": [{"fid": "f", "size": len(self.body),
                                  "download_url": "https://cdn.quark.cn/test"}]}
            return httpx.Response(200, json={"status": 200, "code": 0, "data": data})
        assert "cookie" not in request.headers
        start, end = 0, len(self.body) - 1
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", request.headers.get("range", ""))
        if match and not self.ignore:
            start = int(match[1])
            end = int(match[2]) if match[2] else end
        headers = {} if self.no_etag else {"etag": '"v1"'}
        if start == self.bad_start:
            headers["etag"] = '"changed"'
        if match and not self.ignore:
            headers["content-range"] = f"bytes {start}-{end}/{len(self.body)}"
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if end > start:
                time.sleep(self.delay * (2 if start == 0 else 1))
        finally:
            with self.lock:
                self.active -= 1
        return httpx.Response(206 if match and not self.ignore else 200,
                              content=self.body[start:end + 1], headers=headers)


class ParallelResumeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for name, value in (("PARALLEL_DOWNLOAD_MIN_SIZE", 8192),
                            ("PARALLEL_DOWNLOAD_MIN_CHUNK", 4096)):
            p = patch(f"quark_cli.transfer.{name}", value)
            p.start()
            self.addCleanup(p.stop)

    def download(self, source, **options):
        with QuarkClient("sid=offline", transport=httpx.MockTransport(source)) as client:
            return Transfer(client, state_dir=self.root / "state").download(
                "f", self.root / "test.bin", resume=True, **options)

    def ledger(self):
        return json.loads(next((self.root / "state").glob("download-*.json")).read_text())

    def test_ranges_overlap_within_the_limit_and_publish_exact_bytes(self):
        source = RangeSource()
        events = []
        result = self.download(source, connections=4, progress=events.append)
        self.assertGreaterEqual(source.peak, 2)
        self.assertLessEqual(source.peak, 4)
        self.assertEqual((self.root / "test.bin").read_bytes(), source.body)
        self.assertEqual(result["sha256"], hashlib.sha256(source.body).hexdigest())
        self.assertEqual(self.ledger()["phase"], "completed")
        self.assertTrue(any(event.connections > 1 for event in events))

    def test_completed_download_removed_locally_starts_fresh(self):
        base = self.root
        for connections in (1, 4):
            with self.subTest(connections=connections):
                self.root = base / str(connections)
                self.root.mkdir()
                source = RangeSource()
                self.download(source, connections=connections)
                (self.root / 'test.bin').unlink()
                split = len(source.requests)
                result = self.download(source, connections=connections)
                self.assertFalse(result['resumed'])
                self.assertEqual((self.root / 'test.bin').read_bytes(), source.body)
                self.assertEqual(result['sha256'], hashlib.sha256(source.body).hexdigest())
                requests = [r for r in source.requests[split:] if r.url.host == 'cdn.quark.cn']
                self.assertTrue(requests)
                self.assertTrue(any(r.headers.get('range','bytes=0-').startswith('bytes=0-') for r in requests))

    def test_missing_unfinished_part_still_refuses_resume(self):
        source = RangeSource()
        stopped = threading.Event()
        def progress(event):
            if event.confirmed >= 4096:
                stopped.set()
        with self.assertRaises(DownloadStopped):
            self.download(source, connections=4, progress=progress, should_stop=stopped.is_set)
        before = self.ledger()
        next(self.root.glob('.quark-*.part')).unlink()
        with self.assertRaisesRegex(LocalIOError,'下载检查点数据文件缺失'):
            self.download(source, connections=4)
        self.assertEqual(self.ledger(), before)

    def test_stop_keeps_only_a_contiguous_prefix_and_resumes_with_another_limit(self):
        source = RangeSource()
        stop = threading.Event()

        def progress(event):
            if event.confirmed >= 4096:
                stop.set()

        with self.assertRaises(DownloadStopped):
            self.download(source, connections=4, progress=progress, should_stop=stop.is_set)
        data = self.ledger()
        part = next(self.root.glob(".quark-*.part"))
        self.assertEqual(part.read_bytes(), source.body[:data["offset"]])
        self.assertGreater(data["offset"], 0)
        self.assertFalse((self.root / "test.bin").exists())
        split = len(source.requests)
        self.download(source, connections=2)
        ranges = [r.headers.get("range") for r in source.requests[split:] if r.url.host == "cdn.quark.cn"]
        self.assertTrue(any(value.startswith(f"bytes={data['offset']}-") for value in ranges))
        self.assertEqual((self.root / "test.bin").read_bytes(), source.body)

    def test_changed_segment_is_not_published_and_threads_have_ended(self):
        source = RangeSource()
        source.bad_start = 4096
        with self.assertRaisesRegex(RemoteError, "ETag"):
            self.download(source, connections=4)
        self.assertFalse((self.root / "test.bin").exists())
        self.assertEqual(source.active, 0)
        data = self.ledger()
        parts = list(self.root.glob(".quark-*.part"))
        if parts:
            self.assertEqual(parts[0].read_bytes(), source.body[:data["offset"]])

    def test_ignored_ranges_use_a_single_stream(self):
        source = RangeSource()
        source.ignore = True
        self.download(source, connections=4)
        self.assertEqual(source.peak, 1)
        self.assertEqual((self.root / "test.bin").read_bytes(), source.body)

    def test_unversioned_fallback_stops_without_a_false_checkpoint_then_restarts(self):
        source = RangeSource()
        source.no_etag = True
        stop = threading.Event()
        with self.assertRaises(DownloadStopped) as caught:
            self.download(source, connections=4, should_stop=stop.is_set,
                          progress=lambda event: stop.set() if event.phase == "non_resumable" else None)
        self.assertFalse(caught.exception.resume_available)
        self.assertEqual(self.ledger()["offset"], 0)
        self.assertFalse(list(self.root.glob(".quark-*.part")))
        result = self.download(source, connections=4)
        self.assertFalse(result["resumable"])
        self.assertEqual((self.root / "test.bin").read_bytes(), source.body)

    def test_old_serial_checkpoint_can_continue_with_parallel_ranges(self):
        source = RangeSource(body=b"01234567" * (2 * 1024 * 1024 // 8))
        stop = threading.Event()
        with self.assertRaises(DownloadStopped):
            self.download(source, connections=1, should_stop=stop.is_set,
                          progress=lambda event: stop.set() if event.confirmed else None)
        self.assertEqual(self.ledger()["offset"], 1024 * 1024)
        self.download(source, connections=4)
        self.assertEqual((self.root / "test.bin").read_bytes(), source.body)

    def test_truncated_range_keeps_a_valid_prefix_and_never_publishes(self):
        source = RangeSource()
        original = source.__call__

        def truncated(request):
            response = original(request)
            if request.headers.get("range") == "bytes=4096-8191":
                return httpx.Response(206, content=response.content[:-1],
                                      headers={"etag": '"v1"', "content-range": f"bytes 4096-8191/{len(source.body)}"})
            return response

        with self.assertRaises(RemoteError):
            self.download(truncated, connections=4)
        self.assertFalse((self.root / "test.bin").exists())
        self.assertEqual(source.active, 0)

    def test_a_failed_ledger_write_leaves_an_unconfirmed_tail_that_is_discarded(self):
        source = RangeSource()
        persist = TransferLedger.persist

        def fail(ledger, data):
            if data.get("offset") == 4096:
                raise LocalIOError("offline ledger failure")
            return persist(ledger, data)

        with patch.object(TransferLedger, "persist", fail), self.assertRaises(LocalIOError):
            self.download(source, connections=4)
        self.assertEqual(self.ledger()["offset"], 0)
        self.assertFalse((self.root / "test.bin").exists())
        self.download(source, connections=4)
        self.assertEqual((self.root / "test.bin").read_bytes(), source.body)

    def test_the_window_backend_forwards_the_selected_connection_limit(self):
        from quark_cli.downloader.worker import Download, DriveBackend

        source = RangeSource()
        with patch.dict("os.environ", {"QUARK_CONFIG_DIR": str(self.root / "config")}):
            result = DriveBackend(lambda: "sid=offline", transport=httpx.MockTransport(source)).download(
                Download("f", str(self.root / "test.bin"), False, False, "test.bin", connections=2), None)
        self.assertEqual(result["sha256"], hashlib.sha256(source.body).hexdigest())
        self.assertEqual(source.peak, 2)
