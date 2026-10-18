"""Watch-center grouping, series latest/next, history removal and bookmark-open.

The bridge is driven over real loopback HTTP with the in-memory drive fixture;
the playback monitor runs without its worker so every local-state path is
exercised synchronously. No real Quark request and no user PotPlayer window is
ever touched.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli.potplayer.service import BOOKMARK_FALLBACK_NAME, WATCH_UNNAMED, Service, _spawn_bookmark_player
from quark_cli.state import write_json

from potplayer_fixture import COOKIE, VIDEO_BYTES, DriveFixture, record


class _Launcher:
    def __init__(self) -> None:
        self.calls = 0
        self.returncode = None

    def poll(self):
        return self.returncode


class _Case(unittest.TestCase):
    """One bridge on an ephemeral loopback port with a fake status launcher."""

    cookie: str | None = COOKIE

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="quark-watch-"))
        self.previous_config = os.environ.get("QUARK_CONFIG_DIR")
        os.environ["QUARK_CONFIG_DIR"] = str(self.directory)
        self.fixture = DriveFixture()
        self.launcher = _Launcher()
        self.fail_open = False
        self.service = Service(
            transport=self.fixture.transport(),
            cookie=self.cookie,
            status_launcher=self._launch,
            highlight_launcher=self._launch,
            monitor_start=False,
        )
        self.server = self.service.build_server()
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.service.port}"
        self.client = httpx.Client(timeout=10.0)
        self.addCleanup(self._stop)

    def _launch(self):
        if self.fail_open:
            from quark_cli.errors import LocalIOError

            raise LocalIOError("无法打开播放状态与诊断窗口")
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

    def api(self, method: str, path: str, *, body=None, token: str | None = None) -> httpx.Response:
        headers = {} if token is False else {"Authorization": f"Bearer {self.token if token is None else token}"}
        return self.client.request(method, self.base + path, json=body, headers=headers)

    def play(self, fid: str) -> str:
        response = self.api("POST", "/v1/play", body={"fid": fid})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["url"]

    SIZE = len(VIDEO_BYTES)

    def save_document(self, recent=(), marks=(), favorites=(), bookmarks=()):
        write_json(self.service.library.path, {
            "version": 4,
            "favorites": list(favorites),
            "recent": list(recent),
            "marks": list(marks),
        })

    def row(self, fid, name, size=None, *, parent="root-dir", parent_name="",
            played_at=10, position_ms=0, duration_ms=0, progress_at=0):
        return {
            "fid": fid, "name": name, "size": self.SIZE if size is None else size,
            "played_at": played_at, "position_ms": position_ms, "duration_ms": duration_ms,
            "progress_at": progress_at, "parent": parent, "parent_name": parent_name,
        }

    def note_root(self) -> None:
        """Read the root listing so the directory FID→name facts get recorded."""
        self.assertEqual(self.api("GET", "/v1/files?parent=0").status_code, 200)


# -- watch-center grouping ----------------------------------------------


class HighlightsCase(_Case):
    def test_launcher_uses_the_installed_player_and_a_marker_url_without_a_shell(self):
        player_dir = self.directory / "player"
        player_dir.mkdir()
        executable = player_dir / "PotPlayerMini64.exe"
        executable.write_bytes(b"test")
        import json
        manifest = self.directory / "QuarkPotPlayer" / "install.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"potplayer_dir": str(player_dir)}), "utf-8")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.directory)}), patch("quark_cli.potplayer.service.subprocess.Popen") as launch:
            _spawn_bookmark_player("b1")
        launch.assert_called_once_with([str(executable), "https://quark.invalid/mark/b1"], close_fds=True)
    def test_directory_first_page_combines_series_with_one_upstream_read(self):
        self.api("GET", "/v1/files?parent=root-dir")
        self.api("GET", "/v1/series?parent=root-dir")
        self.assertEqual(len([r for r in self.fixture.requests if r.url.path.endswith('/file/sort')]), 2)
        self.fixture.requests.clear()
        response = self.api("GET", "/v1/files?parent=root-dir&include_series=1")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["series"]["total"], 3)
        self.assertEqual(payload["series"]["continue"]["fid"], "v2")
        self.assertFalse(payload["favorite"])
        reads = [r for r in self.fixture.requests if r.url.path.endswith("/file/sort")]
        self.assertEqual(len(reads), 1)

    def test_combined_page_counts_videos_beyond_the_visible_page(self):
        self.fixture.records = [record("folder", "剧集", directory=True)] + [
            record(f"episode-{index}", f"第{index}集.mp4", "folder") for index in range(205)
        ]
        payload = self.api("GET", "/v1/files?parent=folder&size=50&include_series=1").json()
        self.assertEqual(len(payload["items"]), 50)
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["series"]["total"], 205)
        self.assertEqual(len([r for r in self.fixture.requests if r.url.path.endswith('/file/sort')]), 3)

    def test_marker_play_resolves_saved_video_and_binds_the_requested_point(self):
        entry = self.service.monitor.bookmarks.add("v2", "第2集.mp4", self.SIZE, 60_000, 1_800_000, "对白")
        response = self.api("POST", "/v1/play", body={"bookmark_id": entry["id"], "player_pid": 123})
        self.assertEqual(response.status_code, 200, response.text)
        bound = self.service.monitor._bindings[123].session
        self.assertEqual((bound.fid, bound.bookmark_id), ("v2", entry["id"]))
        self.assertNotIn(entry["id"], response.text)

    def test_marker_play_refuses_unknown_missing_pid_or_changed_size(self):
        entry = self.service.monitor.bookmarks.add("v2", "第2集.mp4", self.SIZE + 1, 60_000, 1_800_000, "")
        for body, status in (({"bookmark_id": "missing", "player_pid": 123}, 404),
                             ({"bookmark_id": entry["id"]}, 400),
                             ({"bookmark_id": entry["id"], "player_pid": 123}, 409)):
            response = self.api("POST", "/v1/play", body=body)
            self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(self.service.monitor.snapshot(), [])

    def test_cold_play_launches_only_a_known_marker(self):
        launched = []
        self.service._player_launcher = launched.append
        entry = self.service.monitor.bookmarks.add("v2", "第2集.mp4", self.SIZE, 60_000, 1_800_000, "")
        response = self.api("POST", "/v1/bookmarks/play", body={"id": entry["id"]})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(launched, [entry["id"]])
        self.assertEqual(self.fixture.requests, [])
        self.assertEqual(self.api("POST", "/v1/bookmarks/play", body={"id": "missing"}).status_code, 404)
        self.assertEqual(launched, [entry["id"]])

    def test_empty_directory_has_no_video_actions(self):
        payload = self.api("GET", "/v1/files?parent=empty-dir&include_series=1").json()
        self.assertEqual(payload["series"]["total"], 0)
        self.assertIsNone(payload["series"]["continue"])

    def test_global_mark_list_and_open_are_local_and_keep_old_data(self):
        store = self.service.monitor.bookmarks
        first = store.add("v2", "第2集.mp4", self.SIZE, 60_000, 1_800_000, "精彩对白")
        second = store.add("movie", "电影.mp4", self.SIZE, 90_000, 1_800_000, "结尾")
        response = self.api("GET", "/v1/bookmarks")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["items"]], [second["id"], first["id"]])
        opened = self.api("POST", "/v1/bookmarks/open", body={"scope": "all"})
        self.assertEqual(opened.status_code, 202)
        self.assertEqual(self.service.bookmark_request()["scope"], "all")
        self.assertEqual(self.fixture.requests, [])


class WatchCenterCase(_Case):
    def test_groups_by_verified_parent_and_keeps_the_latest_episode(self):
        self.note_root()
        self.save_document(recent=[
            self.row("v10", "第10集.mp4", parent_name="剧集", played_at=30,
                     position_ms=90_000, duration_ms=1_800_000, progress_at=25),
            self.row("v2", "第2集.mp4", parent_name="剧集", played_at=10,
                     position_ms=60_000, duration_ms=1_800_000, progress_at=5),
        ])
        items = self.api("GET", "/v1/watch-center").json()["items"]
        self.assertEqual(len(items), 1)
        group = items[0]
        self.assertEqual(group["kind"], "series")
        self.assertEqual(group["key"], "root-dir")
        self.assertEqual(group["parent"], "root-dir")
        self.assertEqual(group["name"], "剧集")
        # The most recently watched episode (v10) names the group, not the row
        # that merely moved to the front of the recent list last.
        self.assertEqual(group["latest"]["fid"], "v10")
        self.assertEqual(group["unfinished_count"], 2)
        self.assertIn("剧集 · 上次：第10集.mp4", group["display"])
        self.assertNotIn("共", group["display"])

    def test_root_and_unknown_parents_stand_alone_keyed_by_video(self):
        self.note_root()
        self.save_document(recent=[
            self.row("movie", "电影.mp4", parent="0", played_at=9),
            self.row("orphan", "孤儿.mp4", parent="", played_at=8),
        ])
        items = {item["key"]: item for item in self.api("GET", "/v1/watch-center").json()["items"]}
        self.assertEqual(items["movie"]["kind"], "video")
        self.assertEqual(items["movie"]["name"], "电影.mp4")
        self.assertEqual(items["orphan"]["kind"], "video")
        self.assertEqual(items["orphan"]["key"], "orphan")

    def test_identical_directory_names_stay_as_separate_groups(self):
        self.fixture.records.extend([
            record("second-dir", "剧集", directory=True),
            record("s1", "第1集.mp4", "second-dir"),
        ])
        self.save_document(recent=[
            self.row("v2", "第2集.mp4", parent="root-dir", played_at=5),
            self.row("s1", "第1集.mp4", parent="second-dir", played_at=6),
        ])
        # Only root-dir's name is verified here; second-dir keeps its stored name.
        self.note_root()
        keys = {item["key"] for item in self.api("GET", "/v1/watch-center").json()["items"]}
        self.assertEqual(keys, {"root-dir", "second-dir"})

    def test_unnamed_series_uses_the_neutral_label(self):
        self.save_document(recent=[self.row("v2", "第2集.mp4", parent="unknown-dir")])
        group = self.api("GET", "/v1/watch-center").json()["items"][0]
        self.assertEqual(group["name"], WATCH_UNNAMED)
        self.assertIn(WATCH_UNNAMED, group["display"])

    def test_groups_sort_by_latest_activity_stamp(self):
        self.note_root()
        self.save_document(recent=[
            self.row("v2", "第2集.mp4", played_at=10, progress_at=5),
            self.row("movie", "电影.mp4", parent="0", played_at=1, progress_at=99),
        ])
        items = self.api("GET", "/v1/watch-center").json()["items"]
        # 电影.mp4's progress (99) beats the series' played_at (10).
        self.assertEqual([item["key"] for item in items], ["movie", "root-dir"])

    def test_requires_the_local_token(self):
        self.assertEqual(self.client.get(self.base + "/v1/watch-center").status_code, 401)
        self.assertEqual(self.api("GET", "/v1/watch-center", token="wrong").status_code, 401)
        self.assertEqual(self.api("POST", "/v1/watch-center").status_code, 405)


# -- series latest / next ------------------------------------------------


class SeriesLatestNextCase(_Case):
    def test_latest_is_the_most_recent_episode_still_in_the_listing(self):
        self.save_document(recent=[
            self.row("v2", "第2集.mp4", played_at=5),
            self.row("v9", "第9集.mkv", played_at=20),
        ])
        payload = self.api("GET", "/v1/series?parent=root-dir").json()
        self.assertEqual(payload["latest"]["fid"], "v9")
        # Natural listing order, no wrap: v9 is followed by v10.
        self.assertEqual(payload["next"]["fid"], "v10")

    def test_latest_at_the_last_episode_never_wraps_to_the_first(self):
        self.save_document(recent=[self.row("v10", "第10集.mp4", played_at=20)])
        payload = self.api("GET", "/v1/series?parent=root-dir").json()
        self.assertEqual(payload["latest"]["fid"], "v10")
        self.assertIsNone(payload["next"])

    def test_a_stale_size_record_never_names_latest_or_next(self):
        self.save_document(recent=[self.row("v9", "第9集.mkv", size=self.SIZE + 1, played_at=20)])
        payload = self.api("GET", "/v1/series?parent=root-dir").json()
        self.assertIsNone(payload["latest"])
        # No latest: the first unwatched episode in listing order is offered.
        self.assertEqual(payload["next"]["fid"], "v2")

    def test_no_history_offers_the_first_unwatched_episode(self):
        payload = self.api("GET", "/v1/series?parent=root-dir").json()
        self.assertIsNone(payload["latest"])
        self.assertEqual(payload["next"]["fid"], "v2")


# -- recent removal ------------------------------------------------------


class RecentRemoveCase(_Case):
    def seed(self):
        self.save_document(
            recent=[self.row("v2", "第2集.mp4", played_at=10), self.row("v9", "第9集.mkv", played_at=9)],
            marks=[{"fid": "v9", "name": "第9集.mkv", "size": self.SIZE, "watched_at": 1}],
            favorites=[{"fid": "root-dir", "name": "剧集", "added_at": 1}],
        )
        self.service.monitor.bookmarks.add("v9", "第9集.mkv", self.SIZE, 30_000, 1_800_000, "")

    def test_remove_drops_only_that_row_and_is_idempotent(self):
        self.seed()
        removed = self.api("POST", "/v1/recent/remove", body={"fid": "v2"})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json(), {"status": "removed"})
        self.assertEqual([row["fid"] for row in self.api("GET", "/v1/recent").json()["items"]], ["v9"])
        self.assertEqual([row["fid"] for row in self.service.library.favorites()], ["root-dir"])
        self.assertEqual([row["fid"] for row in self.service.library.snapshot()["marks"]], ["v9"])
        self.assertEqual(len(self.service.monitor.bookmarks.list("v9")), 1)
        again = self.api("POST", "/v1/recent/remove", body={"fid": "v2"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json(), {"status": "removed"})

    def test_a_late_range_get_on_the_old_session_cannot_resurrect_the_row(self):
        url = self.play("v2")
        self.assertEqual(self.client.get(self.base + url, headers={"Range": "bytes=0-9"}).status_code, 206)
        self.assertEqual([row["fid"] for row in self.api("GET", "/v1/recent").json()["items"]], ["v2"])
        self.assertEqual(self.api("POST", "/v1/recent/remove", body={"fid": "v2"}).status_code, 200)
        # The old session's later range GET must not re-create history.
        self.assertEqual(self.client.get(self.base + url, headers={"Range": "bytes=0-9"}).status_code, 206)
        self.assertEqual(self.api("GET", "/v1/recent").json()["items"], [])

    def test_a_new_explicit_play_records_again(self):
        url = self.play("v2")
        self.assertEqual(self.client.get(self.base + url).status_code, 200)
        self.assertEqual(self.api("POST", "/v1/recent/remove", body={"fid": "v2"}).status_code, 200)
        fresh = self.play("v2")
        self.assertNotEqual(fresh, url)
        self.assertEqual(self.client.get(self.base + fresh).status_code, 200)
        self.assertEqual([row["fid"] for row in self.api("GET", "/v1/recent").json()["items"]], ["v2"])

    def test_unrelated_stream_keeps_recording_after_a_remove(self):
        url = self.play("v9")
        self.assertEqual(self.client.get(self.base + url).status_code, 200)
        self.assertEqual(self.api("POST", "/v1/recent/remove", body={"fid": "v2"}).status_code, 200)
        self.assertEqual([row["fid"] for row in self.api("GET", "/v1/recent").json()["items"]], ["v9"])

    def test_requires_the_local_token_and_rejects_a_bad_fid(self):
        self.assertEqual(self.client.post(self.base + "/v1/recent/remove", json={"fid": "v2"}).status_code, 401)
        self.assertEqual(self.api("POST", "/v1/recent/remove", body={"fid": ""}).status_code, 400)
        self.assertEqual(self.api("GET", "/v1/recent/remove").status_code, 405)

    def test_a_broken_library_answers_500_not_success(self):
        path = self.service.library.path
        self.save_document(recent=[self.row("v2", "第2集.mp4")])
        path.write_text("{broken", encoding="utf-8")
        response = self.api("POST", "/v1/recent/remove", body={"fid": "v2"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["type"], "local_io_error")


# -- bookmark window request --------------------------------------------


class BookmarkOpenCase(_Case):
    cookie = None

    def snapshot_request(self):
        return self.api("GET", "/v1/bookmarks/state").json()["bookmark_request"]

    def test_request_is_retained_named_locally_and_returned(self):
        self.save_document(recent=[self.row("v2", "第2集.mp4")])
        before = len(self.fixture.requests)
        opened = self.api("POST", "/v1/bookmarks/open", body={"fid": "v2"})
        self.assertEqual(opened.status_code, 202)
        self.assertEqual(opened.json(), {"status": "opened"})
        request = self.snapshot_request()
        self.assertEqual(request["fid"], "v2")
        self.assertEqual(request["name"], "第2集.mp4")
        self.assertTrue(request["id"])
        # Local only: no cloud request was made.
        self.assertEqual(len(self.fixture.requests), before)

    def test_unknown_fid_uses_the_neutral_name_and_a_fresh_id(self):
        first = self.api("POST", "/v1/bookmarks/open", body={"fid": "ghost"}).json()
        self.assertEqual(first["status"], "opened")
        request = self.snapshot_request()
        self.assertEqual(request["name"], BOOKMARK_FALLBACK_NAME)
        self.api("POST", "/v1/bookmarks/open", body={"fid": "ghost"})
        again = self.snapshot_request()
        self.assertEqual(again["fid"], "ghost")
        self.assertNotEqual(again["id"], request["id"])

    def test_management_open_does_not_cancel_an_independent_highlight_request(self):
        self.api("POST", "/v1/bookmarks/open", body={"fid": "v2"})
        self.assertIsNotNone(self.snapshot_request())
        self.assertEqual(self.api("POST", "/v1/diagnostics/open", body={}).status_code, 202)
        self.assertEqual(self.snapshot_request()['fid'], 'v2')

    def test_a_launcher_failure_answers_500_and_leaves_no_target(self):
        self.fail_open = True
        response = self.api("POST", "/v1/bookmarks/open", body={"fid": "v2"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["type"], "local_io_error")
        self.assertIsNone(self.snapshot_request())

    def test_the_report_never_carries_the_request(self):
        self.api("POST", "/v1/bookmarks/open", body={"fid": "v2"})
        report = self.api("GET", "/v1/diagnostics/report").json()
        self.assertNotIn("bookmark_request", report)
        self.assertNotIn("v2", repr(report))

    def test_requires_the_local_token(self):
        self.assertEqual(self.client.post(self.base + "/v1/bookmarks/open", json={"fid": "v2"}).status_code, 401)
        self.assertEqual(self.api("GET", "/v1/bookmarks/open").status_code, 405)


if __name__ == "__main__":
    unittest.main()
