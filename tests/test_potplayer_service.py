"""Behavioural tests for the PotPlayer bridge over real loopback HTTP."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from quark_cli import auth
from quark_cli.errors import AuthenticationError
from quark_cli.potplayer import plugin_state
from quark_cli.potplayer.service import Service
from quark_cli.potplayer.stream import Media, Sessions
from quark_cli.state import write_json

from potplayer_fixture import (
    COOKIE,
    ORIGIN_HOST,
    SEGMENT_LIMIT,
    VIDEO_BYTES,
    DriveFixture,
    record,
    virtual_slice,
)


class _ServerCase(unittest.TestCase):
    """Starts one bridge instance on an ephemeral port and tears it down."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="quark-potplayer-"))
        self.previous_config = os.environ.get("QUARK_CONFIG_DIR")
        os.environ["QUARK_CONFIG_DIR"] = str(self.directory)
        self.fixture = DriveFixture()
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

    def play(self, fid: str) -> dict:
        response = self.api("POST", "/v1/play", body={"fid": fid})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def stream(self, url: str, **kwargs) -> httpx.Response:
        return self.client.get(self.base + url, **kwargs)


class BridgeCase(_ServerCase):
    def service_kwargs(self) -> dict:
        return {"transport": self.fixture.transport(), "cookie": COOKIE}

    # -- health and authentication ---------------------------------------

    def test_health_is_public_and_identifies_the_instance(self):
        response = self.client.get(self.base + "/v1/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["instance"], self.service.instance)
        self.assertEqual(payload["version"], "1")

    def test_control_endpoints_require_the_runtime_token(self):
        self.assertEqual(self.client.get(self.base + "/v1/files").status_code, 401)
        self.assertEqual(self.api("GET", "/v1/files", token="wrong").status_code, 401)
        payload = self.client.get(self.base + "/v1/files").json()
        self.assertEqual(payload["error"]["type"], "unauthorized")
        self.assertEqual(self.api("POST", "/v1/auth/logout", token=False).status_code, 401)

    def test_method_and_route_validation(self):
        self.assertEqual(self.client.post(self.base + "/v1/health").status_code, 405)
        self.assertEqual(self.api("DELETE", "/v1/files").status_code, 405)
        self.assertEqual(self.api("GET", "/v1/absent").status_code, 404)

    def test_auth_status_reports_the_plugin_credential(self):
        payload = self.api("GET", "/v1/auth").json()
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["login_state"], "idle")
        self.assertIsNone(payload["qr_png"])

    # -- directory pages -------------------------------------------------

    def test_root_page_lists_directories_without_following_pages(self):
        payload = self.api("GET", "/v1/files?parent=0&page=1").json()
        self.assertEqual(payload["page"], 1)
        self.assertFalse(payload["has_more"])
        self.assertEqual([item["name"] for item in payload["items"]], ["剧集", "空目录"])
        self.assertEqual(payload["items"][0]["is_dir"], True)
        self.assertEqual(payload["items"][0]["size"], 0)

    def test_directory_page_keeps_the_server_order_and_flags_more_pages(self):
        for index in range(120):
            self.fixture.records.append(record(f"bulk-{index:03d}", f"第{index}集.mp4", "root-dir"))
        first = self.api("GET", "/v1/files?parent=root-dir&size=50").json()
        self.assertEqual(len(first["items"]), 50)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["items"][0]["fid"], "v10")
        third = self.api("GET", "/v1/files?parent=root-dir&page=3&size=50").json()
        self.assertEqual(len(third["items"]), 25)
        self.assertFalse(third["has_more"])

    def test_empty_directory_and_page_bounds(self):
        payload = self.api("GET", "/v1/files?parent=empty-dir").json()
        self.assertEqual(payload["items"], [])
        self.assertFalse(payload["has_more"])
        self.assertEqual([item["fid"] for item in self.api("GET", "/v1/files?parent=").json()["items"]],
                         ["root-dir", "empty-dir"])
        self.assertEqual(self.api("GET", "/v1/files?page=0").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/files?size=200").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/files?page=abc").status_code, 400)

    def test_unknown_directory_reports_a_remote_failure(self):
        response = self.api("GET", "/v1/files?parent=missing")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["type"], "remote_error")

    # -- playlist --------------------------------------------------------

    def test_playlist_is_naturally_ordered_and_skips_non_video_files(self):
        payload = self.api("GET", "/v1/playlist?parent=root-dir").json()
        self.assertEqual(payload["parent"], "root-dir")
        self.assertEqual([item["name"] for item in payload["items"]], ["第2集.mp4", "第9集.mkv", "第10集.mp4"])
        self.assertEqual([item["fid"] for item in payload["items"]], ["v2", "v9", "v10"])

    def test_playlist_from_a_video_starts_at_that_video(self):
        payload = self.api("GET", "/v1/playlist?start=v9").json()
        self.assertEqual(payload["parent"], "root-dir")
        self.assertEqual([item["fid"] for item in payload["items"]], ["v9", "v10"])

    def test_playlist_start_can_derive_the_parent_from_file_info(self):
        self.fixture.omit_download_name = True
        payload = self.api("GET", "/v1/playlist?start=v10").json()
        self.assertEqual([item["fid"] for item in payload["items"]], ["v10"])

    def test_playlist_rejects_non_video_start_and_missing_arguments(self):
        self.assertEqual(self.api("GET", "/v1/playlist?start=poster").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/playlist").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/playlist?start=absent").status_code, 502)

    # -- play and stream -------------------------------------------------

    def test_play_returns_a_local_session_url_without_upstream_secrets(self):
        payload = self.play("v2")
        self.assertEqual(payload["title"], "第2集.mp4")
        self.assertTrue(payload["url"].startswith("/v1/stream/"))
        self.assertNotIn(ORIGIN_HOST, payload["url"])
        session = payload["url"].split("/")[3]
        self.assertGreaterEqual(len(session), 32)
        self.assertNotIn("__puus", payload["url"])

    def test_play_falls_back_to_file_info_for_name_and_size(self):
        self.fixture.omit_download_name = True
        payload = self.play("v2")
        self.assertEqual(payload["title"], "第2集.mp4")
        self.assertEqual(len(self.stream(payload["url"]).content), len(VIDEO_BYTES))

    def test_directory_cannot_be_played(self):
        response = self.api("POST", "/v1/play", body={"fid": "root-dir"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.api("POST", "/v1/play", body={"fid": ""}).status_code, 400)
        self.assertEqual(self.api("POST", "/v1/play", body={}).status_code, 400)
        self.assertEqual(self.api("POST", "/v1/play", content=b"not-json").status_code, 400)

    def test_full_get_streams_the_whole_object(self):
        payload = self.play("v2")
        response = self.stream(payload["url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-length"], str(len(VIDEO_BYTES)))
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertEqual(response.content, VIDEO_BYTES)

    def test_ranges_cover_head_middle_tail_and_suffix(self):
        url = self.play("v9")["url"]
        cases = {
            "bytes=0-99": (0, 99),
            "bytes=1000-1099": (1000, 1099),
            "bytes=1948-": (1948, len(VIDEO_BYTES) - 1),
            "bytes=-64": (len(VIDEO_BYTES) - 64, len(VIDEO_BYTES) - 1),
        }
        for header, (start, end) in cases.items():
            with self.subTest(header=header):
                response = self.stream(url, headers={"Range": header})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, VIDEO_BYTES[start:end + 1])
                self.assertEqual(response.headers["content-range"], f"bytes {start}-{end}/{len(VIDEO_BYTES)}")
                self.assertEqual(response.headers["content-length"], str(end - start + 1))
        self.assertEqual(self.stream(url).headers["content-type"], "video/x-matroska")

    def test_unsatisfiable_and_unsupported_ranges_answer_416(self):
        url = self.play("v2")["url"]
        for header in ("bytes=5000-6000", "bytes=0-1,4-5", "bytes=-0", "items=0-1", "bytes=9-3"):
            with self.subTest(header=header):
                response = self.stream(url, headers={"Range": header})
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response.headers["content-range"], f"bytes */{len(VIDEO_BYTES)}")

    def test_head_answers_from_session_metadata(self):
        url = self.play("v2")["url"]
        full = self.client.head(self.base + url)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.headers["content-length"], str(len(VIDEO_BYTES)))
        self.assertEqual(full.content, b"")
        ranged = self.client.head(self.base + url, headers={"Range": "bytes=10-19"})
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.headers["content-range"], f"bytes 10-19/{len(VIDEO_BYTES)}")
        self.assertEqual(ranged.headers["content-length"], "10")
        self.assertEqual(self.fixture.origin_requests(), [])

    def test_unknown_session_is_not_found(self):
        self.assertEqual(self.stream("/v1/stream/absent/name.mp4").status_code, 404)
        self.assertEqual(self.stream("/v1/stream/").status_code, 400)

    def test_expired_address_is_refetched_once(self):
        payload = self.play("v2")
        self.fixture.origin_failures = 1
        self.fixture.origin_epoch += 1
        response = self.stream(payload["url"], headers={"Range": "bytes=0-9"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, VIDEO_BYTES[:10])
        self.assertEqual(self.fixture.download_calls, 2)

    def test_repeated_expiry_stops_after_one_retry(self):
        payload = self.play("v2")
        self.fixture.origin_failures = 4
        self.fixture.origin_epoch += 1
        response = self.stream(payload["url"])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["type"], "access_restricted")
        self.assertGreaterEqual(int(response.headers["retry-after"]), 1)
        self.assertEqual(self.fixture.download_calls, 2)
        # The credential is preserved: the bridge still reports it signed in.
        self.assertTrue(self.api("GET", "/v1/auth").json()["authenticated"])
        requests, origins = len(self.fixture.requests), len(self.fixture.origin_requests())
        # The recorded cooldown refuses the next media and control calls locally,
        # so neither the origin nor the account API is touched again.
        follow = self.stream(payload["url"])
        self.assertEqual(follow.status_code, 403)
        self.assertEqual(follow.json()["error"]["type"], "access_restricted")
        self.assertGreaterEqual(int(follow.headers["retry-after"]), 1)
        self.assertEqual(self.api("GET", "/v1/files").status_code, 403)
        self.assertEqual(len(self.fixture.requests), requests)
        self.assertEqual(len(self.fixture.origin_requests()), origins)

    def test_changed_size_on_refetch_aborts_and_forgets_the_session(self):
        payload = self.play("v2")
        self.fixture.origin_failures = 1
        self.fixture.announced_size = len(VIDEO_BYTES) + 1
        response = self.stream(payload["url"], headers={"Range": "bytes=0-9"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["type"], "media_changed")
        self.assertEqual(self.stream(payload["url"]).status_code, 404)

    def test_changed_origin_length_aborts_before_headers_are_sent(self):
        payload = self.play("v2")
        self.fixture.bodies["v2"] = VIDEO_BYTES + b"more"
        response = self.stream(payload["url"])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["type"], "media_changed")

    def test_origin_never_receives_the_account_cookie_or_echoes_the_signed_url(self):
        payload = self.play("v2")
        response = self.stream(payload["url"], headers={"Range": "bytes=0-9"})
        self.assertEqual(response.status_code, 206)
        self.assertNotIn("set-cookie", {name.lower() for name in response.headers})
        origins = self.fixture.origin_requests()
        self.assertTrue(origins)
        for request in origins:
            self.assertEqual(request.headers.get("cookie"), "__puus=secret")
            self.assertNotIn("b=1", request.headers.get("cookie", ""))
        self.assertEqual(self.fixture.requests[0].headers.get("cookie"), COOKIE)

    # -- upstream response validation ------------------------------------

    def test_origin_error_status_and_compression_are_not_forwarded(self):
        url = self.play("v2")["url"]
        self.fixture.origin_status = 500
        response = self.stream(url)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["type"], "remote_error")
        self.fixture.origin_status = None
        self.fixture.origin_encoding = "gzip"
        response = self.stream(url)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["type"], "remote_error")

    def test_origin_ignoring_the_range_is_not_silently_forwarded(self):
        url = self.play("v2")["url"]
        self.fixture.ignore_range = True
        response = self.stream(url, headers={"Range": "bytes=0-9"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["type"], "remote_error")

    def test_invalid_206_range_bounds_are_rejected(self):
        url = self.play("v2")["url"]
        for override in ("bytes 9-3/2048", f"bytes 0-{len(VIDEO_BYTES)}/{len(VIDEO_BYTES)}"):
            with self.subTest(override=override):
                self.fixture.content_range_override = override
                response = self.stream(url, headers={"Range": "bytes=0-9"})
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["error"]["type"], "remote_error")
        self.fixture.content_range_override = None

    def test_206_length_must_match_the_range(self):
        url = self.play("v2")["url"]
        self.fixture.content_length_override = 5
        response = self.stream(url, headers={"Range": "bytes=0-9"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["type"], "remote_error")
        self.fixture.content_length_override = None
        self.assertEqual(self.stream(url, headers={"Range": "bytes=0-9"}).status_code, 206)

    # -- version pinning -------------------------------------------------

    def test_strong_etag_is_pinned_and_stable_ranges_stream(self):
        self.fixture.origin_etag = '"v1"'
        url = self.play("v2")["url"]
        self.assertEqual(self.stream(url, headers={"Range": "bytes=0-9"}).status_code, 206)
        second = self.stream(url, headers={"Range": "bytes=10-19"})
        self.assertEqual(second.status_code, 206)
        self.assertEqual(second.content, VIDEO_BYTES[10:20])

    def test_changed_strong_etag_aborts_the_session(self):
        self.fixture.origin_etag = '"v1"'
        url = self.play("v2")["url"]
        self.assertEqual(self.stream(url, headers={"Range": "bytes=0-9"}).status_code, 206)
        self.fixture.origin_etag = '"v2"'
        response = self.stream(url, headers={"Range": "bytes=10-19"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["type"], "media_changed")

    def test_weak_etag_is_not_a_version_pin(self):
        self.fixture.origin_etag = 'W/"v1"'
        url = self.play("v2")["url"]
        self.assertEqual(self.stream(url, headers={"Range": "bytes=0-9"}).status_code, 206)
        self.fixture.origin_etag = 'W/"v2"'
        self.assertEqual(self.stream(url, headers={"Range": "bytes=10-19"}).status_code, 206)

    def test_sessions_pin_one_etag_under_concurrent_first_ranges(self):
        sessions = Sessions()
        session = sessions.create(Media(fid="v2", name="第2集.mp4", size=len(VIDEO_BYTES), url="https://origin/v2"))
        pins: list[bool] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            pins.append(sessions.pin_etag(session.session, '"v1"'))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(pins, [True] * 8)
        self.assertTrue(sessions.pin_etag(session.session, '"v1"'))
        self.assertFalse(sessions.pin_etag(session.session, '"v2"'))

    def test_origin_signature_failure_is_not_an_account_401(self):
        payload = self.play("v2")
        self.fixture.origin_failures = 4
        self.fixture.origin_epoch += 1
        response = self.stream(payload["url"])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["type"], "access_restricted")
        self.assertGreaterEqual(int(response.headers["retry-after"]), 1)
        # Not an account failure: the plugin credential is kept and still valid.
        self.assertTrue(self.api("GET", "/v1/auth").json()["authenticated"])
        requests, origins = len(self.fixture.requests), len(self.fixture.origin_requests())
        self.assertEqual(self.stream(payload["url"]).status_code, 403)
        self.assertEqual(self.api("GET", "/v1/files").status_code, 403)
        self.assertEqual(len(self.fixture.requests), requests)
        self.assertEqual(len(self.fixture.origin_requests()), origins)

    # -- keep-alive body handling ----------------------------------------

    def test_control_post_body_keeps_the_connection_usable(self):
        response = self.api("POST", "/v1/auth/logout", body={})
        self.assertEqual(response.status_code, 200)
        follow = self.api("GET", "/v1/auth")
        self.assertEqual(follow.status_code, 200)
        self.assertTrue(follow.headers["content-type"].startswith("application/json"))
        self.assertFalse(follow.json()["authenticated"])


class CredentialInvalidationCase(_ServerCase):
    """The account API proves a plugin credential stale; the CLI Cookie is untouched."""

    def service_kwargs(self) -> dict:
        return {"transport": self.fixture.transport(), "cookie": COOKIE}

    def test_account_401_forgets_the_plugin_credential_only(self):
        cli_cookie = auth.credential_path()
        auth.save_cookie("cli=1; __puus=cli", cli_cookie)
        plugin_state.store_cookie(COOKIE)
        self.assertTrue(self.api("GET", "/v1/auth").json()["authenticated"])

        self.fixture.api_unauthorized = True
        response = self.api("GET", "/v1/files")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["type"], "login_required")

        self.assertFalse(self.api("GET", "/v1/auth").json()["authenticated"])
        self.assertFalse(plugin_state.credential_path().exists())
        self.assertTrue(cli_cookie.exists())
        self.assertEqual(auth.load_saved_cookie(cli_cookie), "cli=1; __puus=cli")

    def test_injected_cookie_is_forgotten_after_account_401(self):
        self.assertTrue(self.api("GET", "/v1/auth").json()["authenticated"])
        self.assertEqual(self.api("GET", "/v1/files").status_code, 200)
        self.fixture.api_unauthorized = True
        self.assertEqual(self.api("GET", "/v1/files").status_code, 401)
        self.assertFalse(self.api("GET", "/v1/auth").json()["authenticated"])
        self.assertEqual(self.api("GET", "/v1/files").status_code, 401)

    def test_logout_forgets_the_injected_cookie(self):
        self.assertTrue(self.api("GET", "/v1/auth").json()["authenticated"])
        self.assertEqual(self.api("POST", "/v1/auth/logout").json(), {"authenticated": False})
        self.assertFalse(self.api("GET", "/v1/auth").json()["authenticated"])
        self.assertEqual(self.api("GET", "/v1/files").status_code, 401)


