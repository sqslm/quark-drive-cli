"""Build the standalone Windows downloader ``dist/QuarkDriveDownloader.exe``.

One windowed ``onefile`` PyInstaller build of ``entry.py``: the resulting EXE
needs no Python installation and no source tree.

Usage::

    python packaging/downloader/build.py                 # release build
    python packaging/downloader/build.py --python PATH   # alternate interpreter
    python packaging/downloader/build.py --dist-dir DIR  # e.g. a smoke directory
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NAME = "QuarkDriveDownloader"

#: Modules PyInstaller's import graph can miss: Tk submodules pulled in lazily by
#: the window, and the QR PNG back-end ``auth.write_qr_png`` imports at call time.
HIDDEN_IMPORTS = (
    "quark_cli.downloader",
    "quark_cli.downloader.app",
    "quark_cli.downloader.core",
    "quark_cli.downloader.worker",
    "quark_cli.potplayer.login_gui",
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "qrcode",
    "qrcode.image.pil",
    "PIL",
    "PIL.Image",
)

#: The window needs httpx, the QR encoder and Tk only. ``PIL.Image`` imports
#: IPython behind an optional display hook, ``PIL`` imports numpy for optional
#: acceleration, and ``httpx._main`` (its command-line front-end) imports rich
#: and pygments. IPython's stack drags in the notebook, Jupyter and plotting
#: packages — including the Qt back-ends PyInstaller refuses to freeze together
#: ("multiple Qt bindings packages") — and Anaconda's numpy carries ~160 MiB of
#: MKL DLLs. None of it is reachable at runtime here.
EXCLUDED_MODULES = (
    "IPython",
    "matplotlib",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "numpy",
    "rich",
    "pygments",
    "zmq",
    "psutil",
    "cloudpickle",
    "bcrypt",
    "nacl",
    "cryptography",
)


class BuildError(RuntimeError):
    pass


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise BuildError(f"命令失败（退出码 {result.returncode}）：{command[0]}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(python: str, build_dir: Path, dist_dir: Path) -> Path:
    command = [
        python,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        NAME,
        "--paths",
        str(ROOT / "src"),
    ]
    for module in HIDDEN_IMPORTS:
        command += ["--hidden-import", module]
    for module in EXCLUDED_MODULES:
        command += ["--exclude-module", module]
    command += [
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir / "work"),
        "--specpath",
        str(build_dir / "spec"),
        str(HERE / "entry.py"),
    ]
    run(command)
    produced = dist_dir / f"{NAME}.exe"
    if not produced.is_file():
        raise BuildError(f"构建产物缺少可执行文件：{produced}")
    return produced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build.py", description="构建夸克网盘下载器单文件窗口程序")
    parser.add_argument("--python", default=sys.executable, help="带有 PyInstaller 的解释器")
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build" / "downloader")
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    arguments = parser.parse_args(argv)

    exe = build(arguments.python, arguments.build_dir, arguments.dist_dir)
    print(f"下载器：{exe}")
    print(f"SHA-256：{sha256(exe)}")
    print(f"体积：{exe.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as error:
        print(f"构建失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
