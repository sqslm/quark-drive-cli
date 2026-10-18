"""Focused integration tests for the bridge's diagnostics and playback-monitor wiring.

These drive a real loopback bridge (real ``Service`` + ``ThreadingHTTPServer``)
over the in-memory Quark fixture, but inject a fake native receiver and a fake
status-window launcher, so no real player or GUI is ever touched. The monitor is
built with ``start=False`` so every poll goes through the deterministic
``_poll_once`` seam on the test thread.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import quote

import httpx

from quark_cli import auth
from quark_cli.errors import LocalIOError
from quark_cli.potplayer import plugin_state
from quark_cli.potplayer.library_state import Library
from quark_cli.potplayer.service import Service

from potplayer_fixture import COOKIE, VIDEO_BYTES, DriveFixture, consuming_clock

UINT32_MAX = 0xFFFFFFFF


class FakeSample:
    __slots__ = ("player_pid", "file_name", "position_ms", "duration_ms", "status")

    def __init__(self, player_pid, file_name, position_ms, duration_ms, status) -> None:
        self.player_pid = player_pid
        self.file_name = file_name
        self.position_ms = position_ms
        self.duration_ms = duration_ms
        self.status = status


class FakeNative:
    """Minimal stand-in for ``native_player.NativePlayer``: no OS, no window."""

    def __init__(self) -> None:
        self.queued: dict[int, FakeSample | None] = {}
        self.seeks: list[tuple[int, str, int]] = []
        self.sample_calls = 0
        self.closed = False

    def feed(self, sample: FakeSample | None) -> None:
        if sample is not None:
            self.queued[sample.player_pid] = sample

    def sample(self, player_pid: int):
        self.sample_calls += 1
        return self.queued.get(player_pid)

    def seek(self, player_pid: int, expected_file_name: str, position_ms: int) -> bool:
        self.seeks.append((player_pid, expected_file_name, position_ms))
        return True

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.calls = 0

    def poll(self):
        return self.returncode


class RecordingLibrary(Library):
    def __init__(self, path) -> None:
        super().__init__(path)
        self.saves: list[tuple[str, int, int, int]] = []

    def save_progress(self, fid, size, position_ms, duration_ms):  # type: ignore[override]
        self.saves.append((fid, size, position_ms, duration_ms))
        return super().save_progress(fid, size, position_ms, duration_ms)


class _StoppedGuard:
    """A guard whose cooldown storage is unreadable; only the two reads the model uses."""

    def active_cooldown(self):
        raise LocalIOError("已停止")

    def now(self) -> float:
        return 0.0


class _ServerCase(unittest.TestCase):
    """One bridge on an ephemeral loopback port, with a fake native and launcher."""

    cookie: str | None = COOKIE
    status_launcher: object | None = None

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="quark-diag-svc-"))
        self.previous_config = os.environ.get("QUARK_CONFIG_DIR")
        os.environ["QUARK_CONFIG_DIR"] = str(self.directory)
        self.fixture = DriveFixture()
        self.native = FakeNative()
        self.launcher = FakeProcess()
        self.service = Service(
            transport=self.fixture.transport(),
            cookie=self.cookie,
            native_factory=lambda: self.native,
            status_launcher=self._launch,
            monitor_start=False,
        )
        consuming = mock.patch.object(self.service.monitor, 'buffer_state', side_effect=consuming_clock(self.service))
        consuming.start()
        self.addCleanup(consuming.stop)
        self.server = self.service.build_server()
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.service.port}"
        self.client = httpx.Client(timeout=10.0)
        self.addCleanup(self._stop)

    def _launch(self):
        self.launcher.calls += 1
        return self.launcher

    def _stop(self) -> None:
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.service.close()
        self.thread.join(10)
        if self.previous_config is None:
            os.environ.pop("QUARK_CONFIG_DIR", None)
        else:
            os.environ["QUARK_CONFIG_DIR"] = self.previous_config
        shutil.rmtree(self.directory, ignore_errors=True)

    @property
    def token(self) -> str:
        return self.service.token

    def api(self, method: str, path: str, *, body=None, token: str | None = None, **kwargs) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        if token is not False:
            headers["Authorization"] = f"Bearer {self.token if token is None else token}"
        return self.client.request(method, self.base + path, json=body, headers=headers, **kwargs)

    def play(self, fid: str, pid=None) -> dict:
        payload = {"fid": fid}
        if pid is not None:
            payload["player_pid"] = pid
        response = self.api("POST", "/v1/play", body=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def wait_streamed(self, player_pid: int, timeout: float = 5.0) -> None:
        """Bounded synchronization on the monitor's first-chunk callback.

        The stream GET can return to the client after the first chunk is on the
        socket but before ``record_once`` has finished its disk write and marked
        the binding streamed, so the byte-accounting and native feeds below must
        wait for that flag rather than assume the GET already set it.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.service.monitor._lock:
                binding = self.service.monitor._bindings.get(player_pid)
                if binding is not None and binding.streamed:
                    return
            time.sleep(0.01)
        self.fail(f"binding {player_pid} never streamed")

    def snapshot(self) -> dict:
        response = self.api("GET", "/v1/diagnostics")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def account(self) -> dict:
        return self.snapshot()["account"]


