"""Account lifecycle and real loopback requests, including late old streams."""
from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli.auth import LoginCancelled, save_cookie
from quark_cli.errors import LocalIOError, UsageError
from quark_cli.potplayer.accounts import Accounts, account_key
from quark_cli.potplayer.bookmark_state import BookmarkStore
from quark_cli.potplayer.library_state import Library
from quark_cli.potplayer.service import Service
from quark_cli.potplayer.highlights_gui import HighlightsClient, HighlightsWorker, HighlightsPanel
from quark_cli.potplayer.gui_common import Endpoint
from quark_cli.state import write_json
from test_potplayer_service import _ServerCase
from potplayer_fixture import VIDEO_BYTES

A = '__uid=account-a; __puus=secret-a'
B = '__uid=account-b; __puus=secret-b'


class AccountStoreTests(unittest.TestCase):
    def test_switch_restart_logout_and_same_nickname(self):
        with tempfile.TemporaryDirectory() as root:
            store = Accounts(Path(root))
            a = store.add(A, '同名')
            b = store.add(B, '同名')
            self.assertNotEqual(a, b)
            store.switch(a)
            restored = Accounts(Path(root))
            self.assertEqual(restored.active, a)
            self.assertEqual(restored.cookie(), A)
            restored.logout(b)
            self.assertEqual(restored.cookie(), A)
            with self.assertRaises(UsageError):
                restored.switch(b)
            public = json.dumps(restored.public())
            self.assertNotIn('__puus', public)
            self.assertNotIn('secret-', public)

    def test_uid_survives_cookie_refresh_and_nickname_changes(self):
        with tempfile.TemporaryDirectory() as root:
            store = Accounts(Path(root))
            key = store.add(A, '旧名字')
            self.assertEqual(store.add('__uid=account-a; __puus=new', '新名字'), key)
            self.assertEqual(len(store.public()['items']), 1)
            self.assertEqual(store.public()['items'][0]['nickname'], '新名字')
            self.assertNotEqual(account_key('sid=a'), account_key('sid=b'))

    def test_bad_document_and_failed_write_never_replace_original_state(self):
        with tempfile.TemporaryDirectory() as root:
            store = Accounts(Path(root))
            a = store.add(A)
            with patch('quark_cli.potplayer.accounts.write_json', side_effect=LocalIOError('disk full')):
                with self.assertRaises(LocalIOError):
                    store.add(B)
            self.assertEqual(store.active, a)
            self.assertEqual(Accounts(Path(root)).active, a)
            write_json(store.path, {'version': 1, 'active': [], 'legacy': None, 'accounts': {}})
            before = store.path.read_bytes()
            with self.assertRaises(LocalIOError):
                Accounts(Path(root))
            self.assertEqual(store.path.read_bytes(), before)

    def test_upgrade_binds_legacy_history_to_original_login_only(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'QUARK_CONFIG_DIR': root}):
            directory = Path(root) / 'potplayer'
            save_cookie(A, directory / 'cookie')
            Library().record_recent('same', 'A.mp4', 10)
            BookmarkStore().add('same', 'A.mp4', 10, 100, 1000, 'A备注')
            service = Service(monitor_start=False)
            try:
                a = service.accounts.active
                self.assertEqual(service.library.recent()[0]['name'], 'A.mp4')
                self.assertFalse((directory / 'cookie').exists())
                with service.account_lock:
                    b = service.accounts.add(B, 'B')
                    service._activate_account()
                self.assertEqual(service.library.recent(), [])
                self.assertEqual(service.monitor.bookmarks.list(), [])
                service.library.record_recent('same', 'B.mp4', 10)
                service.switch_account(a)
                self.assertEqual(service.library.recent()[0]['name'], 'A.mp4')
                self.assertEqual(service.monitor.bookmarks.list()[0]['note'], 'A备注')
                service.switch_account(b)
                self.assertEqual(service.library.recent()[0]['name'], 'B.mp4')
            finally:
                service.close()
            restarted = Service(monitor_start=False)
            try:
                self.assertEqual(restarted.accounts.active, b)
                self.assertEqual(restarted.library.recent()[0]['name'], 'B.mp4')
            finally:
                restarted.close()

    def test_unowned_old_data_is_not_given_to_a_new_login(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'QUARK_CONFIG_DIR': root}):
            Library().record_recent('same', '未知账号.mp4', 10)
            service = Service(monitor_start=False)
            try:
                with service.account_lock:
                    service.accounts.add(B)
                    service._activate_account()
                self.assertEqual(service.library.recent(), [])
                self.assertEqual(Library().recent()[0]['name'], '未知账号.mp4')
            finally:
                service.close()

    def test_failed_or_cancelled_login_keeps_active_account(self):
        class Cancelled:
            def run(self, **_):
                raise LoginCancelled('cancel')
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'QUARK_CONFIG_DIR': root}):
            service = Service(cookie=A, login_factory=Cancelled, monitor_start=False)
            try:
                key = service.accounts.active
                service._run_login()
                self.assertEqual(service.accounts.active, key)
                self.assertEqual(service._cookie, A)
                self.assertTrue(service.authenticated())
                self.assertEqual(service.login_state, 'idle')
            finally:
                service.close()


