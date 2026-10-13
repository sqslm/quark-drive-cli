"""GUI rate budgets reach every source and remain shared across range readers."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quark_cli.client import QuarkClient
from quark_cli.downloader.core import DOWNLOAD_ACTION, Entry, download_limit_rate
from quark_cli.downloader.worker import Download, DriveBackend, Failed, Finished
from quark_cli.errors import UsageError
from quark_cli.transfer import Transfer
from quark_cli.transfer_progress import RateLimiter
from tests import test_downloader_tasks as task_helpers
from tests.test_downloader_window import WindowCase, _FakeButton, _FakeVar
from tests.test_downloader_share import COOKIE, PASSCODE, SHARE_URL, ShareDownloadFixture
from tests.test_parallel_resume import RangeSource
import httpx


class RateInputTests(unittest.TestCase):
    def test_rates_convert_mib_to_bytes_and_reject_invalid_values(self):
        self.assertIsNone(download_limit_rate("0"))
        self.assertEqual(download_limit_rate("0.5"), 524288)
        self.assertEqual(download_limit_rate(" 8 "), 8388608)
        for value in ("", " ", "nan", "inf", "-1", "text", "0.001", "10241"):
            with self.subTest(value=value), self.assertRaises(UsageError):
                download_limit_rate(value)


class RateWindowTests(WindowCase):
    _task_window = task_helpers.TaskTests._task_window
    _begin = task_helpers.TaskTests._begin
    _requests = staticmethod(task_helpers.TaskTests._requests)

    def test_rate_is_frozen_per_batch_and_retry_uses_current_value(self):
        with tempfile.TemporaryDirectory() as folder:
            window = self._task_window()
            window._limit_rate_var = _FakeVar("0.5")
            window._limit_rate_input = _FakeButton()
            self._begin(window, folder, "a.bin", "b.bin")
            self.assertEqual(self._requests(window)[0].limit_rate, 524288)
            self.assertEqual(window._limit_rate_input.state, "disabled")
            window._limit_rate_var.set("2")
            window._handle(Failed(DOWNLOAD_ACTION, "failed"))
            self.assertEqual(self._requests(window)[0].limit_rate, 524288)
            window._handle(Finished(DOWNLOAD_ACTION, "done"))
            window.retry_failed()
            self.assertEqual(self._requests(window)[0].limit_rate, 2097152)

    def test_invalid_rate_blocks_start_and_keeps_failed_records_for_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            window = self._task_window()
            window._output_dir = folder
            window._adopt_rows([Entry("f", "f.bin", False)])
            window._checked = {"f"}
            window._limit_rate_var = _FakeVar("nan")
            window.start_download()
            self.assertEqual(self._requests(window), [])
            self.assertIn("限速请输入", window._status_var.get())
            window._limit_rate_var.set("0")
            window.start_download()
            self.assertIsNone(self._requests(window)[0].limit_rate)
            window._handle(Failed(DOWNLOAD_ACTION, "failed"))
            window._limit_rate_var.set("-1")
            window.retry_failed()
            self.assertEqual(self._requests(window), [])
            self.assertEqual(window._tasks[0].state, "failed")


class RateBackendTests(unittest.TestCase):
    def test_shared_folder_uses_the_recursive_budget_and_cleans_temporary_copy(self):
        from quark_cli.downloader.share import ShareSource

        fixture = ShareDownloadFixture()
        rate = 524288
        now = [0.0]
        limiter = RateLimiter(rate, clock=lambda: now[0], sleep=lambda duration: now.__setitem__(0, now[0] + duration))
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"QUARK_CONFIG_DIR": folder}):
            backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
            target = Path(folder) / "资料"
            request = Download("share-dir", str(target), True, False, "资料",
                               ShareSource(SHARE_URL, PASSCODE), limit_rate=rate)
            with patch("quark_cli.transfer_progress.RateLimiter", return_value=limiter) as factory:
                backend.download(request, None)
            factory.assert_called_once_with(rate)
            self.assertEqual((target / "内部.txt").read_bytes(), fixture.payload)
        self.assertEqual(limiter.transferred, len(fixture.payload))
        self.assertAlmostEqual(now[0], len(fixture.payload) / rate)
        self.assertEqual(fixture.account, [])
        self.assertEqual(fixture.recycle, [{"record_id": "keep-1", "fid": "someone-else"}])

    def test_invalid_share_rate_is_rejected_before_cloud_requests(self):
        from quark_cli.downloader.share import ShareSource

        with tempfile.TemporaryDirectory() as folder:
            for rate in (0, -1, True):
                fixture = ShareDownloadFixture()
                backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
                request = Download("share-note", str(Path(folder) / "报告[1].txt"), False, False, "报告[1].txt",
                                   ShareSource(SHARE_URL, PASSCODE), limit_rate=rate)
                with self.assertRaises(UsageError):
                    backend.download(request, None)
                self.assertEqual(fixture.requests, [])

    def test_parallel_payload_uses_one_budget_not_one_limit_per_connection(self):
        source = RangeSource(delay=0.01)
        now = [0.0]
        limiter = RateLimiter(262144, clock=lambda: now[0], sleep=lambda duration: now.__setitem__(0, now[0] + duration))
        with tempfile.TemporaryDirectory() as folder:
            with QuarkClient(COOKIE, transport=httpx.MockTransport(source)) as client, \
                    patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MIN_SIZE", 4096), \
                    patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MIN_CHUNK", 4096), \
                    patch("quark_cli.transfer._limiter", return_value=limiter) as factory:
                target = Path(folder) / "test.bin"
                Transfer(client, state_dir=Path(folder) / "state").download(
                    "f", target, resume=True, connections=4, limit_rate=262144)
                factory.assert_called_once_with(262144, None)
                self.assertEqual(target.read_bytes(), source.body)
        self.assertGreaterEqual(source.peak, 2)
        self.assertLessEqual(source.peak, 4)
        self.assertEqual(limiter.transferred, len(source.body))
        self.assertAlmostEqual(now[0], len(source.body) / 262144)

    def test_backend_forwards_budget_to_file_folder_and_share_and_cleanup_still_runs(self):
        from quark_cli.downloader.share import ShareSource

        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"QUARK_CONFIG_DIR": folder}):
            for recursive in (False, True):
                fixture = ShareDownloadFixture()
                backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
                request = Download("f", str(Path(folder) / "normal"), recursive, False, "normal", limit_rate=524288)
                destination = "quark_cli.downloader.worker.RecursiveTransfer.download_tree" if recursive else \
                              "quark_cli.downloader.worker.Transfer.download"
                with patch(destination, return_value={"size": 0}) as download:
                    backend.download(request, None)
                self.assertEqual(download.call_args.kwargs["limit_rate"], 524288)
            fixture = ShareDownloadFixture()
            backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
            request = Download("share-note", str(Path(folder) / "报告[1].txt"), False, False, "报告[1].txt",
                               ShareSource(SHARE_URL, PASSCODE), limit_rate=524288)
            now = [0.0]
            limiter = RateLimiter(524288, clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
            with patch("quark_cli.transfer._limiter", return_value=limiter) as factory:
                backend.download(request, None)
            factory.assert_called_once_with(524288, None)
            self.assertEqual(fixture.account, [])
            self.assertEqual(fixture.recycle, [{"record_id": "keep-1", "fid": "someone-else"}])
            self.assertEqual(limiter.transferred, len(fixture.payload))
