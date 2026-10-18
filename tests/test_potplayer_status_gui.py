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

from quark_cli.potplayer import gui_common
from quark_cli.potplayer.status_gui import BridgeClient, BridgeWorker, StatusWindow, run_status_window
from quark_cli.potplayer.highlights_gui import HighlightsClient, HighlightsWorker, HighlightsPanel
from quark_cli.potplayer.gui_common import (
    POLL_INTERVAL,
    REQUEST_TIMEOUT,
    Endpoint,
    bookmark_action_enabled,
    bookmark_enabled,
    bookmark_id_of,
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
    selection_target,
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
        self.assertEqual(message, gui_common._RESTART_OK)
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
        self.assertEqual(message, gui_common._RESTART_FAILED)

    def test_restart_transport_failure_is_bounded_and_fixed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow", request=request)

        ok, message = _client(handler).restart(1, "F")
        self.assertFalse(ok)
        self.assertEqual(message, gui_common._RESTART_FAILED)


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
            self.assertEqual(events.get_nowait(), ("export_failed", gui_common._EXPORT_FAILED_REPORT))
            self.assertFalse(os.path.exists(path))

    def test_export_to_an_unwritable_path_is_a_fixed_note(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/diagnostics/report":
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(200, json={})

        _, worker, events, _ = _worker(handler)
        target = os.path.join(tempfile.gettempdir(), "quark-missing-dir-xyz", "report.json")
        worker._serve(("export", target))
        self.assertEqual(events.get_nowait(), ("export_failed", gui_common._EXPORT_FAILED_WRITE))

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

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class _FakeTree:
    """Records the rows a real ttk.Treeview would show; no Tk involved."""

    def __init__(self) -> None:
        self.rows = []
        self.selected: tuple = ()

    def delete(self, *items) -> None:
        self.rows.clear()
        self.selected = ()

    def get_children(self):
        return tuple(iid for iid, _ in self.rows)

    def insert(self, parent, index, iid=None, values=()) -> None:
        self.rows.append((iid, tuple(values)))

    def selection(self):
        return self.selected

    def selection_set(self, *items) -> None:
        self.selected = tuple(items)

    def focus(self, *items) -> None:
        pass


class _FakeButton:
    """Records the enable/disable calls a real ttk button would receive."""

    def __init__(self) -> None:
        self.states: list[tuple] = []

    def state(self, value) -> None:
        self.states.append(tuple(value))

    @property
    def enabled(self) -> bool:
        return self.states[-1] == ("!disabled",) if self.states else True


class _FakeToplevel:
    """A bookmark window stand-in: existence, destruction and lift only."""

    def __init__(self) -> None:
        self.destroyed = False
        self.lifted = 0

    def winfo_exists(self) -> bool:
        return not self.destroyed

    def destroy(self) -> None:
        self.destroyed = True

    def lift(self) -> None:
        self.lifted += 1


class _FakeDialog:
    """Stand-in for ``tkinter.simpledialog``; may mutate state mid-dialog."""

    def __init__(self, answer=None, error=None, on_call=None) -> None:
        self.answer = answer
        self.error = error
        self.on_call = on_call
        self.calls: list[tuple] = []

    def askstring(self, title, prompt, parent=None, initialvalue=""):
        self.calls.append((title, prompt, parent, initialvalue))
        if self.on_call is not None:
            # ``simpledialog`` re-enters mainloop, so worker events run here.
            self.on_call()
        if self.error is not None:
            raise self.error
        return self.answer


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


BOOKMARK_ROWS = [
    {"player_pid": 1, "fid": "F1", "name": "A.mkv", "state": "playing"},
    {"player_pid": 2, "fid": "F2", "name": "B.mkv", "state": "stopped"},
    {"player_pid": 3, "fid": "F3", "name": "C.mkv", "state": "playing"},
]

BOOKMARK_ENTRY = {
    "id": "b1",
    "fid": "F1",
    "name": "A.mkv",
    "size": 10,
    "position_ms": 3_661_000,
    "duration_ms": 7_200_000,
    "note": "第 3 分钟",
}


def _highlights_panel(rows=None, selected=(1, "F1"), connected=True, dialog=None) -> HighlightsPanel:
    """A HighlightsPanel wired to fake widgets: no Tk object is ever created."""
    window = HighlightsPanel(None, None)
    window._root = object()
    window._connected = connected
    window._playback = list(BOOKMARK_ROWS if rows is None else rows)
    window._selected = selected
    window._transfer_var = _FakeVar()
    window._account_var = _FakeVar()
    window._protection_var = _FakeVar()
    window._notice_var = _FakeVar()
    window._transfer_tree = _FakeTree()
    window._playback_tree = _FakeTree()
    window._errors_tree = _FakeTree()
    window._restart_button = _FakeButton()
    window._export_button = _FakeButton()
    window._bookmark_button = _FakeButton()
    window._bookmark_window = _FakeToplevel()
    window._bookmark_tree = _FakeTree()
    window._bookmark_header_var = _FakeVar()
    window._bookmark_notice_var = _FakeVar()
    window._bookmark_add_button = _FakeButton()
    window._bookmark_jump_button = _FakeButton()
    window._bookmark_rename_button = _FakeButton()
    window._bookmark_remove_button = _FakeButton()
    window._bookmark_refresh_button = _FakeButton()
    window._simpledialog = dialog
    return window


class BookmarkClientTests(unittest.TestCase):
    def _client(self, handler) -> BridgeClient:
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        client = HighlightsClient(_endpoint(), transport=httpx.MockTransport(wrapped))
        client.seen = seen
        return client

    def test_list_reads_one_video_by_fid_with_the_bearer_token(self):
        client = self._client(
            lambda request: httpx.Response(200, json={"items": [dict(BOOKMARK_ENTRY), "junk"]})
        )
        self.assertEqual(client.bookmarks("F1"), [BOOKMARK_ENTRY])
        (request,) = client.seen
        self.assertEqual((request.method, request.url.path), ("GET", "/v1/bookmarks"))
        self.assertEqual(request.url.params["fid"], "F1")
        self.assertEqual(request.headers["Authorization"], f"Bearer {SECRET}")

    def test_list_quotes_a_fid_that_needs_escaping(self):
        client = self._client(lambda request: httpx.Response(200, json={"items": []}))
        self.assertEqual(client.bookmarks("F 1/2?x#y"), [])
        self.assertEqual(client.seen[0].url.params["fid"], "F 1/2?x#y")

    def test_global_list_and_cold_play_keep_their_distinct_contracts(self):
        client = self._client(lambda request: httpx.Response(200, json={"items": []}))
        self.addCleanup(client.close)
        self.assertEqual(client.bookmarks(""), [])
        self.assertEqual(client.seen[-1].url.query, b"")
        self.assertTrue(client.play_bookmark("b1"))
        request = client.seen[-1]
        self.assertEqual(request.url.path, "/v1/bookmarks/play")
        self.assertEqual(json.loads(request.content), {"id": "b1"})

    def test_empty_list_is_a_valid_answer_while_failures_are_none(self):
        empty = self._client(lambda request: httpx.Response(200, json={"items": []}))
        self.assertEqual(empty.bookmarks("F1"), [])
        for response in (
            httpx.Response(500, text="boom"),
            httpx.Response(401, json={"error": {"type": "unauthorized", "message": SECRET}}),
            httpx.Response(200, text="not json"),
            httpx.Response(200, json={"items": "nope"}),
        ):
            client = self._client(lambda request, response=response: response)
            self.assertIsNone(client.bookmarks("F1"))
            client.close()

    def test_add_posts_the_binding_and_the_note(self):
        client = self._client(lambda request: httpx.Response(200, json={"bookmark": dict(BOOKMARK_ENTRY)}))
        self.assertTrue(client.add_bookmark(7, "F1", "第 3 分钟"))
        (request,) = client.seen
        self.assertEqual((request.method, request.url.path), ("POST", "/v1/bookmarks/add"))
        self.assertEqual(
            json.loads(request.content), {"player_pid": 7, "fid": "F1", "note": "第 3 分钟"}
        )
        self.assertEqual(request.headers["Authorization"], f"Bearer {SECRET}")

    def test_rename_remove_and_jump_post_exactly_the_documented_fields(self):
        client = self._client(lambda request: httpx.Response(200, json={"status": "ok"}))
        self.assertTrue(client.rename_bookmark("b1", "新备注"))
        self.assertTrue(client.remove_bookmark("b1"))
        self.assertTrue(client.jump_bookmark(7, "F1", "b1"))
        self.assertEqual(
            [(request.url.path, json.loads(request.content)) for request in client.seen],
            [
                ("/v1/bookmarks/rename", {"id": "b1", "note": "新备注"}),
                ("/v1/bookmarks/remove", {"id": "b1"}),
                ("/v1/bookmarks/jump", {"player_pid": 7, "fid": "F1", "id": "b1"}),
            ],
        )

    def test_any_2xx_is_accepted_and_non_2xx_is_a_plain_false(self):
        for status, expected in ((200, True), (201, True), (202, True), (400, False), (401, False), (409, False), (500, False)):
            client = self._client(
                lambda request, status=status: httpx.Response(
                    status, json={"error": {"type": "boom", "message": f"token={SECRET}"}}
                )
            )
            self.assertIs(client.add_bookmark(1, "F1", "备注"), expected)
            self.assertIs(client.rename_bookmark("b1", "备注"), expected)
            self.assertIs(client.remove_bookmark("b1"), expected)
            self.assertIs(client.jump_bookmark(1, "F1", "b1"), expected)

    def test_transport_failure_never_raises_and_never_leaks_text(self):
        def dead(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = self._client(dead)
        self.assertIsNone(client.bookmarks("F1"))
        self.assertFalse(client.add_bookmark(1, "F1", "备注"))
        self.assertFalse(client.rename_bookmark("b1", "备注"))
        self.assertFalse(client.remove_bookmark("b1"))
        self.assertFalse(client.jump_bookmark(1, "F1", "b1"))

    def test_bookmark_calls_only_touch_the_bookmark_endpoints(self):
        client = self._client(lambda request: httpx.Response(200, json={"items": [], "status": "ok"}))
        client.bookmarks("F1")
        client.add_bookmark(1, "F1", "备注")
        client.rename_bookmark("b1", "备注")
        client.remove_bookmark("b1")
        client.jump_bookmark(1, "F1", "b1")
        self.assertEqual(
            {request.url.path for request in client.seen},
            {
                "/v1/bookmarks",
                "/v1/bookmarks/add",
                "/v1/bookmarks/rename",
                "/v1/bookmarks/remove",
                "/v1/bookmarks/jump",
            },
        )


class BookmarkWorkerTests(unittest.TestCase):
    def _worker(self, handler):
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        client = HighlightsClient(_endpoint(), transport=httpx.MockTransport(wrapped))
        events: queue.Queue = queue.Queue()
        requests: queue.Queue = queue.Queue()
        client.seen = seen
        return client, HighlightsWorker(client, events, requests), events, requests

    def test_every_bookmark_command_is_served_with_its_binding_echoed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"items": [dict(BOOKMARK_ENTRY)]})
            return httpx.Response(200, json={"status": "ok"})

        client, worker, events, _ = self._worker(handler)
        worker._serve(("bookmarks", 1, "F1"))
        self.assertEqual(events.get_nowait(), ("bookmarks", (1, "F1", [BOOKMARK_ENTRY])))
        worker._serve(("bookmark_add", 1, "F1", "备注"))
        self.assertEqual(events.get_nowait(), ("bookmark_added", (1, "F1", True)))
        worker._serve(("bookmark_rename", 1, "F1", "b1", "新备注"))
        self.assertEqual(events.get_nowait(), ("bookmark_renamed", (1, "F1", True)))
        worker._serve(("bookmark_remove", 1, "F1", "b1"))
        self.assertEqual(events.get_nowait(), ("bookmark_removed", (1, "F1", True)))
        worker._serve(("bookmark_jump", 1, "F1", "b1"))
        self.assertEqual(events.get_nowait(), ("bookmark_jumped", (1, "F1", True)))
        self.assertEqual(
            [request.url.path for request in client.seen],
            [
                "/v1/bookmarks",
                "/v1/bookmarks/add",
                "/v1/bookmarks/rename",
                "/v1/bookmarks/remove",
                "/v1/bookmarks/jump",
            ],
        )

    def test_cold_play_echoes_the_global_list_scope_for_the_result(self):
        client, worker, events, _ = self._worker(lambda request: httpx.Response(202, json={"status": "opening"}))
        self.addCleanup(client.close)
        worker._serve(("bookmark_play", 0, "", "b1"))
        self.assertEqual(events.get_nowait(), ("bookmark_jumped", (0, "", True)))
        self.assertEqual(json.loads(client.seen[-1].content), {"id": "b1"})

    def test_failed_or_dead_requests_become_fixed_false_events(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": {"type": "remote_error", "message": SECRET}})

        _, worker, events, _ = self._worker(handler)
        worker._serve(("bookmark_add", 1, "F1", "备注"))
        self.assertEqual(events.get_nowait(), ("bookmark_added", (1, "F1", False)))

        def dead(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        _, worker, events, _ = self._worker(dead)
        worker._serve(("bookmarks", 1, "F1"))
        self.assertEqual(events.get_nowait(), ("bookmarks", (1, "F1", None)))

    def test_bookmark_events_never_leak_token_url_or_upstream_text(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                500, json={"error": {"message": f"token={SECRET} http://evil.example/x"}}
            )

        _, worker, events, _ = self._worker(handler)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            worker._serve(("bookmarks", 1, "F1"))
            worker._serve(("bookmark_add", 1, "F1", "备注"))
            worker._serve(("bookmark_rename", 1, "F1", "b1", "备注"))
            worker._serve(("bookmark_remove", 1, "F1", "b1"))
            worker._serve(("bookmark_jump", 1, "F1", "b1"))
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")
        dumped = repr(list(events.queue))
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn("/v1/", dumped)
        self.assertNotIn("127.0.0.1", dumped)
        self.assertNotIn("evil.example", dumped)


class BookmarkSelectionTests(unittest.TestCase):
    ROWS = [
        {"player_pid": 1, "fid": "F1", "name": "A.mkv", "state": "playing"},
        {"player_pid": 2, "fid": "F2", "name": "B.mkv", "state": "stopped"},
    ]

    def test_target_requires_both_identifiers(self):
        self.assertEqual(selection_target(self.ROWS, (1, "F1")), (1, "F1"))
        self.assertIsNone(selection_target(self.ROWS, (1, "F2")))
        self.assertIsNone(selection_target(self.ROWS, None))
        self.assertIsNone(selection_target("nope", (1, "F1")))

    def test_target_rejects_bogus_identifiers(self):
        rows = [
            {"player_pid": "1", "fid": "F1"},
            {"player_pid": 1, "fid": ""},
            {"player_pid": True, "fid": "F1"},
        ]
        for selected in (("1", "F1"), (1, ""), (True, "F1")):
            self.assertIsNone(selection_target(rows, selected))

    def test_listing_needs_a_connection_and_a_matched_binding(self):
        self.assertTrue(bookmark_enabled(True, self.ROWS, (1, "F1")))
        self.assertTrue(bookmark_enabled(True, self.ROWS, (2, "F2")))  # stopped still lists
        self.assertFalse(bookmark_enabled(False, self.ROWS, (1, "F1")))
        self.assertFalse(bookmark_enabled(True, self.ROWS, (9, "F9")))
        self.assertFalse(bookmark_enabled(True, self.ROWS, None))
        self.assertFalse(bookmark_enabled(True, self.ROWS, (1, "F1"), pending=True))

    def test_adding_and_jumping_need_a_binding_that_still_holds_the_file(self):
        self.assertTrue(bookmark_action_enabled(True, self.ROWS, (1, "F1")))
        self.assertFalse(bookmark_action_enabled(True, self.ROWS, (2, "F2")))
        self.assertFalse(bookmark_action_enabled(False, self.ROWS, (1, "F1")))
        self.assertFalse(bookmark_action_enabled(True, self.ROWS, (1, "F1"), pending=True))

    def test_a_suspended_or_mismatched_binding_cannot_act_natively(self):
        rows = [
            {"player_pid": 1, "fid": "F1", "state": "suspended"},  # same-FID multi-instance
            {"player_pid": 2, "fid": "F2", "state": "mismatch"},
            {"player_pid": 3, "fid": "F3", "state": "playing"},
        ]
        for selected in ((1, "F1"), (2, "F2")):
            self.assertFalse(bookmark_action_enabled(True, rows, selected))
            self.assertFalse(restart_enabled(True, rows, selected))
        self.assertTrue(bookmark_action_enabled(True, rows, (3, "F3")))
        self.assertTrue(restart_enabled(True, rows, (3, "F3")))
        # Listing stays local-only, so a suspended binding may still be read.
        self.assertTrue(bookmark_enabled(True, rows, (1, "F1")))

    def test_bookmark_ids_accept_non_empty_strings_and_ints_only(self):
        self.assertEqual(bookmark_id_of({"id": "b1"}), "b1")
        self.assertEqual(bookmark_id_of({"id": 3}), 3)
        for entry in ({"id": ""}, {"id": True}, {"id": None}, {"id": 1.5}, {}, "nope", None):
            self.assertIsNone(bookmark_id_of(entry))


class BookmarkWindowTests(unittest.TestCase):
    def test_opening_reads_only_the_selected_binding(self):
        window = _highlights_panel()
        window._open_bookmarks()
        self.assertEqual(window._requests.get_nowait(), ("bookmarks", 1, "F1"))
        self.assertEqual(window._bookmark_pending, (1, "F1"))
        self.assertEqual(window._bookmark_notice_var.value, gui_common._BOOKMARK_LOADING)
        self.assertTrue(window._requests.empty())

    def test_opening_without_a_matched_row_never_asks_the_bridge(self):
        for selected in (None, (9, "F9")):
            window = _highlights_panel(selected=selected)
            window._open_bookmarks()
            self.assertTrue(window._requests.empty())

    def test_a_snapshot_rebuild_keeps_the_selection_and_never_rereads(self):
        window = _highlights_panel()
        window._bookmark_window = _FakeToplevel()
        window._refresh_playback()
        self.assertEqual(window._selected, (1, "F1"))
        self.assertIn("A.mkv", window._bookmark_header_var.value)
        self.assertTrue(window._requests.empty())

    def test_a_binding_that_disappeared_from_the_snapshot_drops_the_list(self):
        window = _highlights_panel()
        window._bookmarks = [dict(BOOKMARK_ENTRY)]
        window._render_bookmarks()
        window._playback = [dict(BOOKMARK_ROWS[1])]
        window._refresh_playback()
        self.assertIsNone(window._selected)
        self.assertEqual(window._bookmarks, [])
        self.assertEqual(window._bookmark_tree.rows, [])
        self.assertEqual(window._bookmark_rows, {})
        self.assertTrue(window._requests.empty())

    def test_selecting_another_row_clears_and_rereads_once(self):
        window = _highlights_panel()
        window._bookmarks = [dict(BOOKMARK_ENTRY)]
        window._render_bookmarks()
        window._rows = {"row0": (1, "F1"), "row1": (2, "F2")}
        window._playback_tree.selected = ("row1",)
        window._on_select()
        window._on_select()  # the same selection again is not a change
        self.assertEqual(window._selected, (2, "F2"))
        self.assertEqual(window._bookmarks, [])
        self.assertEqual(window._bookmark_tree.rows, [])
        self.assertEqual(window._requests.get_nowait(), ("bookmarks", 2, "F2"))
        self.assertTrue(window._requests.empty())

    def test_a_late_list_for_another_binding_is_dropped(self):
        window = _highlights_panel()
        window._bookmark_pending = (1, "F1")
        window._handle("bookmarks", (2, "F2", [{"id": "b9", "position_ms": 0, "note": "别的"}]))
        self.assertEqual(window._bookmarks, [])
        self.assertEqual(window._bookmark_pending, (1, "F1"))
        window._handle("bookmarks", (1, "F1", [dict(BOOKMARK_ENTRY), "junk", {"note": "无 id"}]))
        self.assertIsNone(window._bookmark_pending)
        self.assertEqual(window._bookmark_tree.rows, [("mark0", ("A.mkv", "01:01:01", "第 3 分钟", '未分类'))])
        self.assertEqual(window._bookmark_rows, {"mark0": "b1"})
        self.assertIn("精彩标记：1 条", window._bookmark_header_var.value)

    def test_a_failed_list_is_one_fixed_message(self):
        window = _highlights_panel()
        window._bookmark_pending = (1, "F1")
        window._handle("bookmarks", (1, "F1", None))
        self.assertEqual(window._bookmark_notice_var.value, gui_common._BOOKMARK_LOAD_FAILED)
        self.assertEqual(window._bookmark_tree.rows, [])
        self.assertIsNone(window._bookmark_pending)

    def test_a_loaded_list_ends_the_reading_notice(self):
        window = _highlights_panel()
        window._open_bookmarks()
        self.assertEqual(window._bookmark_notice_var.value, gui_common._BOOKMARK_LOADING)
        window._handle("bookmarks", (1, "F1", [dict(BOOKMARK_ENTRY)]))
        self.assertNotEqual(window._bookmark_notice_var.value, gui_common._BOOKMARK_LOADING)
        self.assertIn("已读取", window._bookmark_notice_var.value)
        self.assertIn("1", window._bookmark_notice_var.value)
        self.assertIsNone(window._bookmark_pending)
        self.assertTrue(window._bookmark_button.enabled)

    def test_a_command_refresh_does_not_erase_the_command_result(self):
        window = _highlights_panel()
        window._bookmark_pending = (1, "F1")
        window._handle("bookmark_added", (1, "F1", True))
        self.assertEqual(window._bookmark_notice_var.value, gui_common._BOOKMARK_ADDED)
        # The add triggered an announce=False re-read; its answer must not
        # overwrite the command's own success message with a load message.
        window._handle("bookmarks", (1, "F1", [dict(BOOKMARK_ENTRY)]))
        self.assertEqual(window._bookmark_notice_var.value, gui_common._BOOKMARK_ADDED)
        self.assertIsNone(window._bookmark_pending)

    def test_a_late_command_answer_for_another_binding_is_dropped(self):
        window = _highlights_panel()
        window._bookmark_pending = (1, "F1")
        window._handle("bookmark_added", (2, "F2", True))
        self.assertEqual(window._bookmark_pending, (1, "F1"))
        self.assertTrue(window._requests.empty())
        window._handle("bookmark_removed", (1, "F1", False))
        self.assertIsNone(window._bookmark_pending)
        self.assertEqual(window._bookmark_notice_var.value, gui_common._BOOKMARK_REMOVE_FAILED)

    def test_each_command_has_its_own_fixed_result_message(self):
        cases = (
            ("bookmark_added", "_BOOKMARK_ADDED", "_BOOKMARK_ADD_FAILED", True),
            ("bookmark_renamed", "_BOOKMARK_RENAMED", "_BOOKMARK_RENAME_FAILED", True),
            ("bookmark_removed", "_BOOKMARK_REMOVED", "_BOOKMARK_REMOVE_FAILED", True),
            ("bookmark_jumped", "_BOOKMARK_JUMPED", "_BOOKMARK_JUMP_FAILED", False),
        )
        for kind, done, failed, refreshes in cases:
            window = _highlights_panel()
            window._bookmark_pending = (1, "F1")
            window._handle(kind, (1, "F1", False))
            self.assertEqual(window._bookmark_notice_var.value, getattr(gui_common, failed))
            window = _highlights_panel()
            window._bookmark_pending = (1, "F1")
            window._handle(kind, (1, "F1", True))
            self.assertEqual(window._bookmark_notice_var.value, getattr(gui_common, done))
            self.assertEqual(window._requests.qsize(), 1 if refreshes else 0)

    def test_a_late_answer_never_leaks_sensitive_text(self):
        window = _highlights_panel()
        window._bookmark_pending = (1, "F1")
        window._handle("bookmarks", (1, "F1", None))
        window._handle("bookmark_added", (1, "F1", False))
        window._handle("bookmark_renamed", (1, "F1", False))
        window._handle("bookmark_removed", (1, "F1", False))
        window._handle("bookmark_jumped", (1, "F1", False))
        rendered = window._bookmark_notice_var.value
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("http", rendered)
        for name in ("_BOOKMARK_LOAD_FAILED", "_BOOKMARK_ADD_FAILED", "_BOOKMARK_RENAME_FAILED", "_BOOKMARK_REMOVE_FAILED", "_BOOKMARK_JUMP_FAILED"):
            self.assertTrue(getattr(gui_common, name))

    def test_adding_a_note_targets_the_selected_binding_and_strips_it(self):
        dialog = _FakeDialog(answer="  第 3 分钟  ")
        window = _highlights_panel(dialog=dialog)
        window._add_bookmark()
        self.assertEqual(window._requests.get_nowait(), ("bookmark_add", 1, "F1", "第 3 分钟"))
        self.assertEqual(window._bookmark_pending, (1, "F1"))
        self.assertEqual(dialog.calls[0][0], "标记当前时间点")

    def test_a_cancelled_note_never_reaches_the_bridge(self):
        window = _highlights_panel(dialog=_FakeDialog(answer=None))
        window._add_bookmark()
        self.assertTrue(window._requests.empty())
        self.assertIsNone(window._bookmark_pending)

    def test_an_empty_note_is_a_valid_add_and_a_valid_rename(self):
        # The bridge accepts an empty note; the window must not invent a
        # requirement of its own, so a blank answer is sent as an empty string.
        window = _highlights_panel(dialog=_FakeDialog(answer="   "))
        window._add_bookmark()
        self.assertEqual(window._requests.get_nowait(), ("bookmark_add", 1, "F1", ""))
        self.assertEqual(window._bookmark_pending, (1, "F1"))

        window = _highlights_panel()
        window._bookmarks = [dict(BOOKMARK_ENTRY)]
        window._render_bookmarks()
        window._bookmark_tree.selected = ("mark0",)
        window._simpledialog = _FakeDialog(answer="")
        window._rename_bookmark()
        self.assertEqual(
            window._requests.get_nowait(), ("bookmark_rename", 1, "F1", "b1", "")
        )

    def test_a_note_over_the_limit_is_refused_before_the_bridge(self):
        limit = gui_common.MAX_NOTE_CHARS
        self.assertEqual(limit, 200)
        for answer in ("x" * (limit + 1), "中" * (limit + 1)):
            window = _highlights_panel(dialog=_FakeDialog(answer=answer))
            window._add_bookmark()
            self.assertTrue(window._requests.empty())
            self.assertIsNone(window._bookmark_pending)
            self.assertEqual(
                window._bookmark_notice_var.value, gui_common._BOOKMARK_NOTE_TOO_LONG
            )

        window = _highlights_panel(dialog=_FakeDialog(answer="y" * limit))  # exactly the limit
        window._add_bookmark()
        self.assertEqual(window._requests.get_nowait(), ("bookmark_add", 1, "F1", "y" * limit))

        window = _highlights_panel()
        window._bookmarks = [dict(BOOKMARK_ENTRY)]
        window._render_bookmarks()
        window._bookmark_tree.selected = ("mark0",)
        window._simpledialog = _FakeDialog(answer="z" * (limit + 1))
        window._rename_bookmark()
        self.assertTrue(window._requests.empty())
        self.assertEqual(
            window._bookmark_notice_var.value, gui_common._BOOKMARK_NOTE_TOO_LONG
        )

    def test_an_empty_note_row_shows_its_timestamp_as_the_default_name(self):
        window = _highlights_panel()
        window._bookmark_pending = (1, "F1")
        blank = dict(BOOKMARK_ENTRY, id="b2", note="", position_ms=90_000)
        window._handle("bookmarks", (1, "F1", [blank]))
        self.assertEqual(window._bookmark_tree.rows, [("mark0", ("A.mkv", "00:01:30", "00:01:30", '未分类'))])

    def test_a_dialog_error_and_an_inactive_player_never_ask_the_bridge(self):
        window = _highlights_panel(dialog=_FakeDialog(error=RuntimeError("窗口已销毁")))
        window._add_bookmark()
        self.assertTrue(window._requests.empty())

        dialog = _FakeDialog(answer="备注")
        window = _highlights_panel(selected=(2, "F2"), dialog=dialog)  # stopped row
        window._add_bookmark()
        self.assertTrue(window._requests.empty())
        self.assertEqual(dialog.calls, [])

    def test_a_selection_change_during_the_dialog_aborts_the_operation(self):
        for replacement in ((3, "F3"), (2, "F2")):
            window = _highlights_panel()

            def move(window=window, replacement=replacement):
                # ``simpledialog`` re-enters mainloop: a snapshot may retarget.
                window._selected = replacement

            window._simpledialog = _FakeDialog(answer="备注", on_call=move)
            window._add_bookmark()
            self.assertTrue(window._requests.empty())
            self.assertIsNone(window._bookmark_pending)
            self.assertEqual(window._bookmark_notice_var.value, gui_common._BOOKMARK_STALE)

    def test_rename_remove_and_jump_use_the_visible_row(self):
        window = _highlights_panel()
        window._bookmarks = [dict(BOOKMARK_ENTRY)]
        window._render_bookmarks()
        window._bookmark_tree.selected = ("mark0",)

        dialog = _FakeDialog(answer="新备注")
        window._simpledialog = dialog
        window._rename_bookmark()
        self.assertEqual(window._requests.get_nowait(), ("bookmark_rename", 1, "F1", "b1", "新备注"))
        self.assertEqual(dialog.calls[0][3], "第 3 分钟")  # the stored note is the default

        window._bookmark_pending = None  # the worker answered; drop the guard
        window._remove_bookmark()
        self.assertEqual(window._requests.get_nowait(), ("bookmark_remove", 1, "F1", "b1"))

        window._bookmark_pending = None
        window._jump_bookmark()
        self.assertEqual(window._requests.get_nowait(), ("bookmark_jump", 1, "F1", "b1"))

    def test_commands_without_a_selected_bookmark_row_or_a_live_player_are_ignored(self):
        window = _highlights_panel(dialog=_FakeDialog(answer="新备注"))
        window._bookmarks = [dict(BOOKMARK_ENTRY)]
        window._render_bookmarks()
        window._rename_bookmark()
        window._remove_bookmark()
        window._jump_bookmark()
        self.assertTrue(window._requests.empty())

        window = _highlights_panel(selected=(2, "F2"))  # stopped binding
        window._bookmarks = [dict(BOOKMARK_ENTRY)]
        window._render_bookmarks()
        window._bookmark_tree.selected = ("mark0",)
        window._jump_bookmark()
        self.assertTrue(window._requests.empty())
        window._remove_bookmark()  # removing a stored bookmark needs no player
        self.assertEqual(window._requests.get_nowait(), ("bookmark_remove", 2, "F2", "b1"))

    def test_button_gating_follows_selection_and_pending_requests(self):
        window = _highlights_panel(selected=None)
        window._update_actions()
        self.assertFalse(window._bookmark_button.enabled)

        window._selected = (1, "F1")
        window._update_actions()
        self.assertTrue(window._bookmark_button.enabled)

        window._bookmark_pending = (1, "F1")
        window._update_actions()
        self.assertFalse(window._bookmark_button.enabled)
        window._bookmark_pending = None

        window._bookmarks = [dict(BOOKMARK_ENTRY)]
        window._render_bookmarks()
        self.assertTrue(window._bookmark_add_button.enabled)
        self.assertTrue(window._bookmark_refresh_button.enabled)
        self.assertFalse(window._bookmark_rename_button.enabled)
        self.assertFalse(window._bookmark_remove_button.enabled)
        self.assertFalse(window._bookmark_jump_button.enabled)

        window._bookmark_tree.selected = ("mark0",)
        window._on_bookmark_select()
        self.assertTrue(window._bookmark_rename_button.enabled)
        self.assertTrue(window._bookmark_remove_button.enabled)
        self.assertTrue(window._bookmark_jump_button.enabled)

    def test_a_stopped_binding_may_be_listed_but_not_bookmarked_or_jumped(self):
        window = _highlights_panel(selected=(2, "F2"))
        window._bookmarks = [dict(BOOKMARK_ENTRY)]
        window._render_bookmarks()
        window._bookmark_tree.selected = ("mark0",)
        window._on_bookmark_select()
        self.assertFalse(window._bookmark_add_button.enabled)
        self.assertFalse(window._bookmark_jump_button.enabled)
        self.assertTrue(window._bookmark_rename_button.enabled)
        window._open_bookmarks()
        self.assertEqual(window._requests.get_nowait(), ("bookmarks", 2, "F2"))

    def test_a_multi_instance_binding_may_be_listed_but_not_bookmarked_or_jumped(self):
        window = _highlights_panel(
            rows=[{"player_pid": 1, "fid": "F1", "name": "A.mkv", "state": "suspended"}]
        )
        window._update_actions()
        window._bookmarks = [dict(BOOKMARK_ENTRY)]
        window._render_bookmarks()
        window._bookmark_tree.selected = ("mark0",)
        window._on_bookmark_select()
        self.assertFalse(window._bookmark_add_button.enabled)
        self.assertFalse(window._bookmark_jump_button.enabled)
        self.assertFalse(restart_enabled(window._connected, window._playback, window._selected))
        self.assertTrue(window._bookmark_rename_button.enabled)
        self.assertTrue(window._bookmark_remove_button.enabled)
        window._open_bookmarks()
        self.assertEqual(window._requests.get_nowait(), ("bookmarks", 1, "F1"))

    def test_jump_needs_a_row_of_the_attached_list(self):
        window = _highlights_panel()
        window._update_bookmark_actions()
        self.assertFalse(window._bookmark_jump_button.enabled)  # no list yet
        window._open_bookmarks()
        self.assertFalse(window._bookmark_jump_button.enabled)  # list still in flight
        window._handle("bookmarks", (1, "F1", [dict(BOOKMARK_ENTRY)]))
        self.assertFalse(window._bookmark_jump_button.enabled)  # nothing selected yet
        window._bookmark_tree.selected = ("mark0",)
        window._on_bookmark_select()
        self.assertTrue(window._bookmark_jump_button.enabled)
        window._clear_bookmarks()
        self.assertFalse(window._bookmark_jump_button.enabled)

    def test_disconnect_clears_the_list_and_says_why(self):
        window = _highlights_panel()
        window._bookmarks = [dict(BOOKMARK_ENTRY)]
        window._render_bookmarks()
        window._bookmark_pending = (1, "F1")
        window._show_disconnected()
        self.assertEqual(window._bookmarks, [])
        self.assertEqual(window._bookmark_tree.rows, [])
        self.assertEqual(window._bookmark_rows, {})
        self.assertIsNone(window._bookmark_pending)
        self.assertEqual(window._bookmark_notice_var.value, gui_common._DISCONNECTED_NOTE)
        window._open_bookmarks()
        self.assertTrue(window._requests.empty())

    def test_closing_the_window_drops_its_state_and_ignores_late_answers(self):
        window = _highlights_panel()
        window._bookmarks = [dict(BOOKMARK_ENTRY)]
        window._render_bookmarks()
        window._bookmark_pending = (1, "F1")
        window._close_bookmarks()
        self.assertIsNone(window._bookmark_window)
        self.assertEqual(window._bookmarks, [])
        self.assertIsNone(window._bookmark_pending)
        window._handle("bookmarks", (1, "F1", [dict(BOOKMARK_ENTRY)]))
        window._handle("bookmark_added", (1, "F1", True))
        window._handle("bookmark_jumped", (1, "F1", True))
        self.assertEqual(window._bookmarks, [])
        self.assertTrue(window._requests.empty())

    def test_opening_a_live_window_raises_it_and_rereads(self):
        window = _highlights_panel()
        window._bookmark_window.lift()
        window._open_bookmarks()
        self.assertEqual(window._bookmark_window.lifted, 2)
        self.assertEqual(window._requests.get_nowait(), ("bookmarks", 1, "F1"))


def _external_request(request_id: str = "r1", fid: str = "F1", name: str = "外部视频.mkv") -> dict:
    return {"id": request_id, "fid": fid, "name": name}


class ExternalBookmarkRequestTests(unittest.TestCase):
    """The pure helpers behind opening a FID that the playback table may not show."""

    def test_only_a_well_formed_request_is_accepted(self):
        self.assertEqual(
            gui_common.bookmark_request_of(
                {"bookmark_request": {"id": "r1", "fid": "F1", "name": "A.mkv"}}
            ),
            {"id": "r1", "fid": "F1", "name": "A.mkv"},
        )
        # A missing or blank name falls back to the FID, never to some title.
        for name in (None, ""):
            request = {"id": "r1", "fid": "F1", "name": name}
            self.assertEqual(
                gui_common.bookmark_request_of({"bookmark_request": request}),
                {"id": "r1", "fid": "F1", "name": "F1"},
            )
        for bad in (
            None,
            {},
            {"bookmark_request": None},
            {"bookmark_request": "F1"},
            {"bookmark_request": {"fid": "F1"}},
            {"bookmark_request": {"id": "", "fid": "F1"}},
            {"bookmark_request": {"id": 1, "fid": "F1"}},
            {"bookmark_request": {"id": "r1"}},
            {"bookmark_request": {"id": "r1", "fid": ""}},
            {"bookmark_request": {"id": "r1", "fid": 7}},
        ):
            self.assertIsNone(gui_common.bookmark_request_of(bad))

    def test_the_action_target_needs_one_operable_binding(self):
        def rows(*states, pid=1, fid="F1"):
            return [
                {"player_pid": pid + i, "fid": fid, "name": "A.mkv", "state": state}
                for i, state in enumerate(states)
            ]

        self.assertEqual(gui_common.external_action_target(rows("playing"), "F1"), (1, "F1"))
        self.assertEqual(gui_common.external_action_target(rows("paused"), "F1"), (1, "F1"))
        # A binding that is not actually playing is no action target at all.
        for state in ("waiting", "unknown", "stopped", "suspended", "mismatch", "missing"):
            self.assertIsNone(gui_common.external_action_target(rows(state), "F1"))
        # Only the exact FID counts and the binding must carry a real pid.
        self.assertIsNone(gui_common.external_action_target(rows("playing"), "F2"))
        self.assertIsNone(gui_common.external_action_target(rows("playing"), ""))
        self.assertIsNone(
            gui_common.external_action_target(
                [{"player_pid": 0, "fid": "F1", "state": "playing"}], "F1"
            )
        )
        self.assertIsNone(
            gui_common.external_action_target(
                [{"player_pid": "1", "fid": "F1", "state": "playing"}], "F1"
            )
        )

    def test_multiple_matching_instances_are_never_guessed_apart(self):
        two = [
            {"player_pid": 1, "fid": "F1", "name": "A.mkv", "state": "playing"},
            {"player_pid": 3, "fid": "F1", "name": "A.mkv", "state": "playing"},
        ]
        self.assertIsNone(gui_common.external_action_target(two, "F1"))
        # One operable plus a not-operable duplicate is still exactly one candidate.
        mixed = two[:1] + [
            {"player_pid": 3, "fid": "F1", "name": "A.mkv", "state": "waiting"}
        ]
        self.assertEqual(gui_common.external_action_target(mixed, "F1"), (1, "F1"))

    def test_a_suspended_sibling_blocks_an_operable_instance(self):
        # Two same-FID instances are really active (one playing, one suspended by
        # the same-FID conflict): the server's _suspended(fid) guard refuses
        # add/jump with 409 media_changed, so no order may enable the action.
        playing = {"player_pid": 1, "fid": "F1", "name": "A.mkv", "state": "playing"}
        suspended = {"player_pid": 2, "fid": "F1", "name": "A.mkv", "state": "suspended"}
        self.assertIsNone(gui_common.external_action_target([playing, suspended], "F1"))
        self.assertIsNone(gui_common.external_action_target([suspended, playing], "F1"))
        self.assertIsNone(
            gui_common.external_action_target(
                [playing, suspended, {"player_pid": 3, "fid": "F1", "state": "paused"}],
                "F1",
            )
        )
        # A never-streamed waiting sibling is not a live conflict: the single
        # operable binding still acts, in either order.
        waiting = {"player_pid": 3, "fid": "F1", "name": "A.mkv", "state": "waiting"}
        self.assertEqual(
            gui_common.external_action_target([playing, waiting], "F1"), (1, "F1")
        )
        self.assertEqual(
            gui_common.external_action_target([waiting, playing], "F1"), (1, "F1")
        )
        # A suspended row for another FID, or one without a real pid, never blocks.
        self.assertEqual(
            gui_common.external_action_target(
                [playing, {"player_pid": 4, "fid": "F2", "state": "suspended"}], "F1"
            ),
            (1, "F1"),
        )
        self.assertEqual(
            gui_common.external_action_target(
                [playing, {"player_pid": 0, "fid": "F1", "state": "suspended"}], "F1"
            ),
            (1, "F1"),
        )


class ExternalBookmarkWindowTests(unittest.TestCase):
    """The manager bound to a request FID rather than to the playback selection."""

    def _snapshot(self, window, request_id="r1"):
        return {
            "account": {},
            "transfer": {"totals": {}, "plays": []},
            "errors": {"count": 0, "events": []},
            "playback": list(window._playback),
            "bookmark_request": _external_request(request_id),
        }

    def test_an_external_request_opens_the_manager_without_any_playback(self):
        window = _highlights_panel(rows=[], selected=None)
        window._apply_snapshot(self._snapshot(window))
        self.assertEqual(window._external, _external_request())
        self.assertEqual(window._requests.get_nowait(), ("bookmarks", 0, "F1"))
        self.assertIn("外部视频.mkv", window._bookmark_header_var.value)
        self.assertIn(gui_common._BOOKMARK_EXTERNAL_REASON, window._bookmark_header_var.value)
        self.assertFalse(window._bookmark_add_button.enabled)
        self.assertFalse(window._bookmark_jump_button.enabled)
        self.assertTrue(window._requests.empty())

    def test_each_request_id_runs_once_and_a_new_id_runs_again(self):
        window = _highlights_panel()
        window._apply_snapshot(self._snapshot(window, "r1"))
        self.assertEqual(window._requests.get_nowait(), ("bookmarks", 0, "F1"))
        # The very same request surviving later polls must not re-read the list.
        window._apply_snapshot(self._snapshot(window, "r1"))
        window._apply_snapshot(self._snapshot(window, "r1"))
        self.assertTrue(window._requests.empty())
        # A different id -- even for the same FID -- retargets and reads again.
        window._apply_snapshot(self._snapshot(window, "r2"))
        self.assertEqual(window._requests.get_nowait(), ("bookmarks", 0, "F1"))
        self.assertTrue(window._requests.empty())

    def test_a_queued_reselect_after_a_refresh_keeps_the_external_target(self):
        window = _highlights_panel(selected=(1, "F1"))
        window._external = _external_request()
        window._bookmarks = [dict(BOOKMARK_ENTRY)]
        window._render_bookmarks()
        # A rebuild restores the same row and queues <<TreeviewSelect>>, which Tk
        # delivers *after* ``_refresh_playback`` has reset its rebuild flag.
        window._refresh_playback()
        self.assertEqual(window._playback_tree.selected, ("row0",))
        self.assertFalse(window._rebuilding)
        window._on_select()  # the queued event, delivered too late for the flag
        self.assertEqual(window._external, _external_request())
        self.assertEqual(window._bookmarks, [dict(BOOKMARK_ENTRY)])
        self.assertTrue(window._requests.empty())

    def test_a_periodic_snapshot_keeps_the_external_target_across_queued_events(self):
        window = _highlights_panel(selected=(1, "F1"))
        window._apply_snapshot(self._snapshot(window))
        self.assertEqual(window._requests.get_nowait(), ("bookmarks", 0, "F1"))
        window._handle("bookmarks", (0, "F1", [dict(BOOKMARK_ENTRY)]))
        # A later poll with the same request: no re-read, and the rebuild's
        # queued selection event must not drop the external target or its list.
        window._apply_snapshot(self._snapshot(window))
        window._on_select()
        self.assertEqual(window._external, _external_request())
        self.assertEqual(window._bookmarks, [dict(BOOKMARK_ENTRY)])
        self.assertTrue(window._requests.empty())

    def test_an_explicit_different_row_leaves_external_mode(self):
        window = _highlights_panel(selected=(1, "F1"))
        window._external = _external_request()
        window._rows = {"row0": (1, "F1"), "row1": (2, "F2")}
        window._playback_tree.selected = ("row1",)
        window._on_select()
        self.assertIsNone(window._external)
        self.assertEqual(window._selected, (2, "F2"))
        self.assertEqual(window._requests.get_nowait(), ("bookmarks", 2, "F2"))

    def test_external_add_and_jump_use_the_one_real_pid_never_the_sentinel(self):
        window = _highlights_panel()
        window._external = _external_request()
        window._bookmark_pending = (0, "F1")
        window._handle("bookmarks", (0, "F1", [dict(BOOKMARK_ENTRY)]))
        window._bookmark_tree.selected = ("mark0",)
        window._on_bookmark_select()
        self.assertTrue(window._bookmark_add_button.enabled)
        self.assertTrue(window._bookmark_jump_button.enabled)
        window._simpledialog = _FakeDialog(answer="第 3 分钟")
        window._add_bookmark()
        self.assertEqual(window._requests.get_nowait(), ("bookmark_add", 1, "F1", "第 3 分钟"))
        window._bookmark_pending = None
        window._jump_bookmark()
        self.assertEqual(window._requests.get_nowait(), ("bookmark_jump", 1, "F1", "b1"))

    def test_external_loading_or_multi_instance_disables_add_and_jump(self):
        cases = (
            [
                {"player_pid": 1, "fid": "F1", "name": "A.mkv", "state": "playing"},
                {"player_pid": 3, "fid": "F1", "name": "A.mkv", "state": "playing"},
            ],
            [{"player_pid": 1, "fid": "F1", "name": "A.mkv", "state": "waiting"}],
        )
        for rows in cases:
            window = _highlights_panel(rows=rows, selected=None)
            window._external = _external_request()
            window._bookmark_pending = (0, "F1")
            window._handle("bookmarks", (0, "F1", [dict(BOOKMARK_ENTRY)]))
            window._bookmark_tree.selected = ("mark0",)
            window._on_bookmark_select()
            self.assertFalse(window._bookmark_add_button.enabled)
            self.assertFalse(window._bookmark_jump_button.enabled)
            self.assertTrue(window._bookmark_rename_button.enabled)
            self.assertTrue(window._bookmark_remove_button.enabled)
            self.assertIn(gui_common._BOOKMARK_EXTERNAL_REASON, window._bookmark_header_var.value)
            window._add_bookmark()
            window._jump_bookmark()
            self.assertTrue(window._requests.empty())  # no pid 0 ever reaches the bridge

    def test_external_mark_can_open_a_video_that_is_not_playing(self):
        window = _highlights_panel(rows=[], selected=None)
        window._external = _external_request()
        window._bookmark_pending = (0, "F1")
        window._handle("bookmarks", (0, "F1", [dict(BOOKMARK_ENTRY)]))
        window._bookmark_tree.selected = ("mark0",)
        window._on_bookmark_select()
        self.assertTrue(window._bookmark_jump_button.enabled)
        window._jump_bookmark()
        self.assertEqual(window._requests.get_nowait(), ("bookmark_play", 0, "F1", "b1"))

    def test_global_manager_lists_multiple_videos_and_edits_notes_without_playback(self):
        window = _highlights_panel(rows=[], selected=None)
        request = {"id": "all1", "scope": "all"}
        window._consume_bookmark_request({"bookmark_request": request})
        self.assertEqual(window._requests.get_nowait(), ("bookmarks", 0, ""))
        entries = [dict(BOOKMARK_ENTRY, fid="F1", name="电影.mkv"),
                   dict(BOOKMARK_ENTRY, id="b2", fid="F2", name="剧集.mp4")]
        window._handle("bookmarks", (0, "", entries))
        self.assertEqual([row[1][0] for row in window._bookmark_tree.rows], ['📁 未分类'])
        window._bookmark_tree.selected = ('group0',)
        window._enter_group_or_jump()
        self.assertEqual([row[1][0] for row in window._bookmark_tree.rows], ["电影.mkv", "剧集.mp4"])
        window._bookmark_tree.selected = ("mark1",)
        window._simpledialog = _FakeDialog(answer="高光对白")
        window._rename_bookmark()
        self.assertEqual(window._requests.get_nowait(), ("bookmark_rename", 0, "", "b2", "高光对白"))

    def test_external_offline_rename_and_remove_use_the_sentinel(self):
        window = _highlights_panel(rows=[], selected=None)
        window._external = _external_request()
        window._bookmark_pending = (0, "F1")
        window._handle("bookmarks", (0, "F1", [dict(BOOKMARK_ENTRY)]))
        window._bookmark_tree.selected = ("mark0",)
        window._on_bookmark_select()
        window._simpledialog = _FakeDialog(answer="新备注")
        window._rename_bookmark()
        self.assertEqual(
            window._requests.get_nowait(), ("bookmark_rename", 0, "F1", "b1", "新备注")
        )
        window._bookmark_pending = None
        window._remove_bookmark()
        self.assertEqual(window._requests.get_nowait(), ("bookmark_remove", 0, "F1", "b1"))

    def test_late_answers_for_another_binding_are_dropped(self):
        window = _highlights_panel()
        window._external = _external_request()
        window._bookmark_pending = (0, "F1")
        window._handle("bookmarks", (1, "F1", [dict(BOOKMARK_ENTRY)]))
        self.assertEqual(window._bookmarks, [])
        self.assertEqual(window._bookmark_pending, (0, "F1"))
        window._handle("bookmark_added", (1, "F1", True))
        self.assertEqual(window._bookmark_pending, (0, "F1"))
        window._handle("bookmarks", (0, "F1", [dict(BOOKMARK_ENTRY)]))
        self.assertIsNone(window._bookmark_pending)
        self.assertIn("精彩标记：1 条", window._bookmark_header_var.value)

    def test_closing_drops_external_and_the_same_id_never_reopens(self):
        window = _highlights_panel()
        window._apply_snapshot(self._snapshot(window, "r1"))
        window._requests.get_nowait()
        window._close_bookmarks()
        self.assertIsNone(window._external)
        self.assertIsNone(window._bookmark_window)
        # The consumed id stays, so the same request never reopens the window.
        window._apply_snapshot(self._snapshot(window, "r1"))
        self.assertTrue(window._requests.empty())
        window._handle("bookmarks", (0, "F1", [dict(BOOKMARK_ENTRY)]))
        self.assertEqual(window._bookmarks, [])


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