class AccountsBridgeTests(_ServerCase):
    def service_kwargs(self):
        upstream = self.fixture.transport()
        self.profile_requests = []
        def handler(request):
            if request.url.host == 'origin.pds.quark.cn' and getattr(self, 'block_origin', False):
                self.origin_entered.set()
                self.origin_release.wait(3)
                return httpx.Response(403)
            if request.url.path == '/account/info':
                self.profile_requests.append(request)
                return httpx.Response(200, json={'data': {'nickname': '账号A' if 'account-a' in request.headers['cookie'] else '账号B'}})
            if request.url.path.endswith('/member'):
                self.profile_requests.append(request)
                return httpx.Response(200, json={'data': {'use_capacity': 25, 'total_capacity': 100}})
            return upstream.handle_request(request)
        return {'cookie': A, 'transport': httpx.MockTransport(handler), 'monitor_start': False}

    def setUp(self):
        super().setUp()
        self.a = self.service.accounts.active
        with self.service.account_lock:
            self.b = self.service.accounts.add(B, '账号B')
            self.service._activate_account()
        self.service.switch_account(self.a)

    def scoped(self, method, path, *, key=None, body=None):
        return self.api(method, path, body=body, headers={'X-Quark-Account': key or self.service.accounts.active or ''})

    def test_switch_keeps_credentials_and_isolates_same_fid_everywhere(self):
        service = self.service
        service.library.record_recent('v2', 'A.mp4', len(VIDEO_BYTES))
        service.library.save_progress('v2', len(VIDEO_BYTES), 300, 1000)
        mark = service.monitor.bookmarks.add('v2', 'A.mp4', len(VIDEO_BYTES), 300, 1000, 'A备注')
        self.scoped('POST', '/v1/favorites', body={'fid': 'root-dir'})
        old_url = self.scoped('POST', '/v1/play', body={'fid': 'v2'}).json()['url']
        old_session = service.sessions.get(old_url.split('/')[3])
        self.assertEqual(self.scoped('POST', '/v1/accounts/switch', body={'id': self.b}).status_code, 200)
        self.assertEqual(service.library.recent(), [])
        self.assertEqual(service.library.favorites(), [])
        self.assertEqual(service.monitor.bookmarks.list(), [])
        service.record_played(old_session)
        self.assertEqual(service.library.recent(), [])
        self.assertEqual(self.stream(old_url).status_code, 404)
        stale = self.scoped('POST', '/v1/bookmarks/rename', key=self.a, body={'id': mark['id'], 'note': '串号'})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.api('POST', '/v1/play', body={'fid': 'v2'}).status_code, 409)
        self.scoped('POST', '/v1/accounts/switch', body={'id': self.a})
        self.assertEqual(service.library.recent()[0]['name'], 'A.mp4')
        self.assertEqual(service.monitor.bookmarks.list()[0]['note'], 'A备注')
        self.assertEqual(len(service.library.favorites()), 1)
        self.assertTrue(all(row['logged_in'] for row in service.accounts.public()['items']))

    def test_logout_noncurrent_and_relogin_restore_only_that_accounts_data(self):
        self.service.library.record_recent('v2', 'A.mp4', 10)
        original_path = self.service.library.path
        self.assertEqual(self.scoped('POST', '/v1/accounts/logout', body={'id': self.b}).status_code, 200)
        self.assertEqual(self.service.accounts.active, self.a)
        self.assertTrue(self.service.authenticated())
        self.assertEqual(self.scoped('POST', '/v1/accounts/switch', body={'id': self.b}).status_code, 400)
        self.scoped('POST', '/v1/accounts/logout', body={'id': self.a})
        self.assertFalse(self.service.authenticated())
        self.assertTrue(original_path.exists())
        with self.service.account_lock:
            self.service.accounts.add('__uid=account-a; __puus=refreshed')
            self.service._activate_account()
        self.assertEqual(self.service.library.recent()[0]['name'], 'A.mp4')

    def test_profile_capacity_and_polling_are_local_and_never_return_cookies(self):
        response = self.scoped('POST', '/v1/accounts/refresh', body={})
        self.assertEqual(response.status_code, 200)
        active = next(row for row in response.json()['items'] if row['current'])
        self.assertEqual(active['nickname'], '账号A')
        self.assertEqual(active['capacity'], {'used_bytes': 25, 'total_bytes': 100})
        self.api('GET', '/v1/diagnostics')
        self.api('GET', '/v1/diagnostics')
        self.assertEqual(len(self.profile_requests), 2)
        for path in ('/v1/accounts', '/v1/diagnostics', '/v1/diagnostics/report'):
            text = self.api('GET', path).text
            self.assertNotIn('secret-a', text)
            self.assertNotIn('secret-b', text)
            if path.endswith('/report'):
                self.assertNotIn('账号A', text)

    def test_completed_scan_adds_account_without_logging_out_previous_one(self):
        service = self.service
        class Login:
            def run(self, **_):
                save_cookie(B, service.accounts.directory / 'login-cookie')
        service._login_factory = Login
        service.library.record_recent('v2', 'A.mp4', 10)
        service._run_login()
        self.assertEqual(service.login_state, 'success')
        self.assertEqual(service.accounts.active, self.b)
        self.assertEqual(service.accounts.document['accounts'][self.a]['cookie'], A)
        self.assertEqual(service.library.recent(), [])
        self.assertFalse((service.accounts.directory / 'login-cookie').exists())
        service.switch_account(self.a)
        self.assertEqual(service.library.recent()[0]['name'], 'A.mp4')

    def test_live_old_client_is_pinned_and_late_auth_error_cannot_log_out_new_account(self):
        with self.service.stream_client() as resources:
            old_client, old_sessions, _, _ = resources
            self.service.switch_account(self.b)
            self.assertIn(old_client, self.service._retired_clients)
            self.assertIsNot(old_sessions, self.service.sessions)
            self.assertIsNot(old_client, self.service.client())
            self.assertTrue(self.service.authenticated())
        self.assertNotIn(old_client, self.service._retired_clients)
        self.assertFalse(self.service._stream_users)

    def test_old_origin_failure_after_switch_never_refreshes_using_new_cookie(self):
        old_url = self.scoped('POST', '/v1/play', body={'fid': 'v2'}).json()['url']
        self.block_origin = True
        self.origin_entered, self.origin_release = threading.Event(), threading.Event()
        result = []
        thread = threading.Thread(target=lambda: result.append(self.stream(old_url)))
        thread.start()
        try:
            self.assertTrue(self.origin_entered.wait(1))
            self.service.switch_account(self.b)
        finally:
            self.origin_release.set()
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0].status_code, 400)
        self.assertEqual(self.service.accounts.active, self.b)
        self.assertTrue(self.service.authenticated())
        self.assertEqual(self.service.library.recent(), [])
        self.assertFalse(any('account-b' in request.headers.get('cookie', '') for request in self.fixture.requests))

    def test_switch_waits_for_control_request_so_old_reply_cannot_write_to_new_library(self):
        entered, release, switched = threading.Event(), threading.Event(), threading.Event()
        def old_request():
            with self.service.account_lock:
                entered.set()
                release.wait(3)
                self.service.library.record_recent('same', 'A.mp4', 10)
        first = threading.Thread(target=old_request)
        second = threading.Thread(target=lambda: (self.service.switch_account(self.b), switched.set()))
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        try:
            self.assertFalse(switched.wait(.05))
        finally:
            release.set()
            first.join(3)
            second.join(3)
        self.assertTrue(switched.is_set())
        self.assertEqual(self.service.library.recent(), [])
        self.service.switch_account(self.a)
        self.assertEqual(self.service.library.recent()[0]['name'], 'A.mp4')


