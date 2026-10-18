"""Regression cases for real parents, series context, completion and prefetch."""
import unittest
from pathlib import Path
import tempfile
import httpx

from test_potplayer_service import _ServerCase
from potplayer_fixture import COOKIE, record
from quark_cli.potplayer.library_state import Library


class DirectoryContextCase(_ServerCase):
    def service_kwargs(self):
        self.fixture.records = [record('media', '影视', directory=True),
                                record('tv', '电视剧', 'media', directory=True),
                                record('show', '蒸蒸日上', 'tv', directory=True),
                                record('quality', '4K', 'show', directory=True),
                                record('ep', '04.mkv', 'quality')]
        return {'transport': self.fixture.transport(), 'cookie': COOKIE, 'monitor_start': False}

    def test_deep_link_returns_actual_parent_instead_of_root(self):
        payload = self.api('GET', '/v1/files?parent=quality&include_series=1').json()
        self.assertEqual(payload['up'], 'show')

    def test_highlights_resolve_series_version_and_category_from_ancestors(self):
        self.api('GET', '/v1/files?parent=quality&include_series=1')
        self.service.monitor.bookmarks.add('ep', '04.mkv', 2048, 30_000, 900_000, '对白', parent='quality', parent_name='4K')
        payload = self.api('GET', '/v1/bookmarks').json()
        self.assertEqual(payload['groups'][0]['name'], '蒸蒸日上')
        self.assertEqual(payload['groups'][0]['category'], '电视剧')
        self.assertEqual(payload['items'][0]['video_prefix'], '蒸蒸日上/4K')
        before = len(self.fixture.requests)
        self.api('GET', '/v1/files?parent=quality&include_series=1')
        new = self.fixture.requests[before:]
        self.assertFalse(any(request.url.path.endswith('/file/info') for request in new))

    def test_cached_series_context_survives_restart_and_categories_stay_separate(self):
        from quark_cli.potplayer.directory_state import Directories
        self.api('GET', '/v1/files?parent=quality&include_series=1')
        cache = Directories(self.service.directories.path)
        self.assertEqual(cache.context('quality')['video_prefix'], '蒸蒸日上/4K')
        self.assertEqual(cache.context('quality')['category'], '电视剧')
        self.assertNotIn('电视剧', cache.context('quality')['series_name'])

    def test_cold_highlights_resolve_ancestors_without_browsing_first(self):
        self.service.monitor.bookmarks.add('ep', '04.mkv', 2048, 30_000, 900_000, '', parent='quality', parent_name='4K')
        self.assertEqual(self.api('GET', '/v1/bookmarks').json()['groups'][0]['name'], '蒸蒸日上')

    def test_metadata_cooldown_does_not_hide_existing_local_marks(self):
        from unittest.mock import Mock, patch
        from quark_cli.errors import RateLimitError
        self.service.monitor.bookmarks.add('ep', '04.mkv', 2048, 30_000, 900_000, '对白', parent='quality', parent_name='4K')
        with patch.object(self.service, 'files', return_value=Mock(info=Mock(side_effect=RateLimitError('fixture cooldown')))):
            response = self.api('GET', '/v1/bookmarks')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['items'][0]['note'], '对白')
        self.assertTrue(response.json()['metadata_notice'])


class AutoWatchedCase(unittest.TestCase):
    def test_progress_over_ninety_percent_persists_watched_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Library(Path(directory) / 'library.json')
            library.record_recent('ep', '04.mkv', 2048)
            library.save_progress('ep', 2048, 900_000, 1_000_000)
            self.assertFalse(library.is_watched('ep', 2048))
            library.save_progress('ep', 2048, 901_000, 1_000_000)
            self.assertTrue(library.is_watched('ep', 2048))

    def test_monitor_marks_on_the_next_sample_without_waiting_ten_seconds(self):
        from test_potplayer_playback import FakePlayer, FakeSample, Clock, session
        from quark_cli.potplayer.playback import PlaybackMonitor
        with tempfile.TemporaryDirectory() as directory:
            library = Library(Path(directory) / 'library.json')
            library.record_recent('v1', 'a.mp4', 100)
            player, clock = FakePlayer(), Clock()
            monitor = PlaybackMonitor(library, native_factory=lambda: player, clock=clock, start=False)
            try:
                media = session()
                monitor.bind(media, 123)
                monitor.stream_started(media.session)
                player.feed(123, FakeSample(123, 'https://quark.invalid/video/v1', 800_000, 1_000_000, 2))
                monitor._poll_once()
                player.feed(123, FakeSample(123, 'https://quark.invalid/video/v1', 901_000, 1_000_000, 2))
                clock.advance(2)
                monitor._poll_once()
                self.assertTrue(library.is_watched('v1', 100))
            finally:
                monitor.close()


