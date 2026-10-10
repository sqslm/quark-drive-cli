"""Deterministic tests for the dependency-free native PotPlayer reader.

The Windows backend is simulated through the injectable ``api`` seam, so the
whole protocol (pid/class lookup, coherent filename check, seek gating, timeout
handling, 64-bit WM_COPYDATA layout) is exercised without a real player. One
opt-in smoke checks the real ``ctypes`` backend against an unreachable pid only,
so it never touches a user's live player.
"""

from __future__ import annotations

import ctypes
import sys
import unittest

from quark_cli.potplayer import native_player
from quark_cli.potplayer.native_player import (
    MSG_DURATION,
    MSG_POSITION,
    MSG_STATUS,
    SUPPORTED,
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    NativePlayer,
)

URL_A = "https://quark.invalid/video/v1"
URL_B = "https://quark.invalid/play/v2"
FULL_PATH = "C:\\Users\\视频 库\\我的 电影 (2024)\\clip a.mp4"


class FakeApi:
    """Scripted stand-in for the Win32 backend.

    ``query_filename`` consumes a queue but keeps repeating its last value, so a
    stable never-ending name is expressed with a single-element list while a
    mid-read switch is expressed with ``[URL_A, URL_B]``.
    """

    def __init__(self, windows=None, filenames=None, numbers=None) -> None:
        self.windows = dict(windows if windows is not None else {100: 555})
        self.filename_queue = list(filenames if filenames is not None else [])
        self.numbers = dict(numbers if numbers is not None else {})
        self.set_calls: list[tuple[int, int]] = []
        self.closed = False

    def find_window(self, pid: int):
        return self.windows.get(int(pid))

    def query_filename(self, hwnd: int, pid: int):
        queue = self.filename_queue
        if not queue:
            return None
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def query_number(self, hwnd: int, pid: int, command: int):
        return self.numbers.get(command)

    def set_position(self, hwnd: int, pid: int, position_ms: int):
        self.set_calls.append((int(pid), int(position_ms)))
        return int(position_ms)

    def close(self) -> None:
        self.closed = True


def numbers(duration=95_000, position=0, status=STATUS_RUNNING):
    values = {MSG_POSITION: position, MSG_STATUS: status}
    if duration is not None:
        values[MSG_DURATION] = duration
    return values


def player(filenames, numbers_=None, **kwargs) -> tuple[NativePlayer, FakeApi]:
    api = FakeApi(filenames=filenames, numbers=numbers_ if numbers_ is not None else numbers(), **kwargs)
    return NativePlayer(api=api), api


class DecodeNameTests(unittest.TestCase):
    def test_trailing_nul_is_the_only_stripped_byte(self):
        self.assertEqual(native_player._decode_name(URL_A.encode("utf-8") + b"\x00"), URL_A)
        self.assertEqual(native_player._decode_name((" " + URL_A).encode("utf-8")), " " + URL_A)

    def test_full_utf8_path_is_preserved(self):
        raw = FULL_PATH.encode("utf-8") + b"\x00\x00"
        self.assertEqual(native_player._decode_name(raw), FULL_PATH)

    def test_malformed_empty_and_nul_only_are_dropped(self):
        self.assertIsNone(native_player._decode_name(b"\xff\xfe\x00"))
        self.assertIsNone(native_player._decode_name(b""))
        self.assertIsNone(native_player._decode_name(b"\x00\x00"))


