import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

from quark_cli.potplayer.gui_transport import LocalPanel, LocalWorker


class LifecycleTests(unittest.TestCase):
    def test_unknown_or_zero_capacity_never_invents_a_percentage(self):
        from quark_cli.potplayer.accounts_gui import capacity_text
        self.assertEqual(capacity_text(None,None), '— / —')
        self.assertEqual(capacity_text(0,0), '0 B / —')
        self.assertEqual(capacity_text(5,10), '5 B / 50.00%')
    def test_management_shows_without_a_blocking_health_probe(self):
        from quark_cli.potplayer import management_gui
        runtime={'port':12345,'token':'private-fixture','instance':'fixture'}
        with patch.object(management_gui,'ManagementWindow') as window, patch.object(
            management_gui.BridgeClient,'healthy',side_effect=AssertionError('synchronous health probe')):
            window.return_value.run.return_value=0
            self.assertEqual(management_gui.run_management_window(lambda:runtime),0)

    def test_close_never_waits_for_a_slow_request_or_closes_its_active_transport(self):
        entered, release = threading.Event(), threading.Event()
        client = Mock()
        def slow_snapshot():
            entered.set()
            release.wait(2)
            return {}
        client.snapshot.side_effect = slow_snapshot
        panel = LocalPanel(client, None)
        worker = panel._worker = LocalWorker(client, queue.Queue(), queue.Queue())
        thread = panel._thread = threading.Thread(target=worker.run)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            started = time.monotonic()
            panel._shutdown()
            elapsed = time.monotonic()-started
            self.assertLess(elapsed, .15)
            client.close.assert_not_called()
        finally:
            release.set()
            thread.join(2)
        client.close.assert_called_once()

    def test_idle_worker_cancellation_wakes_immediately(self):
        client = Mock(snapshot=Mock(return_value={}))
        worker = LocalWorker(client, queue.Queue(), queue.Queue(), interval=30)
        thread = threading.Thread(target=worker.run)
        thread.start()
        try:
            time.sleep(.03)
            worker.cancel()
            thread.join(.15)
            self.assertFalse(thread.is_alive())
        finally:
            worker._requests.put(None)
            thread.join(1)

    def test_foreground_never_attaches_to_player_input_or_calls_synchronous_show(self):
        from quark_cli.potplayer import window_ui
        api = Mock()
        api.IsIconic.return_value = True
        api.SetForegroundWindow.return_value = False
        with patch.object(window_ui, '_user32', return_value=api):
            window_ui._foreground(123)
        api.ShowWindow.assert_not_called()
        api.AttachThreadInput.assert_not_called()
        api.ShowWindowAsync.assert_called_once_with(123, 9)


@unittest.skipUnless(__import__('sys').platform == 'win32', 'Windows Tk')
class LayoutTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.geometry('800x700')
        self.addCleanup(self.root.destroy)
        from quark_cli.potplayer.ui_theme import apply_theme
        apply_theme(self.root)

    def test_account_capacity_has_used_and_remaining_percentages(self):
        from quark_cli.potplayer.accounts_gui import AccountsPanel
        panel = AccountsPanel(None, None)
        panel.mount(self.tk, self.root, self.root)
        self.addCleanup(panel._shutdown)
        panel._refresh_accounts({'accounts':{'active':'a','items':[
            {'id':'a','nickname':'账号','current':True,'logged_in':True,
             'capacity':{'used_bytes':256*1024**3,'total_bytes':1024*1024**3}}]}})
        columns = panel._accounts_tree.cget('columns')
        self.assertIn('used', columns)
        values = panel._accounts_tree.item('a','values')
        self.assertEqual(values[columns.index('used')], '256.00 GiB / 25.00%')
        self.assertEqual(values[columns.index('remaining')], '768.00 GiB / 75.00%')

    def test_highlight_default_width_shows_complete_back_button(self):
        from quark_cli.potplayer.highlights_gui import HighlightsPanel
        panel = HighlightsPanel(None, None)
        panel.mount(self.tk, self.root, self.root)
        self.addCleanup(panel._shutdown)
        self.root.update()
        from quark_cli.potplayer.gui_common import _widgets
        button = next(w for w in _widgets(self.root) if w.winfo_class()=='TButton'
                      and w.cget('text')=='返回剧集列表')
        self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
        self.assertLessEqual(button.winfo_rootx()+button.winfo_reqwidth(),
                             self.root.winfo_rootx()+self.root.winfo_width())
