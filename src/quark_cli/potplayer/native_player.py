"""Minimal, dependency-free reader for a PotPlayer instance's playback state.

Only ``ctypes`` from the standard library is used; there is no ``pywin32``
dependency and the module imports safely on non-Windows platforms, where
:data:`SUPPORTED` is ``False`` and :class:`NativePlayer` degrades to a no-op
(``sample`` -> ``None``, ``seek`` -> ``False``).

Protocol
--------
PotPlayer answers ``WM_USER`` messages sent to its main window. This adapter
uses only the narrow, verified subset:

* ``0x5002``  playback duration in ms
* ``0x5004``  current position in ms
* ``0x5005``  set current position (``lParam`` = ms)
* ``0x5006``  playback status (``-1`` stopped, ``1`` paused, ``2`` running)
* ``0x6020``  authoritative file name, replied with ``WM_COPYDATA``

Every query uses ``SendMessageTimeoutW`` with ``SMTO_ABORTIFHUNG`` **only** and
a hard 500 ms budget. ``SMTO_BLOCK`` is deliberately *not* used: it would stop
the calling thread from dispatching the ``WM_COPYDATA`` the player sends back,
so the file-name reply would only surface after the timeout expired (observed
~2.5 s in the feasibility probe). Dropping it makes the reply arrive during the
call. Consequently the ``WM_COPYDATA`` receiver window **must be owned by the
same thread that is blocked in** ``SendMessageTimeoutW``; :class:`NativePlayer`
therefore requires one polling thread and returns ``None``/``False`` if it is
driven from another thread afterwards.

Identity is never guessed from a window title: a window is used only when its
class name is ``PotPlayer``/``PotPlayer64`` *and*
``GetWindowThreadProcessId`` returns exactly the requested pid. The pid is
re-checked before every individual message, and the file name is sampled both
before and after the numeric fields so a file switch during a read cannot pair
one clip's position with another clip's duration. A minimized or hidden window
is still a valid target; only the class name and pid are required.

The reported name is the **complete** authoritative value (the original logical
URL/path exactly as the player holds it), with only a trailing NUL removed: it is
never reduced to a basename, so callers can compare it byte-for-byte against the
identity they registered. A malformed (non-UTF-8) payload is dropped rather than
guessed. The reply is accepted only for the window that was just queried, and a
request that times out discards its receiver so a late reply cannot be attached
to a later request.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "SUPPORTED",
    "STATUS_STOPPED",
    "STATUS_PAUSED",
    "STATUS_RUNNING",
    "NativeSample",
    "NativePlayer",
]

SUPPORTED = sys.platform == "win32"

STATUS_STOPPED = -1
STATUS_PAUSED = 1
STATUS_RUNNING = 2

WM_USER = 0x400
WM_COPYDATA = 0x4A
MSG_DURATION = 0x5002
MSG_POSITION = 0x5004
MSG_SET_POSITION = 0x5005
MSG_STATUS = 0x5006
MSG_FILENAME = 0x6020

SMTO_ABORTIFHUNG = 0x0002
MSGFLT_ALLOW = 1
QUERY_TIMEOUT_MS = 500
#: Small grace window beyond the send timeout in which a reply to *this* request
#: is still accepted; older replies are dropped by token/deadline.
_REPLY_GRACE_S = 0.20
#: Upper bound for a file-name ``WM_COPYDATA`` payload.
_MAX_COPYDATA = 4096
#: Accepted deviation between a requested position and the player's read-back.
_SEEK_TOLERANCE_MS = 3000
#: Bounded poll window while a seek settles (the player applies it asynchronously).
_SEEK_CONFIRM_TIMEOUT_S = 1.0
_SEEK_CONFIRM_INTERVAL_S = 0.1


@dataclass(frozen=True)
class NativeSample:
    """One coherent playback reading of an owned PotPlayer process.

    ``file_name`` is the complete authoritative identity reported by the player
    (its original logical URL/path, trailing NUL stripped): never a basename, and
    it must never be written to a log or an end-user report.
    """

    player_pid: int
    file_name: str
    position_ms: int
    duration_ms: int
    status: int


def _decode_name(raw: bytes) -> Optional[str]:
    """Decode one ``WM_COPYDATA`` name payload, dropping anything not trusted.

    The player's UTF-8 payload may carry a trailing NUL terminator; only that
    terminator is removed. A malformed byte sequence or an empty value yields
    ``None`` instead of a substituted string, so identity is never guessed.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = text.rstrip("\x00")
    return text or None


