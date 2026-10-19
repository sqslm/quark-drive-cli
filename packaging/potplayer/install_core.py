"""Pure install/uninstall logic for the Quark Drive PotPlayer bridge.

Nothing here opens a window: every root (install dir, Start Menu, registry key,
credential dir) is a parameter so unit tests run entirely in temp directories
and never touch a real PotPlayer installation.

Layout produced by ``install``::

    <PotPlayer>\\Plugin\\QuarkPotPlayer\\
        app\\QuarkPotPlayer.exe      bridge component (PyInstaller onedir)
        Uninstall.exe               independent lightweight uninstall entry point
        install.json                 manifest of everything this installer created
    <PotPlayer>\\Extension\\Media\\{UrlList,PlayParse}\\Media* - Quark*.as
    %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\夸克网盘 (PotPlayer).lnk
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # Windows-only; imported lazily so the module stays importable elsewhere.
    import winreg
except ImportError:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore[assignment]

MANIFEST_NAME = "install.json"
MANIFEST_SCHEMA = 1
PRODUCT_NAME = "夸克网盘 PotPlayer 插件"
APP_DIR_NAME = "QuarkPotPlayer"
COMPONENT_EXE = "QuarkPotPlayer.exe"
SETUP_EXE_NAME = "QuarkPotPlayer-Setup.exe"
UNINSTALL_EXE_NAME = 'Uninstall.exe'
#: Path taken by the AngelScript extensions: ``HostGetScriptFolder()\..\..\<name>``.
LAUNCHER_NAME = "quark-potplayer.exe"
LAUNCHER_PATH_NAME = "quark-potplayer.path"
UNINSTALL_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\QuarkPotPlayer"
START_MENU_NAME = "夸克网盘 (PotPlayer).lnk"
PLUGIN_CREDENTIAL_DIR_NAME = "potplayer"
CLI_CONFIG_DIR_NAME = "quark-drive-cli"

#: ``MediaUrlList - Name.as`` installs into ``Extension\Media\UrlList``, and so on.
AS_TARGETS: Mapping[str, str] = {
    "MediaUrlList": "Media/UrlList",
    "MediaPlayParse": "Media/PlayParse",
    "MediaPlaylist": "Media/Playlist",
    "SubtitleTranslate": "Subtitle/Translate",
    "SubtitleSearch": "Subtitle/Search",
}

CREATE_NO_WINDOW = 0x08000000
_DETACHED = 0x00000008
_EXE_CANDIDATES = ("PotPlayerMini64.exe", "PotPlayerMini.exe", "PotPlayer64.exe", "PotPlayer.exe")


class InstallError(RuntimeError):
    """A refusal to modify something this installer did not create."""


# --------------------------------------------------------------------------- roots


def local_app_data(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    base = source.get("LOCALAPPDATA", "").strip()
    if not base:
        raise InstallError("无法确定 LOCALAPPDATA，当前用户环境不完整")
    return Path(base)


def default_install_dir(env: Mapping[str, str] | None = None, *, potplayer_dir: Path | None = None) -> Path:
    player = potplayer_dir or detect_potplayer_dir(env=env)
    return (Path(player) if player else Path()) / 'Plugin' / APP_DIR_NAME


def cli_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """The product's config directory, i.e. the parent of the plugin credential dir.

    An absolute ``QUARK_CONFIG_DIR`` (the bridge's own override) takes precedence
    so uninstall clears the same credential directory the bridge actually wrote.
    """
    source = os.environ if env is None else env
    override = source.get("QUARK_CONFIG_DIR")
    if override is not None:
        candidate = Path(override)
        if override.strip() and candidate.is_absolute():
            return candidate
    base = source.get("APPDATA", "").strip()
    if not base:
        return Path.home() / "AppData" / "Roaming" / CLI_CONFIG_DIR_NAME
    return Path(base) / CLI_CONFIG_DIR_NAME


def plugin_credential_dir(config_dir: Path | None = None) -> Path:
    return (cli_config_dir() if config_dir is None else config_dir) / PLUGIN_CREDENTIAL_DIR_NAME


RELAUNCH_FLAG = "QUARK_POTPLAYER_UNINSTALL_RELAUNCH"
TEMP_COPY_PREFIX = "QuarkPotPlayer-Uninstall-"


def default_start_menu_dir(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    base = source.get("APPDATA", "").strip()
    if not base:
        raise InstallError("无法确定 APPDATA，当前用户环境不完整")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


# ----------------------------------------------------------------- PotPlayer lookup


def _registry_program_dirs() -> list[Path]:
    if winreg is None or sys.platform != "win32":  # pragma: no cover - non-Windows
        return []
    found: list[Path] = []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\DAUM\PotPlayer64"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\DAUM\PotPlayerMini64"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\DAUM\PotPlayer"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\DAUM\PotPlayer"),
        (winreg.HKEY_CURRENT_USER, r"Software\DAUM\PotPlayer64"),
        (winreg.HKEY_CURRENT_USER, r"Software\DAUM\PotPlayerMini64"),
        (winreg.HKEY_CURRENT_USER, r"Software\DAUM\PotPlayer"),
    ]
    for hive, key in roots:
        for value in ("ProgramPath", "ProgramFolder"):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    raw, _ = winreg.QueryValueEx(handle, value)
            except OSError:
                continue
            if not isinstance(raw, str) or not raw.strip():
                continue
            candidate = Path(raw.strip())
            found.append(candidate if candidate.suffix.lower() == ".exe" else candidate / COMPONENT_EXE)
    return found


def _registry_uninstall_icons() -> list[Path]:
    if winreg is None or sys.platform != "win32":  # pragma: no cover - non-Windows
        return []
    found: list[Path] = []
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PotPlayer64"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PotPlayer"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\PotPlayer"),
    ]
    for hive, key in keys:
        for value in ("DisplayIcon", "InstallLocation"):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    raw, _ = winreg.QueryValueEx(handle, value)
            except OSError:
                continue
            if isinstance(raw, str) and raw.strip():
                found.append(Path(raw.strip().strip('"')))
    return found


def _common_install_dirs(env: Mapping[str, str] | None = None) -> list[Path]:
    source = os.environ if env is None else env
    bases = [
        source.get("ProgramFiles", r"C:\Program Files"),
        source.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        source.get("LOCALAPPDATA", ""),
    ]
    dirs: list[Path] = []
    for base in bases:
        if not base.strip():
            continue
        for suffix in ("DAUM/PotPlayer", "PotPlayer", "Programs/PotPlayer"):
            dirs.append(Path(base) / suffix)
    return dirs


def _exe_in(directory: Path) -> Path | None:
    for name in _EXE_CANDIDATES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def is_potplayer_dir(directory: Path) -> bool:
    """True when ``directory`` holds a PotPlayer executable and its ``Extension`` tree."""
    return _exe_in(directory) is not None and (directory / "Extension").is_dir()


def resolve_potplayer_dir(candidate: Path) -> Path:
    """Normalise a user-chosen file or folder into a validated PotPlayer directory."""
    path = Path(candidate)
    directory = path.parent if path.is_file() and path.suffix.lower() == ".exe" else path
    if not directory.is_dir():
        raise InstallError(f"目录不存在：{directory}")
    if not is_potplayer_dir(directory):
        raise InstallError(f"不是有效的 PotPlayer 安装目录（缺少 PotPlayer*.exe 或 Extension 目录）：{directory}")
    return directory


def detect_potplayer_dir(
    *,
    explicit: Path | None = None,
    saved: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """The validated PotPlayer directory, or ``None`` when nothing is found.

    An ``explicit`` directory is authoritative: when it is not a usable PotPlayer
    directory, :class:`InstallError` is raised at once. The search never falls
    back to another location the user did not ask for, which could otherwise
    silently install into a completely different player.

    Without ``explicit``, the order is: the path recorded by a previous install,
    PotPlayer's own registry entries, its uninstall entry, then conventional
    install folders. A stale recorded path is skipped and the search continues.
    """
    if explicit is not None:
        return resolve_potplayer_dir(Path(explicit))
    candidates: list[Path] = []
    if saved:
        candidates.append(Path(saved))
    for entry in _registry_program_dirs():
        candidates.append(entry if entry.suffix.lower() == ".exe" else _exe_in(entry) or entry)
    candidates.extend(_registry_uninstall_icons())
    for directory in _common_install_dirs(env):
        found = _exe_in(directory)
        candidates.append(found if found is not None else directory)
    for cache in _candidate_cache:
        candidates.append(cache)
    for candidate in candidates:
        try:
            return resolve_potplayer_dir(candidate)
        except InstallError:
            continue
    return None


_candidate_cache: list[Path] = []


# ------------------------------------------------------------------ AngelScript map


def as_target_for(name: str) -> str:
    """Extension subdirectory for an AngelScript file, decided by its file-name prefix."""
    prefix = name.split(" - ", 1)[0].strip()
    try:
        return AS_TARGETS[prefix]
    except KeyError:
        raise InstallError(f"无法判断扩展脚本的安装位置（前缀 {prefix!r} 未知）：{name}") from None


@dataclass(frozen=True)
class AsFile:
    name: str
    source: Path

    @property
    def target(self) -> str:
        return as_target_for(self.name)


def launcher_paths(potplayer_dir: Path) -> tuple[Path, Path]:
    """The launcher and its recorded component path, both next to ``Extension``."""
    extension_root = Path(potplayer_dir) / "Extension"
    return extension_root / LAUNCHER_NAME, extension_root / LAUNCHER_PATH_NAME


def collect_as_files(directory: Path) -> list[AsFile]:
    """Every ``*.as`` under ``directory``, plus same-stem ``*.ico`` icons."""
    files = sorted((p for p in directory.glob("*.as") if p.is_file()), key=lambda p: p.name)
    collected: list[AsFile] = []
    for path in files:
        as_target_for(path.name)
        collected.append(AsFile(path.name, path))
        icon = path.with_suffix(".ico")
        if icon.is_file():
            collected.append(AsFile(icon.name, icon))
    return collected


def extension_subdir(potplayer_dir: Path, target: str) -> Path:
    return Path(potplayer_dir) / "Extension" / Path(*target.split("/"))


# ------------------------------------------------------------------------ manifest


@dataclass
class InstallRecord:
    version: str
    install_dir: Path
    potplayer_dir: Path
    created: list[str] = field(default_factory=list)
    as_files: list[str] = field(default_factory=list)
    launcher_files: list[str] = field(default_factory=list)
    shortcut: str | None = None
    installed_at: int = 0
    upgraded: bool = False
    credential_dir: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": MANIFEST_SCHEMA,
            "product": PRODUCT_NAME,
            "version": self.version,
            "install_dir": str(self.install_dir),
            "potplayer_dir": str(self.potplayer_dir),
            "created": list(self.created),
            "as_files": list(self.as_files),
            "launcher_files": list(self.launcher_files),
            "shortcut": self.shortcut,
            "credential_dir": self.credential_dir,
            "installed_at": self.installed_at or int(time.time()),
        }


def read_manifest(install_dir: Path) -> dict[str, Any] | None:
    try:
        raw = (Path(install_dir) / MANIFEST_NAME).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == MANIFEST_SCHEMA else None


def saved_potplayer_dir(install_dir: Path) -> str | None:
    manifest = read_manifest(install_dir)
    if manifest is None:
        return None
    value = manifest.get("potplayer_dir")
    return value if isinstance(value, str) and value else None


def is_installed(install_dir: Path) -> bool:
    return (Path(install_dir) / "app" / COMPONENT_EXE).is_file()


# ------------------------------------------------------------------------- install


def _install_launcher(payload_dir: Path, component: Path, potplayer_dir: Path, record: InstallRecord) -> None:
    """Place ``quark-potplayer.exe`` next to ``Extension`` and record its target path."""
    source = Path(payload_dir) / "launcher" / LAUNCHER_NAME
    if not source.is_file():
        raise InstallError(f"安装包缺少启动器：{source}")
    launcher, path_file = launcher_paths(potplayer_dir)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, launcher)
    path_file.write_text(str(component), encoding="utf-8")
    record.launcher_files.extend([str(launcher), str(path_file)])


def _extract_component(package_zip: Path, destination: Path) -> None:
    with zipfile.ZipFile(package_zip) as archive:
        for member in archive.infolist():
            name = member.filename
            if name.startswith("/") or ".." in Path(name).parts:
                raise InstallError(f"组件压缩包内含非法路径：{name}")
        archive.extractall(destination)


def _remove_empty_dir(path: Path) -> None:
    """Drop a directory only when it is empty; a user file left inside keeps it."""
    try:
        Path(path).rmdir()
    except OSError:
        pass


def _remove_tree(path: Path, *, attempts: int = 40, delay: float = 0.25, hint: str = "") -> None:
    """Delete a tree, retrying while a just-stopped bridge still holds its files."""
    last: OSError | None = None
    for _ in range(max(1, attempts)):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            last = exc
            time.sleep(delay)
            continue
        return
    raise InstallError(f"无法删除 {path}：文件被占用（{last}）。{hint}".rstrip())


def ensure_install_dir_available(install_dir: Path) -> None:
    """Refuse a non-empty directory this installer did not create.

    Installing into an arbitrary populated folder would later force the uninstall
    to either leave our files behind or delete the user's; so only an empty
    directory, or one already marked by our manifest or component, is accepted.
    """
    directory = Path(install_dir)
    if not directory.exists():
        return
    if not directory.is_dir():
        raise InstallError(f"安装目录不是目录：{directory}")
    if not any(directory.iterdir()):
        return
    if read_manifest(directory) is not None or is_installed(directory):
        return
    raise InstallError(
        f"安装目录非空且不是本插件的安装：{directory}。请选择一个空目录，或本插件已有的安装目录。"
    )


def install(
    *,
    payload_dir: Path,
    install_dir: Path,
    potplayer_dir: Path,
    version: str,
    as_files: Iterable[AsFile],
    self_exe: Path | None = None,
    start_menu_dir: Path | None = None,
    make_shortcut: bool = False,
    registry: bool = True,
    config_dir: Path | None = None,
) -> InstallRecord:
    """Install or upgrade: component tree, extension scripts, shortcut, manifest."""
    install_dir = Path(install_dir)
    potplayer_dir = resolve_potplayer_dir(potplayer_dir)
    ensure_install_dir_available(install_dir)
    previous = read_manifest(install_dir) or {}
    from player_uninstall import prepare, attach
    try:
        linked = prepare(potplayer_dir, previous.get('player_uninstall')) if registry else None
    except PermissionError:
        raise InstallError('设置 PotPlayer 卸载联动需要管理员权限，请右键安装包选择“以管理员身份运行”后重试') from None
    package_zip = Path(payload_dir) / "component.zip"
    if not package_zip.is_file():
        raise InstallError(f"安装包缺少组件负载：{package_zip}")

    record = InstallRecord(
        version=version,
        install_dir=install_dir,
        potplayer_dir=potplayer_dir,
        installed_at=int(time.time()),
        upgraded=is_installed(install_dir),
        credential_dir=str(plugin_credential_dir(config_dir)),
    )
    app_dir = install_dir / "app"
    if app_dir.exists():
        # The bridge may still be serving; ask it to exit so its files unlock.
        stop_service(install_dir)
        _remove_tree(
            app_dir,
            hint=f"请关闭 PotPlayer 后重试，或运行 {install_dir}\\app\\{COMPONENT_EXE} --stop 结束后台服务",
        )
    app_dir.mkdir(parents=True, exist_ok=True)
    record.created.append("app")
    component = app_dir / COMPONENT_EXE
    try:
        _extract_component(package_zip, app_dir)
        if not component.is_file():
            raise InstallError(f"组件压缩包中缺少 {COMPONENT_EXE}")
    except InstallError:
        # Only ever remove what this call created; the install directory may hold
        # files the user put there, so it is dropped only once it is empty.
        shutil.rmtree(app_dir, ignore_errors=True)
        _remove_empty_dir(install_dir)
        raise

    for item in as_files:
        directory = extension_subdir(potplayer_dir, item.target)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / item.name
        shutil.copyfile(item.source, destination)
        record.as_files.append(str(destination))

    _install_launcher(payload_dir, install_dir / "app" / COMPONENT_EXE, potplayer_dir, record)

    # The launcher also contains the small uninstall entry point; the downloaded
    # installer and its compressed payload are never retained for management.
    shutil.copyfile(Path(payload_dir) / 'launcher' / LAUNCHER_NAME, install_dir / UNINSTALL_EXE_NAME)
    record.created.append(UNINSTALL_EXE_NAME)
    stop_service(install_dir)

    old_shortcut = previous.get('shortcut')
    menu = Path(start_menu_dir) if start_menu_dir is not None else default_start_menu_dir()
    if isinstance(old_shortcut, str) and menu is not None:
        shortcut = Path(old_shortcut)
        if shortcut.name == START_MENU_NAME and shortcut.resolve().parent == menu.resolve():
            shortcut.unlink(missing_ok=True)

    manifest = record.to_json()
    if linked is not None:
        from player_uninstall import command
        linked['command'] = command(install_dir)
        manifest['player_uninstall'] = linked
    (install_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if registry:
        if linked is not None:
            attach(linked, install_dir)
        register_uninstall(install_dir, version=version)
    return record


def register_uninstall(install_dir: Path, *, version: str) -> None:
    """Per-user Add/Remove Programs entry; no elevation is involved."""
    if winreg is None or sys.platform != "win32":  # pragma: no cover - non-Windows
        return
    setup = Path(install_dir) / UNINSTALL_EXE_NAME
    values = {
        "DisplayName": PRODUCT_NAME,
        "DisplayVersion": version,
        "Publisher": "quark-cli",
        "InstallLocation": str(install_dir),
        "DisplayIcon": str(Path(install_dir) / "app" / COMPONENT_EXE),
        "UninstallString": f'"{setup}" --uninstall --install-dir "{install_dir}"',
        "QuietUninstallString": f'"{setup}" --uninstall --install-dir "{install_dir}" --silent',
        "NoModify": 1,
        "NoRepair": 1,
        "EstimatedSize": _tree_size_kb(Path(install_dir) / "app"),
    }
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_REGISTRY_PATH, 0, winreg.KEY_WRITE) as handle:
        for name, value in values.items():
            winreg.SetValueEx(handle, name, 0, winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ, value)


def _tree_size_kb(root: Path) -> int:
    total = 0
    for current, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += (Path(current) / name).stat().st_size
            except OSError:
                continue
    return total // 1024


# ----------------------------------------------------------------------- uninstall

@dataclass
class UninstallReport:
    removed: list[str] = field(default_factory=list)
    kept_credentials: bool = True
    credentials_removed: bool = False
    notes: list[str] = field(default_factory=list)


def _same_path(left: str | Path, right: str | Path) -> bool:
    """Path equality that survives 8.3 short names and mixed case."""
    try:
        return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))
    except OSError:
        return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False
    return True


def _remove_file(path: Path, root: Path, report: UninstallReport) -> None:
    target = Path(path)
    if not _within(target, root):
        report.notes.append(f"保留越界文件：{target}")
        return
    try:
        if target.is_file():
            target.unlink()
            report.removed.append(str(target))
    except OSError as exc:
        report.notes.append(f"无法删除 {target}：{exc}")


def stop_service(install_dir: Path, *, timeout: float = 5.0) -> bool:
    """Ask our own bridge to exit so its files can be replaced or removed."""
    component = Path(install_dir) / "app" / COMPONENT_EXE
    if not component.is_file():
        return False
    # The installed 0.12.7 executable cannot read its own elevated runtime.
    # Recover it before asking that old executable to release its files.
    from quark_cli.errors import LocalIOError
    from quark_cli.state import repair_windows_private_owner
    try:
        repair_windows_private_owner(plugin_credential_dir() / 'runtime.json')
    except LocalIOError as exc:
        raise InstallError(f'无法恢复旧服务状态：{exc}') from None
    try:
        result = subprocess.run(
            [str(component), "--stop"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW | _DETACHED,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


#: Entries ``install`` creates directly inside the install directory. Anything else
#: found there was added by the user afterwards and is left untouched.
_OWNED_INSTALL_ENTRIES = ("app", SETUP_EXE_NAME, UNINSTALL_EXE_NAME, MANIFEST_NAME)


def _remove_installed_files(install_dir: Path, manifest: Mapping[str, Any], report: UninstallReport) -> None:
    """Delete only the files this installer owns, then the directory once it is empty."""
    owned = list(_OWNED_INSTALL_ENTRIES)
    for name in manifest.get("created") or []:
        if isinstance(name, str) and name and not Path(name).is_absolute() and ".." not in Path(name).parts:
            owned.append(name)
    for name in dict.fromkeys(owned):
        target = install_dir / name
        if not _within(target, install_dir):
            report.notes.append(f"保留越界条目：{target}")
            continue
        if target.is_dir():
            try:
                _remove_tree(target, attempts=12, hint="请退出 PotPlayer 后重试；被占用的文件会保留。")
            except InstallError as exc:
                report.notes.append(str(exc))
                continue
            report.removed.append(str(target))
        elif target.is_file():
            _remove_file(target, install_dir, report)
    _remove_empty_dir(install_dir)


def uninstall(
    *,
    install_dir: Path,
    clear_credentials: bool = False,
    config_dir: Path | None = None,
    start_menu_dir: Path | None = None,
    registry: bool = True,
    stop: bool = True,
) -> UninstallReport:
    """Remove only paths recorded by this installer, leaving every other file alone."""
    install_dir = Path(install_dir)
    report = UninstallReport()
    manifest = read_manifest(install_dir)
    if manifest is None:
        raise InstallError(f"未找到本安装器创建的清单，拒绝删除：{install_dir}")
    if not _same_path(str(manifest.get("install_dir", "")), install_dir):
        raise InstallError("清单记录的位置与目标不一致，拒绝删除以避免误伤")
    if install_dir.parent == install_dir:
        raise InstallError("拒绝删除驱动器根目录")

    if stop:
        stop_service(install_dir)
    if registry:
        from player_uninstall import restore
        restore(manifest.get('player_uninstall'))

    potplayer_dir = Path(str(manifest.get("potplayer_dir", "")))
    if potplayer_dir.is_dir():
        for raw in [*(manifest.get("as_files") or []), *(manifest.get("launcher_files") or [])]:
            target = Path(str(raw))
            if target.name == LAUNCHER_NAME or target.name == LAUNCHER_PATH_NAME:
                _remove_file(target, potplayer_dir, report)
                continue
            if not target.name.startswith("Media") and not target.suffix.lower() in {".as", ".ico"}:
                report.notes.append(f"跳过可疑条目：{target}")
                continue
            _remove_file(target, potplayer_dir, report)

    _remove_installed_files(install_dir, manifest, report)
    cleanup_external(manifest, report, clear_credentials=clear_credentials,
                     config_dir=config_dir, start_menu_dir=start_menu_dir, registry=registry)
    return report


def cleanup_external(manifest, report, *, clear_credentials=False, config_dir=None,
                     start_menu_dir=None, registry=True):
    """Also usable after PotPlayer removed the component's entire directory."""
    shortcut = manifest.get("shortcut")
    if isinstance(shortcut, str) and shortcut:
        root = Path(start_menu_dir) if start_menu_dir is not None else Path(shortcut).parent
        _remove_file(Path(shortcut), root, report)

    if registry:
        from player_uninstall import restore
        restore(manifest.get('player_uninstall'))
        remove_uninstall_registry()

    if clear_credentials:
        plugin_dir = plugin_credential_dir(config_dir)
        expected = Path(str(manifest.get("credential_dir", plugin_dir)))
        if os.path.normcase(str(expected)) != os.path.normcase(str(plugin_dir)):
            report.notes.append("凭证目录与预期不符，已保留")
        elif plugin_dir.name != PLUGIN_CREDENTIAL_DIR_NAME or plugin_dir.parent == plugin_dir:
            report.notes.append(f"凭证目录异常，已保留：{plugin_dir}")
        elif plugin_dir.is_dir():
            shutil.rmtree(plugin_dir, ignore_errors=True)
            report.credentials_removed = not plugin_dir.exists()
            report.kept_credentials = plugin_dir.exists()
            if report.kept_credentials:
                report.notes.append('插件账号与观看数据未能清理，请检查目录权限')
        else:
            report.credentials_removed = True
            report.kept_credentials = False


def remove_uninstall_registry() -> None:
    if winreg is None or sys.platform != "win32":  # pragma: no cover - non-Windows
        return
    try:
        winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_REGISTRY_PATH, winreg.KEY_WRITE)
    except OSError:
        pass


