"""Deterministic tests for local playback bookmarks and their monitor commands.

Every bookmark command is driven through the private synchronous seam
(:meth:`PlaybackMonitor._poll_once`) with a fake adapter and fake clock, so no
real player or timing is involved -- except the two tests that must prove the
command really runs on the worker thread and that a hanging native sample times
out. Samples always carry the *logical* identity the extension hands the player
(``https://quark.invalid/...``); a bare basename is never an accepted identity.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from quark_cli.errors import LocalIOError, UsageError
from quark_cli.potplayer import playback
from quark_cli.potplayer.bookmark_state import NOTE_LIMIT, BookmarkStore
from quark_cli.potplayer.library_state import Library
from quark_cli.potplayer.playback import (
    MSG_MISMATCH,
    PlaybackMonitor,
    STATE_MISMATCH,
)
from quark_cli.potplayer.stream import PlaySession
from quark_cli.state import write_json


def logical(fid: str) -> str:
    return "https://quark.invalid/video/" + fid


class FakeSample:
    __slots__ = ("player_pid", "file_name", "position_ms", "duration_ms", "status")

    def __init__(self, player_pid: int, file_name: str, position_ms: int, duration_ms: int, status: int) -> None:
        self.player_pid = player_pid
        self.file_name = file_name
        self.position_ms = position_ms
        self.duration_ms = duration_ms
        self.status = status


class FakePlayer:
    """Scripted samples per PID; the last queued sample repeats until replaced."""

    def __init__(self) -> None:
        self.queued: dict[int, list[FakeSample | None]] = {}
        self.seeks: list[tuple[int, str, int]] = []
        self.seek_ok = True
        self.closed = False

    def feed(self, player_pid: int, *samples: FakeSample | None) -> None:
        self.queued[player_pid] = list(samples)

    def sample(self, player_pid: int) -> FakeSample | None:
        queue = self.queued.get(player_pid)
        if not queue:
            return None
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def seek(self, player_pid: int, expected_file_name: str, position_ms: int) -> bool:
        self.seeks.append((player_pid, expected_file_name, position_ms))
        return self.seek_ok

    def close(self) -> None:
        self.closed = True


class SeekingPlayer(FakePlayer):
    """A fake whose clock follows the last successful seek (so a jump can verify)."""

    def seek(self, player_pid: int, expected_file_name: str, position_ms: int) -> bool:
        ok = super().seek(player_pid, expected_file_name, position_ms)
        if ok:
            self.feed(player_pid, FakeSample(player_pid, expected_file_name, position_ms, 1_800_000, 2))
        return ok


class RecordingPlayer(FakePlayer):
    def __init__(self) -> None:
        super().__init__()
        self.threads: set[str] = set()

    def sample(self, player_pid: int) -> FakeSample | None:
        self.threads.add(threading.current_thread().name)
        return super().sample(player_pid)


class BlockingPlayer(FakePlayer):
    """A player whose ``sample`` blocks once :attr:`block` is set.

    ``polled`` is set on every sample so a test can wait for the worker to have
    finished a poll before arming ``block``; that makes the worker block *inside*
    the command it is handed, which is what a late write/seek regression needs to
    exercise. ``release`` lets the blocked sample finally return.
    """

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.block = threading.Event()
        self.blocked = threading.Event()
        self.polled = threading.Event()

    def sample(self, player_pid: int) -> FakeSample | None:
        self.polled.set()
        if self.block.is_set():
            self.blocked.set()
            self.release.wait(5.0)
        return super().sample(player_pid)


class FailingStore(BookmarkStore):
    def add(self, *args, **kwargs):  # type: ignore[override]
        raise LocalIOError("disk down")

    def list(self, fid):  # type: ignore[override]
        raise LocalIOError("disk down")


def session(fid: str = "v1", name: str = "a.mp4", size: int = 100, sid: str = "sess-1") -> PlaySession:
    return PlaySession(session=sid, fid=fid, name=name, size=size, url="https://origin/" + sid, created=0.0, last_used=0.0)


def sample(player_pid: int, name: str | None = None, position_ms: int = 30_000, duration_ms: int = 1_800_000, status: int = 2) -> FakeSample:
    return FakeSample(player_pid, logical("v1") if name is None else name, position_ms, duration_ms, status)


class Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class BookmarkCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="quark-bookmark-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.library = Library(self.directory / "library.json")
        self.store = BookmarkStore(self.directory / "bookmark.json")
        self.player = FakePlayer()
        self.clock = Clock()

    def monitor(self, *, player=None, store=None, start: bool = False, poll_interval: float = 2.0) -> PlaybackMonitor:
        return PlaybackMonitor(
            self.library,
            native_factory=lambda: player or self.player,
            clock=self.clock,
            poll_interval=poll_interval,
            bookmarks=store or self.store,
            start=start,
        )

    def bound(self, monitor: PlaybackMonitor, player_pid: int = 100) -> PlaybackMonitor:
        monitor.bind(session(), player_pid)
        monitor.stream_started("sess-1")
        return monitor

    # -- store: list / add / rename / remove / clear ---------------------

    def test_store_add_list_rename_remove_clear(self):
        first = self.store.add("v1", "a.mp4", 100, 30_000, 1_800_000, "开场")
        second = self.store.add("v1", "a.mp4", 100, 90_000, 1_800_000, "高潮")
        self.store.add("v2", "b.mp4", 200, 10_000, 600_000, "")
        rows = self.store.list("v1")
        self.assertEqual([row["id"] for row in rows], [second["id"], first["id"]])  # newest first
        self.assertEqual(set(rows[0]), {"id", "fid", "name", "size", "position_ms", "duration_ms", "note"})
        self.assertEqual(len(self.store.list("v2")), 1)
        self.assertEqual(self.store.list("nope"), [])

        self.assertTrue(self.store.rename(first["id"], "新的备注"))
        self.assertEqual(self.store.list("v1")[1]["note"], "新的备注")
        self.assertFalse(self.store.rename("missing", "x"))

        self.assertTrue(self.store.remove(second["id"]))
        self.assertFalse(self.store.remove(second["id"]))
        self.assertEqual([row["id"] for row in self.store.list("v1")], [first["id"]])

        self.store.clear()
        self.assertEqual(self.store.list("v1"), [])
        self.assertFalse(self.store.path.exists())

    def test_store_list_returns_copies(self):
        self.store.add("v1", "a.mp4", 100, 1_000, 2_000, "original")
        rows = self.store.list("v1")
        rows[0]["note"] = "hacked"
        self.assertEqual(self.store.list("v1")[0]["note"], "original")

    def test_store_note_limit_is_characters(self):
        note = "中" * NOTE_LIMIT
        self.store.add("v1", "a.mp4", 100, 1_000, 2_000, note)
        self.assertEqual(len(self.store.list("v1")[0]["note"]), NOTE_LIMIT)
        with self.assertRaises(UsageError):
            self.store.add("v1", "a.mp4", 100, 1_000, 2_000, "中" * (NOTE_LIMIT + 1))
        with self.assertRaises(UsageError):
            self.store.rename(self.store.list("v1")[0]["id"], "x" * (NOTE_LIMIT + 1))

    def test_store_rejects_invalid_input(self):
        with self.assertRaises(UsageError):
            self.store.add("bad/fid", "a.mp4", 100, 1, 2, "")
        with self.assertRaises(UsageError):
            self.store.add("v1", "", 100, 1, 2, "")
        with self.assertRaises(UsageError):
            self.store.add("v1", "a.mp4", -1, 1, 2, "")
        self.assertFalse(self.store.remove(""))
        self.assertFalse(self.store.rename("", "note"))

    def test_store_reports_corrupt_and_unsupported_documents(self):
        write_json(self.store.path, {"version": 1, "bookmarks": [{"bogus": 1}]})
        with self.assertRaises(LocalIOError):
            self.store.list("v1")
        write_json(self.store.path, {"version": 99, "bookmarks": []})
        with self.assertRaises(LocalIOError):
            self.store.list("v1")

    def test_store_document_holds_no_url_or_token(self):
        monitor = self.monitor()
        self.bound(monitor)
        self.player.feed(100, sample(100, position_ms=30_000))
        self.assertIsNotNone(monitor.add_bookmark(100, "v1", "备注"))
        raw = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn("http", raw)
        self.assertNotIn("token", raw)
        self.assertEqual(json.loads(raw)["bookmarks"][0]["fid"], "v1")

    # -- monitor capture -------------------------------------------------

    def test_capture_reads_the_real_sample_not_the_snapshot(self):
        monitor = self.monitor()
        self.bound(monitor)
        self.player.feed(100, sample(100, position_ms=10_000))
        monitor._poll_once()
        self.assertEqual(monitor.snapshot()[0]["position_ms"], 10_000)
        # A newer real sample is not polled into the snapshot before the capture.
        self.player.feed(100, sample(100, position_ms=42_000))
        bookmark = monitor.add_bookmark(100, "v1", "精彩片段")
        self.assertIsNotNone(bookmark)
        self.assertEqual(bookmark["position_ms"], 42_000)
        self.assertEqual(bookmark["duration_ms"], 1_800_000)
        self.assertEqual(bookmark["fid"], "v1")
        self.assertEqual(bookmark["name"], "a.mp4")
        self.assertEqual(bookmark["size"], 100)
        self.assertEqual(bookmark["note"], "精彩片段")
        self.assertEqual(self.store.list("v1"), [bookmark])
        # The capture never touches the favorite/recent library.
        self.assertFalse(self.library.path.exists())

    def test_paused_player_still_captures(self):
        monitor = self.monitor()
        self.bound(monitor)
        self.player.feed(100, sample(100, position_ms=55_000, status=1))
        bookmark = monitor.add_bookmark(100, "v1", "暂停点")
        self.assertIsNotNone(bookmark)
        self.assertEqual(bookmark["position_ms"], 55_000)

    def test_capture_requires_stream_started(self):
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.player.feed(100, sample(100))
        self.assertIsNone(monitor.add_bookmark(100, "v1", "n"))
        self.assertEqual(self.store.list("v1"), [])

    def test_capture_fails_when_unbound_or_switched(self):
        monitor = self.monitor()
        self.assertIsNone(monitor.add_bookmark(100, "v1", "n"))  # never bound
        self.bound(monitor)
        self.player.feed(100, sample(100))
        self.assertIsNone(monitor.add_bookmark(100, "other", "n"))  # different FID
        self.assertIsNone(monitor.add_bookmark(999, "v1", "n"))  # different pid
        self.assertEqual(self.store.list("v1"), [])

    def test_capture_rejects_a_mismatched_identity(self):
        monitor = self.monitor()
        self.bound(monitor)
        self.player.feed(100, sample(100, name="a.mp4", position_ms=30_000))
        self.assertIsNone(monitor.add_bookmark(100, "v1", "n"))
        self.assertEqual(self.store.list("v1"), [])
        self.assertEqual(monitor.snapshot()[0]["state"], STATE_MISMATCH)

    def test_capture_rejects_zero_position_and_unknown_duration(self):
        monitor = self.monitor()
        self.bound(monitor)
        self.player.feed(100, sample(100, position_ms=0))
        self.assertIsNone(monitor.add_bookmark(100, "v1", "n"))
        self.player.feed(100, sample(100, position_ms=30_000, duration_ms=0))
        self.assertIsNone(monitor.add_bookmark(100, "v1", "n"))

    def test_capture_fails_when_the_same_fid_is_open_twice(self):
        monitor = self.monitor()
        monitor.bind(session(), 100)
        monitor.bind(session(), 101)
        monitor.stream_started("sess-1")
        self.player.feed(100, sample(100, position_ms=30_000))
        self.player.feed(101, sample(101, position_ms=30_000))
        self.assertIsNone(monitor.add_bookmark(100, "v1", "n"))
        self.assertEqual(self.store.list("v1"), [])

    def test_capture_ignores_a_manual_watched_mark(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.mark_watched("v1", "a.mp4", 100)
        self.assertIsNone(self.library.progress("v1", 100))
        monitor = self.monitor()
        self.bound(monitor)
        self.player.feed(100, sample(100, position_ms=30_000))
        self.assertIsNotNone(monitor.add_bookmark(100, "v1", "仍然可存"))

    def test_capture_rejects_an_over_long_note(self):
        monitor = self.monitor()
        self.bound(monitor)
        self.player.feed(100, sample(100))
        self.assertIsNone(monitor.add_bookmark(100, "v1", "中" * (NOTE_LIMIT + 1)))
        self.assertIsNone(monitor.add_bookmark(100, "v1", 123))  # type: ignore[arg-type]

    def test_capture_accepts_an_empty_note(self):
        monitor = self.monitor()
        self.bound(monitor)
        self.player.feed(100, sample(100, position_ms=30_000))
        bookmark = monitor.add_bookmark(100, "v1", "")
        self.assertIsNotNone(bookmark)
        self.assertEqual(bookmark["note"], "")
        self.assertEqual(self.store.list("v1")[0]["note"], "")

    def test_capture_rejects_a_stopped_or_unknown_status(self):
        monitor = self.monitor()
        self.bound(monitor)
        for status in (-1, 0, 3):
            self.player.feed(100, sample(100, position_ms=30_000, status=status))
            self.assertIsNone(monitor.add_bookmark(100, "v1", "n"))
        self.assertEqual(self.store.list("v1"), [])

    def test_capture_rejects_a_position_at_or_past_the_end(self):
        monitor = self.monitor()
        self.bound(monitor)
        self.player.feed(100, sample(100, position_ms=1_800_000, duration_ms=1_800_000))
        self.assertIsNone(monitor.add_bookmark(100, "v1", "n"))
        self.player.feed(100, sample(100, position_ms=2_000_000, duration_ms=1_800_000))
        self.assertIsNone(monitor.add_bookmark(100, "v1", "n"))
        self.assertEqual(self.store.list("v1"), [])

    def test_capture_exposes_a_storage_error_as_local_io_error(self):
        monitor = self.monitor(store=FailingStore(self.store.path))
        self.bound(monitor)
        self.player.feed(100, sample(100))
        with self.assertRaises(LocalIOError):
            monitor.add_bookmark(100, "v1", "n")

    def test_capture_exposes_a_corrupt_document_as_a_storage_error(self):
        write_json(self.store.path, {"version": 1, "bookmarks": [{"bogus": 1}]})
        monitor = self.monitor()
        self.bound(monitor)
        self.player.feed(100, sample(100))
        with self.assertRaises(LocalIOError):
            monitor.add_bookmark(100, "v1", "n")

    # -- monitor jump ----------------------------------------------------

    def test_jump_seeks_and_confirms(self):
        bookmark = self.store.add("v1", "a.mp4", 100, 90_000, 1_800_000, "标记")
        player = SeekingPlayer()
        monitor = self.monitor(player=player)
        self.bound(monitor)
        player.feed(100, sample(100, position_ms=5_000))
        monitor._poll_once()
        player.feed(100, sample(100, position_ms=5_000))
        self.assertTrue(monitor.jump_bookmark(100, "v1", bookmark["id"]))
        self.assertEqual(player.seeks, [(100, logical("v1"), 90_000)])

    def test_jump_wins_over_the_initial_auto_resume(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 120_000, 1_800_000)
        bookmark = self.store.add("v1", "a.mp4", 100, 90_000, 1_800_000, "标记")
        player = SeekingPlayer()
        monitor = self.monitor(player=player)
        self.bound(monitor)
        # No poll has run yet, so the initial auto-resume is still pending.
        player.feed(100, sample(100, position_ms=5_000))
        self.assertTrue(monitor.jump_bookmark(100, "v1", bookmark["id"]))
        self.assertEqual(player.seeks, [(100, logical("v1"), 90_000)])
        # A later poll must not seek back to the resume point over the jump.
        monitor._poll_once()
        self.assertEqual(player.seeks, [(100, logical("v1"), 90_000)])
        self.assertEqual(self.library.progress("v1", 100)["position_ms"], 120_000)

    def test_jump_then_paused_close_persists_the_jumped_point(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 60_000, 1_800_000)
        bookmark = self.store.add("v1", "a.mp4", 100, 30_000, 1_800_000, "point")
        player = SeekingPlayer()
        monitor = self.monitor(player=player)
        self.bound(monitor)
        # A paused player sits on the old saved point; the jump seeks it to 30 s.
        player.feed(100, sample(100, position_ms=60_000, status=1))
        self.assertTrue(monitor.jump_bookmark(100, "v1", bookmark["id"]))
        self.assertEqual(player.seeks, [(100, logical("v1"), 30_000)])
        # The player stays paused on the jumped-to point: closing must flush that
        # point instead of leaving the stale 60 s resume value on disk.
        player.feed(100, sample(100, position_ms=30_000, status=1))
        monitor.close()
        self.assertEqual(self.library.progress("v1", 100)["position_ms"], 30_000)

    def test_jump_then_paused_poll_persists_the_jumped_point(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 60_000, 1_800_000)
        bookmark = self.store.add("v1", "a.mp4", 100, 30_000, 1_800_000, "point")
        player = SeekingPlayer()
        monitor = self.monitor(player=player)
        self.bound(monitor)
        player.feed(100, sample(100, position_ms=60_000, status=1))
        self.assertTrue(monitor.jump_bookmark(100, "v1", bookmark["id"]))
        # The paused collect must still see the jumped point as unsaved and write
        # it, rather than trusting a claim that the jump already stored it.
        player.feed(100, sample(100, position_ms=30_000, status=1))
        monitor._poll_once()
        self.assertEqual(self.library.progress("v1", 100)["position_ms"], 30_000)

    def test_jump_then_paused_close_keeps_a_watched_point_suppressed(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 60_000, 1_800_000)
        self.library.mark_watched("v1", "a.mp4", 100)
        bookmark = self.store.add("v1", "a.mp4", 100, 30_000, 1_800_000, "point")
        player = SeekingPlayer()
        monitor = self.monitor(player=player)
        self.bound(monitor)
        player.feed(100, sample(100, position_ms=60_000, status=1))
        self.assertTrue(monitor.jump_bookmark(100, "v1", bookmark["id"]))
        player.feed(100, sample(100, position_ms=30_000, status=1))
        monitor.close()
        # A manual watched mark still refuses both the resume point and the write.
        self.assertIsNone(self.library.progress("v1", 100))
        document = json.loads(self.library.path.read_text(encoding="utf-8"))
        self.assertEqual(document["recent"][0]["position_ms"], 60_000)

    def test_jump_refuses_a_changed_size(self):
        bookmark = self.store.add("v1", "a.mp4", 100, 90_000, 1_800_000, "标记")
        player = SeekingPlayer()
        monitor = self.monitor(player=player)
        monitor.bind(session(size=200), 100)
        monitor.stream_started("sess-1")
        player.feed(100, sample(100, position_ms=5_000))
        self.assertFalse(monitor.jump_bookmark(100, "v1", bookmark["id"]))
        self.assertEqual(player.seeks, [])

    def test_jump_refuses_a_changed_duration(self):
        bookmark = self.store.add("v1", "a.mp4", 100, 90_000, 1_800_000, "标记")
        player = SeekingPlayer()
        monitor = self.monitor(player=player)
        self.bound(monitor)
        player.feed(100, sample(100, position_ms=5_000, duration_ms=2_400_000))
        self.assertFalse(monitor.jump_bookmark(100, "v1", bookmark["id"]))
        self.assertEqual(player.seeks, [])

    def test_jump_refuses_a_switched_file(self):
        bookmark = self.store.add("v1", "a.mp4", 100, 90_000, 1_800_000, "标记")
        player = SeekingPlayer()
        monitor = self.monitor(player=player)
        self.bound(monitor)
        player.feed(100, sample(100, name=logical("v2"), position_ms=5_000))
        self.assertFalse(monitor.jump_bookmark(100, "v1", bookmark["id"]))
        self.assertEqual(player.seeks, [])
        self.assertEqual(monitor.snapshot()[0]["state"], STATE_MISMATCH)
        self.assertEqual(monitor.snapshot()[0]["message"], MSG_MISMATCH)

    def test_jump_verification_respects_the_identity(self):
        bookmark = self.store.add("v1", "a.mp4", 100, 90_000, 1_800_000, "标记")
        monitor = self.monitor()
        self.bound(monitor)
        # The pre-sample matches, but the file switches during the seek.
        self.player.feed(
            100,
            sample(100, position_ms=5_000),
            sample(100, name=logical("v2"), position_ms=90_000),
        )
        self.assertFalse(monitor.jump_bookmark(100, "v1", bookmark["id"]))
        self.assertEqual(self.player.seeks, [(100, logical("v1"), 90_000)])

    def test_jump_reports_a_failed_seek(self):
        bookmark = self.store.add("v1", "a.mp4", 100, 90_000, 1_800_000, "标记")
        player = SeekingPlayer()
        player.seek_ok = False
        monitor = self.monitor(player=player)
        self.bound(monitor)
        player.feed(100, sample(100, position_ms=5_000))
        self.assertFalse(monitor.jump_bookmark(100, "v1", bookmark["id"]))
        self.assertEqual(player.seeks, [(100, logical("v1"), 90_000)])

    def test_jump_fails_for_an_unknown_or_foreign_bookmark(self):
        monitor = self.monitor()
        self.bound(monitor)
        self.player.feed(100, sample(100))
        self.assertFalse(monitor.jump_bookmark(100, "v1", "missing"))
        other = self.store.add("v2", "b.mp4", 100, 90_000, 1_800_000, "x")
        self.assertFalse(monitor.jump_bookmark(100, "v1", other["id"]))

    def test_jump_fails_after_a_rebind(self):
        bookmark = self.store.add("v1", "a.mp4", 100, 90_000, 1_800_000, "标记")
        monitor = self.monitor()
        self.bound(monitor)
        self.player.feed(100, sample(100))
        monitor.bind(session(fid="v2"), 100)  # the player switched to another file
        self.assertFalse(monitor.jump_bookmark(100, "v1", bookmark["id"]))
        self.player.feed(100, sample(100, name=logical("v2")))
        self.assertFalse(monitor.jump_bookmark(100, "v1", bookmark["id"]))

    def test_jump_fails_unbound_and_suspended(self):
        bookmark = self.store.add("v1", "a.mp4", 100, 90_000, 1_800_000, "标记")
        monitor = self.monitor()
        self.assertFalse(monitor.jump_bookmark(100, "v1", bookmark["id"]))  # never bound
        monitor.bind(session(), 100)
        monitor.bind(session(), 101)
        monitor.stream_started("sess-1")
        self.player.feed(100, sample(100, position_ms=5_000))
        self.player.feed(101, sample(101, position_ms=5_000))
        self.assertFalse(monitor.jump_bookmark(100, "v1", bookmark["id"]))
        self.assertEqual(self.player.seeks, [])

    def test_jump_refuses_a_target_at_or_past_the_end(self):
        bookmark = self.store.add("v1", "a.mp4", 100, 1_800_000, 1_800_000, "片尾")
        player = SeekingPlayer()
        monitor = self.monitor(player=player)
        self.bound(monitor)
        player.feed(100, sample(100, position_ms=5_000, duration_ms=1_800_000))
        self.assertFalse(monitor.jump_bookmark(100, "v1", bookmark["id"]))
        self.assertEqual(player.seeks, [])

    def test_jump_exposes_a_storage_error_as_local_io_error(self):
        monitor = self.monitor(store=FailingStore(self.store.path))
        self.bound(monitor)
        self.player.feed(100, sample(100))
        with self.assertRaises(LocalIOError):
            monitor.jump_bookmark(100, "v1", "any")

    # -- worker thread, timeout, close -----------------------------------

    def test_capture_runs_on_the_worker_thread(self):
        player = RecordingPlayer()
        monitor = self.monitor(player=player, start=True, poll_interval=3600.0)
        try:
            self.bound(monitor)
            player.feed(100, sample(100, position_ms=30_000))
            self.assertIsNotNone(monitor.add_bookmark(100, "v1", "n"))
            self.assertEqual(player.threads, {"quark-potplayer-playback"})
        finally:
            monitor.close()

    def test_capture_timeout_never_writes_after_the_sample_releases(self):
        player = BlockingPlayer()
        monitor = self.monitor(player=player, start=True, poll_interval=3600.0)
        try:
            player.feed(100, sample(100, position_ms=30_000))
            self.bound(monitor)
            self.assertTrue(player.polled.wait(5.0))  # worker polled and is idle
            player.block.set()  # the next sample -- inside add_bookmark -- blocks
            with mock.patch.object(playback, "BOOKMARK_TIMEOUT", 0.05):
                self.assertIsNone(monitor.add_bookmark(100, "v1", "n"))
            self.assertEqual(self.store.list("v1"), [])
            player.release.set()  # the blocked sample finally returns
        finally:
            player.release.set()
            monitor.close()  # joins the worker: a late write would have landed
        self.assertEqual(self.store.list("v1"), [])

    def test_jump_timeout_never_seeks_after_the_sample_releases(self):
        bookmark = self.store.add("v1", "a.mp4", 100, 90_000, 1_800_000, "标记")
        player = BlockingPlayer()
        monitor = self.monitor(player=player, start=True, poll_interval=3600.0)
        try:
            player.feed(100, sample(100, position_ms=5_000))
            self.bound(monitor)
            self.assertTrue(player.polled.wait(5.0))  # worker polled and is idle
            player.block.set()  # the next sample -- inside jump_bookmark -- blocks
            with mock.patch.object(playback, "BOOKMARK_TIMEOUT", 0.05):
                self.assertFalse(monitor.jump_bookmark(100, "v1", bookmark["id"]))
            self.assertEqual(player.seeks, [])
            player.release.set()  # the blocked sample finally returns
        finally:
            player.release.set()
            monitor.close()  # joins the worker: a late seek would have landed
        self.assertEqual(player.seeks, [])

    def test_capture_never_writes_after_a_clear(self):
        player = BlockingPlayer()
        monitor = self.monitor(player=player, start=True, poll_interval=3600.0)
        results: list[dict | None] = []
        try:
            player.feed(100, sample(100, position_ms=30_000))
            self.bound(monitor)
            self.assertTrue(player.polled.wait(5.0))  # worker polled and is idle
            player.block.set()
            worker = threading.Thread(target=lambda: results.append(monitor.add_bookmark(100, "v1", "n")))
            worker.start()
            self.assertTrue(player.blocked.wait(5.0))  # the capture sample is in flight
            monitor.clear()  # bumps the generation/epoch while the sample blocks
            player.release.set()
            worker.join(5.0)
        finally:
            player.release.set()
            monitor.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [None])
        self.assertEqual(self.store.list("v1"), [])

    def test_close_unblocks_a_queued_capture(self):
        monitor = self.monitor()
        self.bound(monitor)
        self.player.feed(100, sample(100))
        monitor.close()
        # After close the monitor refuses new work instead of hanging.
        self.assertIsNone(monitor.add_bookmark(100, "v1", "n"))
        self.assertFalse(monitor.jump_bookmark(100, "v1", "id"))


if __name__ == "__main__":
    unittest.main()