class SampleTests(unittest.TestCase):
    def test_full_logical_url_is_returned_verbatim(self):
        np, _ = player([URL_A, URL_A], numbers(position=1_234))
        sample = np.sample(100)
        self.assertIsNotNone(sample)
        self.assertEqual(sample.file_name, URL_A)
        self.assertEqual(sample.player_pid, 100)
        self.assertEqual(sample.position_ms, 1_234)
        self.assertEqual(sample.duration_ms, 95_000)
        self.assertEqual(sample.status, STATUS_RUNNING)

    def test_full_path_is_not_reduced_to_a_basename(self):
        np, _ = player([FULL_PATH, FULL_PATH])
        sample = np.sample(100)
        self.assertEqual(sample.file_name, FULL_PATH)
        self.assertNotEqual(sample.file_name, "clip a.mp4")

    def test_filename_change_mid_read_yields_none(self):
        np, _ = player([URL_A, URL_B])
        self.assertIsNone(np.sample(100))

    def test_empty_filename_yields_none(self):
        np, _ = player([None, None])
        self.assertIsNone(np.sample(100))

    def test_unknown_status_yields_none(self):
        for status in (0, 3, 7):
            np, _ = player([URL_A, URL_A], numbers(status=status))
            self.assertIsNone(np.sample(100), f"status={status}")

    def test_stopped_status_is_a_coherent_not_ready_sample(self):
        # A live player that has not finished loading answers 0x5006 with -1;
        # that must be reported as a sample (status -1), never as "gone".
        np, _ = player([URL_A, URL_A], numbers(position=0, status=STATUS_STOPPED))
        sample = np.sample(100)
        self.assertIsNotNone(sample)
        self.assertEqual(sample.status, STATUS_STOPPED)
        self.assertEqual(sample.duration_ms, 95_000)

    def test_zero_or_negative_duration_reports_not_ready(self):
        for duration in (0, -1):
            np, _ = player([URL_A, URL_A], numbers(duration=duration))
            sample = np.sample(100)
            self.assertIsNotNone(sample, f"duration={duration}")
            self.assertEqual(sample.duration_ms, 0, f"duration={duration}")

    def test_missing_duration_yields_none(self):
        np, _ = player([URL_A, URL_A], numbers(duration=None))
        self.assertIsNone(np.sample(100))

    def test_negative_position_yields_none(self):
        np, _ = player([URL_A, URL_A], numbers(position=-10))
        self.assertIsNone(np.sample(100))

    def test_unknown_pid_yields_none(self):
        np, _ = player([URL_A, URL_A], windows={})
        self.assertIsNone(np.sample(100))

    def test_paused_is_a_valid_sample(self):
        np, _ = player([URL_A, URL_A], numbers(status=STATUS_PAUSED))
        self.assertIsNotNone(np.sample(100))


class SeekTests(unittest.TestCase):
    def test_seek_confirms_within_tolerance(self):
        np, api = player([URL_A, URL_A, URL_A], numbers(position=29_000))
        self.assertTrue(np.seek(100, URL_A, 30_000))
        self.assertEqual(api.set_calls, [(100, 30_000)])

    def test_seek_clamps_target_to_duration(self):
        np, api = player([URL_A, URL_A, URL_A], numbers(position=95_000))
        self.assertTrue(np.seek(100, URL_A, 200_000))
        self.assertEqual(api.set_calls, [(100, 95_000)])

    def test_seek_refuses_when_filename_changed_before_seek(self):
        np, api = player([URL_A, URL_B], numbers(position=29_000))
        self.assertFalse(np.seek(100, URL_A, 30_000))
        self.assertEqual(api.set_calls, [])

    def test_seek_refuses_when_player_shows_another_file(self):
        np, api = player([URL_B, URL_B], numbers(position=29_000))
        self.assertFalse(np.seek(100, URL_A, 30_000))
        self.assertEqual(api.set_calls, [])

    def test_seek_refuses_when_filename_switches_during_confirm(self):
        np, api = player([URL_A, URL_A, URL_B], numbers(position=30_000))
        self.assertFalse(np.seek(100, URL_A, 30_000))
        self.assertEqual(api.set_calls, [(100, 30_000)])

    def test_timeout_before_duration_never_seeks(self):
        for duration in (None, 0):
            np, api = player([URL_A, URL_A], numbers(duration=duration))
            self.assertFalse(np.seek(100, URL_A, 30_000), f"duration={duration}")
            self.assertEqual(api.set_calls, [])

    def test_start_timeout_on_first_filename_never_seeks(self):
        np, api = player([None, None])
        self.assertFalse(np.seek(100, URL_A, 30_000))
        self.assertEqual(api.set_calls, [])

    def test_position_that_never_confirms_fails(self):
        np, api = player([URL_A, URL_A, URL_A], numbers(position=0))
        self.assertFalse(np.seek(100, URL_A, 30_000))
        self.assertEqual(api.set_calls, [(100, 30_000)])

    def test_invalid_arguments_never_seek(self):
        np, api = player([URL_A, URL_A])
        self.assertFalse(np.seek(100, "", 30_000))
        self.assertFalse(np.seek(100, URL_A, -1))
        self.assertFalse(np.seek(999, URL_A, 30_000))
        self.assertEqual(api.set_calls, [])


