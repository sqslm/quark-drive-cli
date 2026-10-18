"""Tkinter installer / uninstaller for the Quark Drive PotPlayer bridge.

The same executable installs, upgrades and uninstalls:

* ``QuarkPotPlayer-Setup.exe``            install window (default)
* ``QuarkPotPlayer-Setup.exe --uninstall`` uninstall window
* ``--silent`` variants for scripted runs

The installer defaults to PotPlayer's own ``Plugin`` directory (registry in
``HKCU``); a protected player folder requires write permission. It refuses to delete anything absent from the manifest
its own install wrote. Tkinter is imported lazily so ``--silent`` runs without a
window station.
"""

from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import install_core as core

LOG_NAME = "QuarkPotPlayer-Setup.log"
DEFAULT_VERSION = "0.1.0"


@dataclass(frozen=True)
class Payload:
    directory: Path
    as_dir: Path
    version: str

    def as_files(self) -> list[core.AsFile]:
        files = core.collect_as_files(self.as_dir)
        if not files:
            raise core.InstallError(f"安装包不含扩展脚本：{self.as_dir}")
        return files

    def launcher(self) -> Path:
        launcher = self.directory / "launcher" / core.LAUNCHER_NAME
        if not launcher.is_file():
            raise core.InstallError(f"安装包不含启动器：{launcher}")
        return launcher


@dataclass
class Options:
    potplayer_dir: Path | None
    install_dir: Path
    make_shortcut: bool
    registry: bool
    open_player: bool
    clear_credentials: bool


# ------------------------------------------------------------------------ arguments


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="QuarkPotPlayer-Setup", description="夸克网盘 PotPlayer 插件安装程序")
    parser.add_argument("--uninstall", action="store_true", help="卸载本安装器创建的组件")
    parser.add_argument("--silent", action="store_true", help="不显示窗口，按参数直接执行")
    parser.add_argument("--potplayer-dir", type=Path, default=None, help="PotPlayer 安装目录（含 Extension）")
    parser.add_argument("--install-dir", type=Path, default=None, help="组件安装目录，默认 PotPlayer\\Plugin\\QuarkPotPlayer")
    parser.add_argument("--clear-credentials", action="store_true", help="卸载时同时清除插件凭证（不影响 CLI 登录）")
    parser.add_argument("--no-shortcut", action="store_true", help="不创建开始菜单快捷方式")
    parser.add_argument("--no-registry", action="store_true", help="不写入卸载注册表项")
    parser.add_argument("--no-open", action="store_true", help="安装完成后不打开 PotPlayer")
    # Development-only overrides; the packaged installer carries its own payload.
    parser.add_argument("--payload", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--as-dir", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--version", default=None, help=argparse.SUPPRESS)
    return parser


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_payload(args: argparse.Namespace) -> Payload:
    root = Path(args.payload) if args.payload else resource_root()
    as_dir = Path(args.as_dir) if args.as_dir else (root / "as" if (root / "as").is_dir() else source_root() / "potplayer")
    version = args.version or _read_version(root) or DEFAULT_VERSION
    return Payload(directory=root, as_dir=as_dir, version=version)


def _read_version(root: Path) -> str | None:
    try:
        data = json.loads((root / "build-info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version")
    return version if isinstance(version, str) and version else None


def installed_location() -> Path:
    """Where this setup copy lives, when it is the copy the install put in place.

    Add/Remove Programs runs ``<install_dir>\\QuarkPotPlayer-Setup.exe --uninstall``,
    so the directory holding this executable is the installed one, even when the
    user chose a non-default ``--install-dir``.
    """
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).parent
        if (executable_dir / core.MANIFEST_NAME).is_file():
            return executable_dir
    return core.default_install_dir()


def options_from(args: argparse.Namespace) -> Options:
    install_dir = Path(args.install_dir) if args.install_dir else installed_location()
    saved = core.saved_potplayer_dir(install_dir)
    potplayer = args.potplayer_dir
    if potplayer is None and saved:
        potplayer = Path(saved)
    if not args.install_dir and not args.uninstall:
        potplayer = core.detect_potplayer_dir(explicit=potplayer)
        install_dir = core.default_install_dir(potplayer_dir=potplayer)
    return Options(
        potplayer_dir=potplayer,
        install_dir=install_dir,
        make_shortcut=not args.no_shortcut,
        registry=not args.no_registry,
        open_player=not args.no_open,
        clear_credentials=args.clear_credentials,
    )


# ------------------------------------------------------------------------- operations


def self_executable() -> Path | None:
    """The setup executable to copy for the Add/Remove Programs entry (frozen only)."""
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable)
    return executable if executable.is_file() else None