class AccountWorkerTests(unittest.TestCase):
    def test_window_discards_late_result_even_if_new_account_has_same_fid(self):
        window = HighlightsPanel(None, None)
        window._account_id = account_key(B)
        received = []
        window._apply_bookmarks = received.append
        window._handle('scoped', (account_key(A), 'bookmarks', (0, 'same', [{'note': 'A'}])))
        self.assertEqual(received, [])
        window._handle('scoped', (account_key(B), 'bookmarks', (0, 'same', [{'note': 'B'}])))
        self.assertEqual(received, [(0, 'same', [{'note': 'B'}])])

    def test_queued_old_bookmark_command_keeps_account_and_echoes_scope(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(409, json={'error': {'type': 'account_changed'}})
        client = HighlightsClient(Endpoint('http://127.0.0.1:12345', 'token', 'instance', 12345), transport=httpx.MockTransport(handler))
        events = queue.Queue()
        worker = HighlightsWorker(client, events, queue.Queue())
        try:
            worker._serve(('scoped', account_key(A), ('bookmark_rename', 1, 'v2', 'mark', '备注')))
            self.assertEqual(seen[0].headers['X-Quark-Account'], account_key(A))
            self.assertEqual(events.get_nowait(), ('scoped', (account_key(A), 'bookmark_renamed', (1, 'v2', False))))
            self.assertIsNone(client.account_id)
        finally:
            client.close()
