import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli.client import QUARK_DESKTOP_USER_AGENT, QuarkClient
from quark_cli.errors import AuthenticationError, LocalIOError, RemoteError, UsageError
from quark_cli.transfer import Transfer, UPLOAD_READBACK_ATTEMPTS, UPLOAD_READBACK_INTERVAL, _SHA1State
from quark_cli.transfer_state import DownloadLedger, TransferLedger, UploadLedger
from quark_cli.transfer_progress import RateLimiter
from quark_cli.files import Files
from quark_cli.paths import RemotePaths, Scope


def ok(data):
    return {"status": 200, "code": 0, "data": data}


class ProcessStopped(BaseException):
    """Simulate exit without converting it into a transport exception."""


class UploadServer:
    def __init__(self, *, flash=None, failure=None, page_size=1000, uppercase=False, pre_fid=False):
        self.flash, self.failure, self.page_size, self.uppercase, self.pre_fid = flash, failure, page_size, uppercase, pre_fid
        self.calls, self.parts, self.visible = [], {}, False
        self.listed_bucket = "ul-zb"
        self.item = {"fid": "result", "pdir_fid": "0", "file_name": "file.bin", "file_type": 1, "size": 0, "updated_at": 7}

    def __call__(self, request):
        body = json.loads(request.read()) if request.url.host == "drive-pc.quark.cn" and request.method == "POST" else None
        stage = request.url.path.rsplit("/", 1)[-1] if request.url.host == "drive-pc.quark.cn" else request.method
        self.calls.append((stage, request.method, dict(request.url.params), body, dict(request.headers)))
        if request.url.host == "drive-pc.quark.cn":
            if stage == "info":
                fid = request.url.params["fid"]
                item = self.item if fid == "result" else {"fid": fid, "file_type": 0, "file_name": fid, "pdir_fid": "0"}
                return httpx.Response(200, json=ok(item), request=request)
            if stage == "sort":
                rows = [self.item] if self.visible else []
                return httpx.Response(200, json=ok({"list": rows, "_total": len(rows)}), request=request)
            if stage == "pre":
                self.item.update(file_name=body["file_name"], size=body["size"], pdir_fid=body["pdir_fid"])
                if self.flash == "pre":
                    self.visible = True
                    return httpx.Response(200, json=ok({"finish": True, "fid": "result", "task_id": "original-task"}), request=request)
                data = {"task_id": "original-task", "upload_id": "original-upload", "obj_key": "test/key", "bucket": "ul-zb",
                        "auth_info": "private-test-auth", "callback": {"callbackUrl": "https://drive-pc.quark.cn/callback"}, "part_size": 128 * 1024}
                if self.pre_fid:
                    data["fid"] = "result"
            elif stage == "hash":
                data = {"finish": self.flash in ("hash", "hash-no-fid")}
                if data["finish"]:
                    self.visible = True
                    if self.flash == "hash":
                        data["fid"] = "result"
            elif stage == "auth":
                data = {"auth_key": "private-test-key"}
            elif stage == "finish":
                self.visible = True
                data = {"finish": True, "fid": "result"}
            else:
                return httpx.Response(404, request=request)
            if self.failure == stage:
                raise httpx.ReadError("response lost", request=request)
            return httpx.Response(200, json=ok(data), request=request)
        if request.method == "PUT":
            number = int(request.url.params["partNumber"])
            self.parts[number] = request.read()
            if self.failure == "PUT":
                raise httpx.ReadError("response lost", request=request)
            etag = hashlib.md5(self.parts[number]).hexdigest()
            return httpx.Response(200, headers={"etag": '"' + (etag.upper() if self.uppercase else etag) + '"'}, request=request)
        if request.method == "GET":
            marker = int(request.url.params["part-number-marker"])
            numbers = [number for number in sorted(self.parts) if number > marker]
            batch = numbers[:self.page_size]
            truncated = len(numbers) > len(batch)
            xml = f'<ListPartsResult xmlns="urn:oss"><Bucket>{self.listed_bucket}</Bucket><Key>test/key</Key><UploadId>original-upload</UploadId>'
            xml += f'<IsTruncated>{str(truncated).lower()}</IsTruncated>'
            if truncated:
                xml += f'<NextPartNumberMarker>{batch[-1]}</NextPartNumberMarker>'
            for number in batch:
                content = self.parts[number]
                etag = hashlib.md5(content).hexdigest()
                xml += f'<Part><PartNumber>{number}</PartNumber><Size>{len(content)}</Size><ETag>"{etag.upper() if self.uppercase else etag}"</ETag></Part>'
            return httpx.Response(200, content=(xml + '</ListPartsResult>').encode(), request=request)
        if request.method == "POST":
            xml = request.read().decode()
            if self.uppercase and any('"' + hashlib.md5(payload).hexdigest().upper() + '"' not in xml for payload in self.parts.values()):
                return httpx.Response(400, content=b"<Error><Code>InvalidPart</Code></Error>", request=request)
            if self.failure == "POST":
                raise httpx.ReadError("response lost", request=request)
            return httpx.Response(200, content=b"<CompleteMultipartUploadResult/>", request=request)
        return httpx.Response(404, request=request)


