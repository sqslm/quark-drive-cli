"""``quark-potplayer.exe``: the launcher the AngelScript extensions find and call.

The extensions compute the launcher path themselves::

    folder = HostGetScriptFolder()                  # <PotPlayer>\\Extension\\Media\\UrlList
    launcher = folder + "\\\\..\\\\..\\\\quark-potplayer.exe"     # <PotPlayer>\\Extension
    result = HostExecuteProgram(launcher, "--ensure-service")

The bridge itself is installed under ``<PotPlayer>\\Plugin\\QuarkPotPlayer\\app``,
so this small executable sits next to the player's ``Extension`` folder and forwards
``argv`` to the bridge, re-passing our own standard handles so the bridge's stdout
(the ``--ensure-service`` JSON line) reaches PotPlayer unchanged; the exit code is
passed through. Passing the handles explicitly is required: this launcher is a
windowed build with no console, and Windows would otherwise hand a console child its
own invisible console instead of PotPlayer's pipe (``inherit_stdio``).

It also answers one local action itself, before any bridge work::

    result = HostExecuteProgram(launcher, "--player-pid")

``--player-pid`` prints a single ``{"player_pid": <int>}`` line (``0`` when the
player cannot be verified) and never starts the bridge, reads a cookie or
touches the network; see :func:`resolve_player_pid`.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

COMPONENT_NAME = "QuarkPotPlayer.exe"
PATH_FILE_NAME = "quark-potplayer.path"
ENV_OVERRIDE = "QUARK_POTPLAYER_COMPONENT"

#: Local action the extension calls to learn its own player's PID; it never
#: reaches the bridge (no cookie, no network, no service start).
PID_ACTION = "--player-pid"

SUPPORTED = sys.platform == "win32"

#: Window classes a PotPlayer instance owns; a candidate without one is refused,
#: so a non-player parent can never be reported by title or heuristic.
_POTPLAYER_CLASSES = ("PotPlayer", "PotPlayer64")
#: Bound on how far up the (owned) ancestry chain we look for the player.
_MAX_ANCESTRY = 4
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_IMAGE_PATH_BUFFER = 32768


def _invalid_handle() -> int:
    return (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1


if SUPPORTED:
    from ctypes import wintypes as _w

    _WNDENUMPROC = ctypes.WINFUNCTYPE(_w.BOOL, _w.HWND, _w.LPARAM)

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", _w.DWORD),
            ("cntUsage", _w.DWORD),
            ("th32ProcessID", _w.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", _w.DWORD),
            ("cntThreads", _w.DWORD),
            ("th32ParentProcessID", _w.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", _w.DWORD),
            ("szExeFile", _w.WCHAR * 260),
        ]


def launcher_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def component_path(launcher_dir: Path | None = None, env: dict[str, str] | None = None) -> Path | None:
    """Resolve the installed bridge: explicit override, recorded path, then default."""
    source = os.environ if env is None else env
    directory = launcher_directory() if launcher_dir is None else Path(launcher_dir)

    override = source.get(ENV_OVERRIDE, "").strip()
    if override and Path(override).is_file():
        return Path(override)

    recorded = directory / PATH_FILE_NAME
    try:
        candidate = Path(recorded.read_text(encoding="utf-8").strip())
    except OSError:
        candidate = None
    if candidate is not None and candidate.is_file():
        return candidate

    base = source.get("LOCALAPPDATA", "").strip()
    if base:
        default = Path(base) / "QuarkPotPlayer" / "app" / COMPONENT_NAME
        if default.is_file():
            return default
    return None


def inherit_stdio() -> dict[str, object]:
    """Reuse our own standard handles when spawning the bridge.

    A windowed launcher has no console. Windows gives a console child its own
    (invisible) console unless ``STARTF_USESTDHANDLES`` is set, which would
    swallow the ``--ensure-service`` JSON. Passing the streams we were handed
    keeps the bridge on PotPlayer's pipe.
    """
    streams: dict[str, object] = {}
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None:
            streams[name] = stream
    return streams


def _toolhelp_parent(pid: int) -> int | None:
    """Creator PID of ``pid`` from a Toolhelp snapshot, or ``None``.

    Used only to climb past the ``--onefile`` bootloader process the launcher is
    packaged with; it never validates the player itself.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = _w.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [_w.DWORD, _w.DWORD]
    kernel32.Process32FirstW.argtypes = [_w.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [_w.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [_w.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or int(snapshot) == _invalid_handle():
        return None
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    try:
        alive = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while alive:
            if int(entry.th32ProcessID) == pid:
                return int(entry.th32ParentProcessID)
            alive = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return None


def _process_image(pid: int) -> str | None:
    """Full image path of ``pid``, or ``None`` when it cannot be read."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = _w.HANDLE
    kernel32.OpenProcess.argtypes = [_w.DWORD, _w.BOOL, _w.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [_w.HANDLE, _w.DWORD, _w.LPWSTR, ctypes.POINTER(_w.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = _w.BOOL
    kernel32.CloseHandle.argtypes = [_w.HANDLE]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = _w.DWORD(_IMAGE_PATH_BUFFER)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value or None
    finally:
        kernel32.CloseHandle(handle)


def _owns_player_window(pid: int) -> bool:
    """True when ``pid`` owns a top-level ``PotPlayer``/``PotPlayer64`` window.

    Only this desktop's windows are enumerated and the owner is read from the
    window itself (``GetWindowThreadProcessId``), so neither the title nor any
    other player's process can satisfy the check.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetClassNameW.argtypes = [_w.HWND, _w.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [_w.HWND, ctypes.POINTER(_w.DWORD)]
    user32.GetWindowThreadProcessId.restype = _w.DWORD
    user32.EnumWindows.argtypes = [_WNDENUMPROC, _w.LPARAM]
    user32.EnumWindows.restype = _w.BOOL
    found = False

    def callback(hwnd, _lparam):
        nonlocal found
        name = ctypes.create_unicode_buffer(64)
        if user32.GetClassNameW(hwnd, name, 64) and name.value in _POTPLAYER_CLASSES:
            owner = _w.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if int(owner.value) == pid:
                found = True
                return False
        return True

    user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return found


def resolve_player_pid() -> int:
    """PID of the PotPlayer that spawned us, or ``0`` when unverifiable.

    The extension calls ``quark-potplayer.exe --player-pid``; this process is a
    child of the player. A ``--onefile`` build inserts a bootloader between the
    player and this interpreter, so an ancestor whose image is exactly our own
    executable is skipped. The first ancestor that then owns a real PotPlayer
    window is the answer; a shell, an unrelated parent or an unreadable process
    yields ``0`` — there is no fallback to another player.
    """
    if not SUPPORTED:
        return 0
    own = os.path.normcase(os.path.abspath(sys.executable))
    pid = os.getppid()
    seen = {os.getpid()}
    for _ in range(_MAX_ANCESTRY):
        if pid <= 0 or pid in seen:
            return 0
        seen.add(pid)
        image = _process_image(pid)
        if not image:
            return 0
        if os.path.normcase(os.path.abspath(image)) == own:
            parent = _toolhelp_parent(pid)
            if parent is None:
                return 0
            pid = parent
            continue
        return pid if _owns_player_window(pid) else 0
    return 0


def _stdout():
    if sys.stdout is not None:
        return sys.stdout
    try:
        return os.fdopen(1, "w", encoding="utf-8", errors="replace", newline="\n", buffering=1)
    except OSError:
        return None


def emit_player_pid(pid: int) -> None:
    """Write the single ``{"player_pid": <int>}`` line the extension parses."""
    stream = _stdout()
    if stream is None:
        return
    stream.write(json.dumps({"player_pid": int(pid)}) + "\n")
    stream.flush()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    if '--uninstall' in args[1:] or '--uninstall-player' in args[1:] or (getattr(sys, 'frozen', False) and Path(sys.executable).name.lower() == 'uninstall.exe'):
        from uninstall_entry import main as uninstall_main
        return uninstall_main(args[1:])
    if PID_ACTION in args[1:]:
        emit_player_pid(resolve_player_pid())
        return 0
    component = component_path()
    if component is None:
        print("未找到夸克网盘桥接组件，请重新运行插件安装程序。", file=sys.stderr)
        return 3
    return subprocess.run([str(component), *args[1:]], **inherit_stdio()).returncode


if __name__ == "__main__":
    raise SystemExit(main())
