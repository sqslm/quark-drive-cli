"""Behavioural tests for the passive, in-memory PotPlayer diagnostics model."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from quark_cli.errors import LocalIOError
from quark_cli.potplayer.diagnostics import (
    ERROR_LOG_LIMIT,
    MAX_SESSIONS,
    Diagnostics,
)
from quark_cli.request_guard import Cooldown


class _Clock:
    def __init__(self, now: float = 0.0, wall: float = 1000.0) -> None:
        self.now = now
        self.wall = wall


class _Guard:
    """Read-only fake: only the two methods the model is allowed to touch."""

    def __init__(self, cooldown: Cooldown | None = None, now: float = 0.0) -> None:
        self._cooldown = cooldown
        self._now = now
        self.calls: list[str] = []

    def active_cooldown(self):
        self.calls.append("active_cooldown")
        return self._cooldown

    def now(self):
        self.calls.append("now")
        return self._now


class _StoppedGuard:
    """Mimics a guard whose cooldown persistence failed: every read raises."""

    def active_cooldown(self):
        raise LocalIOError("已停止")

    def now(self):
        return 0.0


def _make(now: float = 0.0, wall: float = 1000.0) -> tuple[Diagnostics, _Clock]:
    clock = _Clock(now, wall)
    return Diagnostics(clock=lambda: clock.now, wall_clock=lambda: clock.wall), clock


class RateWindowTests(unittest.TestCase):
    def test_five_one_second_buckets_expire_and_go_idle(self):
        diag, clock = _make()
        diag.media_started("s", "movie.mkv")
        diag.transferred("s", 500)
        self.assertEqual(self._play(diag)["speed_bytes_per_second"], 100.0)  # 500 / 5

        clock.now = 2.0
        diag.transferred("s", 500)
        self.assertEqual(self._play(diag)["speed_bytes_per_second"], 200.0)  # 1000 / 5

        clock.now = 6.0  # second 0 fell out of [2..6], second 2 is still inside
        play = self._play(diag)
        self.assertEqual(play["bytes"], 1000)
        self.assertEqual(play["speed_bytes_per_second"], 100.0)

        clock.now = 7.0  # [3..7] excludes both buckets: idle window is zero
        self.assertEqual(self._play(diag)["speed_bytes_per_second"], 0.0)

    def test_cumulative_repeated_seeks_count_every_chunk(self):
        diag, _ = _make()
        diag.media_started("s", "m")
        for _ in range(5):
            diag.transferred("s", 100)  # the same range seeked five times
        snap = diag.snapshot(credential_present=True)
        play = snap["transfer"]["plays"][0]
        self.assertEqual(play["bytes"], 500)
        self.assertEqual(play["chunks"], 5)
        self.assertEqual(snap["transfer"]["totals"]["bytes"], 500)
        self.assertEqual(snap["transfer"]["totals"]["chunks"], 5)

    def test_transferred_ignores_unknown_sessions_and_bad_counts(self):
        diag, _ = _make()
        diag.media_started("s", "m")
        diag.transferred("s", 100)
        diag.transferred("missing", 999)  # no media_started: ignored
        diag.transferred("s", -5)
        diag.transferred("s", True)
        diag.transferred("s", "10")
        self.assertEqual(diag.snapshot(credential_present=True)["transfer"]["totals"]["bytes"], 100)

    @staticmethod
    def _play(diag: Diagnostics) -> dict:
        return diag.snapshot(credential_present=True)["transfer"]["plays"][0]


class BoundedStateTests(unittest.TestCase):
    def test_session_registry_is_bounded_to_lru_128(self):
        diag, _ = _make()
        for index in range(MAX_SESSIONS + 72):
            diag.media_started(f"s{index}", f"n{index}")
        snap = diag.snapshot(credential_present=True)
        names = {play["name"] for play in snap["transfer"]["plays"]}
        self.assertEqual(len(snap["transfer"]["plays"]), MAX_SESSIONS)
        self.assertIn(f"n{MAX_SESSIONS + 71}", names)
        self.assertNotIn("n0", names)
        self.assertEqual(snap["transfer"]["totals"]["plays"], MAX_SESSIONS + 72)

    def test_error_log_is_bounded_to_last_twenty(self):
        diag, _ = _make()
        for _ in range(50):
            diag.error("media", "network_error")
        errors = diag.snapshot(credential_present=True)["errors"]
        self.assertEqual(len(errors["events"]), ERROR_LOG_LIMIT)
        self.assertEqual(errors["count"], 50)


class AccountStateTests(unittest.TestCase):
    def test_stored_cookie_is_never_assumed_verified(self):
        diag, _ = _make()
        account = diag.snapshot(credential_present=True)["account"]
        self.assertEqual(account["validation"], "saved_unverified")
        self.assertIsNone(account["last_success_at"])

    def test_api_result_transitions_and_rate_limit_never_logs_out(self):
        diag, _ = _make()
        self.assertEqual(diag.snapshot(credential_present=False)["account"]["validation"], "no_login")

        diag.api_result(True)
        account = diag.snapshot(credential_present=True)["account"]
        self.assertEqual(account["validation"], "recently_valid")
        self.assertIsNotNone(account["last_success_at"])

        diag.api_result(False, "rate_limited")
        self.assertEqual(diag.snapshot(credential_present=True)["account"]["validation"], "recently_valid")
        diag.api_result(False, "access_restricted")
        self.assertEqual(diag.snapshot(credential_present=True)["account"]["validation"], "recently_valid")

        diag.api_result(False, "authentication_error")
        self.assertEqual(diag.snapshot(credential_present=True)["account"]["validation"], "expired")

    def test_expired_survives_dropped_credential_until_new_credential_or_logout(self):
        diag, _ = _make()
        diag.api_result(False, "authentication_error")
        # The service dropped the stale cookie, but the expiry stays visible.
        self.assertEqual(diag.snapshot(credential_present=False)["account"]["validation"], "expired")

        diag.credential_changed()  # new credential: unverified again
        account = diag.snapshot(credential_present=True)["account"]
        self.assertEqual(account["validation"], "saved_unverified")
        self.assertIsNone(account["last_success_at"])

        diag.api_result(False, "authentication_error")
        diag.credential_changed()  # logout
        self.assertEqual(diag.snapshot(credential_present=False)["account"]["validation"], "no_login")


class ReportTimestampTests(unittest.TestCase):
    def test_generated_at_comes_from_the_wall_clock_not_the_monotonic_clock(self):
        # Monotonic 10s would render as 1970-01-01T00:00:10Z; the report stamp
        # must instead follow the injected wall clock (fixed here at 2026-01-01).
        diag, _ = _make(now=10.0, wall=1767225600.0)
        report = diag.report(credential_present=True)
        self.assertEqual(report["generated_at"], "2026-01-01T00:00:00+00:00")
        self.assertNotIn("1970", report["generated_at"])

    def test_monotonic_clock_still_drives_elapsed_play_time(self):
        diag, clock = _make(now=0.0, wall=1767225600.0)
        diag.media_started("s", "m")
        clock.now = 12.5
        play = diag.snapshot(credential_present=True)["transfer"]["plays"][0]
        self.assertEqual(play["seconds"], 12.5)


class CooldownVisibilityTests(unittest.TestCase):
    def test_active_cooldown_is_read_only_and_visible(self):
        diag, _ = _make()
        guard = _Guard(cooldown=Cooldown(until=100.0, reason="rate_limited"), now=90.0)
        protection = diag.snapshot(credential_present=True, guard=guard)["protection"]
        self.assertTrue(protection["available"])
        self.assertTrue(protection["active"])
        self.assertEqual(protection["reason"], "rate_limited")
        self.assertAlmostEqual(protection["remaining_seconds"], 10.0)
        self.assertEqual(guard.calls, ["active_cooldown", "now"])

    def test_stopped_storage_stays_visible_with_fixed_instruction(self):
        diag, _ = _make()
        protection = diag.snapshot(credential_present=True, guard=_StoppedGuard())["protection"]
        self.assertFalse(protection["available"])
        self.assertEqual(protection["reason"], "local_protection_stopped")
        self.assertIn("instruction", protection)
        # The fixed text must send the operator to repair local storage and to
        # verify the account with the official client, not to just restart.
        self.assertIn("配置目录", protection["instruction"])
        self.assertIn("官方夸克客户端", protection["instruction"])
        report = diag.report(credential_present=True, guard=_StoppedGuard())["cooldown"]
        self.assertEqual(report["reason"], "local_protection_stopped")
        self.assertEqual(report["instruction"], protection["instruction"])

    def test_idle_and_unavailable_guard_are_reported(self):
        diag, _ = _make()
        idle = diag.report(credential_present=True, guard=_Guard())["cooldown"]
        self.assertTrue(idle["available"])
        self.assertFalse(idle["active"])
        missing = diag.snapshot(credential_present=True)["protection"]
        self.assertFalse(missing["available"])
        self.assertEqual(missing["reason"], "unavailable")


class SanitizationTests(unittest.TestCase):
    def test_report_never_carries_secrets_names_or_session_ids(self):
        diag, _ = _make()
        secret_session = "COOKIE=super-secret-session"
        diag.media_started(secret_session, "secret-name.mp4")
        diag.transferred(secret_session, 10)
        diag.origin_response(secret_session, 200, 3)
        diag.error("https://evil.invalid/leak", "token=super-secret")
        report = diag.report(credential_present=True, guard=_Guard())
        blob = json.dumps(report, ensure_ascii=False)
        for needle in ("super-secret", "COOKIE", "secret-name.mp4", "evil.invalid", "session"):
            self.assertNotIn(needle, blob)
        event = report["errors"]["events"][0]
        self.assertEqual(event["phase"], "unknown")
        self.assertEqual(event["kind"], "unknown")
        self.assertEqual(event["message"], "未知错误")
        self.assertIsNotNone(event["at"])

    def test_snapshot_keeps_local_name_but_never_the_session_id(self):
        diag, _ = _make()
        diag.media_started("COOKIE=super-secret-session", "movie.mkv")
        snapshot = diag.snapshot(credential_present=True)
        self.assertIn("movie.mkv", json.dumps(snapshot["transfer"]["plays"], ensure_ascii=False))
        self.assertNotIn("COOKIE", json.dumps(snapshot, ensure_ascii=False))

    def test_error_phase_and_kind_allowlists(self):
        diag, _ = _make()
        diag.error("media", "network_error", 502)
        diag.error("bogus-phase", "bogus-kind", 999)
        events = diag.snapshot(credential_present=True)["errors"]["events"]
        self.assertEqual((events[0]["phase"], events[0]["kind"], events[0]["status"]), ("media", "network_error", 502))
        self.assertTrue(events[0]["message"])
        self.assertEqual((events[1]["phase"], events[1]["kind"], events[1]["status"]), ("unknown", "unknown", None))

    def test_origin_status_is_bucketed_and_url_free(self):
        diag, _ = _make()
        diag.media_started("s", "m")
        diag.origin_response("s", 206, 4)
        diag.origin_response("s", 429, 30)
        diag.origin_response("unknown-session", None, 1)
        totals = diag.report(credential_present=True)["transfer"]
        self.assertEqual(totals["origin_requests"], 3)
        self.assertEqual(totals["status"], {"2xx": 1, "3xx": 0, "4xx": 1, "5xx": 0, "other": 1})
        play = diag.snapshot(credential_present=True)["transfer"]["plays"][0]
        self.assertEqual(play["origin_requests"], 2)
        self.assertEqual(play["last_status"], 429)

    def test_report_shape_is_json_safe_and_allowlisted(self):
        diag, _ = _make()
        report = diag.report(credential_present=True)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["release"], "0.6.0")
        self.assertEqual(report["protocol"], "1")
        self.assertEqual(
            set(report),
            {"schema_version", "generated_at", "release", "protocol", "python", "os",
             "account", "transfer", "cooldown", "errors"},
        )
        json.dumps(report)
        self.assertEqual(set(report["account"]), {"credential_present", "validation", "last_success_at"})


class NoOutboundTests(unittest.TestCase):
    def test_snapshot_and_report_never_open_a_socket(self):
        diag, _ = _make()
        guard = _Guard()
        diag.media_started("s", "m")
        diag.transferred("s", 10)
        diag.origin_response("s", 200, 5)
        diag.api_result(True)
        diag.error("origin", "network_error", 502)
        with mock.patch("socket.socket", side_effect=AssertionError("outbound network")):
            diag.snapshot(credential_present=True, guard=guard)
            diag.report(credential_present=True, guard=guard)
            diag.credential_changed()
        # Only the two read-only guard methods were ever touched.
        self.assertEqual(guard.calls, ["active_cooldown", "now", "active_cooldown", "now"])


if __name__ == "__main__":
    unittest.main()
