import json
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

from quark_cli.client import QuarkClient
from quark_cli.errors import LocalIOError, RateLimitError
from quark_cli.request_guard import (
    REASON_ACCESS_RESTRICTED,
    REASON_RATE_LIMITED,
    RequestGuard,
    retry_after_seconds,
)

GUARD_FILE = "request_guard.json"


class FakeClock:
    """Deterministic wall clock whose sleep advance simulates real waiting."""

    def __init__(self, now=1000.0):
        self.now = now
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class _LockedState:
    """State whose shared lock is always contended."""

    def read(self):
        return {}

    def mutate(self, change):
        raise LocalIOError("shared request state is locked")


class _FlakyState:
    """Injectable state: reservations/reads succeed, a cooldown write can fail once.

    Models the concrete failure where another process briefly owns the shared
    lock exactly while a cooldown is being recorded. Reads and pacing writes are
    unaffected, so the guard can reserve its first request; once recovered the
    underlying store holds no cooldown.
    """

    def __init__(self):
        self._data = {}
        self.fail_cooldown = False

    def read(self):
        return dict(self._data)

    def mutate(self, change):
        updated = change(dict(self._data))
        if self.fail_cooldown and "cooldown_until" in updated:
            raise LocalIOError("shared request state is locked")
        self._data = updated
        return dict(self._data)


def _ok(data):
    return {"status": 200, "code": 0, "data": data}


