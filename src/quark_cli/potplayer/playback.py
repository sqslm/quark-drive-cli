"""Resume real playback progress for the native player, keyed by FID and PID.

The monitor binds one native player process (its PID) to the registered stream
session and samples that player's clock every :data:`POLL_INTERVAL` seconds.
Only genuine samples for the exact registered file are written to the (small,
URL-free) library document; the native receiver and every disk write run on the
single worker thread, never while the short monitor lock is held and never on an
HTTP request thread.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from ..errors import LocalIOError, UsageError
from .library_state import Library
from .stream import PlaySession

POLL_INTERVAL = 2.0
SAVE_INTERVAL = 10.0
MIN_RESUME_MS = 15_000
INITIAL_POSITION_MS = 5_000
END_MAX_MS = 30_000
END_FRACTION = 0.02
DURATION_TOLERANCE_MS = 2_000
DURATION_TOLERANCE_FRACTION = 0.01
VERIFY_TOLERANCE_MS = 3_000
MAX_BINDINGS = 128
CLOSE_TIMEOUT = 5.0
#: Bound on how long ``restart`` blocks for the worker's real seek/reset result.
RESTART_TIMEOUT = 3.0
#: A stopped player answers ``0x5006`` with ``-1``; ``0`` is kept as the legacy
#: fake/undefined value so both mean "not running" without an unsigned sentinel.
STATUS_STOPPED = -1
# The extension hands the player one of these logical addresses; only an exact
# match against the registered FID is accepted.
LOGICAL_PREFIXES = ("https://quark.invalid/video/", "https://quark.invalid/play/")

STATE_WAITING = "waiting"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"
STATE_STOPPED = "stopped"
STATE_MISSING = "missing"
STATE_MISMATCH = "mismatch"
STATE_SUSPENDED = "suspended"
STATE_UNAVAILABLE = "unavailable"
STATE_ERROR = "error"

MSG_WAITING = "等待播放开始"
MSG_PROGRESS = "等待播放器进度"
MSG_PLAYING = "正在播放"
MSG_PAUSED = "已暂停"
MSG_STOPPED = "已停止"
MSG_MISSING = "未检测到播放器或媒体"
MSG_MISMATCH = "播放器中的文件与登记视频不一致"
MSG_SUSPENDED = "同一文件在多个播放器中打开，已暂停记录进度"
MSG_UNAVAILABLE = "当前系统不支持读取播放器进度"
MSG_RESTORED = "已恢复上次观看位置"
MSG_SEEK_FAILED = "恢复观看位置失败，已保留上次保存位置"
MSG_NATIVE_FAILED = "读取播放器进度失败，播放不受影响"
MSG_STORAGE_FAILED = "保存观看进度失败，已停止记录，播放不受影响"
MSG_CLEARED = "最近观看已清空，不再记录进度"
MSG_RESTARTED = "已从头开始播放"

_DISPLAY = {
    0: (STATE_STOPPED, MSG_STOPPED),
    1: (STATE_PAUSED, MSG_PAUSED),
    2: (STATE_PLAYING, MSG_PLAYING),
}


class NativeAdapter(Protocol):
    """The subset of ``native_player.NativePlayer`` the monitor drives.

    ``is_alive`` is an *optional* extra: a newer adapter exposes a cheap
    ``is_alive(pid) -> bool`` that only looks up the owned window. Legacy
    adapters without it keep the old "no sample means gone" behaviour.
    """

    def sample(self, player_pid: int) -> Any | None: ...

    def seek(self, player_pid: int, expected_file_name: str, position_ms: int) -> bool: ...

    def close(self) -> None: ...


@dataclass
class _Flush:
    """One last-good sample of a binding that is being replaced.

    ``generation`` is the generation of the binding that was replaced; a flush
    is dropped when the same pid has since been re-bound to the same FID (or
    cleared), because that newer binding is the authoritative owner of the
    record and an older last-good point must not overwrite it.
    """

    player_pid: int
    fid: str
    size: int
    position_ms: int
    duration_ms: int
    generation: int


@dataclass
class _Restart:
    """One from-the-top restart request together with its synchronous result.

    ``restart`` blocks on ``done`` so a caller learns the real outcome; the
    worker stores it in ``outcome`` (after a confirmed seek to zero *and* a
    successful progress reset) and always signals ``done`` in a ``finally``.
    ``cancelled`` lets a timed-out or cleared request be skipped before it runs.
    """

    player_pid: int
    generation: int
    fid: str
    done: threading.Event = field(default_factory=threading.Event)
    outcome: bool = False
    cancelled: bool = False


@dataclass
class _Binding:
    player_pid: int
    session: PlaySession
    name: str
    url: str | None
    generation: int
    active: bool = True
    streamed: bool = False
    suspended: bool = False
    suppressed: bool = False
    storage_failed: bool = False
    restore_done: bool = False
    good: bool = False
    paused_seen: bool = False
    last_position: int = 0
    last_duration: int = 0
    last_status: int = -1
    last_saved_position: int = -1
    last_save_at: float = 0.0
    state: str = STATE_WAITING
    message: str = MSG_WAITING


def _ms(value: Any) -> int:
    if type(value) is int and value > 0:
        return value
    return 0


def _identity_matches(value: Any, fid: str, stream_url: str | None) -> bool:
    """True only for the exact logical URL of ``fid`` or the exact registered URL.

    The player reports the address the extension handed it: one of the logical
    ``quark.invalid`` URLs for this FID, or (when a signed address was
    registered) that exact address. A bare file name is never accepted, so a
    same-named file with a different FID can neither be recorded nor seeked.
    """
    if not isinstance(value, str) or not value:
        return False
    if value == LOGICAL_PREFIXES[0] + fid or value == LOGICAL_PREFIXES[1] + fid:
        return True
    return stream_url is not None and value == stream_url


class PlaybackMonitor:
    """One worker thread sampling bound player PIDs and persisting progress."""

    def __init__(
        self,
        library: Library,
        *,
        native_factory: Callable[[], NativeAdapter] | None = None,
        clock: Callable[[], float] = time.monotonic,
        poll_interval: float = POLL_INTERVAL,
        start: bool = True,
    ) -> None:
        self._library = library
        self._native_factory = native_factory
        self._clock = clock
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._adapter_lock = threading.Lock()
        # Serializes every library write and every clear, and carries the epoch
        # that lets a queued write detect that a clear superseded it. It is
        # never held together with ``_lock`` and never held over a native call.
        self._storage_lock = threading.Lock()
        self._storage_epoch = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._bindings: dict[int, _Binding] = {}
        self._pending: list[Any] = []
        self._adapter: NativeAdapter | None = None
        self._generation = 0
        self._closed = False
        self._thread: threading.Thread | None = None
        if start:
            self._thread = threading.Thread(target=self._run, name="quark-potplayer-playback", daemon=True)
            self._thread.start()

    # -- public contract -------------------------------------------------

    def bind(self, session: PlaySession, player_pid: int, stream_url: str | None = None) -> None:
        """Attach ``player_pid`` to one registered session, replacing any previous one."""
        name, url = self._expected_session(session, stream_url)
        with self._lock:
            if self._closed:
                raise UsageError("播放监控已关闭")
            previous = self._bindings.pop(player_pid, None)
            if previous is None and len(self._bindings) >= MAX_BINDINGS:
                oldest = min(self._bindings.values(), key=lambda item: item.generation)
                self._bindings.pop(oldest.player_pid, None)
            self._bindings[player_pid] = _Binding(
                player_pid=player_pid, session=session, name=name, url=url, generation=self._bump(),
            )
            if self._flushable(previous):
                self._pending.append(_Flush(
                    previous.player_pid, previous.session.fid, previous.session.size,
                    previous.last_position, previous.last_duration, previous.generation,
                ))
            self._wake.set()

    def stream_started(self, session_id: str) -> None:
        """Mark the registered session whose media actually began streaming."""
        if not isinstance(session_id, str) or not session_id:
            return
        with self._lock:
            for binding in self._bindings.values():
                if binding.session.session == session_id:
                    binding.streamed = True
                    self._wake.set()

    def snapshot(self) -> list[dict[str, Any]]:
        """Current view: no URL, session id, or signed address is exposed."""
        with self._lock:
            return [
                {
                    "player_pid": binding.player_pid,
                    "fid": binding.session.fid,
                    "name": binding.name,
                    "position_ms": binding.last_position,
                    "duration_ms": binding.last_duration,
                    "state": binding.state,
                    "message": binding.message,
                }
                for binding in sorted(self._bindings.values(), key=lambda item: item.player_pid)
            ]

    def restart(self, player_pid: int, fid: str) -> bool:
        """Restart one binding from the top and report the real, confirmed result.

        The seek to zero and the progress reset run on the worker; this call
        blocks (bounded by :data:`RESTART_TIMEOUT`) until the worker has finished
        and returns whether it actually succeeded. ``False`` means the command
        failed or timed out -- queuing it is never reported as success. With no
        worker thread (the deterministic test seam) the pass runs on this thread.
        """
        with self._lock:
            if self._closed:
                return False
            binding = self._bindings.get(player_pid)
            if binding is None or binding.session.fid != fid:
                raise UsageError("未找到该播放器与文件的绑定")
            if binding.suppressed or binding.storage_failed:
                return False
            request = _Restart(player_pid, binding.generation, fid)
            self._pending.append(request)
            self._wake.set()
        if self._thread is None:
            # No worker to hand it to: run exactly one pass on this thread.
            self._poll_once()
        if not request.done.wait(RESTART_TIMEOUT):
            # Never let a command that timed out run later: cancel it and drop
            # it from the queue before the worker can pick it up.
            with self._lock:
                request.cancelled = True
                request.outcome = False
                try:
                    self._pending.remove(request)
                except ValueError:
                    pass
            return False
        return request.outcome

    def clear(self) -> None:
        """Stop persisting progress for current bindings until each is bound again.

        Bumps the storage epoch and suppresses every binding first, then waits
        out any write that is already past its check, so a caller that empties
        the recent list afterwards can never be overwritten by an in-flight
        write of the same FID.
        """
        with self._lock:
            self._storage_epoch += 1
            for task in self._pending:
                if isinstance(task, _Restart):
                    task.cancelled = True
                    task.outcome = False
                    task.done.set()
            self._pending.clear()
            for binding in self._bindings.values():
                binding.suppressed = True
                binding.generation = self._bump()
                binding.state, binding.message = STATE_STOPPED, MSG_CLEARED
            self._wake.set()
        with self._storage_lock:
            pass  # drain: wait for any write that already passed the epoch check

    def close(self) -> None:
        """Flush the last good sample, stop the worker, and destroy the receiver."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for task in self._pending:
                if isinstance(task, _Restart):
                    task.cancelled = True
                    task.outcome = False
                    task.done.set()
            self._pending.clear()
            thread = self._thread
            self._stop.set()
            self._wake.set()
        with self._storage_lock:
            pass  # drain any in-flight write before the receiver goes away
        if thread is not None and thread.is_alive():
            thread.join(CLOSE_TIMEOUT)
            return
        adapter = self._ensure_adapter()
        self._flush_all()
        self._destroy(adapter)

    # -- worker ----------------------------------------------------------

    def _run(self) -> None:
        adapter = self._ensure_adapter()
        try:
            while not self._stop.is_set():
                try:
                    self._process(adapter)
                except Exception:  # never let one bad sample kill the worker
                    pass
                if self._stop.is_set():
                    break
                self._wake.wait(self._poll_interval)
                self._wake.clear()
            self._flush_all()
        finally:
            self._destroy(adapter)

    def _poll_once(self) -> None:
        """Synchronous seam: run exactly one command/poll pass on the calling thread."""
        self._process(self._ensure_adapter())

    def _process(self, adapter: NativeAdapter | None) -> None:
        with self._lock:
            tasks, self._pending = self._pending[:], []
            targets = [(pid, binding.generation) for pid, binding in self._bindings.items()]
        for task in tasks:
            if isinstance(task, _Flush):
                self._apply_flush(task)
                continue
            if task.cancelled:
                continue
            try:
                task.outcome = self._apply_restart(adapter, task)
            finally:
                task.done.set()  # always unblock the waiting caller
        self._collect(adapter, targets)

    def _collect(self, adapter: NativeAdapter | None, targets: list[tuple[int, int]]) -> None:
        """Sample every live binding first, then decide, so one poll sees the truth.

        A binding that has gone away (no reading, a different file, or a stopped
        player) is flushed and deactivated before any sibling is judged, so a
        same-FID player that is still open is never left suspended by a dead one.
        """
        readings: list[tuple[int, int, Any, BaseException | None]] = []
        with self._lock:
            gated: list[tuple[int, int, str, str | None]] = []
            for player_pid, generation in targets:
                binding = self._bindings.get(player_pid)
                if binding is None or binding.generation != generation or not binding.active:
                    continue
                if not binding.streamed:
                    binding.state, binding.message = STATE_WAITING, MSG_WAITING
                    continue
                if binding.suppressed:
                    binding.state = STATE_STOPPED
                    if not binding.storage_failed:
                        binding.message = MSG_CLEARED
                    continue
                gated.append((player_pid, generation, binding.session.fid, binding.url))
        if adapter is None:
            for player_pid, generation, _, _ in gated:
                self._set(player_pid, generation, STATE_UNAVAILABLE, MSG_UNAVAILABLE)
            return
        for player_pid, generation, _, _ in gated:
            try:
                readings.append((player_pid, generation, adapter.sample(player_pid), None))
            except Exception as exc:  # never let one bad sample kill the worker
                readings.append((player_pid, generation, None, exc))
        self._apply_readings(adapter, readings)

    def _apply_readings(self, adapter: NativeAdapter, readings: list[tuple[int, int, Any, BaseException | None]]) -> None:
        decisions: list[tuple[Any, ...]] = []
        # Flushing a dropped binding needs real disk I/O, so it is recorded here
        # (no I/O under the main lock) and applied after the lock is released.
        flushes: list[_Flush] = []
        with self._lock:
            live: list[tuple[int, int, _Binding, Any]] = []
            for player_pid, generation, sample, error in readings:
                binding = self._bindings.get(player_pid)
                if binding is None or binding.generation != generation:
                    continue
                if error is not None:
                    binding.state, binding.message = STATE_ERROR, MSG_NATIVE_FAILED
                    continue
                if sample is None or getattr(sample, "player_pid", None) != player_pid:
                    # No coherent reading. A live player that is still loading
                    # (or briefly unresponsive) must be awaited; only a really
                    # gone one is flushed and dropped.
                    if self._is_live(adapter, player_pid):
                        binding.state, binding.message = STATE_WAITING, MSG_PROGRESS
                        continue
                    record = self._flush_record(binding)
                    if record is not None:
                        flushes.append(record)
                    self._bindings.pop(player_pid, None)
                    continue
                if not _identity_matches(getattr(sample, "file_name", None), binding.session.fid, binding.url):
                    # A binding that never matched may simply still be
                    # transitioning to the expected file; only a previously
                    # matched video that really switched away is dropped.
                    if binding.good:
                        record = self._flush_record(binding)
                        if record is not None:
                            flushes.append(record)
                        binding.active = False
                    binding.state, binding.message = STATE_MISMATCH, MSG_MISMATCH
                    continue
                status = getattr(sample, "status", -1)
                if status == STATUS_STOPPED or status == 0:
                    if binding.good and binding.restore_done:
                        # A previously playing file really stopped.
                        record = self._flush_record(binding)
                        if record is not None:
                            flushes.append(record)
                        binding.active = False
                        binding.state, binding.message = STATE_STOPPED, MSG_STOPPED
                    else:
                        # Live but not ready yet: keep the binding and the saved
                        # target until the player reports a real duration.
                        binding.state, binding.message = STATE_WAITING, MSG_PROGRESS
                    continue
                if status not in (1, 2):
                    binding.state, binding.message = STATE_WAITING, MSG_PROGRESS
                    continue
                live.append((player_pid, generation, binding, sample))
            for player_pid, generation, binding, sample in live:
                if self._bindings.get(player_pid) is not binding or binding.generation != generation:
                    continue
                if self._suspended(binding.session.fid):
                    binding.suspended = True
                    binding.state, binding.message = STATE_SUSPENDED, MSG_SUSPENDED
                    continue
                binding.suspended = False
                status = int(getattr(sample, "status", -1))
                binding.last_status = status
                binding.state, binding.message = _DISPLAY[status]
                duration = _ms(getattr(sample, "duration_ms", 0))
                position = _ms(getattr(sample, "position_ms", 0))
                if duration <= 0:
                    # Running but the player has not parsed the duration yet:
                    # keep the binding and its target, write nothing this pass.
                    continue
                if position > 0:
                    # A zero reading (a just-restarted or reset clock) is not a
                    # good point: keep the last real one so a flush can never
                    # overwrite a saved position with zero.
                    binding.good = True
                    binding.last_position, binding.last_duration = position, duration
                if binding.storage_failed:
                    binding.state, binding.message = STATE_ERROR, MSG_STORAGE_FAILED
                    continue
                fid, size = binding.session.fid, binding.session.size
                if not binding.restore_done:
                    decisions.append(("restore", player_pid, generation, fid, size,
                                      getattr(sample, "file_name"), binding.url, duration, position))
                else:
                    now = self._clock()
                    # The ten-second cycle applies to every live status, so a
                    # paused player dragged to a new position still records it;
                    # the transition into pause is additionally written at once
                    # (``last_save_at`` may be younger than the interval then).
                    # ``position != last_saved_position`` below keeps an
                    # unchanged paused frame from repeating the IO.
                    writing = now - binding.last_save_at >= SAVE_INTERVAL or (
                        status == 1 and not binding.paused_seen
                    )
                    binding.paused_seen = status == 1
                    if writing and position > 0 and position != binding.last_saved_position:
                        decisions.append(("write", player_pid, generation, fid, size, position, duration))
        for record in flushes:
            self._apply_flush(record)
        for decision in decisions:
            if decision[0] == "restore":
                self._restore(adapter, *decision[1:])
            else:
                self._write(*decision[1:])

    def _apply_flush(self, task: _Flush) -> None:
        with self._lock:
            current = self._bindings.get(task.player_pid)
            if current is not None and current.session.fid == task.fid and current.generation != task.generation:
                # The same pid was re-bound (or cleared) to this FID after the
                # flush was queued; the newer binding owns the record now.
                return
            epoch = self._storage_epoch
        self._save_progress(task.fid, task.size, task.position_ms, task.duration_ms, epoch)

    def _apply_restart(self, adapter: NativeAdapter | None, task: _Restart) -> bool:
        if adapter is None or task.cancelled:
            return False
        with self._lock:
            binding = self._bindings.get(task.player_pid)
            if binding is None or binding.generation != task.generation or binding.session.fid != task.fid:
                return False
            if not binding.active or not binding.streamed or binding.suspended or binding.suppressed or binding.storage_failed:
                return False
            fid, url = binding.session.fid, binding.url
        try:
            sample = adapter.sample(task.player_pid)
        except Exception:
            self._set(task.player_pid, task.generation, STATE_ERROR, MSG_NATIVE_FAILED)
            return False
        if sample is None or getattr(sample, "player_pid", None) != task.player_pid:
            self._set(task.player_pid, task.generation, STATE_MISSING, MSG_MISSING)
            return False
        identity = getattr(sample, "file_name", None)
        if not _identity_matches(identity, fid, url):
            self._set(task.player_pid, task.generation, STATE_MISMATCH, MSG_MISMATCH)
            return False
        with self._lock:
            if not self._fresh(task.player_pid, task.generation):
                return False
            epoch = self._storage_epoch
            if task.cancelled:
                return False
        try:
            seeked = bool(adapter.seek(task.player_pid, identity, 0))
        except Exception:
            seeked = False
        if not seeked:
            self._set(task.player_pid, task.generation, None, MSG_SEEK_FAILED)
            return False
        reset = self._reset_progress(task.fid, epoch)
        if reset is None:
            self._storage_failed(task.player_pid, task.generation)
            return False
        with self._lock:
            binding = self._bindings.get(task.player_pid)
            if binding is None or binding.generation != task.generation:
                return False
            if task.cancelled:
                return False
            binding.restore_done = True
            binding.good = _ms(getattr(sample, "duration_ms", 0)) > 0
            binding.paused_seen = False
            binding.last_position = 0
            binding.last_saved_position = 0
            binding.last_save_at = self._clock()
            binding.state, binding.message = STATE_PLAYING, MSG_RESTARTED
        return True

    # -- progress --------------------------------------------------------

    def _restore(
        self, adapter: NativeAdapter, player_pid: int, generation: int,
        fid: str, size: int, identity: Any, url: str | None, duration: int, position: int,
    ) -> None:
        try:
            target = self._library.progress(fid, size)
        except LocalIOError:
            self._storage_failed(player_pid, generation)
            return
        if target is None or not self._resumable(target, duration, position):
            self._finish_restore(player_pid, generation, None)
            return
        saved = target["position_ms"]
        with self._lock:
            if not self._fresh(player_pid, generation):
                return
        try:
            seeked = bool(adapter.seek(player_pid, identity, saved))
        except Exception:
            seeked = False
        if not seeked:
            self._finish_restore(player_pid, generation, saved, MSG_SEEK_FAILED)
            return
        try:
            verify = adapter.sample(player_pid)
        except Exception:
            verify = None
        verified = (
            verify is not None
            and getattr(verify, "player_pid", None) == player_pid
            and _identity_matches(getattr(verify, "file_name", None), fid, url)
            and abs(_ms(getattr(verify, "position_ms", 0)) - saved) <= VERIFY_TOLERANCE_MS
        )
        self._finish_restore(player_pid, generation, saved, MSG_RESTORED if verified else MSG_SEEK_FAILED)

    def _resumable(self, target: dict[str, Any], duration: int, position: int) -> bool:
        saved = target["position_ms"]
        if saved < MIN_RESUME_MS or position > INITIAL_POSITION_MS:
            return False
        saved_duration = target["duration_ms"]
        if saved_duration > 0:
            tolerance = max(DURATION_TOLERANCE_MS, int(saved_duration * DURATION_TOLERANCE_FRACTION))
            if abs(duration - saved_duration) > tolerance:
                return False
        return duration - saved > min(END_MAX_MS, int(duration * END_FRACTION))

    def _finish_restore(self, player_pid: int, generation: int, restored: int | None, message: str | None = None) -> None:
        with self._lock:
            binding = self._bindings.get(player_pid)
            if binding is None or binding.generation != generation:
                return
            binding.restore_done = True
            if restored is not None:
                binding.last_position = restored
                binding.last_saved_position = restored
            if message is not None:
                binding.message = message

    def _write(self, player_pid: int, generation: int, fid: str, size: int, position: int, duration: int) -> None:
        with self._lock:
            binding = self._bindings.get(player_pid)
            if binding is None or binding.generation != generation:
                return
            if not binding.active or not binding.streamed or binding.suspended or binding.suppressed or binding.storage_failed:
                return
            if position == binding.last_saved_position:
                return
            epoch = self._storage_epoch
        written = self._save_progress(fid, size, position, duration, epoch)
        if written is None:
            self._storage_failed(player_pid, generation)
            return
        if not written:
            return
        with self._lock:
            binding = self._bindings.get(player_pid)
            if binding is None or binding.generation != generation:
                return
            binding.last_saved_position = position
            binding.last_save_at = self._clock()

    def _flush_all(self) -> None:
        with self._lock:
            epoch = self._storage_epoch
            items = [
                (binding.player_pid, binding.generation, binding.session.fid, binding.session.size,
                 binding.last_position, binding.last_duration)
                for binding in self._bindings.values() if self._flushable(binding)
            ]
        for player_pid, generation, fid, size, position, duration in items:
            if self._save_progress(fid, size, position, duration, epoch) is None:
                self._storage_failed(player_pid, generation)

    # -- storage barrier -------------------------------------------------

    def _save_progress(self, fid: str, size: int, position: int, duration: int, epoch: int) -> bool | None:
        """Write a resume point under the storage barrier.

        The epoch is re-checked *while holding* the barrier, so a write that was
        decided before a :meth:`clear` can never land after that clear has been
        observed. Returns ``True``/``False`` like ``Library.save_progress`` or
        ``None`` when the write raised :class:`LocalIOError`.
        """
        with self._storage_lock:
            if epoch != self._storage_epoch:
                return False
            try:
                return bool(self._library.save_progress(fid, size, position, duration))
            except LocalIOError:
                return None

    def _reset_progress(self, fid: str, epoch: int) -> bool | None:
        """Reset one resume point under the storage barrier (``None`` on I/O error)."""
        with self._storage_lock:
            if epoch != self._storage_epoch:
                return False
            try:
                return bool(self._library.reset_progress(fid))
            except LocalIOError:
                return None

    # -- helpers ---------------------------------------------------------

    def _bump(self) -> int:
        self._generation += 1
        return self._generation

    def _is_live(self, adapter: NativeAdapter | None, player_pid: int) -> bool:
        """True only when the adapter can still see the owned player window.

        Uses the adapter's optional cheap ``is_alive`` probe (a window/pid
        lookup with no message sends); an adapter without it keeps the legacy
        "no sample means gone" behaviour.
        """
        if adapter is None:
            return False
        probe = getattr(adapter, "is_alive", None)
        if probe is None:
            return False
        try:
            return bool(probe(player_pid))
        except Exception:
            return False

    def _suspended(self, fid: str) -> bool:
        """True while another *live* instance plays the same registered FID.

        Only bindings that are still active, started, and not suppressed count:
        a window that has closed or switched files is dropped from the tally
        before this runs, so it can never hold a surviving player suspended.
        """
        return sum(
            1
            for binding in self._bindings.values()
            if binding.session.fid == fid and binding.active and binding.streamed and not binding.suppressed
        ) > 1

    def _fresh(self, player_pid: int, generation: int) -> bool:
        binding = self._bindings.get(player_pid)
        return (
            binding is not None
            and binding.generation == generation
            and binding.active
            and not binding.suppressed
            and not binding.storage_failed
        )

    def _flush_record(self, binding: _Binding) -> _Flush | None:
        """Build the last-good flush for one binding, or ``None`` if it has none."""
        if not self._flushable(binding):
            return None
        return _Flush(
            binding.player_pid, binding.session.fid, binding.session.size,
            binding.last_position, binding.last_duration, binding.generation,
        )

    def _flushable(self, binding: _Binding | None) -> bool:
        # A suspended binding belongs to a same-FID sibling conflict: its sample
        # is not authoritative, so it is never flushed (on rebind, drop, or
        # close). Recording resumes only once a poll clears ``suspended``.
        return (
            binding is not None
            and binding.good
            and not binding.suspended
            and not binding.suppressed
            and not binding.storage_failed
            and binding.last_position > 0
            and binding.last_position != binding.last_saved_position
        )

    def _expected_session(self, session: PlaySession, stream_url: str | None) -> tuple[str, str | None]:
        """Validate the registration and return ``(display name, registered url)``."""
        name = getattr(session, "name", None)
        fid = getattr(session, "fid", None)
        if not isinstance(name, str) or not name or not isinstance(fid, str) or not fid:
            raise UsageError("播放会话信息无效")
        if stream_url is None:
            return name, None
        if not isinstance(stream_url, str):
            raise UsageError("播放地址无效")
        for prefix in LOGICAL_PREFIXES:
            if stream_url.startswith(prefix):
                if stream_url[len(prefix):] != fid:
                    raise UsageError("播放地址与登记文件不一致")
                return name, stream_url
        parts = urlsplit(stream_url).path.split("/")
        if len(parts) >= 5 and parts[1] == "v1" and parts[2] == "stream":
            if parts[3] == getattr(session, "session", None) and unquote(parts[4]) == name:
                return name, stream_url
        raise UsageError("播放地址与登记会话不一致")

    def _ensure_adapter(self) -> NativeAdapter | None:
        with self._adapter_lock:
            if self._adapter is None and self._native_factory is not None:
                try:
                    self._adapter = self._native_factory()
                except Exception:
                    self._adapter = None
            return self._adapter

    def _destroy(self, adapter: NativeAdapter | None) -> None:
        if adapter is None:
            return
        try:
            adapter.close()
        except Exception:
            pass

    def _set(self, player_pid: int, generation: int, state: str | None, message: str) -> None:
        with self._lock:
            binding = self._bindings.get(player_pid)
            if binding is None or binding.generation != generation:
                return
            if state is not None:
                binding.state = state
            binding.message = message

    def _storage_failed(self, player_pid: int, generation: int) -> None:
        self._set(player_pid, generation, STATE_ERROR, MSG_STORAGE_FAILED)
        with self._lock:
            binding = self._bindings.get(player_pid)
            if binding is not None and binding.generation == generation:
                binding.storage_failed = True