class DownloadServer:
    def __init__(self, payload, *, etag='"stable"'):
        self.payload, self.etag, self.failure = payload, etag, None
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        if request.url.host == "drive-pc.quark.cn":
            if request.url.path.endswith("/info"):
                return httpx.Response(200, json=ok({"fid": "download-id", "file_name": "remote.bin", "pdir_fid": "0", "size": len(self.payload), "file_type": 1}), request=request)
            return httpx.Response(200, json=ok({"list": [{"fid": "download-id", "size": len(self.payload), "download_url": "https://cdn.quark.cn/bytes"}]}), request=request)
        headers = {"etag": self.etag} if self.etag is not None else {}
        range_header = request.headers.get("range")
        if range_header and self.failure != "ignore-range":
            offset = int(range_header.split("=", 1)[1].split("-", 1)[0])
            if self.failure == "416" or offset >= len(self.payload):
                headers["content-range"] = f"bytes */{len(self.payload)}"
                return httpx.Response(416, headers=headers, request=request)
            end = 0 if range_header == "bytes=0-0" else len(self.payload) - 1
            headers["content-range"] = f"bytes {offset}-{end}/{len(self.payload)}"
            if self.failure == "wrong-start":
                headers["content-range"] = f"bytes {offset + 1}-{end}/{len(self.payload)}"
            if self.failure == "wrong-total":
                headers["content-range"] = f"bytes {offset}-{end}/{len(self.payload) + 1}"
            if self.failure == "encoding":
                headers["content-encoding"] = "gzip"
            if self.failure == "etag":
                headers["etag"] = '"changed"'
            content = self.payload[offset:end + 1]
            if self.failure == "truncated":
                headers["content-length"] = str(len(content))
                content = content[:-1]
            return httpx.Response(206, content=content, headers=headers, request=request)
        return httpx.Response(200, content=self.payload, headers=headers, request=request)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.config = tempfile.TemporaryDirectory()
        self.addCleanup(self.config.cleanup)
        self.environment = patch.dict(os.environ, {"QUARK_CONFIG_DIR": self.config.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.upload_case = 0

    def _source(self, root, content=b"original upload bytes"):
        source = root / "file.bin"
        source.write_bytes(content)
        return source

    def _transfer(self, server, state):
        client = QuarkClient("sid=account-test; __puus=puus-test", transport=httpx.MockTransport(server))
        self.addCleanup(client.close)
        return Transfer(client, state_dir=state)

    def _virtual_limiter(self):
        clock = [0.0]
        return RateLimiter(40, clock=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def _interrupt_download(self, server, output, state, phase="streaming"):
        original = TransferLedger.persist

        def checkpoint(ledger, data):
            original(ledger, data)
            if data["direction"] == "download" and data["phase"] == phase and (phase != "streaming" or data["offset"] == 4):
                raise ProcessStopped()

        with patch.object(TransferLedger, "persist", checkpoint):
            with self.assertRaises(ProcessStopped):
                self._transfer(server, state).download("download-id", output, resume=True, limiter=self._virtual_limiter())

    def test_upload_empty_is_rejected_before_api_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = UploadServer()
            with self.assertRaises(UsageError):
                self._transfer(server, root / "state").upload(self._source(root, b""))
            self.assertEqual(server.calls, [])

    def test_flash_pre_and_hash_do_not_issue_finish_or_storage_writes(self):
        for stage in ("pre", "hash"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root, server = Path(directory), UploadServer(flash=stage)
                result = self._transfer(server, root / "state").upload(self._source(root), resume=True)
                self.assertEqual(result["fid"], "result")
                self.assertEqual(result["parts"], 0)
                self.assertEqual(result["task_id"], "original-task")
                self.assertFalse(any(call[0] in ("PUT", "POST", "finish", "auth") for call in server.calls))
                data = json.loads(next((root / "state").glob("upload-*.json")).read_text())
                self.assertEqual(data["phase"], "completed")
                self.assertEqual(data["upload"], {})
                self.assertNotIn("private-test-auth", json.dumps(data))

    def test_flash_without_fid_remains_unknown_and_never_resends(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer(flash="hash-no-fid")
            source = self._source(root)
            for _ in range(2):
                with self.assertRaisesRegex(RemoteError, "缺少可关联 FID"):
                    self._transfer(server, root / "state").upload(source, resume=True)
            self.assertEqual(sum(call[0] == "hash" for call in server.calls), 1)
            self.assertFalse(any(call[0] in ("PUT", "POST", "finish") for call in server.calls))

    def test_flash_hash_uses_exact_pre_fid_without_extra_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer(flash="hash-no-fid", pre_fid=True)
            result = self._transfer(server, root / "state").upload(self._source(root), resume=True)
            self.assertEqual(result["fid"], "result")
            self.assertFalse(any(c[0] in ("PUT", "POST", "finish") for c in server.calls))

    def test_uppercase_opaque_etag_is_preserved_for_resume_and_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer(uppercase=True)
            source = self._source(root)

            def stop(event):
                if event.phase == "part_confirmed":
                    raise ProcessStopped()

            with self.assertRaises(ProcessStopped):
                self._transfer(server, root / "state").upload(source, resume=True, progress=stop)
            result = self._transfer(server, root / "state").upload(source, resume=True)
            self.assertEqual(result["skipped_parts"], 1)
            data = json.loads(next((root / "state").glob("upload-*.json")).read_text())
            self.assertEqual(data["parts"][0]["etag"], hashlib.md5(source.read_bytes()).hexdigest().upper())

    def test_upload_pre_fid_does_not_bypass_finish_task_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer(pre_fid=True)

            def handler(request):
                if request.url.path.endswith("/finish"):
                    return httpx.Response(200, json=ok({"finish": False, "task_id": "completion-task"}), request=request)
                if request.url.path.endswith("/task"):
                    return httpx.Response(200, json=ok({"status": 3}), request=request)
                return server(request)

            with self.assertRaisesRegex(RemoteError, "失败"):
                self._transfer(handler, root / "state").upload(self._source(root), resume=True)

    def test_upload_http_200_error_merge_cannot_reach_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer()

            def handler(request):
                if request.url.host.endswith(".pds.quark.cn") and request.method == "POST":
                    return httpx.Response(200, content=b"<Error><Code>InvalidPart</Code></Error>", request=request)
                return server(request)

            with self.assertRaises(RemoteError):
                self._transfer(handler, root / "state").upload(self._source(root), resume=True)
            self.assertFalse(any(c[0] == "finish" for c in server.calls))

    def test_uppercase_callback_status_only_accepts_documented_ok(self):
        for status in ("OK", "ok", "Error", 200, True, None):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root, server = Path(directory), UploadServer(pre_fid=True)

                def handler(request):
                    if request.url.host.endswith(".pds.quark.cn") and request.method == "POST":
                        return httpx.Response(200, json={"Status": status}, request=request)
                    return server(request)

                if status == "OK":
                    result = self._transfer(handler, root / "state").upload(self._source(root), resume=True)
                    self.assertEqual(result["fid"], "result")
                else:
                    with self.assertRaises(RemoteError):
                        self._transfer(handler, root / "state").upload(self._source(root), resume=True)
                    self.assertFalse(any(c[0] == "finish" for c in server.calls))

    def test_finish_waiting_resume_reads_linked_task_before_target_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer()
            source = self._source(root)

            def handler(request):
                if request.url.path.endswith("/finish"):
                    server.calls.append(("finish", "POST", {}, {}, {}))
                    server.visible = True
                    return httpx.Response(200, json=ok({"finish": False, "task_id": "completion-task"}), request=request)
                if request.url.path.endswith("/task"):
                    server.calls.append(("task", "GET", {}, {}, {}))
                    self.assertEqual(request.url.params["task_id"], "completion-task")
                    return httpx.Response(200, json=ok({"status": 2, "fid": "result"}), request=request)
                return server(request)

            original = TransferLedger.persist
            def stop(ledger, data):
                original(ledger, data)
                if data["phase"] == "finish_waiting":
                    raise ProcessStopped()

            with patch.object(TransferLedger, "persist", stop):
                with self.assertRaises(ProcessStopped):
                    self._transfer(handler, root / "state").upload(source, resume=True)
            writes = [(c[0], c[1]) for c in server.calls if c[1] != "GET"]
            result = self._transfer(handler, root / "state").upload(source, resume=True)
            self.assertEqual(result["fid"], "result")
            self.assertEqual(sum(c[0] == "task" for c in server.calls), 1)
            self.assertEqual([(c[0], c[1]) for c in server.calls if c[1] != "GET"], writes)

    def test_upload_unknown_post_stages_are_not_resubmitted(self):
        for stage in ("pre", "hash", "POST", "finish"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root, server = Path(directory), UploadServer(failure=stage)
                source = self._source(root)
                with self.assertRaises(RemoteError):
                    self._transfer(server, root / "state").upload(source, resume=True)
                writes = [(c[0], c[1]) for c in server.calls if c[1] != "GET"]
                server.failure = None
                with self.assertRaisesRegex(RemoteError, "未知受理"):
                    self._transfer(server, root / "state").upload(source, resume=True)
                self.assertEqual([(c[0], c[1]) for c in server.calls if c[1] != "GET"], writes)

    def test_upload_resume_same_task_paginated_list_skips_confirmed_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer(page_size=1)
            content = b"a" * (128 * 1024) + b"b" * (128 * 1024) + b"last"
            source = self._source(root, content)
            observed = []

            def stop(event):
                if event.phase == "part_confirmed":
                    data = json.loads(next((root / "state").glob("upload-*.json")).read_text())
                    observed.append(sum(p["confirmed"] for p in data["parts"]))
                    if event.confirmed_parts == 2:
                        raise ProcessStopped()

            with self.assertRaises(ProcessStopped):
                self._transfer(server, root / "state").upload(source, resume=True, progress=stop)
            self.assertEqual(observed, [1, 2])
            split = len(server.calls)
            result = self._transfer(server, root / "state").upload(source, resume=True)
            after = server.calls[split:]
            self.assertEqual(result["skipped_parts"], 2)
            self.assertEqual(result["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual([c[2]["partNumber"] for c in after if c[0] == "PUT"], ["3"])
            self.assertEqual([c[2]["part-number-marker"] for c in after if c[0] == "GET"], ["0", "1"])
            self.assertEqual(sum(c[0] == "pre" for c in server.calls), 1)
            auth = [c[3] for c in after if c[0] == "auth" and c[3]["auth_meta"].startswith("GET\n")]
            self.assertEqual(len(auth), 2)
            for call in auth:
                self.assertEqual(call["task_id"], "original-task")
                self.assertEqual(call["auth_info"], "private-test-auth")
                self.assertTrue(call["auth_meta"].endswith("/ul-zb/test/key?uploadId=original-upload"))
                self.assertNotIn("part-number-marker", call["auth_meta"])
            self.assertEqual(b"".join(server.parts[n] for n in sorted(server.parts)), content)

    def test_upload_lost_put_response_is_adopted_only_after_md5_list_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer(failure="PUT")
            source = self._source(root)
            with self.assertRaises(RemoteError):
                self._transfer(server, root / "state").upload(source, resume=True)
            server.failure = None
            result = self._transfer(server, root / "state").upload(source, resume=True)
            self.assertEqual(result["skipped_parts"], 1)
            self.assertEqual(sum(c[0] == "PUT" for c in server.calls), 1)

    def test_upload_confirmed_part_missing_or_changed_does_not_overwrite(self):
        for change in ("missing", "changed"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root, server = Path(directory), UploadServer()
                source = self._source(root)

                def stop(event):
                    if event.phase == "part_confirmed":
                        raise ProcessStopped()

                with self.assertRaises(ProcessStopped):
                    self._transfer(server, root / "state").upload(source, resume=True, progress=stop)
                if change == "missing":
                    server.parts.clear()
                else:
                    server.parts[1] = b"X" * len(server.parts[1])
                split = len(server.calls)
                with self.assertRaises(RemoteError):
                    self._transfer(server, root / "state").upload(source, resume=True)
                self.assertFalse(any(c[0] in ("PUT", "POST", "finish") for c in server.calls[split:]))

    def test_upload_changed_source_same_stat_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer(failure="PUT")
            source = self._source(root)
            with self.assertRaises(RemoteError):
                self._transfer(server, root / "state").upload(source, resume=True)
            original_stat = source.stat()
            source.write_bytes(b"X" * original_stat.st_size)
            os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            split = len(server.calls)
            with self.assertRaises(LocalIOError):
                self._transfer(server, root / "state").upload(source, resume=True)
            self.assertEqual(len(server.calls), split)

    def test_upload_checkpoint_failure_prevents_next_api_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer()
            source = self._source(root)
            original = TransferLedger.persist

            def failed(ledger, data):
                if data["phase"] == "pre_submitting":
                    raise LocalIOError("disk failure")
                original(ledger, data)

            with patch.object(TransferLedger, "persist", failed):
                with self.assertRaises(LocalIOError):
                    self._transfer(server, root / "state").upload(source)
            self.assertFalse(any(c[1] != "GET" for c in server.calls))

    def test_upload_list_parts_invalid_protocol_or_expired_task_never_creates_new_pre(self):
        for failure in ("bucket", "upload-id", "unknown-part", "marker", "missing-field", "doctype", "etag", "no-such-upload", "auth"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root, server = Path(directory), UploadServer(failure="PUT")
                source = self._source(root)
                with self.assertRaises(RemoteError):
                    self._transfer(server, root / "state").upload(source, resume=True)
                server.failure = None
                split = len(server.calls)

                def handler(request):
                    if failure == "auth" and request.url.path.endswith("/auth"):
                        return httpx.Response(200, json=ok({}), request=request)
                    response = server(request)
                    if request.url.host.endswith(".pds.quark.cn") and request.method == "GET":
                        body = response.content
                        if failure == "bucket":
                            body = body.replace(b"<Bucket>ul-zb", b"<Bucket>Wrong_invalid")
                        elif failure == "upload-id":
                            body = body.replace(b"<UploadId>original-upload", b"<UploadId>wrong")
                        elif failure == "unknown-part":
                            body = body.replace(b"<PartNumber>1", b"<PartNumber>2")
                        elif failure == "marker":
                            body = body.replace(b"<IsTruncated>false</IsTruncated>", b"<IsTruncated>true</IsTruncated><NextPartNumberMarker>0</NextPartNumberMarker>")
                        elif failure == "missing-field":
                            body = body.replace(b"<IsTruncated>false</IsTruncated>", b"")
                        elif failure == "doctype":
                            body = b'<!DOCTYPE a [<!ENTITY x "secret">]>' + body
                        elif failure == "etag":
                            body = body.replace(hashlib.md5(source.read_bytes()).hexdigest().encode(), b"0" * 32)
                        elif failure == "no-such-upload":
                            return httpx.Response(404, content=b"<Error><Code>NoSuchUpload</Code></Error>", request=request)
                        return httpx.Response(200, content=body, request=request)
                    return response

                with self.assertRaises(RemoteError):
                    self._transfer(handler, root / "state").upload(source, resume=True)
                self.assertEqual(sum(c[0] == "pre" for c in server.calls), 1)
                self.assertFalse(any(c[0] in ("PUT", "POST", "finish") for c in server.calls[split:]))

    def test_pds_internal_bucket_mapping_preserves_key_and_upload_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer(failure="PUT", uppercase=True)
            source = self._source(root)
            with self.assertRaises(RemoteError):
                self._transfer(server, root / "state").upload(source, resume=True)
            server.failure = None
            server.listed_bucket = "ccp-region-internal-123"
            split = len(server.calls)
            result = self._transfer(server, root / "state").upload(source, resume=True)
            self.assertEqual(result["skipped_parts"], 1)
            self.assertFalse(any(c[0] in ("pre", "PUT") for c in server.calls[split:]))
            get = next(c for c in server.calls[split:] if c[0] == "GET")
            self.assertEqual(get[2]["uploadId"], "original-upload")

    def test_list_parts_bucket_change_across_pages_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer(page_size=1)
            source = self._source(root, b"x" * (256 * 1024 + 1))
            def stopped(event):
                if event.phase == "part_confirmed" and event.confirmed_parts == 2:
                    raise ProcessStopped()
            with self.assertRaises(ProcessStopped):
                self._transfer(server, root / "state").upload(source, resume=True, progress=stopped)
            server.listed_bucket = "internal-first-region"
            split = len(server.calls)
            def changed(request):
                response = server(request)
                if request.url.host.endswith(".pds.quark.cn") and request.method == "GET" and request.url.params["part-number-marker"] != "0":
                    return httpx.Response(200, content=response.content.replace(b"internal-first-region", b"internal-second-region"), request=request)
                return response
            with self.assertRaisesRegex(RemoteError, "分页"):
                self._transfer(changed, root / "state").upload(source, resume=True)
            self.assertFalse(any(c[0] in ("PUT", "POST", "finish") for c in server.calls[split:]))

    def test_upload_saved_scope_is_enforced_after_parent_moves_without_new_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer(failure="PUT")
            objects = {"anchor": {"fid": "anchor", "pdir_fid": "0", "file_name": "anchor", "file_type": 0},
                       "parent": {"fid": "parent", "pdir_fid": "anchor", "file_name": "parent", "file_type": 0}}

            def handler(request):
                if request.url.path.endswith("/info") and request.url.params["fid"] in objects:
                    return httpx.Response(200, json=ok(objects[request.url.params["fid"]]), request=request)
                if request.url.path.endswith("/sort"):
                    fid = request.url.params["pdir_fid"]
                    rows = [obj for obj in objects.values() if obj["pdir_fid"] == fid]
                    if server.visible and server.item["pdir_fid"] == fid:
                        rows.append(server.item)
                    return httpx.Response(200, json=ok({"list": rows, "_total": len(rows)}), request=request)
                return server(request)

            transfer = self._transfer(handler, root / "state")
            scope = Scope(RemotePaths(Files(transfer.client)), "anchor")
            source = self._source(root)
            with self.assertRaises(RemoteError):
                transfer.upload(source, "parent", resume=True, scope=scope)
            objects["parent"]["pdir_fid"] = "0"
            split = len(server.calls)
            with self.assertRaisesRegex(UsageError, "scope"):
                self._transfer(handler, root / "state").upload(source, "parent", resume=True)
            self.assertFalse(any(c[1] != "GET" for c in server.calls[split:]))

    def test_upload_resume_detects_new_unassociated_target_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer(failure="PUT")
            source = self._source(root)
            with self.assertRaises(RemoteError):
                self._transfer(server, root / "state").upload(source, resume=True)
            server.visible = True
            split = len(server.calls)
            with self.assertRaises(UsageError):
                self._transfer(server, root / "state").upload(source, resume=True)
            self.assertFalse(any(c[1] != "GET" for c in server.calls[split:]))

    def test_upload_completed_resume_keeps_original_task_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer()
            source = self._source(root)
            self._transfer(server, root / "state").upload(source, resume=True)
            split = len(server.calls)
            result = self._transfer(server, root / "state").upload(source, resume=True)
            self.assertEqual(result["task_id"], "original-task")
            self.assertEqual(result["parts"], 1)
            self.assertFalse(any(c[1] != "GET" for c in server.calls[split:]))

    def test_upload_available_remote_hash_must_match_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer()
            server.item["md5"] = "0" * 32
            with self.assertRaisesRegex(RemoteError, "哈希"):
                self._transfer(server, root / "state").upload(self._source(root), resume=True)
            data = json.loads(next((root / "state").glob("upload-*.json")).read_text())
            self.assertEqual(data["phase"], "finish_confirmed")

    def test_upload_known_fid_transient_index_waits_only_reads_before_leaf_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer(pre_fid=True)
            source = self._source(root)
            reads, leaf_checks = [], []
            def handler(request):
                if request.url.path.endswith("/sort") and server.visible:
                    reads.append("sort")
                    if reads.count("sort") < 3:
                        return httpx.Response(200, json=ok({"list": [], "_total": 0}), request=request)
                if request.url.path.endswith("/info") and request.url.params.get("fid") == "result":
                    reads.append("info")
                return server(request)
            transfer = self._transfer(handler, root / "state")
            original_scope = transfer._scope
            def check(fid, scope, saved=None, **options):
                if fid == "result":
                    leaf_checks.append(reads.count("sort"))
                original_scope(fid, scope, saved, **options)
            with patch.object(transfer, "_scope", check), patch("quark_cli.transfer.time.sleep") as sleep:
                result = transfer.upload(source, resume=True)
            self.assertEqual(result["fid"], "result")
            self.assertEqual(reads, ["info", "sort"] * 3)
            self.assertEqual(leaf_checks, [3])
            self.assertEqual(sleep.call_count, 2)
            for stage in ("pre", "hash", "PUT", "POST", "finish"):
                self.assertEqual(sum(c[0] == stage for c in server.calls), 1)

    def test_upload_known_fid_missing_index_has_fixed_read_budget_and_no_name_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), UploadServer()
            server.item.update(file_name="file.bin", size=10)
            def handler(request):
                if request.url.path.endswith("/sort"):
                    # Same name/size from an unrelated FID is never adopted.
                    row = {**server.item, "fid": "unrelated"}
                    server.calls.append(("sort", "GET", {}, {}, {}))
                    return httpx.Response(200, json=ok({"list": [row], "_total": 1}), request=request)
                return server(request)
            transfer = self._transfer(handler, root / "state")
            with patch("quark_cli.transfer.time.sleep") as sleep:
                with self.assertRaisesRegex(RemoteError, "索引读回超时"):
                    transfer._verify_uploaded("result", "0", "file.bin", 10, None, None)
            self.assertEqual(sum(c[0] == "info" for c in server.calls), UPLOAD_READBACK_ATTEMPTS)
            self.assertEqual(sum(c[0] == "sort" for c in server.calls), UPLOAD_READBACK_ATTEMPTS)
            self.assertTrue(all(c[1] == "GET" for c in server.calls))
            self.assertEqual(sleep.call_count, UPLOAD_READBACK_ATTEMPTS - 1)
            self.assertTrue(all(call.args == (UPLOAD_READBACK_INTERVAL,) for call in sleep.call_args_list))

    def test_upload_completed_fid_wrong_parent_and_real_scope_outside_fail_immediately(self):
        for boundary in ("identity", "current", "saved"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                root, server = Path(directory), UploadServer()
                server.item.update(file_name="file.bin", size=10, pdir_fid="outside")
                directories = {fid: {"fid": fid, "pdir_fid": "0", "file_name": fid, "file_type": 0} for fid in ("anchor", "outside")}
                requests = []
                def handler(request):
                    requests.append(request)
                    if request.url.path.endswith("/info") and request.url.params["fid"] in directories:
                        return httpx.Response(200, json=ok(directories[request.url.params["fid"]]), request=request)
                    if request.url.path.endswith("/sort"):
                        rows = list(directories.values()) if request.url.params["pdir_fid"] == "0" else []
                        return httpx.Response(200, json=ok({"list": rows, "_total": len(rows)}), request=request)
                    return server(request)
                transfer = self._transfer(handler, root / "state")
                scope = Scope(RemotePaths(Files(transfer.client)), "anchor") if boundary == "current" else None
                saved = "anchor" if boundary == "saved" else None
                split = len(requests)
                with patch("quark_cli.transfer.time.sleep") as sleep:
                    with self.assertRaises((UsageError, RemoteError)):
                        transfer._verify_uploaded("result", "0" if boundary == "identity" else "outside", "file.bin", 10, scope, saved)
                sleep.assert_not_called()
                after = requests[split:]
                self.assertTrue(all(request.method == "GET" for request in after))
                if boundary == "identity":
                    self.assertEqual(len(after), 1)
                else:
                    self.assertLessEqual(len(after), 5)
                    self.assertFalse(any(request.url.params.get("fid") == "result" for request in after))

    def test_download_resume_uses_strong_if_range_without_api_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), DownloadServer(b"0123456789")
            output = root / "out.bin"
            self._interrupt_download(server, output, root / "state")
            split = len(server.calls)
            result = self._transfer(server, root / "state").download("download-id", output, resume=True)
            storage = [r for r in server.calls[split:] if r.url.host == "cdn.quark.cn"]
            self.assertEqual(storage[0].headers["range"], "bytes=4-")
            self.assertEqual(storage[0].headers["if-range"], '"stable"')
            self.assertEqual(result["sha256"], hashlib.sha256(server.payload).hexdigest())
            self.assertEqual(output.read_bytes(), server.payload)
            self.assertFalse(list(root.glob("*.part")))

    def test_download_invalid_resume_response_keeps_checkpoint(self):
        for failure in ("ignore-range", "wrong-start", "wrong-total", "encoding", "etag", "416", "truncated"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root, server = Path(directory), DownloadServer(b"0123456789")
                output = root / "out.bin"
                self._interrupt_download(server, output, root / "state")
                part = next(root.glob("*.part"))
                before = part.read_bytes()
                server.failure = failure
                with self.assertRaises(RemoteError):
                    self._transfer(server, root / "state").download("download-id", output, resume=True)
                self.assertFalse(output.exists())
                if failure != "truncated":
                    self.assertEqual(part.read_bytes(), before)

    def test_download_missing_or_weak_etag_does_not_build_false_resume(self):
        for etag in (None, 'W/"weak"'):
            with self.subTest(etag=etag), tempfile.TemporaryDirectory() as directory:
                root, server = Path(directory), DownloadServer(b"bytes", etag=etag)
                with self.assertRaisesRegex(RemoteError, "强 ETag"):
                    self._transfer(server, root / "state").download("download-id", root / "out", resume=True)
                self.assertFalse(list(root.glob("*.part")))

    def test_download_checkpoint_tamper_is_rejected_before_storage_get(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), DownloadServer(b"0123456789")
            output = root / "out"
            self._interrupt_download(server, output, root / "state")
            part = next(root.glob("*.part"))
            part.write_bytes(b"XXXX")
            split = len(server.calls)
            with self.assertRaises(LocalIOError):
                self._transfer(server, root / "state").download("download-id", output, resume=True)
            self.assertFalse(any(r.url.host == "cdn.quark.cn" for r in server.calls[split:]))

    def test_download_unconfirmed_tail_is_truncated_and_remote_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), DownloadServer(b"0123456789")
            output = root / "out"
            self._interrupt_download(server, output, root / "state")
            part = next(root.glob("*.part"))
            with part.open("ab") as stream:
                stream.write(b"unconfirmed")
            self._transfer(server, root / "state").download("download-id", output, resume=True)
            self.assertEqual(output.read_bytes(), server.payload)
            server.payload = b"changed-size"
            split = len(server.calls)
            with self.assertRaises(RemoteError):
                self._transfer(server, root / "state").download("download-id", output, resume=True)
            self.assertFalse(any(r.url.host == "cdn.quark.cn" for r in server.calls[split:]))

    def test_download_verified_payload_416_can_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), DownloadServer(b"0123456789")
            output = root / "out"
            self._interrupt_download(server, output, root / "state", "payload_verified")
            result = self._transfer(server, root / "state").download("download-id", output, resume=True)
            self.assertTrue(result["resumed"])
            self.assertEqual(output.read_bytes(), server.payload)

    def test_completed_download_rechecks_zero_size_and_nonempty_payload(self):
        for payload, replacement in ((b"", b"modified-empty-output"), (b"original", b"tampered"), (b"original", b"larger-than-original")):
            with self.subTest(payload=payload, replacement=replacement), tempfile.TemporaryDirectory() as directory:
                root, server = Path(directory), DownloadServer(payload)
                output = root / "out"
                self._transfer(server, root / "state").download("download-id", output, resume=True)
                output.write_bytes(replacement)
                split = len(server.calls)
                with self.assertRaises(LocalIOError):
                    self._transfer(server, root / "state").download("download-id", output, resume=True)
                self.assertEqual(output.read_bytes(), replacement)
                self.assertFalse(any(r.url.host == "cdn.quark.cn" for r in server.calls[split:]))

    def test_completed_empty_download_and_published_crash_can_finalize(self):
        for payload in (b"", b"0123456789"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                root, server = Path(directory), DownloadServer(payload)
                output = root / "out"
                original = TransferLedger.persist

                def stopped(ledger, data):
                    if data["phase"] == "completed":
                        raise ProcessStopped()
                    original(ledger, data)

                with patch.object(TransferLedger, "persist", stopped):
                    with self.assertRaises(ProcessStopped):
                        self._transfer(server, root / "state").download("download-id", output, resume=True)
                self.assertTrue(output.exists())
                result = self._transfer(server, root / "state").download("download-id", output, resume=True)
                self.assertEqual(result["size"], len(payload))
                self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())

    def test_published_download_cleans_verified_owned_part_after_link_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), DownloadServer(b"0123456789")
            output = root / "out"
            self._interrupt_download(server, output, root / "state", "publishing")
            part = next(root.glob("*.part"))
            # A same-directory hard-link installation can be followed by a
            # process exit before the old part name is removed.
            output.write_bytes(part.read_bytes())
            result = self._transfer(server, root / "state").download("download-id", output, resume=True)
            self.assertEqual(result["sha256"], hashlib.sha256(server.payload).hexdigest())
            self.assertFalse(part.exists())

    def test_download_no_clobber_when_target_appears_during_transfer(self):
        from quark_cli.transfer import _publish
        with tempfile.TemporaryDirectory() as directory:
            root, server = Path(directory), DownloadServer(b"original")
            output = root / "out"

            def race(part, target, overwrite):
                target.write_bytes(b"other-process")
                _publish(part, target, overwrite)

            with patch("quark_cli.transfer._publish", race):
                with self.assertRaises(UsageError):
                    self._transfer(server, root / "state").download("download-id", output, resume=True)
            self.assertEqual(output.read_bytes(), b"other-process")
            self.assertTrue(list(root.glob("*.part")))

    def test_upload_rejects_empty_finish_response(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "file.bin"
            source.write_bytes(b"finish evidence")
            handler, seen = self._upload_handler()

            def empty_finish(request):
                if request.url.path.endswith("/file/upload/finish"):
                    return httpx.Response(200, json=ok({}), request=request)
                return handler(request)

            with QuarkClient("sid=mock", transport=httpx.MockTransport(empty_finish)) as client:
                with self.assertRaises(RemoteError):
                    Transfer(client).upload(source)

    def test_upload_task_rejects_failure_even_when_finish_is_true(self):
        def handler(request):
            return httpx.Response(200, json=ok({"status": 3, "finish": True, "fid": "remote-id"}), request=request)

        with QuarkClient("sid=mock", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(RemoteError):
                Transfer(client)._wait_task("task-id")

    def test_upload_task_rejects_invalid_terminal_types(self):
        for data in ({"status": True}, {"status": "2"}, {"finish": "true"}):
            with self.subTest(data=data):
                with QuarkClient("sid=mock", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=ok(data), request=request))) as client:
                    with self.assertRaises(RemoteError):
                        Transfer(client)._wait_task("task-id")

    def _upload_handler(self, *, part_size=4 * 1024 * 1024, fail=None):
        self.upload_case += 1
        os.environ["QUARK_CONFIG_DIR"] = str(Path(self.config.name) / str(self.upload_case))
        seen = {"api": [], "parts": [], "part_headers": [], "complete": [], "finish": 0, "cookies": []}
        item = {"fid": "remote-id", "file_name": "", "pdir_fid": "0", "size": 0, "file_type": 1, "updated_at": 1}

        def handler(request):
            host = request.url.host
            seen["cookies"].append((host, request.headers.get("cookie")))
            path = request.url.path
            if host == "drive-pc.quark.cn":
                body = json.loads(request.read()) if request.method == "POST" else {}
                seen["api"].append((path, body))
                if path.endswith("/file/sort"):
                    rows = [item] if seen["finish"] else []
                    return httpx.Response(200, json=ok({"list": rows, "_total": len(rows)}), request=request)
                if path.endswith("/file/info"):
                    return httpx.Response(200, json=ok(item), request=request)
                if path.endswith("/file/upload/pre"):
                    item.update({"file_name": body["file_name"], "size": body["size"], "pdir_fid": body["pdir_fid"]})
                    if fail == "pre":
                        return httpx.Response(200, json={"status": 400, "code": 9, "message": "failed"}, request=request)
                    return httpx.Response(200, json=ok({
                        "task_id": "task-1", "upload_id": "upload-1", "obj_key": "object/key",
                        "bucket": "ul-zb", "auth_info": "opaque-auth", "callback": {"callbackUrl": "https://drive-pc.quark.cn/callback"},
                        "part_size": part_size,
                    }), request=request)
                if path.endswith("/file/update/hash"):
                    if fail == "hash":
                        return httpx.Response(200, json={"status": 400, "code": 9, "message": "failed"}, request=request)
                    return httpx.Response(200, json=ok({}), request=request)
                if path.endswith("/file/upload/auth"):
                    if fail == "auth":
                        return httpx.Response(200, json=ok({}), request=request)
                    return httpx.Response(200, json=ok({"auth_key": "oss-mock-auth"}), request=request)
                if path.endswith("/file/upload/finish"):
                    seen["finish"] += 1
                    if fail in ("finish", "callback_finish"):
                        return httpx.Response(200, json={"status": 400, "code": 9, "message": "failed"}, request=request)
                    return httpx.Response(200, json=ok({
                        "finish": True,
                        "fid": "remote-id",
                        "preview_url": "https://cdn.quark.cn/view?auth_key=upload-auth-private",
                        "nested": {"token": "finish-token-private"},
                    }), request=request)
                return httpx.Response(404, request=request)
            if host == "ul-zb.pds.quark.cn":
                if request.method == "PUT":
                    content = request.read()
                    seen["parts"].append(content)
                    seen["part_headers"].append(dict(request.headers))
                    if fail == "part":
                        return httpx.Response(500, request=request)
                    return httpx.Response(
                        200,
                        headers={
                            "etag": '"' + hashlib.md5(content).hexdigest() + '"',
                            "set-cookie": "storage-session=storage-private; Domain=.pds.quark.cn; Path=/",
                        },
                        request=request,
                    )
                if request.method == "POST":
                    seen["complete"].append(request.read())
                    if fail == "merge":
                        return httpx.Response(500, request=request)
                    if fail in ("callback", "callback_finish"):
                        return httpx.Response(203, request=request)
                    return httpx.Response(200, content=b"<CompleteMultipartUploadResult/>", request=request)
            return httpx.Response(404, request=request)

        return handler, seen

    def test_upload_single_part(self):
        for content in (b"small file",):
            with self.subTest(size=len(content)), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "test.bin"
                source.write_bytes(content)
                handler, seen = self._upload_handler()
                with QuarkClient("sid=account-cookie", transport=httpx.MockTransport(handler)) as client:
                    result = Transfer(client).upload(source)
                self.assertEqual(result["parts"], 1)
                self.assertEqual(result["fid"], "remote-id")
                self.assertEqual(set(result), {"fid", "file_name", "file_size", "task_id", "parts", "callback_warning"})
                self.assertNotIn("upload-auth-private", str(result))
                self.assertNotIn("finish-token-private", str(result))
                self.assertEqual(seen["parts"], [content])
                self.assertEqual(seen["part_headers"][0]["content-length"], str(len(content)))
                self.assertEqual(seen["finish"], 1)
                self.assertEqual(len(seen["complete"]), 1)
                self.assertTrue(all(cookie is None for host, cookie in seen["cookies"] if host != "drive-pc.quark.cn"))
                put_auth = next(body for path, body in seen["api"] if path.endswith("/file/upload/auth"))
                self.assertIn("PUT\n\napplication/octet-stream", put_auth["auth_meta"])
                post_auth = next(
                    body["auth_meta"] for path, body in seen["api"]
                    if path.endswith("/file/upload/auth") and body["auth_meta"].startswith("POST\n")
                )
                self.assertTrue(post_auth.startswith("POST\n"))

    def test_upload_multiple_parts_uses_server_size_and_sha1_context(self):
        part_size = 128 * 1024
        content = bytes((index % 251 for index in range(part_size * 2 + 17)))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large.bin"
            source.write_bytes(content)
            handler, seen = self._upload_handler(part_size=part_size)
            with QuarkClient("sid=account-cookie", transport=httpx.MockTransport(handler)) as client:
                result = Transfer(client).upload(source)
        self.assertEqual(result["parts"], 3)
        self.assertEqual(b"".join(seen["parts"]), content)
        self.assertEqual([len(part) for part in seen["parts"]], [part_size, part_size, 17])
        self.assertEqual([header["content-length"] for header in seen["part_headers"]], [str(part_size), str(part_size), "17"])
        auth_calls = [body for path, body in seen["api"] if path.endswith("/file/upload/auth")]
        put_calls = [call for call in auth_calls if call["auth_meta"].startswith("PUT\n")]
        self.assertEqual(len(put_calls), 3)
        self.assertNotIn("X-Oss-Hash-Ctx", put_calls[0]["auth_meta"])
        for index, call in enumerate(put_calls[1:], start=1):
            context = call["auth_meta"].split("X-Oss-Hash-Ctx:", 1)[1].split("\n", 1)[0]
            self.assertLess(call["auth_meta"].index("X-Oss-Hash-Ctx:"), call["auth_meta"].index("x-oss-date:"))
            state = json.loads(base64.b64decode(context))
            self.assertEqual(state["hash_type"], "sha1")
            self.assertEqual(state["Nl"], str(part_size * index * 8))
            self.assertEqual(state["num"], "0")
            # Continue the context with SHA-1 padding and compare all h words
            # against hashlib, independently validating compression state.
            reference = _SHA1State()
            reference.h = [int(state[f"h{n}"]) for n in range(5)]
            reference.length = part_size * index
            reference.update_aligned(b"\x80" + b"\x00" * 55 + (reference.length * 8).to_bytes(8, "big"))
            actual = b"".join(word.to_bytes(4, "big") for word in reference.h).hex()
            self.assertEqual(actual, hashlib.sha1(content[:part_size * index]).hexdigest())
        complete_xml = seen["complete"][0]
        self.assertIn(b"<PartNumber>3</PartNumber>", complete_xml)
        self.assertIn(('"' + hashlib.md5(content[-17:]).hexdigest() + '"').encode(), complete_xml)

    def test_every_upload_stage_failure_is_an_error_and_never_finishes_early(self):
        content = b"f" * (128 * 1024 + 1)
        for failure in ("pre", "hash", "auth", "part", "merge", "finish"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "file.bin"
                source.write_bytes(content)
                handler, seen = self._upload_handler(part_size=128 * 1024, fail=failure)
                with QuarkClient("sid=very-secret-cookie", transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(RemoteError) as caught:
                        Transfer(client).upload(source)
                self.assertNotIn("very-secret-cookie", str(caught.exception))
                if failure in ("pre", "hash", "auth", "part", "merge"):
                    self.assertEqual(seen["finish"], 0)
                else:
                    self.assertEqual(seen["finish"], 1)

    def test_oss_203_callback_warning_requires_quark_finish_success(self):
        for failure, expected_code in (("callback", 0), ("callback_finish", 4)):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "file.bin"
                source.write_bytes(b"callback status")
                handler, seen = self._upload_handler(fail=failure)
                with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
                    if expected_code:
                        with self.assertRaises(RemoteError):
                            Transfer(client).upload(source)
                    else:
                        result = Transfer(client).upload(source)
                        self.assertTrue(result["callback_warning"])
                self.assertEqual(seen["finish"], 1)

    def _download_handler(self, body=b"download bytes", *, status=200, url="https://cdn.quark.cn/file?signature=mock"):
        seen = []

        def handler(request):
            seen.append(request)
            if request.url.host == "drive-pc.quark.cn":
                self.assertEqual(json.loads(request.read()), {"fids": ["fid-1"]})
                return httpx.Response(200, json=ok({"list": [{"fid": "fid-1", "file_name": "remote.bin", "size": len(body), "download_url": url}]}), request=request)
            if request.url.host == "cdn.quark.cn":
                return httpx.Response(status, content=body, request=request)
            return httpx.Response(404, request=request)

        return handler, seen

    def test_download_streams_without_cookie_and_installs_content(self):
        body = b"download bytes" * 300
        handler, seen = self._download_handler(body)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.bin"
            with QuarkClient("sid=private-cookie", transport=httpx.MockTransport(handler)) as client:
                result = Transfer(client).download("fid-1", output)
            self.assertEqual(output.read_bytes(), body)
        self.assertEqual(result["size"], len(body))
        self.assertEqual(seen[0].headers["user-agent"], QUARK_DESKTOP_USER_AGENT)
        self.assertEqual(seen[1].headers.get("cookie"), None)
        self.assertEqual(seen[1].headers.get("accept-encoding"), "identity")
        self.assertEqual(seen[1].url.host, "cdn.quark.cn")

    def test_parallel_download_validates_ranges_and_installs_content(self):
        import re
        import threading
        import time

        body = bytes(range(256)) * 4
        seen_ranges = []
        activity = {"active": 0, "max_active": 0}
        lock = threading.Lock()

        def handler(request):
            if request.url.host == "drive-pc.quark.cn":
                return httpx.Response(200, json=ok({"list": [{"fid": "fid-1", "size": len(body), "download_url": "https://cdn.quark.cn/file"}]}), request=request)
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers.get("range", ""))
            self.assertIsNotNone(match)
            start, end = map(int, match.groups())
            with lock:
                activity["active"] += 1
                activity["max_active"] = max(activity["max_active"], activity["active"])
                seen_ranges.append((start, end))
            time.sleep(0.02)
            with lock:
                activity["active"] -= 1
            return httpx.Response(206, content=body[start:end + 1], headers={
                "content-range": f"bytes {start}-{end}/{len(body)}", "content-length": str(end - start + 1),
                "etag": '"stable-version"', "content-encoding": "identity",
            }, request=request)

        with tempfile.TemporaryDirectory() as directory, \
                patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MIN_SIZE", 128), \
                patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MIN_CHUNK", 64), \
                patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MAX_CHUNK", 64):
            output = Path(directory) / "out.bin"
            with QuarkClient("sid=private-cookie", transport=httpx.MockTransport(handler)) as client:
                result = Transfer(client).download("fid-1", output, connections=4)
            self.assertEqual(output.read_bytes(), body)
            self.assertEqual(result["size"], len(body))
        self.assertEqual(seen_ranges[0], (0, 63))
        self.assertEqual(sum(end - start + 1 for start, end in seen_ranges), len(body))
        self.assertGreater(activity["max_active"], 1)

    def test_parallel_download_falls_back_when_range_is_ignored(self):
        body = b"fallback to the established stream"
        data_requests = []

        def handler(request):
            if request.url.host == "drive-pc.quark.cn":
                return httpx.Response(200, json=ok({"list": [{"fid": "fid-1", "size": len(body), "download_url": "https://cdn.quark.cn/file"}]}), request=request)
            data_requests.append(request)
            return httpx.Response(200, content=body, request=request)

        with tempfile.TemporaryDirectory() as directory, \
                patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MIN_SIZE", 1), \
                patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MIN_CHUNK", 4), \
                patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MAX_CHUNK", 4):
            output = Path(directory) / "out.bin"
            with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
                Transfer(client).download("fid-1", output, connections=4)
            self.assertEqual(output.read_bytes(), body)
            self.assertFalse(list(Path(directory).glob(".quark-download-*")))
        self.assertEqual(len(data_requests), 1)
        self.assertEqual(data_requests[0].headers.get("range"), "bytes=0-3")

    def test_parallel_download_falls_back_when_range_has_no_strong_etag(self):
        body = b"serial fallback after an unversioned range probe"
        data_requests = []

        def handler(request):
            if request.url.host == "drive-pc.quark.cn":
                return httpx.Response(200, json=ok({"list": [{"fid": "fid-1", "size": len(body), "download_url": "https://cdn.quark.cn/file"}]}), request=request)
            data_requests.append(request)
            if request.headers.get("range"):
                return httpx.Response(206, content=body[:4], headers={
                    "content-range": f"bytes 0-3/{len(body)}", "content-length": "4",
                }, request=request)
            return httpx.Response(200, content=body, request=request)

        with tempfile.TemporaryDirectory() as directory, \
                patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MIN_SIZE", 1), \
                patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MIN_CHUNK", 4), \
                patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MAX_CHUNK", 4):
            output = Path(directory) / "out.bin"
            with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
                Transfer(client).download("fid-1", output, connections=4)
            self.assertEqual(output.read_bytes(), body)
        self.assertEqual(len(data_requests), 2)
        self.assertEqual(data_requests[0].headers.get("range"), "bytes=0-3")
        self.assertIsNone(data_requests[1].headers.get("range"))

    def test_parallel_download_rejects_mixed_etag_and_preserves_existing_file(self):
        import re

        body = b"0123456789abcdef" * 32

        def handler(request):
            if request.url.host == "drive-pc.quark.cn":
                return httpx.Response(200, json=ok({"list": [{"fid": "fid-1", "size": len(body), "download_url": "https://cdn.quark.cn/file"}]}), request=request)
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers.get("range", ""))
            start, end = map(int, match.groups())
            etag = '"changed"' if start else '"stable"'
            return httpx.Response(206, content=body[start:end + 1], headers={
                "content-range": f"bytes {start}-{end}/{len(body)}", "etag": etag,
            }, request=request)

        with tempfile.TemporaryDirectory() as directory, \
                patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MIN_SIZE", 128), \
                patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MIN_CHUNK", 64), \
                patch("quark_cli.transfer.PARALLEL_DOWNLOAD_MAX_CHUNK", 64):
            output = Path(directory) / "out.bin"
            output.write_bytes(b"old")
            with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(RemoteError, "ETag"):
                    Transfer(client).download("fid-1", output, overwrite=True, connections=4)
            self.assertEqual(output.read_bytes(), b"old")
            self.assertFalse(list(Path(directory).glob(".quark-download-*")))

    def test_download_sends_only_puus_to_pds_where_anonymous_get_would_be_403(self):
        body = b"authenticated PDS download"
        seen = []

        def handler(request):
            seen.append(request)
            if request.url.host == "drive-pc.quark.cn":
                return httpx.Response(
                    200,
                    json=ok({"list": [{"fid": "fid-1", "size": len(body), "download_url": "https://dl-pc-sz.pds.quark.cn/object?signature=private"}]}),
                    request=request,
                )
            if request.url.host == "dl-pc-sz.pds.quark.cn":
                # The service returns 403 to an anonymous request and accepts
                # the minimal browser cookie used by its callback check.
                if request.headers.get("cookie") != "__puus=puus-private":
                    return httpx.Response(403, request=request)
                return httpx.Response(200, content=body, request=request)
            return httpx.Response(404, request=request)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.bin"
            with QuarkClient(
                "sid=account-private; __puus=puus-private; other=other-private",
                transport=httpx.MockTransport(handler),
            ) as client:
                Transfer(client).download("fid-1", output)
            self.assertEqual(output.read_bytes(), body)
        self.assertEqual(seen[1].url.host, "dl-pc-sz.pds.quark.cn")
        self.assertEqual(seen[1].headers.get("cookie"), "__puus=puus-private")
        self.assertNotIn("account-private", seen[1].headers.get("cookie", ""))
        self.assertNotIn("other-private", seen[1].headers.get("cookie", ""))

    def test_download_sends_only_puus_to_validated_drive_storage_subdomain(self):
        body = b"Quark drive storage transfer"
        seen = []

        def handler(request):
            seen.append(request)
            if request.url.host == "drive-pc.quark.cn":
                return httpx.Response(200, json=ok({"list": [{
                    "fid": "fid-1", "size": len(body), "download_url": "https://dl-pc.drive.quark.cn/file?signature=private",
                }]}), request=request)
            return httpx.Response(200, content=body, request=request)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.bin"
            with QuarkClient(
                "sid=account-private; __puus=puus-private; other=other-private",
                transport=httpx.MockTransport(handler),
            ) as client:
                Transfer(client).download("fid-1", output)
            self.assertEqual(output.read_bytes(), body)
        self.assertEqual(seen[1].headers.get("cookie"), "__puus=puus-private")
        self.assertNotIn("account-private", seen[1].headers.get("cookie", ""))
        self.assertNotIn("other-private", seen[1].headers.get("cookie", ""))

    def test_download_does_not_forward_puus_after_redirect_outside_pds(self):
        seen = []

        def handler(request):
            seen.append(request)
            if request.url.host == "drive-pc.quark.cn":
                return httpx.Response(
                    200,
                    json=ok({"list": [{"fid": "fid-1", "size": 4, "download_url": "https://dl-pc-sz.pds.quark.cn/start?signature=private"}]}),
                    request=request,
                )
            if request.url.host == "dl-pc-sz.pds.quark.cn":
                return httpx.Response(
                    302,
                    headers={
                        "location": "https://cdn.quark.cn/file",
                        "set-cookie": "redirect-session=redirect-private; Domain=.quark.cn; Path=/",
                    },
                    request=request,
                )
            if request.url.host == "cdn.quark.cn":
                return httpx.Response(200, content=b"file", request=request)
            return httpx.Response(404, request=request)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.bin"
            with QuarkClient("sid=account-private; __puus=puus-private", transport=httpx.MockTransport(handler)) as client:
                Transfer(client).download("fid-1", output)
            self.assertEqual(output.read_bytes(), b"file")
        self.assertEqual(seen[1].headers.get("cookie"), "__puus=puus-private")
        self.assertIsNone(seen[2].headers.get("cookie"))

    def test_download_uses_puus_rotated_by_api_without_account_cookies(self):
        content = b"download using the refreshed PDS credential"
        seen = []

        def handler(request):
            seen.append(request)
            if request.url.host == "drive-pc.quark.cn":
                return httpx.Response(
                    200,
                    headers={"set-cookie": "__puus=refreshed-private; Domain=.quark.cn; Path=/; Secure"},
                    json=ok({"list": [{"fid": "fid-1", "size": len(content), "download_url": "https://dl-pc-sz.pds.quark.cn/object?signature=private"}]}),
                    request=request,
                )
            if request.headers.get("cookie") != "__puus=refreshed-private":
                return httpx.Response(403, request=request)
            return httpx.Response(
                200,
                headers={"set-cookie": "__puus=storage-private; Domain=.quark.cn; Path=/; Secure"},
                content=content,
                request=request,
            )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.bin"
            with QuarkClient("sid=account-private; __puus=original-private; other=other-private", transport=httpx.MockTransport(handler)) as client:
                Transfer(client).download("fid-1", output)
                self.assertEqual(len(client.transfer_client().cookies), 0)
                self.assertEqual(client.pds_cookie_header("dl-pc-sz.pds.quark.cn"), "__puus=refreshed-private")
            self.assertEqual(output.read_bytes(), content)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1].headers.get("cookie"), "__puus=refreshed-private")
        self.assertNotIn("account-private", seen[1].headers.get("cookie", ""))
        self.assertNotIn("other-private", seen[1].headers.get("cookie", ""))

    def test_download_fails_clearly_when_pds_requires_missing_puus(self):
        seen = []

        def handler(request):
            seen.append(request.url.host)
            if request.url.host == "drive-pc.quark.cn":
                return httpx.Response(
                    200,
                    json=ok({"list": [{"fid": "fid-1", "size": 1, "download_url": "https://dl-pc-sz.pds.quark.cn/file?signature=private"}]}),
                    request=request,
                )
            return httpx.Response(403, request=request)

        with tempfile.TemporaryDirectory() as directory:
            with QuarkClient("sid=account-private", transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(AuthenticationError, "__puus"):
                    Transfer(client).download("fid-1", Path(directory) / "out.bin")
        self.assertEqual(seen, ["drive-pc.quark.cn"])

    def test_download_reports_pds_403_without_echoing_signed_url(self):
        signed_url = "https://dl-pc-sz.pds.quark.cn/file?auth_key=signature-private"

        def handler(request):
            if request.url.host == "drive-pc.quark.cn":
                return httpx.Response(200, json=ok({"list": [{"fid": "fid-1", "size": 0, "download_url": signed_url}]}), request=request)
            return httpx.Response(403, request=request)

        with tempfile.TemporaryDirectory() as directory:
            with QuarkClient("__puus=puus-private", transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(RemoteError) as caught:
                    Transfer(client).download("fid-1", Path(directory) / "out.bin")
        self.assertNotIn("signature-private", str(caught.exception))
        self.assertNotIn("puus-private", str(caught.exception))

    def test_download_failure_preserves_old_file_and_cleans_partial(self):
        handler, _ = self._download_handler(b"new", status=500)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.bin"
            output.write_bytes(b"old")
            with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(RemoteError):
                    Transfer(client).download("fid-1", output, overwrite=True)
            self.assertEqual(output.read_bytes(), b"old")
            self.assertEqual(list(Path(directory).glob("*.quark-download")), [])

    def test_interrupted_download_cleans_temp_and_preserves_old_file(self):
        seen = []

        class BrokenStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"partial"
                raise httpx.ReadError("connection ended")

            def close(self):
                pass

        def handler(request):
            seen.append(request.url.host)
            if request.url.host == "drive-pc.quark.cn":
                return httpx.Response(200, json=ok({"list": [{"fid": "fid-1", "size": 12, "download_url": "https://cdn.quark.cn/file"}]}), request=request)
            return httpx.Response(200, headers={"content-length": "12"}, stream=BrokenStream(), request=request)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.bin"
            output.write_bytes(b"old")
            with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(RemoteError):
                    Transfer(client).download("fid-1", output, overwrite=True)
            self.assertEqual(output.read_bytes(), b"old")
            self.assertEqual(list(Path(directory).glob("*.quark-download")), [])
        self.assertEqual(seen, ["drive-pc.quark.cn", "cdn.quark.cn"])

    def test_download_refuses_existing_file_and_untrusted_redirect(self):
        handler, seen = self._download_handler(b"new")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.bin"
            output.write_bytes(b"old")
            with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(UsageError):
                    Transfer(client).download("fid-1", output)
            self.assertEqual(len(seen), 0)

        seen_hosts = []

        def redirect_handler(request):
            seen_hosts.append(request.url.host)
            if request.url.host == "drive-pc.quark.cn":
                return httpx.Response(200, json=ok({"list": [{"fid": "fid-1", "size": 4, "download_url": "https://cdn.quark.cn/start"}]}), request=request)
            return httpx.Response(302, headers={"location": "https://attacker.invalid/file"}, request=request)

        with tempfile.TemporaryDirectory() as directory:
            with QuarkClient("sid=x", transport=httpx.MockTransport(redirect_handler)) as client:
                with self.assertRaisesRegex(RemoteError, "不受信任"):
                    Transfer(client).download("fid-1", Path(directory) / "out")
        self.assertEqual(seen_hosts, ["drive-pc.quark.cn", "cdn.quark.cn"])

    def test_download_does_not_accept_a_different_fid_record(self):
        seen = []

        def handler(request):
            seen.append(request.url.host)
            return httpx.Response(
                200,
                json=ok({"list": [{"fid": "other-file", "download_url": "https://cdn.quark.cn/file", "size": 0}]}),
                request=request,
            )

        with tempfile.TemporaryDirectory() as directory:
            with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(RemoteError, "缺少下载地址"):
                    Transfer(client).download("fid-1", Path(directory) / "out.bin")
        self.assertEqual(seen, ["drive-pc.quark.cn"])


if __name__ == "__main__":
    unittest.main()
