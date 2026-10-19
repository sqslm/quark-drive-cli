"""Installed lightweight uninstaller; carries no component ZIP or installer UI."""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import install_core as core


def _run(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--uninstall-player', action='store_true')
    parser.add_argument('--install-dir', type=Path, default=Path(sys.executable).resolve().parent)
    parser.add_argument('--clear-credentials', action='store_true')
    parser.add_argument('--silent', action='store_true')
    parser.add_argument('--confirmed', action='store_true')
    parser.add_argument('--no-registry', action='store_true')
    args = parser.parse_args(argv)
    manifest = core.read_manifest(args.install_dir) or {}
    linked = manifest.get('player_uninstall') or {}
    if (not args.no_registry and linked.get('hive') == 'HKLM' and sys.platform == 'win32'
            and getattr(sys, 'frozen', False)):
        import ctypes
        shell = ctypes.WinDLL('shell32')
        if not shell.IsUserAnAdmin():
            shell.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                           ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
            shell.ShellExecuteW.restype = ctypes.c_void_p
            parameters = list(sys.argv[1:] if argv is None else argv)
            result = shell.ShellExecuteW(None, 'runas', sys.executable,
                                         subprocess.list2cmdline(parameters), None, 1)
            return 0 if result and result > 32 else 4
    if core.needs_temp_relaunch(args.install_dir):
        extra = ['--silent'] if args.silent else []
        if args.uninstall_player:
            extra.append('--uninstall-player')
        if args.confirmed:
            extra.append('--confirmed')
        if args.clear_credentials:
            extra.append('--clear-credentials')
        if args.no_registry:
            extra.append('--no-registry')
        copied = core.relaunch_uninstall_from_temp(args.install_dir, silent=args.silent, extra=extra)
        return 0 if copied is not None else 4
    root = None
    if not args.silent:
        import tkinter as tk
        from quark_cli.potplayer import dialogs as messagebox
        from quark_cli.potplayer.ui_theme import apply_theme
        root = tk.Tk()
        apply_theme(root)
        icon = args.install_dir / 'app' / '_internal' / 'quark_cli' / 'potplayer' / 'MediaPlayParse - Quark.ico'
        if icon.is_file():
            root.iconbitmap(default=str(icon))
        root.withdraw()
        if not args.uninstall_player and not args.confirmed and not messagebox.askyesno('卸载夸克网盘插件', '请先关闭 PotPlayer。确定卸载插件？\n默认保留账号及观看记录。', parent=root):
            root.destroy()
            return 0
    try:
        from player_uninstall import run, close_plugin_windows
        if args.uninstall_player:
            return run(args.install_dir, silent=args.silent, registry=not args.no_registry)
        core.stop_service(args.install_dir)
        close_plugin_windows(args.install_dir)
        report = core.uninstall(install_dir=args.install_dir, clear_credentials=args.clear_credentials,
                                start_menu_dir=core.default_start_menu_dir(), registry=not args.no_registry, stop=False)
        if report.notes:
            raise core.InstallError('\n'.join(report.notes))
        if root:
            messagebox.showinfo('夸克网盘插件', '插件已卸载。' + ('本地账号数据已清除。' if args.clear_credentials else '账号与观看记录已保留。'), parent=root)
        return 0
    except (core.InstallError, OSError) as exc:
        if root:
            messagebox.showerror('卸载失败', str(exc), parent=root)
        elif sys.stderr:
            print(str(exc), file=sys.stderr)
        return 4
    finally:
        if root:
            root.destroy()


def main(argv=None):
    try:
        return _run(argv)
    finally:
        source = Path(sys.executable).resolve()
        if (getattr(sys, 'frozen', False) and os.environ.get(core.RELAUNCH_FLAG) == '1'
                and source.parent == Path(tempfile.gettempdir()).resolve()
                and source.name.startswith(core.TEMP_COPY_PREFIX)):
            # Windows keeps a running EXE locked. A hidden helper waits only for
            # this process, then deletes this exact private temporary copy.
            target = "'" + str(source).replace("'", "''") + "'"
            script = f'Wait-Process -Id {os.getpid()},{os.getppid()} -ErrorAction SilentlyContinue\nRemove-Item -LiteralPath {target} -Force'
            try:
                subprocess.Popen(['powershell', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-Command', script],
                                 creationflags=core.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                if sys.stderr:
                    print('临时卸载程序未能自动清理：' + str(source), file=sys.stderr)
