"""Current-user private state, durable atomic JSON, and kernel file locks."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from .errors import LocalIOError


def config_directory() -> Path:
    override = os.environ.get("QUARK_CONFIG_DIR")
    if override is not None:
        if not override or not Path(override).is_absolute():
            raise LocalIOError("QUARK_CONFIG_DIR 必须是非空绝对路径")
        return Path(override)
    base = os.environ.get("APPDATA" if sys.platform == "win32" else "XDG_CONFIG_HOME", "").strip()
    if not base:
        try:
            base = str(Path.home() / ("AppData/Roaming" if sys.platform == "win32" else ".config"))
        except (OSError, RuntimeError):
            raise LocalIOError("无法确定当前用户配置目录") from None
    if not Path(base).expanduser().is_absolute():
        raise LocalIOError("用户配置目录必须是绝对路径")
    return Path(base).expanduser() / "quark-drive-cli"


def _not_link(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.stat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise LocalIOError("私有状态路径不能包含符号链接或重解析点")


def _acl_failure(message: str, exception: BaseException) -> str:
    """One safe line with the native error code: never a path, SID or credential."""
    if not isinstance(exception, OSError):
        return message
    code = exception.winerror or exception.errno
    return f"{message}（Win32 错误 {code}）" if code else message


def restrict_windows_acl(path: Path, *, directory: bool, set_owner: bool = False) -> None:
    """Replace the DACL, including explicit grants, with one current-SID ACE.

    By default only the DACL is written. The owner implicitly holds
    READ_CONTROL and WRITE_DAC, but WRITE_OWNER comes from the DACL alone, so
    requesting OWNER_SECURITY_INFORMATION needs rights an inheriting folder such
    as a plain save directory does not grant and fails with ERROR_ACCESS_DENIED.
    Ownership is set only for newly created files or explicitly validated
    upgrade recovery. Existing Modify-only files keep the DACL-only repair.

    The DACL written here is protected: the P flag of the SDDL sets
    SE_DACL_PROTECTED on the descriptor, so the object stops inheriting ACEs
    and later reads see exactly the single ACE placed here.
    """
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    api.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    api.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    api.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    token = wintypes.HANDLE()
    sid_text, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        if not api.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise OSError(ctypes.get_last_error(), "OpenProcessToken")
        needed = wintypes.DWORD()
        api.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not api.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not api.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW")
        current_sid = ctypes.wstring_at(sid_text)
        inheritance = "OICI" if directory else ""
        sddl = (f"O:{current_sid}" if set_owner else "") + f"D:P(A;{inheritance};FA;;;{current_sid})"
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise OSError(ctypes.get_last_error(), "ConvertStringSecurityDescriptorToSecurityDescriptorW")
        # A fresh file may belong to Administrators under an elevated token.
        # Publish it as the actual user, matching strict read-only validation.
        flags = 0x80000004 | (1 if set_owner else 0)  # DACL | PROTECTED_DACL [| OWNER]
        if not api.SetFileSecurityW(str(path), flags, descriptor):
            raise OSError(ctypes.get_last_error(), "SetFileSecurityW")
    except (OSError, ValueError) as exception:
        raise LocalIOError(_acl_failure("无法设置仅当前用户可访问的 Windows ACL", exception)) from None
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        if sid_text.value:
            kernel.LocalFree(sid_text)
        if token.value:
            kernel.CloseHandle(token)


def validate_windows_acl(path: Path, *, owner_only: bool = False, allow_admin_owner: bool = False) -> None:
    """Require a current-user owner/DACL; optional admin-owner upgrade check."""
    import ctypes
    from ctypes import wintypes

    class AclSize(ctypes.Structure):
        _fields_ = [("count", wintypes.DWORD), ("used", wintypes.DWORD), ("free", wintypes.DWORD)]

    class AllowedAce(ctypes.Structure):
        _fields_ = [("kind", wintypes.BYTE), ("flags", wintypes.BYTE), ("size", wintypes.WORD),
                    ("mask", wintypes.DWORD), ("sid", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    api.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    api.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
    api.GetNamedSecurityInfoW.restype = wintypes.DWORD
    api.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    api.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    api.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    api.IsValidSid.argtypes = [ctypes.c_void_p]
    api.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    api.CreateWellKnownSid.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    token = wintypes.HANDLE()
    descriptor, owner, dacl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        if not api.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise OSError()
        needed = wintypes.DWORD()
        api.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise OSError()
        buffer = ctypes.create_string_buffer(needed.value)
        if not api.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise OSError()
        current_sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if api.GetNamedSecurityInfoW(str(path), 1, 0x00000005, ctypes.byref(owner), None,
                                    ctypes.byref(dacl), None, ctypes.byref(descriptor)):
            raise OSError()
        if not owner.value or not api.IsValidSid(owner):
            raise OSError()
        if not api.EqualSid(owner, current_sid):
            # This exception is only for repairing an old plugin runtime,
            # after its parent was validated. The DACL checks still apply.
            admin_sid = ctypes.create_string_buffer(68)  # SECURITY_MAX_SID_SIZE
            sid_size = wintypes.DWORD(len(admin_sid))
            if (not allow_admin_owner or owner_only
                    or not api.CreateWellKnownSid(26, None, admin_sid, ctypes.byref(sid_size))
                    or not api.EqualSid(owner, admin_sid)):
                raise OSError()
        if owner_only:
            return
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if (not dacl.value
                or not api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
                or not control.value & 0x1000):  # SE_DACL_PROTECTED
            raise OSError()
        size = AclSize()
        if not api.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2) or not size.count:
            raise OSError()
        for index in range(size.count):
            pointer = ctypes.c_void_p()
            if not api.GetAce(dacl, index, ctypes.byref(pointer)) or not pointer.value:
                raise OSError()
            ace = ctypes.cast(pointer, ctypes.POINTER(AllowedAce)).contents
            sid = pointer.value + AllowedAce.sid.offset
            if (ace.kind != 0 or ace.size < ctypes.sizeof(AllowedAce) or ace.flags & 0x08
                    or not ace.mask or not api.IsValidSid(sid) or not api.EqualSid(sid, current_sid)):
                raise OSError()
    except (OSError, ValueError):
        raise LocalIOError("无法读取本地配置文件，请确认当前用户拥有读写权限") from None
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        if token.value:
            kernel.CloseHandle(token)


def repair_windows_private_owner(path: Path) -> None:
    """Recover an old admin-owned runtime only inside a current-user private directory."""
    if sys.platform != 'win32' or not path.exists():
        return
    _not_link(path)
    if not path.is_file():
        raise LocalIOError('服务状态对象类型无效')
    try:
        validate_windows_acl(path)
    except LocalIOError:
        validate_windows_acl(path.parent)
        validate_windows_acl(path, allow_admin_owner=True)
        restrict_windows_acl(path, directory=False, set_owner=True)


def private_path(path: Path, *, directory: bool = False, read_only: bool = False) -> None:
    try:
        _not_link(path)
        info = path.stat()
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise LocalIOError("私有状态对象类型无效")
        if sys.platform == "win32":
            if read_only:
                validate_windows_acl(path)
            else:
                restrict_windows_acl(path, directory=directory)
        else:
            if info.st_uid != os.getuid():
                raise LocalIOError("私有状态不属于当前用户")
            expected_mode = 0o700 if directory else 0o600
            if read_only:
                if stat.S_IMODE(info.st_mode) != expected_mode:
                    raise LocalIOError("私有状态权限不安全，已停止只读检查")
            else:
                os.chmod(path, expected_mode)
    except OSError:
        raise LocalIOError("无法保护本地私有状态") from None


def secure_directory(path: Path) -> None:
    if not path.is_absolute():
        raise LocalIOError("私有状态目录必须是绝对路径")
    try:
        _not_link(path)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_path(path, directory=True)
    except OSError:
        raise LocalIOError("无法创建私有状态目录") from None


def read_json(path: Path, *, default: dict[str, Any] | None = None, read_only: bool = False) -> dict[str, Any]:
    descriptor = None
    try:
        _not_link(path)
        if not path.exists():
            if default is not None:
                return default
            raise LocalIOError("必要状态文件缺失，已停止")
        if sys.platform == "win32" and not read_only:
            # Old elevated versions also created admin-owned request guard and
            # account JSON, not just runtime.json. Recover only that known case
            # with a private current-user parent and DACL; reject foreign state.
            try:
                validate_windows_acl(path, owner_only=True)
            except LocalIOError:
                repair_windows_private_owner(path)
        private_path(path, read_only=read_only)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        stream = os.fdopen(descriptor, "r", encoding="utf-8")
        descriptor = None
        with stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (OSError, UnicodeError, ValueError):
        raise LocalIOError("状态文件无法读取或格式损坏，已停止") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def write_json(path: Path, data: dict[str, Any]) -> None:
    secure_directory(path.parent)
    _not_link(path)
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        temporary = Path(name)
        if sys.platform == "win32":
            restrict_windows_acl(temporary, directory=False, set_owner=True)
        else:
            private_path(temporary)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        if sys.platform != "win32":
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except (OSError, UnicodeError, ValueError):
        raise LocalIOError("私有状态原子写入失败；未继续远端操作") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class StateLock(AbstractContextManager):
    """Nonblocking OS lock; leave its pathname intact after unlocking."""

    def __init__(self, directory: Path, name: str = "jobs.lock") -> None:
        self.directory, self.path = directory, directory / name
        self.stream = None

    def __enter__(self):
        secure_directory(self.directory)
        descriptor = None
        try:
            _not_link(self.path)
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            self.stream = os.fdopen(descriptor, "r+b", buffering=0)
            descriptor = None
            private_path(self.path)
            if sys.platform == "win32":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.stream.write(b"\0")
                    os.fsync(self.stream.fileno())
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self
        except OSError:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            raise LocalIOError("任务状态已被另一进程锁定，或锁无法取得") from None
        except BaseException:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            raise

    def __exit__(self, *args):
        if self.stream is not None:
            self.stream.close()  # Closing releases the kernel lock.
            self.stream = None
        return False