class LoginCase(_ServerCase):
    """The login state machine uses the bridge's own credential file."""

    def service_kwargs(self) -> dict:
        self.login = FakeLogin(self.directory)
        return {"transport": self.fixture.transport(), "login_factory": lambda: self.login}

    def test_login_flow_saves_the_plugin_credential(self):
        cli_cookie = auth.credential_path()
        auth.save_cookie("cli=1; __puus=cli", cli_cookie)
        self.assertNotEqual(plugin_state.credential_path(), cli_cookie)
        self.assertFalse(self.api("GET", "/v1/auth").json()["authenticated"])
        refused = self.api("GET", "/v1/files")
        self.assertEqual(refused.status_code, 401)
        self.assertEqual(refused.json()["error"]["type"], "login_required")

        started = self.api("POST", "/v1/auth/login")
        self.assertEqual(started.status_code, 202)
        self.assertTrue(self.login.started.wait(5))
        waiting = self.api("GET", "/v1/auth").json()
        self.assertEqual(waiting["login_state"], "waiting")
        self.assertEqual(waiting["qr_png"], str(self.login.png))

        self.login.release.set()
        self._wait_for_state("success")
        self.assertTrue(self.api("GET", "/v1/auth").json()["authenticated"])
        self.assertEqual(self.api("GET", "/v1/files").status_code, 200)

        self.assertEqual(self.api("POST", "/v1/auth/logout").json(), {"authenticated": False})
        self.assertFalse(plugin_state.credential_path().exists())
        self.assertTrue(cli_cookie.exists())
        self.assertEqual(auth.load_saved_cookie(cli_cookie), "cli=1; __puus=cli")

    def test_failed_login_reports_the_reason(self):
        self.login.fail_with = AuthenticationError("二维码已过期或登录已取消，请重新执行 quark auth login")
        self.api("POST", "/v1/auth/login")
        self.assertTrue(self.login.started.wait(5))
        self.login.release.set()
        self._wait_for_state("failed")
        payload = self.api("GET", "/v1/auth").json()
        self.assertIn("二维码已过期", payload["login_message"])
        self.assertFalse(payload["authenticated"])

    def test_login_post_body_keeps_the_connection_usable(self):
        response = self.api("POST", "/v1/auth/login", body={})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(self.login.started.wait(5))
        status = self.api("GET", "/v1/auth")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.headers["content-type"].startswith("application/json"))
        self.assertEqual(status.json()["login_state"], "waiting")
        self.login.release.set()
        self._wait_for_state("success")

    def _wait_for_state(self, state: str) -> None:
        for _ in range(200):
            if self.api("GET", "/v1/auth").json()["login_state"] == state:
                return
            time.sleep(0.05)
        self.fail(f"login never reached {state}")