# -- routes and privacy -------------------------------------------------


class DiagnosticsRouteCase(_ServerCase):
    cookie = None  # no plugin credential at all: the routes are local-only.

    def test_snapshot_and_report_work_without_a_cookie_and_touch_no_api(self):
        snapshot = self.snapshot()
        self.assertFalse(snapshot["account"]["credential_present"])
        self.assertEqual(snapshot["account"]["validation"], "no_login")
        self.assertEqual(snapshot["playback"], [])
        report = self.api("GET", "/v1/diagnostics/report").json()
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["release"], "0.12.3")
        # No account/API request was made by either projection.
        self.assertEqual(self.fixture.requests, [])

    def test_wrong_bearer_and_methods_are_rejected(self):
        self.assertEqual(self.client.get(self.base + "/v1/diagnostics").status_code, 401)
        self.assertEqual(self.api("GET", "/v1/diagnostics", token="wrong").status_code, 401)
        self.assertEqual(self.api("GET", "/v1/diagnostics/report", token=False).status_code, 401)
        for method, path in (
            ("POST", "/v1/diagnostics"),
            ("POST", "/v1/diagnostics/report"),
            ("GET", "/v1/diagnostics/open"),
            ("GET", "/v1/playback/restart"),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.api(method, path, body={}).status_code, 405)
        self.assertEqual(self.api("GET", "/v1/diagnostics/absent").status_code, 404)


class DiagnosticsPlaybackPrivacyCase(_ServerCase):
    """Playback-bearing diagnostics need a credential: ``/v1/play`` resolves the
    file through the Quark API, so it is legitimately 401 without one."""

    cookie = COOKIE

    def test_report_is_allowlisted_and_never_carries_playback(self):
        self.play("v2")
        report = self.api("GET", "/v1/diagnostics/report").json()
        self.assertNotIn("playback", report)
        blob = json.dumps(report, ensure_ascii=False)
        for needle in ("第2集", "v2", "/v1/stream", "playback"):
            self.assertNotIn(needle, blob)

    def test_snapshot_includes_playback_but_report_never_does(self):
        self.assertEqual(self.snapshot()["playback"], [])
        self.play("v2", pid=1234)
        combined = self.snapshot()
        self.assertIn("playback", combined)
        self.assertEqual([row["player_pid"] for row in combined["playback"]], [1234])
        self.assertNotIn("playback", self.api("GET", "/v1/diagnostics/report").json())


# -- credential observation ---------------------------------------------


