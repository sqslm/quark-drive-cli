"""Regression for legacy accounts preventing the diagnostics service starting."""
import os
import tempfile
import unittest
import threading
import time
from pathlib import Path
from unittest.mock import patch

from quark_cli.potplayer.accounts import Accounts, account_key
from quark_cli.potplayer.service import Service
from quark_cli.state import write_json
from quark_cli.errors import LocalIOError
from quark_cli.errors import AuthenticationError
from quark_cli.potplayer.library_state import Library
from test_potplayer_service import _ServerCase

A = '__uid=legacy-a; __puus=a'
B = '__uid=legacy-b; __puus=b'


class StartupIsolationTests(unittest.TestCase):
    def test_legacy_migration_preserves_backup_and_account_specific_library_paths(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            old = {'current': 'old-a', 'accounts': [
                {'id': 'old-a', 'cookie': A, 'nickname': '账号 A', 'saved_at': 1},
                {'id': 'old-b', 'cookie': B, 'nickname': '账号 B', 'saved_at': 2},
            ]}
            Library(directory / 'library-old-a.json').record_recent('same', 'A.mp4', 10)
            Library(directory / 'library-old-b.json').record_recent('same', 'B.mp4', 10)
            write_json(directory / 'accounts.json', old)
            store = Accounts(directory)
            self.assertEqual(store.data_path('library.json'), directory / 'library-old-a.json')
            store.switch(account_key(B))
            self.assertEqual(Library(store.data_path('library.json')).recent()[0]['name'], 'B.mp4')
            self.assertTrue((directory / 'accounts.legacy-backup.json').exists())
            self.assertFalse(Accounts(directory).migrated)

    def test_unavailable_store_rejects_all_mutations_without_overwriting_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'accounts.json'
            write_json(path, {'version': 99, 'accounts': 'bad'})
            before = path.read_bytes()
            store = Accounts(Path(root), allow_unavailable=True)
            for command in (lambda: store.add(A), lambda: store.switch('a'),
                            lambda: store.logout('a'), lambda: store.profile('name', {}), store.public):
                with self.assertRaises(LocalIOError):
                    command()
            self.assertEqual(path.read_bytes(), before)
    def test_legacy_account_list_keeps_both_logins_and_current_selection(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'QUARK_CONFIG_DIR': root}):
            path = Path(root) / 'potplayer' / 'accounts.json'
            write_json(path, {'current': 'old-a', 'accounts': [
                {'id': 'old-a', 'cookie': A, 'nickname': '账号 A', 'saved_at': 1},
                {'id': 'old-b', 'cookie': B, 'nickname': '账号 B', 'saved_at': 2},
            ]})
            service = Service(monitor_start=False)
            try:
                self.assertEqual(service.accounts.active, account_key(A))
                self.assertEqual(len(service.accounts.public()['items']), 2)
                self.assertTrue(all(row['logged_in'] for row in service.accounts.public()['items']))
                self.assertEqual(service.accounts.document['accounts'][account_key(B)]['cookie'], B)
            finally:
                service.close()


class BrokenAccountsBridgeTests(_ServerCase):
    def service_kwargs(self):
        self.account_path = Path(self.directory) / 'potplayer' / 'accounts.json'
        write_json(self.account_path, {'version': 99, 'accounts': 'damaged'})
        self.original = self.account_path.read_bytes()
        return {'transport': self.fixture.transport(), 'monitor_start': False}

    def test_diagnostics_and_highlights_state_survive_an_account_store_failure(self):
        response = self.api('GET', '/v1/diagnostics')
        self.assertEqual(response.status_code, 200)
        self.assertIn('transfer', response.json())
        for field in ('accounts', 'bookmark_request', 'login_state'):
            self.assertNotIn(field, response.json())
        self.assertEqual(self.api('GET', '/v1/bookmarks/state').status_code, 200)
        self.assertEqual(self.api('GET', '/v1/accounts').status_code, 500)
        self.assertEqual(self.api('POST', '/v1/auth/login').status_code, 500)
        self.assertEqual(self.account_path.read_bytes(), self.original)

    def test_broken_account_file_does_not_prevent_service_or_diagnostics(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'QUARK_CONFIG_DIR': root}):
            path = Path(root) / 'potplayer' / 'accounts.json'
            write_json(path, {'version': 99, 'accounts': 'damaged'})
            original = path.read_bytes()
            service = Service(monitor_start=False)
            try:
                payload = service.diagnostics.snapshot(credential_present=service.authenticated(), guard=service._guard)
                self.assertIn('transfer', payload)
                self.assertFalse(service.authenticated())
                self.assertEqual(path.read_bytes(), original)
            finally:
                service.close()


