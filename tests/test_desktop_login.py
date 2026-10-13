"""Account lifecycle uses temporary credentials, never the user's saved login."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from quark_cli.auth import save_cookie, load_saved_cookie
from quark_cli.desktop.service import DesktopService
from quark_cli.errors import AuthenticationError, UsageError
from tests.test_desktop_service import Backend


class AccountBackend(Backend):
    def verify_login(self, cookie):
        if cookie == "sid=bad":
            raise AuthenticationError("登录失效")
        return "新账号"


class AccountTests(unittest.TestCase):
    def test_unrelated_web_cancel_does_not_unlock_qr_login(self):
        self.service.logging_in = True
        self.call('login.web.cancel', {})
        self.assertTrue(self.service.logging_in)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(
            os.environ,
            {
                "QUARK_CONFIG_DIR": str(self.root),
                "QUARK_COOKIE": "sid=environment",
                "QUARK_COOKIE_FILE": "",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.backend = AccountBackend()
        self.service = self.make_service()
        save_cookie("sid=original")
        self.service.saved_cookie = "sid=original"

    def make_service(self):
        service = DesktopService(
            lambda value: None, backend=self.backend, directory=self.root / "downloader"
        )
        self.addCleanup(service.shutdown)
        return service

    def call(self, method, params=None):
        return self.service.dispatch("test", method, params or {}, None)

    def test_logout_clears_context_and_stays_logged_out_after_restart(self):
        self.service.tasks = [{"valid": True}]
        self.service.cut = {"id": "cut"}
        self.service.sources["old"] = "source"
        result = self.call("logout")
        self.assertFalse(result["logged_in"])
        self.assertIsNone(load_saved_cookie())
        self.assertEqual(self.service.sources, {})
        self.assertIsNone(self.service.cut)
        self.assertFalse(self.service.tasks[0]["valid"])
        for method in (
            "browse",
            "search",
            "download",
            "upload",
            "share.open",
            "tasks.retry",
        ):
            with self.assertRaises(AuthenticationError):
                self.call(method)
        self.assertFalse(self.call("account")["logged_in"])
        self.assertEqual(self.call("favorites.list"), [])
        restarted = self.make_service()
        with self.assertRaises(AuthenticationError):
            restarted.cookie()
        self.assertFalse(restarted.dispatch("init", "hello", {}, None)["signed_in"])

    def test_cancel_and_rejected_web_login_preserve_original_account(self):
        token = self.call("login.web.begin")["token"]
        self.assertTrue(self.service.logging_in)
        with self.assertRaises(AuthenticationError):
            self.call("login.web.finish", {"token": token, "cookie": "sid=bad"})
        self.assertEqual(self.service.cookie(), "sid=original")
        self.assertEqual(load_saved_cookie(), "sid=original")
        self.call("login.web.cancel", {"token": token})
        self.assertFalse(self.service.logging_in)
        with self.assertRaises(AuthenticationError):
            self.call("login.web.finish", {"token": token, "cookie": "sid=late"})
        self.assertEqual(load_saved_cookie(), "sid=original")

    def test_successful_switch_persists_saved_selection_over_environment(self):
        self.service.tasks = [{"valid": True}]
        token = self.call("login.web.begin")["token"]
        result = self.call("login.web.finish", {"token": token, "cookie": "sid=new"})
        self.assertEqual(result["nickname"], "新账号")
        self.assertNotIn("cookie", str(result))
        self.assertFalse(self.service.logging_in)
        self.assertEqual(self.service.cookie(), "sid=new")
        self.assertFalse(self.service.tasks[0]["valid"])
        self.assertEqual(self.make_service().cookie(), "sid=new")

    def test_active_transfer_or_mutation_blocks_account_operations(self):
        for attribute in ("busy", "managing", "logging_in"):
            setattr(self.service, attribute, True)
            for method in ("logout", "login.web.begin"):
                with self.assertRaises(UsageError):
                    self.call(method)
            setattr(self.service, attribute, False)
        self.assertEqual(load_saved_cookie(), "sid=original")

    def test_cancellation_during_verification_cannot_commit_late_cookie(self):
        entered, release = threading.Event(), threading.Event()

        def verify(cookie):
            entered.set()
            release.wait(2)
            return "新账号"

        self.backend.verify_login = verify
        token = self.call("login.web.begin")["token"]
        outcome = []

        def finish():
            try:
                self.call("login.web.finish", {"token": token, "cookie": "sid=late"})
            except AuthenticationError:
                outcome.append("cancelled")

        thread = threading.Thread(target=finish)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.call("login.web.cancel", {"token": token})
        release.set()
        thread.join(2)
        self.assertEqual(outcome, ["cancelled"])
        self.assertEqual(load_saved_cookie(), "sid=original")

    def test_qr_login_after_logout_reactivates_and_invalidates_old_context(self):
        self.call("logout")
        self.service.sources["old"] = "source"
        with patch("quark_cli.desktop.service.QRLogin") as qr:
            qr.return_value.run.side_effect = lambda **kwargs: save_cookie("sid=qr-new")
            result = self.service.dispatch("qr", "login", {}, threading.Event())
        self.assertTrue(result["logged_in"])
        self.assertEqual(self.service.sources, {})
        self.assertEqual(self.make_service().cookie(), "sid=qr-new")
