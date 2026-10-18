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
    def test_scoped_identity_rejects_other_accounts_and_unexpected_queries(self):
        from quark_cli.potplayer.playback import _identity_matches
        for path in ('video/v1', 'play/v1', 'mark/marker'):
            url = 'https://quark.invalid/' + path
            self.assertTrue(_identity_matches(url + '?account=account-a', 'v1', None, 'marker', 'account-a'))
            for suffix in ('?account=account-b', '?account=account-a&extra=1', '?account=account-a&account=account-b'):
                self.assertFalse(_identity_matches(url + suffix, 'v1', None, 'marker', 'account-a'))
        self.assertFalse(_identity_matches('a.mp4', 'v1', None, account_id='account-a'))

    def test_account_scoped_player_address_supplies_trusted_progress(self):
        from test_potplayer_playback import FakePlayer, FakeSample, session
        from quark_cli.potplayer.playback import PlaybackMonitor
        with tempfile.TemporaryDirectory() as directory:
            library = Library(Path(directory) / 'library.json')
            player = FakePlayer()
            monitor = PlaybackMonitor(library, native_factory=lambda: player, start=False)
            try:
                media = session()
                media.account_id = 'account-a'
                monitor.bind(media, 123)
                monitor.stream_started(media.session)
                player.feed(123, FakeSample(123, 'https://quark.invalid/video/v1?account=account-a', 1000, 100_000, 2))
                monitor._poll_once()
                self.assertEqual(monitor.buffer_state(media.session)['position_ms'], 1000)
                self.assertEqual(monitor.buffer_state(media.session)['state'], 'playing')
            finally:
                monitor.close()

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
    def test_twenty_percent_at_full_speed_then_resume_at_ten_percent(self):
        from quark_cli.potplayer.buffering import ReadAhead
        gate = ReadAhead(size=1000, start=0)
        row = {'position_ms': 0, 'duration_ms': 1000, 'state': 'playing'}
        self.assertEqual(gate.allowance(0, row), 200)
        self.assertEqual(gate.reserve(0, row, 999), 200)
        gate.consume(200)
        self.assertEqual(gate.allowance(200, row), 0)
        row['position_ms'] = 99
        self.assertEqual(gate.allowance(200, row), 0)
        row['position_ms'] = 100
        self.assertEqual(gate.reserve(200, row, 999), 100)
        gate.consume(100)
        self.assertEqual(gate.allowance(300, row), 0)
        row['position_ms'] = 200
        self.assertEqual(gate.allowance(300, row), 100)

    def test_unknown_clock_has_no_byte_rate_or_elapsed_time_refill(self):
        from unittest.mock import patch
        from quark_cli.potplayer.buffering import ReadAhead
        gate = ReadAhead(size=100 * 1024 ** 2, start=0)
        row = {'state': 'waiting'}
        self.assertEqual(gate.reserve(0, row, 100 * 1024 ** 2), 20 * 1024 ** 2)
        gate.consume(20 * 1024 ** 2)
        with patch('time.monotonic', return_value=10**12):
            self.assertEqual(gate.allowance(20 * 1024 ** 2, row), 0)

    def test_concurrent_ranges_share_reservations_and_release_unused_bytes(self):
        from quark_cli.potplayer.buffering import ReadAhead, BufferBudget
        budget = BufferBudget()
        first = ReadAhead(size=1000, start=0, budget=budget)
        following = ReadAhead(size=1000, start=100, budget=budget)
        self.assertEqual(first.reserve(0, {}, 150), 150)
        self.assertEqual(following.reserve(100, {}, 150), 50)
        self.assertEqual(first.allowance(150, {}), 0)
        first.release()
        self.assertEqual(following.reserve(150, {}, 150), 150)
        following.consume(200)
        self.assertEqual(first.allowance(0, {}), 0)

    def test_seek_and_tail_index_share_budget_without_stalling_new_position(self):
        from quark_cli.potplayer.buffering import ReadAhead, BufferBudget
        size, mib = 100 * 1024 ** 2, 1024 ** 2
        budget = BufferBudget()
        row = {'position_ms': 0, 'duration_ms': 1000, 'state': 'playing'}
        first = ReadAhead(size=size, start=0, budget=budget)
        self.assertEqual(first.reserve(0, row, 10 * mib), 10 * mib)
        first.consume(10 * mib)
        tail = ReadAhead(size=size, start=90 * mib, budget=budget)
        self.assertEqual(tail.reserve(90 * mib, row, 10 * mib), 10 * mib)
        tail.consume(10 * mib)
        next_tail = ReadAhead(size=size, start=92 * mib, budget=budget)
        self.assertEqual(next_tail.allowance(92 * mib, row), 0)
        row['position_ms'] = 900
        self.assertEqual(next_tail.allowance(92 * mib, row), 8 * mib)
        row['position_ms'] = 100
        self.assertEqual(first.allowance(10 * mib, row), 20 * mib)


