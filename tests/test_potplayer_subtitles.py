"""Behavioural tests for the bridge's subtitle matching and reading endpoints."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

from quark_cli.potplayer.service import Service
from quark_cli.potplayer.subtitles import MAX_SUBTITLE_SIZE

from potplayer_fixture import COOKIE, ORIGIN_HOST, DriveFixture, record


class _BridgeCase(unittest.TestCase):
    """Starts one bridge instance on an ephemeral port and tears it down."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="quark-subtitles-"))
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
        return {"transport": self.fixture.transport(), "cookie": COOKIE}

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

    def api(self, method: str, path: str, *, token: object = None, headers: dict | None = None, **kwargs) -> httpx.Response:
        merged = dict(headers or {})
        if token is not False:
            merged["Authorization"] = f"Bearer {self.service.token if token is None else token}"
        return self.client.request(method, self.base + path, headers=merged, **kwargs)

    def play(self, fid: str) -> dict:
        response = self.api("POST", "/v1/play", json={"fid": fid})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class MatchCase(_BridgeCase):
    """Listing of same-directory subtitle candidates."""

    def setUp(self):
        super().setUp()
        self.fixture.records.append(record("sdir", "字幕剧", directory=True))
        self.fixture.records.append(record("sv1", "剧.S01E01.mkv", "sdir"))

    def subtitle(self, fid: str, name: str) -> None:
        self.fixture.records.append(record(fid, name, "sdir", size=16))

    def items(self, fid: str) -> list[dict]:
        response = self.api("GET", f"/v1/subtitles?fid={fid}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    def test_matches_expose_only_the_contract_fields(self):
        self.subtitle("s-zh", "剧.S01E01.zh.ass")
        payload = self.api("GET", "/v1/subtitles?fid=sv1").json()
        self.assertEqual(payload["video_fid"], "sv1")
        self.assertEqual(payload["items"], [{
            "fid": "s-zh",
            "name": "剧.S01E01.zh.ass",
            "format": "ass",
            "lang": "zh",
            "match": "name",
        }])
        self.assertNotIn(ORIGIN_HOST, json.dumps(payload))
        self.assertNotIn("cookie", json.dumps(payload).casefold())

    def test_chinese_candidates_sort_first_and_languages_are_reported(self):
        self.subtitle("s-plain", "剧.S01E01.srt")
        self.subtitle("s-zh", "剧.S01E01.zh.ass")
        self.subtitle("s-chs", "剧.S01E01.chs.SRT")
        self.subtitle("s-en", "剧.S01E01.EN.srt")
        self.subtitle("s-tag", "剧.S01E01.1080p.zh.srt")
        items = self.items("sv1")
        self.assertEqual([item["lang"] for item in items], ["zh", "zh", "zh", "en", ""])
        self.assertEqual({
            item["name"]: (item["format"], item["match"]) for item in items
        }, {
            "剧.S01E01.1080p.zh.srt": ("srt", "episode"),
            "剧.S01E01.chs.SRT": ("srt", "name"),
            "剧.S01E01.zh.ass": ("ass", "name"),
            "剧.S01E01.EN.srt": ("srt", "name"),
            "剧.S01E01.srt": ("srt", "name"),
        })
        self.assertEqual(
            [item["fid"] for item in items if item["lang"] == "zh"], ["s-tag", "s-chs", "s-zh"]
        )

    def test_other_episode_season_or_series_is_rejected(self):
        self.subtitle("s-good", "剧.S01E01.zh.srt")
        self.subtitle("s-episode", "剧.S01E02.zh.srt")
        self.subtitle("s-season", "剧.S02E01.zh.srt")
        self.subtitle("s-series", "另一剧.S01E01.zh.srt")
        self.subtitle("s-format", "剧.第1集.zh.srt")
        self.assertEqual([item["fid"] for item in self.items("sv1")], ["s-good"])

    def test_chinese_episode_numbers_match_across_zero_padding(self):
        self.fixture.records.append(record("sv-cn", "剧.第01集.mp4", "sdir"))
        self.subtitle("s-pad", "剧.第1集.zh.srt")
        self.subtitle("s-other", "剧.第2集.zh.srt")
        self.assertEqual(
            [(item["fid"], item["match"]) for item in self.items("sv-cn")], [("s-pad", "episode")]
        )

    def test_scan_covers_every_page_of_the_directory(self):
        for index in range(150):
            self.subtitle(f"filler-{index:03d}", f"填充{index:03d}.srt")
        self.subtitle("late", "剧.S01E01.zh.srt")
        self.assertEqual([item["fid"] for item in self.items("sv1")], ["late"])
        pages = {
            request.url.params["_page"]
            for request in self.fixture.requests
            if request.url.path.endswith("/file/sort")
        }
        self.assertIn("2", pages)

    def test_video_without_candidates_returns_an_empty_list(self):
        self.fixture.records.append(record("sv-empty", "剧.S01E05.mkv", "sdir"))
        self.assertEqual(
            self.api("GET", "/v1/subtitles?fid=sv-empty").json(),
            {"video_fid": "sv-empty", "items": []},
        )

    def test_only_srt_and_ass_files_are_candidates(self):
        self.subtitle("s-upper", "剧.S01E01.ZH.ASS")
        self.subtitle("s-txt", "剧.S01E01.zh.txt")
        self.subtitle("s-bak", "剧.S01E01.zh.srt.bak")
        self.fixture.records.append(record("s-dir", "剧.S01E01.zh.srt", "sdir", directory=True))
        self.assertEqual(
            [(item["fid"], item["format"], item["lang"]) for item in self.items("sv1")],
            [("s-upper", "ass", "zh")],
        )

    def test_session_selector_resolves_and_rejects_ambiguous_or_stale_ones(self):
        self.subtitle("s-zh", "剧.S01E01.zh.ass")
        session = self.play("sv1")["url"].split("/")[3]
        payload = self.api("GET", f"/v1/subtitles?session={session}").json()
        self.assertEqual(payload["video_fid"], "sv1")
        self.assertEqual([item["fid"] for item in payload["items"]], ["s-zh"])
        self.assertEqual(self.api("GET", "/v1/subtitles?session=absent").status_code, 404)
        self.assertEqual(self.api("GET", f"/v1/subtitles?fid=sv1&session={session}").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/subtitles").status_code, 400)

    def test_non_video_and_unknown_selectors_are_rejected(self):
        self.assertEqual(self.api("GET", "/v1/subtitles?fid=poster").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/subtitles?fid=root-dir").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/subtitles?fid=absent").status_code, 502)
        self.assertEqual(self.api("POST", "/v1/subtitles").status_code, 405)

    def test_listing_never_downloads_or_records_playback(self):
        self.subtitle("s-zh", "剧.S01E01.zh.ass")
        self.assertEqual(self.api("GET", "/v1/subtitles?fid=sv1").status_code, 200)
        self.assertEqual(self.fixture.origin_requests(), [])
        self.assertEqual(self.api("GET", "/v1/recent").json()["items"], [])

    def test_head_listing_answers_headers_only(self):
        self.subtitle("s-zh", "剧.S01E01.zh.ass")
        response = self.api("HEAD", "/v1/subtitles?fid=sv1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")


class ReadCase(_BridgeCase):
    """Bounded, trusted, in-memory subtitle reads."""

    def setUp(self):
        super().setUp()
        self.body = "1\n00:00:01,000 --> 00:00:02,000\n你好，世界\n".encode("utf-8")
        self.subtitle_record("sub1", "剧.S01E01.zh.srt", self.body)

    def subtitle_record(self, fid: str, name: str, body: bytes, size: int | None = None) -> None:
        self.fixture.records.append(
            record(fid, name, "root-dir", size=len(body) if size is None else size)
        )
        self.fixture.bodies[fid] = body

    def test_read_returns_utf8_plain_text(self):
        response = self.api("GET", "/v1/subtitle?fid=sub1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.content, self.body)
        self.assertNotIn("set-cookie", {name.lower() for name in response.headers})

    def test_read_sends_only_the_isolated_cookie_to_the_origin(self):
        self.assertEqual(self.api("GET", "/v1/subtitle?fid=sub1").status_code, 200)
        origins = self.fixture.origin_requests()
        self.assertEqual(len(origins), 1)
        self.assertEqual(origins[0].headers.get("cookie"), "__puus=secret")
        self.assertNotIn("b=1", origins[0].headers.get("cookie", ""))
        self.assertEqual(self.fixture.requests[0].headers.get("cookie"), COOKIE)

    def test_expired_signature_is_refetched_once(self):
        self.fixture.origin_failures = 1
        self.fixture.origin_epoch += 1
        response = self.api("GET", "/v1/subtitle?fid=sub1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.body)
        self.assertEqual(self.fixture.download_calls, 2)

    def test_repeated_signature_failure_is_not_an_account_error(self):
        self.fixture.origin_failures = 4
        self.fixture.origin_epoch += 1
        response = self.api("GET", "/v1/subtitle?fid=sub1")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["type"], "access_restricted")
        self.assertGreaterEqual(int(response.headers["retry-after"]), 1)
        self.assertEqual(self.fixture.download_calls, 2)
        self.assertTrue(self.api("GET", "/v1/auth").json()["authenticated"])
        requests, origins = len(self.fixture.requests), len(self.fixture.origin_requests())
        # The cooldown refuses the next subtitle and control calls locally.
        self.assertEqual(self.api("GET", "/v1/subtitle?fid=sub1").status_code, 403)
        self.assertEqual(self.api("GET", "/v1/files").status_code, 403)
        self.assertEqual(len(self.fixture.requests), requests)
        self.assertEqual(len(self.fixture.origin_requests()), origins)

    def test_bom_utf16_and_gb18030_text_is_decoded(self):
        cases = [
            ("utf8-bom.srt", "\ufeff你好世界\n".encode("utf-8"), "你好世界\n"),
            ("utf16.srt", "你好世界\n".encode("utf-16"), "你好世界\n"),
            ("gb18030.srt", "简体中文字幕\n".encode("gb18030"), "简体中文字幕\n"),
        ]
        for index, (name, raw, expected) in enumerate(cases):
            with self.subTest(name=name):
                fid = f"enc-{index}"
                self.subtitle_record(fid, name, raw)
                response = self.api("GET", f"/v1/subtitle?fid={fid}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.text, expected)

    def test_timeline_and_formatting_are_preserved_without_replacement(self):
        ass = (
            "[Script Info]\nTitle: 剧\r\n"
            "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你好\n"
        )
        self.subtitle_record("ass1", "剧.S01E01.zh.ass", ass.encode("utf-8"))
        response = self.api("GET", "/v1/subtitle?fid=ass1")
        self.assertEqual(response.text, ass)
        self.assertNotIn("\ufffd", response.text)

    def test_read_refuses_non_subtitle_and_directory_fids(self):
        self.assertEqual(self.api("GET", "/v1/subtitle?fid=poster").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/subtitle?fid=root-dir").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/subtitle?fid=v2").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/subtitle?fid=absent").status_code, 502)
        self.assertEqual(self.api("GET", "/v1/subtitle").status_code, 400)
        self.assertEqual(self.api("GET", "/v1/subtitle?fid=").status_code, 400)
        self.assertEqual(self.api("POST", "/v1/subtitle?fid=sub1").status_code, 405)
        self.assertEqual(self.fixture.origin_requests(), [])

    def test_oversized_metadata_is_refused_before_any_transfer(self):
        self.subtitle_record("big1", "大.srt", b"x", size=MAX_SUBTITLE_SIZE + 1)
        response = self.api("GET", "/v1/subtitle?fid=big1")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fixture.origin_requests(), [])
        self.assertEqual(self.fixture.download_calls, 0)

    def test_origin_declared_length_over_the_limit_is_refused(self):
        self.subtitle_record("big2", "大2.srt", b"x" * (MAX_SUBTITLE_SIZE + 10), size=10)
        response = self.api("GET", "/v1/subtitle?fid=big2")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["type"], "remote_error")

    def test_body_over_the_limit_is_stopped_even_when_the_header_lies(self):
        self.subtitle_record("big3", "大3.srt", b"x" * (MAX_SUBTITLE_SIZE + 10), size=10)
        self.fixture.content_length_override = 10
        response = self.api("GET", "/v1/subtitle?fid=big3")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["type"], "remote_error")

    def test_read_writes_nothing_and_leaves_recent_untouched(self):
        self.assertEqual(self.api("GET", "/v1/subtitle?fid=sub1").status_code, 200)
        self.assertFalse((self.directory / "library.json").exists())
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.api("GET", "/v1/recent").json()["items"], [])

    def test_head_read_answers_headers_without_a_body(self):
        response = self.api("HEAD", "/v1/subtitle?fid=sub1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(int(response.headers["content-length"]), len(self.body))
        self.assertEqual(response.content, b"")
        # The encoded length is only known after the bounded read.
        self.assertEqual(len(self.fixture.origin_requests()), 1)


class AuthGateCase(_BridgeCase):
    """The subtitle endpoints stay behind the runtime token and a login."""

    def service_kwargs(self) -> dict:
        return {"transport": self.fixture.transport()}

    def test_endpoints_require_the_runtime_token(self):
        self.assertEqual(self.client.get(self.base + "/v1/subtitles?fid=v2").status_code, 401)
        self.assertEqual(self.client.get(self.base + "/v1/subtitle?fid=v2").status_code, 401)

    def test_endpoints_require_a_login(self):
        for path in ("/v1/subtitles?fid=v2", "/v1/subtitle?fid=v2"):
            with self.subTest(path=path):
                response = self.api("GET", path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["type"], "login_required")


if __name__ == "__main__":
    unittest.main()
