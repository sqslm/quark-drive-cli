"""Desktop IPC contracts use isolated configuration and no real account."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from quark_cli.desktop.service import DesktopService
from quark_cli.downloader.core import Entry
from quark_cli.downloader.preferences import Preferences, save_preferences
from quark_cli.downloader.worker import Search
from quark_cli.transfer import DownloadStopped


class Backend:
    def __init__(self):
        self.requests = []
    def account_nickname(self):
        return "测试账号"
    def listing(self, fid):
        return [Entry("f", "知否.txt", False, 3)]
    def search_stream(self, request: Search, report, stopped):
        if not stopped():
            report((Entry("f", "知否.txt", False, 3),), 0)
    def share_listing(self, source, path):
        return [Entry("s", "分享.txt", False, 2, "/分享.txt")]
    def download(self, request, progress, stopped):
        self.requests.append(request)
        if stopped():
            raise DownloadStopped("已停止")
        return {"size": 3}


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.messages = []
        self.backend = Backend()
        self.service = DesktopService(self.messages.append, backend=self.backend, directory=Path(self.tmp.name) / "state")
        self.addCleanup(self.service.shutdown)
    def wait_result(self, identifier):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            for message in self.messages:
                if message.get("id") == identifier and ("result" in message or "error" in message):
                    return message
            threading.Event().wait(.01)
        self.fail("IPC response missing")
    def test_unknown_method_and_malformed_id_do_not_execute(self):
        self.service.accept({"id": "bad", "method": "os.system", "params": {"command": "echo unsafe"}})
        self.assertIn("error", self.wait_result("bad"))
        self.service.accept({"id": None, "method": "browse"})
        self.assertIn("error", self.messages[-1])
        self.assertEqual(self.backend.requests, [])
    def test_handshake_preferences_and_browse_contain_no_credential(self):
        self.service.accept({"id": "init", "method": "hello"})
        data = self.wait_result("init")["result"]
        self.assertEqual(data["protocol"], 1)
        self.assertEqual(data["preferences"]["connections"], 4)
        self.service.accept({"id": "browse", "method": "browse"})
        self.assertEqual(self.wait_result("browse")["result"][0]["fid"], "f")
        self.assertNotIn("cookie", str(self.messages).lower())
    def test_search_events_share_exact_request_id(self):
        self.service.accept({"id": "search-1", "method": "search", "params": {"query": "知否"}})
        self.assertIn("result", self.wait_result("search-1"))
        pages = [message for message in self.messages if message.get("event") == "search.page"]
        self.assertEqual(pages[0]["id"], "search-1")
        self.assertEqual(pages[0]["data"]["entries"][0]["name"], "知否.txt")
    def test_share_code_is_parsed_and_handle_hides_source(self):
        parsed = self.service.dispatch("p", "share.parse", {"text": "https://pan.quark.cn/s/abcdef 提取码：1234"}, None)
        self.assertEqual(parsed["passcode"], "1234")
        source = self.service.dispatch("p", "share.open", {"text": parsed["url"], "passcode": "5678"}, None)
        self.assertNotIn("5678", str(source))
        self.assertEqual(self.service.sources[source["handle"]].passcode, "5678")
    def test_download_uses_original_settings_and_retry_uses_current_rate(self):
        save_preferences(self.service.preferences_path, Preferences(self.tmp.name, 4, limit_rate=1048576))
        rows = self.service.dispatch("d", "download", {"entries": [{"fid":"f","name":"知否.txt","directory":False}]}, None)
        self.service.transfers.shutdown(wait=True)
        request = self.backend.requests[0]
        self.assertEqual(request.connections, 4)
        self.assertEqual(request.limit_rate, 1048576)
        self.assertFalse(request.overwrite)
        self.assertEqual(rows[0]["target"], str(Path(self.tmp.name) / "知否.txt"))
    def test_invalid_filename_does_not_start_remote_download(self):
        save_preferences(self.service.preferences_path, Preferences(self.tmp.name))
        with self.assertRaises(Exception):
            self.service.dispatch("d", "download", {"entries": [{"fid":"f","name":"../bad","directory":False}]}, None)
        self.assertEqual(self.backend.requests, [])
    def test_failed_login_does_not_discard_share_source(self):
        source = self.service.dispatch("p", "share.open", {"text":"https://pan.quark.cn/s/abcdef"}, None)
        with patch("quark_cli.desktop.service.QRLogin") as login:
            login.return_value.run.side_effect = RuntimeError("private cookie")
            self.service.execute("login", "login", {}, threading.Event())
        self.assertIn(source["handle"], self.service.sources)
        self.assertNotIn("private", str(self.messages))

    def test_corrupt_preferences_allow_session_use_without_overwriting(self):
        self.service.directory.mkdir(parents=True)
        self.service.preferences_path.write_text('{broken', encoding='utf-8')
        hello = self.service.dispatch("hello", "hello", {}, None)
        self.assertTrue(hello["warning"])
        result = self.service.dispatch("prefs", "preferences.save", {
            "output_dir": self.tmp.name, "connections": 8, "width": 1000, "height": 700, "limit_rate": None}, None)
        self.assertFalse(result["persisted"])
        self.assertIn("窗口内", result["warning"])
        self.assertEqual(self.service.preferences_path.read_text('utf-8'), '{broken')
        self.assertEqual(self.service.current_preferences().connections, 8)

    def test_share_writes_and_writes_during_login_are_rejected(self):
        with self.assertRaises(Exception):
            self.service.dispatch("write", "file.delete", {"handle":"share","entries":[]}, None)
        self.service.logging_in = True
        with self.assertRaises(Exception):
            self.service.dispatch("write", "file.mkdir", {"parent":"0","name":"X"}, None)
