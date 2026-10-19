"""Real Tk checks for shared controls and modal cancel/confirm behavior."""
import sys
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Windows Tk UI')
class ThemeTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from quark_cli.potplayer.ui_theme import apply_theme
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        apply_theme(self.root)

    def test_all_management_pages_share_the_same_dark_controls(self):
        from tkinter import ttk
        from quark_cli.potplayer.accounts_gui import AccountsPanel
        from quark_cli.potplayer.highlights_gui import HighlightsPanel
        from quark_cli.potplayer.status_gui import StatusWindow
        from quark_cli.potplayer.installation_gui import InstallationPanel
        from quark_cli.potplayer.ui_theme import BACKGROUND, SURFACE, ACCENT
        for page_type in (AccountsPanel, HighlightsPanel, StatusWindow, InstallationPanel):
            frame = ttk.Frame(self.root)
            panel = page_type(None, None)
            panel.mount(__import__('tkinter'), self.root, frame)
            self.addCleanup(panel._shutdown)
        style = ttk.Style(self.root)
        self.assertEqual(style.lookup('TFrame', 'background'), BACKGROUND)
        self.assertEqual(style.lookup('Treeview', 'fieldbackground'), SURFACE)
        self.assertEqual(style.lookup('TEntry', 'fieldbackground'), SURFACE)
        self.assertEqual(style.lookup('TNotebook.Tab', 'foreground', ('selected',)), ACCENT)
        self.assertEqual(self.root.cget('background'), BACKGROUND)

    def test_note_confirm_and_cancel_use_themed_modal_and_keep_result(self):
        import weakref
        from quark_cli.potplayer import dialogs
        from quark_cli.potplayer.ui_theme import BACKGROUND
        seen = []
        closed = []
        def complete(cancel=False):
            window = next(w for w in self.root.winfo_children() if isinstance(w, dialogs._Prompt))
            seen.append(window.cget('background'))
            closed.append(weakref.ref(window))
            window.value.delete(0, 'end')
            window.value.insert(0, '这是精彩原因')
            window.cancel() if cancel else window.ok()
        self.root.after(80, complete)
        result = dialogs.askstring('备注', '记录原因', parent=self.root)
        self.assertEqual(result, '这是精彩原因')
        self.root.after(80, lambda: complete(True))
        self.assertIsNone(dialogs.askstring('备注', '记录原因', parent=self.root, initialvalue='保留'))
        self.assertEqual(seen, [BACKGROUND, BACKGROUND])
        self.assertTrue(all(reference() is None for reference in closed),
                        'closed Tk dialogs must release on this thread without a later cyclic GC')

    def test_confirmation_cancel_does_not_accept_destructive_action(self):
        from quark_cli.potplayer import dialogs
        def cancel():
            next(w for w in self.root.winfo_children() if isinstance(w, dialogs._Prompt)).cancel()
        self.root.after(80, cancel)
        self.assertFalse(dialogs.askyesno('卸载', '确认？', parent=self.root))
