"""Offline tests for browsing a share link and downloading from it without a trace.

Every request goes through ``httpx.MockTransport`` fixtures: the account copy,
the temporary drive folder, its deletion and the recycle record are all scripted
permissions-free, so no account and no network are involved. What the fixtures
record is exactly what the implementation is allowed to have written.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli.downloader.share import ShareSource
from quark_cli.downloader.worker import (
    BrowseShare,
    Download,
    DriveBackend,
    Failed,
    ShareListing,
    Stopped,
    Worker,
)
from quark_cli.errors import RemoteError, UsageError
from quark_cli.transfer import DownloadStopped
from quark_cli.transfer_progress import ProgressEvent

COOKIE = "sid=offline-cookie"
SHARE_URL = "https://pan.quark.cn/s/offline-share"
PASSCODE = "private-passcode"
SECRETS = ("stoken-private", PASSCODE, "private-token-", SHARE_URL)


def _share_row(fid: str, name: str, parent: str = "0", *, directory: bool = False, size: int = 4) -> dict:
    return {
        "fid": fid,
        "file_name": name,
        "file_type": 0 if directory else 1,
        "size": size,
        "pdir_fid": parent,
        "updated_at": 123,
        "share_fid_token": f"private-token-{fid}",
    }


class _Pieces(httpx.SyncByteStream):
    """One response body in pieces, so a stop can land mid-body."""

    def __init__(self, body: bytes, chunk: int) -> None:
        self.body, self.chunk = body, chunk

    def __iter__(self):
        for start in range(0, len(self.body), self.chunk):
            yield self.body[start:start + self.chunk]


class ShareDownloadFixture:
    """A scripted share, account, download host and recycle bin."""

    def __init__(self, payload: bytes = b"share payload") -> None:
        self.share_items: dict[str, list[dict]] = {
            "0": [
                _share_row("share-note", "报告[1].txt"),
                _share_row("share-note-2", "报告1.txt"),
                _share_row("share-dir", "资料", directory=True),
            ],
            "share-dir": [_share_row("share-inside", "内部.txt", "share-dir")],
        }
        self.account: list[dict] = []
        self.recycle: list[dict] = [{"record_id": "keep-1", "fid": "someone-else"}]
        self.requests: list[httpx.Request] = []
        self.saves: list[dict] = []
        self.recycle_removes: list[dict] = []
        self.payload = payload
        for rows in self.share_items.values():
            for row in rows:
                if row["file_type"] == 1:
                    row["size"] = len(payload)
        self.chunk: int | None = None
        self.delete_task_id: str | None = "delete-task"
        self.mkdir_response_fid: str | None = None
        self.mkdir_response_missing = False
        self.hide_created = False
        self.delete_error = False
        self.save_error = False
        self.recycle_remove_error = False
        self.recycle_override: list[dict] | None = None
        self.recycle_total_override: int | None = None
        self.copy_fid: str | None = None
        self.temp_fid: str | None = None
        self._counter = 0
        self._pending_save: dict | None = None

    @property
    def writes(self) -> list[httpx.Request]:
        """The requests that could mutate the account.

        ``share/sharepage/token`` is a ``POST`` that only exchanges the link's
        passcode for a read token: it is an authorization read, not a mutation,
        so a read-only check must not count it as one.
        """
        return [
            request
            for request in self.requests
            if request.method != "GET" and not request.url.path.endswith("/share/sharepage/token")
        ]

    def bodies(self, suffix: str) -> list[dict]:
        return [json.loads(request.read()) for request in self.requests if request.url.path.endswith(suffix)]

    # -- transport --------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "cdn.quark.cn":
            return self._body(request)
        if request.url.host != "drive-pc.quark.cn":
            return httpx.Response(404, request=request)
        path = request.url.path
        if path.endswith("/share/sharepage/token"):
            return self._ok(request, {"stoken": "stoken-private"})
        if path.endswith("/share/sharepage/detail"):
            return self._detail(request)
        if path.endswith("/share/sharepage/save"):
            return self._save(request)
        if path.endswith("/task"):
            return self._task(request)
        if path.endswith("/file/info"):
            return self._info(request)
        if path.endswith("/file/sort"):
            return self._sort(request)
        if path.endswith("/file/download"):
            return self._download(request)
        if path.endswith("/file") and request.method == "POST":
            return self._mkdir(request)
        if path.endswith("/file/delete"):
            return self._delete(request)
        if path.endswith("/file/recycle/list"):
            return self._recycle_list(request)
        if path.endswith("/file/recycle/remove"):
            return self._recycle_remove(request)
        return httpx.Response(404, request=request)

    @staticmethod
    def _ok(request: httpx.Request, data, *, total: int | None = None) -> httpx.Response:
        payload = {"status": 200, "code": 0, "data": data}
        if total is not None:
            payload["metadata"] = {"_total": total}
        return httpx.Response(200, json=payload, request=request)

    # -- endpoints --------------------------------------------------------------

    def _detail(self, request: httpx.Request) -> httpx.Response:
        parent = request.url.params["pdir_fid"]
        # Share file rows always describe the payload the host currently serves,
        # so a test that changes ``payload`` keeps listing, copy and URL sizes in
        # step with each other.
        records = [dict(row) for row in self.share_items.get(parent, [])]
        for row in records:
            if row["file_type"] == 1:
                row["size"] = len(self.payload)
        page, size = int(request.url.params["_page"]), int(request.url.params["_size"])
        offset = (page - 1) * size
        return self._ok(request, {"list": records[offset:offset + size]}, total=len(records))

    def _save(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        if self.save_error:
            return httpx.Response(500, json={"status": 500, "code": 1, "message": "stoken-private"}, request=request)
        self.saves.append(body)
        self._pending_save = body
        return self._ok(request, {"task_id": "save-task"})

    def _task(self, request: httpx.Request) -> httpx.Response:
        task_id = request.url.params["task_id"]
        if task_id != "save-task":
            return self._ok(request, {"status": 2})
        body = self._pending_save
        assert body is not None
        source = next(
            row for row in self.share_items[body["pdir_fid"]] if row["fid"] == body["fid_list"][0]
        )
        self._counter += 1
        self.copy_fid = f"acct-copy-{self._counter}"
        record = {
            "fid": self.copy_fid,
            "file_name": source["file_name"],
            "pdir_fid": body["to_pdir_fid"],
            "file_type": source["file_type"],
            "updated_at": 123,
        }
        if source["file_type"] == 1:
            record["size"] = len(self.payload)
        self.account.append(record)
        if source["file_type"] == 0:
            # Saving a folder copies its whole subtree, exactly like the service.
            self._copy_tree(source["fid"], self.copy_fid)
        return self._ok(
            request,
            {"status": 2, "save_as": {"to_pdir_fid": body["to_pdir_fid"], "save_as_top_fids": [self.copy_fid]}},
        )

    def _copy_tree(self, share_fid: str, parent_fid: str) -> None:
        for row in self.share_items.get(share_fid, []):
            self._counter += 1
            record = {
                "fid": f"acct-copy-{self._counter}",
                "file_name": row["file_name"],
                "pdir_fid": parent_fid,
                "file_type": row["file_type"],
                "updated_at": 123,
            }
            if row["file_type"] == 1:
                record["size"] = len(self.payload)
            self.account.append(record)
            if row["file_type"] == 0:
                self._copy_tree(row["fid"], record["fid"])

    def _info(self, request: httpx.Request) -> httpx.Response:
        fid = request.url.params["fid"]
        record = next((row for row in self.account if str(row["fid"]) == fid), None)
        if record is None:
            return httpx.Response(500, json={"status": 500, "code": 0, "message": "缺失"}, request=request)
        return self._ok(request, dict(record))

    def _sort(self, request: httpx.Request) -> httpx.Response:
        parent = request.url.params["pdir_fid"]
        records = [
            row
            for row in self.account
            if row["pdir_fid"] == parent and not (self.hide_created and str(row["fid"]) == self.temp_fid)
        ]
        page, size = int(request.url.params["_page"]), int(request.url.params["_size"])
        offset = (page - 1) * size
        return self._ok(request, {"list": records[offset:offset + size], "_total": len(records)}, total=len(records))

    def _download(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        fid = body["fids"][0]
        record = next(row for row in self.account if row["fid"] == fid)
        return self._ok(
            request,
            {
                "list": [
                    {
                        "fid": fid,
                        "file_name": record["file_name"],
                        "size": len(self.payload),
                        "download_url": "https://cdn.quark.cn/bytes",
                    }
                ]
            },
        )

    def _mkdir(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        self._counter += 1
        self.temp_fid = f"acct-dir-{self._counter}"
        self.account.append(
            {
                "fid": self.temp_fid,
                "file_name": body["file_name"],
                "pdir_fid": body["pdir_fid"],
                "file_type": 0,
                "updated_at": 1,
            }
        )
        # Like the service, the default response echoes the FID of the folder
        # that was really created; tests can make it mismatch or disappear.
        if self.mkdir_response_missing:
            return self._ok(request, {})
        return self._ok(request, {"fid": self.mkdir_response_fid or self.temp_fid})

    def _delete(self, request: httpx.Request) -> httpx.Response:
        if self.delete_error:
            return httpx.Response(500, json={"status": 500, "code": 1, "message": "stoken-private"}, request=request)
        fid = json.loads(request.read())["filelist"][0]
        doomed = {fid}
        changed = True
        while changed:
            changed = False
            for row in self.account:
                if row["fid"] not in doomed and row.get("pdir_fid") in doomed:
                    doomed.add(row["fid"])
                    changed = True
        self.account = [row for row in self.account if row["fid"] not in doomed]
        self.recycle.append({"record_id": f"rec-{fid}", "fid": fid})
        data = {"finish": True}
        if self.delete_task_id is not None:
            data["task_id"] = self.delete_task_id
        return self._ok(request, data)

    def _recycle_list(self, request: httpx.Request) -> httpx.Response:
        page, size = int(request.url.params["_page"]), int(request.url.params["_size"])
        offset = (page - 1) * size
        if self.recycle_override is not None:
            records = self.recycle_override[offset:offset + size]
            return self._ok(request, {"list": records}, total=self.recycle_total_override)
        records = self.recycle[offset:offset + size]
        return self._ok(request, {"list": records, "_total": len(self.recycle)}, total=len(self.recycle))

    def _recycle_remove(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        self.recycle_removes.append(body)
        if self.recycle_remove_error:
            return httpx.Response(500, json={"status": 500, "code": 1, "message": "stoken-private"}, request=request)
        self.recycle = [row for row in self.recycle if row["record_id"] not in body["record_list"]]
        return self._ok(request, {"finish": True})

    def _body(self, request: httpx.Request) -> httpx.Response:
        total = len(self.payload)
        headers = {"etag": '"stable-version"'}
        range_header = request.headers.get("range")
        if range_header is not None and request.headers.get("if-range") == '"stable-version"':
            start = int(range_header.removeprefix("bytes=").split("-")[0])
            if start >= total:
                return httpx.Response(416, headers={**headers, "content-range": f"bytes */{total}"}, request=request)
            return httpx.Response(
                206,
                stream=_Pieces(self.payload[start:], self.chunk or total),
                headers={**headers, "content-range": f"bytes {start}-{total - 1}/{total}"},
                request=request,
            )
        return httpx.Response(
            200, stream=_Pieces(self.payload, self.chunk or total), headers=headers, request=request
        )


class StubTree:
    """Stands in for the recursive walk, which has its own tests."""

    calls: list[tuple[str, Path, dict]] = []

    def __init__(self, client, *, transfer=None, state_dir=None) -> None:
        self.state_dir = state_dir

    def download_tree(self, fid, output, **options):
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        (output / "内部.txt").write_bytes(b"inside")
        StubTree.calls.append((fid, output, options))
        return {
            "downloaded": True,
            "recursive": True,
            "resumed": False,
            "fid": fid,
            "path": str(output),
            "files": 1,
            "directories": 2,
            "size": 6,
        }


class ShareTestBase(unittest.TestCase):
    def setUp(self):
        self._config = tempfile.TemporaryDirectory()
        self.addCleanup(self._config.cleanup)
        self.config = Path(self._config.name)
        self._environment = patch.dict(os.environ, {"QUARK_CONFIG_DIR": str(self.config)})
        self._environment.start()
        self.addCleanup(self._environment.stop)
        self._output = tempfile.TemporaryDirectory()
        self.addCleanup(self._output.cleanup)
        # Resolve it the way the implementation does: the download target is
        # reported as a long path, so a short temporary path would not compare.
        self.output = Path(self._output.name).resolve()
        for target in ("quark_cli.shares.time.sleep", "quark_cli.downloader.share.time.sleep"):
            self._sleep = patch(target)
            self._sleep.start()
            self.addCleanup(self._sleep.stop)
        StubTree.calls = []

    def backend(self, fixture: ShareDownloadFixture) -> DriveBackend:
        return DriveBackend(lambda: COOKIE, transport=fixture.transport())

    @staticmethod
    def source() -> ShareSource:
        return ShareSource(SHARE_URL, PASSCODE)

    def request(self, fixture: ShareDownloadFixture, *, fid: str, name: str, recursive: bool = False,
                share_path: str = "/", overwrite: bool = False) -> Download:
        return Download(
            fid=fid,
            target=str(self.output / name),
            recursive=recursive,
            overwrite=overwrite,
            name=name,
            share_source=self.source(),
            share_path=share_path,
        )

    def isolate_output(self, label: str) -> Path:
        """A fresh download directory for one subtest.

        Sibling subtests share ``self.output``, and a case that publishes a file
        before its cleanup fails would otherwise make the next case refuse an
        already existing target.
        """
        self.output = Path(self._output.name).resolve() / label
        self.output.mkdir(parents=True)
        return self.output

    def assert_no_secrets(self, text: str) -> None:
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def assert_staging_removed(self) -> None:
        self.assertEqual(list(self.output.glob(".quark-download-*")), [])
        transfers = self.config / "transfers"
        self.assertEqual(sorted(transfers.rglob("*")) if transfers.exists() else [], [])


class ShareSourceTests(unittest.TestCase):
    def test_the_repr_never_prints_the_link_or_the_passcode(self):
        rendered = repr(ShareSource(SHARE_URL, PASSCODE))
        self.assertEqual(rendered, "ShareSource()")
        self.assertEqual(ShareSource(SHARE_URL).passcode, None)


class ShareBrowsingTests(ShareTestBase):
    def test_browsing_a_share_folder_is_read_only_and_paths_every_row(self):
        fixture = ShareDownloadFixture()
        rows = self.backend(fixture).share_listing(self.source(), "/")
        self.assertEqual(
            [(row.name, row.fid, row.directory, row.path, row.location) for row in rows],
            [
                ("资料", "share-dir", True, "/资料", ()),
                # Directories first, then names casefolded: "报告1.txt" sorts
                # before "报告[1].txt" because "1" precedes "[".
                ("报告1.txt", "share-note-2", False, "/报告1.txt", ()),
                ("报告[1].txt", "share-note", False, "/报告[1].txt", ()),
            ],
        )
        nested = self.backend(fixture).share_listing(self.source(), "/资料")
        self.assertEqual([(row.name, row.path) for row in nested], [("内部.txt", "/资料/内部.txt")])
        self.assertEqual(fixture.writes, [], "browsing a share must never write")

    def test_browsing_a_missing_share_folder_reports_a_usage_error(self):
        fixture = ShareDownloadFixture()
        with self.assertRaises(UsageError):
            self.backend(fixture).share_listing(self.source(), "/没有这个目录")
        self.assertEqual(fixture.writes, [])


class ShareDownloadTests(ShareTestBase):
    def test_a_share_download_copies_only_the_selected_item_and_leaves_nothing_behind(self):
        fixture = ShareDownloadFixture()
        phases: list[str] = []
        target = self.output / "报告[1].txt"
        result = self.backend(fixture).download(
            self.request(fixture, fid="share-note", name="报告[1].txt"), phases.append
        )

        self.assertEqual(target.read_bytes(), fixture.payload)
        self.assertEqual(result["path"], str(target))
        self.assertEqual(result["size"], len(fixture.payload))
        self.assertFalse(result["resumed"], "a share download never resumes a previous part")

        # The one requested FID is selected exactly, by FID, never by a name
        # pattern that could also match the other 报告1.txt in the share.
        save = fixture.saves[0]
        self.assertEqual(save["fid_list"], ["share-note"])
        self.assertEqual(save["fid_token_list"], ["private-token-share-note"])
        self.assertEqual(save["pwd_id"], "offline-share")
        self.assertEqual(save["pdir_fid"], "0")
        mkdir = fixture.bodies("/file")
        self.assertEqual(mkdir[0]["pdir_fid"], "0")
        self.assertTrue(mkdir[0]["file_name"].startswith(".quark-download-"))
        # Only the FID the readback confirmed is used, not the share FID.
        self.assertEqual(save["to_pdir_fid"], fixture.temp_fid)
        self.assertNotIn(save["to_pdir_fid"], ("share-note", "share-note-2", "share-dir"))

        # The temporary copy is gone from the drive and from the recycle bin,
        # and no other record was touched.
        self.assertEqual(fixture.account, [])
        self.assertEqual(fixture.bodies("/file/delete"), [{"action_type": 2, "filelist": [fixture.temp_fid], "exclude_fids": []}])
        self.assertEqual(
            fixture.recycle_removes, [{"select_mode": 2, "record_list": [f"rec-{fixture.temp_fid}"]}]
        )
        self.assertEqual([row["record_id"] for row in fixture.recycle], ["keep-1"])

        self.assert_staging_removed()
        self.assertIn("saving", [event.phase for event in phases])
        self.assertIn("cleaning", [event.phase for event in phases])

    def test_an_older_temporary_folder_is_never_collected_by_this_run(self):
        fixture = ShareDownloadFixture()
        fixture.account.append(
            {"fid": "old-temp", "file_name": ".quark-download-existing", "pdir_fid": "0", "file_type": 0, "updated_at": 1}
        )
        self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None)
        self.assertEqual([row["fid"] for row in fixture.account if row["fid"] == "old-temp"], ["old-temp"])
        self.assertEqual([row["fid"] for row in fixture.recycle if row["fid"] == "old-temp"], [])
        self.assertEqual(fixture.bodies("/file/delete")[0]["filelist"], [fixture.temp_fid])

    def test_a_share_folder_is_downloaded_recursively_and_published_under_its_name(self):
        fixture = ShareDownloadFixture()
        target = self.output / "资料"
        request = self.request(fixture, fid="share-dir", name="资料", recursive=True)
        with patch("quark_cli.downloader.share.RecursiveTransfer", StubTree):
            result = self.backend(fixture).download(request, None)  # noqa: E501
        self.assertEqual((target / "内部.txt").read_bytes(), b"inside")
        self.assertEqual(result["path"], str(target))
        self.assertTrue(result["recursive"])
        fid, staged, options = StubTree.calls[0]
        self.assertEqual(fid, fixture.copy_fid)
        self.assertTrue(options["resume"])
        self.assertFalse(options["overwrite"])
        self.assertTrue(staged.is_relative_to(self.output))
        self.assert_staging_removed()

    def test_a_share_folder_downloads_through_the_real_recursive_transfer(self):
        fixture = ShareDownloadFixture()
        target = self.output / "资料"
        result = self.backend(fixture).download(
            self.request(fixture, fid="share-dir", name="资料", recursive=True), None
        )
        self.assertEqual((target / "内部.txt").read_bytes(), fixture.payload)
        self.assertEqual(result["path"], str(target))
        # The real walk counts the saved root folder itself, plus the one file.
        self.assertEqual((result["recursive"], result["files"], result["directories"]), (True, 1, 1))
        self.assertEqual(fixture.account, [], "the whole temporary copy is cleaned up")
        self.assertEqual([row["record_id"] for row in fixture.recycle], ["keep-1"])
        self.assert_staging_removed()

    def test_the_download_stops_before_any_cloud_write_when_a_stop_was_already_asked(self):
        fixture = ShareDownloadFixture()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(DownloadStopped):
            self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None, stop.is_set)
        self.assertEqual(fixture.writes, [], "a pre-emptive stop must not save anything")
        self.assertFalse((self.output / "报告[1].txt").exists())

    def test_a_stop_mid_download_keeps_nothing_local_and_cleans_the_temporary_folder(self):
        fixture = ShareDownloadFixture(payload=b"0123456789abcdef" * (2 * 1024 * 1024 // 16))
        fixture.chunk = 8192
        stop = threading.Event()

        def on_progress(event: ProgressEvent) -> None:
            if event.transferred:
                stop.set()

        with self.assertRaises(DownloadStopped) as caught:
            self.backend(fixture).download(
                self.request(fixture, fid="share-note", name="报告[1].txt"), on_progress, stop.is_set
            )
        self.assertFalse((self.output / "报告[1].txt").exists())
        self.assert_no_secrets(str(caught.exception))
        self.assertEqual([row["fid"] for row in fixture.account if row["fid"] == fixture.temp_fid], [])
        self.assertEqual([row["fid"] for row in fixture.recycle if row["fid"] == fixture.temp_fid], [])
        self.assert_staging_removed()

    def test_a_stopped_share_download_starts_over_instead_of_resuming_a_dead_fid(self):
        fixture = ShareDownloadFixture(payload=b"0123456789abcdef" * (2 * 1024 * 1024 // 16))
        fixture.chunk = 8192
        stop = threading.Event()

        def on_progress(event: ProgressEvent) -> None:
            if event.transferred:
                stop.set()

        request = self.request(fixture, fid="share-note", name="报告[1].txt")
        backend = self.backend(fixture)
        with self.assertRaises(DownloadStopped):
            backend.download(request, on_progress, stop.is_set)
        first_temp = fixture.temp_fid

        fixture.payload = b"retry payload"
        result = self.backend(fixture).download(request, None)
        self.assertNotEqual(fixture.temp_fid, first_temp, "the retry must create its own temporary folder")
        self.assertFalse(result["resumed"])
        self.assertEqual((self.output / "报告[1].txt").read_bytes(), b"retry payload")
        self.assertEqual(len(fixture.saves), 2)
        self.assert_staging_removed()

    def test_a_failed_payload_download_still_cleans_the_temporary_folder(self):
        fixture = ShareDownloadFixture(payload=b"")
        with patch.object(fixture, "_body", lambda request: httpx.Response(500, request=request)):
            with self.assertRaises(RemoteError) as caught:
                self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None)
        self.assert_no_secrets(str(caught.exception))
        self.assertEqual([row["fid"] for row in fixture.account if row["fid"] == fixture.temp_fid], [])
        self.assertEqual([row["record_id"] for row in fixture.recycle], ["keep-1"])
        self.assert_staging_removed()

    def test_an_existing_target_is_refused_before_anything_is_saved(self):
        fixture = ShareDownloadFixture()
        (self.output / "报告[1].txt").write_bytes(b"mine")
        with self.assertRaises(UsageError):
            self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None)
        self.assertEqual(fixture.writes, [])
        self.assertEqual((self.output / "报告[1].txt").read_bytes(), b"mine")

    def test_a_selection_that_left_the_share_stops_before_any_write(self):
        fixture = ShareDownloadFixture()
        with self.assertRaises(RemoteError):
            self.backend(fixture).download(self.request(fixture, fid="share-vanished", name="报告[1].txt"), None)
        self.assertEqual(fixture.writes, [])

    def test_an_overwrite_request_is_refused_for_a_share(self):
        fixture = ShareDownloadFixture()
        with self.assertRaises(UsageError):
            self.backend(fixture).download(
                self.request(fixture, fid="share-note", name="报告[1].txt", overwrite=True), None
            )
        self.assertEqual(fixture.writes, [])

    def test_an_unconfirmed_temporary_folder_is_named_but_never_deleted(self):
        fixture = ShareDownloadFixture()
        fixture.hide_created = True
        with self.assertRaises(RemoteError) as caught:
            self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None)
        message = str(caught.exception)
        self.assertIn(".quark-download-", message)
        self.assertIn("手动处理", message)
        self.assert_no_secrets(message)
        self.assertEqual(fixture.bodies("/file/delete"), [], "an unconfirmed folder must not be deleted")
        self.assertEqual(fixture.saves, [])

    def test_a_preexisting_folder_with_the_random_name_is_never_adopted_or_deleted(self):
        fixture = ShareDownloadFixture()
        fixed = uuid.UUID(int=0)
        name = f".quark-download-{fixed.hex}"
        fixture.account.append(
            {"fid": "user-folder", "file_name": name, "pdir_fid": "0", "file_type": 0, "updated_at": 1}
        )
        with patch("quark_cli.downloader.share.uuid.uuid4", lambda: fixed):
            with self.assertRaises(RemoteError) as caught:
                self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None)
        message = str(caught.exception)
        self.assertIn(name, message)
        self.assert_no_secrets(message)
        self.assertEqual(fixture.bodies("/file"), [], "no folder may be created while the name is taken")
        self.assertEqual(fixture.bodies("/file/delete"), [], "a folder this run did not create is never deleted")
        self.assertEqual(fixture.saves, [])
        self.assertEqual([row["fid"] for row in fixture.account], ["user-folder"])

    def test_a_mismatched_creation_response_fid_is_never_adopted(self):
        fixture = ShareDownloadFixture()
        fixture.mkdir_response_fid = "share-note"
        with self.assertRaises(RemoteError) as caught:
            self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None)
        message = str(caught.exception)
        self.assertIn(".quark-download-", message)
        self.assertIn("手动处理", message)
        self.assert_no_secrets(message)
        self.assertEqual(fixture.bodies("/file/delete"), [], "the real folder is never deleted under a wrong FID")
        self.assertEqual(fixture.saves, [])
        self.assertIn(fixture.temp_fid, [row["fid"] for row in fixture.account])

    def test_a_creation_response_without_a_fid_is_never_adopted_or_deleted(self):
        fixture = ShareDownloadFixture()
        fixture.mkdir_response_missing = True
        with self.assertRaises(RemoteError) as caught:
            self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None)
        message = str(caught.exception)
        self.assertIn(".quark-download-", message)
        self.assertIn("手动处理", message)
        self.assert_no_secrets(message)
        self.assertEqual(fixture.bodies("/file/delete"), [], "an unprovable folder is never deleted")
        self.assertEqual(fixture.saves, [])
        self.assertIn(fixture.temp_fid, [row["fid"] for row in fixture.account])

    def test_a_stop_asked_for_after_the_share_read_ends_before_the_temporary_folder(self):
        fixture = ShareDownloadFixture()
        reads: list[bool] = []

        def should_stop() -> bool:
            reads.append(True)
            return len(reads) > 1

        with self.assertRaises(DownloadStopped):
            self.backend(fixture).download(
                self.request(fixture, fid="share-note", name="报告[1].txt"), None, should_stop
            )
        self.assertEqual(len(reads), 2, "the stop is re-read once the share has been read")
        self.assertEqual(fixture.bodies("/file"), [], "no folder may be created after that stop")
        self.assertEqual(fixture.bodies("/file/delete"), [])
        self.assertEqual(fixture.saves, [])
        self.assertFalse((self.output / "报告[1].txt").exists())

    def test_a_stop_after_the_temporary_folder_exists_still_cleans_it_up(self):
        fixture = ShareDownloadFixture()
        reads: list[bool] = []

        def should_stop() -> bool:
            reads.append(True)
            return len(reads) > 2

        with self.assertRaises(DownloadStopped):
            self.backend(fixture).download(
                self.request(fixture, fid="share-note", name="报告[1].txt"), None, should_stop
            )
        self.assertEqual(fixture.saves, [], "nothing may be saved once the stop is known")
        self.assertEqual(fixture.bodies("/file/delete")[0]["filelist"], [fixture.temp_fid])
        self.assertEqual([row["fid"] for row in fixture.account], [])
        self.assertEqual([row["record_id"] for row in fixture.recycle], ["keep-1"])
        self.assertFalse((self.output / "报告[1].txt").exists())
        self.assert_staging_removed()

    def test_a_save_failure_still_removes_its_empty_temporary_folder(self):
        fixture = ShareDownloadFixture()
        fixture.save_error = True
        with self.assertRaises(RemoteError) as caught:
            self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None)
        self.assert_no_secrets(str(caught.exception))
        self.assertEqual(fixture.saves, [])
        self.assertEqual(fixture.bodies("/file/delete")[0]["filelist"], [fixture.temp_fid])
        self.assertEqual([row["fid"] for row in fixture.account], [])
        self.assertEqual([row["record_id"] for row in fixture.recycle], ["keep-1"])
        self.assert_staging_removed()

    def test_a_failed_copy_info_read_still_removes_the_saved_item(self):
        fixture = ShareDownloadFixture()
        with patch.object(fixture, "_info", side_effect=lambda request: httpx.Response(500, request=request)):
            with self.assertRaises(RemoteError):
                self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None)
        self.assertEqual(len(fixture.saves), 1)
        self.assertEqual(fixture.bodies("/file/download"), [])
        self.assertEqual(fixture.account, [])
        self.assertEqual([row["record_id"] for row in fixture.recycle], ["keep-1"])
        self.assertFalse((self.output / "报告[1].txt").exists())
        self.assert_staging_removed()

    def test_a_delayed_recycle_record_is_removed_before_success(self):
        fixture = ShareDownloadFixture()
        original_list = fixture._recycle_list
        calls = 0

        def delayed_list(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return fixture._ok(request, {"list": [fixture.recycle[0]], "_total": 1})
            return original_list(request)

        with patch.object(fixture, "_recycle_list", side_effect=delayed_list):
            self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None)
        self.assertGreaterEqual(calls, 3)
        self.assertEqual(fixture.account, [])
        self.assertEqual([row["record_id"] for row in fixture.recycle], ["keep-1"])
        self.assertEqual(fixture.recycle_removes, [{"select_mode": 2, "record_list": [f"rec-{fixture.temp_fid}"]}])

    def test_a_temporary_folder_whose_identity_changed_is_never_deleted(self):
        def rename(row: dict) -> None:
            row["file_name"] = "被改名的目录"

        def move(row: dict) -> None:
            row["pdir_fid"] = "somewhere-else"

        def retarget(row: dict) -> None:
            row["fid"] = "someone-elses-fid"

        for mutate in (rename, move, retarget):
            with self.subTest(mutation=mutate.__name__):
                self.isolate_output(mutate.__name__)
                fixture = ShareDownloadFixture()

                def on_progress(event: ProgressEvent, fixture=fixture, mutate=mutate) -> None:
                    if event.phase == "cleaning":
                        row = next(item for item in fixture.account if item["fid"] == fixture.temp_fid)
                        mutate(row)

                with self.assertRaises(RemoteError) as caught:
                    self.backend(fixture).download(
                        self.request(fixture, fid="share-note", name="报告[1].txt"), on_progress
                    )
                message = str(caught.exception)
                self.assertIn("拒绝删除", message)
                self.assertIn("本地文件已下载完成", message)
                self.assert_no_secrets(message)
                self.assertEqual(fixture.bodies("/file/delete"), [], "a changed folder is never deleted")
                self.assertEqual(fixture.recycle_removes, [], "nothing else is purged")
                # The changed folder and the copy this run saved inside it both
                # survive: only their identity is what the refusal protects.
                self.assertIn(fixture.copy_fid, [row["fid"] for row in fixture.account])
                self.assertEqual(len(fixture.account), 2, "folder and copy survive")
                self.assertEqual([row["record_id"] for row in fixture.recycle], ["keep-1"])

    def test_a_malformed_recycle_listing_never_triggers_a_removal(self):
        cases = {
            "duplicate-id": ([{"record_id": "rec-1", "fid": "acct-dir-1"}, {"record_id": "rec-1", "fid": "acct-dir-1"}], None, "重复记录"),
            "not-a-record": ([{"record_id": "rec-1", "fid": "acct-dir-1"}, "boom"], None, "无效记录"),
            "no-record-id": ([{"fid": "acct-dir-1"}], None, "record_id"),
            "short-of-total": ([{"record_id": "rec-1", "fid": "acct-dir-1"}], 5, "提前结束"),
            "invalid-total": ([{"record_id": "rec-1", "fid": "acct-dir-1"}], "1", "总数无效"),
            "boolean-total": ([{"record_id": "rec-1", "fid": "acct-dir-1"}], True, "总数无效"),
            "missing-fid": ([{"record_id": "rec-1"}], None, "有效 FID"),
        }
        for label, (records, total, reason) in cases.items():
            with self.subTest(case=label):
                self.isolate_output(label)
                fixture = ShareDownloadFixture()
                fixture.recycle_override = records
                fixture.recycle_total_override = total
                with self.assertRaises(RemoteError) as caught:
                    self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None)
                message = str(caught.exception)
                self.assertIn("清理失败", message)
                self.assertIn(reason, message)
                self.assert_no_secrets(message)
                self.assertEqual(fixture.recycle_removes, [], "a malformed recycle listing never removes anything")

    def test_a_cleanup_failure_is_reported_and_never_as_a_success(self):
        fixture = ShareDownloadFixture()
        fixture.recycle_remove_error = True
        with self.assertRaises(RemoteError) as caught:
            self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None)
        message = str(caught.exception)
        self.assertIn("清理失败", message)
        self.assertIn("本地文件已下载完成", message)
        self.assertIn("手动", message)
        self.assert_no_secrets(message)
        self.assertEqual((self.output / "报告[1].txt").read_bytes(), fixture.payload)
        self.assertEqual(
            [row["record_id"] for row in fixture.recycle], ["keep-1", f"rec-{fixture.temp_fid}"]
        )

    def test_an_unreachable_delete_service_is_reported(self):
        fixture = ShareDownloadFixture()
        fixture.delete_error = True
        with self.assertRaises(RemoteError) as caught:
            self.backend(fixture).download(self.request(fixture, fid="share-note", name="报告[1].txt"), None)
        message = str(caught.exception)
        self.assertIn("临时目录删除失败", message)
        self.assertIn("本地文件已下载完成", message)
        self.assert_no_secrets(message)


class WorkerShareTests(ShareTestBase):
    def test_a_stopped_share_reports_cleanup_and_restart_instead_of_a_checkpoint(self):
        fixture = ShareDownloadFixture()
        worker = Worker(self.backend(fixture))
        self.addCleanup(worker.stop)
        worker.request_stop()
        worker.start()
        worker.submit(self.request(fixture, fid="share-note", name="报告[1].txt"))
        event = worker.events.get(timeout=5)
        self.assertIsInstance(event, Stopped)
        self.assertEqual(event.message, "分享下载已停止：临时内容已清理，需重新下载")
        self.assertNotIn("检查点", event.message)
        self.assertEqual(fixture.writes, [])

    def test_browsing_a_share_reports_one_share_listing(self):
        class Backend:
            def __init__(self):
                self.calls: list[tuple[ShareSource, str]] = []

            def share_listing(self, source, path):
                self.calls.append((source, path))
                return []

        backend = Backend()
        worker = Worker(backend)
        self.addCleanup(worker.stop)
        worker.start()
        source = self.source()
        worker.submit(BrowseShare(source, "/资料"))
        event = worker.events.get(timeout=5)
        self.assertIsInstance(event, ShareListing)
        self.assertEqual((event.source, event.path, event.entries), (source, "/资料", ()))
        self.assertEqual(backend.calls, [(source, "/资料")])

    def test_a_share_read_failure_is_one_safe_line(self):
        class Backend:
            def share_listing(self, source, path):
                raise RuntimeError(f"boom {SHARE_URL} {PASSCODE}")

        worker = Worker(Backend())
        self.addCleanup(worker.stop)
        worker.start()
        worker.submit(BrowseShare(self.source()))
        event = worker.events.get(timeout=5)
        self.assertIsInstance(event, Failed)
        self.assertEqual(event.action, "读取分享")
        self.assertEqual(event.message, "读取分享失败：发生未预期的错误")
        self.assert_no_secrets(event.message)

    def test_a_share_download_that_cannot_clean_up_is_a_failure_never_a_finish(self):
        fixture = ShareDownloadFixture()
        fixture.recycle_remove_error = True
        worker = Worker(self.backend(fixture))
        self.addCleanup(worker.stop)
        worker.start()
        worker.submit(self.request(fixture, fid="share-note", name="报告[1].txt"))

        events = []
        while True:
            event = worker.events.get(timeout=5)
            events.append(event)
            if isinstance(event, (Failed,)):
                break
        self.assertEqual(event.action, "下载")
        self.assertIn("清理失败", event.message)
        self.assert_no_secrets(event.message)
        self.assertTrue((self.output / "报告[1].txt").exists())
        self.assertNotIn("stoken", event.message)


if __name__ == "__main__":
    unittest.main()
