"""Cancellation, retry, and window behaviour for the PotPlayer QR login."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from quark_cli.auth import LoginCancelled, QRExpired, QRLogin, build_qr_url, write_qr_png
from quark_cli.errors import AuthenticationError, RemoteError
from quark_cli.potplayer import __main__ as entry
from quark_cli.potplayer import plugin_state
from quark_cli.potplayer.login_gui import MAX_QR_PIXELS, LoginRunner, QrLoginWindow
from quark_cli.potplayer.service import Service

from potplayer_fixture import DriveFixture


def _tk_available() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


_TK = _tk_available()


def _qr_transport(state: dict, poll_status: int = 50004001) -> httpx.MockTransport:
    """A minimal QR endpoint: a fixed token, then the supplied poll status."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getTokenForQrcodeLogin"):
            return httpx.Response(
                200,
                json={"status": 2000000, "data": {"members": {"token": "qr-token"}}},
                request=request,
            )
        state["polls"] += 1
        return httpx.Response(200, json={"status": poll_status}, request=request)

    return httpx.MockTransport(handler)


# -- Tk lifecycle (own process) ------------------------------------------

# Run in a child process: a Tk object finalised on a thread other than the one
# that created it aborts the whole interpreter (``Tcl_AsyncDelete``), so a
# regression here must not take the test runner down with it. The script opens
# and closes a real QR window both directly and on the bridge's login thread,
# then forces a GC pass on an unrelated thread and only then exits 0.
_TK_CHILD = r'''
import gc
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["QUARK_CONFIG_DIR"] = tempfile.mkdtemp(prefix="quark-tk-lifecycle-")

import httpx

from quark_cli.auth import LoginCancelled, build_qr_url, write_qr_png
from quark_cli.potplayer.login_gui import QrLoginWindow
from quark_cli.potplayer.service import Service

PNG = Path(os.environ["QUARK_CONFIG_DIR"]) / "qr.png"
write_qr_png(build_qr_url("x" * 64), PNG)


def collect_elsewhere():
    """Force a GC pass on a thread that owns no Tk object."""
    done = []
    worker = threading.Thread(target=lambda: (gc.collect(), done.append(True)), daemon=True)
    worker.start()
    worker.join(10)
    assert done, "collector thread did not finish"


class BlockingLogin:
    """Report one real QR image, then wait for the window to be closed."""

    def __init__(self, cancel):
        self._cancel = cancel

    def run(self, on_qr_ready=None):
        if on_qr_ready is not None:
            on_qr_ready(PNG)
        self._cancel.wait(15)
        raise LoginCancelled("登录已取消")


def noting_root(delay_ms, seen, holder):
    """Record whether the Tk thread actually built a PhotoImage."""
    import tkinter

    def make_root():
        root = tkinter.Tk()
        root.after(delay_ms, lambda: seen.append(holder["window"]._photo is not None))
        return root

    return make_root


def auto_closing_root(delay_ms, seen, holder):
    """Like ``noting_root``, but the window closes itself (direct-window case)."""
    make_root = noting_root(delay_ms, seen, holder)

    def build():
        root = make_root()
        root.after(delay_ms + 200, lambda: holder["window"].close())
        return root

    return build


def direct_window():
    holder = {}
    seen = []
    window = QrLoginWindow(
        login_factory=BlockingLogin,
        root_factory=auto_closing_root(250, seen, holder),
    )
    holder["window"] = window
    try:
        window.run()
    except LoginCancelled:
        pass
    assert seen == [True], seen
    del holder, seen, window
    collect_elsewhere()


def mock_transport(polls):
    def handler(request):
        if request.url.path.endswith("getTokenForQrcodeLogin"):
            return httpx.Response(
                200, json={"status": 2000000, "data": {"members": {"token": "qr-token"}}}, request=request
            )
        polls.append(1)
        return httpx.Response(200, json={"status": 50004001}, request=request)

    return httpx.MockTransport(handler)


def service_window():
    holder = {}
    seen = []
    polls = []

    def factory():
        window = QrLoginWindow(
            transport=mock_transport(polls),
            cookie_path=Path(os.environ["QUARK_CONFIG_DIR"]) / "cookie",
            root_factory=noting_root(250, seen, holder),
        )
        holder["window"] = window
        return window

    service = Service(transport=mock_transport(polls), cookie=None, login_factory=factory)
    server = service.build_server()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{service.port}"
    headers = {"Authorization": f"Bearer {service.token}"}
    state = {}
    try:
        with httpx.Client(timeout=10.0) as client:
            started = client.post(base + "/v1/auth/login", headers=headers)
            assert started.status_code == 202, started.text
            for _ in range(200):
                state = client.get(base + "/v1/auth", headers=headers).json()
                if state.get("qr_png"):
                    break
                time.sleep(0.05)
            assert state.get("qr_png"), state
            time.sleep(0.4)  # let the scan thread finish drawing the code
            service.close()  # the bridge asks the scan thread to close its own window
            for _ in range(300):
                state = client.get(base + "/v1/auth", headers=headers).json()
                if state.get("login_state") == "idle":
                    break
                time.sleep(0.05)
    finally:
        service.stop()
        thread.join(10)
        server.server_close()
        service.close()
    assert state.get("login_state") == "idle", state
    assert state.get("authenticated") is False, state
    assert seen == [True], seen
    assert polls, "the login never polled"
    del holder, seen, polls, service, factory
    collect_elsewhere()


if __name__ == "__main__":
    direct_window()
    service_window()
    gc.collect()
    print("tk-lifecycle-ok", flush=True)
'''


