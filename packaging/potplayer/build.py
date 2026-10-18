"""Build the PotPlayer plugin installer.

Two PyInstaller builds and one payload directory:

1. ``QuarkPotPlayer.exe``      windowed ``onedir`` bridge component (``service_entry.py``)
2. ``component.zip``           the onedir tree, embedded into the installer
3. ``QuarkPotPlayer-Setup.exe`` windowed ``onefile`` Tkinter installer holding
   ``component.zip``, ``as/`` (the AngelScript files taken from ``--as-dir``) and
   ``build-info.json``

Usage::

    python packaging/potplayer/build.py                 # release build
    python packaging/potplayer/build.py --as-dir DIR    # smoke build with scratch scripts
    python packaging/potplayer/build.py --component-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
COMPONENT_NAME = "QuarkPotPlayer"
LAUNCHER_NAME = "quark-potplayer"
SETUP_NAME = "QuarkPotPlayer-Setup"
DEFAULT_VERSION = "0.12.3"
ICON = ROOT / 'potplayer' / 'MediaUrlList - Quark.ico'


class BuildError(RuntimeError):
    pass


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=str(cwd) if cwd else None, check=False)
    if result.returncode != 0:
        raise BuildError(f"命令失败（退出码 {result.returncode}）：{command[0]}")


def pyinstaller(python: str, arguments: list[str]) -> None:
    # Optional notebook/plotting integrations in the build host's Pillow stack
    # are not reachable in this Tk/HTTP app and can drag in entire Qt runtimes.
    excluded = [arg for name in (
        "IPython", "numpy", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
        "rich", "pygments", "zmq",
    ) for arg in ("--exclude-module", name)]
    run([python, "-m", "PyInstaller", *excluded, *arguments])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def zip_tree(tree: Path, destination: Path) -> None:
    """Archive the *contents* of ``tree`` so extraction yields the component root."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(tree.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(tree).as_posix())


def load_install_core():
    sys.path.insert(0, str(HERE))
    try:
        import install_core
    finally:
        sys.path.pop(0)
    return install_core


#: Runs inside the build interpreter (``--python``): resolves the dependency closure
#: and prints every license file it can read locally, plus the Python/Tcl-Tk terms.
_LICENSE_PROBE = r'''
import importlib.metadata as md
import json
import os
import re
import sys

try:
    from packaging.requirements import Requirement
except ImportError:  # pragma: no cover - the build env ships PyInstaller, which needs it
    Requirement = None


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def bare_name(requirement):
    return re.split(r"[\[<>=!~; ]", requirement.strip())[0].strip()


def license_files(dist):
    found = []
    for entry in dist.files or []:
        parts = [str(part).lower() for part in entry.parts]
        # Only the distribution metadata, never a package module that happens to
        # be named after a license.
        if not parts or not parts[0].endswith(".dist-info"):
            continue
        if parts[-1].startswith(("license", "copying", "notice", "authors")):
            path = dist.locate_file(entry)
            if os.path.isfile(path):
                found.append(str(path))
    return sorted(set(found))


def parse(requirement):
    """(name, extras) for a requirement string, or None when it is unreadable."""
    if Requirement is not None:
        try:
            parsed = Requirement(requirement)
        except Exception:
            return None
        return parsed.name, frozenset(parsed.extras)
    name = bare_name(requirement)
    return (name, frozenset()) if name else None


queue = []
for spec in sys.argv[1:]:
    parsed = parse(spec) if spec.strip() else None
    if parsed is not None:
        queue.append(parsed)

dists = {}
visited = set()
while queue:
    name, extras = queue.pop(0)
    key = (normalize(name), extras)
    if key in visited:
        continue
    visited.add(key)
    try:
        dist = md.distribution(name)
    except md.PackageNotFoundError:
        continue
    dists.setdefault(normalize(name), dist)
    for requirement in dist.requires or []:
        if Requirement is not None:
            try:
                parsed = Requirement(requirement)
            except Exception:
                continue
            requested = set(extras) | {""}  # unconditional deps use the empty extra
            if parsed.marker is not None and not any(
                parsed.marker.evaluate({"extra": extra}) for extra in requested
            ):
                continue
            queue.append((parsed.name, frozenset(parsed.extras)))
        else:
            dependency = bare_name(requirement)
            if dependency:
                queue.append((dependency, frozenset()))

result_dists = []
for dist in dists.values():
    files = license_files(dist)
    if files:
        result_dists.append(
            {"name": dist.metadata["Name"] or "unknown", "version": dist.version, "files": files}
        )

python_license = []
for candidate in ("LICENSE.txt", "LICENSE_PYTHON.txt", "LICENSE"):
    path = os.path.join(sys.base_prefix, candidate)
    if os.path.isfile(path):
        python_license.append(path)
        break

tk_license = []
for relative in (
    os.path.join("Library", "lib", "tk8.6", "license.terms"),
    os.path.join("tcl", "tk8.6", "license.terms"),
    os.path.join("lib", "tk8.6", "license.terms"),
    os.path.join("Library", "lib", "tcl8.6", "license.terms"),
    os.path.join("tcl", "tcl8.6", "license.terms"),
    os.path.join("lib", "tcl8.6", "license.terms"),
):
    path = os.path.join(sys.base_prefix, relative)
    if os.path.isfile(path):
        tk_license.append(path)
        break

print(json.dumps({"dists": result_dists, "python": python_license, "tk": tk_license}))
'''


