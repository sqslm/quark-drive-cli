"""Highlight browser regressions through the real loopback routes."""
import unittest
from unittest.mock import Mock

from test_potplayer_watch_center import _Case
from test_potplayer_status_gui import _highlights_panel, _FakeDialog, BOOKMARK_ENTRY


class HighlightBrowserCase(_Case):
    def test_open_launches_only_the_highlight_window_and_reuses_it(self):
        process = Mock()
        process.poll.return_value = None
        launch = self.service._highlight_launcher = Mock(return_value=process)
        for _ in range(2):
            self.assertEqual(self.api('POST', '/v1/bookmarks/open', body={'fid': 'v2'}).status_code, 202)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.launcher.calls, 0)

    def test_directory_request_preserves_scope_and_capture_action(self):
        self.service._highlight_launcher = Mock(return_value=Mock())
        response = self.api('POST', '/v1/bookmarks/open', body={'parent': 'root-dir', 'action': 'add'})
        self.assertEqual(response.status_code, 202)
        request = self.api('GET', '/v1/bookmarks/state').json()['bookmark_request']
        self.assertEqual(request['scope'], 'parent')
        self.assertEqual(request['parent'], 'root-dir')
        self.assertEqual(request['action'], 'add')

    def test_groups_use_real_directory_identity_and_survive_history_removal(self):
        store = self.service.monitor.bookmarks
        for fid, parent, title in [('v2', 'root-dir', '兰香如故'), ('v9', 'other-dir', '兰香如故')]:
            store.add(fid, '01.mkv', 2048, 30_000, 900_000, '精彩原因')
            self.service.library.record_recent(fid, '01.mkv', 2048, parent, parent_name=title)
        payload = self.api('GET', '/v1/bookmarks').json()
        self.assertEqual({g['parent'] for g in payload['groups']}, {'root-dir', 'other-dir'})
        self.service.library.clear_recent()
        again = self.api('GET', '/v1/bookmarks').json()
        self.assertEqual(again['groups'], payload['groups'])
        selected = self.api('GET', '/v1/bookmarks?parent=root-dir').json()['items']
        self.assertEqual([row['fid'] for row in selected], ['v2'])
        self.assertEqual(selected[0]['note'], '精彩原因')

    def test_unknown_legacy_mark_is_visible_without_cloud_lookup(self):
        self.service.monitor.bookmarks.add('ghost', '01.mkv', 2048, 1_000, 9_000, '')
        before = len(self.fixture.requests)
        payload = self.api('GET', '/v1/bookmarks').json()
        self.assertEqual(payload['groups'][0]['name'], '未分类')
        self.assertEqual(len(self.fixture.requests), before)

    def test_new_mark_keeps_directory_without_a_recent_record(self):
        self.fixture.records[0]['file_name'] = '兰香如故'
        store = self.service.monitor.bookmarks
        store.add('v2', '01.mkv', 2048, 1_000, 9_000, '对白', parent='root-dir', parent_name='兰香如故')
        self.assertEqual(self.api('GET', '/v1/bookmarks').json()['groups'],
                         [{'parent': 'root-dir', 'name': '兰香如故', 'count': 1, 'category': '未分类'}])

    def test_directory_filter_never_fetches_the_drive_for_unknown_marks(self):
        self.service.monitor.bookmarks.add('ghost', '01.mkv', 2048, 1_000, 9_000, '')
        before = len(self.fixture.requests)
        self.assertEqual(self.api('GET', '/v1/bookmarks?parent=root-dir').json()['items'], [])
        self.assertEqual(len(self.fixture.requests), before)

    def test_session_opened_from_recent_keeps_directory_after_service_restart(self):
        from quark_cli.potplayer.stream import Media
        self.service.library.record_recent('v2', '01.mkv', 2048, 'root-dir', '兰香如故')
        session = self.service.new_session(Media('v2', '01.mkv', 2048, 'https://example.test/video'))
        self.service.record_played(session)
        self.assertEqual((session.parent, session.parent_name), ('root-dir', '兰香如故'))


class HighlightBrowserUiTests(unittest.TestCase):
    def test_folders_open_only_their_episodes_and_back_restores_groups(self):
        panel = _highlights_panel(rows=[], selected=None)
        panel._external = {'id': 'all', 'fid': '', 'name': '全部视频', 'scope': 'all'}
        panel._bookmarks = [dict(BOOKMARK_ENTRY, parent=parent, parent_name='同名剧集', id=key, note=note)
                            for parent, key, note in [('a', 'b1', '对白'), ('b', 'b2', '打斗')]]
        panel._render_bookmarks()
        self.assertEqual(len(panel._bookmark_tree.rows), 2)
        panel._bookmark_tree.selected = ('group1',)
        panel._enter_group_or_jump()
        self.assertEqual(len(panel._bookmark_tree.rows), 1)
        self.assertEqual(panel._bookmark_tree.rows[0][1][2], '打斗')
        self.assertEqual(list(panel._bookmark_rows.values()), ['b2'])
        panel._back_to_groups()
        self.assertEqual(len(panel._bookmark_tree.rows), 2)
        self.assertEqual(panel._bookmark_rows, {})

    def test_directory_capture_opens_note_dialog_and_uses_real_player(self):
        dialog = _FakeDialog(answer='精彩原因')
        panel = _highlights_panel(rows=[{'fid': 'F1', 'player_pid': 12, 'state': 'playing', 'parent': 'series'}],
                                  selected=None, dialog=dialog)
        panel._consume_bookmark_request({'bookmark_request': {
            'id': 'capture', 'scope': 'parent', 'parent': 'series', 'action': 'add'}})
        self.assertEqual(panel._requests.get_nowait(), ('bookmarks', 0, 'parent/series'))
        panel._apply_bookmarks((0, 'parent/series', []))
        self.assertEqual(dialog.calls[0][0], '标记当前时间点')
        self.assertEqual(panel._requests.get_nowait(), ('bookmark_add', 12, 'F1', '精彩原因'))

    def test_capture_rejects_a_video_from_another_directory(self):
        panel = _highlights_panel(rows=[{'fid': 'F1', 'player_pid': 12, 'state': 'playing', 'parent': 'other'}], selected=None)
        panel._external = {'id': 'dir', 'scope': 'parent', 'parent': 'series', 'fid': '', 'name': '当前目录'}
        self.assertIsNone(panel._action_target())
