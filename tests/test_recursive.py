import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quark_cli.client import QuarkClient
from quark_cli.errors import LocalIOError, QuarkError, RemoteError, UsageError
from quark_cli.recursive import RecursiveTransfer, local_name
from quark_cli.state import read_json
from quark_cli.transfer_progress import ProgressEvent

from drive_fixture import DriveFixture, item


class ProcessExit(BaseException):
    pass


class TreeFixture(DriveFixture):
    def __init__(self):
        super().__init__()
        self.mkdir_calls = []
        self.payloads = {}

    def handler(self, request):
        if request.url.path.endswith("/file") and request.method == "POST":
            self.calls.append(request)
            body = json.loads(request.content)
            self.mkdir_calls.append(body)
            record = item("directory-" + str(len(self.mkdir_calls)), body["file_name"], body["pdir_fid"], directory=True)
            self.items.append(record)
            return self.response(request, record)
        return super().handler(request)

    @staticmethod
    def response(request, data):
        import httpx
        return httpx.Response(200, json={"status": 200, "code": 0, "data": data}, request=request)


class FileTransfer:
    """Behavioral single-file boundary; its HTTP state machine is tested separately."""
    def __init__(self, fixture):
        self.fixture = fixture
        self.uploads, self.downloads = [], []
        self.after_upload = lambda record: None
        self.before_upload = lambda path, parent, options: None
        self.before_download = lambda fid, output, options: None

    def upload(self, path, parent, **options):
        self.before_upload(Path(path), parent, options)
        payload = Path(path).read_bytes()
        name = options.get("remote_name", Path(path).name)
        matches = [record for record in self.fixture.items if record.get("pdir_fid") == parent and record["file_name"] == name]
        if matches:
            if not options.get("resume") or self.fixture.payloads.get(matches[0]["fid"]) != payload:
                raise UsageError("single file conflict")
            return {"fid": matches[0]["fid"]}
        self.uploads.append((Path(path), parent, options))
        record = item("uploaded-" + str(len(self.uploads)), name, parent, size=len(payload), updated_at=123, md5=hashlib.md5(payload).hexdigest())
        self.fixture.items.append(record)
        self.fixture.payloads[record["fid"]] = payload
        callback = options.get("progress")
        if callback:
            callback(ProgressEvent("upload", "completed", name, len(payload), len(payload), len(payload), 1.0))
        self.after_upload(record)
        return {"fid": record["fid"], "file_size": len(payload)}

    def download(self, fid, output, **options):
        output = Path(output)
        self.before_download(fid, output, options)
        self.downloads.append((fid, output, options))
        output.write_bytes(self.fixture.payloads[fid])
        callback = options.get("progress")
        if callback:
            size = len(self.fixture.payloads[fid])
            callback(ProgressEvent("download", "completed", output.name, size, size, size, 1.0))
        return {"fid": fid, "path": str(output), "size": output.stat().st_size}


class RecursiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "中文 上传"
        self.source.mkdir()
        (self.source / "空目录").mkdir()
        (self.source / "资料").mkdir()
        (self.source / "a.txt").write_bytes(b"one")
        (self.source / "资料" / "中文 文件.txt").write_bytes("公开测试".encode())
        self.fixture = TreeFixture()
        self.client = QuarkClient("offline-cookie", transport=self.fixture.transport())
        self.addCleanup(self.client.close)
        self.transfer = FileTransfer(self.fixture)
        self.recursive = RecursiveTransfer(self.client, transfer=self.transfer, state_dir=self.root / "state")
        from quark_cli.paths import Scope
        self.scope = Scope(self.recursive.paths, "scope")

    def upload(self, **options):
        return self.recursive.upload_tree(self.source, "scope", scope=self.scope, **options)

    def state(self, direction="upload", output=None):
        _, path, _ = self.recursive._state(direction, str(self.source) if direction == "upload" else "remote-root", "scope" if direction == "upload" else str(output), self.source.name if direction == "upload" else "")
        return path, read_json(path)

    def remote_tree(self):
        records = [item("remote-root", "下载源", "scope", directory=True), item("remote-empty", "空目录", "remote-root", directory=True),
                   item("remote-sub", "资料", "remote-root", directory=True)]
        for fid, name, parent, payload in (("remote-a", "a.txt", "remote-root", b"one"), ("remote-b", "中文 文件.txt", "remote-sub", "公开测试".encode())):
            records.append(item(fid, name, parent, size=len(payload), updated_at=123, md5=hashlib.md5(payload).hexdigest()))
            self.fixture.payloads[fid] = payload
        self.fixture.items.extend(records)

    def test_portable_names_reject_devices_ads_controls_and_trailing_dot_space(self):
        for name in ("CON", "con.txt", "PRN.log", "nul", "AUX", "COM1.txt", "LPT9", "COM²", "LPT³.dat", "stream:ads", "last.", "last ", "a?b", "a\tb", "../x"):
            with self.subTest(name=name), self.assertRaises(UsageError):
                local_name(name)
        self.assertEqual(local_name("中文 空格.txt"), "中文 空格.txt")

    def test_upload_chinese_files_empty_directories_and_root_mapping(self):
        result = self.upload()
        self.assertEqual((result["files"], result["directories"], result["uploaded_files"]), (2, 3, 2))
        root = next(record for record in self.fixture.items if record["fid"] == result["destination_fid"])
        self.assertEqual((root["file_name"], root["pdir_fid"]), (self.source.name, "scope"))
        path, ledger = self.state()
        self.assertTrue(ledger["completed"])
        self.assertEqual(set(ledger["directories"]), {"", "空目录", "资料"})
        self.assertEqual(set(ledger["files"]), {"a.txt", "资料/中文 文件.txt"})
        self.assertNotIn("offline-cookie", path.read_text(encoding="utf-8"))

    def test_dry_run_creates_no_state_or_lock_and_zero_writes(self):
        result = self.upload(dry_run=True)
        self.assertEqual((result["dry_run"], result["files"]), (True, 2))
        self.assertFalse((self.root / "state").exists())
        self.assertEqual(self.fixture.writes, [])
        self.assertEqual(self.transfer.uploads, [])

    def test_resume_completed_files_uses_fids_and_hashes_without_uploads(self):
        self.upload()
        count = len(self.transfer.uploads)
        result = self.upload(resume=True)
        self.assertEqual((result["uploaded_files"], result["skipped_files"]), (0, 2))
        self.assertEqual(len(self.transfer.uploads), count)
        self.assertEqual(self.transfer.downloads, [])

    def test_missing_remote_hash_uses_readback_sha256_before_skip(self):
        self.upload()
        path, ledger = self.state()
        from quark_cli.state import write_json
        for record in self.fixture.items:
            record.pop("md5", None)
        for receipt in ledger["files"].values():
            receipt["identity"].pop("md5", None)
        write_json(path, ledger)
        self.assertEqual(self.upload(resume=True)["skipped_files"], 2)
        self.assertEqual(len(self.transfer.downloads), 2)

    def test_fixed_tree_rejects_new_file_or_same_size_changed_content(self):
        self.upload()
        count = len(self.transfer.uploads)
        original = (self.source / "a.txt").stat()
        (self.source / "a.txt").write_bytes(b"two")
        os.utime(self.source / "a.txt", ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(UsageError):
            self.upload(resume=True)
        (self.source / "a.txt").write_bytes(b"one")
        os.utime(self.source / "a.txt", ns=(original.st_atime_ns, original.st_mtime_ns))
        (self.source / "later.txt").write_bytes(b"later")
        with self.assertRaises(UsageError):
            self.upload(resume=True)
        self.assertEqual(len(self.transfer.uploads), count)

    def test_existing_root_directory_reused_but_file_collision_prevents_all_writes(self):
        self.fixture.items.extend([item("existing-root", self.source.name, "scope", directory=True),
                                   item("existing-sub", "资料", "existing-root", directory=True),
                                   item("existing-file", "中文 文件.txt", "existing-sub", size=12)])
        with self.assertRaises(UsageError):
            self.upload()
        self.assertEqual(self.fixture.writes, [])
        self.assertEqual(self.transfer.uploads, [])

    def test_casefold_and_unicode_normalization_collisions_prewrite(self):
        (self.source / "A.TXT").write_bytes(b"collision")
        if not (self.source / "a.txt").samefile(self.source / "A.TXT"):
            with self.assertRaises(UsageError):
                self.upload()
            self.assertEqual(self.fixture.writes, [])
        for names in (("é", "e\u0301"),):
            from quark_cli.recursive import _names
            with self.assertRaises(UsageError):
                _names(names)

    def test_linux_actual_symlink_or_windows_actual_junction_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"unselected")
        link = self.source / "linked"
        if sys.platform == "win32":
            script = "$p=[Console]::In.ReadToEnd()|ConvertFrom-Json; New-Item -ItemType Junction -Path $p.link -Target $p.target | Out-Null"
            response = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", "$ErrorActionPreference='Stop'; " + script],
                                      input=json.dumps({"link": str(link), "target": str(outside)}), capture_output=True, text=True, check=False)
            self.assertEqual(response.returncode, 0)
        else:
            link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(UsageError):
            self.upload()
        self.assertEqual(self.fixture.writes, [])

    def test_scope_moved_directory_rejected_before_next_file_upload(self):
        def move(record):
            root = next(entry for entry in self.fixture.items if entry["file_name"] == self.source.name)
            root["pdir_fid"] = "outside"
        self.transfer.after_upload = move
        with self.assertRaises(UsageError):
            self.upload()
        self.assertEqual(len(self.transfer.uploads), 1)

    def test_mkdir_intent_is_durable_before_request_and_unknown_not_retried(self):
        def unknown(name, parent):
            _, ledger = self.state()
            self.assertEqual((ledger["pending"]["kind"], ledger["pending"]["fid"]), ("mkdir", None))
            self.fixture.items.append(item("unknown-root", name, parent, directory=True))
            raise RemoteError("response lost")
        with patch.object(self.recursive.files, "mkdir", side_effect=unknown):
            with self.assertRaises(RemoteError):
                self.upload()
        with self.assertRaises(UsageError):
            self.upload(resume=True)
        self.assertEqual(self.transfer.uploads, [])

    def test_created_fid_persisted_before_confirmation_and_recovered_without_mkdir(self):
        with patch.object(self.recursive.saver, "_confirm_created_directory", side_effect=ProcessExit), self.assertRaises(ProcessExit):
            self.upload()
        _, ledger = self.state()
        self.assertEqual(ledger["pending"]["fid"], "directory-1")
        self.upload(resume=True)
        self.assertEqual(len(self.fixture.mkdir_calls), 3)

    def test_file_completion_crash_recovers_exact_original_file(self):
        def stopped(record):
            _, ledger = self.state()
            self.assertEqual(ledger["pending"]["kind"], "upload")
            raise ProcessExit()
        self.transfer.after_upload = stopped
        with self.assertRaises(ProcessExit):
            self.upload()
        self.transfer.after_upload = lambda record: None
        with self.assertRaises(UsageError):
            self.upload()
        result = self.upload(resume=True)
        self.assertEqual(result["files"], 2)
        self.assertEqual(len(self.transfer.uploads), 2)

    def test_state_write_failure_before_mkdir_zero_remote_writes(self):
        with patch("quark_cli.recursive.write_json", side_effect=LocalIOError("failure")), self.assertRaises(LocalIOError):
            self.upload()
        self.assertEqual(self.fixture.writes, [])
        self.assertEqual(self.transfer.uploads, [])

    def test_completed_directory_same_name_new_fid_is_not_rebound(self):
        self.upload()
        _, ledger = self.state()
        old = ledger["directories"][""]["fid"]
        next(record for record in self.fixture.items if record["fid"] == old)["fid"] = "replacement-root"
        with self.assertRaises(QuarkError):
            self.upload(resume=True)
        self.assertEqual(len(self.transfer.uploads), 2)

    def test_shared_limiter_and_aggregate_safe_progress_across_files(self):
        events = []
        result = self.upload(limit_rate=100, progress=events.append)
        limiters = [call[2]["limiter"] for call in self.transfer.uploads]
        self.assertIs(limiters[0], limiters[1])
        self.assertEqual(limiters[0].bytes_per_second, 100)
        self.assertEqual(events[-1].confirmed, result["size"])
        self.assertEqual(events[-1].total, result["size"])
        self.assertEqual(events[-1].name, "资料/中文 文件.txt")

    def test_download_contents_to_output_and_empty_directories(self):
        self.remote_tree()
        output = self.root / "downloaded"
        result = self.recursive.download_tree("remote-root", output, scope=self.scope)
        self.assertEqual((result["files"], result["directories"]), (2, 3))
        self.assertEqual((output / "a.txt").read_bytes(), b"one")
        self.assertEqual((output / "资料" / "中文 文件.txt").read_bytes(), "公开测试".encode())
        self.assertTrue((output / "空目录").is_dir())
        self.assertFalse((output / "下载源").exists())

    def test_download_late_file_collision_prevents_any_local_creation(self):
        self.remote_tree()
        output = self.root / "downloaded"
        (output / "资料").mkdir(parents=True)
        (output / "资料" / "中文 文件.txt").write_bytes(b"existing")
        with self.assertRaises(UsageError):
            self.recursive.download_tree("remote-root", output, scope=self.scope)
        self.assertFalse((output / "a.txt").exists())
        self.assertFalse((output / "空目录").exists())
        self.assertEqual(self.transfer.downloads, [])

    def test_download_portable_name_collision_and_path_escape_preflight(self):
        self.remote_tree()
        output = self.root / "downloaded"
        for name in ("../escape", "NUL.txt", "stream:ads", "name."):
            record = next(record for record in self.fixture.items if record["fid"] == "remote-b")
            record["file_name"] = name
            with self.subTest(name=name), self.assertRaises(QuarkError):
                self.recursive.download_tree("remote-root", output, scope=self.scope)
            self.assertFalse(output.exists())
        record["file_name"] = "中文 文件.txt"
        self.fixture.items.append(item("case-duplicate", "A.TXT", "remote-root", size=1, updated_at=123))
        with self.assertRaises(UsageError):
            self.recursive.download_tree("remote-root", output, scope=self.scope)
        self.assertFalse(output.exists())

    def test_download_resume_completed_local_hash_and_remote_identity(self):
        self.remote_tree()
        output = self.root / "downloaded"
        self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        count = len(self.transfer.downloads)
        result = self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        self.assertEqual((result["downloaded_files"], result["skipped_files"]), (0, 2))
        self.assertEqual(len(self.transfer.downloads), count)
        (output / "a.txt").write_bytes(b"two")
        with self.assertRaises(UsageError):
            self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        self.assertEqual(len(self.transfer.downloads), count)

    def test_download_missing_completed_tree_can_be_downloaded_again(self):
        import shutil
        self.remote_tree()
        output = self.root / "downloaded"
        self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        shutil.rmtree(output)
        result = self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        self.assertEqual((result["downloaded_files"], result["skipped_files"]), (2, 0))
        self.assertEqual((output / "a.txt").read_bytes(), b"one")
        self.assertTrue((output / "空目录").is_dir())
        _, ledger = self.state("download", output)
        self.assertTrue(ledger["completed"])

    def test_download_missing_member_is_retrieved_without_overwriting_kept_files(self):
        self.remote_tree()
        output = self.root / "downloaded"
        self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        (output / "a.txt").unlink()
        kept = output / "资料" / "中文 文件.txt"
        timestamp = kept.stat().st_mtime_ns
        result = self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        self.assertEqual((result["downloaded_files"], result["skipped_files"]), (1, 1))
        self.assertEqual(kept.stat().st_mtime_ns, timestamp)
        self.assertEqual((output / "a.txt").read_bytes(), b"one")

    def test_missing_member_does_not_allow_changed_existing_member_to_be_overwritten(self):
        self.remote_tree()
        output = self.root / "downloaded"
        self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        (output / "a.txt").unlink()
        kept = output / "资料" / "中文 文件.txt"
        kept.write_bytes(b"local edits")
        count = len(self.transfer.downloads)
        with self.assertRaisesRegex(UsageError, "内容变化"):
            self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        self.assertEqual(len(self.transfer.downloads), count)
        self.assertFalse((output / "a.txt").exists())
        self.assertEqual(kept.read_bytes(), b"local edits")

    def test_missing_completed_tree_still_rejects_changed_remote_identity(self):
        import shutil
        self.remote_tree()
        output = self.root / "downloaded"
        self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        shutil.rmtree(output)
        next(record for record in self.fixture.items if record["fid"] == "remote-a")["updated_at"] = 456
        count = len(self.transfer.downloads)
        with self.assertRaises(UsageError):
            self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        self.assertEqual(len(self.transfer.downloads), count)
        self.assertFalse(output.exists())

    def test_resume_does_not_adopt_directory_created_after_fixed_tree_preflight(self):
        def stopped(record):
            raise ProcessExit()
        self.transfer.after_upload = stopped
        with self.assertRaises(ProcessExit):
            self.upload()
        _, ledger = self.state()
        root = ledger["directories"][""]["fid"]
        self.fixture.items.append(item("later-foreign-directory", "资料", root, directory=True))
        self.transfer.after_upload = lambda record: None
        count = len(self.fixture.mkdir_calls)
        with self.assertRaises(UsageError):
            self.upload(resume=True)
        self.assertEqual(len(self.fixture.mkdir_calls), count)
        self.assertEqual(len(self.transfer.uploads), 1)

    def test_state_ancestors_private_and_different_sources_same_target_share_lock(self):
        self.upload()
        if sys.platform != "win32":
            for path in (self.root / "state", self.root / "state" / "transfers", self.root / "state" / "transfers" / "trees"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        first = self.recursive._state("upload", "/first/source", "scope", "Name")
        second = self.recursive._state("upload", "/other/source", "scope", "name")
        self.assertEqual((first[1], first[2]), (second[1], second[2]))
        self.assertEqual(self.recursive._state("download", "first-fid", "same-output", "")[2], self.recursive._state("download", "other-fid", "same-output", "")[2])

    def test_download_fixed_tree_rejects_added_file_without_adopting_it(self):
        self.remote_tree()
        output = self.root / "downloaded"
        self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        self.fixture.items.append(item("new-remote", "new.txt", "remote-root", size=1, updated_at=123))
        with self.assertRaises(UsageError):
            self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        self.assertFalse((output / "new.txt").exists())

    def test_resume_saved_scope_checked_before_reading_moved_download_subtree(self):
        self.remote_tree()
        output = self.root / "downloaded"
        self.recursive.download_tree("remote-root", output, resume=True, scope=self.scope)
        next(record for record in self.fixture.items if record["fid"] == "remote-root")["pdir_fid"] = "outside"
        self.fixture.calls.clear()
        with self.assertRaises(UsageError):
            self.recursive.download_tree("remote-root", output, resume=True)
        child_reads = [request for request in self.fixture.calls if request.url.path.endswith("/file/sort") and request.url.params["pdir_fid"] in {"remote-root", "remote-sub", "remote-empty"}]
        self.assertEqual(child_reads, [])

    def test_explicit_download_overwrite_is_applied_after_full_preflight(self):
        self.remote_tree()
        output = self.root / "downloaded"
        output.mkdir()
        (output / "a.txt").write_bytes(b"old")
        self.recursive.download_tree("remote-root", output, overwrite=True, scope=self.scope)
        self.assertEqual((output / "a.txt").read_bytes(), b"one")

    def test_download_known_hash_mismatch_cannot_commit_completion(self):
        self.remote_tree()
        self.fixture.payloads["remote-a"] = b"two"
        output = self.root / "downloaded"
        with self.assertRaises(RemoteError):
            self.recursive.download_tree("remote-root", output, scope=self.scope)
        _, ledger = self.state("download", output)
        self.assertFalse(ledger["completed"])
        self.assertNotIn("a.txt", ledger["files"])
        self.assertEqual(ledger["pending"]["fid"], "remote-a")

    def test_pending_mkdir_resume_preview_does_not_adopt_name_without_fid(self):
        def unknown(name, parent):
            self.fixture.items.append(item("later-root", name, parent, directory=True))
            raise RemoteError("unknown")
        with patch.object(self.recursive.files, "mkdir", side_effect=unknown), self.assertRaises(RemoteError):
            self.upload()
        path, _ = self.state()
        snapshot = path.read_bytes()
        with self.assertRaises(UsageError):
            self.upload(resume=True, dry_run=True)
        self.assertEqual(path.read_bytes(), snapshot)

    def test_resume_new_current_scope_cannot_widen_saved_boundary(self):
        self.upload()
        _, ledger = self.state()
        root_fid = ledger["directories"][""]["fid"]
        next(record for record in self.fixture.items if record["fid"] == root_fid)["pdir_fid"] = "outside"
        from quark_cli.paths import Scope
        count = len(self.transfer.uploads)
        with self.assertRaises(UsageError):
            self.recursive.upload_tree(self.source, "scope", resume=True, scope=Scope(self.recursive.paths, "0"))
        self.assertEqual(len(self.transfer.uploads), count)

    def test_completed_remote_file_same_name_new_fid_not_skipped(self):
        self.upload()
        _, ledger = self.state()
        original = ledger["files"]["a.txt"]["identity"]["fid"]
        next(record for record in self.fixture.items if record["fid"] == original)["fid"] = "same-name-replacement"
        with self.assertRaises(UsageError):
            self.upload(resume=True)
        self.assertEqual(len(self.transfer.uploads), 2)

    def test_missing_hash_readback_mismatch_refuses_completed_skip(self):
        self.upload()
        path, ledger = self.state()
        from quark_cli.state import write_json
        for receipt in ledger["files"].values():
            receipt["identity"].pop("md5", None)
        for record in self.fixture.items:
            record.pop("md5", None)
        fid = ledger["files"]["a.txt"]["identity"]["fid"]
        self.fixture.payloads[fid] = b"two"
        write_json(path, ledger)
        with self.assertRaises(UsageError):
            self.upload(resume=True)
        self.assertEqual(len(self.transfer.uploads), 2)

    def test_state_failure_after_known_mkdir_stops_before_any_upload(self):
        from quark_cli.state import write_json
        def fail_response(path, document):
            if document["pending"] and document["pending"]["fid"]:
                raise LocalIOError("persist failed")
            write_json(path, document)
        with patch("quark_cli.recursive.write_json", side_effect=fail_response), self.assertRaises(LocalIOError):
            self.upload()
        self.assertEqual(len(self.fixture.mkdir_calls), 1)
        self.assertEqual(self.transfer.uploads, [])
        _, ledger = self.state()
        self.assertIsNone(ledger["pending"]["fid"])
        with self.assertRaises(UsageError):
            self.upload(resume=True)

    def test_download_cycle_and_false_parent_fail_before_local_creation(self):
        self.remote_tree()
        output = self.root / "downloaded"
        self.fixture.list_overrides["remote-sub"] = [item("remote-root", "cycle", "remote-sub", directory=True)]
        with self.assertRaises(QuarkError):
            self.recursive.download_tree("remote-root", output, scope=self.scope)
        self.assertFalse(output.exists())
        self.fixture.list_overrides.clear()
        record = next(record for record in self.fixture.items if record["fid"] == "remote-sub")
        self.fixture.info_overrides["remote-sub"] = {**record, "pdir_fid": "child"}
        with self.assertRaises(QuarkError):
            self.recursive.download_tree("remote-root", output, scope=self.scope)
        self.assertFalse(output.exists())

    def test_moved_child_outside_scope_not_traversed_during_download(self):
        self.remote_tree()
        sub = next(record for record in self.fixture.items if record["fid"] == "remote-sub")
        stale = {key: value for key, value in sub.items() if key != "pdir_fid"}
        sub["pdir_fid"] = "outside"
        self.fixture.list_overrides["remote-root"] = [stale]
        self.fixture.calls.clear()
        with self.assertRaises(UsageError):
            self.recursive.download_tree("remote-root", self.root / "downloaded", scope=self.scope)
        self.assertFalse(any(request.url.path.endswith("/file/sort") and request.url.params["pdir_fid"] == "remote-sub" for request in self.fixture.calls))

    def test_recursive_schema_corrupts_without_reset_or_writes(self):
        self.upload()
        path, ledger = self.state()
        ledger["nodes"][1]["path"] = "../escape"
        from quark_cli.state import write_json
        write_json(path, ledger)
        snapshot = path.read_bytes()
        with self.assertRaises(LocalIOError):
            self.upload(resume=True)
        self.assertEqual(path.read_bytes(), snapshot)
        self.assertEqual(len(self.transfer.uploads), 2)

    def test_existing_empty_object_manifest_cannot_reset_unknown_mkdir(self):
        def unknown(name, parent):
            self.fixture.items.append(item("unconfirmed-root", name, parent, directory=True))
            raise RemoteError("response lost")
        with patch.object(self.recursive.files, "mkdir", side_effect=unknown), self.assertRaises(RemoteError):
            self.upload()
        path, original = self.state()
        self.assertEqual((original["pending"]["kind"], original["pending"]["fid"]), ("mkdir", None))
        path.write_text("{}", encoding="utf-8")
        snapshot = path.read_bytes()
        writes = len(self.fixture.writes)
        with self.assertRaises(LocalIOError):
            self.upload(resume=True)
        self.assertEqual(path.read_bytes(), snapshot)
        self.assertEqual(len(self.fixture.writes), writes)
        self.assertEqual(self.transfer.uploads, [])

    def test_invalid_local_output_control_name_is_structured_before_os_lookup(self):
        with self.assertRaises(UsageError):
            self.recursive.download_tree("scope", self.root / "bad\x00name")
        self.assertEqual(self.fixture.writes, [])
        self.assertEqual(self.transfer.downloads, [])

    def test_output_preflight_io_failure_preserves_structured_error(self):
        self.remote_tree()
        output = self.root / "downloaded"
        output.mkdir()
        with patch("quark_cli.recursive.Path.iterdir", side_effect=PermissionError("private")), self.assertRaises(LocalIOError):
            self.recursive.download_tree("remote-root", output, scope=self.scope)
        self.assertEqual(self.transfer.downloads, [])

    def test_low_level_rate_validation_rejects_invalid_values_before_state_or_requests(self):
        calls = len(self.fixture.calls)
        for rate in (0, -1, True, 1.5):
            with self.subTest(rate=rate), self.assertRaises(UsageError):
                self.upload(limit_rate=rate)
        self.assertEqual(len(self.fixture.calls), calls)
        self.assertFalse((self.root / "state").exists())

    def test_download_full_tree_includes_second_directory_page(self):
        self.remote_tree()
        for index in range(55):
            fid, name, payload = f"paged-{index}", f"page-{index:02}.txt", bytes([index])
            self.fixture.items.append(item(fid, name, "remote-root", size=1, updated_at=123, md5=hashlib.md5(payload).hexdigest()))
            self.fixture.payloads[fid] = payload
        output = self.root / "downloaded"
        result = self.recursive.download_tree("remote-root", output, scope=self.scope)
        self.assertEqual(result["files"], 57)
        self.assertEqual((output / "page-54.txt").read_bytes(), bytes([54]))
        pages = [request.url.params["_page"] for request in self.fixture.calls if request.url.path.endswith("/file/sort") and request.url.params["pdir_fid"] == "remote-root"]
        self.assertIn("2", pages)

    def test_zero_byte_file_rejects_whole_upload_tree_before_mkdir_or_state(self):
        (self.source / "empty.txt").write_bytes(b"")
        calls = len(self.fixture.calls)
        with self.assertRaises(UsageError):
            self.upload()
        with self.assertRaises(UsageError):
            self.upload(dry_run=True)
        self.assertEqual(len(self.fixture.calls), calls)
        self.assertEqual(self.fixture.writes, [])
        self.assertFalse((self.root / "state").exists())


if __name__ == "__main__":
    unittest.main()