@unittest.skipUnless(_TK, "Tkinter with a display is required")
class TkLifecycleCase(unittest.TestCase):
    def test_closed_windows_leave_no_tcl_for_other_threads(self):
        env = dict(os.environ)
        roots = [str(Path(__file__).resolve().parents[1] / "src")]
        if env.get("PYTHONPATH"):
            roots.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(roots)
        result = subprocess.run(
            [sys.executable, "-c", _TK_CHILD],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=180,
        )
        output = result.stdout + result.stderr
        self.assertIn("tk-lifecycle-ok", result.stdout, output)
        self.assertNotIn("Tcl_AsyncDelete", output)
        self.assertNotIn("main thread is not in main loop", output)
        self.assertEqual(result.returncode, 0, output)


# -- QRLogin cancellation ------------------------------------------------


class QrLoginCancelCase(unittest.TestCase):
    def test_terminal_states_stay_authentication_errors(self):
        self.assertTrue(issubclass(QRExpired, AuthenticationError))
        self.assertTrue(issubclass(LoginCancelled, AuthenticationError))

    def test_cancel_before_polling_saves_nothing(self):

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {"polls": 0}
            login = QRLogin(
                transport=_qr_transport(state),
                timeout=30,
                cookie_path=root / "config" / "cookie",
                cancelled=lambda: True,
            )
            with self.assertRaises(LoginCancelled):
                login.run(qr_file=root / "cancel.png")
            self.assertEqual(state["polls"], 0)
            self.assertFalse((root / "config" / "cookie").exists())
            self.assertFalse((root / "cancel.png").exists())

    def test_cancel_between_polls_stops_the_wait(self):

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {"polls": 0}
            login = QRLogin(
                transport=_qr_transport(state),
                timeout=30,
                sleep=lambda seconds: None,
                cookie_path=root / "config" / "cookie",
                cancelled=lambda: state["polls"] > 0,
            )
            with self.assertRaises(LoginCancelled):
                login.run(qr_file=root / "cancel-mid.png")
            self.assertEqual(state["polls"], 1)
            self.assertFalse((root / "config" / "cookie").exists())

    def test_expired_code_is_retryable_not_a_login_failure(self):

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {"polls": 0}
            login = QRLogin(
                transport=_qr_transport(state, poll_status=50004002),
                timeout=30,
                cookie_path=root / "config" / "cookie",
            )
            with self.assertRaises(QRExpired):
                login.run(qr_file=root / "expired.png")
            self.assertFalse((root / "config" / "cookie").exists())


# -- LoginRunner (no Tk) -------------------------------------------------


class _SuccessLogin:
    def __init__(self, png: str) -> None:
        self._png = png

    def run(self, on_qr_ready=None) -> None:
        if on_qr_ready is not None:
            on_qr_ready(self._png)


class _FlakyLogin:
    def __init__(self, attempt: int) -> None:
        self._attempt = attempt

    def run(self, on_qr_ready=None) -> None:
        if self._attempt == 1:
            raise QRExpired("二维码已过期")


class _FailedLogin:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    def run(self, on_qr_ready=None) -> None:
        raise self._error


class _BlockingLogin:
    """Blocks until cancelled, then reports the cancellation."""

    def __init__(self, cancel: threading.Event, png: str = "missing.png") -> None:
        self._cancel = cancel
        self._png = png
        self.observed = False

    def run(self, on_qr_ready=None) -> None:
        if on_qr_ready is not None:
            on_qr_ready(self._png)
        if not self._cancel.wait(5):
            raise AssertionError("the window never cancelled the poll")
        self.observed = True
        raise LoginCancelled("登录已取消")