class FakeLogin:
    """Stands in for QRLogin: exposes the QR path and waits for the scan."""

    def __init__(self, directory) -> None:
        self.png = Path(directory) / "qrcode.png"
        self.started = threading.Event()
        self.release = threading.Event()
        self.fail_with: Exception | None = None

    def run(self, on_qr_ready=None) -> None:
        self.png.write_bytes(b"\x89PNG\r\n\x1a\n")
        if on_qr_ready is not None:
            on_qr_ready(self.png)
        self.started.set()
        self.release.wait(10)
        if self.fail_with is not None:
            raise self.fail_with
        plugin_state.store_cookie(COOKIE)


# Four full 4 MiB segments plus a short tail: enough to force several
# requests without moving a real 16 MiB object through memory.
VIRTUAL_SIZE = 4 * SEGMENT_LIMIT + 1000


class SegmentCase(_ServerCase):
    """Large media GETs are split into serial ``<=4 MiB`` upstream ranges.

    The origin refuses any single request over 4 MiB (recording it in
    ``over_limit``), so the previous one-request-per-GET code fails these tests
    by design.
    """

    def service_kwargs(self) -> dict:
        return {"transport": self.fixture.transport(), "cookie": COOKIE}

    def setUp(self):
        super().setUp()
        self.fixture.records.append(record("big", "大剧集.mp4", "root-dir", size=VIRTUAL_SIZE))
        self.fixture.virtual["big"] = VIRTUAL_SIZE
        self.fixture.segment_limit = SEGMENT_LIMIT

    # -- helpers ---------------------------------------------------------

    def expected_segments(self, start: int, end: int) -> list[tuple[int, int]]:
        segments, position = [], start
        while position <= end:
            stop = min(position + SEGMENT_LIMIT - 1, end)
            segments.append((position, stop))
            position = stop + 1
        return segments

    def upstream_ranges(self) -> list[tuple[int, int]]:
        ranges = []
        for request in self.fixture.origin_requests():
            header = request.headers.get("range")
            self.assertIsNotNone(header, "媒体请求必须带显式上游 Range")
            first, _, last = header[6:].partition("-")
            ranges.append((int(first), int(last)))
        return ranges

    def drain(self, response, start: int) -> int:
        """Compare a streamed body with the virtual object without buffering it."""
        seen = 0
        for chunk in response.iter_bytes():
            self.assertEqual(chunk, virtual_slice(start + seen, len(chunk)))
            seen += len(chunk)
        return seen

    def fetch(self, url: str, **kwargs):
        response = self.client.get(self.base + url, **kwargs)
        response.raise_for_status()
        return response

    # -- splitting -------------------------------------------------------

    def test_no_range_large_object_keeps_200_and_splits_into_bounded_segments(self):
        url = self.play("big")["url"]
        with self.client.stream("GET", self.base + url) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-length"], str(VIRTUAL_SIZE))
            self.assertEqual(response.headers["accept-ranges"], "bytes")
            self.assertNotIn("content-range", response.headers)
            self.assertEqual(self.drain(response, 0), VIRTUAL_SIZE)
        self.assertEqual(self.upstream_ranges(), self.expected_segments(0, VIRTUAL_SIZE - 1))
        self.assertEqual(self.fixture.over_limit, [])

    def test_open_ended_range_keeps_206_and_splits_within_the_request(self):
        url = self.play("big")["url"]
        start = SEGMENT_LIMIT - 100
        with self.client.stream("GET", self.base + url, headers={"Range": f"bytes={start}-"}) as response:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.headers["content-range"], f"bytes {start}-{VIRTUAL_SIZE - 1}/{VIRTUAL_SIZE}")
            self.assertEqual(response.headers["content-length"], str(VIRTUAL_SIZE - start))
            self.assertEqual(self.drain(response, start), VIRTUAL_SIZE - start)
        self.assertEqual(self.upstream_ranges(), self.expected_segments(start, VIRTUAL_SIZE - 1))
        self.assertEqual(self.fixture.over_limit, [])

    def test_cross_segment_bounded_range_is_split_at_boundaries(self):
        url = self.play("big")["url"]
        start, end = SEGMENT_LIMIT - 10, 2 * SEGMENT_LIMIT + 10
        response = self.fetch(url, headers={"Range": f"bytes={start}-{end}"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], f"bytes {start}-{end}/{VIRTUAL_SIZE}")
        self.assertEqual(response.content, virtual_slice(start, end - start + 1))
        self.assertEqual(self.upstream_ranges(), [(SEGMENT_LIMIT - 10, 2 * SEGMENT_LIMIT - 11),
                                                  (2 * SEGMENT_LIMIT - 10, end)])

    def test_large_suffix_range_resolves_to_the_tail(self):
        url = self.play("big")["url"]
        wanted = SEGMENT_LIMIT + 1000
        start = VIRTUAL_SIZE - wanted
        response = self.fetch(url, headers={"Range": f"bytes=-{wanted}"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], f"bytes {start}-{VIRTUAL_SIZE - 1}/{VIRTUAL_SIZE}")
        self.assertEqual(response.content, virtual_slice(start, wanted))
        self.assertEqual(self.upstream_ranges(), self.expected_segments(start, VIRTUAL_SIZE - 1))

    def test_small_requests_stay_a_single_passthrough_range(self):
        url = self.play("big")["url"]
        exact = self.fetch(url, headers={"Range": f"bytes=0-{SEGMENT_LIMIT - 1}"})
        self.assertEqual(exact.status_code, 206)
        self.assertEqual(self.upstream_ranges(), [(0, SEGMENT_LIMIT - 1)])

    def test_one_byte_over_the_boundary_splits(self):
        url = self.play("big")["url"]
        response = self.fetch(url, headers={"Range": f"bytes=0-{SEGMENT_LIMIT}"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.content), SEGMENT_LIMIT + 1)
        self.assertEqual(self.upstream_ranges(), [(0, SEGMENT_LIMIT - 1), (SEGMENT_LIMIT, SEGMENT_LIMIT)])

    # -- aborting mid-stream ---------------------------------------------

    def test_changed_etag_on_a_later_segment_stops_concatenation(self):
        self.fixture.origin_etag = '"v1"'
        self.fixture.switch_etag_at = 3
        self.fixture.etag_after = '"v2"'
        url = self.play("big")["url"]
        with self.assertRaises(httpx.HTTPError):
            with self.client.stream("GET", self.base + url) as response:
                self.assertEqual(response.status_code, 200)
                for _ in response.iter_bytes():
                    pass
        self.assertEqual(len(self.fixture.origin_requests()), 3)

    def test_changed_etag_before_the_first_segment_is_409(self):
        self.fixture.origin_etag = '"v1"'
        url = self.play("big")["url"]
        self.assertEqual(self.fetch(url, headers={"Range": "bytes=0-99"}).status_code, 206)
        self.fixture.switch_etag_at = self.fixture.origin_ordinal + 1
        self.fixture.etag_after = '"v2"'
        response = self.stream(url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["type"], "media_changed")

    def test_truncated_later_segment_stops_further_fetches(self):
        self.fixture.truncate_at = 2
        url = self.play("big")["url"]
        with self.assertRaises(httpx.HTTPError):
            with self.client.stream("GET", self.base + url) as response:
                self.assertEqual(response.status_code, 200)
                for _ in response.iter_bytes():
                    pass
        self.assertEqual(len(self.fixture.origin_requests()), 2)

    def test_player_disconnect_stops_fetching_further_segments(self):
        url = self.play("big")["url"]
        with self.client.stream("GET", self.base + url) as response:
            self.assertEqual(response.status_code, 200)
            next(response.iter_bytes(1024))
        self.assertLess(self._settled_origin_count(), len(self.expected_segments(0, VIRTUAL_SIZE - 1)))

    def _settled_origin_count(self) -> int:
        current, stable = len(self.fixture.origin_requests()), 0
        for _ in range(200):
            time.sleep(0.02)
            latest = len(self.fixture.origin_requests())
            if latest == current:
                stable += 1
                if stable >= 10:
                    break
            else:
                current, stable = latest, 0
        return current


