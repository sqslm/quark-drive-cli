import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli.auth import get_account_nickname
from quark_cli.downloader.worker import AccountProfile, Browse, Listing, LoadAccountProfile, Worker
from quark_cli.errors import RemoteError


class AccountProfileTests(unittest.TestCase):
    def test_fixed_account_host_returns_only_the_nickname_and_saves_no_credentials(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"data": {"nickname": "  测试用户  ", "token": "private"}})

        with tempfile.TemporaryDirectory() as root, patch.dict("os.environ", {"QUARK_CONFIG_DIR": root}):
            self.assertEqual(get_account_nickname("sid=offline", transport=httpx.MockTransport(handler)), "测试用户")
            self.assertEqual(list(Path(root).iterdir()), [])
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "https://pan.quark.cn/account/info?fr=pc&platform=pc")
        self.assertEqual(requests[0].headers["cookie"], "sid=offline")

    def test_a_redirect_cannot_send_cookie_to_another_host(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "https://evil.invalid/account"})

        with self.assertRaisesRegex(RemoteError, "不安全"):
            get_account_nickname("sid=offline", transport=httpx.MockTransport(handler))
        self.assertEqual([r.url.host for r in requests], ["pan.quark.cn"])

    def test_missing_nickname_is_reported_without_raw_response_values(self):
        for nickname in (None, 1, " "):
            with self.subTest(nickname=nickname), self.assertRaises(RemoteError) as caught:
                get_account_nickname("sid=offline", transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json={"data": {"nickname": nickname, "token": "private"}})))
            self.assertNotIn("private", str(caught.exception))

    def test_profile_read_does_not_block_file_work_and_errors_are_sanitized(self):
        gate, entered = threading.Event(), threading.Event()

        class Backend:
            def account_nickname(self):
                entered.set()
                gate.wait(3)
                raise RuntimeError("private-cookie")

            def listing(self, fid):
                return []

        worker = Worker(Backend())
        worker.start()
        self.addCleanup(worker.stop)
        self.addCleanup(gate.set)
        worker.submit(LoadAccountProfile(3))
        self.assertTrue(entered.wait(1))
        worker.submit(Browse("0"))
        self.assertIsInstance(worker.events.get(timeout=1), Listing)
        with self.assertRaises(queue.Empty):
            worker.events.get_nowait()
        gate.set()
        event = worker.events.get(timeout=1)
        self.assertIsInstance(event, AccountProfile)
        self.assertEqual(event.generation, 3)
        self.assertNotIn("private-cookie", event.error)
