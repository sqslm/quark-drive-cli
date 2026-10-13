"""Durable queue boundaries, credential isolation and share reauthorization."""

import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from quark_cli.desktop.service import DesktopService
from quark_cli.downloader.preferences import Preferences, save_preferences
from quark_cli.transfer import DownloadStopped
from tests.test_desktop_service import Backend


class QueueBackend(Backend):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.active = 0
        self.maximum = 0

    def download(self, request, progress, stopped):
        self.requests.append(request)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.entered.set()
        self.release.wait(3)
        self.active -= 1
        if stopped():
            raise DownloadStopped("暂停")
        return {"size": 3}

    @contextmanager
    def _client(self):
        class Client:
            def auth_status(self):
                return {"authenticated": True}

        yield Client()


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.messages = []
        self.backend = QueueBackend()
        self.service = DesktopService(
            self.messages.append,
            backend=self.backend,
            directory=Path(self.tmp.name) / "state",
        )
        self.service.saved_cookie = "sid=queue-credential"
        self.addCleanup(self.service.shutdown)
        self.addCleanup(self.backend.release.set)
        save_preferences(self.service.preferences_path, Preferences(self.tmp.name))

    def queue(self, fid, **options):
        return self.service.dispatch(
            "d",
            "download",
            {
                "entries": [{"fid": fid, "name": fid + ".txt", "directory": False}],
                **options,
            },
            None,
        )

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.service.busy and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertFalse(self.service.busy)

    def test_append_during_download_remains_sequential_and_durable(self):
        self.queue("a")
        self.assertTrue(self.backend.entered.wait(1))
        self.queue("b")
        self.assertEqual(self.service.pending_count, 2)
        self.backend.release.set()
        self.wait_idle()
        self.assertEqual([request.fid for request in self.backend.requests], ["a", "b"])
        self.assertEqual(self.backend.maximum, 1)
        document = json.loads(self.service.task_store.path.read_text("utf-8"))
        self.assertEqual([row["state"] for row in document["tasks"]], ["done", "done"])
        self.assertNotIn("sid=queue-credential", str(document))

    def test_cancel_waiting_item_never_calls_backend(self):
        self.queue("a")
        self.assertTrue(self.backend.entered.wait(1))
        tasks = self.queue("b")
        self.service.dispatch("cancel", "tasks.cancel", {"ids": [tasks[1]["id"]]}, None)
        self.backend.release.set()
        self.wait_idle()
        self.assertEqual([request.fid for request in self.backend.requests], ["a"])
        self.assertEqual(self.service.tasks[1]["state"], "cancelled")

    def test_pause_and_restart_do_not_autorun_and_other_cookie_cannot_retry(self):
        self.queue("a")
        self.assertTrue(self.backend.entered.wait(1))
        self.service.stop.set()
        self.backend.release.set()
        self.wait_idle()
        other = DesktopService(
            self.messages.append,
            backend=QueueBackend(),
            directory=self.service.directory,
        )
        self.addCleanup(other.shutdown)
        other.saved_cookie = "sid=other-credential"
        tasks = other.dispatch("tasks", "tasks", {}, None)
        self.assertEqual(tasks[0]["state"], "stopped")
        self.assertFalse(tasks[0]["valid"])
        with self.assertRaises(Exception):
            other.dispatch("retry", "tasks.retry", {}, None)
        self.assertEqual(other.backend.requests, [])

    def test_share_record_has_no_code_and_restart_requires_authorization(self):
        source = self.service.dispatch(
            "share",
            "share.open",
            {"text": "https://pan.quark.cn/s/abcdef?pwd=1234"},
            None,
        )
        self.queue("share-file", handle=source["handle"])
        self.assertTrue(self.backend.entered.wait(1))
        self.service.stop.set()
        self.backend.release.set()
        self.wait_idle()
        document = self.service.task_store.path.read_text("utf-8")
        self.assertNotIn("1234", document)
        other = DesktopService(
            self.messages.append,
            backend=QueueBackend(),
            directory=self.service.directory,
        )
        self.addCleanup(other.shutdown)
        other.saved_cookie = "sid=queue-credential"
        task = other.dispatch("tasks", "tasks", {}, None)[0]
        self.assertTrue(task["needs_authorization"])
        self.assertFalse(task["valid"])

    def test_running_record_restores_as_paused(
        self,
    ):
        self.queue("a")
        self.assertTrue(self.backend.entered.wait(1))
        other = DesktopService(
            self.messages.append,
            backend=QueueBackend(),
            directory=self.service.directory,
        )
        self.addCleanup(other.shutdown)
        self.assertEqual(other.tasks[0]["state"], "stopped")
        self.assertEqual(other.backend.requests, [])

    def test_corrupt_record_keeps_session_queue_without_overwriting_original(self):
        self.service.directory.mkdir(parents=True, exist_ok=True)
        path = self.service.task_store.path
        path.write_text("{corrupt", encoding="utf-8")
        other = DesktopService(
            self.messages.append, backend=Backend(), directory=self.service.directory
        )
        self.addCleanup(other.shutdown)
        other.saved_cookie = "sid=queue-credential"
        other.dispatch(
            "download",
            "download",
            {"entries": [{"fid": "x", "name": "x.txt", "directory": False}]},
            None,
        )
        other.transfers.shutdown(wait=True)
        self.assertEqual(path.read_text("utf-8"), "{corrupt")
        self.assertEqual(other.tasks[0]["state"], "done")
        self.assertTrue(other.task_store.warning)
