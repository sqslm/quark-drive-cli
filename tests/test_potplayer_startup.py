"""Click-to-play request counts over real loopback HTTP, with a delayed cloud."""
import time
import unittest
from unittest.mock import patch
import httpx

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


if __name__ == '__main__':
    unittest.main()
