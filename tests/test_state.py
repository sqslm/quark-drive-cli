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


def powershell(script, payload=None):
    response = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", "$ErrorActionPreference='Stop'; " + script],
                              input=json.dumps(payload) if payload is not None else None,
                              capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(response.stdout)


def legacy_owner_acl_request(target, sid):
    """Replay the pre-fix owner plus protected DACL request; returns the Win32 error, 0 on success."""
    import ctypes

    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    api.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    try:
        sddl = f"O:{sid}D:P(A;;FA;;;{sid})"
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            return ctypes.get_last_error()
        if api.SetFileSecurityW(str(target), 0x80000005, descriptor):  # OWNER | DACL | PROTECTED_DACL
            return 0
        return ctypes.get_last_error()
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)


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

    @unittest.skipUnless(sys.platform == "win32", "Windows default owner semantics")
    def test_new_json_and_cookie_set_current_user_owner_before_publication(self):
        # Elevated Windows tokens default to Administrators ownership. Verify
        # the native write requests OWNER as well as a private DACL before
        # either atomic file becomes visible to a read-only service client.
        import ctypes
        native_dll = ctypes.WinDLL
        writes = []

        def library(name, *args, **kwargs):
            dll = native_dll(name, *args, **kwargs)
            if name == "advapi32":
                set_security = dll.SetFileSecurityW
                def record(path, flags, descriptor):
                    writes.append((Path(path).name, flags))
                    return set_security(path, flags, descriptor)
                dll.SetFileSecurityW = record
            return dll

        path, cookie = self.root / "runtime.json", self.root / "cookie"
        with patch("ctypes.WinDLL", side_effect=library):
            state.write_json(path, {"version": 1})
            auth.save_cookie("sid=offline", cookie)
        temporary_writes = [(name, flags) for name, flags in writes if name.startswith((".runtime.json-", ".cookie-"))]
        self.assertEqual(len(temporary_writes), 2)
        for name, flags in temporary_writes:
            self.assertTrue(flags & 1, name + " did not explicitly set its owner")
        self.assertEqual(state.read_json(path, read_only=True), {"version": 1})
        state.validate_windows_acl(cookie)

    @unittest.skipUnless(sys.platform == "win32", "Windows owner and DACL semantics")
    def test_admin_recovery_exception_still_rejects_other_owners_and_extra_grants(self):
        import ctypes
        from ctypes import wintypes
        native_dll = ctypes.WinDLL
        api = native_dll('advapi32', use_last_error=True)
        api.CreateWellKnownSid.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        fake_owner = ctypes.create_string_buffer(68)
        size = wintypes.DWORD(len(fake_owner))
        self.assertTrue(api.CreateWellKnownSid(26, None, fake_owner, ctypes.byref(size)))

        def library(name, *args, **kwargs):
            dll = native_dll(name, *args, **kwargs)
            if name == 'advapi32':
                get_security = dll.GetNamedSecurityInfoW
                def elevated_owner(path, kind, flags, owner, *rest):
                    result = get_security(path, kind, flags, owner, *rest)
                    if not result:
                        ctypes.cast(owner, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(fake_owner)
                    return result
                dll.GetNamedSecurityInfoW = elevated_owner
            return dll

        path = self.root / 'runtime.json'
        state.write_json(path, {'version': 1})
        with patch('ctypes.WinDLL', side_effect=library):
            with self.assertRaises(LocalIOError):
                state.validate_windows_acl(path)
            state.validate_windows_acl(path, allow_admin_owner=True)
            with self.assertRaises(LocalIOError):
                state.validate_windows_acl(path, allow_admin_owner=True, owner_only=True)
            # A non-admin foreign owner is never eligible.
            size.value = len(fake_owner)
            self.assertTrue(api.CreateWellKnownSid(1, None, fake_owner, ctypes.byref(size)))  # Everyone
            with self.assertRaises(LocalIOError):
                state.validate_windows_acl(path, allow_admin_owner=True)
            size.value = len(fake_owner)
            self.assertTrue(api.CreateWellKnownSid(26, None, fake_owner, ctypes.byref(size)))
            grant = subprocess.run(['icacls', str(path), '/grant', '*S-1-1-0:F'], capture_output=True)
            self.assertEqual(grant.returncode, 0)
            with self.assertRaises(LocalIOError):
                state.validate_windows_acl(path, allow_admin_owner=True)

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

    def test_acl_repair_needs_only_write_dac_on_modify_granted_targets(self):
        # A DACL granting the current user nothing but Modify carries WRITE_OWNER
        # neither explicitly nor implicitly for the object's owner, so the pre-fix
        # request that also included OWNER_SECURITY_INFORMATION failed on such
        # targets with ERROR_ACCESS_DENIED, while a DACL-only write does not.
        if sys.platform != "win32":
            self.skipTest("Windows DACL semantics")
        sid = powershell("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value | ConvertTo-Json -Compress")
        path = self.root / "data.json"
        path.write_text("{}")
        directory = self.root / "parts"
        directory.mkdir()
        for target in (path, directory):
            grant = subprocess.run(["icacls", str(target), "/inheritance:r", "/grant:r", f"*{sid}:(M)"], capture_output=True, check=False)
            self.assertEqual(grant.returncode, 0, "unable to inject a Modify-only current-user DACL")
            legacy_error = legacy_owner_acl_request(target, sid)
            if legacy_error == 0:
                self.skipTest("token can take ownership, so the legacy denial cannot be shown here")
            self.assertEqual(legacy_error, 5, "Modify alone grants no WRITE_OWNER")
            for _ in range(2):  # a second pass must not need rights the first one consumed
                state.private_path(target, directory=target.is_dir())
                state.validate_windows_acl(target)
            self.assertEqual(legacy_owner_acl_request(target, sid), 0, "repaired DACL must grant WRITE_OWNER again")
            state.validate_windows_acl(target)
        self.assertEqual(state.read_json(path), {})

    def test_acl_failure_reports_native_code_without_paths_or_sids(self):
        if sys.platform != "win32":
            self.skipTest("Windows DACL semantics")
        missing = self.root / "absent.json"
        with self.assertRaises(LocalIOError) as caught:
            state.restrict_windows_acl(missing, directory=False)
        message = str(caught.exception)
        self.assertIn("Win32 错误", message)
        self.assertNotIn(str(missing), message)
        self.assertNotIn(str(self.root), message)

    def test_native_private_permissions_remove_old_explicit_grants_or_reject_untrusted_owner_and_links(self):
        path = self.root / "data.json"
        state.write_json(path, {"version": 1})
        if sys.platform == "win32":
            grant = subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:F"], capture_output=True, check=False)
            self.assertEqual(grant.returncode, 0, "unable to inject an explicit Everyone grant")
            injected = powershell("$p=[Console]::In.ReadToEnd()|ConvertFrom-Json; $b=Get-Acl -LiteralPath $p; @{count=@($b.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])).Count}|ConvertTo-Json -Compress", str(path))
            self.assertGreater(injected["count"], 1)
            self.assertEqual(state.read_json(path), {"version": 1})
            actual = powershell("$p=[Console]::In.ReadToEnd()|ConvertFrom-Json; $a=Get-Acl -LiteralPath $p; $u=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; $rules=@($a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])); @{count=$rules.Count; protected=$a.AreAccessRulesProtected; current=($rules[0].IdentityReference.Value -eq $u); allow=($rules[0].AccessControlType -eq 'Allow')}|ConvertTo-Json -Compress", str(path))
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