class IndependentObservationTests(_ServerCase):
    def service_kwargs(self):
        return {'transport': self.fixture.transport(), 'cookie': A, 'monitor_start': False}

    def test_slow_profile_refresh_does_not_block_diagnostics_or_highlight_context(self):
        entered, release = threading.Event(), threading.Event()
        replies = []
        def nickname(*_, **__):
            entered.set()
            release.wait(3)
            return '账号 A'
        with patch('quark_cli.potplayer.service.get_account_nickname', nickname), patch(
                'quark_cli.potplayer.service.get_account_capacity', return_value={'used_bytes': 1, 'total_bytes': 10}):
            thread = threading.Thread(target=lambda: replies.append(self.api('POST', '/v1/accounts/refresh', body={})))
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                self.assertEqual(self.api('GET', '/v1/diagnostics').status_code, 200)
                self.assertEqual(self.api('GET', '/v1/bookmarks/state').status_code, 200)
                self.assertEqual(self.api('GET', '/v1/bookmarks?fid=demo').status_code, 200)
                self.assertLess(time.monotonic() - started, 1)
                self.assertFalse(release.is_set())
            finally:
                release.set()
                thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(replies[0].status_code, 200)

    def test_late_profile_success_or_expiry_cannot_change_the_new_account(self):
        with self.service.account_lock:
            second = self.service.accounts.add(B, '账号 B')
            self.service._activate_account()
        first = account_key(A)
        for expired in (False, True):
            with self.subTest(expired=expired):
                self.service.switch_account(first)
                entered, release = threading.Event(), threading.Event()
                replies = []
                def nickname(*_, **__):
                    entered.set()
                    release.wait(3)
                    if expired:
                        raise AuthenticationError('旧账号登录已失效')
                    return '迟到的账号 A'
                with patch('quark_cli.potplayer.service.get_account_nickname', nickname), patch(
                        'quark_cli.potplayer.service.get_account_capacity', return_value={'used_bytes': 1, 'total_bytes': 10}):
                    thread = threading.Thread(target=lambda: replies.append(self.api('POST', '/v1/accounts/refresh',
                        body={}, headers={'X-Quark-Account': first})))
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        self.service.switch_account(second)
                    finally:
                        release.set()
                        thread.join(4)
                self.assertFalse(thread.is_alive())
                self.assertIn(replies[0].status_code, (400, 401))
                self.assertEqual(self.service.accounts.active, second)
                self.assertTrue(self.service.authenticated())
                self.assertEqual(self.service.accounts.document['accounts'][second]['nickname'], '账号 B')

    def test_pages_have_distinct_clients_queues_and_business_commands(self):
        from quark_cli.potplayer.status_gui import StatusWindow, BridgeClient
        from quark_cli.potplayer.accounts_gui import AccountsPanel
        from quark_cli.potplayer.highlights_gui import HighlightsPanel
        diagnostics = StatusWindow(None, None)
        accounts = AccountsPanel(None, None)
        highlights = HighlightsPanel(None, None)
        self.assertIsNot(diagnostics._events, accounts._events)
        self.assertIsNot(diagnostics._requests, highlights._requests)
        self.assertFalse(hasattr(diagnostics, '_account_command'))
        self.assertFalse(hasattr(diagnostics, '_open_bookmarks'))
        self.assertFalse(hasattr(BridgeClient, 'account_action'))
        self.assertFalse(hasattr(BridgeClient, 'bookmarks'))
