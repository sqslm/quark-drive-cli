"""Cross-process request pacing and cooldown guard for Quark metadata calls.

The guard does two small things before a client opens a metadata request:

* reserves a start slot at least ``pacing`` seconds after the last reserved
  start, so independently launched CLI invocations and PotPlayer do not burst
  the same account; and
* refuses to send while a persisted cooldown from a previous rejection is
  still active.

Durable state lives in the current-user configuration directory and contains
only wall-clock times and a fixed reason kind; no account identity, Cookie or
token value is ever stored. Kernel-lock contention fails safely (no request is
sent) and is never retried blindly.
"""

from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple, Protocol

import httpx

from .errors import AccessRestrictedError, LocalIOError, RateLimitError, QuarkError
from .state import StateLock, config_directory, read_json, write_json


PACING_SECONDS = 0.5
DEFAULT_COOLDOWN_SECONDS = 60.0
DEFAULT_RESTRICTION_COOLDOWN_SECONDS = 600.0

REASON_RATE_LIMITED = "rate_limited"
REASON_ACCESS_RESTRICTED = "access_restricted"
REASON_SERVICE_UNAVAILABLE = "service_unavailable"

_REASONS = frozenset({REASON_RATE_LIMITED, REASON_ACCESS_RESTRICTED, REASON_SERVICE_UNAVAILABLE})

# Fixed, sanitized reason used once cooldown persistence has failed. A stop is
# permanent for this guard: the unpersisted cooldown cannot be honored later, so
# continuing to send would silently violate "stop after observed throttling".
_STOPPED_MESSAGE = "无法保存请求冷却状态，已停止当前进程的自动化请求以避免限流后继续发送"


class Cooldown(NamedTuple):
    """Active persisted cooldown: wall-clock deadline plus a fixed reason kind."""

    until: float
    reason: str


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        # A non-finite time can never be persisted faithfully, so it is treated
        # as absent rather than stored (and silently truncated) as a decimal.
        return None
    return number


def _document(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only times and a fixed reason kind; never persist free-form values."""
    result: dict[str, Any] = {}
    for key in ("pacing_until", "cooldown_until"):
        value = _number(data.get(key))
        if value is not None:
            result[key] = value
    reason = data.get("cooldown_reason")
    if isinstance(reason, str) and reason in _REASONS:
        result["cooldown_reason"] = reason
    return result


def _cooldown_from(data: dict[str, Any], now: float) -> Cooldown | None:
    until = _number(data.get("cooldown_until"))
    if until is None or until <= now:
        return None
    reason = data.get("cooldown_reason")
    return Cooldown(until, reason if reason in _REASONS else REASON_RATE_LIMITED)


def _cooldown_error(remaining: float, reason: str) -> QuarkError:
    seconds = max(0, int(remaining + 0.999))
    retry_after = max(0.0, remaining)
    if reason == REASON_ACCESS_RESTRICTED:
        return AccessRestrictedError(f"夸克访问受限冷却中，约 {seconds} 秒后再试", retry_after=retry_after)
    return RateLimitError(f"夸克请求冷却中，约 {seconds} 秒后再试", retry_after=retry_after)


def retry_after_seconds(value: str | None, now: float) -> float | None:
    """Parse ``Retry-After`` (delay-seconds or HTTP-date); ``None`` when absent/invalid.

    A longer server delay is never truncated: malformed values are ignored and
    the caller then falls back to its own minimum.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.isascii() and raw.isdigit():
        number = float(raw)
        if not math.isfinite(number):
            # A digit run long enough to overflow a float parses to ``inf``;
            # that is an invalid header, not a real (infinite) server delay, so
            # the caller falls back to its own minimum instead of losing the
            # persisted cooldown. Representable long delays are kept in full.
            return None
        return number
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - now)


class _GuardState(Protocol):
    """Minimal injectable read-modify-write store for pacing and cooldown times."""

    def read(self) -> dict[str, Any]: ...

    def mutate(self, change: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]: ...


