"""Integration tests for the shared request guard over real loopback HTTP.

Each test drives one bridge instance on an ephemeral port with an in-memory
``httpx.MockTransport`` standing in for the account API and the signed origin;
no real Quark request is made. These cover the seams that the guard unit tests
do not exercise:

* an API 429 seen through ``/v1/files`` pauses locally while the saved plugin
  Cookie, ``/v1/auth`` and the purely-local library endpoints stay usable;
* a login started under an active cooldown is refused without any QR window;
* a media-origin and a subtitle-origin 429 stop the next request without a
  signed-URL refetch and preserve the plugin Cookie.

The account/origin stand-in (``DriveFixture``) comes from ``potplayer_fixture``;
each test starts its own service so it can inject its own guard and transport.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

from quark_cli.potplayer import plugin_state
from quark_cli.potplayer.accounts import Accounts
from quark_cli.potplayer.service import Service
from quark_cli.request_guard import REASON_RATE_LIMITED, RequestGuard

from potplayer_fixture import COOKIE, DriveFixture, record


class _ThrottledApi(DriveFixture):
    """A fixture whose account API answers every call with 429 + Retry-After."""

    retry_after = "120"

    def _api(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": self.retry_after},
            json={"status": 429, "code": 429, "message": "请求过于频繁"},
            request=request,
        )


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected upstream call: {request.url}")


class RequestProtectionIntegrationTests(unittest.TestCase):
    """Four cooldown behaviours proven end to end through the local bridge."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="quark-request-protection-"))
        self.previous_config = os.environ.get("QUARK_CONFIG_DIR")
        os.environ["QUARK_CONFIG_DIR"] = str(self.directory)
        self.client = httpx.Client(timeout=10.0)
        self.service: Service | None = None
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        self.client.close()
        if self.service is not None:
            self.server.shutdown()
            self.server.server_close()
            self.service.close()
            self.thread.join(10)
        if self.previous_config is None:
            os.environ.pop("QUARK_CONFIG_DIR", None)
        else:
            os.environ["QUARK_CONFIG_DIR"] = self.previous_config
        shutil.rmtree(self.directory, ignore_errors=True)

    def _start(self, **kwargs) -> None:
        self.service = Service(**kwargs)
        self.server = self.service.build_server()
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.service.port}"

    def _api(self, method: str, path: str, *, body=None) -> httpx.Response:
        return self.client.request(
            method,
            self.base + path,
            json=body,
            headers={"Authorization": f"Bearer {self.service.token}"},
        )

    def _upstream_split(self, fixture: DriveFixture) -> tuple[int, int]:
        """Return ``(account_api_calls, origin_calls)`` recorded by the fixture."""
        origins = len(fixture.origin_requests())
        return len(fixture.requests) - origins, origins

    def _save_plugin_cookie(self) -> Path:
        plugin_state.store_cookie(COOKIE)
        credential = plugin_state.credential_path()
        self.assertTrue(credential.exists())
        return credential

    def test_api_429_pauses_locally_and_keeps_the_plugin_cookie(self):
        self._save_plugin_cookie()
        fixture = _ThrottledApi()
        self._start(transport=fixture.transport(), guard=RequestGuard.memory())

        refused = self._api("GET", "/v1/files")
        self.assertEqual(refused.status_code, 429, refused.text)
        self.assertEqual(refused.json()["error"]["type"], "rate_limited")
        self.assertGreaterEqual(int(refused.headers["Retry-After"]), 60)

        # Startup migrates the legacy Cookie into accounts.json; reopen that
        # store to prove the credential stayed on disk after a 429.
        self.assertEqual(Accounts().cookie(), COOKIE)
        self.assertTrue(self._api("GET", "/v1/auth").json()["authenticated"])

        # Local-only endpoints still work while the cooldown is active.
        self.assertEqual(self._api("GET", "/v1/health").status_code, 200)
        self.assertEqual(self._api("GET", "/v1/favorites").json()["items"], [])
        self.assertEqual(self._api("GET", "/v1/recent").json()["items"], [])

        sent = len(fixture.requests)
        self.assertEqual(sent, 1)
        again = self._api("GET", "/v1/files")
        self.assertEqual(again.status_code, 429)
        self.assertEqual(len(fixture.requests), sent)  # refused before any upstream call
        self.assertEqual(Accounts().cookie(), COOKIE)
        self.assertTrue(self._api("GET", "/v1/auth").json()["authenticated"])

    def test_login_under_active_cooldown_is_refused_without_a_window(self):
        calls: list[int] = []

        def unexpected_login():
            calls.append(1)
            raise AssertionError("a cooldown must not create a QR window")

        guard = RequestGuard.memory()
        guard.record_cooldown(60, REASON_RATE_LIMITED)
        self._start(transport=httpx.MockTransport(_unreachable), login_factory=unexpected_login, guard=guard)

        started = self._api("POST", "/v1/auth/login")
        self.assertEqual(started.status_code, 429, started.text)
        self.assertEqual(started.json()["error"]["type"], "rate_limited")
        self.assertGreaterEqual(int(started.headers["Retry-After"]), 1)
        self.assertEqual(calls, [])
        self.assertEqual(self._api("GET", "/v1/auth").json()["login_state"], "idle")

    def test_video_origin_429_stops_streaming_without_a_refetch(self):
        self._save_plugin_cookie()
        fixture = DriveFixture()
        self._start(transport=fixture.transport(), guard=RequestGuard.memory())

        url = self._api("POST", "/v1/play", body={"fid": "v10"}).json()["url"]
        self.assertEqual(fixture.download_calls, 1)

        fixture.origin_status = 429
        refused = self.client.get(self.base + url)
        self.assertEqual(refused.status_code, 429, refused.text)
        self.assertEqual(refused.json()["error"]["type"], "rate_limited")
        self.assertGreaterEqual(int(refused.headers["Retry-After"]), 60)
        self.assertEqual(fixture.download_calls, 1)  # no signed-URL refetch

        api_before, origins_before = self._upstream_split(fixture)
        again = self.client.get(self.base + url)
        self.assertEqual(again.status_code, 429)
        self.assertEqual(self._upstream_split(fixture), (api_before, origins_before))

        self.assertEqual(Accounts().cookie(), COOKIE)
        self.assertTrue(self._api("GET", "/v1/auth").json()["authenticated"])

    def test_subtitle_origin_429_stops_reading_without_a_refetch(self):
        self._save_plugin_cookie()
        fixture = DriveFixture()
        fixture.records.append(record("sdir", "字幕剧", directory=True))
        fixture.records.append(record("sv1", "剧.S01E01.mkv", "sdir"))
        fixture.records.append(record("s1", "剧.S01E01.srt", "sdir", size=16))
        self._start(transport=fixture.transport(), guard=RequestGuard.memory())

        fixture.origin_status = 429
        refused = self._api("GET", "/v1/subtitle?fid=s1")
        self.assertEqual(refused.status_code, 429, refused.text)
        self.assertEqual(refused.json()["error"]["type"], "rate_limited")
        self.assertGreaterEqual(int(refused.headers["Retry-After"]), 60)
        self.assertEqual(fixture.download_calls, 1)  # no signed-URL refetch

        api_before, origins_before = self._upstream_split(fixture)
        again = self._api("GET", "/v1/subtitle?fid=s1")
        self.assertEqual(again.status_code, 429)
        self.assertEqual(self._upstream_split(fixture), (api_before, origins_before))

        self.assertEqual(Accounts().cookie(), COOKIE)
        self.assertTrue(self._api("GET", "/v1/auth").json()["authenticated"])


if __name__ == "__main__":
    unittest.main()