class SearchCase(_ServerCase):
    """Remote-wide name search reduced to directories and videos."""

    def service_kwargs(self) -> dict:
        return {"transport": self.fixture.transport(), "cookie": COOKIE}

    def search_request(self) -> httpx.Request:
        return next(
            request for request in self.fixture.requests if request.url.path.endswith("/file/search")
        )

    def test_search_sends_the_verified_parameters_and_keeps_dirs_and_videos(self):
        payload = self.api("GET", "/v1/search?q=%E7%AC%AC").json()
        self.assertEqual(payload["page"], 1)
        self.assertFalse(payload["has_more"])
        self.assertEqual(
            [item["name"] for item in payload["items"]],
            ["第10集.mp4", "第2集.mp4", "第9集.mkv"],
        )
        request = self.search_request()
        self.assertEqual(request.url.params["q"], "第")
        self.assertEqual(request.url.params["_page"], "1")
        self.assertEqual(request.url.params["_size"], "50")
        self.assertEqual(request.url.params["_fetch_total"], "1")
        self.assertEqual(request.url.params["_sort"], "file_type:asc,file_name:asc")
        names = {name.lower() for name in request.url.params.keys()}
        self.assertNotIn("highlight", names)
        self.assertNotIn("_highlight", names)

    def test_search_hides_other_file_types_but_keeps_the_remote_total(self):
        self.fixture.records.append(record("s-dir", "课程目录", directory=True))
        self.fixture.records.append(record("s-video", "课程01.mp4"))
        self.fixture.records.append(record("s-notes", "课程说明.txt", size=8))
        payload = self.api("GET", "/v1/search?q=课程").json()
        self.assertEqual([item["name"] for item in payload["items"]], ["课程目录", "课程01.mp4"])
        self.assertEqual([item["is_dir"] for item in payload["items"]], [True, False])
        self.assertFalse(payload["has_more"])

    def test_search_pages_on_the_remote_total(self):
        for index in range(60):
            self.fixture.records.append(record(f"p{index:03d}", f"批量{index:03d}.mp4"))
        first = self.api("GET", "/v1/search?q=批量&size=50").json()
        self.assertEqual(len(first["items"]), 50)
        self.assertTrue(first["has_more"])
        second = self.api("GET", "/v1/search?q=批量&size=50&page=2").json()
        self.assertEqual(second["page"], 2)
        self.assertEqual(len(second["items"]), 10)
        self.assertFalse(second["has_more"])

    def test_search_validates_the_query_and_paging(self):
        self.assertEqual(self.api("GET", "/v1/search").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/search?q=%20").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/search?q=a&page=0").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/search?q=a&size=101").status_code, 400)
        self.assertEqual(self.api("POST", "/v1/search?q=a").status_code, 405)

    def test_search_rejects_an_invalid_remote_record(self):
        self.fixture.records.append(record("bad/fid", "坏记录.mp4"))
        response = self.api("GET", "/v1/search?q=坏")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["type"], "remote_error")