if SUPPORTED:
    from ctypes import wintypes as _w

    _HWND = _w.HWND
    _UINT = _w.UINT
    _DWORD = _w.DWORD
    _LRESULT = ctypes.c_ssize_t  # signed: a stopped player answers -1
    _WPARAM = ctypes.c_size_t
    _LPARAM = ctypes.c_ssize_t
    _BOOL = _w.BOOL

    _POTPLAYER_CLASSES = ("PotPlayer", "PotPlayer64")

    class _CopyDataStruct(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_size_t), ("cbData", _DWORD), ("lpData", ctypes.c_void_p)]

    class _WndClassExW(ctypes.Structure):
        _fields_ = [
            ("cbSize", _UINT),
            ("style", _UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", _w.HINSTANCE),
            ("hIcon", _w.HICON),
            ("hCursor", _w.HANDLE),
            ("hbrBackground", _w.HBRUSH),
            ("lpszMenuName", _w.LPCWSTR),
            ("lpszClassName", _w.LPCWSTR),
            ("hIconSm", _w.HICON),
        ]

    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, _HWND, _UINT, _WPARAM, _LPARAM)
    _WNDENUMPROC = ctypes.WINFUNCTYPE(_BOOL, _HWND, _LPARAM)

    class _Pending:
        __slots__ = ("token", "hwnd", "pid", "deadline")

        def __init__(self, token: int, hwnd: int, pid: int, deadline: float) -> None:
            self.token = token
            self.hwnd = hwnd
            self.pid = pid
            self.deadline = deadline