class _MemoryState:
    """Isolated in-process state for mocked or explicitly isolated guards."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}

    def read(self) -> dict[str, Any]:
        with self._lock:
            return _document(self._data)

    def mutate(self, change: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            self._data = _document(change(_document(self._data)))
            return dict(self._data)


class _DurableState:
    """Atomic JSON shared by all clients of the current-user configuration."""

    file_name = "request_guard.json"
    lock_name = "request_guard.lock"

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / self.file_name

    def read(self) -> dict[str, Any]:
        return _document(read_json(self.path, default={}))

    def mutate(self, change: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        # Nonblocking kernel lock: contention raises LocalIOError and no network
        # call happens; the caller never silently bypasses the shared state.
        with StateLock(self.directory, self.lock_name):
            updated = _document(change(_document(read_json(self.path, default={}))))
            write_json(self.path, updated)
            return updated


class RequestGuard:
    """Serialize one client's metadata requests, pace starts, and honor cooldown."""

    def __init__(
        self,
        state: _GuardState,
        *,
        pacing: float = PACING_SECONDS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._state = state
        self._pacing = max(0.0, float(pacing))
        self._clock = clock
        self._sleep = sleep
        self._thread_lock = threading.Lock()
        self._stopped = False
        self._stopped_message = ""

    @classmethod
    def memory(
        cls,
        *,
        pacing: float = 0.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> "RequestGuard":
        """Isolated guard for mocked transports: no shared state, no default pacing."""
        return cls(_MemoryState(), pacing=pacing, clock=clock, sleep=sleep)

    @classmethod
    def durable(
        cls,
        *,
        directory: Path | None = None,
        pacing: float = PACING_SECONDS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> "RequestGuard":
        """Guard backed by the shared configuration directory (or an explicit one)."""
        return cls(_DurableState(directory if directory is not None else config_directory()),
                   pacing=pacing, clock=clock, sleep=sleep)

    def now(self) -> float:
        return self._clock()

    def _ensure_usable(self) -> None:
        """Refuse every automated action once cooldown persistence has failed.

        A fresh ``LocalIOError`` (fixed, sanitized message) is raised each time;
        the failed exception object is never re-raised, so no traceback grows.
        """
        if self._stopped:
            raise LocalIOError(self._stopped_message)

    def active_cooldown(self) -> Cooldown | None:
        """Return the current persisted cooldown, if any, without sending anything."""
        self._ensure_usable()
        return _cooldown_from(self._state.read(), self._clock())

    def check_active_cooldown(self) -> None:
        """Raise the typed cooldown error before a new storage request is opened."""
        self._ensure_usable()
        cooldown = _cooldown_from(self._state.read(), self._clock())
        if cooldown is not None:
            raise _cooldown_error(cooldown.until - self._clock(), cooldown.reason)

    def observe_transfer_response(self, response: httpx.Response) -> None:
        """Classify one transfer/origin response and persist a shared cooldown.

        Only HTTP 429 and HTTP 503 with ``Retry-After`` pause the shared state.
        Storage-level 401/403 are deliberately ignored: they belong to the
        caller's single signed-URL refresh and never mean a stale credential.
        """
        status = response.status_code
        if status == 429:
            delay = retry_after_seconds(response.headers.get("retry-after"), self._clock())
            cooldown = self.record_cooldown(max(DEFAULT_COOLDOWN_SECONDS, delay or 0.0), REASON_RATE_LIMITED)
            remaining = max(0.0, cooldown.until - self._clock())
            raise RateLimitError(
                f"夸克请求过于频繁（HTTP 429），已暂停后续请求，约 {max(0, int(remaining + 0.999))} 秒后再试；"
                "请在官方客户端确认账号状态后重试",
                retry_after=remaining,
            )
        if status == 503:
            delay = retry_after_seconds(response.headers.get("retry-after"), self._clock())
            if delay is not None:
                cooldown = self.record_cooldown(delay, REASON_SERVICE_UNAVAILABLE)
                remaining = max(0.0, cooldown.until - self._clock())
                raise RateLimitError(
                    f"夸克服务暂时不可用（HTTP 503），已暂停后续请求，约 {max(0, int(remaining + 0.999))} 秒后再试；"
                    "请在官方客户端确认账号状态后重试",
                    retry_after=remaining,
                )

    @contextmanager
    def request(self) -> Iterator["RequestGuard"]:
        """Hold the per-client lock across one call and its response classification.

        Raises ``RateLimitError``/``AccessRestrictedError`` (or ``LocalIOError``)
        before yielding when a cooldown is active or the shared state is locked.
        """
        self._ensure_usable()
        with self._thread_lock:
            # Recheck under the lock: a thread that queued behind an in-flight
            # request must not send after that request stopped the guard.
            self._ensure_usable()
            self._reserve_slot()
            # A transfer thread may have stopped the guard while this thread
            # paced inside ``_reserve_slot``; never yield after that.
            self._ensure_usable()
            yield self

    def record_cooldown(self, seconds: float, reason: str) -> Cooldown:
        """Persist a cooldown deadline; an already longer deadline is never shortened.

        Contention *before* or *during* a request is not fatal (the caller may
        retry later). Contention here, while trying to record an observed
        throttle, is fatal: the cooldown cannot be persisted, so this guard
        marks itself stopped and fails closed for all later automated requests.
        """
        self._ensure_usable()
        if reason not in _REASONS:
            raise ValueError(f"unknown cooldown reason: {reason}")
        now = self._clock()
        deadline = now + max(0.0, float(seconds))
        holder: dict[str, Cooldown] = {}

        def change(data: dict[str, Any]) -> dict[str, Any]:
            existing = _cooldown_from(data, now)
            if existing is not None and existing.until >= deadline:
                holder["cooldown"] = existing
                return data
            holder["cooldown"] = Cooldown(deadline, reason)
            data["cooldown_until"] = deadline
            data["cooldown_reason"] = reason
            return data

        try:
            self._state.mutate(change)
        except LocalIOError:
            # Publish the fixed message *before* the flag: a thread that observes
            # ``_stopped`` must never then read an empty ``_stopped_message``.
            self._stopped_message = _STOPPED_MESSAGE
            self._stopped = True
            # Replace the storage error (which may carry a path) with a fixed,
            # sanitized reason; chain is dropped so no state data leaks.
            raise LocalIOError(self._stopped_message) from None
        return holder["cooldown"]

    def _reserve_slot(self) -> None:
        now = self._clock()
        holder: dict[str, Any] = {}

        def change(data: dict[str, Any]) -> dict[str, Any]:
            cooldown = _cooldown_from(data, now)
            if cooldown is not None:
                holder["cooldown"] = cooldown
                return data
            start = max(now, _number(data.get("pacing_until")) or 0.0)
            holder["start"] = start
            data["pacing_until"] = start + self._pacing
            return data

        self._state.mutate(change)
        cooldown = holder.get("cooldown")
        if cooldown is not None:
            raise _cooldown_error(cooldown.until - now, cooldown.reason)
        delay = holder["start"] - now
        if delay > 0:
            # Sleep outside the shared lock; the thread lock still serializes
            # this client so its own requests cannot overlap.
            self._sleep(delay)
        # Another client may have recorded a cooldown while we paced; never send
        # then, even though the slot was already reserved.
        current = self._clock()
        cooldown = _cooldown_from(self._state.read(), current)
        if cooldown is not None:
            raise _cooldown_error(cooldown.until - current, cooldown.reason)