class FavoritesCase(_ServerCase):
    """Favorite directories: local JSON only, never a remote write."""

    def service_kwargs(self) -> dict:
        return {"transport": self.fixture.transport(), "cookie": COOKIE}

    def test_add_verifies_a_real_directory_and_returns_the_collection(self):
        response = self.api("POST", "/v1/favorites", body={"fid": "root-dir"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"items": [{"fid": "root-dir", "name": "剧集", "is_dir": True, "size": 0, "last_episode": None}]},
        )
        self.assertEqual(self.api("GET", "/v1/favorites").json(), response.json())

    def test_add_is_idempotent_and_rejects_non_directories(self):
        self.api("POST", "/v1/favorites", body={"fid": "root-dir"})
        again = self.api("POST", "/v1/favorites", body={"fid": "root-dir"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(again.json()["items"]), 1)
        self.assertEqual(self.api("POST", "/v1/favorites", body={"fid": "v2"}).status_code, 400)
        self.assertEqual(self.api("POST", "/v1/favorites", body={"fid": "0"}).status_code, 400)
        self.assertEqual(self.api("POST", "/v1/favorites", body={}).status_code, 400)
        self.assertEqual(self.api("POST", "/v1/favorites", content=b"[]").status_code, 400)
        self.assertEqual(self.api("POST", "/v1/favorites", body={"fid": "missing"}).status_code, 502)

    def test_favorites_are_listed_newest_first(self):
        self.api("POST", "/v1/favorites", body={"fid": "root-dir"})
        self.fixture.records.append(record("second-dir", "第二目录", directory=True))
        self.api("POST", "/v1/favorites", body={"fid": "second-dir"})
        payload = self.api("GET", "/v1/favorites").json()
        self.assertEqual([item["fid"] for item in payload["items"]], ["second-dir", "root-dir"])

    def test_remove_is_idempotent_and_needs_no_remote_directory(self):
        self.api("POST", "/v1/favorites", body={"fid": "root-dir"})
        self.fixture.records = [row for row in self.fixture.records if row["fid"] != "root-dir"]
        removed = self.api("POST", "/v1/favorites/remove", body={"fid": "root-dir"})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json(), {"items": []})
        self.assertEqual(
            self.api("POST", "/v1/favorites/remove", body={"fid": "root-dir"}).json(),
            {"items": []},
        )
        self.assertEqual(
            self.api("POST", "/v1/favorites/remove", body={"fid": "never"}).json(),
            {"items": []},
        )
        self.assertEqual(self.api("POST", "/v1/favorites/remove", body={}).status_code, 400)

    def test_favorites_never_write_to_the_remote(self):
        self.api("POST", "/v1/favorites", body={"fid": "root-dir"})
        self.api("POST", "/v1/favorites/remove", body={"fid": "root-dir"})
        self.assertEqual([request.method for request in self.fixture.requests if request.method != "GET"], [])

    def test_corrupt_library_reports_a_local_error_and_keeps_the_file(self):
        path = self.service.library.path
        write_json(path, {"version": 1, "favorites": [], "recent": []})
        path.write_text("{broken", encoding="utf-8")
        response = self.api("GET", "/v1/favorites")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["type"], "local_io_error")
        self.assertEqual(self.api("GET", "/v1/recent").status_code, 500)
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_explicit_logout_clears_favorites_and_recent(self):
        cli_cookie = auth.credential_path()
        auth.save_cookie("cli=1; __puus=cli", cli_cookie)
        self.api("POST", "/v1/favorites", body={"fid": "root-dir"})
        self.stream(self.play("v2")["url"])
        # The bridge records the play right after the first chunk leaves the
        # socket, so the disk write can land just after the client has finished
        # reading the body. Wait (bounded) for that persistence to become
        # observable, otherwise logout wipes a record the bridge never stored.
        deadline = time.monotonic() + 2.0
        while not self.service.library.recent() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.service.library.recent()), 1)

        self.assertEqual(self.api("POST", "/v1/auth/logout").json(), {"authenticated": False})
        self.assertEqual(self.service.library.favorites(), [])
        self.assertEqual(self.service.library.recent(), [])
        self.assertFalse(self.service.library.path.exists())
        self.assertTrue(cli_cookie.exists())

    def test_logout_reports_a_clear_failure_and_keeps_the_records(self):
        plugin_state.store_cookie(COOKIE)
        self.api("POST", "/v1/favorites", body={"fid": "root-dir"})
        library_path = self.service.library.path
        self.assertTrue(library_path.exists())
        real_unlink = Path.unlink

        def refuse(target, *args, **kwargs):
            if target == library_path:
                raise PermissionError("sharing violation")
            return real_unlink(target, *args, **kwargs)

        with mock.patch.object(Path, "unlink", refuse):
            response = self.api("POST", "/v1/auth/logout")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["type"], "local_io_error")
        self.assertFalse(self.api("GET", "/v1/auth").json()["authenticated"])
        self.assertEqual(self.api("GET", "/v1/files").status_code, 401)
        self.assertFalse(plugin_state.credential_path().exists())
        self.assertEqual([entry["fid"] for entry in self.service.library.favorites()], ["root-dir"])

    def test_credential_expiry_keeps_favorites_and_recent(self):
        self.api("POST", "/v1/favorites", body={"fid": "root-dir"})
        self.stream(self.play("v2")["url"])

        self.fixture.api_unauthorized = True
        self.assertEqual(self.api("GET", "/v1/files").status_code, 401)
        self.assertEqual(self.api("GET", "/v1/favorites").status_code, 401)
        self.assertEqual([item["fid"] for item in self.service.library.favorites()], ["root-dir"])
        self.assertEqual([item["fid"] for item in self.service.library.recent()], ["v2"])
        self.assertTrue(self.service.library.path.exists())


