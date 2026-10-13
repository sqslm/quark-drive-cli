"""Pause only known upload states; resume proves parts instead of repeating preflight writes."""

import hashlib
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

import httpx

from quark_cli.client import QuarkClient
from quark_cli.transfer import Transfer, UploadStopped
from quark_cli.recursive import RecursiveTransfer
from tests.test_transfer import UploadServer
from tests.test_recursive import TreeFixture, FileTransfer


class UploadStopTests(unittest.TestCase):
    def transfer(self, server, state):
        client = QuarkClient(
            "sid=account-test; __puus=puus-test", transport=httpx.MockTransport(server)
        )
        self.addCleanup(client.close)
        return Transfer(client, state_dir=state)

    def test_initial_stop_sends_no_request(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "test.bin"
            source.write_bytes(b"content")
            server = UploadServer()
            with self.assertRaises(UploadStopped):
                self.transfer(server, Path(folder) / "state").upload(
                    source, resume=True, should_stop=lambda: True
                )
            self.assertEqual(server.calls, [])

    def test_confirmed_part_pause_resumes_same_task_and_skips_part(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "test.bin"
            payload = b"a" * (128 * 1024) + b"b" * (128 * 1024) + b"end"
            source.write_bytes(payload)
            stopped = threading.Event()
            server = UploadServer(page_size=1)
            transfer = self.transfer(server, root / "state")

            def progress(event):
                if event.phase == "part_confirmed":
                    stopped.set()

            with self.assertRaises(UploadStopped):
                transfer.upload(
                    source, resume=True, should_stop=stopped.is_set, progress=progress
                )
            self.assertEqual(sorted(server.parts), [1])
            result = transfer.upload(source, resume=True)
            self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(result["skipped_parts"], 1)
            self.assertEqual(sum(call[0] == "pre" for call in server.calls), 1)

    def test_pause_during_part_retains_resume_state_and_does_not_finish(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "test.bin"
            source.write_bytes(b"a" * 200000)
            server = UploadServer()
            stopped = threading.Event()

            transfer = self.transfer(server, root / "state")
            storage = transfer._storage_request

            def during_part(method, url, **options):
                if method == "PUT":
                    body = options["content"]

                    def chunks():
                        for chunk in body:
                            yield chunk
                            stopped.set()

                    options["content"] = chunks()
                return storage(method, url, **options)

            with (
                patch.object(transfer, "_storage_request", side_effect=during_part),
                patch("quark_cli.transfer.STREAM_CHUNK_SIZE", 32768),
            ):
                with self.assertRaises(UploadStopped):
                    transfer.upload(source, resume=True, should_stop=stopped.is_set)
            self.assertFalse(server.visible)
            stopped.clear()
            result = transfer.upload(source, resume=True, should_stop=stopped.is_set)
            self.assertEqual(result["file_size"], 200000)
            self.assertEqual(sum(call[0] == "pre" for call in server.calls), 1)

    def test_tree_pause_keeps_confirmed_mapping_and_continues_remaining_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "目录"
            source.mkdir()
            (source / "a.txt").write_bytes(b"aaa")
            (source / "b.txt").write_bytes(b"bbb")
            fixture = TreeFixture()
            client = QuarkClient("sid=account-test", transport=fixture.transport())
            self.addCleanup(client.close)
            stopped = threading.Event()
            files = FileTransfer(fixture)
            files.after_upload = lambda _record: stopped.set()
            transfer = RecursiveTransfer(
                client, transfer=files, state_dir=root / "state"
            )
            with self.assertRaises(UploadStopped):
                transfer.upload_tree(source, resume=True, should_stop=stopped.is_set)
            self.assertEqual(len(files.uploads), 1)
            stopped.clear()
            files.after_upload = lambda _record: None
            result = transfer.upload_tree(
                source, resume=True, should_stop=stopped.is_set
            )
            self.assertEqual(result["files"], 2)
            self.assertEqual(len(files.uploads), 2)
