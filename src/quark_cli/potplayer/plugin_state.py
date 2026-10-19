"""The bridge's own private directory, credential, runtime file, and lock.

The CLI Cookie file is never read, written, or removed here: the bridge keeps an
independent credential so a plugin logout cannot disturb ``quark``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..auth import load_saved_cookie, save_cookie
from ..auth import logout as logout_credential
from ..errors import LocalIOError
from ..state import StateLock, config_directory, read_json, write_json

CREDENTIAL_NAME = "cookie"
RUNTIME_NAME = "runtime.json"
SERVICE_LOCK_NAME = "service.lock"


def plugin_directory() -> Path:
    return config_directory() / "potplayer"


def credential_path() -> Path:
    return plugin_directory() / CREDENTIAL_NAME


def runtime_path() -> Path:
    return plugin_directory() / RUNTIME_NAME


def load_cookie() -> str | None:
    """Return the bridge's saved Cookie, or ``None`` when it has none."""
    return load_saved_cookie(credential_path())


def store_cookie(cookie: str) -> None:
    save_cookie(cookie, credential_path())


def clear_cookie() -> bool:
    path = credential_path()
    try:
        if not path.exists():
            return False
    except OSError:
        return False
    return logout_credential(path)


def read_runtime() -> dict[str, Any] | None:
    """Read the endpoint record; unreadable or tampered records count as absent."""
    path = runtime_path()
    try:
        if not path.exists():
            return None
        data = read_json(path, read_only=True)
    except (LocalIOError, OSError):
        return None
    return data


def repair_runtime_owner() -> None:
    """Recover the private runtime left by an elevated pre-0.12.8 bridge.

    Never adopt a different user's file or relax read-only runtime validation.
    Only the Administrators default owner with our user's private DACL qualifies.
    """
    from ..state import repair_windows_private_owner
    repair_windows_private_owner(runtime_path())


def write_runtime(entry: dict[str, Any]) -> None:
    write_json(runtime_path(), entry)


def clear_runtime(instance: str) -> None:
    """Remove the endpoint record only when it still describes ``instance``."""
    current = read_runtime()
    if current is None or current.get("instance") != instance:
        return
    try:
        runtime_path().unlink()
    except OSError:
        pass


def service_lock() -> StateLock:
    return StateLock(plugin_directory(), SERVICE_LOCK_NAME)


def acquire_service_lock() -> StateLock | None:
    """Take the single-instance lock, or return ``None`` when another process holds it."""
    lock = service_lock()
    try:
        lock.__enter__()
    except LocalIOError:
        return None
    return lock