# ---------------------------------------------------------------------- launching


def launch_potplayer(potplayer_dir: Path | None, entry: str | None = None) -> bool:
    """Open the player (the drive entry lives in its extension list); no invented URL."""
    if potplayer_dir is None:
        return False
    exe = _exe_in(Path(potplayer_dir))
    if exe is None:
        return False
    try:
        args = [str(exe)] if not entry else [str(exe), entry]
        subprocess.Popen(args, creationflags=_DETACHED, close_fds=True)
    except OSError:
        return False
    return True


# ------------------------------------------------------- uninstaller self-removal


def needs_temp_relaunch(install_dir: Path, env: Mapping[str, str] | None = None) -> bool:
    """True when this frozen setup copy runs from the install dir it must delete.

    A running executable cannot delete itself, so the uninstall is handed to a copy
    in ``%TEMP%``; that copy removes the whole install directory, this file included.
    """
    source = os.environ if env is None else env
    if source.get(RELAUNCH_FLAG):
        return False
    if not getattr(sys, "frozen", False):
        return False
    return _same_path(Path(sys.executable).parent, install_dir)


def relaunch_uninstall_from_temp(install_dir: Path, *, silent: bool, extra: Iterable[str]) -> Path | None:
    """Start the uninstall from a temporary copy and return its path, or ``None``."""
    import tempfile

    source = Path(sys.executable)
    target = Path(tempfile.gettempdir()) / f"{TEMP_COPY_PREFIX}{os.getpid()}.exe"
    try:
        shutil.copyfile(source, target)
    except OSError:
        return None
    env = dict(os.environ)
    env[RELAUNCH_FLAG] = "1"
    command = [str(target), "--uninstall", "--install-dir", str(install_dir), *extra]
    options: dict[str, Any] = {"close_fds": True, "env": env}
    if silent:
        options["creationflags"] = CREATE_NO_WINDOW
        options["stdout"] = subprocess.DEVNULL
        options["stderr"] = subprocess.DEVNULL
    try:
        subprocess.Popen(command, **options)
    except OSError:
        return None
    return target