class CredentialStateCase(_ServerCase):
    cookie = COOKIE

    def test_saved_cookie_is_pending_until_a_real_call_succeeds(self):
        self.assertEqual(self.account()["validation"], "saved_unverified")
        self.assertEqual(self.api("GET", "/v1/files").status_code, 200)
        account = self.account()
        self.assertEqual(account["validation"], "recently_valid")
        self.assertIsNotNone(account["last_success_at"])

    def test_upstream_auth_failure_marks_expired_and_drops_only_the_plugin_cookie(self):
        cli_cookie = auth.credential_path()
        auth.save_cookie("cli=1; __puus=cli", cli_cookie)
        self.assertEqual(self.api("GET", "/v1/files").status_code, 200)
        self.fixture.api_unauthorized = True
        self.assertEqual(self.api("GET", "/v1/files").status_code, 401)
        # The observed expiry outlives the (now dropped) credential.
        account = self.account()
        self.assertEqual(account["validation"], "expired")
        self.assertFalse(account["credential_present"])
        self.assertTrue(cli_cookie.exists())
        self.assertEqual(auth.load_saved_cookie(cli_cookie), "cli=1; __puus=cli")

    def test_missing_cookie_is_not_an_expired_account(self):
        service = self.service
        service._cookie = None  # no injected and no saved credential
        self.assertEqual(self.api("GET", "/v1/files").status_code, 401)
        account = self.account()
        self.assertEqual(account["validation"], "no_login")
        self.assertNotEqual(account["validation"], "expired")

    def test_new_credential_resets_the_observed_expiry(self):
        self.assertEqual(self.api("GET", "/v1/files").status_code, 200)
        self.fixture.api_unauthorized = True
        self.assertEqual(self.api("GET", "/v1/files").status_code, 401)
        self.assertEqual(self.account()["validation"], "expired")
        # A new saved credential is a credential change: unverified again.
        plugin_state.store_cookie("b=2; __puus=other")
        self.api("GET", "/v1/auth")
        self.service.client()
        self.assertEqual(self.account()["validation"], "saved_unverified")
        self.assertIsNone(self.account()["last_success_at"])

    def test_logout_reports_no_login_and_keeps_the_cli_cookie(self):
        cli_cookie = auth.credential_path()
        auth.save_cookie("cli=1; __puus=cli", cli_cookie)
        self.assertEqual(self.api("GET", "/v1/files").status_code, 200)
        self.assertEqual(self.api("POST", "/v1/auth/logout").json(), {"authenticated": False})
        self.assertEqual(self.account()["validation"], "no_login")
        self.assertTrue(cli_cookie.exists())

    def test_origin_429_sets_a_shared_cooldown_without_touching_the_credential(self):
        url = self.play("v2")["url"]
        self.fixture.origin_status = 429
        response = self.api("GET", url)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["type"], "rate_limited")
        # The credential is retained and the account is not marked expired.
        self.assertTrue(self.api("GET", "/v1/auth").json()["authenticated"])
        account = self.account()
        self.assertEqual(account["validation"], "recently_valid")
        protection = self.snapshot()["protection"]
        self.assertTrue(protection["active"])
        self.assertEqual(protection["reason"], "rate_limited")
        # The shared cooldown refuses the next calls without touching the fixture.
        before = len(self.fixture.requests)
        self.assertEqual(self.api("GET", "/v1/files").status_code, 429)
        self.assertEqual(len(self.fixture.requests), before)
        self.assertNotEqual(self.account()["validation"], "expired")

    def test_origin_403_still_keeps_the_credential(self):
        url = self.play("v2")["url"]
        self.fixture.origin_status = 403
        self.fixture.origin_failures = 0
        response = self.api("GET", url)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["type"], "access_restricted")
        self.assertTrue(self.api("GET", "/v1/auth").json()["authenticated"])
        self.assertNotEqual(self.account()["validation"], "expired")


