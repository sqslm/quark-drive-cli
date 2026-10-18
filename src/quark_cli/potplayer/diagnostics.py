"""Passive, in-memory diagnostics for the PotPlayer bridge.

The model is deliberately dumb and local: it never touches the network, never
writes to disk, and never retains a Cookie, a signed URL, a token, or raw
upstream text. Callers only hand it fixed identifiers and numbers, and the
public :meth:`Diagnostics.report` is an explicit allowlisted projection rather
than a filtered copy of internal state.

Integrator contract
-------------------
Create one ``Diagnostics`` per bridge process and call, from the serving code:

``media_started(session_id, name)``
    A play session opened. Starts (or restarts) the per-play counters. ``name``
    is a local-only label: it appears in :meth:`snapshot` but never in
    :meth:`report`. The registry is bounded to ``MAX_SESSIONS`` (128); the
    least-recently-used play is evicted, mirroring ``stream.Sessions``.

``transferred(session_id, count)``
    ``count`` **actual bytes successfully written to the player**. This is the
    only byte counter: repeated seeks over the same range are counted again
    (cumulative, never per-byte events). Feed rate uses a fixed five one-second
    buckets, so the window is O(1) per chunk, divides by five, and falls to
    zero once five seconds pass with no bytes.

``origin_response(session_id, status, elapsed_ms)``
    An upstream response header arrived: ``status`` is the HTTP status and
    ``elapsed_ms`` the time to headers. No URL is accepted. A failure *before*
    headers is recorded through :meth:`error` instead.

``api_result(success, error_kind=None)``
    One ordinary Quark management call finished. ``success=True`` marks the
    account recently valid. ``error_kind="authentication_error"`` marks it
    expired. Rate limiting / access restrictions never log out and never mark
    the account expired. No extra network validation is done here.

``credential_changed()``
    The plugin credential was replaced (a new login) or explicitly logged out.
    Any previously observed validity is reset to unverified. Merely dropping a
    credential the account API proved stale is *not* a credential change: the
    observed expiry stays visible until a new credential or logout.

``error(phase, kind, status=None)``
    One failure, only when no upstream headers arrived (or for local/account
    problems). ``phase`` and ``kind`` are mapped onto fixed allowlists; anything
    unknown becomes ``"unknown"``. Only the last ``ERROR_LOG_LIMIT`` (20) events
    and their fixed Chinese messages are kept. No exception text is accepted.

``snapshot(*, credential_present, guard=None)``
    Read-only local view: ``account``, ``transfer`` (totals and per-play, names
    included), ``errors``, ``versions`` and ``protection``. It reads only
    ``guard.active_cooldown()`` / ``guard.now()``; a :class:`~quark_cli.errors.LocalIOError`
    turns into a fixed ``local_protection_stopped`` note. It never sends and
    never resets pacing.

``report(*, credential_present, guard=None)``
    Strictly allowlisted, JSON-safe public report with a schema version,
    ``generated_at``, release/protocol/Python/OS, aggregate transfer stats and
    the fixed account/cooldown/error state. It never contains a file name, FID,
    session id, token, URL, watch position/history, environment value, or path.
"""

from __future__ import annotations

import platform
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from ..errors import LocalIOError
from . import SERVICE_VERSION

RELEASE = "0.12.0"
PROTOCOL = SERVICE_VERSION
SCHEMA_VERSION = 1
MAX_SESSIONS = 128
ERROR_LOG_LIMIT = 20
WINDOW_SECONDS = 5

# Fixed Chinese labels for allowlisted error kinds; upstream text is never kept.
_KIND_LABELS = {
    "authentication_error": "登录已失效，请重新扫码登录",
    "rate_limited": "上游限流，已暂停请求",
    "access_restricted": "上游拒绝访问，凭证未被清除",
    "network_error": "网络连接失败",
    "remote_error": "上游返回错误",
    "local_io_error": "本地读写错误",
    "usage_error": "请求参数无效",
    "media_changed": "媒体内容已变化",
    "range_not_satisfiable": "播放范围无效",
    "internal_error": "内部错误",
    "unknown": "未知错误",
}

_PHASE_LABELS = {
    "media": "播放",
    "origin": "源站",
    "account": "账号",
    "credential": "凭证",
    "local": "本地",
    "request": "请求",
    "unknown": "未知阶段",
}

_STATUS_BUCKETS = ("2xx", "3xx", "4xx", "5xx", "other")
_COOLDOWN_REASONS = frozenset({"rate_limited", "access_restricted", "service_unavailable"})

# Fixed wording: the guard stopped persisting cooldowns, so nothing else can be
# read from it. A blind restart of the guard cannot help (persisting is exactly
# what failed), so the operator is sent to repair the storage first and to
# confirm the account with the official Quark client before restarting.
_STOPPED_INSTRUCTION = (
    "本地保护状态不可读，已停止写入冷却记录；请先修复配置目录的读写权限，"
    "并用官方夸克客户端确认账号状态后再重启插件"
)


