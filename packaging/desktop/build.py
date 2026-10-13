"""Build the embedded Python backend, then the Electron Windows application."""
import argparse
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-only", action="store_true")
    parser.add_argument("--skip-backend", action="store_true")
    args = parser.parse_args()
    if not args.skip_backend:
        shared = runpy.run_path(str(ROOT / "packaging/downloader/build.py"))
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
            "--name", "QuarkBackend", "--paths", str(ROOT / "src"), "--distpath", str(ROOT / "dist"),
            "--workpath", str(ROOT / "build/desktop/work"), "--specpath", str(ROOT / "build/desktop/spec")]
        for module in (*shared["EXCLUDED_MODULES"], "tkinter"):
            command += ["--exclude-module", module]
        command.append(str(ROOT / "packaging/desktop/entry.py"))
        subprocess.run(command, check=True)
    if not args.backend_only:
        subprocess.run(["npm.cmd", "run", "make", "--", "--platform=win32", "--arch=x64"], cwd=ROOT / "desktop", check=True)


if __name__ == "__main__":
    main()
