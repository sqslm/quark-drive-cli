"""Headless tests for the standalone PotPlayer status window.

Nothing here creates a Tk window: the formatting, endpoint trust checks, HTTP
client, worker timing/cancellation and export path are all exercised directly,
which is exactly the surface the service will drive in production.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import queue
import tempfile
import threading
import time
import typing
import unittest
from datetime import datetime

import httpx

from quark_cli.potplayer import status_gui
from quark_cli.potplayer.status_gui import (
    POLL_INTERVAL,
    REQUEST_TIMEOUT,
    BridgeClient,
    BridgeWorker,
    Endpoint,
    StatusWindow,
    default_export_name,
    format_bytes,
    format_duration_ms,
    format_duration_seconds,
    format_response_ms,
    format_speed,
    format_timestamp,
    load_endpoint,
    match_selection,
    protection_text,
    restart_enabled,
    run_status_window,
    state_label,
    validation_label,
)

MIB = 1024 * 1024
GIB = 1024 * 1024 * 1024
SECRET = "SUPER-SECRET-TOKEN"


def _calls_recorder(handler):
    """Wrap a mock handler and record every (method, path, authorization) seen."""
    calls: list[tuple[str, str, str | None]] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("Authorization")))
        return handler(request)

    return wrapped, calls


def _endpoint(token: str = SECRET, instance: str = "inst", port: int = 1234) -> Endpoint:
    return Endpoint(f"http://127.0.0.1:{port}", token, instance, port)


def _client(handler, **kwargs) -> BridgeClient:
    wrapped, calls = _calls_recorder(handler)
    client = BridgeClient(
        _endpoint(), transport=httpx.MockTransport(wrapped), **kwargs
    )
    client.calls = calls
    return client


def _snapshot_handler(**extra):
    payload = {"account": {}, "transfer": {"totals": {}, "plays": []}, "errors": {"count": 0, "events": []}}
    payload.update(extra)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"status": "ok", "instance": "inst"})
        return httpx.Response(200, json=payload)

    return handler


def _worker(handler, **kwargs):
    client = _client(handler)
    events: queue.Queue = queue.Queue()
    requests: queue.Queue = queue.Queue()
    return client, BridgeWorker(client, events, requests, **kwargs), events, requests


class FormattingTests(unittest.TestCase):
    def test_bytes_use_mib_and_gib(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(2048), "2.0 KiB")
        self.assertEqual(format_bytes(5 * MIB), "5.0 MiB")
        self.assertEqual(format_bytes(3 * GIB), "3.00 GiB")

    def test_speed_is_actual_mib_per_second(self):
        self.assertEqual(format_speed(0), "0.0 MiB/s")
        self.assertEqual(format_speed(5 * MIB), "5.0 MiB/s")
        self.assertEqual(format_speed(1536), "0.0 MiB/s")

    def test_response_ms_is_plain_milliseconds(self):
        self.assertEqual(format_response_ms(0), "0 ms")
        self.assertEqual(format_response_ms(120), "120 ms")
        for value in (None, "12", True, -1):
            self.assertEqual(format_response_ms(value), "—")

    def test_unknown_values_are_dashes_never_zero_or_exceptions(self):
        for value in (None, "12", True, -1, "x"):
            self.assertEqual(format_bytes(value), "—")
            self.assertEqual(format_speed(value), "—")
            self.assertEqual(format_duration_ms(value), "—")
            self.assertEqual(format_duration_seconds(value), "—")

    def test_durations_are_hh_mm_ss(self):
        self.assertEqual(format_duration_seconds(0), "00:00:00")
        self.assertEqual(format_duration_seconds(59), "00:00:59")
        self.assertEqual(format_duration_seconds(3661), "01:01:01")
        self.assertEqual(format_duration_ms(3661000), "01:01:01")
        self.assertEqual(format_duration_ms(999), "00:00:00")

    def test_timestamps_are_local_clock_times(self):
        rendered = format_timestamp("2026-01-01T00:00:00+00:00")
        self.assertRegex(rendered, r"^\d{2}:\d{2}:\d{2}$")
        self.assertEqual(format_timestamp(None), "—")
        self.assertEqual(format_timestamp("not-a-time"), "—")
        self.assertEqual(format_timestamp(""), "—")

    def test_saved_credential_is_pending_not_normal(self):
        label = validation_label("saved_unverified")
        self.assertNotIn("正常", label)
        self.assertIn("待验证", label)
        self.assertEqual(validation_label("no_login"), "未登录")
        self.assertEqual(validation_label("expired"), "登录已失效")
        self.assertEqual(validation_label("recently_valid"), "最近调用成功")
        self.assertEqual(validation_label("bogus"), "未知状态")

    def test_state_labels_are_chinese_and_bounded(self):
        self.assertEqual(state_label("playing"), "正在播放")
        self.assertEqual(state_label("stopped"), "已停止")
        self.assertEqual(state_label("whatever"), "未知状态")

    def test_protection_text_covers_every_documented_shape(self):
        stopped = {
            "available": False,
            "active": False,
            "reason": "local_protection_stopped",
            "remaining_seconds": 0.0,
            "instruction": "固定说明",
        }
        self.assertEqual(protection_text(stopped), "固定说明")
        self.assertIn("冷却中", protection_text(
            {"available": True, "active": True, "reason": "rate_limited", "remaining_seconds": 9.6}
        ))
        self.assertIn("00:00:09", protection_text(
            {"available": True, "active": True, "reason": "rate_limited", "remaining_seconds": 9.6}
        ))
        self.assertEqual(protection_text({"available": True, "active": False, "reason": None}), "无冷却")
        self.assertIn("不可用", protection_text({"available": False, "reason": "unavailable"}))
        self.assertIn("不可用", protection_text(None))


class EndpointTests(unittest.TestCase):
    def test_valid_runtime_record_becomes_a_loopback_endpoint(self):
        endpoint = load_endpoint(lambda: {"port": 4321, "token": "tok", "instance": "abc"})
        self.assertEqual(endpoint, Endpoint("http://127.0.0.1:4321", "tok", "abc", 4321))

    def test_any_host_in_the_runtime_record_is_ignored(self):
        endpoint = load_endpoint(
            lambda: {
                "host": "evil.example.com",
                "base_url": "http://evil.example.com:80",
                "url": "http://evil.example.com",
                "port": 4321,
                "token": "tok",
                "instance": "abc",
            }
        )
        self.assertEqual(endpoint.base_url, "http://127.0.0.1:4321")

    def test_incomplete_or_bogus_records_are_rejected(self):
        for entry in (
            None,
            "not-a-dict",
            {},
            {"port": 4321, "token": "tok"},
            {"port": 4321, "instance": "abc"},
            {"port": 4321, "token": "", "instance": "abc"},
            {"port": True, "token": "tok", "instance": "abc"},
            {"port": "4321", "token": "tok", "instance": "abc"},
            {"port": 0, "token": "tok", "instance": "abc"},
            {"port": 70000, "token": "tok", "instance": "abc"},
        ):
            self.assertIsNone(load_endpoint(lambda entry=entry: entry))


class ClientTests(unittest.TestCase):
    def test_snapshot_is_one_bearer_authorized_diagnostics_get(self):
        client = _client(_snapshot_handler())
        self.assertEqual(client.snapshot()["transfer"]["totals"], {})
        self.assertEqual(client.calls, [("GET", "/v1/diagnostics", f"Bearer {SECRET}")])

    def test_client_ignores_environment_proxies_and_bounds_timeout(self):
        client = _client(_snapshot_handler())
        self.assertIs(client._client.trust_env, False)
        self.assertEqual(client._client.timeout.connect, REQUEST_TIMEOUT)
        self.assertEqual(REQUEST_TIMEOUT, 3.0)

    def test_health_matches_the_recorded_instance(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok", "instance": "other"})

        self.assertTrue(_client(_snapshot_handler()).healthy())

        def wrong(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/health":
                return httpx.Response(200, json={"status": "ok", "instance": "other"})
            return httpx.Response(200, json={})

        self.assertFalse(_client(wrong).healthy())

    def test_health_false_on_errors_and_never_raises(self):
        def dead(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        def broken(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        for handler in (dead, broken):
            client = _client(handler)
            self.assertFalse(client.healthy())
            self.assertIsNone(client.snapshot())
            self.assertIsNone(client.report())
            client.close()

    def test_restart_posts_only_player_pid_and_fid(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        client = _client(handler)
        ok, message = client.restart(7, "FID-1")
        self.assertTrue(ok)
        self.assertEqual(message, status_gui._RESTART_OK)
        self.assertEqual(seen, [{"player_pid": 7, "fid": "FID-1"}])

    def test_restart_failures_use_fixed_messages_without_echoing_upstream_text(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={"error": {"type": "rate_limited", "message": f"token={SECRET} http://evil"}},
            )

        client = _client(handler)
        ok, message = client.restart(7, "FID-1")
        self.assertFalse(ok)
        self.assertEqual(message, "上游限流，请稍后重试")
        self.assertNotIn(SECRET, message)
        self.assertNotIn("http", message)

    def test_restart_unknown_error_type_falls_back_to_a_fixed_message(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"type": "brand_new_kind"}})

        ok, message = _client(handler).restart(1, "F")
        self.assertFalse(ok)
        self.assertEqual(message, status_gui._RESTART_FAILED)

    def test_restart_transport_failure_is_bounded_and_fixed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow", request=request)

        ok, message = _client(handler).restart(1, "F")
        self.assertFalse(ok)
        self.assertEqual(message, status_gui._RESTART_FAILED)


class WorkerTests(unittest.TestCase):
    def test_tick_reports_snapshot_failed_and_disconnected(self):
        _, worker, events, _ = _worker(_snapshot_handler())
        worker.tick()
        self.assertEqual(events.get_nowait()[0], "snapshot")
        self.assertEqual(worker.polls, 1)

        def unauthorised(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/health":
                return httpx.Response(200, json={"status": "ok", "instance": "inst"})
            return httpx.Response(401, json={"error": {"type": "unauthorized"}})

        _, worker, events, _ = _worker(unauthorised)
        worker.tick()
        self.assertEqual(events.get_nowait()[0], "failed")

        def dead(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        _, worker, events, _ = _worker(dead)
        worker.tick()
        self.assertEqual(events.get_nowait()[0], "disconnected")

    def test_every_tick_is_exactly_one_diagnostics_get(self):
        client, worker, _, _ = _worker(_snapshot_handler())
        for _ in range(3):
            worker.tick()
        self.assertEqual(len(client.calls), 3)
        self.assertEqual([call[1] for call in client.calls], ["/v1/diagnostics"] * 3)

    def test_failures_are_never_logged_and_never_leak_the_token_or_url(self):
        def dead(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        _, worker, events, requests = _worker(dead)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            worker.tick()
            worker._serve(("export", os.path.join(tempfile.gettempdir(), "does-not-exist-dir", "x.json")))
            worker._serve(("restart", 1, "F"))
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")
        dumped = repr(list(events.queue))
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn("/v1/", dumped)
        self.assertNotIn("127.0.0.1", dumped)

    def test_export_writes_utf8_json_bytes_from_the_report_endpoint(self):
        report = {"generated_at": "2026-01-01T00:00:00+00:00", "message": "中文说明", "errors": {"count": 0}}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/diagnostics/report":
                return httpx.Response(200, json=report)
            return httpx.Response(200, json={"transfer": {"totals": {}, "plays": []}})

        _, worker, events, requests = _worker(handler)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "report.json")
            worker._serve(("export", path))
            self.assertEqual(events.get_nowait(), ("exported", path))
            with open(path, "rb") as handle:
                raw = handle.read()
        self.assertEqual(raw, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
        self.assertIn("中文说明".encode("utf-8"), raw)

    def test_export_without_a_report_writes_nothing(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/diagnostics/report":
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json={})

        _, worker, events, _ = _worker(handler)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "report.json")
            worker._serve(("export", path))
            self.assertEqual(events.get_nowait(), ("export_failed", status_gui._EXPORT_FAILED_REPORT))
            self.assertFalse(os.path.exists(path))

    def test_export_to_an_unwritable_path_is_a_fixed_note(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/diagnostics/report":
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(200, json={})

        _, worker, events, _ = _worker(handler)
        target = os.path.join(tempfile.gettempdir(), "quark-missing-dir-xyz", "report.json")
        worker._serve(("export", target))
        self.assertEqual(events.get_nowait(), ("export_failed", status_gui._EXPORT_FAILED_WRITE))

    def test_restart_command_returns_a_fixed_result(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"type": "access_restricted", "message": SECRET}})

        _, worker, events, _ = _worker(handler)
        worker._serve(("restart", 3, "FID"))
        kind, (ok, message) = events.get_nowait()
        self.assertEqual(kind, "restart_done")
        self.assertFalse(ok)
        self.assertEqual(message, "上游拒绝访问，请稍后重试")
        self.assertNotIn(SECRET, message)

    def test_run_keeps_one_poll_per_interval_with_no_overlap(self):
        lock = threading.Lock()
        concurrent = 0
        peak = 0
        stamps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal concurrent, peak
            if request.url.path == "/v1/diagnostics":
                with lock:
                    concurrent += 1
                    peak = max(peak, concurrent)
                    stamps.append(time.monotonic())
                time.sleep(0.01)
                with lock:
                    concurrent -= 1
            return httpx.Response(200, json={"transfer": {"totals": {}, "plays": []}})

        client, worker, events, requests = _worker(handler, interval=0.05)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "out.json")
            requests.put(("export", path))
            thread = threading.Thread(target=worker.run, daemon=True)
            thread.start()
            exported = None
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                try:
                    kind, value = events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if kind == "exported":
                    exported = value
            worker.cancel()
            thread.join(timeout=REQUEST_TIMEOUT + 1.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(exported, path)
            self.assertTrue(os.path.exists(path))
        self.assertEqual(peak, 1)  # never two diagnostics polls in flight
        self.assertEqual(len(stamps), sum(1 for call in client.calls if call[1] == "/v1/diagnostics"))
        self.assertGreaterEqual(len(stamps), 3)
        gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:])]
        for gap in gaps:
            self.assertGreaterEqual(gap, 0.04)

    def test_run_cancels_cleanly_while_a_poll_is_in_flight(self):
        entered = threading.Event()
        release = threading.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            entered.set()
            release.wait(2.0)
            raise httpx.ConnectError("refused", request=request)

        _, worker, events, _ = _worker(handler, interval=POLL_INTERVAL)
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(2.0))
        worker.cancel()
        release.set()
        thread.join(timeout=REQUEST_TIMEOUT + 1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(worker.polls, 1)  # the cancelled in-flight poll is the last
        self.assertEqual(events.get_nowait()[0], "disconnected")


class SelectionTests(unittest.TestCase):
    ROWS = [
        {"player_pid": 1, "fid": "F1", "state": "playing"},
        {"player_pid": 2, "fid": "F2", "state": "stopped"},
    ]

    def test_match_requires_both_pid_and_fid(self):
        self.assertEqual(match_selection(self.ROWS, (1, "F1"))["fid"], "F1")
        self.assertIsNone(match_selection(self.ROWS, (1, "F2")))
        self.assertIsNone(match_selection(self.ROWS, (2, "F1")))
        self.assertIsNone(match_selection(self.ROWS, None))
        self.assertIsNone(match_selection("nope", (1, "F1")))

    def test_restart_requires_connected_matched_active_playback(self):
        self.assertTrue(restart_enabled(True, self.ROWS, (1, "F1")))
        self.assertFalse(restart_enabled(False, self.ROWS, (1, "F1")))
        self.assertFalse(restart_enabled(True, self.ROWS, (2, "F2")))  # stopped
        self.assertFalse(restart_enabled(True, self.ROWS, (9, "F9")))  # unmatched
        self.assertFalse(restart_enabled(True, self.ROWS, None))
        self.assertFalse(restart_enabled(True, self.ROWS, (1, "F1"), pending=True))

    def test_restart_rejects_bogus_identifiers(self):
        rows = [{"player_pid": "1", "fid": "F1", "state": "playing"},
                {"player_pid": 1, "fid": "", "state": "playing"},
                {"player_pid": 1, "fid": "F1", "state": "playing"}]
        self.assertFalse(restart_enabled(True, rows, ("1", "F1")))
        self.assertFalse(restart_enabled(True, rows, (1, "")))
        self.assertTrue(restart_enabled(True, rows, (1, "F1")))


class _FakeVar:
    """Minimal stand-in for a Tk StringVar: records the last set value."""

    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value


class _FakeTree:
    """Records the rows a real ttk.Treeview would show; no Tk involved."""

    def __init__(self) -> None:
        self.rows = []

    def delete(self, *items) -> None:
        self.rows.clear()

    def get_children(self):
        return tuple(iid for iid, _ in self.rows)

    def insert(self, parent, index, iid=None, values=()) -> None:
        self.rows.append((iid, tuple(values)))


class TransferRenderTests(unittest.TestCase):
    def _window(self, snapshot: dict) -> StatusWindow:
        window = StatusWindow(None, None)
        window._transfer_tree = _FakeTree()
        window._transfer_var = _FakeVar()
        window._refresh_transfer(snapshot)
        return window

    def test_overview_keeps_overall_speed_and_total_without_guessed_rtt(self):
        snapshot = {
            "transfer": {
                "totals": {"speed_bytes_per_second": 5 * MIB, "bytes": 3 * GIB},
                "plays": [],
            }
        }
        window = self._window(snapshot)
        self.assertIn("整体速度（5 秒）：5.0 MiB/s", window._transfer_var.value)
        self.assertIn("累计传输：3.00 GiB", window._transfer_var.value)
        self.assertNotIn("ms", window._transfer_var.value)
        self.assertEqual(window._transfer_tree.rows, [])

    def test_each_session_row_renders_its_own_bytes_speed_and_rtt(self):
        snapshot = {
            "transfer": {
                "totals": {},
                "plays": [
                    {"name": "A.mkv", "bytes": 5 * MIB, "speed_bytes_per_second": 2 * MIB, "last_elapsed_ms": 120},
                    {"name": "B.mkv", "bytes": 1024, "speed_bytes_per_second": 0, "last_elapsed_ms": 340},
                ],
            }
        }
        window = self._window(snapshot)
        self.assertEqual(
            window._transfer_tree.rows,
            [
                ("play0", ("A.mkv", "5.0 MiB", "2.0 MiB/s", "120 ms")),
                ("play1", ("B.mkv", "1.0 KiB", "0.0 MiB/s", "340 ms")),
            ],
        )

    def test_rows_tolerate_missing_fields_and_skip_non_dicts(self):
        window = self._window({"transfer": {"plays": [{"name": "A"}, None, "x"]}})
        self.assertEqual(window._transfer_tree.rows, [("play0", ("A", "—", "—", "—"))])


class NamingTests(unittest.TestCase):
    def test_default_export_name_has_a_sortable_timestamp(self):
        name = default_export_name(datetime(2026, 10, 10, 17, 24, 15))
        self.assertEqual(name, "quark-diagnostics-20261010-172415.json")
        self.assertRegex(default_export_name(), r"^quark-diagnostics-\d{8}-\d{6}\.json$")


class PublicApiTests(unittest.TestCase):
    def test_run_status_window_is_a_zero_argument_int_entry_point(self):
        signature = inspect.signature(run_status_window)
        self.assertEqual(list(signature.parameters), ["runtime_reader"])
        self.assertIs(typing.get_type_hints(run_status_window)["return"], int)


if __name__ == "__main__":
    unittest.main()
