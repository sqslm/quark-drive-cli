"""Regressions for popup return locations and version-only video labels."""
from pathlib import Path
import unittest
import sys

from test_potplayer_extensions import body_raw
from test_potplayer_service import _ServerCase
from potplayer_fixture import COOKIE, record


class PopupReturnTests(unittest.TestCase):
    def test_all_popup_openers_request_origin_restoration(self):
        script = (Path(__file__).resolve().parents[1] / 'potplayer/MediaUrlList - Quark.as').read_text('utf-8-sig')
        for signature in ('array<dictionary> QuarkBookmarksOpenPage(string fid)',
                          'array<dictionary> QuarkHighlightsOpenPage(string parent, bool capture)'):
            with self.subTest(signature=signature):
                self.assertIn('QuarkWindowResult(', body_raw(script, signature))
        entry = body_raw(script, 'array<dictionary> QuarkAccountUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)')
        for message in ('已打开片头片尾设置窗口', '已打开全部精彩标记管理窗口'):
            line = next(line for line in entry.splitlines() if message in line)
            self.assertIn('QuarkWindowResult(', line)

    def test_navigation_keeps_explicit_origin_instead_of_native_parent_guess(self):
        script = (Path(__file__).resolve().parents[1] / 'potplayer/MediaUrlList - Quark.as').read_text('utf-8-sig')
        entry = body_raw(script, 'array<dictionary> GetUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)')
        self.assertIn('quark-origin:', entry)
        self.assertIn('HostUrlEncode(origin)', entry)
        self.assertIn('HostUrlDecode(', entry)
        self.assertIn('QuarkRestoreOrigin(', entry)
        self.assertIn('ret[index]["PathToken"] = "quark-back"', entry)
        self.assertIn('folder == "parent"', entry)
        self.assertIn('"|quark-page:" + HostUrlEncode(PathToken)', entry)
        self.assertLess(entry.index('ret = QuarkRestoreOrigin('), entry.index('ret[index]["PathToken"]'))
        restore = body_raw(script, 'array<dictionary> QuarkRestoreOrigin(string Category, string Extra, string origin, string Query, string page)')
        self.assertIn('QuarkDirectoryList(origin, page, true)', restore)

    def test_account_validation_happens_before_origin_decode_and_every_return_is_scoped(self):
        script = (Path(__file__).resolve().parents[1] / 'potplayer/MediaUrlList - Quark.as').read_text('utf-8-sig')
        entry = body_raw(script, 'array<dictionary> GetUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)')
        self.assertLess(entry.index('!QuarkValidAccount(QUARK_ACCOUNT_SCOPE)'), entry.index('string navigation = "quark-origin:"'))
        self.assertIn('ret[index]["PathToken"] = tag + QUARK_ACCOUNT_SCOPE + "|" + value', entry)
        self.assertIn('ret[index]["PageToken"] = tag + QUARK_ACCOUNT_SCOPE', entry)


class VersionLabelTests(_ServerCase):
    def service_kwargs(self):
        self.fixture.records = [record('media', '影视', directory=True),
                                record('tv', '电视剧', 'media', directory=True),
                                record('show', '蒸蒸日上', 'tv', directory=True),
                                record('quality', '4K', 'show', directory=True),
                                record('ep', '04.mkv', 'quality'),
                                record('show2', '兰香如故', 'tv', directory=True),
                                record('quality2', '4K', 'show2', directory=True),
                                record('ep2', '04.mkv', 'quality2')]
        return {'transport':self.fixture.transport(), 'cookie':COOKIE, 'monitor_start':False}

    def test_recent_continue_watch_center_and_favorites_identify_each_title(self):
        for parent, fid in (('quality', 'ep'), ('quality2', 'ep2')):
            self.assertEqual(self.api('GET', '/v1/files?parent=' + parent).status_code, 200)
            self.service.library.record_recent(fid, '04.mkv', 2048, parent, '4K')
            self.service.library.save_progress(fid, 2048, 60000, 600000)
            self.service.library.add_favorite(parent, '4K')
        for path in ('recent', 'continue', 'watch-center', 'favorites'):
            with self.subTest(path=path):
                rows = self.api('GET', '/v1/' + path).json()['items']
                text = str(rows)
                self.assertIn('蒸蒸日上', text)
                self.assertIn('兰香如故', text)
                if path in ('recent', 'continue'):
                    self.assertTrue(all('4K' in row['display'] and '04.mkv' in row['display'] for row in rows))

    def test_old_history_rehydrates_missing_ancestors_without_user_rebrowsing(self):
        self.service.library.record_recent('ep', '04.mkv', 2048, 'quality', '4K')
        rows = self.api('GET', '/v1/recent').json()['items']
        self.assertIn('蒸蒸日上', rows[0]['display'])
        before = len(self.fixture.requests)
        for route in ('recent', 'watch-center', 'continue'):
            self.api('GET', '/v1/' + route)
        self.assertEqual(len(self.fixture.requests), before, 'resolved ancestors must be reused, without scans')

    def test_unavailable_metadata_keeps_history_with_a_visible_notice(self):
        from unittest.mock import Mock, patch
        from quark_cli.errors import RateLimitError
        self.service.library.record_recent('ep', '04.mkv', 2048, 'quality', '4K')
        with patch.object(self.service, 'files', return_value=Mock(info=Mock(side_effect=RateLimitError('fixture cooldown')))):
            payload = self.api('GET', '/v1/recent').json()
        self.assertEqual(payload['items'][0]['fid'], 'ep')
        self.assertIn('名称尚未补全', payload['metadata_notice'])

    def test_empty_history_does_not_fetch_unrelated_favorite_ancestors(self):
        self.service.library.add_favorite('quality', '4K')
        before = len(self.fixture.requests)
        self.assertEqual(self.api('GET', '/v1/recent').json()['items'], [])
        self.assertEqual(self.api('GET', '/v1/watch-center').json()['items'], [])
        self.assertEqual(len(self.fixture.requests), before)

    def test_arbitrary_versions_movies_and_restart_share_the_content_name(self):
        self.api('GET', '/v1/files?parent=quality')
        for version in ('1080p', '蓝光', '国语配音版', '收藏版', '4K HDR DV', '第一季/国语'):
            with self.subTest(version=version):
                self.service.directories.note('show', [{'fid':'variant', 'name':version, 'is_dir':True}])
                from quark_cli.potplayer.directory_state import Directories
                context = Directories(self.service.directories.path).context('variant')
                self.assertEqual(context['series_name'], '蒸蒸日上')
                self.assertEqual(context['video_prefix'], '蒸蒸日上/' + version)
                self.assertEqual(context['category'], '电视剧')
        self.service.directories.note('media', [{'fid':'movie-category','name':'电影','is_dir':True}])
        self.service.directories.note('movie-category', [{'fid':'movie','name':'示例电影','is_dir':True}])
        self.service.directories.note('movie', [{'fid':'movie-version','name':'导演剪辑版','is_dir':True}])
        self.assertEqual(self.service.directories.context('movie-version')['video_prefix'], '示例电影/导演剪辑版')
        self.service.directories.note('show', [{'fid':'season','name':'第一季','is_dir':True}])
        self.service.directories.note('season', [{'fid':'language','name':'粤语配音版','is_dir':True}])
        self.assertEqual(self.service.directories.context('language')['video_prefix'], '蒸蒸日上/第一季/粤语配音版')