class RecentCase(_ServerCase):
    """A media GET is recorded once as soon as it starts streaming."""

    def service_kwargs(self) -> dict:
        return {"transport": self.fixture.transport(), "cookie": COOKIE}

    def setUp(self):
        super().setUp()
        self.fixture.records.append(record("big", "大剧集.mp4", "root-dir", size=VIRTUAL_SIZE))
        self.fixture.virtual["big"] = VIRTUAL_SIZE
        self.fixture.segment_limit = SEGMENT_LIMIT

    def test_play_and_head_are_not_recorded_but_a_full_get_is(self):
        self.assertEqual(self.api("GET", "/v1/recent").json(), {"items": []})
        url = self.play("v2")["url"]
        self.assertEqual(self.api("GET", "/v1/recent").json(), {"items": []})
        self.assertEqual(self.client.head(self.base + url).status_code, 200)
        self.assertEqual(self.api("GET", "/v1/recent").json(), {"items": []})

        self.assertEqual(self.stream(url).status_code, 200)
        payload = self.api("GET", "/v1/recent").json()
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["fid"], "v2")
        self.assertEqual(item["name"], "第2集.mp4")
        self.assertEqual(item["is_dir"], False)
        self.assertEqual(item["size"], len(VIDEO_BYTES))
        self.assertIsInstance(item["played_at"], int)

    def test_a_range_get_records_once_and_repeats_move_to_the_front(self):
        self.assertEqual(self.stream(self.play("v2")["url"], headers={"Range": "bytes=0-9"}).status_code, 206)
        self.assertEqual(self.stream(self.play("v9")["url"]).status_code, 200)
        self.assertEqual(
            [item["fid"] for item in self.api("GET", "/v1/recent").json()["items"]],
            ["v9", "v2"],
        )
        self.assertEqual(self.stream(self.play("v2")["url"]).status_code, 200)
        self.assertEqual(
            [item["fid"] for item in self.api("GET", "/v1/recent").json()["items"]],
            ["v2", "v9"],
        )

    def test_a_segmented_large_get_records_exactly_once(self):
        url = self.play("big")["url"]
        with self.client.stream("GET", self.base + url) as response:
            self.assertEqual(response.status_code, 200)
            forwarded = 0
            for chunk in response.iter_bytes():
                forwarded += len(chunk)
            self.assertEqual(forwarded, VIRTUAL_SIZE)
        self.assertGreater(len(self.fixture.origin_requests()), 1)
        self.assertEqual([item["fid"] for item in self.api("GET", "/v1/recent").json()["items"]], ["big"])

    def test_a_started_stream_is_recorded_once_before_the_body_finishes(self):
        calls: list[str] = []
        library = self.service.library
        original = library.record_recent

        def spy(fid, name, size, parent=""):
            calls.append(fid)
            return original(fid, name, size, parent)

        library.record_recent = spy

        url = self.play("big")["url"]
        with self.client.stream("GET", self.base + url) as response:
            self.assertEqual(response.status_code, 200)
            received = 0
            for chunk in response.iter_bytes(16 * 1024):
                received += len(chunk)
                if received >= 128 * 1024:
                    break
            # A >4 MiB title spans several segments; because the >64 KiB read past
            # the first chunk forces a second segment write, the record has already
            # happened while the media GET is still open.
            self.assertGreaterEqual(received, 128 * 1024)
            payload = self.api("GET", "/v1/recent").json()
            self.assertEqual([item["fid"] for item in payload["items"]], ["big"])
            self.assertEqual(calls, ["big"])

        # Closing the player connection must not drop the already-started entry.
        self.assertEqual([item["fid"] for item in self.api("GET", "/v1/recent").json()["items"]], ["big"])
        self.assertEqual(calls, ["big"])

    def test_failures_without_media_bytes_are_not_recorded(self):
        url = self.play("big")["url"]
        self.assertEqual(self.client.head(self.base + url).status_code, 200)
        self.assertEqual(self.api("GET", "/v1/recent").json(), {"items": []})

        self.fixture.origin_status = 500
        self.assertEqual(self.stream(url).status_code, 502)
        self.assertEqual(self.api("GET", "/v1/recent").json(), {"items": []})

        self.fixture.origin_status = None
        unsatisfiable = self.stream(url, headers={"Range": f"bytes={VIRTUAL_SIZE}-"})
        self.assertEqual(unsatisfiable.status_code, 416)
        self.assertEqual(self.api("GET", "/v1/recent").json(), {"items": []})

    def test_a_truncated_response_after_the_first_chunk_is_recorded(self):
        url = self.play("big")["url"]
        self.fixture.truncate_at = 1
        with self.assertRaises(httpx.HTTPError):
            with self.client.stream("GET", self.base + url) as response:
                self.assertEqual(response.status_code, 200)
                for _ in response.iter_bytes():
                    pass
        self.assertEqual([item["fid"] for item in self.api("GET", "/v1/recent").json()["items"]], ["big"])

    def test_a_corrupt_library_aborts_a_started_stream_and_keeps_the_file(self):
        path = self.service.library.path
        write_json(path, {"version": 1, "favorites": [], "recent": []})
        path.write_text("{broken", encoding="utf-8")

        url = self.play("big")["url"]
        with self.assertRaises(httpx.HTTPError):
            with self.client.stream("GET", self.base + url) as response:
                self.assertEqual(response.status_code, 200)
                for _ in response.iter_bytes():
                    pass

        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")
        broken = self.api("GET", "/v1/recent")
        self.assertEqual(broken.status_code, 500)
        self.assertEqual(broken.json()["error"]["type"], "local_io_error")
        self.assertEqual(self.api("GET", "/v1/favorites").status_code, 500)

    def test_clear_recent_answers_with_an_empty_collection(self):
        self.stream(self.play("v2")["url"])
        cleared = self.api("POST", "/v1/recent/clear", body={})
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json(), {"items": []})
        self.assertEqual(self.api("GET", "/v1/recent").json(), {"items": []})
        self.assertEqual(self.api("POST", "/v1/recent/clear", body={}).json(), {"items": []})