class _WinApi:
    """ctypes backend: window lookup plus the ``SendMessageTimeoutW`` queries.

    A single hidden receiver window is created lazily on the first query and is
    owned by that calling thread for the object's lifetime; ``close()`` must be
    called from the same thread. Requests carry a monotonically increasing
    token and a deadline, and a ``WM_COPYDATA`` reply is accepted only while a
    request to that exact sender window is pending, so a delayed reply can never
    be attached to a later read. A request that times out throws its receiver
    away, so a straggler reply to the old window cannot reach a newer request.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._bind()
        self._owner_thread: Optional[int] = None
        self._hwnd: Optional[int] = None
        self._stale = False
        self._wndproc = None
        self._class_name = "QuarkPotPlayerNative_%d_%x" % (os.getpid(), id(self))
        self._pending: Optional[_Pending] = None
        self._reply: Optional[tuple] = None
        self._token = 0

    def _bind(self) -> None:
        u, k = self._user32, self._kernel32
        u.SendMessageTimeoutW.restype = _LRESULT
        u.SendMessageTimeoutW.argtypes = [_HWND, _UINT, _WPARAM, _LPARAM, _UINT, _UINT, ctypes.POINTER(_LRESULT)]
        u.EnumWindows.argtypes = [_WNDENUMPROC, _LPARAM]
        u.EnumWindows.restype = _BOOL
        u.GetClassNameW.argtypes = [_HWND, _w.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = [_HWND, ctypes.POINTER(_DWORD)]
        u.GetWindowThreadProcessId.restype = _DWORD
        u.IsWindow.argtypes = [_HWND]
        u.IsWindow.restype = _BOOL
        u.RegisterClassExW.argtypes = [ctypes.POINTER(_WndClassExW)]
        u.RegisterClassExW.restype = _w.ATOM
        u.CreateWindowExW.argtypes = [_DWORD, _w.LPCWSTR, _w.LPCWSTR, _DWORD, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, _HWND, _w.HANDLE, _w.HINSTANCE, ctypes.c_void_p]
        u.CreateWindowExW.restype = _HWND
        u.UnregisterClassW.argtypes = [_w.LPCWSTR, _w.HINSTANCE]
        u.UnregisterClassW.restype = _BOOL
        u.DefWindowProcW.argtypes = [_HWND, _UINT, _WPARAM, _LPARAM]
        u.DefWindowProcW.restype = _LRESULT
        u.DestroyWindow.argtypes = [_HWND]
        u.DestroyWindow.restype = _BOOL
        u.ChangeWindowMessageFilterEx.argtypes = [_HWND, _UINT, _DWORD, ctypes.c_void_p]
        u.ChangeWindowMessageFilterEx.restype = _BOOL
        u.PeekMessageW.argtypes = [ctypes.POINTER(_w.MSG), _HWND, _UINT, _UINT, _UINT]
        u.PeekMessageW.restype = _BOOL
        u.TranslateMessage.argtypes = [ctypes.POINTER(_w.MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(_w.MSG)]
        k.CloseHandle.argtypes = [_w.HANDLE]

    # -- thread affinity -----------------------------------------------------
    def _on_owner_thread(self) -> bool:
        tid = threading.get_ident()
        if self._owner_thread is None:
            self._owner_thread = tid
            return True
        return self._owner_thread == tid

    def _owner_of(self, hwnd: int) -> int:
        pid = _DWORD(0)
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    # -- receiver window -----------------------------------------------------
    def _on_copydata(self, hwnd, msg, wparam, lparam):
        if msg != WM_COPYDATA:
            return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        with self._lock:
            pending = self._pending
            if pending is None or self._reply is not None:
                return 1
            sender = int(wparam)
            # PotPlayer normally stamps its own window handle; a bare 0 is
            # tolerated because the receiver is private to this reader and a
            # request must still be pending within its deadline. Any other
            # sender is a stray message and is ignored.
            if sender and (sender != pending.hwnd or self._owner_of(sender) != pending.pid):
                return 1
            data = ctypes.cast(lparam, ctypes.POINTER(_CopyDataStruct)).contents
            if int(data.dwData) != MSG_FILENAME or not (0 < data.cbData <= _MAX_COPYDATA) or not data.lpData:
                return 1
            if time.monotonic() > pending.deadline:
                return 1
            text = _decode_name(ctypes.string_at(data.lpData, data.cbData))
            if text is not None:
                self._reply = (pending.token, text)
        return 1

    def _receiver(self) -> Optional[int]:
        if self._hwnd is not None and not self._stale:
            return self._hwnd
        if self._hwnd is not None:
            # Drop the timed-out window entirely: a late reply addressed to the
            # old handle then dies with it instead of reaching a newer request.
            old, self._hwnd, self._stale = self._hwnd, None, False
            self._user32.DestroyWindow(old)
        # The window class is registered once with the callback below and
        # RegisterClassExW refuses to replace an existing class (1410), so that
        # class keeps pointing at the *first* callback for the whole backend
        # lifetime. Rebuild the receiver with the same live object: replacing
        # it here would let the old ctypes callback be collected and leave the
        # registered class holding a dangling WndProc pointer.
        if self._wndproc is None:
            self._wndproc = _WNDPROC(self._on_copydata)
        wc = _WndClassExW()
        wc.cbSize = ctypes.sizeof(_WndClassExW)
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wc.lpszClassName = self._class_name
        self._user32.RegisterClassExW(ctypes.byref(wc))  # 1410 = already registered, harmless
        self._hwnd = self._user32.CreateWindowExW(0, self._class_name, self._class_name, 0, 0, 0, 0, 0,
                                                  None, None, None, None)
        # An elevated bridge must receive WM_COPYDATA from a normal player.
        # Limit the UIPI exception to this private receiver and this one message;
        # _on_copydata still checks the pending query, sender and bounded payload.
        if self._hwnd and not self._user32.ChangeWindowMessageFilterEx(self._hwnd, WM_COPYDATA, MSGFLT_ALLOW, None):
            self._user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        return self._hwnd

    def _pump(self) -> None:
        msg = _w.MSG()
        while self._user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))

    # -- public backend surface ---------------------------------------------
    def find_window(self, pid: int) -> Optional[int]:
        if not self._on_owner_thread():
            return None
        found: list = []

        def cb(hwnd, _lp):
            name = ctypes.create_unicode_buffer(64)
            self._user32.GetClassNameW(hwnd, name, 64)
            if name.value in _POTPLAYER_CLASSES and self._owner_of(hwnd) == pid:
                found.append(int(hwnd))
                return False
            return True

        self._user32.EnumWindows(_WNDENUMPROC(cb), 0)
        return found[0] if found else None

    def query_number(self, hwnd: int, pid: int, command: int) -> Optional[int]:
        if not self._on_owner_thread() or not self._user32.IsWindow(hwnd) or self._owner_of(hwnd) != pid:
            return None
        result = _LRESULT(0)
        ok = self._user32.SendMessageTimeoutW(hwnd, WM_USER, command, 0, SMTO_ABORTIFHUNG,
                                              QUERY_TIMEOUT_MS, ctypes.byref(result))
        if not ok:
            return None
        return int(result.value)

    def set_position(self, hwnd: int, pid: int, position_ms: int) -> Optional[int]:
        if not self._on_owner_thread() or not self._user32.IsWindow(hwnd) or self._owner_of(hwnd) != pid:
            return None
        result = _LRESULT(0)
        ok = self._user32.SendMessageTimeoutW(hwnd, WM_USER, MSG_SET_POSITION, int(position_ms), SMTO_ABORTIFHUNG,
                                              QUERY_TIMEOUT_MS, ctypes.byref(result))
        if not ok:
            return None
        return int(result.value)

    def query_filename(self, hwnd: int, pid: int) -> Optional[str]:
        if not self._on_owner_thread() or not self._user32.IsWindow(hwnd) or self._owner_of(hwnd) != pid:
            return None
        if self._receiver() is None:
            return None
        token = self._token
        self._token += 1
        pending = _Pending(token, hwnd, pid, time.monotonic() + QUERY_TIMEOUT_MS / 1000.0 + _REPLY_GRACE_S)
        with self._lock:
            self._pending = pending
            self._reply = None
        self._user32.SendMessageTimeoutW(hwnd, WM_USER, MSG_FILENAME, self._hwnd, SMTO_ABORTIFHUNG,
                                         QUERY_TIMEOUT_MS, ctypes.byref(_LRESULT(0)))
        self._pump()  # safety net if the reply landed just after the call returned
        with self._lock:
            reply = self._reply
            self._pending = None
            self._reply = None
        if reply is not None and reply[0] == token:
            return reply[1]
        self._stale = True  # discard the receiver so a late reply cannot be reused
        return None

    def close(self) -> None:
        with self._lock:
            self._pending = None
            self._reply = None
            hwnd, self._hwnd = self._hwnd, None
            self._stale = False
        if hwnd:
            self._user32.DestroyWindow(hwnd)
            self._user32.UnregisterClassW(self._class_name, None)


class NativePlayer:
    """Read positions from, and seek, a PotPlayer process owned by the caller.

    ``file_name`` on :class:`NativeSample` is the complete authoritative identity
    reported by the player (its original logical URL/path, trailing NUL removed);
    it is meant for internal production matching only and must never be written
    to a log or an end-user report.

    One instance is bound to the thread that first uses it (the polling thread),
    because the ``WM_COPYDATA`` receiver must share that thread's message loop.
    """

    def __init__(self, api: Optional[Any] = None) -> None:
        if api is not None:
            self._api: Optional[Any] = api
        elif SUPPORTED:
            try:
                self._api = _WinApi()
            except OSError:
                self._api = None
        else:
            self._api = None
        self._closed = False

    def is_alive(self, player_pid: int) -> bool:
        """True while a PotPlayer window for ``player_pid`` still exists.

        A cheap class/PID window lookup only: no ``WM_USER`` message is sent, so
        it is safe to call when a full sample cannot be taken (still loading or
        briefly unresponsive) and is never confused with a closed player.
        """
        api, pid = self._api, int(player_pid)
        if api is None or self._closed:
            return False
        try:
            return bool(api.find_window(pid))
        except Exception:
            return False

    def sample(self, player_pid: int) -> Optional[NativeSample]:
        """Return a coherent reading, or ``None`` when none can be trusted.

        The file name is read before and after the numeric queries; a mismatch
        (the file switched mid-read), an empty name, a missing query reply, an
        invalid status or negative position all yield ``None``.

        A live player whose media has not finished loading is still a coherent
        reading: ``duration_ms`` may be ``0`` and ``status`` may be
        :data:`STATUS_STOPPED`. Those are reported as-is so the caller can wait
        for playback to become resumable instead of mistaking it for a closed
        player (``None`` is never returned merely because the duration is not
        known yet).
        """
        api, pid = self._api, int(player_pid)
        if api is None or self._closed:
            return None
        hwnd = api.find_window(pid)
        if not hwnd:
            return None
        first = api.query_filename(hwnd, pid)
        if not first:
            return None
        duration = api.query_number(hwnd, pid, MSG_DURATION)
        position = api.query_number(hwnd, pid, MSG_POSITION)
        status = api.query_number(hwnd, pid, MSG_STATUS)
        last = api.query_filename(hwnd, pid)
        if not last or first != last:
            return None
        if status not in (STATUS_STOPPED, STATUS_PAUSED, STATUS_RUNNING):
            return None
        if position is None or position < 0:
            return None
        if duration is None:
            return None
        if duration < 0:
            duration = 0  # an unknown-length value is "not ready", never bogus
        return NativeSample(pid, first, int(position), int(duration), int(status))

    def seek(self, player_pid: int, expected_file_name: str, position_ms: int) -> bool:
        """Seek only if the player still plays exactly ``expected_file_name``.

        Returns ``False`` without sending anything when the pid is unreachable,
        the name has changed, no live duration is available, or the name changes
        again around the numeric read. After sending, the new position is polled
        back for at most :data:`_SEEK_CONFIRM_TIMEOUT_S`; ``True`` requires an
        unchanged file name and a position within :data:`_SEEK_TOLERANCE_MS`.
        A player that stops answering, or that reports a different file, fails
        safely with ``False``.
        """
        api, pid = self._api, int(player_pid)
        if api is None or self._closed or not expected_file_name or position_ms < 0:
            return False
        hwnd = api.find_window(pid)
        if not hwnd or api.query_filename(hwnd, pid) != expected_file_name:
            return False
        duration = api.query_number(hwnd, pid, MSG_DURATION)
        if not duration or duration <= 0 or api.query_filename(hwnd, pid) != expected_file_name:
            return False
        target = min(int(position_ms), int(duration))
        if api.set_position(hwnd, pid, target) is None:
            return False
        deadline = time.monotonic() + _SEEK_CONFIRM_TIMEOUT_S
        while True:
            time.sleep(_SEEK_CONFIRM_INTERVAL_S)
            observed = api.query_number(hwnd, pid, MSG_POSITION)
            if observed is not None:
                name = api.query_filename(hwnd, pid)
                if name is not None:
                    if name != expected_file_name:
                        return False  # file switched under us: fail safely
                    if abs(observed - target) <= _SEEK_TOLERANCE_MS:
                        return True
            if time.monotonic() >= deadline:
                return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        api = self._api
        if api is not None:
            api.close()