@unittest.skipUnless(sys.platform == 'win32', 'Windows Tk controls')
class DurationWidgetTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_minute_second_inputs_and_slider_update_each_other(self):
        from tkinter import ttk
        from quark_cli.potplayer.series_gui import DurationControl, SkipRangeControl
        value = DurationControl(self.tk, ttk, self.root, '片头')
        outro = DurationControl(self.tk, ttk, self.root, '片尾')
        self.addCleanup(value.close)
        self.addCleanup(outro.close)
        slider = SkipRangeControl(self.tk, self.root, value, outro)
        value.set(90)
        self.assertEqual((value.minutes.get(), value.seconds.get()), ('1', '30'))
        slider._set(125)
        self.root.update()
        self.assertEqual(value.get(), 125)
        self.assertEqual((value.minutes.get(), value.seconds.get()), ('2', '5'))
        value.minutes.set('3')
        value.seconds.set('20')
        self.assertEqual(value.get(), 200)
        value.set(900)
        self.assertGreaterEqual(slider.limit, 900)
        value.minutes.set('60')
        value.seconds.set('0')
        self.assertEqual(value.get(), 3600)
        value.seconds.set('1')
        from quark_cli.errors import UsageError
        with self.assertRaises(UsageError):
            value.get()
        value.seconds.set('60')
        with self.assertRaises(UsageError):
            value.get()

    def test_one_track_opposing_drags_do_not_change_the_other_end(self):
        from tkinter import ttk
        from types import SimpleNamespace
        from quark_cli.potplayer.series_gui import DurationControl, SkipRangeControl
        frame = ttk.Frame(self.root)
        frame.pack()
        intro = DurationControl(self.tk, ttk, frame, '片头')
        outro = DurationControl(self.tk, ttk, frame, '片尾')
        self.addCleanup(intro.close)
        self.addCleanup(outro.close)
        slider = SkipRangeControl(self.tk, self.root, intro, outro)
        self.root.geometry('600x250')
        self.root.deiconify()
        self.root.update()
        intro.set(90)
        outro.set(145)
        start, half = slider._geometry()
        left, right = slider.positions()
        slider._press(SimpleNamespace(x=left))
        slider._drag(SimpleNamespace(x=start+half/2))
        self.assertEqual((intro.get(), outro.get()), (300, 145))
        slider._press(SimpleNamespace(x=right))
        slider._drag(SimpleNamespace(x=start+half*1.5))
        self.assertEqual((intro.get(), outro.get()), (300, 300))
        slider._step(-1)
        self.assertEqual(outro.get(), 301, 'left arrow increases skip-from-end')
        slider._set(0)
        self.assertEqual(outro.get(), 0)
        self.assertEqual(slider.positions()[1], start+2*half)

    def test_close_does_not_save_and_save_converts_to_total_seconds(self):
        from quark_cli.potplayer.series_gui import SeriesPanel
        panel = SeriesPanel(None, None, 'show', 'account-a')
        panel._root = self.root
        panel._build(self.tk, self.root)
        self.addCleanup(panel._shutdown)
        panel._handle('snapshot', {'name':'蒸蒸日上','intro_seconds':90,'outro_seconds':120})
        panel._intro.minutes.set('2')
        panel._intro.seconds.set('15')
        panel._close()
        self.assertTrue(panel._requests.empty(), 'closing an edited dialog must not write a rule')
        panel.save()
        scope, account, command = panel._requests.get_nowait()
        self.assertEqual((scope, account), ('scoped', 'account-a'))
        self.assertEqual(command, ('save', {'series':'show','intro_seconds':135,'outro_seconds':120}))
