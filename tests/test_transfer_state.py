import copy
import hashlib
import io
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from quark_cli.errors import LocalIOError, UsageError
from quark_cli.transfer import _part_size, safe_transfer_name
from quark_cli.transfer_progress import Progress, ProgressEvent, RateLimiter, render_progress
from quark_cli.transfer_state import DownloadLedger, UploadLedger, safe_local_path


def upload_state(ledger):
    return {"version": 1, "direction": "upload", "phase": "prepared", "identity": ledger.identity,
            "local": {"size": 1, "mtime_ns": 1, "device": 1, "inode": 1,
                      "sha256": hashlib.sha256(b"x").hexdigest(), "md5": hashlib.md5(b"x").hexdigest(), "sha1": hashlib.sha1(b"x").hexdigest()},
            "scope_fid": "scope", "upload": {}, "parts": [], "flash": False, "upload_task_id": None,
            "finish_task_id": None, "final_fid": None, "final_identity": None, "callback_warning": False,
            "created_at": 1, "updated_at": 1}


def download_state(ledger):
    return {"version": 1, "direction": "download", "phase": "prepared", "identity": ledger.identity,
            "scope_fid": None, "remote": {"fid": ledger.identity["fid"], "parent": "0", "name": "file", "size": 4},
            "etag": None, "offset": 0, "prefix_sha256": hashlib.sha256(b"").hexdigest(), "payload_sha256": None,
            "created_at": 1, "updated_at": 1}


def transfer_lock_worker(directory, identity, pipe):
    ledger = UploadLedger(identity, directory=Path(directory))
    with ledger.lock:
        pipe.send("locked")
        pipe.recv()


class TransferStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = UploadLedger({"source": str(self.root / "source"), "parent": "parent", "name": "file"}, directory=self.root / "state")

    def test_target_key_and_lock_are_shared_across_local_sources(self):
        other = UploadLedger({"source": str(self.root / "other"), "parent": "parent", "name": "FILE"}, directory=self.root / "state")
        self.assertEqual(self.ledger.key, other.key)
        self.assertEqual(self.ledger.lock.path, other.lock.path)
        self.ledger.persist(upload_state(self.ledger))
        with self.assertRaises(LocalIOError):
            other.load()
        different = UploadLedger({"source": str(self.root / "source"), "parent": "parent", "name": "new-name"}, directory=self.root / "state")
        self.assertNotEqual(self.ledger.key, different.key)

    def test_upload_ledger_read_only_load_does_not_repair_state_permissions(self):
        self.ledger.persist(upload_state(self.ledger))
        before = (self.ledger.path.read_bytes(), self.ledger.path.stat().st_ctime_ns)
        with patch("quark_cli.state.restrict_windows_acl", side_effect=AssertionError("read-only ACL repair")) as acl, \
             patch("quark_cli.state.os.chmod", side_effect=AssertionError("read-only chmod")) as chmod:
            loaded = self.ledger.load(read_only=True)
            loaded["local"]["size"] = 7
            self.assertEqual(self.ledger.data["local"]["size"], 1)
            acl.assert_not_called()
            chmod.assert_not_called()
        self.assertEqual((self.ledger.path.read_bytes(), self.ledger.path.stat().st_ctime_ns), before)
        missing = UploadLedger({**self.ledger.identity, "name": "missing"}, directory=self.root / "absent")
        self.assertIsNone(missing.load(read_only=True))
        self.assertFalse(missing.directory.exists())

    def test_upload_ledger_read_only_load_rejects_unsafe_permissions(self):
        self.ledger.persist(upload_state(self.ledger))
        if sys.platform == "win32":
            response = subprocess.run(["icacls", str(self.ledger.path), "/grant", "*S-1-1-0:F"], capture_output=True, check=False)
            self.assertEqual(response.returncode, 0, "unable to inject Everyone grant")
        else:
            os.chmod(self.ledger.path, 0o644)
        with patch("quark_cli.state.restrict_windows_acl", side_effect=AssertionError("read-only ACL repair")), \
             patch("quark_cli.state.os.chmod", side_effect=AssertionError("read-only chmod")), self.assertRaises(LocalIOError):
            self.ledger.load(read_only=True)

    def test_independent_process_target_lock_release_after_process_death(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=transfer_lock_worker, args=(str(self.ledger.directory), self.ledger.identity, child))
        process.start()
        self.addCleanup(lambda: process.terminate() if process.is_alive() else None)
        self.assertTrue(parent.poll(15), "transfer lock did not become ready")
        self.assertEqual(parent.recv(), "locked")
        other = UploadLedger({**self.ledger.identity, "source": str(self.root / "other")}, directory=self.ledger.directory)
        with self.assertRaises(LocalIOError):
            with other.lock:
                self.fail("different source acquired same destination lock")
        process.terminate()
        process.join(15)
        self.assertFalse(process.is_alive())
        with other.lock:
            pass
        parent.close()
        child.close()

    def test_private_roundtrip_is_deep_copied_and_atomic_failure_keeps_old_receipt(self):
        data = upload_state(self.ledger)
        self.ledger.persist(data)
        before = self.ledger.path.read_bytes()
        data["local"]["size"] = 7
        self.assertEqual(self.ledger.load()["local"]["size"], 1)
        next_data = self.ledger.load()
        next_data["phase"] = "pre_submitting"
        with patch("quark_cli.state.os.replace", side_effect=OSError("sensitive-error")):
            with self.assertRaises(LocalIOError) as caught:
                self.ledger.persist(next_data)
        self.assertNotIn("sensitive-error", str(caught.exception))
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertEqual(self.ledger.data["phase"], "prepared")
        self.assertFalse(list(self.ledger.directory.glob(".upload-*")))

    def test_forbidden_secret_fields_and_malformed_schema_are_rejected(self):
        mutations = [lambda d: d.update(cookie="account-secret"), lambda d: d.update(auth_key="signing-secret"),
                     lambda d: d.update(download_url="https://example.invalid/signed"), lambda d: d.update(version=True),
                     lambda d: d.update(created_at=float("nan")), lambda d: d.update(phase="made-up"),
                     lambda d: d["local"].update(sha256="broken"), lambda d: d["local"].update(size=True),
                     lambda d: d["identity"].update(name="../outside")]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                data = upload_state(self.ledger)
                mutate(data)
                with self.assertRaises(LocalIOError):
                    self.ledger.persist(data)

    def test_upload_auth_only_whitelisted_private_shape_and_raw_etag(self):
        data = upload_state(self.ledger)
        data.update(phase="parts", upload_task_id="task")
        data["upload"] = {"task_id": "task", "upload_id": "upload", "bucket": "ul-zb", "obj_key": "key", "auth_info": "protected",
                          "callback": {"callbackUrl": "https://drive-pc.quark.cn/callback"}, "mime_type": "text/plain", "part_size": 131072, "part_count": 1}
        md5 = data["local"]["md5"]
        data["parts"] = [{"number": 1, "offset": 0, "length": 1, "md5": md5, "etag": md5.upper(), "confirmed": True}]
        self.ledger.persist(data)
        self.assertEqual(self.ledger.load()["parts"][0]["etag"], md5.upper())
        for update in (lambda d: d["upload"].update(auth_key="forbidden"),
                       lambda d: d["upload"]["callback"].update(token="forbidden"),
                       lambda d: d["parts"][0].update(etag="0" * 32),
                       lambda d: d["parts"][0].update(length=2)):
            invalid = copy.deepcopy(data)
            update(invalid)
            with self.assertRaises(LocalIOError):
                self.ledger.persist(invalid)

    def test_completed_upload_receipt_requires_exact_fid_and_target(self):
        data = upload_state(self.ledger)
        data.update(phase="completed", flash=True, final_fid="result", final_identity={"fid": "other", "parent": "parent", "name": "file", "size": 1})
        with self.assertRaises(LocalIOError):
            self.ledger.persist(data)
        data["final_identity"]["fid"] = "result"
        self.ledger.persist(data)
        for value in (True, "", float("inf")):
            bad = copy.deepcopy(data)
            bad["final_identity"]["updated_at"] = value
            with self.assertRaises(LocalIOError):
                self.ledger.persist(bad)

    def test_download_schema_requires_strong_validator_and_full_payload_proof(self):
        ledger = DownloadLedger({"fid": "file", "output": str(self.root / "out")}, directory=self.root / "state")
        data = download_state(ledger)
        for etag in ('W/"weak"', "unquoted", '"with\ncontrol"'):
            invalid = copy.deepcopy(data)
            invalid["etag"] = etag
            with self.assertRaises(LocalIOError):
                ledger.persist(invalid)
        data["etag"] = '""'
        ledger.persist(data)  # RFC permits an empty strong opaque tag.
        data.update(phase="completed", offset=4)
        with self.assertRaises(LocalIOError):
            ledger.persist(data)
        data["payload_sha256"] = data["prefix_sha256"]
        ledger.persist(data)

    def test_default_config_and_transfers_are_current_user_private(self):
        config = self.root / "new-config"
        with patch.dict(os.environ, {"QUARK_CONFIG_DIR": str(config)}):
            ledger = UploadLedger(self.ledger.identity)
            with ledger.lock:
                ledger.persist(upload_state(ledger))
        paths = [config, config / "transfers", ledger.path, ledger.lock.path]
        if sys.platform == "win32":
            script = "$ErrorActionPreference='Stop'; $paths=[Console]::In.ReadToEnd()|ConvertFrom-Json; $u=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; @($paths|ForEach-Object {$a=Get-Acl -LiteralPath $_; $r=@($a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])); ($a.AreAccessRulesProtected -and $r.Count -eq 1 -and $r[0].IdentityReference.Value -eq $u)})|ConvertTo-Json -Compress"
            response = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script], input=json.dumps([str(p) for p in paths]), capture_output=True, text=True, encoding="utf-8", check=True)
            self.assertEqual(json.loads(response.stdout), [True] * len(paths))
        else:
            self.assertEqual([p.stat().st_mode & 0o777 for p in paths], [0o700, 0o700, 0o600, 0o600])

    def test_local_path_rejects_link_before_dotdot_normalization(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symbolic links unavailable to current user")
        with self.assertRaises(LocalIOError):
            safe_local_path(link / ".." / "source")
        with self.assertRaises(LocalIOError):
            safe_local_path(link / "source")

    @unittest.skipUnless(sys.platform == "win32", "Windows junction uses native reparse points")
    def test_windows_junction_is_rejected_without_symlink_privilege(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "junction"
        script = "$ErrorActionPreference='Stop'; $p=[Console]::In.ReadToEnd()|ConvertFrom-Json; New-Item -ItemType Junction -Path $p.link -Target $p.target | Out-Null"
        subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script], input=json.dumps({"link": str(link), "target": str(outside)}), capture_output=True, text=True, encoding="utf-8", check=True)
        try:
            with self.assertRaises(LocalIOError):
                safe_local_path(link / "source")
            with self.assertRaises(LocalIOError):
                safe_local_path(link / ".." / "source")
        finally:
            link.rmdir()

    def test_portable_remote_basename_rejects_windows_devices_and_ads(self):
        for name in ("COM1", "LPT¹.txt", "COM²", "NUL.txt", "trailing.", "trailing ", "file:stream", "../name", "a\\b"):
            with self.subTest(name=name), self.assertRaises(UsageError):
                safe_transfer_name(name)
        self.assertEqual(safe_transfer_name("中文 sample.txt"), "中文 sample.txt")

    def test_local_nul_and_invalid_part_size_types_fail_cleanly(self):
        from quark_cli.errors import RemoteError
        with self.assertRaises(LocalIOError):
            safe_local_path(str(self.root / "file") + "\0")
        for value in (True, 131072.5, "131072.0", -1):
            with self.subTest(value=value), self.assertRaises(RemoteError):
                _part_size({"part_size": value})
        self.assertEqual(_part_size({"part_size": "131072"}), 131072)


