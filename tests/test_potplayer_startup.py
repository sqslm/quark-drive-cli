"""Click-to-play requests, service discovery and private upgrade recovery."""
import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, call
import httpx

from quark_cli.errors import LocalIOError
from quark_cli.potplayer import plugin_state
from quark_cli.state import write_json
from quark_cli.state import StateLock
from test_potplayer_service import _ServerCase
from potplayer_fixture import COOKIE, API_HOST


class StartupTests(_ServerCase):
    def service_kwargs(self):
        def delayed(request):
            if request.url.host == API_HOST:
                time.sleep(0.08)
            return self.fixture._handle(request)
        return {'transport': httpx.MockTransport(delayed), 'cookie': COOKIE, 'monitor_start': False}

    def browse(self):
        self.assertEqual(self.api('GET', '/v1/files?parent=root-dir&include_series=1').status_code, 200)

    def test_browsed_directory_starts_without_reloading_metadata_or_siblings(self):
        self.browse()
        before = len(self.fixture.requests)
        started = time.perf_counter()
        response = self.api('GET', '/v1/playlist?start=v9')
        self.assertEqual([r['fid'] for r in response.json()['items']], ['v9', 'v10'])
        self.play('v9')
        paths = [r.url.path.rsplit('/', 1)[-1] for r in self.fixture.requests[before:]]
        print(f'click preparation: {time.perf_counter()-started:.3f}s; cloud operations: {paths}')
        self.assertEqual(paths, ['download'])

    def test_expired_snapshot_reloads_and_explicit_browse_replaces_it(self):
        self.browse()
        self.fixture.records = [r for r in self.fixture.records if r['fid'] != 'v9']
        self.browse()
        self.assertEqual(self.api('GET', '/v1/playlist?start=v9').status_code, 400)
        with patch.object(self.service, '_clock', return_value=self.service._clock() + 31):
            before = len(self.fixture.requests)
            rows = self.api('GET', '/v1/playlist?parent=root-dir').json()['items']
        self.assertEqual([r['fid'] for r in rows], ['v2', 'v10'])
        self.assertTrue(any(r.url.path.endswith('/file/sort') for r in self.fixture.requests[before:]))

    def test_partial_page_is_never_a_complete_playlist(self):
        self.api('GET', '/v1/files?parent=root-dir&size=1')
        rows = self.api('GET', '/v1/playlist?start=v2').json()['items']
        self.assertEqual([r['fid'] for r in rows], ['v2', 'v9', 'v10'])

    def test_reactivation_discards_old_account_snapshots(self):
        self.browse()
        self.service._activate_account()
        self.assertEqual(self.service._playlists, {})

    def test_click_path_serves_first_bytes_without_prefetching_other_episodes(self):
        from potplayer_fixture import VIDEO_BYTES
        self.browse()
        before = self.fixture.download_calls
        self.api('GET', '/v1/playlist?start=v9')
        media = self.play('v9')
        response = self.stream(media['url'], headers={'Range':'bytes=0-127'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, VIDEO_BYTES[:128])
        self.assertEqual(self.fixture.download_calls-before, 1)

    def test_snapshot_count_is_bounded(self):
        for number in range(40):
            self.service.remember_playlist(str(number), [])
        self.assertEqual(len(self.service._playlists), 32)

class RuntimeOwnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, QUARK_CONFIG_DIR=str(self.root))
        env.start()
        self.addCleanup(env.stop)
        self.path = plugin_state.runtime_path()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows upgrade recovery')
    def test_existing_admin_owner_is_repaired_only_after_private_parent_and_dacl_checks(self):
        write_json(self.path, {'version': '1'})
        with patch('quark_cli.state.validate_windows_acl', side_effect=[LocalIOError('owner'), None, None]) as validate, \
             patch('quark_cli.state.restrict_windows_acl') as protect:
            plugin_state.repair_runtime_owner()
        self.assertEqual(validate.call_args_list, [call(self.path), call(self.path.parent),
                                                   call(self.path, allow_admin_owner=True)])
        protect.assert_called_once_with(self.path, directory=False, set_owner=True)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows upgrade recovery')
    def test_untrusted_runtime_or_parent_is_never_taken_over(self):
        write_json(self.path, {'version': '1'})
        for failures in ([LocalIOError('owner'), LocalIOError('parent')],
                         [LocalIOError('owner'), None, LocalIOError('untrusted ACL')]):
            with self.subTest(failures=len(failures)), \
                 patch('quark_cli.state.validate_windows_acl', side_effect=failures), \
                 patch('quark_cli.state.restrict_windows_acl') as protect:
                with self.assertRaises(LocalIOError):
                    plugin_state.repair_runtime_owner()
                protect.assert_not_called()

    def test_valid_or_missing_runtime_needs_no_repair(self):
        plugin_state.repair_runtime_owner()
        self.assertFalse(self.path.exists())
        write_json(self.path, {'version': '1'})
        with patch('quark_cli.state.restrict_windows_acl', side_effect=AssertionError('unexpected ACL write')):
            plugin_state.repair_runtime_owner()
        self.assertEqual(plugin_state.read_runtime(), {'version': '1'})

    def test_cli_starts_reuses_and_stops_discoverable_service(self):
        def cli(action):
            return subprocess.run([sys.executable, '-m', 'quark_cli.potplayer', action],
                                  capture_output=True, timeout=20, check=True)
        def stop():
            cli('--stop')
            deadline = time.monotonic() + 3
            while True:
                try:
                    with StateLock(self.path.parent, 'service.lock'):
                        return
                except LocalIOError:
                    if time.monotonic() >= deadline:
                        raise AssertionError('service did not release its lock after shutdown')
                    time.sleep(0.02)
        self.addCleanup(stop)
        first = json.loads(cli('--ensure-service').stdout)
        second = json.loads(cli('--ensure-service').stdout)
        self.assertEqual(first, second)
        runtime = plugin_state.read_runtime()
        self.assertIsNotNone(runtime, 'running service is invisible to strict runtime readers')
        response = httpx.get(first['base_url'] + '/v1/auth',
                             headers={'Authorization': 'Bearer ' + first['token']}, timeout=3)
        self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