class SeriesCase(_ServerCase):
    """Continue/series/mark views: local state, one selected-directory listing."""

    SIZE = len(VIDEO_BYTES)

    def service_kwargs(self) -> dict:
        return {"transport": self.fixture.transport(), "cookie": COOKIE}

    def save_document(self, recent=(), marks=(), favorites=()):
        write_json(self.service.library.path, {
            "version": 3,
            "favorites": list(favorites),
            "recent": list(recent),
            "marks": list(marks),
        })

    def recent_row(self, fid, name, size=None, *, parent="root-dir", played_at=10,
                   position_ms=0, duration_ms=0, progress_at=0):
        return {
            "fid": fid, "name": name, "size": self.SIZE if size is None else size,
            "played_at": played_at, "position_ms": position_ms, "duration_ms": duration_ms,
            "progress_at": progress_at, "parent": parent,
        }

    def mark_row(self, fid, name, size=None, watched_at=10):
        return {"fid": fid, "name": name, "size": self.SIZE if size is None else size,
                "watched_at": watched_at}

    def test_continue_lists_only_resumable_unfinished_videos(self):
        self.save_document(recent=[
            self.recent_row("v2", "第2集.mp4", position_ms=60_000, duration_ms=1_800_000, progress_at=30),
            self.recent_row("v9", "第9集.mkv", position_ms=10_000, duration_ms=1_800_000, progress_at=20),
            self.recent_row("v10", "第10集.mp4", position_ms=1_790_000, duration_ms=1_800_000, progress_at=10),
            self.recent_row("zero", "未播.mp4", position_ms=0, duration_ms=0, progress_at=5),
        ])
        before = len(self.fixture.requests)
        payload = self.api("GET", "/v1/continue").json()
        self.assertEqual([item["fid"] for item in payload["items"]], ["v2"])
        item = payload["items"][0]
        self.assertEqual(item["size"], self.SIZE)
        self.assertFalse(item["watched"])
        self.assertIn("已看 3%", item["display"])
        self.assertEqual(len(self.fixture.requests), before)

    def test_continue_excludes_marks_and_orders_by_latest_progress(self):
        self.save_document(
            recent=[
                self.recent_row("v2", "第2集.mp4", position_ms=60_000, duration_ms=1_800_000, progress_at=10),
                self.recent_row("v9", "第9集.mkv", position_ms=90_000, duration_ms=1_800_000, progress_at=20),
                self.recent_row("v10", "第10集.mp4", position_ms=120_000, duration_ms=1_800_000, progress_at=30),
            ],
            marks=[self.mark_row("v10", "第10集.mp4")],
        )
        payload = self.api("GET", "/v1/continue").json()
        self.assertEqual([item["fid"] for item in payload["items"]], ["v9", "v2"])

    def test_recent_view_counts_manual_and_real_completion_as_watched(self):
        self.save_document(
            recent=[
                self.recent_row("v2", "第2集.mp4", position_ms=1_795_000, duration_ms=1_800_000, progress_at=5),
                self.recent_row("v9", "第9集.mkv", position_ms=60_000, duration_ms=1_800_000, progress_at=6),
                self.recent_row("v10", "第10集.mp4", position_ms=0, duration_ms=0, progress_at=0),
            ],
            marks=[self.mark_row("v10", "第10集.mp4")],
        )
        items = {item["fid"]: item for item in self.api("GET", "/v1/recent").json()["items"]}
        self.assertTrue(items["v2"]["watched"])
        self.assertEqual(items["v2"]["display"], "第2集.mp4 · 已看")
        self.assertFalse(items["v9"]["watched"])
        self.assertTrue(items["v10"]["watched"])

    def test_series_uses_the_listing_name_size_and_parent(self):
        # A stored record carries an old name and an unknown parent; the listing
        # is the authority for the display name and the directory association.
        self.save_document(recent=[{
            "fid": "v2", "name": "旧名字.mp4", "size": self.SIZE, "played_at": 1,
            "position_ms": 60_000, "duration_ms": 1_800_000, "progress_at": 9, "parent": "",
        }])
        payload = self.api("GET", "/v1/series?parent=root-dir").json()
        self.assertEqual(payload["parent"], "root-dir")
        by_fid = {episode["fid"]: episode for episode in payload["episodes"]}
        self.assertEqual(by_fid["v2"]["name"], "第2集.mp4")
        self.assertEqual(by_fid["v2"]["size"], self.SIZE)
        self.assertEqual(by_fid["v2"]["position_ms"], 60_000)
        self.assertIn("已看 3%", by_fid["v2"]["display"])

    def test_series_ignores_a_stale_size_record(self):
        self.save_document(recent=[{
            "fid": "v9", "name": "第9集.mkv", "size": self.SIZE + 1, "played_at": 1,
            "position_ms": 1_790_000, "duration_ms": 1_800_000, "progress_at": 9, "parent": "root-dir",
        }])
        episodes = {episode["fid"]: episode for episode in
                    self.api("GET", "/v1/series?parent=root-dir").json()["episodes"]}
        self.assertEqual(episodes["v9"]["position_ms"], 0)
        self.assertFalse(episodes["v9"]["watched"])

    def test_series_lists_the_directory_in_natural_order(self):
        before = len(self.fixture.requests)
        payload = self.api("GET", "/v1/series?parent=root-dir").json()
        self.assertEqual([episode["fid"] for episode in payload["episodes"]], ["v2", "v9", "v10"])
        self.assertEqual(payload["total"], 3)
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["continue"]["fid"], "v2")
        added = self.fixture.requests[before:]
        self.assertEqual(len(added), 1)
        self.assertTrue(added[0].url.path.endswith("/file/sort"))

    def test_series_continues_the_latest_unfinished_episode(self):
        self.save_document(recent=[
            self.recent_row("v2", "第2集.mp4", position_ms=60_000, duration_ms=1_800_000, progress_at=10),
            self.recent_row("v10", "第10集.mp4", position_ms=90_000, duration_ms=1_800_000, progress_at=20),
        ])
        payload = self.api("GET", "/v1/series?parent=root-dir").json()
        self.assertEqual(payload["continue"]["fid"], "v10")
        self.assertIn("已看 5%", payload["continue"]["display"])
        self.assertEqual(payload["watched"], 0)

    def test_series_all_manually_watched_reports_complete(self):
        self.save_document(marks=[
            self.mark_row("v2", "第2集.mp4"),
            self.mark_row("v9", "第9集.mkv"),
            self.mark_row("v10", "第10集.mp4"),
        ])
        payload = self.api("GET", "/v1/series?parent=root-dir").json()
        self.assertTrue(payload["complete"])
        self.assertIsNone(payload["continue"])
        self.assertEqual(payload["watched"], 3)

    def test_series_all_completed_by_real_progress_reports_complete(self):
        self.save_document(recent=[
            self.recent_row(fid, name, position_ms=1_795_000, duration_ms=1_800_000, progress_at=index)
            for index, (fid, name) in enumerate(
                (("v2", "第2集.mp4"), ("v9", "第9集.mkv"), ("v10", "第10集.mp4")), start=1
            )
        ])
        payload = self.api("GET", "/v1/series?parent=root-dir").json()
        self.assertTrue(payload["complete"])
        self.assertIsNone(payload["continue"])
        self.assertEqual(payload["watched"], 3)

    def test_series_directory_without_videos_is_never_complete(self):
        # An empty directory, or one holding only non-video files, has no
        # episode to finish: it must not look entirely watched, which the
        # extension would otherwise turn into a misleading 已全部看完 row.
        self.fixture.records.extend([
            record("art-dir", "图片", directory=True),
            record("only-poster", "封面.jpg", "art-dir", size=32),
        ])
        for parent in ("empty-dir", "art-dir"):
            with self.subTest(parent=parent):
                payload = self.api("GET", f"/v1/series?parent={parent}").json()
                self.assertEqual(payload["parent"], parent)
                self.assertEqual(payload["total"], 0)
                self.assertEqual(payload["watched"], 0)
                self.assertEqual(payload["episodes"], [])
                self.assertFalse(payload["complete"])
                self.assertIsNone(payload["continue"])

    def test_series_ignores_records_from_another_directory(self):
        self.save_document(recent=[
            self.recent_row("other", "别的.mp4", parent="second-dir",
                            position_ms=60_000, duration_ms=1_800_000, progress_at=99),
        ])
        payload = self.api("GET", "/v1/series?parent=root-dir").json()
        self.assertEqual([episode["fid"] for episode in payload["episodes"]], ["v2", "v9", "v10"])
        self.assertEqual(payload["continue"]["fid"], "v2")

    def test_series_rejects_a_missing_parent(self):
        self.assertEqual(self.api("GET", "/v1/series").status_code, 400)
        self.assertEqual(self.api("POST", "/v1/series?parent=root-dir").status_code, 405)

    def test_favorite_shows_the_last_episode_from_its_own_directory(self):
        self.save_document(
            favorites=[{"fid": "root-dir", "name": "剧集", "added_at": 1}],
            recent=[
                self.recent_row("v2", "第2集.mp4", played_at=7,
                                position_ms=60_000, duration_ms=1_800_000, progress_at=5),
                self.recent_row("other", "别的.mp4", parent="second-dir", played_at=8,
                                position_ms=90_000, duration_ms=1_800_000, progress_at=9),
            ],
        )
        item = self.api("GET", "/v1/favorites").json()["items"][0]
        self.assertEqual(item["last_episode"]["fid"], "v2")
        self.assertIn("已看 3%", item["last_episode"]["display"])

    def test_mark_verifies_the_directory_video_and_is_idempotent(self):
        first = self.api("POST", "/v1/mark", body={"fid": "v2", "parent": "root-dir", "watched": True})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), {"fid": "v2", "watched": True})
        self.assertTrue(self.service.library.is_watched("v2", self.SIZE))
        before = self.service.library.path.read_text(encoding="utf-8")
        again = self.api("POST", "/v1/mark", body={"fid": "v2", "parent": "root-dir", "watched": True})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.service.library.path.read_text(encoding="utf-8"), before)

        self.assertEqual(
            self.api("POST", "/v1/mark", body={"fid": "root-dir", "parent": "root-dir", "watched": True}).status_code,
            400,
        )
        self.assertEqual(
            self.api("POST", "/v1/mark", body={"fid": "v2", "parent": "empty-dir", "watched": True}).status_code,
            400,
        )
        self.assertEqual(
            self.api("POST", "/v1/mark", body={"fid": "v2", "parent": "root-dir", "watched": "yes"}).status_code,
            400,
        )
        self.assertEqual(self.api("POST", "/v1/mark", body={"fid": "v2", "parent": "root-dir"}).status_code, 400)

    def test_mark_unwatched_resets_progress_and_allows_a_future_write(self):
        library = self.service.library
        library.record_recent("v2", "第2集.mp4", self.SIZE, "root-dir")
        self.assertTrue(library.save_progress("v2", self.SIZE, 60_000, 1_800_000))
        self.api("POST", "/v1/mark", body={"fid": "v2", "parent": "root-dir", "watched": True})
        self.assertIsNone(library.progress("v2", self.SIZE))
        self.assertFalse(library.save_progress("v2", self.SIZE, 65_000, 1_800_000))

        cleared = self.api("POST", "/v1/mark", body={"fid": "v2", "parent": "root-dir", "watched": False})
        self.assertEqual(cleared.json(), {"fid": "v2", "watched": False})
        self.assertFalse(library.is_watched("v2", self.SIZE))
        self.assertIsNone(library.progress("v2", self.SIZE))
        self.assertTrue(library.save_progress("v2", self.SIZE, 70_000, 1_800_000))

    def test_local_views_add_no_upstream_request(self):
        self.save_document(recent=[
            self.recent_row("v2", "第2集.mp4", position_ms=60_000, duration_ms=1_800_000, progress_at=1)
        ])
        before = len(self.fixture.requests)
        self.api("GET", "/v1/continue")
        self.api("GET", "/v1/recent")
        self.api("GET", "/v1/favorites")
        self.assertEqual(len(self.fixture.requests), before)