class StoppedGuardCase(unittest.TestCase):
    """A guard whose storage failed surfaces as a fixed protection note."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="quark-diag-guard-"))
        self.previous_config = os.environ.get("QUARK_CONFIG_DIR")
        os.environ["QUARK_CONFIG_DIR"] = str(self.directory)
        self.service = Service(
            cookie=COOKIE, guard=_StoppedGuard(), native_factory=lambda: FakeNative(), monitor_start=False
        )
        self.server = self.service.build_server()
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.service.port}"
        self.client = httpx.Client(timeout=10.0)
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.service.close()
        self.thread.join(10)
        if self.previous_config is None:
            os.environ.pop("QUARK_CONFIG_DIR", None)
        else:
            os.environ["QUARK_CONFIG_DIR"] = self.previous_config
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_storage_failure_is_reported_as_a_fixed_note(self):
        response = self.client.get(
            self.base + "/v1/diagnostics",
            headers={"Authorization": f"Bearer {self.service.token}"},
        )
        self.assertEqual(response.status_code, 200)
        protection = response.json()["protection"]
        self.assertEqual(protection["reason"], "local_protection_stopped")
        self.assertIn("instruction", protection)


# -- byte accounting and origin observation -----------------------------


class TransferCounterCase(_ServerCase):
    cookie = COOKIE

    def test_head_counts_nothing_and_a_full_get_counts_every_byte(self):
        url = self.play("v2")["url"]
        self.assertEqual(self.client.head(self.base + url).status_code, 200)
        totals = self.snapshot()["transfer"]["totals"]
        self.assertEqual(totals["bytes"], 0)
        self.assertEqual(totals["chunks"], 0)

        self.assertEqual(self.api("GET", url).status_code, 200)
        totals = self.snapshot()["transfer"]["totals"]
        self.assertEqual(totals["bytes"], len(VIDEO_BYTES))
        self.assertGreater(totals["chunks"], 0)
        self.assertEqual(totals["status"]["2xx"], 1)

    def test_repeated_seeks_over_the_same_range_count_again(self):
        url = self.play("v2")["url"]
        for _ in range(2):
            self.assertEqual(self.api("GET", url, headers={"Range": "bytes=0-9"}).status_code, 206)
        totals = self.snapshot()["transfer"]["totals"]
        self.assertEqual(totals["bytes"], 20)
        self.assertEqual(totals["chunks"], 2)

    def test_origin_rtt_is_recorded_as_a_nonnegative_integer(self):
        url = self.play("v2")["url"]
        self.assertEqual(self.api("GET", url, headers={"Range": "bytes=0-9"}).status_code, 206)
        plays = self.snapshot()["transfer"]["plays"]
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]["last_status"], 206)
        self.assertIsInstance(plays[0]["last_elapsed_ms"], int)
        self.assertGreaterEqual(plays[0]["last_elapsed_ms"], 0)
        self.assertEqual(plays[0]["origin_requests"], 1)

    def test_origin_429_is_observed_even_though_the_guard_rejects_it(self):
        url = self.play("v2")["url"]
        self.fixture.origin_status = 429
        self.assertEqual(self.api("GET", url).status_code, 429)
        totals = self.snapshot()["transfer"]
        self.assertEqual(totals["totals"]["status"]["4xx"], 1)
        play = totals["plays"][0]
        self.assertEqual(play["last_status"], 429)
        self.assertIsInstance(play["last_elapsed_ms"], int)


# -- playback monitor ----------------------------------------------------


class PlaybackMonitorCase(_ServerCase):
    cookie = COOKIE

    def test_invalid_player_pid_is_refused_before_any_quark_request(self):
        for bad in (0, -1, True, "12", 1.5, UINT32_MAX + 1):
            with self.subTest(pid=bad):
                response = self.api("POST", "/v1/play", body={"fid": "v2", "player_pid": bad})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["type"], "bad_request")
        self.assertEqual(self.fixture.requests, [])
        self.assertEqual(self.fixture.download_calls, 0)

    def test_play_without_a_pid_keeps_the_old_body_and_response(self):
        self.assertEqual(self.snapshot()["playback"], [])
        payload = self.play("v2")
        self.assertEqual(set(payload), {"url", "title"})
        self.assertEqual(payload["title"], "第2集.mp4")
        self.assertTrue(payload["url"].startswith("/v1/stream/"))
        self.assertEqual(self.snapshot()["playback"], [])

    def test_first_chunk_starts_the_monitor_sampling(self):
        url = self.play("v2", pid=4321)["url"]
        view = self.snapshot()["playback"]
        self.assertEqual([row["player_pid"] for row in view], [4321])
        self.assertEqual(view[0]["state"], "waiting")
        # Nothing is sampled until a byte actually reaches the player.
        self.service.monitor._poll_once()
        self.assertEqual(self.native.sample_calls, 0)

        self.assertEqual(self.api("GET", url).status_code, 200)
        self.wait_streamed(4321)
        self.native.feed(FakeSample(4321, "https://quark.invalid/video/v2", 30_000, 1_800_000, 2))
        self.service.monitor._poll_once()
        self.assertGreaterEqual(self.native.sample_calls, 1)
        self.assertNotEqual(self.snapshot()["playback"][0]["state"], "waiting")

    def test_restart_only_accepts_a_currently_bound_pid_and_fid(self):
        url = self.play("v2", pid=777)["url"]
        self.assertEqual(self.api("GET", url).status_code, 200)
        self.wait_streamed(777)
        self.native.feed(FakeSample(777, "https://quark.invalid/video/v2", 30_000, 1_800_000, 2))
        ok = self.api("POST", "/v1/playback/restart", body={"player_pid": 777, "fid": "v2"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json(), {"status": "restarted"})
        self.assertEqual(self.native.seeks[-1], (777, "https://quark.invalid/video/v2", 0))

        wrong_pid = self.api("POST", "/v1/playback/restart", body={"player_pid": 778, "fid": "v2"})
        self.assertEqual(wrong_pid.status_code, 409)
        self.assertNotEqual(wrong_pid.json()["error"]["type"], "login_required")
        wrong_fid = self.api("POST", "/v1/playback/restart", body={"player_pid": 777, "fid": "v9"})
        self.assertEqual(wrong_fid.status_code, 409)
        invalid = self.api("POST", "/v1/playback/restart", body={"player_pid": "777", "fid": "v2"})
        self.assertEqual(invalid.status_code, 400)
        self.assertTrue(self.api("GET", "/v1/auth").json()["authenticated"])

    def test_restart_is_refused_until_a_byte_actually_streamed(self):
        url = self.play("v2", pid=999)["url"]
        self.native.feed(FakeSample(999, "https://quark.invalid/video/v2", 30_000, 1_800_000, 2))
        # A bound pid with a real sample is still not restarted while no byte
        # has reached the player: the command is refused, never queued.
        refused = self.api("POST", "/v1/playback/restart", body={"player_pid": 999, "fid": "v2"})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(self.native.seeks, [])

        self.assertEqual(self.api("GET", url).status_code, 200)
        self.wait_streamed(999)
        ok = self.api("POST", "/v1/playback/restart", body={"player_pid": 999, "fid": "v2"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.native.seeks[-1], (999, "https://quark.invalid/video/v2", 0))

    def test_same_fid_binding_that_never_streamed_does_not_suspend_the_live_one(self):
        live = self.play("v2", pid=1001)["url"]
        self.play("v2", pid=1002)
        self.assertEqual(self.api("GET", live).status_code, 200)
        self.wait_streamed(1001)
        self.native.feed(FakeSample(1001, "https://quark.invalid/video/v2", 30_000, 1_800_000, 2))
        # 1002 is bound to the same FID but never streamed: it must not be
        # counted as a second player and must not suspend the real one.
        self.native.feed(FakeSample(1002, "https://quark.invalid/video/v2", 0, 1_800_000, 0))
        self.service.monitor._poll_once()
        states = {row["player_pid"]: row["state"] for row in self.snapshot()["playback"]}
        self.assertEqual(states[1001], "playing")

    def test_clear_recent_suppresses_progress_until_a_rebind(self):
        self.service.library = RecordingLibrary(self.service.library.path)
        self.service.monitor._library = self.service.library
        fid, size, pid = "v2", len(VIDEO_BYTES), 555
        url = self.play(fid, pid=pid)["url"]
        self.assertEqual(self.api("GET", url).status_code, 200)
        self.wait_streamed(pid)
        self.native.feed(FakeSample(pid, f"https://quark.invalid/video/{fid}", 30_000, 1_800_000, 2))
        self.service.monitor._poll_once()
        self.service.monitor._poll_once()
        self.assertIsNotNone(self.service.library.progress(fid, size))

        self.assertEqual(self.api("POST", "/v1/recent/clear", body={}).json(), {"items": []})
        self.assertIsNone(self.service.library.progress(fid, size))
        self.native.feed(FakeSample(pid, f"https://quark.invalid/video/{fid}", 60_000, 1_800_000, 2))
        self.service.monitor._poll_once()
        self.assertEqual(self.service.library.recent(), [])

    def test_logout_suppresses_the_monitor_and_keeps_the_cli_cookie(self):
        cli_cookie = auth.credential_path()
        auth.save_cookie("cli=1; __puus=cli", cli_cookie)
        plugin_state.store_cookie(COOKIE)
        url = self.play("v2", pid=888)["url"]
        self.assertEqual(self.api("GET", url).status_code, 200)
        self.wait_streamed(888)
        self.native.feed(FakeSample(888, "https://quark.invalid/video/v2", 30_000, 1_800_000, 2))
        self.service.monitor._poll_once()
        self.service.monitor._poll_once()
        self.assertTrue(self.service.library.recent())

        self.assertEqual(self.api("POST", "/v1/auth/logout").json(), {"authenticated": False})
        self.assertEqual(self.service.library.recent(), [])
        self.assertEqual(self.service.library.favorites(), [])
        self.assertTrue(cli_cookie.exists())
        self.native.feed(FakeSample(888, "https://quark.invalid/video/v2", 90_000, 1_800_000, 2))
        self.service.monitor._poll_once()
        self.assertEqual(self.service.library.recent(), [])


# -- status window launcher ---------------------------------------------


class OpenWindowCase(_ServerCase):
    cookie = None

    def test_open_spawns_once_and_reuses_the_live_process(self):
        first = self.api("POST", "/v1/diagnostics/open", body={})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json(), {"status": "opened"})
        again = self.api("POST", "/v1/diagnostics/open", body={})
        self.assertEqual(again.json(), {"status": "already_open"})
        self.assertEqual(self.launcher.calls, 1)

        # Once the child exits, the next open spawns a fresh one.
        self.launcher.returncode = 0
        third = self.api("POST", "/v1/diagnostics/open", body={})
        self.assertEqual(third.json(), {"status": "opened"})
        self.assertEqual(self.launcher.calls, 2)

    def test_open_needs_no_quark_login(self):
        self.assertEqual(self.api("GET", "/v1/auth").json()["authenticated"], False)
        self.assertEqual(self.api("POST", "/v1/diagnostics/open", body={}).status_code, 202)


if __name__ == "__main__":
    unittest.main()
