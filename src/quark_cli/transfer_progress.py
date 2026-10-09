"""Shared payload pacing and credential-free progress events."""

from __future__ import annotations

import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, TextIO

from .errors import UsageError


@dataclass(frozen=True)
class ProgressEvent:
    direction: str
    phase: str
    name: str
    transferred: int
    confirmed: int
    total: int | None
    rate: float
    confirmed_parts: int = 0


class RateLimiter:
    """One operation's thread-safe cumulative payload budget, reusable across files."""

    def __init__(self, bytes_per_second: int | None = None, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if bytes_per_second is not None and (type(bytes_per_second) is not int or bytes_per_second <= 0):
            raise UsageError("limit-rate 必须是正整数字节/秒")
        self.bytes_per_second = bytes_per_second
        self.clock, self.sleep = clock, sleep
        self.deadline = clock()
        self.transferred = 0
        self._lock = threading.Lock()

    @property
    def chunk_size(self) -> int:
        return min(1024 * 1024, max(1, self.bytes_per_second // 10)) if self.bytes_per_second else 1024 * 1024

    def consume(self, count: int) -> None:
        if type(count) is not int or count < 0:
            raise ValueError("invalid payload count")
        with self._lock:
            if self.bytes_per_second and count:
                now = self.clock()
                self.deadline = max(now, self.deadline) + count / self.bytes_per_second
                while (delay := self.deadline - self.clock()) > 0:
                    self.sleep(min(delay, 1.0))
            self.transferred += count


class Progress:
    def __init__(self, direction: str, name: str, total: int | None, limiter: RateLimiter,
                 callback: Callable[[ProgressEvent], None] | None = None) -> None:
        self.direction, self.name, self.total = direction, name, total
        self.limiter, self.callback = limiter, callback
        self.started = limiter.clock()
        self.initial_bytes = limiter.transferred
        self.last_emit = self.started - 1
        self.transferred = self.confirmed = self.confirmed_parts = 0

    def emit(self, phase: str, *, force: bool = False) -> None:
        now = self.limiter.clock()
        if self.callback is None or (not force and now - self.last_emit < 0.5):
            return
        self.last_emit = now
        elapsed = now - self.started
        event = ProgressEvent(self.direction, phase, self.name, self.transferred, self.confirmed, self.total,
                              (self.limiter.transferred - self.initial_bytes) / elapsed if elapsed > 0 else 0.0,
                              self.confirmed_parts)
        self.callback(event)


def render_progress(event: ProgressEvent, stream: TextIO | None = None) -> None:
    stream = stream if stream is not None else sys.stderr
    name = "".join("?" if unicodedata.category(c) in ("Cc", "Cf") else c for c in event.name)
    total = str(event.total) if event.total is not None else "?"
    text = f"{event.direction} {event.phase} {name}: sent={event.transferred} B confirmed={event.confirmed}/{total} B ({event.rate:.0f} B/s)"
    terminal = getattr(stream, "isatty", lambda: False)()
    stream.write(("\r" if terminal else "") + text + ("\n" if not terminal or event.phase == "completed" else ""))
    stream.flush()
