"""Rules and actual seek decisions, without network or a real player."""
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quark_cli.errors import LocalIOError, UsageError
from quark_cli.potplayer.series_settings import SeriesSettings
from quark_cli.potplayer.playback import PlaybackMonitor
from quark_cli.potplayer.library_state import Library
from quark_cli.potplayer.bookmark_state import BookmarkStore
from test_potplayer_playback import FakePlayer, sample, session
from test_potplayer_service import _ServerCase
from potplayer_fixture import COOKIE, record


class SeriesSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='quark-series-rules-'))
        self.addCleanup(shutil.rmtree, self.directory)
        self.rules = SeriesSettings(self.directory / 'account-a' / 'series-settings.json')

    def test_rules_persist_per_series_and_per_account(self):
        self.rules.save('show-a', 90, 120)
        self.rules.save('show-b', 30, 40)
        self.assertEqual(SeriesSettings(self.rules.path).get('show-a')['intro_seconds'], 90)
        self.assertEqual(self.rules.get('show-b')['outro_seconds'], 40)
        self.assertEqual(SeriesSettings(self.directory / 'account-b.json').get('show-a')['intro_seconds'], 0)
        self.rules.save('show-a', 0, 0)
        self.assertEqual(self.rules.get('show-a')['outro_seconds'], 0)

    def test_invalid_rules_and_corrupt_document_are_not_overwritten(self):
        for value in (-1, 3601, True, '90', 1.5):
            with self.assertRaises(UsageError):
                self.rules.save('show-a', value, 0)
        with self.assertRaises(UsageError):
            self.rules.save('0', 10, 10)
        self.rules.path.parent.mkdir(parents=True, exist_ok=True)
        self.rules.path.write_text('{"version":1,"series":[]}', encoding='utf-8')
        before = self.rules.path.read_bytes()
        with self.assertRaises(LocalIOError):
            self.rules.save('show-a', 10, 10)
        self.assertEqual(self.rules.path.read_bytes(), before)

    def setup_monitor(self, *, bookmark=False):
        self.rules.save('show-a', 90, 120)
        library = Library(self.directory / 'library.json')
        library.record_recent('v1', '01.mkv', 100)
        player = FakePlayer()
        monitor = PlaybackMonitor(library, native_factory=lambda: player, start=False, series_settings=self.rules,
                                  bookmarks=BookmarkStore(self.directory / 'bookmark.json'))
        media = session()
        media.context = {'series_fid':'show-a'}
        if bookmark:
            media.bookmark_id = 'missing'
        monitor.bind(media, 100)
        monitor.stream_started(media.session)
        self.addCleanup(monitor.close)
        return monitor, player, library

    def test_new_play_skips_intro_once_and_outro_at_threshold(self):
        monitor, player, library = self.setup_monitor()
        player.feed(100, sample(100, position_ms=1000))
        monitor._poll_once()
        self.assertEqual(player.seeks[-1][2], 90000)
        player.feed(100, sample(100, position_ms=1000))
        monitor._poll_once()
        self.assertEqual(len(player.seeks), 1, 'manual backward seek must not bounce')
        player.feed(100, sample(100, position_ms=1680000))
        monitor._poll_once()
        self.assertEqual(player.seeks[-1][2], 1799000)
        monitor._poll_once()
        self.assertEqual(len(player.seeks), 2)
        self.assertTrue(library.is_watched('v1', 100))
        self.assertGreaterEqual(library.snapshot()['recent'][0]['position_ms'], 1680000)

    def test_saved_resume_wins_over_intro(self):
        monitor, player, library = self.setup_monitor()
        library.save_progress('v1', 100, 300000, 1800000)
        player.feed(100, sample(100, position_ms=1000), sample(100, position_ms=300000))
        monitor._poll_once()
        self.assertEqual(player.seeks[-1][2], 300000)

    def test_pause_then_play_skips_intro_and_failed_seek_does_not_loop(self):
        monitor, player, _ = self.setup_monitor()
        player.feed(100, sample(100, position_ms=1000, status=1))
        monitor._poll_once()
        player.feed(100, sample(100, position_ms=1000))
        player.seek_ok = False
        monitor._poll_once()
        monitor._poll_once()
        self.assertEqual([target for _, _, target in player.seeks], [90000])

    def test_user_seek_during_sampling_and_history_removal_prevent_auto_seek(self):
        monitor, player, _ = self.setup_monitor()
        player.feed(100, sample(100, position_ms=1000), sample(100, position_ms=300000))
        monitor._poll_once()
        self.assertEqual(player.seeks, [])
        monitor.forget_fid('v1')
        player.feed(100, sample(100, position_ms=1680000))
        monitor._poll_once()
        self.assertEqual(player.seeks, [])

    def test_monitor_closed_during_native_read_never_sends_a_late_seek(self):
        monitor, player, _ = self.setup_monitor()
        reading = sample(100, position_ms=1000)
        calls = 0
        def native_read(pid):
            nonlocal calls
            calls += 1
            if calls == 2:
                monitor.close()
            return reading
        player.sample = native_read
        monitor._poll_once()
        self.assertEqual(player.seeks, [])

    def test_paused_mismatched_or_too_short_video_is_not_skipped(self):
        for reading in (sample(100, position_ms=1000, status=1),
                        sample(100, name='https://quark.invalid/video/other', position_ms=1000),
                        sample(100, position_ms=1000, duration_ms=200000)):
            with self.subTest(reading=reading):
                monitor, player, _ = self.setup_monitor()
                player.feed(100, reading)
                monitor._poll_once()
                self.assertEqual(player.seeks, [])

    def test_unknown_series_and_explicit_mark_do_not_apply_intro(self):
        monitor, player, _ = self.setup_monitor(bookmark=True)
        player.feed(100, sample(100, position_ms=1000))
        monitor._poll_once()
        self.assertEqual(player.seeks, [])
        monitor.bind(session(sid='unknown-series'), 100)
        monitor.stream_started('unknown-series')
        monitor._poll_once()
        self.assertEqual(player.seeks, [])

    def test_explicit_highlight_wins_and_same_file_conflicts_never_auto_seek(self):
        monitor, player, _ = self.setup_monitor()
        saved = monitor.bookmarks.add('v1', '01.mkv', 100, 15000, 1800000, '片头内标记')
        monitor._bindings[100].session.bookmark_id = saved['id']
        player.feed(100, sample(100, position_ms=1000), sample(100, position_ms=1000), sample(100, position_ms=15000))
        monitor._poll_once()
        self.assertEqual(player.seeks[-1][2], 15000)
        player.feed(100, sample(100, position_ms=1680000))
        monitor._poll_once()
        self.assertEqual(len(player.seeks), 1)
        monitor.bind(session(sid='other-player'), 101)
        monitor.stream_started('other-player')
        player.feed(101, sample(101, position_ms=1000))
        monitor._poll_once()
        self.assertEqual(len(player.seeks), 1)


