"""Bounded look-ahead for a player's ranged stream, without a disk cache."""
from __future__ import annotations
import time
import threading

MIB = 1024 * 1024
BUFFER_MS = 30_000


class BufferBudget:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = None
        self.forwarded = 0
        self.probe = None
        self.position = 0


class ReadAhead:
    def __init__(self, *, size: int, start: int, clock=time.monotonic, budget=None):
        self.size, self.start, self.clock = size, start, clock
        self.budget = budget if budget is not None else BufferBudget()
        with self.budget.lock:
            if self.budget.started is None:
                self.budget.started = clock()

    def consume(self, count):
        with self.budget.lock:
            self.budget.forwarded += count

    def allowance(self, offset: int, row: dict) -> int:
        duration, position = row.get('duration_ms', 0), row.get('position_ms', 0)
        if duration > 0 and row.get('state') in ('playing', 'paused'):
            head = self.size * min(position, duration) // duration
            ahead = max(4 * MIB, min(32 * MIB, self.size * BUFFER_MS // duration))
            limit = head + ahead
            # A seek or a tail-index probe must get enough bytes to parse its
            # new location before the native clock can report that position.
            if self.start and abs(self.start - head) > ahead:
                with self.budget.lock:
                    if (self.budget.probe is None or abs(position - self.budget.position) > 5000
                            or abs(self.start - self.budget.probe) > 4 * MIB):
                        self.budget.probe, self.budget.position = self.start, position
                    if self.budget.probe <= offset < self.budget.probe + 2 * MIB:
                        limit = max(limit, self.budget.probe + 2 * MIB)
        else:
            # Startup / unavailable native reader: bounded initial burst, then
            # conservative pacing until a usable playback clock arrives.
            with self.budget.lock:
                remaining = 8 * MIB + int(max(0, self.clock() - self.budget.started) * MIB) - self.budget.forwarded
            return max(0, min(self.size - offset, remaining))
        return max(0, min(self.size, limit) - offset)