class RunnerCase(unittest.TestCase):
    def _collect(self, factory, expected: int, timeout: float = 5.0) -> list:
        events: list = []
        runner = LoginRunner(factory, lambda kind, value: events.append((kind, value)))
        runner.start()
        deadline = time.monotonic() + timeout
        while len(events) < expected and time.monotonic() < deadline:
            time.sleep(0.01)
        return events

    def test_success_reports_the_qr_then_success(self):
        events = self._collect(lambda cancel: _SuccessLogin("qr.png"), 2)
        self.assertEqual(events, [("qr", "qr.png"), ("success", None)])

    def test_expired_is_reported_so_the_code_can_be_refreshed(self):
        events = self._collect(lambda cancel: _FlakyLogin(1), 1)
        self.assertEqual(events[0][0], "expired")

    def test_failure_carries_the_original_exception(self):
        error = RemoteError("上游连接失败")
        events = self._collect(lambda cancel: _FailedLogin(error), 1)
        self.assertEqual(events[0], ("failed", error))

    def test_cancel_is_reported(self):
        events: list = []
        runner = LoginRunner(_BlockingLogin, lambda kind, value: events.append((kind, value)))
        runner.start()
        runner.cancel()
        deadline = time.monotonic() + 5
        while not any(kind == "cancelled" for kind, _ in events) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("cancelled", [kind for kind, _ in events])


# -- QrLoginWindow (real Tk) ---------------------------------------------