def do_install(options: Options, payload: Payload, emit: Callable[[str], None]) -> str:
    potplayer = core.detect_potplayer_dir(explicit=options.potplayer_dir, saved=None)
    if potplayer is None:
        raise core.InstallError("未找到 PotPlayer，请手动选择其安装目录（应包含 PotPlayerMini64.exe 与 Extension）")
    if not options.install_dir.is_absolute():
        options.install_dir = potplayer / options.install_dir
    emit(f"PotPlayer：{potplayer}")
    emit(f"组件目录：{options.install_dir}")
    emit(f"组件版本：{payload.version}")
    if core.is_installed(options.install_dir):
        emit("检测到既有安装，按升级处理（保留插件凭证）")
    record = core.install(
        payload_dir=payload.directory,
        install_dir=options.install_dir,
        potplayer_dir=potplayer,
        version=payload.version,
        as_files=payload.as_files(),
        self_exe=self_executable(),
        start_menu_dir=core.default_start_menu_dir() if options.make_shortcut else None,
        make_shortcut=options.make_shortcut,
        registry=options.registry,
    )
    for item in record.as_files:
        emit(f"扩展脚本：{item}")
    if record.shortcut:
        emit(f"开始菜单：{record.shortcut}")
    emit("安装完成" if not record.upgraded else "升级完成")
    return str(potplayer)


def do_uninstall(options: Options, emit: Callable[[str], None]) -> str:
    install_dir = options.install_dir
    if not core.is_installed(install_dir) and core.read_manifest(install_dir) is None:
        raise core.InstallError(
            f"未找到本安装器创建的安装：{install_dir}。若组件装在其它目录，请用 --install-dir 指定。"
        )
    report = core.uninstall(
        install_dir=install_dir,
        clear_credentials=options.clear_credentials,
        start_menu_dir=core.default_start_menu_dir(),
        registry=options.registry,
    )
    for item in report.removed:
        emit(f"已删除：{item}")
    for note in report.notes:
        emit(f"注意：{note}")
    if report.credentials_removed:
        emit("已清除插件凭证（CLI 登录状态未受影响）")
    elif options.clear_credentials:
        emit("插件凭证未清除（目录不存在或无法删除）")
    else:
        emit("已保留插件凭证：下次安装无需重新扫码")
    emit("卸载完成")
    return str(install_dir)


def write_log(lines: Sequence[str]) -> Path | None:
    try:
        path = Path(tempfile.gettempdir()) / LOG_NAME
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def run_silent(args: argparse.Namespace, payload: Payload) -> int:
    options = options_from(args)
    lines: list[str] = []
    code = 0
    try:
        if args.uninstall:
            do_uninstall(options, lines.append)
        else:
            potplayer = do_install(options, payload, lines.append)
            if options.open_player:
                core.launch_potplayer(Path(potplayer))
    except (core.InstallError, OSError) as exc:
        lines.append(f"失败：{exc}")
        code = 4
    log = write_log(lines)
    for line in lines:
        print(line, file=sys.stderr)
    if log is not None:
        print(str(log), file=sys.stderr)
    return code


# ------------------------------------------------------------------------------- GUI


