"""Deterministic tests for the native-player progress monitor.

Every test drives the monitor through the private synchronous seam
(:meth:`PlaybackMonitor._poll_once`) with a fake adapter and fake clock, so no
real player, thread, or timing is involved. Samples always carry the *logical*
identity the extension hands the player (``https://quark.invalid/...``) or an
exact registered address; a bare basename is never an accepted identity.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from quark_cli.errors import LocalIOError, UsageError
from quark_cli.potplayer import playback
from quark_cli.potplayer.library_state import Library
from quark_cli.potplayer.playback import (
    MSG_MISMATCH,
    MSG_NATIVE_FAILED,
    MSG_RESTORED,
    MSG_SEEK_FAILED,
    MSG_STORAGE_FAILED,
    MSG_SUSPENDED,
    PlaybackMonitor,
    STATE_ERROR,
    STATE_MISMATCH,
    STATE_PLAYING,
    STATE_STOPPED,
    STATE_SUSPENDED,
    STATE_UNAVAILABLE,
)
from quark_cli.potplayer.stream import PlaySession


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
        self.closed = False
        self.sample_calls = 0
        self.sample_error: BaseException | None = None
        self.seek_ok = True

    def feed(self, player_pid: int, *samples: FakeSample | None) -> None:
        self.queued[player_pid] = list(samples)

    def sample(self, player_pid: int) -> FakeSample | None:
        self.sample_calls += 1
        if self.sample_error is not None:
            raise self.sample_error
        queue = self.queued.get(player_pid)
        if not queue:
            return None
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def seek(self, player_pid: int, expected_file_name: str, position_ms: int) -> bool:
        self.seeks.append((player_pid, expected_file_name, position_ms))
        return self.seek_ok

    def close(self) -> None:
        self.closed = True


class BlockingPlayer(FakePlayer):
    """A player whose ``sample`` blocks until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def sample(self, player_pid: int) -> FakeSample | None:
        self.entered.set()
        self.release.wait(5.0)
        return super().sample(player_pid)


def session(fid: str = "v1", name: str = "a.mp4", size: int = 100, sid: str = "sess-1") -> PlaySession:
    return PlaySession(session=sid, fid=fid, name=name, size=size, url="https://origin/" + sid, created=0.0, last_used=0.0)


def sample(player_pid: int, name: str | None = None, position_ms: int = 0, duration_ms: int = 1_800_000, status: int = 2) -> FakeSample:
    return FakeSample(player_pid, logical("v1") if name is None else name, position_ms, duration_ms, status)


class Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RecordingLibrary(Library):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.saves: list[tuple[str, int, int, int]] = []
        self.resets: list[str] = []
        self.hook = None

    def save_progress(self, fid, size, position_ms, duration_ms):  # type: ignore[override]
        self.saves.append((fid, size, position_ms, duration_ms))
        return super().save_progress(fid, size, position_ms, duration_ms)

    def reset_progress(self, fid):  # type: ignore[override]
        self.resets.append(fid)
        return super().reset_progress(fid)

    def progress(self, fid, size):  # type: ignore[override]
        if self.hook is not None:
            self.hook()
        return super().progress(fid, size)


class FailingLibrary(Library):
    def save_progress(self, fid, size, position_ms, duration_ms):  # type: ignore[override]
        raise LocalIOError("disk down")

    def progress(self, fid, size):  # type: ignore[override]
        raise LocalIOError("disk down")


class MonitorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="quark-playback-"))
        self.path = self.directory / "library.json"
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.library = Library(self.path)
        self.player = FakePlayer()
        self.clock = Clock()

    def monitor(self, *, library=None, native_factory=None) -> PlaybackMonitor:
        return PlaybackMonitor(
            library if library is not None else self.library,
            native_factory=native_factory or (lambda: self.player),
            clock=self.clock,
            start=False,
        )

    def start(self, monitor: PlaybackMonitor, player_pid: int = 100, sid: str = "sess-1") -> None:
        monitor.stream_started(sid)

    # -- binding and gating ---------------------------------------------

    def test_snapshot_exposes_no_url_or_session(self):
        monitor = self.monitor()
        monitor.bind(session(), 100)
        view = monitor.snapshot()
        self.assertEqual(len(view), 1)
        self.assertEqual(set(view[0]), {"player_pid", "fid", "name", "position_ms", "duration_ms", "state", "message"})
        self.assertNotIn("url", view[0])

    def test_sampling_waits_for_stream_started(self):
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.player.feed(100, sample(100))
        monitor._poll_once()
        self.assertEqual(self.player.sample_calls, 0)
        self.assertEqual(monitor.snapshot()[0]["state"], "waiting")

    def test_basename_never_identifies_the_file(self):
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, name="a.mp4", position_ms=30_000))
        monitor._poll_once()
        self.assertEqual(monitor.snapshot()[0]["state"], STATE_MISMATCH)
        self.assertEqual(self.player.seeks, [])
        self.assertEqual(self.library.recent(), [])

    def test_mismatched_filename_is_never_written(self):
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, name=logical("v2"), position_ms=90_000))
        monitor._poll_once()
        self.assertEqual(monitor.snapshot()[0]["state"], STATE_MISMATCH)
        self.assertEqual(monitor.snapshot()[0]["message"], MSG_MISMATCH)
        self.assertEqual(self.library.recent(), [])

    def test_both_logical_prefixes_are_accepted(self):
        for index, prefix in enumerate(("video", "play")):
            monitor = self.monitor()
            monitor.bind(session(), 100 + index)
            self.start(monitor, player_pid=100 + index)
            self.player.feed(100 + index, sample(100 + index, name=f"https://quark.invalid/{prefix}/v1", position_ms=30_000))
            monitor._poll_once()
            self.assertEqual(monitor.snapshot()[0]["state"], STATE_PLAYING, prefix)

    def test_registered_local_stream_url_is_matched_exactly(self):
        url = "/v1/stream/sess-1/a.mp4"
        monitor = self.monitor()
        monitor.bind(session(), 100, url)
        self.start(monitor)
        self.player.feed(100, sample(100, name=url, position_ms=30_000))
        monitor._poll_once()
        self.assertEqual(monitor.snapshot()[0]["state"], STATE_PLAYING)
        # The logical URL for the same FID is equally valid.
        self.player.feed(100, sample(100, name=logical("v1"), position_ms=31_000))
        monitor._poll_once()
        self.assertEqual(monitor.snapshot()[0]["state"], STATE_PLAYING)
        # A near miss is not.
        self.player.feed(100, sample(100, name=url + "x", position_ms=32_000))
        monitor._poll_once()
        self.assertEqual(monitor.snapshot()[0]["state"], STATE_MISMATCH)

    def test_bind_url_validation(self):
        monitor = self.monitor()
        monitor.bind(session(), 100, "https://quark.invalid/video/v1")
        with self.assertRaises(UsageError):
            monitor.bind(session(), 101, "https://quark.invalid/video/other")
        monitor.bind(session(), 102, "/v1/stream/sess-1/a.mp4")
        with self.assertRaises(UsageError):
            monitor.bind(session(), 103, "/v1/stream/other/a.mp4")
        with self.assertRaises(UsageError):
            monitor.bind(session(), 104, "https://example.invalid/a.mp4")

    def test_bindings_are_capped(self):
        monitor = self.monitor()
        for index in range(130):
            monitor.bind(session(fid=f"v{index}", sid=f"s{index}"), 100 + index)
        self.assertLessEqual(len(monitor.snapshot()), 128)

    # -- resume ----------------------------------------------------------

    def test_initial_zero_never_overwrites_saved_and_seeks_once(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 60_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=0), sample(100, position_ms=60_000))
        monitor._poll_once()
        self.assertEqual(self.player.seeks, [(100, logical("v1"), 60_000)])
        self.assertEqual(self.library.progress("v1", 100)["position_ms"], 60_000)
        # Periodic save after ten seconds writes the real position.
        self.clock.advance(10.0)
        self.player.feed(100, sample(100, position_ms=120_000))
        monitor._poll_once()
        self.assertEqual(self.library.progress("v1", 100)["position_ms"], 120_000)
        self.assertEqual(len(self.player.seeks), 1)

    def test_saved_below_fifteen_seconds_is_not_resumed(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 10_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=0))
        monitor._poll_once()
        self.assertEqual(self.player.seeks, [])

    def test_saved_near_end_is_not_resumed(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 99_000, 100_000)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, duration_ms=100_000, position_ms=0))
        monitor._poll_once()
        self.assertEqual(self.player.seeks, [])

    def test_player_moved_past_initial_five_seconds_is_respected(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 60_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=8_000))
        monitor._poll_once()
        self.assertEqual(self.player.seeks, [])
        self.assertEqual(self.library.progress("v1", 100)["position_ms"], 60_000)

    def test_changed_duration_blocks_restore(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 60_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, duration_ms=2_400_000, position_ms=0))
        monitor._poll_once()
        self.assertEqual(self.player.seeks, [])

    def test_changed_size_blocks_restore(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 60_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(size=200), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=0))
        monitor._poll_once()
        self.assertEqual(self.player.seeks, [])

    def test_failed_seek_keeps_the_saved_target(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 60_000, 1_800_000)
        self.player.seek_ok = False
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=0))
        monitor._poll_once()
        self.assertEqual(self.player.seeks, [(100, logical("v1"), 60_000)])
        self.assertEqual(self.library.progress("v1", 100)["position_ms"], 60_000)

    def test_restore_verification_accepts_the_tolerance_edge(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        library.save_progress("v1", 100, 60_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=0), sample(100, position_ms=63_000))
        monitor._poll_once()
        self.assertEqual(monitor.snapshot()[0]["message"], MSG_RESTORED)

    def test_restore_verification_rejects_just_past_the_tolerance(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        library.save_progress("v1", 100, 60_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=0), sample(100, position_ms=63_001))
        monitor._poll_once()
        self.assertEqual(monitor.snapshot()[0]["message"], MSG_SEEK_FAILED)

    # -- writes ----------------------------------------------------------

    def test_pause_transition_saves_once_and_paused_repeat_does_not(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=30_000))
        monitor._poll_once()  # resolves the (empty) restore
        self.clock.advance(10.0)
        self.player.feed(100, sample(100, position_ms=30_000))
        monitor._poll_once()
        self.assertEqual(library.saves[-1], ("v1", 100, 30_000, 1_800_000))
        self.clock.advance(2.0)
        self.player.feed(100, sample(100, position_ms=33_000, status=1))
        monitor._poll_once()
        self.assertEqual(library.saves[-1], ("v1", 100, 33_000, 1_800_000))
        count = len(library.saves)
        self.clock.advance(2.0)
        self.player.feed(100, sample(100, position_ms=33_000, status=1))
        monitor._poll_once()
        self.assertEqual(len(library.saves), count)

    def test_paused_seek_is_saved_on_the_ten_second_cycle(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=30_000))
        monitor._poll_once()  # resolves the (empty) restore
        # Pausing writes at once, well inside the ten-second cycle.
        self.clock.advance(2.0)
        self.player.feed(100, sample(100, position_ms=30_000, status=1))
        monitor._poll_once()
        self.assertEqual(library.saves[-1], ("v1", 100, 30_000, 1_800_000))
        count = len(library.saves)
        # Dragged to a new position while still paused: no transition, and the
        # cycle has not elapsed since the last write, so nothing is written yet.
        self.clock.advance(2.0)
        self.player.feed(100, sample(100, position_ms=60_000, status=1))
        monitor._poll_once()
        self.assertEqual(len(library.saves), count)
        # Ten seconds after the pause write the cycle fires for the new point.
        self.clock.advance(10.0)
        self.player.feed(100, sample(100, position_ms=60_000, status=1))
        monitor._poll_once()
        self.assertEqual(library.saves[-1], ("v1", 100, 60_000, 1_800_000))
        count = len(library.saves)
        # The unchanged paused position never repeats the IO.
        self.clock.advance(10.0)
        self.player.feed(100, sample(100, position_ms=60_000, status=1))
        monitor._poll_once()
        self.assertEqual(len(library.saves), count)

    def test_zero_duration_sample_saves_nothing_and_keeps_playing(self):
        library = RecordingLibrary(self.path)
        self.library = library
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=30_000, duration_ms=0))
        monitor._poll_once()
        self.assertEqual(monitor.snapshot()[0]["state"], STATE_PLAYING)
        self.assertEqual(library.saves, [])
        self.assertEqual(self.player.seeks, [])

    def test_zero_position_sample_never_clears_saved_resume(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        library.save_progress("v1", 100, 60_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=0))
        monitor._poll_once()  # restore runs, target kept
        library.saves.clear()
        # A paused player that reports position 0 must not clear the saved point.
        self.player.feed(100, sample(100, position_ms=0, status=1))
        monitor._poll_once()
        self.assertEqual([saved for saved in library.saves if saved[2] == 0], [])
        self.assertEqual(library.progress("v1", 100)["position_ms"], 60_000)

    def test_last_good_survives_a_zero_sample_and_is_flushed_on_close(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=50_000))
        monitor._poll_once()  # no saved target: nothing to restore, keep 50 s
        self.player.feed(100, sample(100, position_ms=0, status=1))
        monitor._poll_once()  # a zero sample must not replace the last good point
        monitor.close()
        self.assertIn(("v1", 100, 50_000, 1_800_000), library.saves)
        self.assertEqual([saved for saved in library.saves if saved[2] == 0], [])

    def test_rebind_flushes_the_previous_good_sample(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=50_000))
        monitor._poll_once()
        monitor.bind(session(fid="v2", sid="sess-2"), 100)
        monitor._poll_once()
        self.assertIn(("v1", 100, 50_000, 1_800_000), library.saves)

    def test_close_flushes_last_good_and_destroys_receiver(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=70_000))
        monitor._poll_once()
        monitor.close()
        self.assertIn(("v1", 100, 70_000, 1_800_000), library.saves)
        self.assertTrue(self.player.closed)

    # -- queued-flush races ----------------------------------------------

    def test_queued_flush_is_dropped_after_clear(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=50_000))
        monitor._poll_once()
        monitor.bind(session(), 100)  # queues a flush of the 50 s point
        stale = list(monitor._pending)
        monitor._pending.clear()  # worker already snapshotted this task
        monitor.clear()
        library.saves.clear()
        for task in stale:
            monitor._apply_flush(task)
        self.assertEqual(library.saves, [])

    def test_queued_flush_is_not_applied_to_a_rebound_same_fid(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=50_000))
        monitor._poll_once()
        monitor.bind(session(), 100)  # rebound to the same FID: new owner
        stale = list(monitor._pending)
        monitor._pending.clear()
        library.saves.clear()
        for task in stale:
            monitor._apply_flush(task)
        self.assertEqual(library.saves, [])

    # -- same file / clear / restart -------------------------------------

    def test_same_fid_in_two_players_suspends_both(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 60_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(sid="s1"), 100)
        monitor.bind(session(sid="s2"), 200)
        monitor.stream_started("s1")
        monitor.stream_started("s2")
        self.player.feed(100, sample(100, position_ms=0))
        self.player.feed(200, sample(200, position_ms=0))
        monitor._poll_once()
        self.assertEqual(self.player.seeks, [])
        states = {item["player_pid"]: item for item in monitor.snapshot()}
        self.assertEqual(states[100]["state"], STATE_SUSPENDED)
        self.assertEqual(states[200]["state"], STATE_SUSPENDED)
        self.assertEqual(states[100]["message"], MSG_SUSPENDED)

    def test_second_same_fid_player_closing_unblocks_the_remaining_one(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 60_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(sid="s1"), 100)
        monitor.bind(session(sid="s2"), 200)
        monitor.stream_started("s1")
        monitor.stream_started("s2")
        self.player.feed(100, sample(100, position_ms=0))
        self.player.feed(200, sample(200, position_ms=0))
        monitor._poll_once()
        self.assertEqual(self.player.seeks, [])
        # The second window closes; the first is now the only live instance.
        self.player.feed(200, None)
        monitor._poll_once()
        self.assertEqual(self.player.seeks, [(100, logical("v1"), 60_000)])
        states = {item["player_pid"]: item for item in monitor.snapshot()}
        self.assertEqual(len(states), 1)
        self.assertEqual(states[100]["state"], STATE_PLAYING)

    def _suspended_pair(self, monitor, library):
        """Drive pid 100 to a suspended last-good point and clear recorded saves."""
        monitor.bind(session(sid="s1"), 100)
        self.start(monitor, sid="s1")
        self.player.feed(100, sample(100, position_ms=30_000))
        monitor._poll_once()  # restore done; 30 s last-good, no periodic write yet
        monitor.bind(session(sid="s2"), 200)
        monitor.stream_started("s2")
        self.player.feed(200, sample(200, position_ms=0))
        monitor._poll_once()  # both same-FID players are now suspended
        self.assertEqual(monitor.snapshot()[0]["state"], STATE_SUSPENDED)
        library.saves.clear()

    def test_close_never_flushes_a_suspended_binding(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        monitor = self.monitor()
        self._suspended_pair(monitor, library)
        monitor.close()
        self.assertEqual(library.saves, [])
        self.assertTrue(self.player.closed)

    def test_rebind_of_a_suspended_binding_never_flushes_its_sample(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        monitor = self.monitor()
        self._suspended_pair(monitor, library)
        # Same pid re-binds (new episode) while the suspended sample is stale.
        monitor.bind(session(fid="v2", name="b.mp4", sid="s3"), 100)
        monitor._poll_once()
        self.assertEqual(library.saves, [])

    def test_suspended_binding_writes_again_once_the_sibling_closes(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        monitor = self.monitor()
        self._suspended_pair(monitor, library)
        self.clock.advance(10.0)
        # The second window closes; pid 100 is the only live instance again.
        self.player.feed(200, None)
        self.player.feed(100, sample(100, position_ms=45_000))
        monitor._poll_once()
        self.assertEqual(monitor.snapshot()[0]["state"], STATE_PLAYING)
        self.assertIn(("v1", 100, 45_000, 1_800_000), library.saves)

    def test_different_fids_restore_independently(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 60_000, 1_800_000)
        self.library.record_recent("v2", "b.mp4", 200)
        self.library.save_progress("v2", 200, 90_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(sid="s1"), 100)
        monitor.bind(session(fid="v2", name="b.mp4", size=200, sid="s2"), 200)
        monitor.stream_started("s1")
        monitor.stream_started("s2")
        self.player.feed(100, sample(100, logical("v1"), 0), sample(100, logical("v1"), 60_000))
        self.player.feed(200, sample(200, logical("v2"), 0), sample(200, logical("v2"), 90_000))
        monitor._poll_once()
        self.assertEqual(sorted(pid for pid, _, _ in self.player.seeks), [100, 200])

    def test_stopped_status_flushes_last_good_and_stops(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=50_000), sample(100, position_ms=0, status=0))
        monitor._poll_once()
        monitor._poll_once()
        self.assertEqual(monitor.snapshot()[0]["state"], STATE_STOPPED)
        self.assertIn(("v1", 100, 50_000, 1_800_000), library.saves)

    def test_clear_blocks_old_bindings_until_a_new_bind(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=30_000))
        monitor._poll_once()
        monitor.clear()
        library.saves.clear()
        self.clock.advance(10.0)
        self.player.feed(100, sample(100, position_ms=60_000))
        monitor._poll_once()
        self.assertEqual(library.saves, [])
        # A fresh bind is allowed to record again.
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=70_000))
        monitor._poll_once()
        self.clock.advance(10.0)
        self.player.feed(100, sample(100, position_ms=80_000))
        monitor._poll_once()
        self.assertIn(("v1", 100, 80_000, 1_800_000), library.saves)

    def test_restart_seeks_to_zero_and_resets_progress(self):
        self.library.record_recent("v1", "a.mp4", 100)
        self.library.save_progress("v1", 100, 60_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=90_000))
        monitor._poll_once()
        self.assertTrue(monitor.restart(100, "v1"))
        monitor._poll_once()
        self.assertEqual(self.player.seeks, [(100, logical("v1"), 0)])
        self.assertIsNone(self.library.progress("v1", 100))

    def test_restart_unknown_binding_raises_and_changes_nothing(self):
        monitor = self.monitor()
        with self.assertRaises(UsageError):
            monitor.restart(999, "v1")
        monitor.bind(session(), 100)
        with self.assertRaises(UsageError):
            monitor.restart(100, "other")

    def test_restart_timeout_never_seeks_late(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        library.save_progress("v1", 100, 60_000, 1_800_000)
        player = BlockingPlayer()
        monitor = PlaybackMonitor(library, native_factory=lambda: player, clock=self.clock, start=True)
        self.addCleanup(monitor.close)
        monitor.bind(session(), 100)
        monitor.stream_started("sess-1")
        player.feed(100, sample(100, position_ms=90_000))
        # The worker blocks inside the sampled call, so the queued restart
        # expires before the seek; it must be cancelled, not run late.
        with mock.patch.object(playback, "RESTART_TIMEOUT", 0.05):
            self.assertFalse(monitor.restart(100, "v1"))
        player.release.set()
        monitor.close()  # joins the worker, proving the whole pass finished
        self.assertEqual(player.seeks, [])
        self.assertEqual(library.resets, [])
        self.assertIsNotNone(library.progress("v1", 100))

    def test_real_player_exit_flushes_the_last_good_sample_on_the_next_poll(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=50_000))
        monitor._poll_once()  # no saved target: nothing to restore, keep 50 s
        # The window is gone: no sample at all, no is_alive probe on this fake.
        self.player.feed(100, None)
        monitor._poll_once()
        self.assertEqual(monitor.snapshot(), [])
        self.assertEqual(library.saves, [("v1", 100, 50_000, 1_800_000)])

    # -- failures --------------------------------------------------------

    def test_native_failure_is_reported_without_raising(self):
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.sample_error = RuntimeError("boom")
        monitor._poll_once()
        view = monitor.snapshot()[0]
        self.assertEqual(view["state"], STATE_ERROR)
        self.assertEqual(view["message"], MSG_NATIVE_FAILED)

    def test_storage_failure_stops_writes_without_crashing(self):
        library = FailingLibrary(self.path)
        monitor = self.monitor(library=library)
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=0))
        monitor._poll_once()
        view = monitor.snapshot()[0]
        self.assertEqual(view["state"], STATE_ERROR)
        self.assertEqual(view["message"], MSG_STORAGE_FAILED)
        self.player.feed(100, sample(100, position_ms=120_000))
        self.clock.advance(10.0)
        monitor._poll_once()  # no exception, still no writes

    def test_unsupported_native_does_not_crash(self):
        monitor = PlaybackMonitor(self.library, native_factory=None, clock=self.clock, start=False)
        monitor.bind(session(), 100)
        self.start(monitor)
        monitor._poll_once()
        self.assertEqual(monitor.snapshot()[0]["state"], STATE_UNAVAILABLE)
        monitor.close()

    def test_stale_generation_after_slow_disk_read_aborts_restore(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        library.save_progress("v1", 100, 60_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=0))
        # The disk read races a switch that replaces the binding.
        library.hook = lambda: monitor.bind(session(sid="sess-2"), 100)
        monitor._poll_once()
        self.assertEqual(self.player.seeks, [])

    def test_switch_to_a_new_fid_uses_that_fid_s_progress(self):
        library = RecordingLibrary(self.path)
        self.library = library
        library.record_recent("v1", "a.mp4", 100)
        library.save_progress("v1", 100, 60_000, 1_800_000)
        library.record_recent("v2", "b.mp4", 100)
        library.save_progress("v2", 100, 30_000, 1_800_000)
        monitor = self.monitor()
        monitor.bind(session(), 100)
        self.start(monitor)
        self.player.feed(100, sample(100, position_ms=0), sample(100, position_ms=60_000))
        monitor._poll_once()
        self.assertEqual(monitor.snapshot()[0]["message"], MSG_RESTORED)
        # Switch the same window to the other registered video.
        monitor.bind(session(fid="v2", name="b.mp4", sid="sess-2"), 100)
        monitor.stream_started("sess-2")
        self.player.feed(100, sample(100, logical("v2"), 0))
        monitor._poll_once()
        self.assertIn((100, logical("v2"), 30_000), self.player.seeks)
        self.assertEqual(library.progress("v1", 100)["position_ms"], 60_000)
        self.assertEqual(library.progress("v2", 100)["position_ms"], 30_000)


if __name__ == "__main__":
    unittest.main()