class TransferProgressTests(unittest.TestCase):
    def test_shared_limiter_has_small_low_rate_chunks_and_cumulative_budget(self):
        now, sleeps = [0.0], []
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        limiter = RateLimiter(10, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(limiter.chunk_size, 1)
        limiter.consume(5)
        first = now[0]
        limiter.consume(5)  # Reuse the same instance for a second file.
        self.assertAlmostEqual(first, 0.5)
        self.assertAlmostEqual(now[0], 1.0)
        self.assertEqual(limiter.transferred, 10)
        limiter.consume(25)
        self.assertTrue(all(delay <= 1 for delay in sleeps))
        self.assertAlmostEqual(now[0], 3.5)
        for invalid in (0, -1, True, 1.5, "10"):
            with self.subTest(invalid=invalid), self.assertRaises(UsageError):
                RateLimiter(invalid)

    def test_shared_limiter_serializes_concurrent_range_budget(self):
        now = [0.0]
        limiter = RateLimiter(10, clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
        workers = [threading.Thread(target=limiter.consume, args=(5,)) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(limiter.transferred, 20)
        self.assertAlmostEqual(now[0], 2.0)

    def test_progress_throttles_and_distinguishes_sent_from_confirmed(self):
        now, events = [0.0], []
        limiter = RateLimiter(clock=lambda: now[0])
        progress = Progress("upload", "file", 10, limiter, events.append)
        progress.transferred = 10
        progress.emit("transferring")
        now[0] += 0.1
        progress.emit("transferring")
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].transferred, events[0].confirmed), (10, 0))
        progress.confirmed = 10
        progress.emit("part_confirmed", force=True)
        self.assertEqual(len(events), 2)

    def test_recent_speed_tracks_last_five_seconds_and_reports_connection_mode(self):
        now, events = [0.0], []
        limiter = RateLimiter(clock=lambda: now[0])
        progress = Progress("download", "file", 2000, limiter, events.append)
        progress.connections = 4
        for second in range(1, 8):
            now[0] = float(second)
            limiter.consume(500 if second == 7 else 100)
            progress.emit("transferring")
        self.assertEqual(events[-1].rate, 180.0)
        self.assertEqual(events[-1].connections, 4)
        now[0] = 13.0
        progress.emit("transferring")
        self.assertEqual(events[-1].rate, 0.0)

    def test_stderr_renderer_removes_c0_del_c1_controls_and_keeps_unicode(self):
        stream = io.StringIO()
        event = ProgressEvent("download", "transferring", "中文\x1b[31m\r\n\x7f\x85\x9b[2J\u202e", 4, 4, None, 3.0)
        with patch("sys.stderr", stream):
            render_progress(event)
        text = stream.getvalue()
        self.assertIn("中文", text)
        self.assertIn("4/? B", text)
        self.assertIn("sent=4 B confirmed=4/? B", text)
        self.assertEqual(text.count("\n"), 1)
        self.assertFalse(any(c in text for c in ("\x1b", "\r", "\x7f", "\x85", "\x9b", "\u202e")))


if __name__ == "__main__":
    unittest.main()