class InstallerWindow:
    def __init__(self, root, args: argparse.Namespace, payload: Payload) -> None:
        from tkinter import BooleanVar, StringVar, ttk

        self.root = root
        self.args = args
        self.payload = payload
        self.busy = False
        self.messages: queue.Queue[str] = queue.Queue()
        options = options_from(args)
        self.clear_credentials = BooleanVar(value=args.clear_credentials)
        self.make_shortcut = BooleanVar(value=options.make_shortcut)
        self.open_player = BooleanVar(value=options.open_player)
        self.potplayer = StringVar(value=str(options.potplayer_dir or ""))
        self.install_dir = StringVar(value=str(options.install_dir))

        root.title("夸克网盘 PotPlayer 插件安装程序")
        root.minsize(660, 460)
        frame = ttk.Frame(root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="PotPlayer 目录").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.potplayer).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frame, text="自动检测", command=self.detect).grid(row=0, column=2)
        ttk.Button(frame, text="浏览…", command=self.browse).grid(row=0, column=3, padx=(4, 0))

        ttk.Label(frame, text="组件安装目录").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frame, textvariable=self.install_dir).grid(row=1, column=1, columnspan=3, sticky="ew", padx=6, pady=(6, 0))

        ttk.Checkbutton(frame, text="创建开始菜单快捷方式", variable=self.make_shortcut).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        ttk.Checkbutton(frame, text="安装完成后打开 PotPlayer", variable=self.open_player).grid(
            row=3, column=0, columnspan=2, sticky="w"
        )
        ttk.Checkbutton(frame, text="卸载时同时清除插件凭证（不影响 CLI 登录）", variable=self.clear_credentials).grid(
            row=4, column=0, columnspan=4, sticky="w"
        )

        ttk.Button(frame, text="安装 / 升级", command=self.install).grid(row=5, column=1, sticky="e", pady=(10, 0))
        ttk.Button(frame, text="卸载", command=self.uninstall).grid(row=5, column=2, pady=(10, 0))
        ttk.Button(frame, text="退出", command=root.destroy).grid(row=5, column=3, padx=(4, 0), pady=(10, 0))

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(10, 4))
        from tkinter.scrolledtext import ScrolledText

        self.text = ScrolledText(frame, height=12, state="disabled", wrap="word")
        self.text.grid(row=7, column=0, columnspan=4, sticky="nsew")
        frame.rowconfigure(7, weight=1)

        self.detect()
        self.log(f"组件版本 {self.payload.version}")
        self.messages.put("ready")
        root.after(80, self.pump)

    # -- helpers
    def log(self, message: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", message + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    def emit(self, message: str) -> None:
        self.messages.put(message)

    def detect(self) -> None:
        """Fill the directory box from the typed path, else auto-detect.

        A typed path is authoritative for ``detect_potplayer_dir``, so an
        unusable one raises :class:`InstallError`. It is reported in the window
        instead of escaping this Tk callback, which would print a traceback and
        leave the user with no reason at all. An empty box keeps auto-detecting.
        """
        typed = self.potplayer.get().strip()
        try:
            found = core.detect_potplayer_dir(explicit=Path(typed) if typed else None)
        except core.InstallError as exc:
            self.log(f"目录无效：{exc}")
            return
        if found is None:
            self.log("未自动找到 PotPlayer，请点击「浏览…」选择安装目录")
            return
        self.potplayer.set(str(found))
        if not self.args.install_dir:
            self.install_dir.set(str(core.default_install_dir(potplayer_dir=found)))
        self.log(f"已找到 PotPlayer：{found}")

    def browse(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(title="选择 PotPlayer 安装目录")
        if not chosen:
            return
        try:
            directory = core.resolve_potplayer_dir(Path(chosen))
        except core.InstallError as exc:
            self.log(f"无效目录：{exc}")
            return
        self.potplayer.set(str(directory))
        if not self.args.install_dir:
            self.install_dir.set(str(core.default_install_dir(potplayer_dir=directory)))
        self.log(f"已选择：{directory}")

    def options(self) -> Options:
        raw = self.potplayer.get().strip()
        return Options(
            potplayer_dir=Path(raw) if raw else None,
            install_dir=Path(self.install_dir.get().strip()),
            make_shortcut=self.make_shortcut.get(),
            registry=not self.args.no_registry,
            open_player=self.open_player.get(),
            clear_credentials=self.clear_credentials.get(),
        )

    # -- actions
    def _run(self, job: Callable[[], None]) -> None:
        if self.busy:
            self.log("正在执行上一项操作，请稍候")
            return
        self.busy = True
        self.progress.configure(mode="indeterminate")
        self.progress.start(60)

        def worker() -> None:
            try:
                job()
            except (core.InstallError, OSError) as exc:
                self.emit(f"失败：{exc}")
            finally:
                self.messages.put("__done__")

        threading.Thread(target=worker, daemon=True).start()

    def install(self) -> None:
        try:
            options = self.options()
        except core.InstallError as exc:
            self.log(str(exc))
            return
        payload = self.payload

        def job() -> None:
            potplayer = do_install(options, payload, self.emit)
            if options.open_player:
                self.emit("正在打开 PotPlayer…")
                core.launch_potplayer(Path(potplayer))

        self.log("开始安装…")
        self._run(job)

    def uninstall(self) -> None:
        from tkinter import messagebox

        options = self.options()
        extra = "，并清除插件凭证" if options.clear_credentials else "，保留插件凭证"
        if not messagebox.askyesno("确认卸载", f"将删除本插件组件与扩展脚本{extra}。继续？"):
            return

        def job() -> None:
            do_uninstall(options, self.emit)

        self.log("开始卸载…")
        self._run(job)

    def pump(self) -> None:
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            if message == "__done__":
                self.busy = False
                self.progress.stop()
                self.progress.configure(mode="determinate", value=100)
                continue
            if message != "ready":
                self.log(message)
        self.root.after(80, self.pump)


def run_gui(args: argparse.Namespace, payload: Payload) -> int:
    from tkinter import Tk

    root = Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass
    InstallerWindow(root, args, payload)
    root.mainloop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = load_payload(args)
    except core.InstallError as exc:
        print(str(exc), file=sys.stderr)
        return 4
    if args.uninstall:
        options = options_from(args)
        if core.needs_temp_relaunch(options.install_dir):
            extra = ["--silent"] if args.silent else []
            if args.clear_credentials:
                extra.append("--clear-credentials")
            if args.no_registry:
                extra.append("--no-registry")
            copy = core.relaunch_uninstall_from_temp(options.install_dir, silent=args.silent, extra=extra)
            if copy is not None:
                print(f"卸载由临时副本执行：{copy}", file=sys.stderr)
                return 0
    if args.silent:
        return run_silent(args, payload)
    return run_gui(args, payload)


if __name__ == "__main__":
    raise SystemExit(main())
