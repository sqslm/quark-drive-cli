"""PotPlayer icon and foreground presentation for explicitly opened windows."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path
import sys

ICON_NAME = 'MediaPlayParse - Quark.ico'


@lru_cache(maxsize=1)
def _user32():
    api = ctypes.WinDLL('user32', use_last_error=True)
    for name, arguments, result in (
        ('GetAncestor', [wintypes.HWND, wintypes.UINT], wintypes.HWND),
        ('GetForegroundWindow', [], wintypes.HWND),
        ('GetWindowThreadProcessId', [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        ('IsWindowVisible', [wintypes.HWND], wintypes.BOOL),
        ('IsIconic', [wintypes.HWND], wintypes.BOOL),
        ('ShowWindowAsync', [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        ('SetWindowPos', [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
        ('BringWindowToTop', [wintypes.HWND], wintypes.BOOL),
        ('SetForegroundWindow', [wintypes.HWND], wintypes.BOOL),
    ):
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


def _foreground(handle):
    if not handle:
        return False
    api = _user32()
    api.ShowWindowAsync(handle, 9 if api.IsIconic(handle) else 5)
    if api.SetForegroundWindow(handle):
        return True
    # Never couple our input queue to PotPlayer's. All operations address only
    # the explicitly opened owned window; don't wait for its UI thread.
    flags = 0x0001 | 0x0002 | 0x0010 | 0x4000  # NOSIZE|NOMOVE|NOACTIVATE|ASYNC
    api.SetWindowPos(handle, -1, 0, 0, 0, 0, flags)
    api.SetWindowPos(handle, -2, 0, 0, 0, 0, flags)
    return bool(api.SetForegroundWindow(handle))


def present_window(window):
    """Restore, raise and focus a Tk window without leaving it always-on-top."""
    from tkinter import TclError
    try:
        window.deiconify()
        window.lift()
        window.update_idletasks()
        if sys.platform == 'win32':
            handle = _user32().GetAncestor(window.winfo_id(), 2)
            _foreground(handle)
        window.focus_force()
    except TclError:
        pass  # The user can close the window before its idle callback runs.


def configure_window(window, *, themed=True):
    if themed:
        from .ui_theme import apply_theme
        apply_theme(window)
    icon = Path(__file__).with_name(ICON_NAME)
    if not icon.is_file():
        icon = Path(__file__).resolve().parents[3] / 'potplayer' / ICON_NAME
    if sys.platform == 'win32' and icon.is_file():
        window.iconbitmap(default=str(icon))
        window.iconbitmap(str(icon))
    window.after_idle(lambda: present_window(window))
    if sys.platform == 'win32' and themed:
        window.after_idle(lambda: _dark_titlebar(window))


def _dark_titlebar(window):
    from tkinter import TclError
    try:
        handle = _user32().GetAncestor(window.winfo_id(), 2)
        enabled = wintypes.BOOL(True)
        api = ctypes.WinDLL('dwmapi')
        api.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        # Windows 10 20H1+; earlier systems retain their native title bar.
        api.DwmSetWindowAttribute(handle, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
    except TclError:
        pass


def foreground_process(player_pid):
    """Raise the existing visible window when its menu entry is clicked again."""
    if sys.platform != 'win32' or type(player_pid) is not int or player_pid <= 0:
        return False
    api = _user32()
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(handle, _):
        owner = wintypes.DWORD()
        api.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == player_pid and api.IsWindowVisible(handle):
            found.append(handle)
            return False
        return True

    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    api.EnumWindows.restype = wintypes.BOOL
    api.EnumWindows(visit, 0)
    return _foreground(found[0]) if found else False