class RequestGuardTests(unittest.TestCase):
    def test_starts_are_reserved_at_least_pacing_seconds_apart(self):
        fake = FakeClock()
        guard = RequestGuard.memory(pacing=0.5, clock=fake.clock, sleep=fake.sleep)
        starts = []
        for _ in range(3):
            with guard.request():
                starts.append(fake.now)
        self.assertEqual(starts, [1000.0, 1000.5, 1001.0])
        self.assertEqual(fake.slept, [0.5, 0.5])

    def test_threaded_clients_are_paced_without_overlapping_starts(self):
        fake = FakeClock()
        guard = RequestGuard.memory(pacing=0.5, clock=fake.clock, sleep=fake.sleep)
        starts = []
        record = threading.Lock()

        def worker():
            with guard.request():
                with record:
                    starts.append(fake.now)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(starts), 8)
        ordered = sorted(starts)
        self.assertEqual(len(set(ordered)), 8)
        for earlier, later in zip(ordered, ordered[1:]):
            self.assertGreaterEqual(later - earlier, 0.5 - 1e-9)

    def test_durable_state_is_shared_by_a_new_guard_and_recovers_after_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = FakeClock()
            first = RequestGuard.durable(directory=directory, clock=fake.clock, sleep=fake.sleep)
            first.record_cooldown(60.0, REASON_RATE_LIMITED)

            document = json.loads((directory / GUARD_FILE).read_text("utf-8"))
            self.assertEqual(set(document), {"cooldown_until", "cooldown_reason"})
            self.assertEqual(document["cooldown_reason"], REASON_RATE_LIMITED)

            second = RequestGuard.durable(directory=directory, clock=fake.clock, sleep=fake.sleep)
            with self.assertRaises(RateLimitError):
                with second.request():
                    pass
            self.assertIsNotNone(second.active_cooldown())

            fake.now += 61.0
            self.assertIsNone(second.active_cooldown())
            with second.request():
                pass

    def test_cooldown_recording_never_shortens_a_longer_deadline(self):
        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)
        guard.record_cooldown(600.0, REASON_ACCESS_RESTRICTED)
        guard.record_cooldown(60.0, REASON_RATE_LIMITED)
        cooldown = guard.active_cooldown()
        self.assertEqual(cooldown.reason, REASON_ACCESS_RESTRICTED)
        self.assertEqual(cooldown.until, 1600.0)

    def test_memory_guards_are_isolated_and_pace_nothing_by_default(self):
        fake = FakeClock()
        isolated = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)
        with isolated.request():
            pass
        with isolated.request():
            pass
        self.assertEqual(fake.slept, [])

        polluted = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)
        polluted.record_cooldown(60.0, REASON_RATE_LIMITED)
        self.assertIsNotNone(polluted.active_cooldown())
        self.assertIsNone(isolated.active_cooldown())

    def test_retry_after_parser_keeps_representable_delays_and_rejects_non_finite(self):
        now = 1000.0
        # A digit run long enough to overflow a float means an invalid header,
        # not an infinite server delay: the caller must fall back, never store inf.
        self.assertIsNone(retry_after_seconds("9" * 400, now))
        self.assertIsNone(retry_after_seconds("nan", now))
        self.assertIsNone(retry_after_seconds("inf", now))
        self.assertIsNone(retry_after_seconds("", now))
        self.assertIsNone(retry_after_seconds(None, now))
        self.assertIsNone(retry_after_seconds("-5", now))
        self.assertIsNone(retry_after_seconds("invalid", now))
        # Long but representable server delays are preserved in full, never capped.
        self.assertEqual(retry_after_seconds("999999999999", now), 999999999999.0)
        self.assertEqual(retry_after_seconds(" 120 ", now), 120.0)
        self.assertEqual(retry_after_seconds("0", now), 0.0)

    def test_contended_shared_state_fails_without_opening_a_request(self):
        guard = RequestGuard(_LockedState(), pacing=0.0)
        sends = []

        def handler(request):
            sends.append(request)
            return httpx.Response(200, json=_ok({"list": []}), request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            with self.assertRaises(LocalIOError):
                client.auth_status()
        self.assertEqual(sends, [])

    def test_failed_cooldown_persistence_stops_the_guard_even_after_store_recovers(self):
        fake = FakeClock()
        state = _FlakyState()
        guard = RequestGuard(state, pacing=0.0, clock=fake.clock, sleep=fake.sleep)
        sends = []

        def handler(request):
            sends.append(request)
            return httpx.Response(429, headers={"retry-after": "60"}, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            # The request is reserved and sent; recording its cooldown is what fails.
            state.fail_cooldown = True
            with self.assertRaises(LocalIOError) as caught:
                client.auth_status()
            self.assertEqual(len(sends), 1)
            self.assertNotIn("shared request state is locked", str(caught.exception))

            # The shared lock is free again and the store holds no cooldown, yet
            # this guard must refuse every later automated call without sending.
            state.fail_cooldown = False

            with self.assertRaises(LocalIOError):
                client.auth_status()
            with self.assertRaises(LocalIOError):
                client.check_request_cooldown()
            with self.assertRaises(LocalIOError):
                guard.record_cooldown(5.0, REASON_RATE_LIMITED)
            with self.assertRaises(LocalIOError):
                guard.active_cooldown()
            self.assertEqual(len(sends), 1)

            # The recovered store truly carries no cooldown: a fresh guard reads
            # empty, documenting that a separate process cannot learn the loss.
            self.assertIsNone(RequestGuard(state, clock=fake.clock, sleep=fake.sleep).active_cooldown())

    def test_queued_request_does_not_send_after_front_request_stops_the_guard(self):
        fake = FakeClock()
        state = _FlakyState()
        guard = RequestGuard(state, pacing=0.0, clock=fake.clock, sleep=fake.sleep)
        entered = threading.Event()
        release = threading.Event()
        queued_checked = threading.Event()
        sends = []
        errors = {}

        def handler(request):
            sends.append(request)
            if len(sends) == 1:
                entered.set()
                release.wait(5)
                return httpx.Response(429, headers={"retry-after": "60"}, request=request)
            return httpx.Response(200, json=_ok({"list": []}), request=request)

        # Observe the queued thread's pre-lock usability check so the test can
        # prove it passed that check before the front request stopped the guard.
        front_ident = {}
        original_ensure = guard._ensure_usable

        def tracked_ensure():
            if front_ident.get("id") not in (None, threading.get_ident()):
                queued_checked.set()
            original_ensure()

        guard._ensure_usable = tracked_ensure

        def front():
            front_ident["id"] = threading.get_ident()
            try:
                client.auth_status()
            except Exception as exc:  # noqa: BLE001 - captured for assertions
                errors["front"] = exc

        def queued():
            try:
                client.auth_status()
            except Exception as exc:  # noqa: BLE001 - captured for assertions
                errors["queued"] = exc

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            # The front request is reserved and sent; recording its 429 cooldown
            # is what fails, then the store silently recovers.
            state.fail_cooldown = True
            first = threading.Thread(target=front)
            first.start()
            self.assertTrue(entered.wait(5))
            second = threading.Thread(target=queued)
            second.start()
            self.assertTrue(queued_checked.wait(5))
            release.set()
            first.join(5)
            second.join(5)
            state.fail_cooldown = False

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertIsInstance(errors.get("front"), LocalIOError)
        self.assertIsInstance(errors.get("queued"), LocalIOError)
        self.assertEqual(len(sends), 1)

    def test_stop_observed_during_pacing_never_yields(self):
        fake = FakeClock()
        state = _FlakyState()
        holder = {}
        yields = []

        def sleep(seconds):
            fake.sleep(seconds)
            # A transfer thread stops the guard while this thread paces.
            try:
                holder["guard"].record_cooldown(60.0, REASON_RATE_LIMITED)
            except LocalIOError:
                pass

        guard = RequestGuard(state, pacing=0.5, clock=fake.clock, sleep=sleep)
        holder["guard"] = guard
        state.fail_cooldown = True

        with guard.request():
            pass

        with self.assertRaises(LocalIOError):
            with guard.request():
                yields.append(True)
        self.assertEqual(yields, [])
        self.assertEqual(fake.slept, [0.5])

    def test_mocked_clients_do_not_share_persisted_state(self):
        def handler(request):
            return httpx.Response(429, headers={"retry-after": "600"}, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as first:
            with self.assertRaises(RateLimitError):
                first.auth_status()
        calls = []

        def ok_handler(request):
            calls.append(request)
            return httpx.Response(200, json=_ok({"list": []}), request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(ok_handler)) as second:
            self.assertTrue(second.auth_status()["authenticated"])
        self.assertEqual(len(calls), 1)


    def test_real_transport_client_uses_durable_state_but_stays_lazy(self):
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"QUARK_CONFIG_DIR": tmp}
        ):
            with QuarkClient("sid=offline"):
                self.assertFalse((Path(tmp) / GUARD_FILE).exists())


if __name__ == "__main__":
    unittest.main()