class _RateWindow:
    """Five one-second buckets; expired seconds are naturally excluded."""

    __slots__ = ("_slots",)

    def __init__(self) -> None:
        self._slots: list[list[int]] = [[-1, 0] for _ in range(WINDOW_SECONDS)]

    def add(self, second: int, count: int) -> None:
        slot = self._slots[second % WINDOW_SECONDS]
        if slot[0] != second:
            slot[0] = second
            slot[1] = 0
        slot[1] += count

    def total(self, second: int) -> int:
        low = second - (WINDOW_SECONDS - 1)
        return sum(slot[1] for slot in self._slots if low <= slot[0] <= second)

    def speed(self, second: int) -> float:
        return self.total(second) / WINDOW_SECONDS


class _Play:
    """Per-play counters. No byte list is kept: only running totals."""

    __slots__ = ("name", "bytes", "chunks", "started", "last", "requests",
                 "statuses", "last_status", "last_elapsed_ms", "window")

    def __init__(self, name: str, now: float) -> None:
        self.name = name
        self.bytes = 0
        self.chunks = 0
        self.started = now
        self.last = now
        self.requests = 0
        self.statuses = {bucket: 0 for bucket in _STATUS_BUCKETS}
        self.last_status: int | None = None
        self.last_elapsed_ms: int | None = None
        self.window = _RateWindow()


def _status_bucket(status: object) -> str:
    if type(status) is not int:
        return "other"
    if 200 <= status < 300:
        return "2xx"
    if 300 <= status < 400:
        return "3xx"
    if 400 <= status < 500:
        return "4xx"
    if 500 <= status < 600:
        return "5xx"
    return "other"


def _key(session_id: object) -> str:
    text = session_id if isinstance(session_id, str) else str(session_id)
    return text[:128]


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _versions() -> dict[str, Any]:
    return {
        "release": RELEASE,
        "protocol": PROTOCOL,
        "schema_version": SCHEMA_VERSION,
        "python": platform.python_version(),
        "os": platform.system(),
    }