class PersistenceCase(_ServerCase):
    """The private document is read back by a fresh service instance."""

    def service_kwargs(self) -> dict:
        return {"transport": self.fixture.transport(), "cookie": COOKIE}

    def test_favorites_and_recent_survive_a_service_restart(self):
        self.api("POST", "/v1/favorites", body={"fid": "root-dir"})
        self.assertEqual(self.stream(self.play("v2")["url"]).status_code, 200)

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)
        self.service.close()

        self.service = Service(transport=self.fixture.transport(), cookie=COOKIE)
        self.server = self.service.build_server()
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.service.port}"

        self.assertEqual(
            [item["fid"] for item in self.api("GET", "/v1/favorites").json()["items"]],
            ["root-dir"],
        )
        self.assertEqual(
            [item["fid"] for item in self.api("GET", "/v1/recent").json()["items"]],
            ["v2"],
        )


class AuthGateCase(_ServerCase):
    """Library endpoints follow the bridge's existing token plus login gate."""

    def service_kwargs(self) -> dict:
        return {"transport": self.fixture.transport()}

    def test_library_endpoints_require_the_token(self):
        for path in ("/v1/search?q=a", "/v1/favorites", "/v1/recent",
                     "/v1/continue", "/v1/series?parent=root-dir"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(self.base + path).status_code, 401)

    def test_library_endpoints_require_a_login(self):
        requests = (
            ("GET", "/v1/search?q=a"),
            ("GET", "/v1/favorites"),
            ("GET", "/v1/recent"),
            ("GET", "/v1/continue"),
            ("GET", "/v1/series?parent=root-dir"),
            ("POST", "/v1/favorites"),
            ("POST", "/v1/favorites/remove"),
            ("POST", "/v1/recent/clear"),
            ("POST", "/v1/mark"),
        )
        for method, path in requests:
            with self.subTest(path=path):
                response = self.api(method, path, body={"fid": "root-dir"} if method == "POST" else None)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["type"], "login_required")


if __name__ == "__main__":
    unittest.main()