class PrefetchHttpCase(_ServerCase):
    simulate_buffer_consumption = False

    def service_kwargs(self):
        from test_potplayer_playback import FakePlayer
        self.player = FakePlayer()
        return {'transport': self.fixture.transport(), 'cookie': COOKIE,
                'native_factory': lambda: self.player, 'monitor_start': False}

    def test_full_speed_twenty_percent_pause_and_ten_percent_refill_over_http(self):
        import queue
        import threading
        import time
        from test_potplayer_playback import FakeSample
        size, mib = 100 * 1024 ** 2, 1024 ** 2
        self.fixture.records.append(record('refill', 'video.mkv', 'root-dir', size=size))
        self.fixture.virtual['refill'] = size
        payload = self.api('POST', '/v1/play', body={'fid': 'refill', 'player_pid': 123}).json()
        sid = payload['url'].split('/')[3]
        media = self.service.sessions.get(sid)
        identity = 'https://quark.invalid/video/refill?account=' + media.account_id
        self.player.feed(123, FakeSample(123, identity, 0, 1_000_000, 1))
        chunks = queue.Queue()
        stopped = threading.Event()

        def receive():
            try:
                with httpx.Client(timeout=3) as client:
                    with client.stream('GET', self.base + payload['url']) as response:
                        response.raise_for_status()
                        total = 0
                        for chunk in response.iter_bytes(64 * 1024):
                            total += len(chunk)
                            chunks.put(len(chunk))
                            if total >= 30 * mib or stopped.is_set():
                                break
            except Exception as exc:
                if not stopped.is_set():
                    chunks.put(exc)

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()

        def cleanup():
            stopped.set()
            reader.join(4)
            self.assertFalse(reader.is_alive())

        self.addCleanup(cleanup)

        def receive_exact(count):
            total = 0
            deadline = time.monotonic() + 3
            while total < count:
                item = chunks.get(timeout=max(.01, deadline - time.monotonic()))
                if isinstance(item, Exception):
                    raise item
                total += item
            self.assertEqual(total, count)

        # 20 MiB arrives without a 1 MiB/s refill clock; the total deadline
        # catches that old limiter while avoiding an Internet-speed benchmark.
        receive_exact(20 * mib)
        with self.assertRaises(queue.Empty):
            chunks.get(timeout=.25)
        self.assertEqual(sum(int(r.headers['range'].split('-')[1]) - int(r.headers['range'][6:].split('-')[0]) + 1
                             for r in self.fixture.origin_requests()), 20 * mib)
        self.player.feed(123, FakeSample(123, identity, 99_999, 1_000_000, 1))
        self.service.monitor._poll_once()
        with self.assertRaises(queue.Empty):
            chunks.get(timeout=.25)
        self.player.feed(123, FakeSample(123, identity, 100_000, 1_000_000, 1))
        self.service.monitor._poll_once()
        receive_exact(10 * mib)
        reader.join(3)
        self.assertFalse(reader.is_alive())
        self.assertEqual(sum(end - start for start, end in media.buffer_budget.ranges), 20 * mib)
        self.assertEqual(media.buffer_budget.reserved, 0)

    def test_loading_player_keeps_receiving_startup_bytes_before_identity_matches(self):
        from unittest.mock import patch
        from test_potplayer_playback import FakeSample
        for size in (512 * 1024, 5 * 1024 ** 2):
            with self.subTest(size=size):
                fid = 'loading' + str(size)
                self.fixture.records.append(record(fid, 'video.mkv', 'root-dir', size=size))
                self.fixture.virtual[fid] = size
                payload = self.api('POST', '/v1/play', body={'fid': fid, 'player_pid': 123}).json()
                # PotPlayer can still report the previous/empty file while the
                # new demuxer waits for the first response bytes.
                self.player.feed(123, FakeSample(123, '', 0, 0, 0))
                started = self.service.monitor.stream_started
                state = self.service.monitor.buffer_state
                observed = []

                def poll_after_first_bytes(session_id):
                    started(session_id)
                    self.service.monitor._poll_once()
                    observed.append(state(session_id)['state'])

                def clock_after_startup(session_id):
                    current = state(session_id)
                    if observed:
                        media = self.service.sessions.get(session_id)
                        with media.buffer_budget.lock:
                            head = max([media.buffer_budget.head, *(end for _, end in media.buffer_budget.ranges)])
                        self.player.feed(123, FakeSample(123, 'https://quark.invalid/video/' + fid + '?account=' + media.account_id,
                                                        max(1, head), size, 2))
                        self.service.monitor._poll_once()
                        return state(session_id)
                    return current

                with patch.object(self.service.monitor, 'stream_started', side_effect=poll_after_first_bytes), \
                        patch.object(self.service.monitor, 'buffer_state', side_effect=clock_after_startup):
                    response = self.stream(payload['url'])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.content), size)
                self.assertIn('mismatch', observed)

    def test_previously_playing_video_still_stops_when_player_switches_away(self):
        from unittest.mock import patch
        from test_potplayer_playback import FakeSample
        size = 512 * 1024
        self.fixture.records.append(record('switched', 'video.mkv', 'root-dir', size=size))
        self.fixture.virtual['switched'] = size
        payload = self.api('POST', '/v1/play', body={'fid': 'switched', 'player_pid': 123}).json()
        sid = payload['url'].split('/')[3]
        self.service.monitor.stream_started(sid)
        self.player.feed(123, FakeSample(123, 'https://quark.invalid/video/switched', 1000, 100_000, 2))
        self.service.monitor._poll_once()
        self.player.feed(123, FakeSample(123, 'https://quark.invalid/video/other', 1000, 100_000, 2))
        started = self.service.monitor.stream_started

        def poll_after_first_bytes(session_id):
            started(session_id)
            self.service.monitor._poll_once()

        with patch.object(self.service.monitor, 'stream_started', side_effect=poll_after_first_bytes):
            with self.assertRaises(httpx.RemoteProtocolError):
                self.stream(payload['url'])

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
