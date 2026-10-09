import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quark_cli.backup import Backup, safe_operation_result
from quark_cli.client import QuarkClient
from quark_cli.errors import LocalIOError, RemoteError, UsageError
from quark_cli.paths import Scope
from quark_cli.state import StateLock, read_json, write_json
from quark_cli.transfer_state import UploadLedger

from backup_fixture import BackupDrive, BackupTransfer, ProcessExit
from drive_fixture import item


class BackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "中文 来源"
        self.source.mkdir()
        (self.source / "a.txt").write_bytes(b"old")
        (self.source / "空目录").mkdir()
        self.drive = BackupDrive()
        self.client = QuarkClient("offline-cookie-private", transport=self.drive.transport())
        self.addCleanup(self.client.close)
        self.transfer = BackupTransfer(self.client, self.drive, self.root / "uploads")
        self.backup = Backup(self.client, self.transfer, self.root / "backups")
        self.scope = Scope(self.backup.paths, "scope")

    def run_backup(self, **options):
        return self.backup.run(self.source, "target", scope=self.scope, **options)

    def state(self):
        path, _ = self.backup._state("target")
        return path, read_json(path)

    def seed(self):
        self.run_backup(apply=True)
        self.drive.calls.clear()
        return self.state()[1]["entries"]["a.txt"]["remote"]["fid"]

    def change(self, payload=b"new version"):
        (self.source / "a.txt").write_bytes(payload)

    def stop_at(self, phase, *, after=True):
        original = self.backup._persist

        def persist():
            pending = self.backup.document["pending"]
            if pending and pending["phase"] == phase:
                if after:
                    original()
                raise ProcessExit()
            original()
        return patch.object(self.backup, "_persist", side_effect=persist)

    def test_preview_no_state_no_lock_no_writes(self):
        with patch("quark_cli.backup.StateLock", side_effect=AssertionError("preview lock")), patch("quark_cli.backup.write_json", side_effect=AssertionError("preview persist")):
            result = self.run_backup()
        self.assertEqual((result["result"], result["dry_run"], result["files"], result["directories"]), ("preview", True, 1, 2))
        self.assertFalse((self.root / "backups").exists())
        self.assertFalse((self.root / "uploads").exists())
        self.assertEqual(self.drive.writes, [])
        self.assertEqual(self.transfer.counts["pre"], 0)

    def test_first_second_and_incremental_contents_mapping(self):
        result = self.run_backup(apply=True)
        self.assertEqual((result["uploaded_files"], result["created_directories"]), (1, 1))
        self.assertEqual(self.drive.record("uploaded-1")["pdir_fid"], "target")
        path, state = self.state()
        before = path.read_bytes()
        result = self.run_backup(apply=True)
        self.assertEqual((result["uploaded_files"], result["skipped_files"]), (0, 1))
        self.assertEqual(path.read_bytes(), before)
        (self.source / "空目录" / "中文.txt").write_bytes("公开文本".encode())
        result = self.run_backup(apply=True)
        self.assertEqual(result["uploaded_files"], 1)
        self.assertEqual(set(self.state()[1]["entries"]), {"a.txt", "空目录", "空目录/中文.txt"})
        self.assertEqual(self.transfer.counts["pre"], 2)
        self.assertNotIn("offline-cookie-private", path.read_text())

    def test_changed_file_requires_update_exit_two_before_any_write(self):
        self.seed()
        self.change()
        with self.assertRaises(UsageError) as caught:
            self.run_backup(apply=True)
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertEqual(self.drive.writes, [])
        self.assertEqual(self.transfer.counts["pre"], 1)

    def test_local_deletion_and_remote_extra_retained(self):
        fid = self.seed()
        (self.source / "a.txt").unlink()
        self.drive.add_file("extra", "outside-manifest.txt", b"unmanaged")
        result = self.run_backup(apply=True)
        self.assertEqual(result["uploaded_files"], 0)
        self.assertEqual(self.drive.payloads[fid], b"old")
        self.assertEqual(self.drive.payloads["extra"], b"unmanaged")

    def test_unmanaged_file_or_directory_same_name_no_adoption(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                record = item("collision", "a.txt", "target", directory=directory, size=3)
                self.drive.items.append(record)
                with self.assertRaises(UsageError):
                    self.run_backup(apply=True)
                self.drive.items.remove(record)
        self.assertEqual(self.drive.writes, [])
        self.assertEqual(self.transfer.counts["pre"], 0)

    def test_casefold_unicode_collisions_and_zero_file_prewrite(self):
        self.drive.items.append(item("collision", "A.TXT", "target", size=3))
        with self.assertRaises(UsageError):
            self.run_backup(apply=True)
        self.drive.items.pop()
        (self.source / "zero").touch()
        with self.assertRaises(UsageError):
            self.run_backup(apply=True)
        self.assertEqual(self.drive.writes, [])

    def test_real_local_symlink_or_junction_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.source / "linked"
        if sys.platform == "win32":
            command = "$p=[Console]::In.ReadToEnd()|ConvertFrom-Json; New-Item -ItemType Junction -Path $p.link -Target $p.target | Out-Null"
            completed = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
                                       input=json.dumps({"link": str(link), "target": str(outside)}), text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0)
        else:
            link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(UsageError):
            self.run_backup(apply=True)
        self.assertEqual(self.drive.writes, [])

    def test_existing_empty_corrupt_unknown_schema_and_parent_binding_rejected(self):
        self.seed()
        path, original = self.state()
        broken = [{}, {**original, "version": True}, {**original, "token": "secret"}]
        changed = copy.deepcopy(original)
        changed["entries"]["a.txt"]["remote"]["parent"] = "outside"
        broken.append(changed)
        for state in broken:
            with self.subTest(state=state):
                write_json(path, state)
                with self.assertRaises(LocalIOError):
                    self.run_backup(apply=True)
        self.assertEqual(self.drive.writes, [])

    def test_saved_source_and_root_identity_changes_rejected(self):
        self.seed()
        other = self.root / "other-source"
        other.mkdir()
        (other / "a.txt").write_bytes(b"old")
        with self.assertRaises(UsageError):
            self.backup.run(other, "target", apply=True, scope=self.scope)
        self.drive.record("target")["file_name"] = "changed"
        with self.assertRaises(UsageError):
            self.run_backup(apply=True)
        self.assertEqual(self.drive.writes, [])

    def test_remote_version_or_same_name_replacement_refuses_skip(self):
        fid = self.seed()
        self.drive.record(fid)["updated_at"] += 1
        with self.assertRaises(UsageError):
            self.run_backup(apply=True)
        self.drive.record(fid)["updated_at"] -= 1
        self.drive.record(fid)["fid"] = "replacement"
        with self.assertRaises((UsageError, RemoteError)):
            self.run_backup(apply=True)
        self.assertEqual(self.transfer.counts["pre"], 1)

    def test_exact_parent_change_fails_before_outside_ancestor_reads(self):
        fid = self.seed()
        self.drive.record(fid)["pdir_fid"] = "outside"
        self.drive.calls.clear()
        with self.assertRaises(UsageError):
            self.run_backup(apply=True)
        self.assertFalse(any(r.url.params.get("fid") == "outside" or r.url.params.get("pdir_fid") == "outside" for r in self.drive.calls))

    def test_queued_directory_identity_checked_before_scope_or_children(self):
        (self.source / "空目录" / "nested.txt").write_bytes(b"nested")
        self.seed()
        fid = self.state()[1]["entries"]["空目录"]["remote"]["fid"]
        record = self.drive.record(fid)
        original_record = copy.deepcopy(record)
        original = self.backup._children
        for mutation in ({"pdir_fid": "outside"}, {"file_name": "renamed"}, {"file_type": 1, "size": 1}):
            def changed(directory, boundaries):
                if directory == fid:
                    record.update(mutation)
                    self.drive.calls.clear()
                return original(directory, boundaries)
            with self.subTest(mutation=mutation), patch.object(self.backup, "_children", side_effect=changed), self.assertRaises(UsageError):
                self.run_backup(apply=True)
            self.assertFalse(any(r.url.params.get("fid") == "outside" or r.url.params.get("pdir_fid") in {"outside", fid} for r in self.drive.calls))
            self.assertEqual(self.drive.writes, [])
            record.clear()
            record.update(original_record)

    def test_narrowed_or_missing_current_scope_never_broadens_saved_scope(self):
        self.seed()
        narrow = Scope(self.backup.paths, "target")
        self.drive.record("target")["pdir_fid"] = "outside"
        self.drive.calls.clear()
        with self.assertRaises(UsageError):
            self.backup.run(self.source, "target", apply=True, scope=None)
        self.assertFalse(any(r.url.params.get("pdir_fid") == "target" for r in self.drive.calls))
        self.drive.record("target")["pdir_fid"] = "scope"
        self.assertEqual(self.backup.run(self.source, "target", apply=True, scope=narrow)["skipped_files"], 1)

    def test_missing_version_preview_sha_read_creates_no_state_or_lock(self):
        self.transfer.version = self.transfer.hashes = False
        self.seed()
        path, _ = self.state()
        before = path.read_bytes()
        self.transfer.proofs.clear()
        with patch("quark_cli.backup.StateLock", side_effect=AssertionError("lock")), patch("quark_cli.backup.write_json", side_effect=AssertionError("persist")), patch.object(self.transfer, "download", side_effect=AssertionError("public download creates a ledger")):
            result = self.run_backup()
        self.assertEqual(result["items"][0]["action"], "unchanged")
        self.assertTrue(self.transfer.proofs)
        self.assertEqual(path.read_bytes(), before)

    def test_existing_manifest_preview_does_not_modify_permissions_or_create_state(self):
        self.seed()
        path, _ = self.state()
        before = path.read_bytes()
        paths = set(self.root.rglob("*"))
        with patch("quark_cli.state.restrict_windows_acl", side_effect=AssertionError("preview ACL mutation")), patch("quark_cli.state.os.chmod", side_effect=AssertionError("preview chmod mutation")), patch("quark_cli.backup.StateLock", side_effect=AssertionError("preview lock")), patch("quark_cli.backup.write_json", side_effect=AssertionError("preview write")):
            result = self.run_backup()
        self.assertEqual(result["result"], "preview")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(self.root.rglob("*")), paths)
        self.assertEqual(self.drive.writes, [])

    def test_pending_preview_reads_original_ledger_without_local_mutation(self):
        self.transfer.after_pre = lambda _: (_ for _ in ()).throw(ProcessExit())
        with self.assertRaises(ProcessExit):
            self.run_backup(apply=True)
        path, _ = self.state()
        ledger = UploadLedger({"source": str((self.source / "a.txt").resolve()), "parent": "target", "name": "a.txt"}, directory=self.transfer.state_dir)
        before = {path: path.read_bytes(), ledger.path: ledger.path.read_bytes()}
        paths = set(self.root.rglob("*"))
        counts = self.transfer.counts.copy()
        with patch("quark_cli.state.restrict_windows_acl", side_effect=AssertionError("preview ACL mutation")), patch("quark_cli.state.os.chmod", side_effect=AssertionError("preview chmod mutation")), patch("quark_cli.backup.StateLock", side_effect=AssertionError("preview lock")), patch("quark_cli.backup.write_json", side_effect=AssertionError("preview write")), patch.object(self.transfer, "upload", side_effect=AssertionError("preview upload")):
            result = self.run_backup()
        self.assertEqual(result["pending"]["phase"], "upload_submitting")
        self.assertEqual({item: item.read_bytes() for item in before}, before)
        self.assertEqual(set(self.root.rglob("*")), paths)
        self.assertEqual(self.transfer.counts, counts)
        self.assertEqual(self.drive.writes, [])

    def test_reliable_hash_conflict_and_no_version_same_size_changed_data(self):
        fid = self.seed()
        self.drive.record(fid)["sha1"] = "0" * 40
        with self.assertRaises(UsageError):
            self.run_backup()
        self.drive.record(fid)["sha1"] = hashlib.sha1(b"old").hexdigest()
        path, state = self.state()
        self.drive.record(fid).pop("updated_at")
        state["entries"]["a.txt"]["remote"].pop("updated_at")
        write_json(path, state)
        self.drive.payloads[fid] = b"bad"
        with self.assertRaises(UsageError):
            self.run_backup()

    def test_read_proof_rejects_range_compression_wrong_length_and_identity_change(self):
        self.transfer.version = False
        fid = self.seed()
        for headers, status in [({}, 206), ({"Content-Range": "bytes 0-2/3"}, 200), ({"Content-Encoding": "gzip"}, 200), ({"Content-Length": "2"}, 200)]:
            self.transfer.response_headers, self.transfer.response_status = headers, status
            with self.subTest(headers=headers, status=status), self.assertRaises(RemoteError):
                self.run_backup()
        self.transfer.response_headers, self.transfer.response_status = {}, 200
        self.transfer.before_proof = lambda _: self.drive.record(fid).update({"file_name": "changed.txt"})
        with self.assertRaises(UsageError):
            self.run_backup()

    def test_content_address_then_parent_change_blocks_stream_and_outside_reads(self):
        self.transfer.version = False
        fid = self.seed()
        self.transfer.streams.clear()
        def changed(_):
            self.drive.record(fid)["pdir_fid"] = "outside"
            self.drive.calls.clear()
        self.transfer.before_proof = changed
        with self.assertRaises(UsageError):
            self.run_backup()
        self.assertEqual(self.transfer.streams, [])
        self.assertFalse(any(r.url.params.get("fid") == "outside" or r.url.params.get("pdir_fid") == "outside" for r in self.drive.calls))
        self.assertEqual(self.drive.writes, [])

    def test_mkdir_unknown_missing_fid_is_durable_and_never_name_adopted(self):
        (self.source / "a.txt").unlink()
        self.drive.mkdir_mode = "no-fid"
        with self.assertRaises(RemoteError):
            self.run_backup(apply=True)
        _, state = self.state()
        self.assertEqual((state["pending"]["phase"], state["pending"]["fid"]), ("mkdir_submitting", None))
        self.drive.mkdir_mode = None
        with self.assertRaises(RemoteError):
            self.run_backup(apply=True)
        self.assertEqual(len(self.drive.mkdir_calls), 1)

    def test_mkdir_receipt_is_fsynced_and_recovery_does_not_repeat(self):
        (self.source / "a.txt").unlink()
        with self.stop_at("mkdir_confirmed"), self.assertRaises(ProcessExit):
            self.run_backup(apply=True)
        self.assertEqual(self.state()[1]["pending"]["fid"], "directory-1")
        self.run_backup(apply=True)
        self.assertEqual(len(self.drive.mkdir_calls), 1)

    def test_original_upload_task_after_process_exit_no_new_pre(self):
        self.transfer.after_pre = lambda _: (_ for _ in ()).throw(ProcessExit())
        with self.assertRaises(ProcessExit):
            self.run_backup(apply=True)
        self.transfer.after_pre = lambda _: None
        self.run_backup(apply=True)
        self.assertEqual(self.transfer.counts, {"pre": 1, "part": 1, "merge": 1, "finish": 1})
        self.assertEqual(self.transfer.uploads[0][3], "task-1")

    def test_completed_receipt_without_version_full_sha_no_upload_replay(self):
        self.transfer.version = self.transfer.hashes = False
        self.transfer.after_upload = lambda _: (_ for _ in ()).throw(ProcessExit())
        with self.assertRaises(ProcessExit):
            self.run_backup(apply=True)
        before = self.transfer.counts.copy()
        self.transfer.after_upload = lambda _: None
        self.run_backup(apply=True)
        self.assertEqual(self.transfer.counts, before)
        self.assertEqual(self.state()[1]["entries"]["a.txt"]["remote"]["fid"], "uploaded-1")
        self.assertGreater(len(self.transfer.proofs), 0)

    def test_completed_receipt_same_payload_different_fid_not_adopted(self):
        self.transfer.after_upload = lambda _: (_ for _ in ()).throw(ProcessExit())
        with self.assertRaises(ProcessExit):
            self.run_backup(apply=True)
        self.drive.record("uploaded-1")["fid"] = "same-payload-replacement"
        before = self.transfer.counts.copy()
        with self.assertRaises(RemoteError):
            self.run_backup(apply=True)
        self.assertEqual(self.transfer.counts, before)

    def test_upload_unknown_pre_submission_stops_without_replay(self):
        self.transfer.after_pre = lambda _: (_ for _ in ()).throw(ProcessExit())
        with self.assertRaises(ProcessExit):
            self.run_backup(apply=True)
        pending = self.state()[1]["pending"]
        ledger = UploadLedger({"source": str((self.source / "a.txt").resolve()), "parent": "target", "name": "a.txt"}, directory=self.transfer.state_dir)
        data = ledger.load()
        data.update({"phase": "pre_submitting", "upload": {}, "parts": [], "upload_task_id": None, "final_fid": None})
        ledger.persist(data)
        before = self.transfer.counts.copy()
        with patch.object(self.transfer, "upload", side_effect=RemoteError("unknown; no retry")), self.assertRaises(RemoteError):
            self.run_backup(apply=True)
        self.assertEqual(self.transfer.counts, before)
        self.assertEqual(self.state()[1]["pending"], pending)

    def test_missing_original_upload_ledger_does_not_start_new_task(self):
        with self.stop_at("upload_submitting"), self.assertRaises(ProcessExit):
            self.run_backup(apply=True)
        with self.assertRaises(RemoteError):
            self.run_backup(apply=True)
        self.assertEqual(self.transfer.counts["pre"], 0)

    def test_local_fingerprint_changed_during_recovery_refuses_upload(self):
        self.transfer.after_pre = lambda _: (_ for _ in ()).throw(ProcessExit())
        with self.assertRaises(ProcessExit):
            self.run_backup(apply=True)
        self.change(b"new")
        with self.assertRaises(UsageError):
            self.run_backup(apply=True)
        self.assertEqual(self.transfer.counts["pre"], 1)

    def test_update_preserves_old_sha_and_confirms_new_fid(self):
        old = self.seed()
        self.change()
        result = self.run_backup(apply=True, update=True)
        state = self.state()[1]
        current, archived = state["entries"]["a.txt"], state["archives"][0]
        self.assertEqual(result["updated_files"], 1)
        self.assertEqual(archived["remote"]["fid"], old)
        self.assertEqual(self.drive.payloads[old], b"old")
        self.assertEqual(current["remote"]["name"], "a.txt")
        self.assertEqual(self.drive.payloads[current["remote"]["fid"]], b"new version")
        self.assertEqual(len(self.drive.rename_calls), 2)
        self.assertFalse(any(r.url.path.endswith("/file/delete") for r in self.drive.calls))
        self.assertEqual(result["archives"][0]["fid"], old)

    def test_update_crash_at_each_confirmed_phase_resumes_without_duplicate_writes(self):
        for phase in ("upload_confirmed", "archive_confirmed", "promote_confirmed"):
            with self.subTest(phase=phase):
                # Independent target/source/manifest for each state-machine edge.
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                folder = Path(temporary.name)
                source = folder / "source"
                source.mkdir()
                (source / "a.txt").write_bytes(b"old")
                drive = BackupDrive()
                client = QuarkClient("offline-cookie-private", transport=drive.transport())
                self.addCleanup(client.close)
                transfer = BackupTransfer(client, drive, folder / "uploads")
                backup = Backup(client, transfer, folder / "state")
                scope = Scope(backup.paths, "scope")
                backup.run(source, "target", apply=True, scope=scope)
                (source / "a.txt").write_bytes(b"new")
                original = backup._persist
                def stop():
                    original()
                    if backup.document["pending"] and backup.document["pending"]["phase"] == phase:
                        raise ProcessExit()
                with patch.object(backup, "_persist", side_effect=stop), self.assertRaises(ProcessExit):
                    backup.run(source, "target", apply=True, update=True, scope=scope)
                pre = transfer.counts["pre"]
                backup.run(source, "target", apply=True, update=True, scope=scope)
                self.assertEqual(transfer.counts["pre"], pre)
                self.assertEqual(len(drive.rename_calls), 2)

    def test_lost_rename_response_confirmed_by_fid_without_retry(self):
        self.seed()
        self.change()
        self.drive.rename_mode = "after"
        self.run_backup(apply=True, update=True)
        self.assertEqual(len(self.drive.rename_calls), 2)

    def test_archive_unknown_still_old_name_never_resent(self):
        self.seed()
        self.change()
        self.drive.rename_mode = "before"
        with self.assertRaises(RemoteError):
            self.run_backup(apply=True, update=True)
        self.drive.rename_mode = None
        with self.assertRaises(RemoteError):
            self.run_backup(apply=True, update=True)
        self.assertEqual(len(self.drive.rename_calls), 1)
        self.assertEqual(self.state()[1]["pending"]["phase"], "archive_submitting")

    def test_successful_rename_receipt_write_failure_read_only_recovers(self):
        self.seed()
        self.change()
        original = self.backup._persist
        def failing():
            pending = self.backup.document["pending"]
            if pending and pending["phase"] == "archive_confirmed":
                raise LocalIOError("intentional durable receipt failure")
            original()
        with patch.object(self.backup, "_persist", side_effect=failing), self.assertRaises(LocalIOError) as caught:
            self.run_backup(apply=True, update=True)
        self.assertEqual(self.state()[1]["pending"]["phase"], "archive_submitting")
        self.assertEqual(caught.exception.backup_evidence["pending"]["phase"], "archive_submitting")
        self.run_backup(apply=True, update=True)
        self.assertEqual(len(self.drive.rename_calls), 2)
        self.assertEqual(self.transfer.counts["pre"], 2)

    def test_promote_receipt_write_failure_does_not_enter_temp_upload_again(self):
        self.seed()
        self.change()
        original = self.backup._persist
        def failing():
            if self.backup.document["pending"] and self.backup.document["pending"]["phase"] == "promote_confirmed":
                raise LocalIOError("receipt failure")
            original()
        with patch.object(self.backup, "_persist", side_effect=failing), self.assertRaises(LocalIOError) as caught:
            self.run_backup(apply=True, update=True)
        self.assertEqual(caught.exception.backup_evidence["pending"]["phase"], "promote_submitting")
        with patch.object(self.transfer, "upload", side_effect=AssertionError("old temp ledger")):
            self.run_backup(apply=True, update=True)
        self.assertEqual(len(self.drive.rename_calls), 2)

    def test_third_party_formal_name_after_archive_preserves_all_objects(self):
        old = self.seed()
        self.change()
        with self.stop_at("archive_confirmed"), self.assertRaises(ProcessExit):
            self.run_backup(apply=True, update=True)
        pending = self.state()[1]["pending"]
        self.drive.add_file("third-party", "a.txt", b"third party")
        with self.assertRaises(UsageError):
            self.run_backup(apply=True, update=True)
        self.assertEqual(len(self.drive.rename_calls), 1)
        self.assertEqual(self.drive.payloads[old], b"old")
        self.assertEqual(self.drive.payloads[pending["fid"]], b"new version")
        self.assertEqual(self.drive.payloads["third-party"], b"third party")

    def test_persist_intent_failure_prevents_all_remote_writes(self):
        with patch("quark_cli.backup.write_json", side_effect=LocalIOError("fsync failure")), self.assertRaises(LocalIOError):
            self.run_backup(apply=True)
        self.assertEqual(self.drive.writes, [])
        self.assertEqual(self.transfer.counts["pre"], 0)

    def test_upload_receipt_failure_prevents_mkdir_and_is_recoverable(self):
        original = self.backup._persist
        def failing():
            pending = self.backup.document["pending"]
            if pending and pending["fid"] is not None:
                raise LocalIOError("receipt write fails")
            original()
        with patch.object(self.backup, "_persist", side_effect=failing), self.assertRaises(LocalIOError) as caught:
            self.run_backup(apply=True)
        evidence = caught.exception.backup_evidence["pending"]
        self.assertEqual(evidence["phase"], "upload_submitting")
        self.assertNotIn("fid", evidence)
        self.assertEqual(evidence["unpersisted_fid"], "uploaded-1")
        self.assertIsNone(self.state()[1]["pending"]["fid"])
        self.assertEqual(self.drive.mkdir_calls, [])
        before = self.transfer.counts.copy()
        self.run_backup(apply=True)
        self.assertEqual(self.transfer.counts, before)

    def test_first_mkdir_receipt_failure_reports_unpersisted_exact_fid(self):
        (self.source / "a.txt").unlink()
        original = write_json
        def failing(path, document):
            pending = document["pending"]
            if pending and pending["fid"] is not None:
                raise LocalIOError("first directory receipt failure")
            original(path, document)
        with patch("quark_cli.backup.write_json", side_effect=failing), self.assertRaises(LocalIOError) as caught:
            self.run_backup(apply=True)
        evidence = caught.exception.backup_evidence["pending"]
        self.assertEqual(evidence["phase"], "mkdir_submitting")
        self.assertNotIn("fid", evidence)
        self.assertEqual(evidence["unpersisted_fid"], "directory-1")
        self.assertIsNone(self.state()[1]["pending"]["fid"])
        with self.assertRaises(RemoteError):
            self.run_backup(apply=True)
        self.assertEqual(len(self.drive.mkdir_calls), 1)

    def assert_rename_final_listing_race(self, mutation, *, parent_changed=False):
        old = self.seed()
        self.change()
        original = self.backup._vacant
        def vacant(parent, name, boundaries, allowed=None):
            pending = self.backup.document["pending"]
            if pending and name == pending["archive_name"]:
                def changed(listed_parent):
                    if listed_parent == parent:
                        self.drive.record(parent if parent_changed else old).update(mutation)
                        self.drive.after_listing = lambda _: None
                        self.drive.calls.clear()
                self.drive.after_listing = changed
            return original(parent, name, boundaries, allowed)
        with patch.object(self.backup, "_vacant", side_effect=vacant), self.assertRaises(UsageError):
            self.run_backup(apply=True, update=True)
        self.assertEqual(self.drive.rename_calls, [])
        self.assertEqual(self.state()[1]["pending"]["phase"], "archive_submitting")
        self.assertFalse(any(r.url.params.get("fid") == "outside" or r.url.params.get("pdir_fid") == "outside" for r in self.drive.calls))
        self.assertEqual(self.transfer.counts["pre"], 2)

    def test_rename_last_parent_listing_then_move_blocks_write(self):
        self.assert_rename_final_listing_race({"pdir_fid": "outside"})

    def test_rename_last_parent_listing_then_version_change_blocks_write(self):
        self.assert_rename_final_listing_race({"updated_at": 9999})

    def test_rename_last_parent_listing_then_parent_move_blocks_write(self):
        self.assert_rename_final_listing_race({"pdir_fid": "outside"}, parent_changed=True)

    def assert_parent_final_listing_race(self, kind, mutation):
        if kind == "mkdir":
            (self.source / "a.txt").unlink()
        original = self.backup._vacant
        def vacant(parent, name, boundaries, allowed=None):
            pending = self.backup.document["pending"]
            if pending and pending["kind"] == kind:
                def changed(listed_parent):
                    if listed_parent == parent:
                        self.drive.record(parent).update(mutation)
                        self.drive.after_listing = lambda _: None
                        self.drive.calls.clear()
                self.drive.after_listing = changed
            return original(parent, name, boundaries, allowed)
        with patch.object(self.backup, "_vacant", side_effect=vacant), patch.object(self.transfer, "upload", side_effect=AssertionError("parent identity must block upload")), self.assertRaises(UsageError):
            self.run_backup(apply=True)
        self.assertEqual(self.drive.writes, [])
        self.assertEqual(self.transfer.counts["pre"], 0)
        self.assertEqual(self.state()[1]["pending"]["phase"], kind + "_submitting")
        self.assertFalse(any(r.url.params.get("fid") == "outside" or r.url.params.get("pdir_fid") == "outside" for r in self.drive.calls))

    def test_mkdir_last_parent_listing_then_move_blocks_write(self):
        self.assert_parent_final_listing_race("mkdir", {"pdir_fid": "outside"})

    def test_upload_last_parent_listing_then_parent_rename_blocks_upload_call(self):
        self.assert_parent_final_listing_race("upload", {"file_name": "moved-target"})

    def test_upload_last_parent_listing_then_parent_move_blocks_outside_reads(self):
        self.assert_parent_final_listing_race("upload", {"pdir_fid": "outside"})

    def fail_final_receipt(self, path):
        original = write_json
        def failing(destination, document):
            if document["pending"] is None and path in document["entries"]:
                raise LocalIOError("final receipt write failure")
            original(destination, document)
        return patch("quark_cli.backup.write_json", side_effect=failing)

    def assert_durable_pending_evidence(self, error, phase):
        document = self.state()[1]
        pending = document["pending"]
        evidence = error.backup_evidence
        self.assertEqual(self.backup.document, document)
        self.assertEqual(evidence["result"], "unknown")
        self.assertEqual(evidence["pending"]["phase"], phase)
        self.assertEqual(evidence["pending"]["path"], pending["path"])
        self.assertEqual(evidence["pending"]["fid"], pending["old"]["remote"]["fid"] if pending["old"] else pending["fid"])
        self.assertEqual(evidence["archives"], [])
        return pending

    def test_final_upload_receipt_failure_reports_durable_fid_and_recovers_once(self):
        with self.fail_final_receipt("a.txt"), self.assertRaises(LocalIOError) as caught:
            self.run_backup(apply=True)
        pending = self.assert_durable_pending_evidence(caught.exception, "upload_confirmed")
        self.assertEqual(pending["fid"], "uploaded-1")
        self.assertEqual(self.drive.mkdir_calls, [])
        before = self.transfer.counts.copy()
        self.run_backup(apply=True)
        self.assertEqual(self.transfer.counts, before)
        self.assertEqual(self.state()[1]["entries"]["a.txt"]["remote"]["fid"], pending["fid"])

    def test_final_mkdir_receipt_failure_reports_durable_fid_and_recovers_once(self):
        (self.source / "a.txt").unlink()
        with self.fail_final_receipt("空目录"), self.assertRaises(LocalIOError) as caught:
            self.run_backup(apply=True)
        pending = self.assert_durable_pending_evidence(caught.exception, "mkdir_confirmed")
        self.assertEqual(pending["fid"], "directory-1")
        self.run_backup(apply=True)
        self.assertEqual(len(self.drive.mkdir_calls), 1)

    def test_final_update_receipt_failure_preserves_durable_pending_old_and_new_fids(self):
        old = self.seed()
        self.change()
        with self.fail_final_receipt("a.txt"), self.assertRaises(LocalIOError) as caught:
            self.run_backup(apply=True, update=True)
        pending = self.assert_durable_pending_evidence(caught.exception, "promote_confirmed")
        evidence = caught.exception.backup_evidence["pending"]
        self.assertEqual((evidence["fid"], evidence["new_fid"]), (old, pending["fid"]))
        self.assertEqual(evidence["archive_name"], self.drive.record(old)["file_name"])
        self.assertEqual(self.drive.payloads[old], b"old")
        self.assertEqual(self.drive.payloads[pending["fid"]], b"new version")
        before = self.transfer.counts.copy()
        with patch.object(self.transfer, "upload", side_effect=AssertionError("confirmed upload must not replay")):
            self.run_backup(apply=True, update=True)
        self.assertEqual(self.transfer.counts, before)
        self.assertEqual(len(self.drive.rename_calls), 2)
        self.assertEqual(len(self.state()[1]["archives"]), 1)

    def test_new_upload_content_proof_failure_blocks_both_update_renames(self):
        old = self.seed()
        self.change()
        def changed(fid):
            if fid != old:
                self.drive.payloads[fid] = b"bad version"
        self.transfer.before_proof = changed
        with self.assertRaises(UsageError) as caught:
            self.run_backup(apply=True, update=True)
        pending = self.state()[1]["pending"]
        self.assertEqual(pending["phase"], "upload_submitting")
        self.assertIsNotNone(pending["fid"])
        self.assertIsNone(pending["new"])
        self.assertEqual(self.drive.rename_calls, [])
        self.assertEqual(self.drive.record(old)["file_name"], "a.txt")
        self.assertEqual(caught.exception.backup_evidence["pending"]["new_fid"], pending["fid"])

    def test_real_process_lock_same_target_ignores_different_source(self):
        path, lock_name = self.backup._state("target")
        script = "from pathlib import Path;from quark_cli.state import StateLock;import sys\nwith StateLock(Path(sys.argv[1]),sys.argv[2]):\n print('locked',flush=True);sys.stdin.read(1)"
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
        process = subprocess.Popen([sys.executable, "-u", "-c", script, str(path.parent), lock_name], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            other = self.root / "other"
            other.mkdir()
            with self.assertRaises(LocalIOError):
                self.backup.run(other, "target", apply=True, scope=self.scope)
        finally:
            process.communicate("x", timeout=10)
        self.assertEqual(self.drive.writes, [])

    def test_lock_then_reread_detects_intervening_manifest(self):
        self.seed()
        real_lock = StateLock
        class InterveningLock:
            def __init__(inner, *args):
                inner.lock = real_lock(*args)
            def __enter__(inner):
                inner.lock.__enter__()
                path, state = self.state()
                state["source"] = str(self.root / "competing-source")
                write_json(path, state)
                return inner
            def __exit__(inner, *args):
                return inner.lock.__exit__(*args)
        with patch("quark_cli.backup.StateLock", InterveningLock), self.assertRaises(UsageError):
            self.run_backup(apply=True)
        self.assertEqual(self.drive.writes, [])

    def test_safe_error_evidence_and_shared_limiter_progress(self):
        events = []
        (self.source / "b.txt").write_bytes(b"another")
        self.run_backup(apply=True, progress=events.append)
        self.assertIs(self.transfer.limiters[0], self.transfer.limiters[1])
        self.assertEqual(events[-1].name, "b.txt")
        self.assertEqual(events[-1].total, 10)
        self.assertEqual(events[-1].confirmed, 10)
        self.change()
        self.drive.rename_mode = "before"
        with self.assertRaises(RemoteError) as caught:
            self.run_backup(apply=True, update=True)
        serialized = json.dumps(caught.exception.backup_evidence, ensure_ascii=False) + str(caught.exception)
        for secret in ("raw-cookie-secret", "offline-cookie-private", "auth-private-canary", "object-private-canary"):
            self.assertNotIn(secret, serialized)
        self.assertIn("temporary_name", serialized)
        self.assertEqual(safe_operation_result({"result": "completed", "token": "canary", "items": [{"path": "safe", "auth_info": "canary"}]}),
                         {"result": "completed", "items": [{"path": "safe"}]})

    def test_safe_result_rejects_unhashable_enum_fields_without_type_error(self):
        for bad in ([], {}, ["completed"], {"result": "completed"}):
            value = {"result": bad, "items": [{"kind": bad, "action": bad, "result": bad, "path": "safe"}],
                     "pending": {"kind": bad, "phase": bad, "path": "safe"}}
            with self.subTest(bad=bad):
                self.assertEqual(safe_operation_result(value), {"items": [{"path": "safe"}], "pending": {"path": "safe"}})

    def test_private_state_permissions_and_no_account_cookie_in_manifest(self):
        self.seed()
        path, _ = self.state()
        self.assertNotIn("auth-private-canary", path.read_text())
        if sys.platform != "win32":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
