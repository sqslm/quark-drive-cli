"""Shared 20%/10% read-ahead budget; no byte-rate limit or disk cache."""
from __future__ import annotations
import threading


class BufferBudget:
    def __init__(self):
        self.lock = threading.Lock()
        # Half-open byte ranges delivered ahead of the native playback clock.
        self.ranges = []
        self.reserved = 0
        self.head = 0
        self.paused = False


class ReadAhead:
    def __init__(self, *, size: int, start: int, budget=None):
        self.size, self.start, self.cursor = size, start, start
        self.budget = budget if budget is not None else BufferBudget()
        self.credit = 0

    def _allowance(self, offset: int, row: dict) -> int:
        """Called with the shared lock held. Ranges include index/seek reads."""
        budget = self.budget
        duration, position = row.get('duration_ms', 0), row.get('position_ms', 0)
        if duration > 0 and row.get('state') in ('playing', 'paused'):
            head = self.size * min(max(0, position), duration) // duration
            if head < budget.head - self.size * min(1000, max(1, duration // 100)) // duration:
                # A backward seek discards the player's former forward cache.
                budget.ranges.clear()
                budget.paused = False
            budget.head = head
        budget.ranges = [(max(start, budget.head), end)
                         for start, end in budget.ranges if end > budget.head]
        buffered = sum(end - start for start, end in budget.ranges) + budget.reserved
        upper, lower = max(1, self.size // 5), self.size // 10
        if budget.paused and buffered <= lower:
            budget.paused = False
        if buffered >= upper:
            budget.paused = True
        if budget.paused:
            return 0
        return max(0, min(self.size - offset, upper - buffered))

    def allowance(self, offset: int, row: dict) -> int:
        with self.budget.lock:
            return self._allowance(offset, row)

    def reserve(self, offset: int, row: dict, maximum: int) -> int:
        """Reserve before forwarding, so concurrent responses share one cap."""
        with self.budget.lock:
            count = min(maximum, self._allowance(offset, row))
            self.budget.reserved += count
            self.credit += count
            buffered = sum(end - start for start, end in self.budget.ranges) + self.budget.reserved
            if buffered >= max(1, self.size // 5):
                self.budget.paused = True
            return count

    def consume(self, count: int) -> None:
        with self.budget.lock:
            self.credit -= count
            self.budget.reserved -= count
            start, end = self.cursor, self.cursor + count
            self.cursor = end
            merged = []
            for first, last in sorted([*self.budget.ranges, (start, end)]):
                if merged and first <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], last))
                else:
                    merged.append((first, last))
            self.budget.ranges = merged

    def release(self) -> None:
        """A disconnect/failed socket write returns any unused reservation."""
        with self.budget.lock:
            self.budget.reserved -= self.credit
            self.credit = 0