def runtime_requirements() -> list[str]:
    """Requirement strings from ``[project].dependencies`` (extras kept for marker checks)."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    opener = re.search(r"(?m)^\s*dependencies\s*=\s*\[", text)
    if opener is None:
        raise BuildError("pyproject.toml 中未找到 [project].dependencies")
    index = opener.end()
    depth = 1
    while index < len(text) and depth:
        char = text[index]
        if char == '"':  # skip a quoted entry so a '[' or ']' inside it is inert
            index += 1
            while index < len(text) and text[index] != '"':
                index += 2 if text[index] == "\\" else 1
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        index += 1
    return [item for item in re.findall(r'"([^"]+)"', text[opener.end() : index - 1]) if item.strip()]


def collect_licenses(python: str) -> dict:
    """Ask the build interpreter for its dependency, Python and Tcl/Tk license files.

    Reading the build interpreter matters: PyInstaller bundles that environment's
    packages, so those are the licenses the frozen component must carry.
    """
    result = subprocess.run(
        [python, "-c", _LICENSE_PROBE, *runtime_requirements()],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BuildError(f"收集许可证元数据失败（{python}）：{result.stderr.strip() or result.stdout.strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise BuildError(f"许可证探针输出无法解析：{exc}") from exc


def stage_licenses(component_tree: Path, python: str) -> list[str]:
    """Copy the bundled licenses into ``component_tree/licenses`` (rebuilt each time)."""
    data = collect_licenses(python)
    destination = component_tree / "licenses"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    copies: list[tuple[Path, Path]] = []
    for kind, sources in (("python", data.get("python") or []), ("tcl-tk", data.get("tk") or [])):
        for source in sources:
            copies.append((Path(source), destination / kind / Path(source).name))
    for dist in data.get("dists") or []:
        base = destination / "python-packages" / f"{dist['name']}-{dist['version']}"
        for source in dist["files"]:
            copies.append((Path(source), base / Path(source).name))
    written: list[str] = []
    for source, target in copies:
        relative = target.relative_to(component_tree)
        if str(relative) in written:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        written.append(str(relative))
    print(f"随组件打包许可证：{len(written)} 个文件 -> {destination}")
    return written


def check_scripts(as_dir: Path, allow_empty: bool) -> list[Path]:
    core = load_install_core()
    if not as_dir.is_dir():
        raise BuildError(f"扩展脚本目录不存在：{as_dir}")
    scripts = [item for item in core.collect_as_files(as_dir) if item.name.endswith(".as")]
    if not scripts and not allow_empty:
        raise BuildError(f"{as_dir} 下没有 AngelScript 扩展脚本；如仅做链路冒烟请加 --allow-empty-as")
    for item in scripts:
        print(f"扩展脚本 {item.name} -> Extension/{item.target}")
    return [item.source for item in scripts]


def build_component(python: str, build_dir: Path) -> Path:
    component_build = build_dir / "component"
    dist = component_build / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    pyinstaller(
        python,
        [
            "--noconfirm",
            "--clean",
            "--onedir",
            "--windowed",
            "--name",
            COMPONENT_NAME,
            "--icon", str(ICON),
            "--add-data", f"{ICON};quark_cli/potplayer",
            "--paths",
            str(ROOT / "src"),
            "--hidden-import",
            "quark_cli.potplayer.__main__",
            "--hidden-import",
            "quark_cli.potplayer.service",
            "--hidden-import",
            "quark_cli.potplayer.status_gui",
            "--hidden-import",
            "quark_cli.potplayer.management_gui",
            "--hidden-import",
            "quark_cli.potplayer.native_player",
            "--hidden-import",
            "quark_cli.potplayer.playback",
            "--hidden-import",
            "quark_cli.potplayer.diagnostics",
            "--hidden-import",
            "quark_cli.potplayer.library_state",
            "--hidden-import",
            "tkinter",
            "--distpath",
            str(dist),
            "--workpath",
            str(component_build / "work"),
            "--specpath",
            str(component_build / "spec"),
            str(HERE / "service_entry.py"),
        ],
    )
    tree = dist / COMPONENT_NAME
    if not (tree / f"{COMPONENT_NAME}.exe").is_file():
        raise BuildError(f"组件构建产物缺少可执行文件：{tree}")
    return tree


def build_launcher(python: str, build_dir: Path) -> Path:
    """The tiny ``quark-potplayer.exe`` the extensions find next to ``Extension``."""
    launcher_build = build_dir / "launcher"
    dist = launcher_build / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    pyinstaller(
        python,
        [
            "--noconfirm",
            "--clean",
            "--onefile",
            "--windowed",
            "--name",
            LAUNCHER_NAME,
            "--icon", str(ICON),
            "--distpath",
            str(dist),
            "--workpath",
            str(launcher_build / "work"),
            "--specpath",
            str(launcher_build / "spec"),
            str(HERE / "launcher_entry.py"),
        ],
    )
    produced = dist / f"{LAUNCHER_NAME}.exe"
    if not produced.is_file():
        raise BuildError(f"启动器构建产物缺少可执行文件：{produced}")
    return produced


def stage_payload(
    build_dir: Path,
    component_tree: Path,
    launcher_exe: Path,
    scripts: list[Path],
    version: str,
    console: str,
) -> Path:
    payload = build_dir / "payload"
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)
    zip_tree(component_tree, payload / "component.zip")
    launcher_dir = payload / "launcher"
    launcher_dir.mkdir()
    shutil.copyfile(launcher_exe, launcher_dir / f"{LAUNCHER_NAME}.exe")
    as_dir = payload / "as"
    as_dir.mkdir()
    for script in scripts:
        shutil.copyfile(script, as_dir / script.name)
        icon = script.with_suffix(".ico")
        if icon.is_file():
            shutil.copyfile(icon, as_dir / icon.name)
    info = {
        "product": COMPONENT_NAME,
        "version": version,
        "component_subsystem": console,
        "launcher_bytes": (launcher_dir / f"{LAUNCHER_NAME}.exe").stat().st_size,
        "as_files": sorted(path.name for path in scripts),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": sys.version.split()[0],
        "bits": 64 if sys.maxsize > 2**32 else 32,
    }
    (payload / "build-info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"负载目录：{payload}")
    return payload


def build_installer(python: str, build_dir: Path, payload: Path, out_dir: Path) -> Path:
    installer_build = build_dir / "installer"
    dist = installer_build / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    add = ["--add-data", f"{payload / 'component.zip'}{';'}."]
    add += ["--add-data", f"{payload / 'launcher'}{';'}launcher"]
    add += ["--add-data", f"{payload / 'as'}{';'}as"]
    add += ["--add-data", f"{payload / 'build-info.json'}{';'}."]
    pyinstaller(
        python,
        [
            "--noconfirm",
            "--clean",
            "--onefile",
            "--windowed",
            "--name",
            SETUP_NAME,
            "--icon", str(ICON),
            "--paths",
            str(HERE),
            *add,
            "--distpath",
            str(dist),
            "--workpath",
            str(installer_build / "work"),
            "--specpath",
            str(installer_build / "spec"),
            str(HERE / "installer.py"),
        ],
    )
    produced = dist / f"{SETUP_NAME}.exe"
    if not produced.is_file():
        raise BuildError(f"安装器构建产物缺少可执行文件：{produced}")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / produced.name
    shutil.copyfile(produced, target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build.py", description="构建夸克网盘 PotPlayer 插件安装包")
    parser.add_argument("--as-dir", type=Path, default=ROOT / "potplayer", help="AngelScript 扩展脚本目录")
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--tag", default=None, help="输出到 dist/potplayer/<tag>/，用于区分冒烟产物")
    parser.add_argument("--python", default=sys.executable, help="带有 PyInstaller 的解释器")
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build" / "potplayer")
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--skip-component", action="store_true", help="复用既有 component.zip")
    parser.add_argument("--component-only", action="store_true", help="只构建并打包组件")
    parser.add_argument("--allow-empty-as", action="store_true", help="允许没有扩展脚本（仅管道冒烟）")
    arguments = parser.parse_args(argv)

    build_dir: Path = arguments.build_dir
    out_dir: Path = arguments.out_dir or (ROOT / "dist" / "potplayer" / arguments.tag if arguments.tag else ROOT / "dist" / "potplayer")
    scripts = check_scripts(arguments.as_dir, arguments.allow_empty_as)
    components: list[Path] = []

    component_tree = build_dir / "component" / "dist" / COMPONENT_NAME
    if not arguments.skip_component:
        component_tree = build_component(arguments.python, build_dir)
        print(f"组件：{component_tree}")
    if not (component_tree / f"{COMPONENT_NAME}.exe").is_file():
        raise BuildError(f"缺少组件产物 {component_tree}；请先执行组件构建")
    stage_licenses(component_tree, arguments.python)
    launcher_exe = build_dir / "launcher" / "dist" / f"{LAUNCHER_NAME}.exe"
    if arguments.component_only:
        out_dir.mkdir(parents=True, exist_ok=True)
        archive = out_dir / "component.zip"
        zip_tree(component_tree, archive)
        components.append(archive)
        print(f"组件压缩包：{archive}")
        return _write_sums(out_dir, components, arguments.version, scripts)
    if not arguments.skip_component or not launcher_exe.is_file():
        launcher_exe = build_launcher(arguments.python, build_dir)
        print(f"启动器：{launcher_exe}")

    payload = stage_payload(build_dir, component_tree, launcher_exe, scripts, arguments.version, "windowed")
    setup = build_installer(arguments.python, build_dir, payload, out_dir)
    components.append(setup)
    print(f"安装程序：{setup}")
    return _write_sums(out_dir, components, arguments.version, scripts)


def _write_sums(out_dir: Path, artifacts: list[Path], version: str, scripts: list[Path]) -> int:
    lines = [f"{sha256(path)}  {path.name}" for path in artifacts]
    (out_dir / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "build-info.json").write_text(
        json.dumps(
            {
                "product": SETUP_NAME,
                "version": version,
                "as_files": sorted(path.name for path in scripts),
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "python": sys.version.split()[0],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as error:
        print(f"构建失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
