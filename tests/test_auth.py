import contextlib
import hmac
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli import auth
from quark_cli.auth import QRLogin, get_cookie, logout, render_terminal_qr, save_cookie, write_qr_png
from quark_cli.errors import AuthenticationError, LocalIOError, RemoteError


def api_ok(data):
    return {"status": 200, "code": 0, "data": data}


class AuthTests(unittest.TestCase):
    def test_terminal_qr_matrix_decodes_to_web_login_with_full_quiet_zone(self):
        import zxingcpp
        from PIL import Image

        expected = auth.build_qr_url("terminal-decode-token")
        rendered = render_terminal_qr(expected)
        symbols = {" ": (False, False), "▀": (True, False), "▄": (False, True), "█": (True, True)}
        matrix = []
        for line in rendered.splitlines():
            self.assertTrue(line.startswith("\x1b[30;47m") and line.endswith("\x1b[0m"))
            pairs = [symbols[character] for character in line[8:-4]]
            matrix.extend([[pair[0] for pair in pairs], [pair[1] for pair in pairs]])
        width = len(matrix[0])
        self.assertTrue(all(len(row) == width for row in matrix))
        self.assertTrue(all(not pixel for row in matrix[:4] + matrix[-4:] for pixel in row))
        self.assertTrue(all(not pixel for row in matrix for pixel in row[:4] + row[-4:]))
        image = Image.new("L", (width, len(matrix)), 255)
        image.putdata([0 if pixel else 255 for row in matrix for pixel in row])
        result = zxingcpp.read_barcode(image.resize((width * 8, len(matrix) * 8), Image.Resampling.NEAREST))
        self.assertIsNotNone(result, "terminal QR raster did not decode")
        self.assertTrue(hmac.compare_digest(result.text, expected), "terminal QR decoded to a different URL")
        self.assertIn("ssb=weblogin", result.text)

    def test_simulated_terminal_login_and_callback_failure_clean_temporary_png(self):
        for fails in (False, True):
            handler, requests = self._make_handler()
            captured = []
            with self.subTest(fails=fails), tempfile.TemporaryDirectory() as directory:
                path, cookie = Path(directory) / "qr.png", Path(directory) / "config" / "cookie"
                def terminal_callback(rendered):
                    captured.append(rendered)
                    if fails:
                        raise LocalIOError("terminal unavailable")
                login = QRLogin(transport=httpx.MockTransport(handler), timeout=2, poll_interval=0, cookie_path=cookie)
                if fails:
                    with self.assertRaises(LocalIOError):
                        login.run(qr_file=path, on_qr_terminal=terminal_callback)
                else:
                    login.run(qr_file=path, on_qr_terminal=terminal_callback)
                self.assertFalse(path.exists())
                self.assertEqual(cookie.exists(), not fails)
                self.assertEqual(len(captured), 1)
                self.assertNotIn("qr-token-private", captured[0])
                self.assertTrue(all(request.method == "GET" for request in requests))

    def _make_handler(self, *, poll_statuses=None, include_puus=True, verify_status=200):
        statuses = list(poll_statuses or [2000000])
        requests = []

        def handler(request):
            requests.append(request)
            host = request.url.host
            path = request.url.path
            if host == "uop.quark.cn" and path.endswith("getTokenForQrcodeLogin"):
                return httpx.Response(
                    200,
                    json={"status": 2000000, "data": {"members": {"token": "qr-token-private"}}},
                    request=request,
                )
            if host == "uop.quark.cn" and path.endswith("getServiceTicketByQrcodeToken"):
                status = statuses.pop(0) if statuses else 2000000
                data = {"members": {"service_ticket": "service-ticket-private"}} if status == 2000000 else {}
                return httpx.Response(200, json={"status": status, "data": data}, request=request)
            if host == "pan.quark.cn" and path == "/account/info":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"nickname": "not persisted"}},
                    headers={"set-cookie": "sid=pan-cookie-private; Domain=pan.quark.cn; Path=/; Secure; HttpOnly"},
                    request=request,
                )
            if host == "pan.quark.cn" and path == "/list":
                return httpx.Response(200, text="<html></html>", request=request)
            if host == "drive-pc.quark.cn" and path.endswith("/file/sort"):
                if "sid=pan-cookie-private" not in request.headers.get("cookie", ""):
                    return httpx.Response(403, request=request)
                if request.url.params.get("_size") == "1":
                    if verify_status != 200:
                        return httpx.Response(verify_status, request=request)
                    headers = {
                        "set-cookie": "capacity_cookie=refreshed-private; Domain=.quark.cn; Path=/; Secure; HttpOnly"
                    }
                    return httpx.Response(200, json=api_ok({"list": []}), headers=headers, request=request)
                headers = {}
                if include_puus:
                    headers["set-cookie"] = "__puus=drive-cookie-private; Path=/; Secure; HttpOnly"
                return httpx.Response(200, json=api_ok({"list": []}), headers=headers, request=request)
            if host == "drive.quark.cn" and path.endswith("/member"):
                headers = {}
                if include_puus:
                    headers["set-cookie"] = "__puus=member-cookie-private; Domain=.quark.cn; Path=/; Secure; HttpOnly"
                return httpx.Response(200, json=api_ok({}), headers=headers, request=request)
            return httpx.Response(404, request=request)

        return handler, requests

    def test_login_merges_cookie_from_drive_read_only_response_and_validates_same_jar(self):
        handler, requests = self._make_handler(poll_statuses=[50004001, 2000000])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            qr_path = root / "login.png"
            cookie_path = root / "config" / "cookie"
            observed_qr_paths = []

            def on_qr_ready(path):
                self.assertTrue(path.is_file())
                observed_qr_paths.append(path)

            with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
                QRLogin(
                    transport=httpx.MockTransport(handler),
                    timeout=10,
                    poll_interval=0,
                    sleep=lambda _: None,
                    cookie_path=cookie_path,
                ).run(qr_file=qr_path, on_qr_ready=on_qr_ready)

            expected_qr = auth.build_qr_url("qr-token-private")
            self.assertEqual(len(observed_qr_paths), 1)
            self.assertTrue(observed_qr_paths[0].name.endswith(".png"))
            self.assertTrue(expected_qr.startswith(auth.QR_BASE_URL), "QR URL was not built from the Quark QR endpoint")
            self.assertFalse(qr_path.exists(), "temporary QR image was not removed")
            stored = cookie_path.read_text(encoding="utf-8").strip()
            # CookieJar groups domains in an order that differs across httpx
            # versions. Header order does not determine merge completeness.
            components = [component.strip().partition("=") for component in stored.split(";")]
            self.assertTrue(all(separator and name and value for name, separator, value in components), "Cookie jar contains invalid fields")
            stored_cookie = {name: value for name, _, value in components}
            expected_cookie = {
                "sid": "pan-cookie-private",
                "__puus": "drive-cookie-private",
                "capacity_cookie": "refreshed-private",
            }
            self.assertEqual(len(components), len(expected_cookie), "Cookie jar has missing or duplicate fields")
            self.assertEqual(set(stored_cookie), set(expected_cookie), "Cookie jar names were incomplete")
            self.assertTrue(
                all(hmac.compare_digest(stored_cookie[name], value) for name, value in expected_cookie.items()),
                "Cookie jar merge was incomplete",
            )
            capacity = [
                request
                for request in requests
                if request.url.path.endswith("/file/sort") and request.url.params.get("_size") == "1"
            ]
            self.assertEqual(len(capacity), 1)
            capacity_cookie = capacity[0].headers.get("cookie", "")
            self.assertTrue("sid=pan-cookie-private" in capacity_cookie and "__puus=drive-cookie-private" in capacity_cookie)
            drive_sort = [
                request
                for request in requests
                if request.url.path.endswith("/file/sort") and request.url.params.get("_size") == "50"
            ]
            self.assertEqual(len(drive_sort), 1)
            sort_cookie = drive_sort[0].headers.get("cookie", "")
            self.assertTrue("sid=pan-cookie-private" in sort_cookie, "host-only Cookie was not forwarded to the trusted read-only host")
            self.assertEqual(stdout.getvalue(), "")
            output = stdout.getvalue() + stderr.getvalue()
            secrets = ("qr-token-private", "service-ticket-private", "pan-cookie-private", "drive-cookie-private")
            leaked = any(secret in output for secret in secrets)
            self.assertFalse(leaked, "sensitive data reached CLI output")
            query_violations = []
            for request in requests:
                params = request.url.params
                if "token" in params and request.url.host != "uop.quark.cn":
                    query_violations.append("qr token sent to another host")
                if "st" in params and request.url.host != "pan.quark.cn":
                    query_violations.append("service ticket sent to another host")
            self.assertFalse(query_violations, "sensitive query parameter crossed hosts")
            if os.name != "nt":
                self.assertEqual(cookie_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(cookie_path.parent.stat().st_mode & 0o777, 0o700)

    def test_qr_png_decodes_to_login_url(self):
        decoder = None
        try:
            import zxingcpp
            from PIL import Image

            def decode(path):
                with Image.open(path) as image:
                    result = zxingcpp.read_barcodes(image)
                return result[0].text if result else ""

            decoder = decode
        except ImportError:
            try:
                import cv2

                def decode(path):
                    image = cv2.imread(str(path))
                    value, _, _ = cv2.QRCodeDetector().detectAndDecode(image)
                    return value

                decoder = decode
            except ImportError:
                self.skipTest("install the test extra or OpenCV to run QR decode verification")

        token = "decode-only-token"
        expected = auth.build_qr_url(token)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "decode.png"
            created = write_qr_png(expected, path)
            decoded = decoder(created)
            self.assertTrue(hmac.compare_digest(decoded, expected), "QR image did not decode to the expected URL")

    def test_expired_qr_fails_and_removes_image(self):
        handler, _ = self._make_handler(poll_statuses=[50004002])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            qr_path = root / "expired.png"
            cookie_path = root / "config" / "cookie"
            with self.assertRaises(AuthenticationError):
                QRLogin(
                    transport=httpx.MockTransport(handler),
                    timeout=2,
                    cookie_path=cookie_path,
                ).run(qr_file=qr_path)
            self.assertFalse(qr_path.exists())
            self.assertFalse(cookie_path.exists())

    def test_pending_login_times_out_without_storing_cookie(self):
        handler, requests = self._make_handler(poll_statuses=[50004001] * 10)
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            qr_path = root / "timeout.png"
            cookie_path = root / "config" / "cookie"
            with self.assertRaisesRegex(AuthenticationError, "超时"):
                QRLogin(
                    transport=httpx.MockTransport(handler),
                    timeout=3,
                    poll_interval=2,
                    sleep=sleep,
                    monotonic=lambda: now[0],
                    cookie_path=cookie_path,
                ).run(qr_file=qr_path)
            polls = [request for request in requests if request.url.path.endswith("getServiceTicketByQrcodeToken")]
            self.assertEqual(len(polls), 3)
            self.assertFalse(qr_path.exists())
            self.assertFalse(cookie_path.exists())

    def test_missing_qr_token_is_rejected_before_image_creation(self):
        def handler(request):
            return httpx.Response(200, json={"status": 2000000, "data": {"members": {}}}, request=request)

        with tempfile.TemporaryDirectory() as temp_dir:
            qr_path = Path(temp_dir) / "bad.png"
            with self.assertRaises(RemoteError):
                QRLogin(transport=httpx.MockTransport(handler), timeout=1).run(qr_file=qr_path)
            self.assertFalse(qr_path.exists())

    def test_qr_success_without_ticket_is_rejected_and_image_removed(self):
        def handler(request):
            if request.url.path.endswith("getTokenForQrcodeLogin"):
                return httpx.Response(200, json={"status": 2000000, "data": {"members": {"token": "private"}}}, request=request)
            return httpx.Response(200, json={"status": 2000000, "data": {"members": {}}}, request=request)

        with tempfile.TemporaryDirectory() as temp_dir:
            qr_path = Path(temp_dir) / "bad-ticket.png"
            with self.assertRaises(RemoteError):
                QRLogin(transport=httpx.MockTransport(handler), timeout=2).run(qr_file=qr_path)
            self.assertFalse(qr_path.exists())

    def test_cookie_auth_validation_failure_does_not_store(self):
        handler, _ = self._make_handler(verify_status=401)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            qr_path = root / "invalid.png"
            cookie_path = root / "config" / "cookie"
            with self.assertRaises(AuthenticationError):
                QRLogin(
                    transport=httpx.MockTransport(handler),
                    timeout=2,
                    cookie_path=cookie_path,
                ).run(qr_file=qr_path)
            self.assertFalse(qr_path.exists())
            self.assertFalse(cookie_path.exists())

    def test_missing_puus_after_all_read_only_warmups_is_not_saved(self):
        handler, requests = self._make_handler(include_puus=False)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cookie_path = root / "config" / "cookie"
            with self.assertRaisesRegex(AuthenticationError, "完整登录 Cookie"):
                QRLogin(
                    transport=httpx.MockTransport(handler),
                    timeout=2,
                    cookie_path=cookie_path,
                ).run(qr_file=root / "missing-puus.png")
            warmup_hosts = {request.url.host for request in requests if request.url.path.endswith(("/list", "/file/sort", "/member"))}
            self.assertEqual(warmup_hosts, {"pan.quark.cn", "drive-pc.quark.cn", "drive.quark.cn"})
            self.assertFalse(cookie_path.exists())

    def test_cookie_precedence_and_logout_only_remove_saved_cookie(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cookie_path = root / "config" / "cookie"
            env_file = root / "explicit-cookie.txt"
            save_cookie("sid=saved-private", cookie_path)
            env_file.write_text("sid=file-private\n", encoding="utf-8")

            self.assertTrue(hmac.compare_digest(get_cookie({}, path=cookie_path), "sid=saved-private"))
            self.assertTrue(hmac.compare_digest(get_cookie({"QUARK_COOKIE_FILE": str(env_file)}, path=cookie_path), "sid=file-private"))
            self.assertTrue(
                hmac.compare_digest(
                    get_cookie({"QUARK_COOKIE": "sid=environment-private", "QUARK_COOKIE_FILE": str(env_file)}, path=cookie_path),
                    "sid=environment-private",
                )
            )
            self.assertTrue(logout(cookie_path))
            self.assertFalse(cookie_path.exists())
            self.assertTrue(env_file.exists())
            self.assertTrue(hmac.compare_digest(get_cookie({"QUARK_COOKIE": "sid=environment-private"}, path=cookie_path), "sid=environment-private"))

    def test_cookie_file_without_newline_is_valid_and_environment_file_errors_do_not_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cookie_path = root / "config" / "cookie"
            env_file = root / "cookie.txt"
            save_cookie("sid=saved-private", cookie_path)
            env_file.write_text("", encoding="utf-8")
            with self.assertRaises(AuthenticationError):
                get_cookie({"QUARK_COOKIE_FILE": str(env_file)}, path=cookie_path)

    def test_qr_output_refuses_existing_file_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "existing.png"
            path.write_bytes(b"keep existing file")
            with self.assertRaises(LocalIOError):
                write_qr_png("https://example.test/sensitive", path)
            self.assertEqual(path.read_bytes(), b"keep existing file")

    def test_missing_qrcode_dependency_does_not_leave_an_open_partial_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "no-dependency.png"
            with patch.dict(sys.modules, {"qrcode": None}):
                with self.assertRaises(LocalIOError):
                    write_qr_png("https://example.test/sensitive", path)
            self.assertFalse(path.exists())

    def test_windows_acl_failure_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_path = Path(temp_dir) / "config" / "cookie"
            with patch.object(auth.sys, "platform", "win32"), patch("quark_cli.state.restrict_windows_acl", side_effect=LocalIOError("ACL unavailable")) as acl:
                with self.assertRaises(LocalIOError):
                    save_cookie("sid=must-not-be-written", cookie_path)
            acl.assert_called_once()
            self.assertFalse(cookie_path.exists())


if __name__ == "__main__":
    unittest.main()
