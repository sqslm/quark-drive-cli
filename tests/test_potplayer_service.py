"""Behavioural tests for the PotPlayer bridge over real loopback HTTP."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx

from quark_cli import auth
from quark_cli.errors import AuthenticationError
from quark_cli.potplayer import plugin_state
from quark_cli.potplayer.service import Service
from quark_cli.potplayer.stream import Media, Sessions

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
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.fixture.download_calls, 2)

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
        self.assertEqual(self.stream(payload["url"]).status_code, 502)
        self.assertTrue(self.api("GET", "/v1/auth").json()["authenticated"])
        self.assertEqual(self.api("GET", "/v1/files").status_code, 200)

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


if __name__ == "__main__":
    unittest.main()