class SeriesSettingsServiceTests(_ServerCase):
    def service_kwargs(self):
        self.fixture.records = [record('media', '影视', directory=True),
                                record('tv', '电视剧', 'media', directory=True),
                                record('show', '蒸蒸日上', 'tv', directory=True),
                                record('quality', '4K', 'show', directory=True),
                                record('quality2', '1080p', 'show', directory=True),
                                record('ep', '04.mkv', 'quality')]
        return {'transport': self.fixture.transport(), 'cookie': COOKIE, 'monitor_start': False}

    def test_versions_share_real_series_rule_and_window_is_reused(self):
        with patch('quark_cli.potplayer.service.subprocess.Popen', return_value=SimpleNamespace(pid=1234, poll=lambda: None)) as spawn:
            response = self.api('POST', '/v1/series-settings/open', body={'parent': 'quality'})
            self.assertEqual(response.status_code, 202, response.text)
            args = spawn.call_args.args[0]
            self.assertEqual(args[args.index('--series-fid') + 1], 'show')
            self.assertNotIn(COOKIE, str(args))
            with patch('quark_cli.potplayer.service.foreground_process') as foreground:
                self.assertEqual(self.api('POST', '/v1/series-settings/open', body={'parent':'quality2'}).json()['status'], 'already_open')
                foreground.assert_called_once_with(1234)
        result = self.api('POST', '/v1/series-settings', body={'series':'show', 'intro_seconds':90, 'outro_seconds':120})
        self.assertEqual(result.status_code, 200, result.text)
        result = self.api('GET', '/v1/series-settings?series=quality2').json()
        self.assertEqual((result['series'], result['name'], result['intro_seconds']), ('show', '蒸蒸日上', 90))
        self.api('GET', '/v1/files?parent=quality')
        media = self.service.new_session(SimpleNamespace(fid='ep', name='04.mkv', size=2048, url='https://origin/ep'))
        self.assertEqual(media.context['series_fid'], 'show')

    def test_old_account_window_cannot_read_or_save_new_account_rules(self):
        self.api('GET', '/v1/files?parent=quality')
        a = self.service.accounts.active
        self.service.series_settings.save('show', 90, 120)
        with self.service.account_lock:
            b = self.service.accounts.add('__uid=series-account-b; __puus=fake')
            self.service._activate_account()
        for method, path, body in (
            ('GET', '/v1/series-settings?series=show', None),
            ('POST', '/v1/series-settings', {'series':'show', 'intro_seconds':1, 'outro_seconds':2}),
            ('POST', '/v1/series-settings/open', {'parent':'quality'}),
        ):
            self.assertEqual(self.api(method, path, body=body, headers={'X-Quark-Account':a}).status_code, 409)
        self.assertEqual(self.service.series_settings.get('show')['intro_seconds'], 0)
        self.service.switch_account(a)
        self.assertEqual(self.service.series_settings.get('show')['intro_seconds'], 90)

    def test_settings_window_client_keeps_its_account_after_scoped_worker_commands(self):
        from quark_cli.potplayer.series_gui import SeriesClient, SeriesWorker
        from quark_cli.potplayer.gui_common import Endpoint
        import queue
        client = SeriesClient(Endpoint(self.base, self.token, self.service.instance, self.service.port),
                              'show', self.service.accounts.active)
        self.addCleanup(client.close)
        self.api('GET', '/v1/files?parent=quality')
        worker = SeriesWorker(client, queue.Queue(), queue.Queue())
        account = self.service.accounts.active
        worker._serve(('scoped', account, ('save', {'series':'show', 'intro_seconds':90, 'outro_seconds':120})))
        self.assertEqual(client._account_header(), {'X-Quark-Account':account})
        self.assertEqual(client.snapshot()['intro_seconds'], 90)

    def test_bad_input_and_root_do_not_create_rules(self):
        self.assertEqual(self.api('POST', '/v1/series-settings/open', body={'parent':'0'}).status_code, 400)
        self.api('GET', '/v1/files?parent=quality')
        self.assertEqual(self.api('POST', '/v1/series-settings', body={'series':'show', 'intro_seconds':-1, 'outro_seconds':0}).status_code, 400)
        self.assertFalse(self.service.series_settings.path.exists())