class BufferingCase(unittest.TestCase):
    def test_paused_playback_cannot_read_the_whole_video(self):
        from quark_cli.potplayer.buffering import ReadAhead
        gate = ReadAhead(size=1024 ** 3, start=0)
        row = {'position_ms': 1000, 'duration_ms': 1000_000, 'state': 'paused'}
        self.assertGreater(gate.allowance(0, row), 0)
        self.assertEqual(gate.allowance(64 * 1024 ** 2, row), 0)

    def test_seek_probe_is_allowed_and_clock_advance_opens_more_budget(self):
        from quark_cli.potplayer.buffering import ReadAhead
        row = {'position_ms': 1000, 'duration_ms': 1000_000, 'state': 'playing'}
        gate = ReadAhead(size=1024 ** 3, start=512 * 1024 ** 2)
        self.assertGreater(gate.allowance(gate.start, row), 0)
        self.assertEqual(gate.allowance(gate.start + 3 * 1024 ** 2, row), 0)
        row['position_ms'] = 500_000
        self.assertGreater(gate.allowance(gate.start + 3 * 1024 ** 2, row), 0)

    def test_small_consecutive_ranges_share_the_probe_and_startup_budget(self):
        from quark_cli.potplayer.buffering import ReadAhead, BufferBudget
        budget = BufferBudget()
        row = {'position_ms': 1000, 'duration_ms': 1000_000, 'state': 'paused'}
        size, start = 1024 ** 3, 128 * 1024 ** 2
        first = ReadAhead(size=size, start=start, budget=budget)
        self.assertGreater(first.allowance(start, row), 0)
        following = ReadAhead(size=size, start=start + 2 * 1024 ** 2, budget=budget)
        self.assertEqual(following.allowance(following.start, row), 0)
        first.consume(8 * 1024 ** 2)
        unknown = ReadAhead(size=size, start=0, budget=budget, clock=lambda: budget.started)
        self.assertEqual(unknown.allowance(0, {'state': 'waiting'}), 0)


class PrefetchHttpCase(_ServerCase):
    def service_kwargs(self):
        from test_potplayer_playback import FakePlayer
        self.player = FakePlayer()
        return {'transport': self.fixture.transport(), 'cookie': COOKIE,
                'native_factory': lambda: self.player, 'monitor_start': False}

    def test_paused_player_stops_upstream_requests_before_fetching_the_video(self):
        from quark_cli.potplayer.stream import Media
        from test_potplayer_playback import FakeSample
        size = 32 * 1024 ** 2
        self.fixture.records.append(record('big', 'video.mkv', 'root-dir', size=size))
        self.fixture.virtual['big'] = size
        media = self.service.new_session(Media('big', 'video.mkv', size, 'https://drive-pc.quark.cn/video'))
        # Use the fixture's signed origin, without making this regression depend
        # on the public /play resolver's metadata requests.
        from quark_cli.potplayer.stream import resolve
        resolved = resolve(self.service.files(), self.service.client(), 'big')
        media.url = resolved.url
        self.service.monitor.bind(media, 123)
        self.service.monitor.stream_started(media.session)
        self.player.feed(123, FakeSample(123, 'https://quark.invalid/video/big', 1000, 1000_000, 1))
        self.service.monitor._poll_once()
        received = 0
        with self.client.stream('GET', self.base + f'/v1/stream/{media.session}/video.mkv', timeout=.5) as response:
            self.assertEqual(response.status_code, 200)
            with self.assertRaises(httpx.ReadTimeout):
                for chunk in response.iter_bytes():
                    received += len(chunk)
        self.assertGreater(received, 0)
        self.assertLess(received, 8 * 1024 ** 2)
        self.assertLessEqual(len(self.fixture.origin_requests()), 2)
