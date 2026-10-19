"""Windows ARP co-uninstall. The original PotPlayer uninstaller stays intact."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

try:
    import winreg
except ImportError:
    winreg = None

PREFIX = r'Software\Microsoft\Windows\CurrentVersion\Uninstall' + '\\'


def command(install_dir):
    return f'"{Path(install_dir) / "Uninstall.exe"}" --uninstall-player --install-dir "{install_dir}"'


def _hive(meta):
    if meta['hive'] not in ('HKLM', 'HKCU') or meta['key'] not in (PREFIX+'PotPlayer64', PREFIX+'PotPlayer'):
        raise ValueError('无效的 PotPlayer 卸载项归属')
    return winreg.HKEY_LOCAL_MACHINE if meta['hive'] == 'HKLM' else winreg.HKEY_CURRENT_USER


def _open(meta, write=False):
    return winreg.OpenKey(_hive(meta), meta['key'], 0,
                          (winreg.KEY_SET_VALUE if write else winreg.KEY_READ) | meta['view'])


def _value(handle, name):
    try:
        return winreg.QueryValueEx(handle, name)
    except FileNotFoundError:
        return None, None


def _native_command(raw, player):
    """Accept only this install's exact original executable, quoted or unquoted."""
    if not isinstance(raw, str):
        return False
    target = str(Path(player) / 'uninstall.exe')
    expanded = os.path.expandvars(raw).strip()
    for prefix in (f'"{target}"', target):
        if expanded.casefold() == prefix.casefold() or expanded.casefold().startswith(prefix.casefold()+' '):
            return True
    return False


def prepare(player, previous=None):
    """Read/permission-check before any installation mutation; portable is unlinked."""
    if winreg is None:
        return None
    native = Path(player) / 'uninstall.exe'
    if not native.is_file():
        return None
    # The synchronous temporary-copy invocation below is NSIS-specific.
    if b'\xef\xbe\xad\xdeNullsoftInst' not in native.read_bytes():
        return None
    for hive in ('HKCU', 'HKLM'):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            for name in ('PotPlayer64', 'PotPlayer'):
                meta = {'hive':hive, 'key':PREFIX+name, 'view':view}
                try:
                    with _open(meta) as handle:
                        raw, kind = _value(handle, 'UninstallString')
                        quiet, quiet_kind = _value(handle, 'QuietUninstallString')
                except FileNotFoundError:
                    continue
                if (isinstance(previous, dict) and all(previous.get(k)==v for k,v in meta.items())
                        and raw == previous.get('command')):
                    raw, kind = previous['original'], previous['original_type']
                    quiet, quiet_kind = previous.get('quiet_original'), previous.get('quiet_type')
                if not _native_command(raw, player):
                    continue
                # A denied HKLM write is reported before files are changed.
                with _open(meta, True):
                    pass
                return dict(meta, original=raw, original_type=kind,
                            quiet_original=quiet, quiet_type=quiet_kind)
    return None


def attach(meta, install_dir):
    with _open(meta, True) as handle:
        winreg.SetValueEx(handle, 'UninstallString', 0, winreg.REG_SZ, command(install_dir))
        winreg.SetValueEx(handle, 'QuietUninstallString', 0, winreg.REG_SZ, command(install_dir)+' --silent')


def restore(meta):
    """Restore only our own current value, never overwrite a later user change."""
    if not meta or winreg is None:
        return
    try:
        with _open(meta) as handle:
            current, _ = _value(handle, 'UninstallString')
            current_quiet, _ = _value(handle, 'QuietUninstallString')
        if current != meta.get('command'):
            return
        with _open(meta, True) as handle:
            winreg.SetValueEx(handle, 'UninstallString', 0, meta['original_type'], meta['original'])
            if current_quiet != meta.get('command', '')+' --silent':
                return  # Keep an independently changed quiet command.
            if meta.get('quiet_original') is None:
                try:
                    winreg.DeleteValue(handle, 'QuietUninstallString')
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(handle, 'QuietUninstallString', 0, meta['quiet_type'], meta['quiet_original'])
    except FileNotFoundError:
        pass  # The original uninstaller normally deletes its own ARP key.


def close_plugin_windows(install_dir):
    """Close only processes with this exact component path; never kill PotPlayer."""
    if sys.platform != 'win32':
        return
    target = str(Path(install_dir) / 'app' / 'QuarkPotPlayer.exe').replace("'", "''")
    script = ("$target='"+target+"'\n"
              "$owned=@(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $target })\n"
              "foreach($p in $owned) { if($p.MainWindowHandle -ne 0) { $null=$p.CloseMainWindow() } }\n"
              "$owned | Wait-Process -Timeout 3 -ErrorAction SilentlyContinue\n"
              "if(@(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $target }).Count) { exit 1 }")
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                             '-Command', script], creationflags=0x08000000,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    if result.returncode:
        from install_core import InstallError
        raise InstallError('插件窗口仍在运行，请关闭插件窗口后重试卸载')


def run(install_dir, *, silent=False, registry=True):
    import install_core as core
    install_dir = Path(install_dir)
    manifest = core.read_manifest(install_dir)
    if not manifest or not core._same_path(manifest.get('install_dir', ''), install_dir):
        raise core.InstallError('联合卸载缺少有效安装清单')
    meta = manifest.get('player_uninstall')
    player = Path(manifest['potplayer_dir'])
    if not isinstance(meta, dict) or not _native_command(meta.get('original'), player):
        raise core.InstallError('没有匹配当前 PotPlayer 的原始卸载命令')
    native = player / 'uninstall.exe'
    if not native.is_file():
        raise core.InstallError('找不到 PotPlayer 原始卸载程序')
    core.stop_service(install_dir)
    close_plugin_windows(install_dir)
    # NSIS _?= disables its detached self-copy. Invoke a private native copy so
    # we can wait for the actual UI result while the player folder is removed.
    with tempfile.TemporaryDirectory(prefix='quark-player-uninstall-') as temporary:
        copied = Path(temporary) / 'PotPlayer-Uninstall.exe'
        shutil.copyfile(native, copied)
        quiet = meta.get('quiet_original')
        original = quiet if silent and _native_command(quiet, player) else meta['original']
        original = os.path.expandvars(original).strip()
        prefix = f'"{native}"' if original.startswith('"') else str(native)
        suffix = original[len(prefix):]
        if silent and not _native_command(quiet, player):
            suffix += ' /S'
        invocation = subprocess.list2cmdline([str(copied)]) + suffix + ' _?=' + str(player)
        result = subprocess.run(invocation, creationflags=0x08000000 if sys.platform=='win32' else 0)
    if core._exe_in(player) is not None:
        return result.returncode  # Cancel/failed uninstall: preserve plugin data.
    if (install_dir / core.MANIFEST_NAME).is_file():
        report = core.uninstall(install_dir=install_dir, clear_credentials=True,
                                registry=registry, stop=False)
    else:
        report = core.UninstallReport()
        core.cleanup_external(manifest, report, clear_credentials=True, registry=registry)
    if report.notes:
        raise core.InstallError('\n'.join(report.notes))
    return result.returncode