class LivenessTests(unittest.TestCase):
    def test_alive_tracks_the_owned_window_only(self):
        np, _ = player([URL_A, URL_A])
        self.assertTrue(np.is_alive(100))
        self.assertFalse(np.is_alive(999))

    def test_alive_is_false_without_a_window_or_backend(self):
        np, _ = player([URL_A, URL_A], windows={})
        self.assertFalse(np.is_alive(100))

    def test_closed_player_is_not_alive(self):
        np, _ = player([URL_A, URL_A])
        np.close()
        self.assertFalse(np.is_alive(100))

    def test_alive_does_not_query_the_player(self):
        # A single queued name would be consumed by a query; is_alive must not
        # touch it, so a following sample still reads the real identity.
        np, _ = player([URL_A, URL_A])
        self.assertTrue(np.is_alive(100))
        self.assertIsNotNone(np.sample(100))


class LifecycleTests(unittest.TestCase):
    def test_close_is_idempotent_and_stops_sampling(self):
        np, api = player([URL_A, URL_A])
        np.close()
        np.close()
        self.assertTrue(api.closed)
        self.assertIsNone(np.sample(100))


class PlatformTests(unittest.TestCase):
    def test_import_is_safe_and_supported_flag_matches_platform(self):
        self.assertIs(SUPPORTED, sys.platform == "win32")
        self.assertIs(native_player.SUPPORTED, SUPPORTED)

    @unittest.skipUnless(SUPPORTED, "Win32 backend only")
    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit only")
    def test_copy_data_structure_is_pointer_sized(self):
        struct = native_player._CopyDataStruct
        self.assertEqual(ctypes.sizeof(struct), 24)
        self.assertIs(struct._fields_[0][1], ctypes.c_size_t)
        self.assertIs(struct._fields_[2][1], ctypes.c_void_p)

    @unittest.skipUnless(SUPPORTED, "Win32 backend only")
    def test_real_backend_against_an_unreachable_pid(self):
        # Never touches a live player: the pid below owns no window.
        np = NativePlayer()
        try:
            self.assertIsNone(np.sample(0x7FFFFFF0))
            self.assertFalse(np.is_alive(0x7FFFFFF0))
            self.assertFalse(np.seek(0x7FFFFFF0, URL_A, 1_000))
        finally:
            np.close()


@unittest.skipUnless(SUPPORTED, "Win32 backend only")
class ReceiverCallbackLifetimeTests(unittest.TestCase):
    """The registered window class must keep one live ``WNDPROC`` callback.

    A timed-out receiver is destroyed and rebuilt, but ``RegisterClassExW``
    reports an already-registered class instead of swapping in a new proc, so
    the backend has to reuse a single callback object for its whole lifetime.
    The test builds the real backend's own hidden receiver window only; it never
    looks at, or sends anything to, a player window.
    """

    def test_receiver_rebuild_reuses_the_kept_callback(self):
        api = native_player._WinApi()
        try:
            first = api._receiver()
            callback = api._wndproc
            self.assertTrue(first, "initial receiver window creation failed")
            self.assertIsNotNone(callback)

            api._stale = True  # what a timed-out query leaves behind
            second = api._receiver()
            self.assertTrue(second, "rebuilt receiver window creation failed")
            self.assertIs(api._wndproc, callback)
        finally:
            api.close()  # destroys this api's own hidden window and class


if __name__ == "__main__":
    unittest.main()
