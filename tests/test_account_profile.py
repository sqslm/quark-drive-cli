import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli.auth import get_account_capacity, get_account_nickname
from quark_cli.desktop.service import DesktopService
from quark_cli.downloader.worker import DriveBackend
from quark_cli.downloader.worker import AccountProfile, Browse, Listing, LoadAccountProfile, Worker
from quark_cli.errors import AuthenticationError, RateLimitError, RemoteError


class AccountProfileTests(unittest.TestCase):
    def test_capacity_uses_fixed_official_host_and_returns_only_byte_counters(self):
        requests=[]
        def handle(request):
            requests.append(request)
            return httpx.Response(200,json={'data':{'use_capacity':123,'total_capacity':1000,'token':'private'}})
        result=get_account_capacity('sid=offline',transport=httpx.MockTransport(handle))
        self.assertEqual(result,{'used_bytes':123,'total_bytes':1000})
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0].url.host,'drive.quark.cn')
        self.assertEqual(requests[0].url.path,'/1/clouddrive/member')
        self.assertEqual(requests[0].url.params['fetch_identity'],'true')
        self.assertNotIn('private',str(result))

    def test_capacity_requires_real_counts_without_replacing_missing_values_with_zero(self):
        for value in (None,True,-1,'100',0.5):
            with self.subTest(value=value),self.assertRaises(RemoteError):
                get_account_capacity('sid=offline',transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'data':{'use_capacity':value,'total_capacity':100}})))
        for used,total in ((0,0),(200,100)):
            result=get_account_capacity('sid=offline',transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'data':{'use_capacity':used,'total_capacity':total}})))
            self.assertEqual(result,{'used_bytes':used,'total_bytes':total})

    def test_capacity_rejects_redirects_and_preserves_cooldown(self):
        requests=[]
        def redirect(request):
            requests.append(request)
            return httpx.Response(302,headers={'location':'https://evil.invalid/member'})
        with self.assertRaises(RemoteError):get_account_capacity('sid=offline',transport=httpx.MockTransport(redirect))
        self.assertEqual(len(requests),1)
        with self.assertRaises(RateLimitError):
            get_account_capacity('sid=offline',transport=httpx.MockTransport(lambda _:httpx.Response(429,headers={'retry-after':'5'})))

    def test_capacity_ipc_login_gate_and_mutation_refresh_event(self):
        backend=DriveBackend(lambda:'sid=offline',transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'data':{'use_capacity':1,'total_capacity':10}})))
        messages=[]
        with tempfile.TemporaryDirectory() as directory:
            service=DesktopService(messages.append,backend=backend,directory=directory);service.saved_cookie='sid=offline'
            try:
                self.assertEqual(service.dispatch('cap','account.capacity',{},None),{'used_bytes':1,'total_bytes':10})
                with service.mutation():pass
                self.assertEqual(messages[-1]['event'],'capacity.changed')
                service.logged_out=True
                with self.assertRaises(AuthenticationError):service.dispatch('cap','account.capacity',{},None)
            finally:service.shutdown()

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
