import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quark_cli import auth, state
from quark_cli.errors import LocalIOError


def lock_worker(directory, pipe):
    with state.StateLock(Path(directory)):
        pipe.send("locked")
        pipe.recv()


class StateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "private"
        state.secure_directory(self.root)

    def test_config_override_is_absolute_and_shared_with_auth_without_changing_default(self):
        with patch.dict(os.environ, {"QUARK_CONFIG_DIR": str(self.root)}):
            self.assertEqual(auth.credential_path(), self.root / "cookie")
        for value in ("", "relative"):
            with self.subTest(value=value), patch.dict(os.environ, {"QUARK_CONFIG_DIR": value}), self.assertRaises(LocalIOError):
                state.config_directory()
        environment = {"USERPROFILE" if sys.platform == "win32" else "HOME": str(Path.home())}
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(auth.credential_path(), Path.home() / ("AppData/Roaming/quark-drive-cli/cookie" if sys.platform == "win32" else ".config/quark-drive-cli/cookie"))

    def test_atomic_json_failure_preserves_old_bytes_and_no_temp(self):
        path = self.root / "data.json"
        state.write_json(path, {"version": 1})
        before = path.read_bytes()
        with patch("quark_cli.state.os.replace", side_effect=OSError("private-secret")), self.assertRaises(LocalIOError):
            state.write_json(path, {"version": 2})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".data.json-*")), [])

    def test_read_only_json_checks_existing_permissions_without_repair_or_creation(self):
        path = self.root / "data.json"
        state.write_json(path, {"version": 1})
        before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns)
        missing = self.root / "absent" / "data.json"
        with patch("quark_cli.state.restrict_windows_acl", side_effect=AssertionError("read-only ACL repair")) as acl, \
             patch("quark_cli.state.os.chmod", side_effect=AssertionError("read-only chmod")) as chmod:
            self.assertEqual(state.read_json(path, read_only=True), {"version": 1})
            self.assertEqual(state.read_json(missing, default={}, read_only=True), {})
            with self.assertRaises(LocalIOError):
                state.read_json(self.root, read_only=True)
            acl.assert_not_called()
            chmod.assert_not_called()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns), before)
        self.assertFalse(missing.parent.exists())

    def test_read_only_json_rejects_unsafe_native_permissions_without_repair(self):
        path = self.root / "data.json"
        state.write_json(path, {"version": 1})
        if sys.platform == "win32":
            response = subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:F"], capture_output=True, check=False)
            self.assertEqual(response.returncode, 0, "unable to inject Everyone grant")
        else:
            os.chmod(path, 0o644)
        before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns)
        with patch("quark_cli.state.restrict_windows_acl", side_effect=AssertionError("read-only ACL repair")) as acl, \
             patch("quark_cli.state.os.chmod", side_effect=AssertionError("read-only chmod")) as chmod:
            with self.assertRaises(LocalIOError):
                state.read_json(path, read_only=True)
            acl.assert_not_called()
            chmod.assert_not_called()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns), before)
        # The existing default still repairs legacy current-owner permissions.
        self.assertEqual(state.read_json(path), {"version": 1})
        self.assertEqual(state.read_json(path, read_only=True), {"version": 1})

    def test_read_only_json_rejects_untrusted_owner_links_and_unprotected_dacl(self):
        path = self.root / "data.json"
        state.write_json(path, {"version": 1})
        if sys.platform == "win32":
            response = subprocess.run(["icacls", str(path), "/inheritance:e"], capture_output=True, check=False)
            self.assertEqual(response.returncode, 0, "unable to enable DACL inheritance")
            with self.assertRaises(LocalIOError):
                state.read_json(path, read_only=True)
        else:
            with patch("quark_cli.state.os.getuid", return_value=os.getuid() + 1), self.assertRaises(LocalIOError):
                state.read_json(path, read_only=True)
            link = self.root / "link.json"
            link.symlink_to(path)
            with self.assertRaises(LocalIOError):
                state.read_json(link, read_only=True)

    def test_existing_json_untrusted_owner_is_rejected_before_permission_repair(self):
        path = self.root / "data.json"
        state.write_json(path, {"version": 1})
        before = path.read_bytes()
        if sys.platform == "win32":
            import ctypes
            native_dll = ctypes.WinDLL
            def foreign_owner(name, *arguments, **options):
                library = native_dll(name, *arguments, **options)
                if name == "advapi32":
                    # Keep native descriptor/token reads, report a different owner SID.
                    library.EqualSid = lambda first, second: 0
                return library
            ownership = patch("ctypes.WinDLL", side_effect=foreign_owner)
        else:
            ownership = patch("quark_cli.state.os.getuid", return_value=os.getuid() + 1)
        with ownership, patch("quark_cli.state.restrict_windows_acl") as acl, patch("quark_cli.state.os.chmod") as chmod:
            for read_only in (False, True):
                with self.assertRaises(LocalIOError):
                    state.read_json(path, read_only=read_only)
            acl.assert_not_called()
            chmod.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_fsync_precedes_replace_and_linux_syncs_parent_directory(self):
        events = []
        replace, fsync = os.replace, os.fsync
        def sync(fd):
            events.append("sync")
            fsync(fd)
        def publish(source, target):
            events.append("replace")
            replace(source, target)
        with patch("quark_cli.state.os.fsync", side_effect=sync), patch("quark_cli.state.os.replace", side_effect=publish):
            state.write_json(self.root / "data.json", {"version": 1})
        self.assertEqual(events, ["sync", "replace"] + (["sync"] if sys.platform != "win32" else []))

    def test_real_independent_process_lock_competition_and_release_after_process_death(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=lock_worker, args=(str(self.root), child))
        process.start()
        self.addCleanup(lambda: process.terminate() if process.is_alive() else None)
        self.assertTrue(parent.poll(15), "child did not acquire kernel lock")
        self.assertEqual(parent.recv(), "locked")
        lock_path = self.root / "jobs.lock"
        inode = lock_path.stat().st_ino
        with self.assertRaises(LocalIOError):
            with state.StateLock(self.root):
                self.fail("overlapping process acquired the lock")
        process.terminate()
        process.join(15)
        self.assertFalse(process.is_alive())
        with state.StateLock(self.root):
            self.assertEqual(lock_path.stat().st_ino, inode)
        self.assertTrue(lock_path.exists())
        parent.close()
        child.close()

    def test_fdopen_failure_closes_raw_descriptor_for_lock_json_and_saved_cookie(self):
        path = self.root / "data.json"
        state.write_json(path, {"version": 1})
        cookie = self.root / "cookie"
        cookie.write_text("sid=offline", encoding="utf-8")
        operations = (lambda: state.StateLock(self.root).__enter__(), lambda: state.read_json(path),
                      lambda: auth._read_text_file(cookie, saved_credential=True))
        for operation in operations:
            captured = []
            original_open = os.open
            def open_file(*args, **options):
                descriptor = original_open(*args, **options)
                captured.append(descriptor)
                return descriptor
            with self.subTest(operation=operation), patch("quark_cli.state.os.open", side_effect=open_file), patch("quark_cli.state.os.fdopen", side_effect=OSError("private-secret")):
                with self.assertRaises(LocalIOError):
                    operation()
            self.assertEqual(len(captured), 1)
            with self.assertRaises(OSError):
                os.fstat(captured[0])
        self.assertEqual(cookie.read_text(encoding="utf-8"), "sid=offline")

    def test_native_private_permissions_remove_old_explicit_grants_or_reject_untrusted_owner_and_links(self):
        path = self.root / "data.json"
        state.write_json(path, {"version": 1})
        if sys.platform == "win32":
            def powershell(script):
                response = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", "$ErrorActionPreference='Stop'; " + script],
                                          input=json.dumps(str(path)), capture_output=True, text=True, encoding="utf-8", check=True)
                return json.loads(response.stdout)
            grant = subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:F"], capture_output=True, check=False)
            self.assertEqual(grant.returncode, 0, "unable to inject an explicit Everyone grant")
            injected = powershell("$p=[Console]::In.ReadToEnd()|ConvertFrom-Json; $b=Get-Acl -LiteralPath $p; @{count=@($b.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])).Count}|ConvertTo-Json -Compress")
            self.assertGreater(injected["count"], 1)
            self.assertEqual(state.read_json(path), {"version": 1})
            actual = powershell("$p=[Console]::In.ReadToEnd()|ConvertFrom-Json; $a=Get-Acl -LiteralPath $p; $u=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; $rules=@($a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])); @{count=$rules.Count; protected=$a.AreAccessRulesProtected; current=($rules[0].IdentityReference.Value -eq $u); allow=($rules[0].AccessControlType -eq 'Allow')}|ConvertTo-Json -Compress")
            self.assertEqual(actual, {"count": 1, "protected": True, "current": True, "allow": True})
        else:
            self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch("quark_cli.state.os.getuid", return_value=os.getuid() + 1), self.assertRaises(LocalIOError):
                state.read_json(path)
            link = self.root / "link.json"
            link.symlink_to(path)
            with self.assertRaises(LocalIOError):
                state.read_json(link)
            with self.assertRaises(LocalIOError):
                state.write_json(link, {})


if __name__ == "__main__":
    unittest.main()
