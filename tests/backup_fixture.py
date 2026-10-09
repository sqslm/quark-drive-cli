"""Durable single-file boundary and exact-FID drive for backup fault regressions."""

import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from quark_cli.errors import RemoteError, UsageError
from quark_cli.recursive import _hashes
from quark_cli.transfer import Transfer
from quark_cli.transfer_progress import ProgressEvent
from quark_cli.transfer_state import UploadLedger

from drive_fixture import DriveFixture, item


class ProcessExit(BaseException):
    pass


class BackupDrive(DriveFixture):
    def __init__(self):
        super().__init__([item("scope", "范围", directory=True), item("target", "备份目标", "scope", directory=True),
                          item("outside", "范围外", directory=True)])
        self.payloads = {}
        self.mkdir_calls, self.rename_calls = [], []
        self.mkdir_mode = self.rename_mode = None
        self.before_rename = lambda body: None
        self.after_listing = lambda parent: None
        self.version = 100

    def add_file(self, fid, name, payload, parent="target", *, version=True, hashes=True):
        self.version += 1
        record = item(fid, name, parent, size=len(payload))
        if version:
            record["updated_at"] = self.version
        if hashes:
            record.update({"md5": hashlib.md5(payload).hexdigest(), "sha1": hashlib.sha1(payload).hexdigest()})
        self.items.append(record)
        self.payloads[fid] = payload
        return record

    def record(self, fid):
        return next(record for record in self.items if record["fid"] == fid)

    def handler(self, request):
        if request.url.path.endswith("/file") and request.method == "POST":
            self.calls.append(request)
            body = json.loads(request.content)
            self.mkdir_calls.append(body)
            record = item("directory-" + str(len(self.mkdir_calls)), body["file_name"], body["pdir_fid"], directory=True)
            if self.mkdir_mode == "before":
                raise httpx.ReadTimeout("raw-cookie-secret", request=request)
            self.items.append(record)
            if self.mkdir_mode == "after":
                raise httpx.ReadTimeout("raw-cookie-secret", request=request)
            return httpx.Response(200, request=request, json={"status": 200, "code": 0, "data": {} if self.mkdir_mode == "no-fid" else record})
        if request.url.path.endswith("/file/rename"):
            self.calls.append(request)
            body = json.loads(request.content)
            self.rename_calls.append(body)
            self.before_rename(body)
            if self.rename_mode == "before":
                raise httpx.ReadTimeout("raw-cookie-secret", request=request)
            record = self.record(body["fid"])
            record["file_name"] = body["file_name"]
            self.version += 1
            record["updated_at"] = self.version
            if self.rename_mode == "after":
                raise httpx.ReadTimeout("raw-cookie-secret", request=request)
            return httpx.Response(200, request=request, json={"status": 200, "code": 0, "data": {}})
        response = super().handler(request)
        if request.url.path.endswith("/file/sort"):
            self.after_listing(request.url.params["pdir_fid"])
        return response


class BackupTransfer(Transfer):
    """Its production protocol is tested in test_transfer; persist real schema here."""
    def __init__(self, client, drive, directory):
        super().__init__(client, state_dir=directory)
        self.drive = drive
        self.uploads, self.proofs, self.streams, self.limiters = [], [], [], []
        self.counts = {"pre": 0, "part": 0, "merge": 0, "finish": 0}
        self.after_pre = self.after_upload = lambda data: None
        self.before_upload = lambda path, parent, options: None
        self.before_proof = lambda fid: None
        self.version = self.hashes = True
        self.response_headers = {}
        self.response_status = 200

    def upload(self, path, parent, **options):
        path = Path(path)
        self.before_upload(path, parent, options)
        self.limiters.append(options["limiter"])
        name = options.get("remote_name", path.name)
        local = self._fingerprint(path)
        ledger = UploadLedger({"source": str(path.resolve()), "parent": parent, "name": name}, directory=self.state_dir)
        with ledger.lock:
            data = ledger.load()
            if data is None:
                self.counts["pre"] += 1
                number = self.counts["pre"]
                now = time.time()
                task, fid = "task-" + str(number), "uploaded-" + str(number)
                data = {"version": 1, "direction": "upload", "phase": "parts", "identity": ledger.identity, "local": local,
                        "scope_fid": options["scope"].fid if options.get("scope") else None,
                        "upload": {"task_id": task, "upload_id": "upload-" + str(number), "bucket": "ul-zb", "obj_key": "object-private-canary",
                                   "auth_info": "auth-private-canary", "callback": {"callbackUrl": "https://example.invalid/private"},
                                   "mime_type": "application/octet-stream", "part_size": 4194304, "part_count": 1},
                        "parts": [{"number": 1, "offset": 0, "length": local["size"], "md5": local["md5"], "etag": None, "confirmed": False}],
                        "flash": False, "upload_task_id": task, "finish_task_id": None, "final_fid": fid, "final_identity": None,
                        "callback_warning": False, "created_at": now, "updated_at": now}
                ledger.persist(data)
                self.after_pre(data)
            elif not options.get("resume") or data["local"] != local:
                raise UsageError("cannot replace original task")
            if data["phase"] == "completed":
                raise RemoteError("completed object without usable version")
            self.uploads.append((path, parent, options, data["upload_task_id"]))
            payload = path.read_bytes()
            record = self.drive.add_file(data["final_fid"], name, payload, parent, version=self.version, hashes=self.hashes)
            for key in ("part", "merge", "finish"):
                self.counts[key] += 1
            data.update({"phase": "completed", "upload": {}, "final_identity": {"fid": record["fid"], "parent": parent, "name": name, "size": len(payload),
                          **({"updated_at": record["updated_at"]} if self.version else {})}})
            data["parts"][0].update({"etag": data["local"]["md5"].upper(), "confirmed": True})
            ledger.persist(data)
            callback = options.get("progress")
            if callback:
                callback(ProgressEvent("upload", "completed", name, len(payload), len(payload), len(payload), 1.0))
            self.after_upload(data)
            return {"fid": record["fid"], "file_size": len(payload), "task_id": data["upload_task_id"], "auth_info": "not-public"}

    def _download_address(self, fid):
        self.before_proof(fid)
        self.proofs.append(fid)
        return fid, len(self.drive.payloads[fid])

    @contextmanager
    def _download_stream(self, url, headers):
        self.streams.append(url)
        yield httpx.Response(self.response_status, headers=self.response_headers, content=self.drive.payloads[url])