class Diagnostics:
    """Thread-safe, passive diagnostics; one instance per bridge process."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._wall = wall_clock
        self._lock = threading.Lock()
        self._plays: dict[str, _Play] = {}
        self._total_bytes = 0
        self._total_chunks = 0
        self._plays_started = 0
        self._origin_requests = 0
        self._statuses = {bucket: 0 for bucket in _STATUS_BUCKETS}
        self._window = _RateWindow()
        self._errors: deque[dict[str, Any]] = deque(maxlen=ERROR_LOG_LIMIT)
        self._error_count = 0
        self._validation = "unverified"
        self._last_success: float | None = None

    # -- ingestion -------------------------------------------------------

    def media_started(self, session_id: str, name: str) -> None:
        """Start (or restart) one play's counters; the name stays local."""
        key = _key(session_id)
        label = name if isinstance(name, str) else ""
        if len(label) > 512:
            label = label[:512]
        now = self._clock()
        with self._lock:
            if key not in self._plays and len(self._plays) >= MAX_SESSIONS:
                oldest = min(self._plays, key=lambda item: self._plays[item].last)
                del self._plays[oldest]
            self._plays[key] = _Play(label, now)
            self._plays_started += 1

    def transferred(self, session_id: str, count: int) -> None:
        """Count actual bytes forwarded; repeats over the same range count again."""
        if type(count) is not int or count <= 0:
            return
        key = _key(session_id)
        now = self._clock()
        second = int(now)
        with self._lock:
            play = self._plays.get(key)
            if play is None:
                return
            play.bytes += count
            play.chunks += 1
            play.last = now
            play.window.add(second, count)
            self._total_bytes += count
            self._total_chunks += 1
            self._window.add(second, count)

    def origin_response(self, session_id: str, status: int, elapsed_ms: int) -> None:
        """Record one upstream response header time; no URL is accepted."""
        key = _key(session_id)
        bucket = _status_bucket(status)
        code = status if type(status) is int else None
        elapsed = elapsed_ms if type(elapsed_ms) is int and elapsed_ms >= 0 else None
        with self._lock:
            play = self._plays.get(key)
            if play is not None:
                play.requests += 1
                play.statuses[bucket] += 1
                play.last_status = code
                play.last_elapsed_ms = elapsed
            self._origin_requests += 1
            self._statuses[bucket] += 1

    def api_result(self, success: bool, error_kind: str | None = None) -> None:
        """Observe one ordinary management call; no extra validation is sent."""
        with self._lock:
            if success:
                self._validation = "valid"
                self._last_success = self._wall()
            elif error_kind == "authentication_error":
                self._validation = "expired"

    def credential_changed(self) -> None:
        """A new credential or a logout: prior validity no longer applies."""
        with self._lock:
            self._validation = "unverified"
            self._last_success = None

    def error(self, phase: str, kind: str, status: int | None = None) -> None:
        """Append one fixed, sanitized error event; unknown input becomes 'unknown'."""
        phase_label = phase if isinstance(phase, str) and phase in _PHASE_LABELS else "unknown"
        kind_label = kind if isinstance(kind, str) and kind in _KIND_LABELS else "unknown"
        code = status if type(status) is int and 100 <= status <= 599 else None
        event = {"ts": self._wall(), "phase": phase_label, "kind": kind_label, "status": code}
        with self._lock:
            self._errors.append(event)
            self._error_count += 1

    # -- projections -----------------------------------------------------

    def snapshot(
        self, *, credential_present: bool, guard: Any = None
    ) -> dict[str, Any]:
        """Read-only local view (names included); never sends or resets pacing."""
        now, account, plays, totals, errors = self._collect(credential_present)
        return {
            "account": {
                "credential_present": account["credential_present"],
                "validation": account["validation"],
                "last_success_at": _iso(account["last_success"]),
            },
            "transfer": {"totals": totals, "plays": plays},
            "errors": {"count": errors["count"], "events": [_event_view(e) for e in errors["events"]]},
            "versions": _versions(),
            "protection": self._cooldown(guard),
        }

    def report(self, *, credential_present: bool, guard: Any = None) -> dict[str, Any]:
        """Strictly allowlisted, JSON-safe public report; explicit projection."""
        # ``_collect`` returns a monotonic instant (only useful as an elapsed
        # baseline); a report timestamp must be wall-clock or it names 1970.
        _now, account, _plays, totals, errors = self._collect(credential_present)
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso(self._wall()),
            "release": RELEASE,
            "protocol": PROTOCOL,
            "python": platform.python_version(),
            "os": platform.system(),
            "account": {
                "credential_present": account["credential_present"],
                "validation": account["validation"],
                "last_success_at": _iso(account["last_success"]),
            },
            "transfer": {
                "total_bytes": totals["bytes"],
                "total_plays": totals["plays"],
                "total_chunks": totals["chunks"],
                "speed_bytes_per_second": totals["speed_bytes_per_second"],
                "origin_requests": totals["origin_requests"],
                "status": totals["status"],
            },
            "cooldown": self._cooldown(guard),
            "errors": {"count": errors["count"], "events": [_event_view(e) for e in errors["events"]]},
        }

    # -- internals -------------------------------------------------------

    def _state_locked(self, credential_present: bool) -> str:
        # An observed expiry outlives the credential being dropped, so the UI
        # keeps showing "expired" rather than silently reverting to "no_login".
        if self._validation == "expired":
            return "expired"
        if not credential_present:
            return "no_login"
        if self._validation == "valid":
            return "recently_valid"
        return "saved_unverified"

    def _collect(
        self, credential_present: bool
    ) -> tuple[float, dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        now = self._clock()
        second = int(now)
        with self._lock:
            account = {
                "credential_present": bool(credential_present),
                "validation": self._state_locked(bool(credential_present)),
                "last_success": self._last_success,
            }
            plays = []
            for play in self._plays.values():
                plays.append({
                    "name": play.name,
                    "bytes": play.bytes,
                    "chunks": play.chunks,
                    "seconds": round(max(0.0, now - play.started), 3),
                    "speed_bytes_per_second": round(play.window.speed(second), 3),
                    "origin_requests": play.requests,
                    "status": dict(play.statuses),
                    "last_status": play.last_status,
                    "last_elapsed_ms": play.last_elapsed_ms,
                })
            totals = {
                "plays": self._plays_started,
                "active_plays": len(self._plays),
                "bytes": self._total_bytes,
                "chunks": self._total_chunks,
                "speed_bytes_per_second": round(self._window.speed(second), 3),
                "origin_requests": self._origin_requests,
                "status": dict(self._statuses),
            }
            errors = {"count": self._error_count, "events": list(self._errors)}
        return now, account, plays, totals, errors

    def _cooldown(self, guard: Any) -> dict[str, Any]:
        if guard is None:
            return {"available": False, "active": False, "reason": "unavailable", "remaining_seconds": 0.0}
        try:
            cooldown = guard.active_cooldown()
            now = guard.now()
        except LocalIOError:
            return {"available": False, "active": False, "reason": "local_protection_stopped",
                    "remaining_seconds": 0.0, "instruction": _STOPPED_INSTRUCTION}
        except Exception:
            return {"available": False, "active": False, "reason": "unavailable", "remaining_seconds": 0.0}
        if cooldown is None:
            return {"available": True, "active": False, "reason": None, "remaining_seconds": 0.0}
        remaining = max(0.0, float(cooldown.until) - float(now))
        reason = cooldown.reason if cooldown.reason in _COOLDOWN_REASONS else "rate_limited"
        return {"available": True, "active": True, "reason": reason, "remaining_seconds": round(remaining, 3)}


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": _iso(event["ts"]),
        "phase": event["phase"],
        "kind": event["kind"],
        "message": _KIND_LABELS[event["kind"]],
        "status": event["status"],
    }
