"""Navigation assets, plain-language diagnostics and explicit window reuse."""
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from quark_cli.potplayer.accounts_gui import AccountsPanel
from quark_cli.potplayer.status_gui import diagnostic_summary
from test_potplayer_service import _ServerCase
from potplayer_fixture import COOKIE


class PresentationTests(unittest.TestCase):
    def test_navigation_icons_are_semantic_and_window_icon_stays_potplayer(self):
        directory = Path(__file__).resolve().parents[1] / 'potplayer'
        from quark_cli.potplayer.window_ui import ICON_NAME
        self.assertEqual(ICON_NAME, 'MediaPlayParse - Quark.ico')
        player_icon = (directory / ICON_NAME).read_bytes()
        icons = [(directory / 'icons' / (name + '.ico')).read_bytes()
                 for name in ('cloud', 'search', 'folder-heart', 'history', 'clapperboard', 'bookmark', 'settings')]
        self.assertEqual(len(set(icons)), 7)
        self.assertNotIn(player_icon, icons)
        self.assertEqual((directory / 'MediaUrlList - Quark.ico').read_bytes(), icons[0])
        for icon in icons + [player_icon]:
            reserved, kind, count = struct.unpack_from('<HHH', icon)
            self.assertEqual((reserved, kind), (0, 1))
            self.assertGreaterEqual(count, 2)

    def test_root_names_have_no_parenthetical_explanations(self):
        from test_potplayer_extensions import body_raw
        script = (Path(__file__).resolve().parents[1] / 'potplayer/MediaUrlList - Quark.as').read_text('utf-8-sig')
        categories = body_raw(script, 'array<dictionary> GetCategorys()')
        self.assertIn('{$CP936=追剧中心$}', categories)
        self.assertIn('{$CP936=插件管理$}', categories)
        self.assertNotIn('追剧中心（', categories)
        self.assertNotIn('插件管理（', categories)

    def test_zero_speed_is_not_reported_as_a_playback_failure(self):
        summary = diagnostic_summary({'account': {'credential_present': True},
                                      'playback': [{'state': 'playing'}],
                                      'transfer': {'totals': {'speed_bytes_per_second': 0}}})
        self.assertIn('视频正在播放', summary)
        self.assertIn('不一定是故障', summary)
        self.assertIn('已有足够缓存', summary)

    def test_summary_explains_pause_login_and_remote_wait(self):
        base = {'account': {'credential_present': True}, 'playback': [{'state': 'paused'}]}
        self.assertIn('视频已暂停', diagnostic_summary(base))
        self.assertIn('账号管理', diagnostic_summary({}))
        self.assertIn('重新扫码登录', diagnostic_summary({'account': {'credential_present': True, 'validation': 'expired'}}))
        self.assertIn('请等待', diagnostic_summary(dict(base, protection={'active': True})))

    def test_default_account_page_refreshes_once_after_its_first_snapshot(self):
        page = AccountsPanel(None, None)
        page._refresh_accounts = Mock()
        page._account_command = Mock()
        page.activate()
        page._account_command.assert_not_called()
        snapshot = {'active': 'demo', 'items': []}
        page._handle('snapshot', snapshot)
        page._handle('snapshot', snapshot)
        page._account_command.assert_called_once_with('refresh')


class WindowReuseTests(_ServerCase):
    def service_kwargs(self):
        return {'transport': self.fixture.transport(), 'cookie': COOKIE, 'monitor_start': False}

    def test_repeated_management_open_raises_existing_process(self):
        self.service._status_process = SimpleNamespace(pid=12345, poll=lambda: None)
        with patch('quark_cli.potplayer.service.foreground_process') as foreground:
            self.assertEqual(self.service.open_plain_status_window(), 'already_open')
        foreground.assert_called_once_with(12345)

    def test_repeated_highlight_open_raises_only_the_highlight_process(self):
        self.service._highlight_process = SimpleNamespace(pid=54321, poll=lambda: None)
        with patch('quark_cli.potplayer.service.foreground_process') as foreground:
            self.assertEqual(self.service.request_bookmark_window(), 'already_open')
        foreground.assert_called_once_with(54321)
        self.assertIsNone(self.service._status_process)