@unittest.skipUnless(_TK, "Tkinter with a display is required")
class WindowCase(unittest.TestCase):
    def _root(self, window: QrLoginWindow, delay_ms: int):
        import tkinter

        root = tkinter.Tk()
        root.after(delay_ms, window.close)
        return root

    def test_closing_the_window_cancels_the_poll(self):
        observed: list = []

        def factory(cancel):
            login = _BlockingLogin(cancel)
            observed.append(login)
            return login

        window = QrLoginWindow(login_factory=factory, root_factory=lambda: self._root(window, 250))
        with self.assertRaises(LoginCancelled):
            window.run()
        self.assertTrue(observed[0].observed)

    def test_qr_area_reserves_pixels_before_an_image_is_set(self):
        import tkinter

        captured: dict = {}

        def make_root():
            root = tkinter.Tk()

            def inspect() -> None:
                for child in root.winfo_children():
                    if isinstance(child, tkinter.Frame):
                        captured["size"] = (child.winfo_reqwidth(), child.winfo_reqheight())

            root.after(200, inspect)
            root.after(400, window.close)
            return root

        window = QrLoginWindow(login_factory=lambda cancel: _BlockingLogin(cancel), root_factory=make_root)
        with self.assertRaises(LoginCancelled):
            window.run()
        self.assertEqual(captured.get("size"), (280, 280))

    def _measure(self, png: str) -> dict:
        import tkinter

        captured: dict = {}

        def make_root():
            root = tkinter.Tk()

            def inspect() -> None:
                for child in root.winfo_children():
                    if isinstance(child, tkinter.Frame):
                        label = child.winfo_children()[0]
                        photo = window._photo
                        captured["image"] = (photo.width(), photo.height())
                        captured["holder"] = (child.winfo_reqwidth(), child.winfo_reqheight())
                        captured["label"] = (label.winfo_reqwidth(), label.winfo_reqheight())

            root.after(300, inspect)
            root.after(600, window.close)
            return root

        window = QrLoginWindow(login_factory=lambda cancel: _BlockingLogin(cancel, png), root_factory=make_root)
        with self.assertRaises(LoginCancelled):
            window.run()
        return captured

    def test_generated_codes_are_shown_whole(self):
        for length in (20, 32, 64):
            with self.subTest(token_length=length):
                with tempfile.TemporaryDirectory() as tmp:
                    png = Path(tmp) / "qr.png"
                    write_qr_png(build_qr_url("x" * length), png)
                    captured = self._measure(str(png))
                image = captured["image"]
                holder = captured["holder"]
                label = captured["label"]
                # Oversized codes are subsampled, never clipped: the whole
                # square (quiet zone included) is drawn at a sane size.
                self.assertLessEqual(max(image), MAX_QR_PIXELS)
                self.assertGreater(min(image), 0)
                self.assertGreaterEqual(max(image), 300)
                # The holder follows the label (image plus border), so nothing
                # is cropped by the old fixed 280 px box.
                self.assertEqual(holder, label)
                self.assertGreaterEqual(label[0], image[0])
                self.assertGreaterEqual(label[1], image[1])

    def test_expired_code_is_refreshed_then_succeeds(self):
        attempts: list = []

        def factory(cancel):
            attempts.append(1)
            return _FlakyLogin(len(attempts))

        window = QrLoginWindow(login_factory=factory, root_factory=lambda: self._root(window, 4000))
        window.run()
        self.assertEqual(len(attempts), 2)

    def test_failure_surfaces_when_the_window_is_closed(self):
        window = QrLoginWindow(
            login_factory=lambda cancel: _FailedLogin(RemoteError("上游连接失败")),
            root_factory=lambda: self._root(window, 400),
        )
        with self.assertRaisesRegex(RemoteError, "上游连接失败"):
            window.run()

    def test_default_qrlogin_cancels_before_saving(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            window = QrLoginWindow(
                transport=_qr_transport({"polls": 0}),
                cookie_path=root / "config" / "cookie",
                root_factory=lambda: self._root(window, 500),
            )
            with self.assertRaises(LoginCancelled):
                window.run()
            self.assertFalse((root / "config" / "cookie").exists())


# -- service login state -------------------------------------------------


class _BridgeCase(unittest.TestCase):
    """Starts one bridge on an ephemeral port under a private config dir."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="quark-potplayer-login-"))
        self.previous = os.environ.get("QUARK_CONFIG_DIR")
        os.environ["QUARK_CONFIG_DIR"] = str(self.directory)
        self.fixture = DriveFixture()
        self.login = None
        self.service = Service(**self.service_kwargs())
        self.server = self.service.build_server()
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.service.port}"
        self.client = httpx.Client(timeout=10.0)
        self.addCleanup(self._stop)

    def service_kwargs(self) -> dict:
        raise NotImplementedError

    def _stop(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.service.close()
        self.thread.join(10)
        if self.previous is None:
            os.environ.pop("QUARK_CONFIG_DIR", None)
        else:
            os.environ["QUARK_CONFIG_DIR"] = self.previous
        shutil.rmtree(self.directory, ignore_errors=True)

    def api(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.service.token}"}
        return self.client.request(method, self.base + path, headers=headers, **kwargs)


class _ServiceLogin:
    def __init__(self) -> None:
        self.png = Path("qrcode.png")
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, on_qr_ready=None) -> None:
        if on_qr_ready is not None:
            on_qr_ready(self.png)
        self.started.set()
        self.release.wait(5)
        raise LoginCancelled("登录已取消")


class ServiceCancelCase(_BridgeCase):
    def service_kwargs(self) -> dict:
        self.login = _ServiceLogin()
        return {"transport": self.fixture.transport(), "cookie": None, "login_factory": lambda: self.login}

    def test_cancelled_window_returns_to_idle_without_saving(self):
        started = self.api("POST", "/v1/auth/login")
        self.assertEqual(started.status_code, 202)
        self.assertTrue(self.login.started.wait(5))
        self.assertEqual(self.api("GET", "/v1/auth").json()["login_state"], "waiting")

        self.login.release.set()
        payload = {}
        for _ in range(100):
            payload = self.api("GET", "/v1/auth").json()
            if payload["login_state"] == "idle":
                break
            time.sleep(0.05)
        self.assertEqual(payload["login_state"], "idle")
        self.assertFalse(payload["authenticated"])
        self.assertFalse(plugin_state.credential_path().exists())


class MissingCredentialCase(_BridgeCase):
    def service_kwargs(self) -> dict:
        return {"transport": self.fixture.transport(), "cookie": None}

    def test_control_endpoint_prompts_a_rescan(self):
        response = self.api("GET", "/v1/files")
        self.assertEqual(response.status_code, 401)
        error = response.json()["error"]
        self.assertEqual(error["type"], "login_required")
        self.assertIn("扫码", error["message"])


# -- entry point ---------------------------------------------------------


class EntryPointCase(unittest.TestCase):
    def _login(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return entry.main(["--login"])

    def test_login_action_opens_the_qr_window(self):
        calls: list = []

        class FakeWindow:
            def __init__(self, **kwargs) -> None:
                calls.append(("init", kwargs))

            def run(self, **kwargs) -> None:
                calls.append(("run", kwargs))

        with mock.patch.object(entry, "QrLoginWindow", FakeWindow):
            code = self._login()
        self.assertEqual(code, 0)
        self.assertEqual(calls[0], ("init", {"cookie_path": plugin_state.credential_path()}))
        self.assertEqual(calls[1], ("run", {}))

    def test_login_failure_reports_nonzero(self):
        class FakeWindow:
            def __init__(self, **kwargs) -> None:
                pass

            def run(self, **kwargs) -> None:
                raise AuthenticationError("二维码已过期，请重新获取二维码")

        with mock.patch.object(entry, "QrLoginWindow", FakeWindow):
            code = self._login()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
