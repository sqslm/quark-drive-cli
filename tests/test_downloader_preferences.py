"""Private local preferences and restoration through real Tk windows."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quark_cli.downloader.app import DownloaderWindow, _tkinter
from quark_cli.downloader.preferences import Preferences, load_preferences, save_preferences
from quark_cli.errors import LocalIOError
from quark_cli.state import private_path
from tests.test_downloader_window import WindowCase, _Backend, _FakeVar, _TK, _no_cookie, _tk_root


class PreferencesFileTests(unittest.TestCase):
    def test_absent_preferences_use_defaults_without_creating_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "downloader" / "preferences.json"
            self.assertEqual(load_preferences(path), Preferences())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_roundtrip_has_only_local_fields_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "downloader" / "preferences.json"
            preferences = Preferences(folder, 16, 980, 700)
            save_preferences(path, preferences)
            self.assertEqual(load_preferences(path), preferences)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(data), {"version", "output_dir", "connections", "width", "height"})
            private_path(path, read_only=True)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["preferences.json"])

    def test_invalid_schema_and_values_are_reported_without_raw_content(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preferences.json"
            save_preferences(path, Preferences())
            base = json.loads(path.read_text(encoding="utf-8"))
            for changes in ({"version": True}, {"version": 2}, {"connections": True}, {"connections": 17},
                            {"connections": "8"}, {"width": 0}, {"height": True},
                            {"output_dir": "relative"}, {"output_dir": "bad\0path"},
                            {"cookie": "private-cookie"}):
                path.write_text(json.dumps({**base, **changes}), encoding="utf-8")
                with self.subTest(changes=changes), self.assertRaises(LocalIOError) as caught:
                    load_preferences(path)
                self.assertNotIn("private-cookie", str(caught.exception))


class PreferencesWindowTests(WindowCase):
    def test_missing_output_directory_keeps_valid_thread_and_size_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preferences.json"
            save_preferences(path, Preferences(str(Path(folder) / "gone"), 8, 960, 700))
            window = self._window(preferences_path=path)
            window._load_preferences()
            self.assertEqual(window._output_dir, "")
            self.assertEqual(window._preferences.connections, 8)
            self.assertIn("保存目录已不可用", window._preferences_warning)

    def test_corrupt_preferences_do_not_block_startup_or_get_silently_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preferences.json"
            save_preferences(path, Preferences())
            path.write_text("broken private-secret", encoding="utf-8")
            window = self._window(cookie=_no_cookie, preferences_path=path)
            window._load_preferences()
            window._initialise()
            self.assertFalse(window._preferences_save_allowed)
            self.assertEqual(window._preferences, Preferences())
            self.assertIn("读取下载器设置失败", window._log.content)
            self.assertNotIn("private-secret", window._status_var.get())
            self.assertEqual(window._login_button.state, "normal")
            self.assertTrue(window._save_preferences())
            self.assertEqual(path.read_text(encoding="utf-8"), "broken private-secret")

    def test_write_failure_is_visible_and_keeps_the_previous_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preferences.json"
            save_preferences(path, Preferences(folder, 4))
            window = self._window(preferences_path=path)
            window._load_preferences()
            window._connections_var = _FakeVar("8")
            with patch("quark_cli.downloader.preferences.write_json", side_effect=LocalIOError("无法写入设置")):
                self.assertFalse(window._save_preferences())
            self.assertIn("保存下载器设置失败", window._status_var.get())
            self.assertEqual(load_preferences(path).connections, 4)
            self.assertEqual(window._preferences.connections, 4)

    def test_non_private_link_is_rejected_and_not_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "target.json"
            save_preferences(target, Preferences())
            link = Path(folder) / "link.json"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("Symlink creation is unavailable")
            window = self._window(preferences_path=link)
            window._load_preferences()
            self.assertFalse(window._preferences_save_allowed)
            self.assertIn("重解析点", window._preferences_warning)
            self.assertTrue(link.is_symlink())


@unittest.skipUnless(_TK, "Tk is unavailable")
class PreferencesControlsTests(unittest.TestCase):
    @staticmethod
    def _window(path, root):
        return DownloaderWindow(cookie=_no_cookie, backend_factory=lambda loader: _Backend(),
                                preferences_path=path, root_factory=lambda: root)

    def test_two_window_runs_restore_folder_threads_size_and_store_no_share_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config" / "preferences.json"
            root = _tk_root()
            window = self._window(path, root)
            observed = []

            def configure():
                try:
                    root.geometry("940x650")
                    root.update_idletasks()
                    with patch("tkinter.filedialog.askdirectory", return_value=folder):
                        window.choose_output()
                    window._connections_var.set("12")
                    root.tk.call(window._connections_input["command"])
                    window._share_var.set("https://pan.quark.cn/s/private?pwd=secret-code")
                    window._passcode_var.set("another-secret")
                    window._connections_var.set("-")
                    observed.append((root.winfo_width(), root.winfo_height()))
                finally:
                    window.close()

            root.after(120, configure)
            window.run()
            self.assertEqual(len(observed), 1)
            restored = load_preferences(path)
            self.assertEqual((restored.output_dir, restored.connections), (folder, 12))
            self.assertEqual((restored.width, restored.height), observed[0])
            self.assertNotIn("secret", path.read_text(encoding="utf-8"))
            root = _tk_root()
            window = self._window(path, root)

            def inspect():
                try:
                    observed.append((window._output_var.get(), window._connections_var.get(),
                                     root.winfo_width(), root.winfo_height()))
                finally:
                    window.close()

            root.after(120, inspect)
            window.run()
            self.assertEqual(observed[1], (folder, "12", *observed[0]))

    def test_large_saved_dimensions_are_limited_to_the_current_screen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preferences.json"
            save_preferences(path, Preferences(width=100000, height=100000))
            root = _tk_root()
            window = self._window(path, root)
            try:
                window._root = root
                window._load_preferences()
                window._build(_tkinter())
                root.update_idletasks()
                self.assertLessEqual(root.winfo_width(), root.winfo_screenwidth())
                self.assertLessEqual(root.winfo_height(), root.winfo_screenheight())
            finally:
                window._shutdown()

    def test_close_save_failure_reports_warning_and_still_closes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preferences.json"
            root = _tk_root()
            window = self._window(path, root)
            window._root = root
            window._load_preferences()
            window._build(_tkinter())
            with patch("quark_cli.downloader.app.save_preferences", side_effect=LocalIOError("写入失败")), \
                    patch("tkinter.messagebox.showwarning") as warning:
                window.close()
            warning.assert_called_once()
            self.assertIn("本次设置未保存", warning.call_args.args[1])
            self.assertIsNone(window._root)
