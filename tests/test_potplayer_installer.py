"""Installation logic tests: temp directories only, no real PotPlayer is touched."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging" / "potplayer"))

import install_core as core  # noqa: E402
import installer  # noqa: E402
import launcher_entry  # noqa: E402
import service_entry  # noqa: E402
import uninstall_entry  # noqa: E402


def make_payload(root: Path) -> Path:
    payload = root / "payload"
    payload.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(payload / "component.zip", "w") as archive:
        archive.writestr("QuarkPotPlayer.exe", b"MZ fake")
        archive.writestr("_internal/python313.dll", b"dll")
    (payload / "build-info.json").write_text(json.dumps({"version": "9.9.9"}), encoding="utf-8")
    (payload / "launcher").mkdir(exist_ok=True)
    (payload / "launcher" / core.LAUNCHER_NAME).write_bytes(b"MZ launcher")
    return payload


def make_potplayer(root: Path) -> Path:
    directory = root / "PotPlayer"
    (directory / "Extension" / "Media" / "UrlList").mkdir(parents=True)
    (directory / "Extension" / "Media" / "PlayParse").mkdir(parents=True)
    (directory / "PotPlayerMini64.exe").write_bytes(b"MZ")
    return directory


def make_as(root: Path) -> list[core.AsFile]:
    directory = root / "as"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "MediaUrlList - Quark网盘.as").write_text("// urllist", encoding="utf-8")
    (directory / "MediaPlayParse - Quark网盘.as").write_text("// playparse", encoding="utf-8")
    (directory / "MediaUrlList - Quark网盘.ico").write_bytes(b"ico")
    return core.collect_as_files(directory)


class RoutingTests(unittest.TestCase):
    def test_standalone_uninstall_keeps_silent_flag_on_the_temporary_copy(self):
        with mock.patch.object(core, 'needs_temp_relaunch', return_value=True), \
             mock.patch.object(core, 'relaunch_uninstall_from_temp', return_value=Path('temp.exe')) as relaunch:
            self.assertEqual(uninstall_entry._run(['--uninstall', '--silent', '--install-dir', 'E:/fixture']), 0)
        self.assertIn('--silent', relaunch.call_args.kwargs['extra'])

    def test_targets_follow_file_name_prefix(self) -> None:
        self.assertEqual(core.as_target_for("MediaUrlList - Quark网盘.as"), "Media/UrlList")
        self.assertEqual(core.as_target_for("MediaPlayParse - Quark网盘.as"), "Media/PlayParse")
        self.assertEqual(core.as_target_for("SubtitleSearch - Quark网盘.as"), "Subtitle/Search")

    def test_unknown_prefix_is_rejected(self) -> None:
        with self.assertRaises(core.InstallError):
            core.as_target_for("Whatever - Quark.as")

    def test_collect_pairs_icons_and_ignores_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "as"
            directory.mkdir()
            (directory / "MediaUrlList - Quark.as").write_text("// a", encoding="utf-8")
            (directory / "MediaUrlList - Quark.ico").write_bytes(b"i")
            (directory / "notes.txt").write_text("x", encoding="utf-8")
            names = [item.name for item in core.collect_as_files(directory)]
            self.assertEqual(names, ["MediaUrlList - Quark.as", "MediaUrlList - Quark.ico"])

    def test_extension_subdir_is_below_the_players_extension_folder(self) -> None:
        self.assertEqual(
            core.extension_subdir(Path("C:/PotPlayer"), "Media/UrlList"),
            Path("C:/PotPlayer/Extension/Media/UrlList"),
        )


class DetectionTests(unittest.TestCase):
    def test_resolve_accepts_folder_and_exe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_potplayer(Path(tmp))
            self.assertEqual(core.resolve_potplayer_dir(directory), directory)
            self.assertEqual(core.resolve_potplayer_dir(directory / "PotPlayerMini64.exe"), directory)

    def test_resolve_rejects_folder_without_extension_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            with self.assertRaises(core.InstallError):
                core.resolve_potplayer_dir(empty)

    def test_explicit_and_saved_paths_win_over_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = make_potplayer(Path(tmp))
            self.assertEqual(core.detect_potplayer_dir(explicit=directory), directory)
            self.assertEqual(core.detect_potplayer_dir(saved=str(directory / "PotPlayerMini64.exe")), directory)

    def test_an_invalid_explicit_directory_is_refused_without_any_probe(self) -> None:
        # An explicit choice is authoritative: a bad one must raise instead of
        # silently installing into a different, auto-detected player. The search
        # itself must not even be attempted.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = make_potplayer(root)
            empty = root / "empty"
            empty.mkdir()
            with mock.patch.object(core, "_registry_program_dirs",
                                   side_effect=AssertionError("registry probed")), \
                 mock.patch.object(core, "_registry_uninstall_icons",
                                   side_effect=AssertionError("uninstall registry probed")), \
                 mock.patch.object(core, "_common_install_dirs",
                                   side_effect=AssertionError("common folders probed")):
                with self.assertRaises(core.InstallError):
                    core.detect_potplayer_dir(explicit=root / "missing", saved=str(good))
                with self.assertRaises(core.InstallError):
                    core.detect_potplayer_dir(explicit=empty, saved=str(good))

    def test_auto_detection_skips_a_stale_saved_path_and_keeps_looking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = make_potplayer(root)
            with mock.patch.object(core, "_registry_program_dirs", return_value=[]), \
                 mock.patch.object(core, "_registry_uninstall_icons", return_value=[]), \
                 mock.patch.object(core, "_common_install_dirs", return_value=[good]):
                self.assertEqual(core.detect_potplayer_dir(saved=str(root / "gone")), good)
            with mock.patch.object(core, "_registry_program_dirs", return_value=[]), \
                 mock.patch.object(core, "_registry_uninstall_icons", return_value=[]), \
                 mock.patch.object(core, "_common_install_dirs", return_value=[]):
                self.assertIsNone(core.detect_potplayer_dir(saved=str(root / "gone")))

    def test_user_roots_come_from_environment(self) -> None:
        env = {"LOCALAPPDATA": r"C:\Users\u\AppData\Local", "APPDATA": r"C:\Users\u\AppData\Roaming"}
        self.assertEqual(core.default_install_dir(env, potplayer_dir=Path('E:/PotPlayer')), Path('E:/PotPlayer/Plugin/QuarkPotPlayer'))
        self.assertEqual(
            core.plugin_credential_dir(core.cli_config_dir(env)),
            Path(r"C:\Users\u\AppData\Roaming/quark-drive-cli/potplayer"),
        )
        self.assertEqual(
            core.default_start_menu_dir(env),
            Path(r"C:\Users\u\AppData\Roaming/Microsoft/Windows/Start Menu/Programs"),
        )

    def test_absolute_config_override_matches_the_bridge(self) -> None:
        env = {"QUARK_CONFIG_DIR": r"C:\custom\cfg", "APPDATA": r"C:\Users\u\AppData\Roaming"}
        self.assertEqual(core.plugin_credential_dir(core.cli_config_dir(env)), Path(r"C:\custom\cfg/potplayer"))
        relative = {"QUARK_CONFIG_DIR": "relative", "APPDATA": r"C:\Users\u\AppData\Roaming"}
        self.assertEqual(
            core.cli_config_dir(relative), Path(r"C:\Users\u\AppData\Roaming/quark-drive-cli")
        )


class _StubVar:
    """Minimal stand-in for a ``tkinter.StringVar`` (no Tk root in tests)."""

    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class DetectCallbackTests(unittest.TestCase):
    """The GUI 自动检测 callback reports a bad typed directory instead of raising."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def window(self, typed: str) -> installer.InstallerWindow:
        window = installer.InstallerWindow.__new__(installer.InstallerWindow)
        window.potplayer = _StubVar(typed)
        window.messages = []
        window.args = installer.build_parser().parse_args([])
        window.install_dir = _StubVar('')
        window.log = window.messages.append
        return window

    def test_typed_invalid_directory_is_reported_without_a_traceback(self) -> None:
        typed = self.root / "not-a-player"
        typed.mkdir()
        window = self.window(str(typed))
        window.detect()  # a Tk callback: it must never raise out of here
        self.assertEqual(window.potplayer.get(), str(typed))
        self.assertEqual(len(window.messages), 1)
        self.assertIn("目录无效", window.messages[0])
        self.assertIn(str(typed), window.messages[0])

    def test_typed_missing_directory_is_reported_without_a_traceback(self) -> None:
        window = self.window(str(self.root / "gone"))
        window.detect()
        self.assertEqual(len(window.messages), 1)
        self.assertIn("目录无效", window.messages[0])
        self.assertIn("目录不存在", window.messages[0])

    def test_typed_player_directory_is_kept(self) -> None:
        directory = make_potplayer(self.root)
        window = self.window(str(directory))
        window.detect()
        self.assertEqual(window.potplayer.get(), str(directory))
        self.assertEqual(window.messages, [f"已找到 PotPlayer：{directory}"])

    def test_blank_input_still_auto_detects(self) -> None:
        directory = make_potplayer(self.root)
        window = self.window("   ")
        with mock.patch.object(core, "_registry_program_dirs", return_value=[]), \
             mock.patch.object(core, "_registry_uninstall_icons", return_value=[]), \
             mock.patch.object(core, "_common_install_dirs", return_value=[directory]):
            window.detect()
        self.assertEqual(window.potplayer.get(), str(directory))
        self.assertEqual(window.messages, [f"已找到 PotPlayer：{directory}"])

    def test_blank_input_reports_when_nothing_is_found(self) -> None:
        window = self.window("")
        with mock.patch.object(core, "_registry_program_dirs", return_value=[]), \
             mock.patch.object(core, "_registry_uninstall_icons", return_value=[]), \
             mock.patch.object(core, "_common_install_dirs", return_value=[]):
            window.detect()
        self.assertEqual(window.messages, ["未自动找到 PotPlayer，请点击「浏览…」选择安装目录"])


class RelaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.install_dir = self.root / "install"
        self.install_dir.mkdir()
        self.setup = self.install_dir / core.SETUP_EXE_NAME
        self.setup.write_bytes(b"MZ")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _frozen_at(self, executable: Path):
        return mock.patch.object(installer.sys, "frozen", True, create=True), mock.patch.object(
            installer.sys, "executable", str(executable)
        )

    def test_installed_copy_delegates_to_temp(self) -> None:
        frozen, executable = self._frozen_at(self.setup)
        with frozen, executable:
            self.assertTrue(core.needs_temp_relaunch(self.install_dir, {}))
            # the flag prevents an endless chain of copies
            self.assertFalse(core.needs_temp_relaunch(self.install_dir, {core.RELAUNCH_FLAG: "1"}))
            self.assertFalse(core.needs_temp_relaunch(self.root / "elsewhere", {}))
        self.assertFalse(core.needs_temp_relaunch(self.install_dir, {}))

    def test_main_hands_the_uninstall_to_the_temp_copy(self) -> None:
        (self.install_dir / core.MANIFEST_NAME).write_text(json.dumps({"schema": core.MANIFEST_SCHEMA}), encoding="utf-8")
        frozen, executable = self._frozen_at(self.setup)
        bundle = mock.patch.object(installer.sys, "_MEIPASS", str(self.root), create=True)
        with frozen, executable, bundle, mock.patch.object(
            core, "relaunch_uninstall_from_temp", return_value=self.root / "tmp-copy.exe"
        ) as relaunch:
            code = installer.main(["--uninstall", "--silent", "--clear-credentials"])
        self.assertEqual(code, 0)
        install_dir, = relaunch.call_args.args
        self.assertTrue(core._same_path(install_dir, self.install_dir))
        self.assertTrue(relaunch.call_args.kwargs["silent"])
        self.assertIn("--clear-credentials", relaunch.call_args.kwargs["extra"])


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.payload = make_payload(self.root)
        self.potplayer = make_potplayer(self.root)
        self.install_dir = self.root / "install"
        self.as_files = make_as(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def install(self, version: str = "1.0.0") -> core.InstallRecord:
        return core.install(
            payload_dir=self.payload,
            install_dir=self.install_dir,
            potplayer_dir=self.potplayer,
            version=version,
            as_files=self.as_files,
            start_menu_dir=None,
            make_shortcut=False,
            registry=False,
        )

    def test_install_lays_out_component_scripts_and_manifest(self) -> None:
        record = self.install()
        self.assertTrue((self.install_dir / "app" / "QuarkPotPlayer.exe").is_file())
        self.assertTrue((self.install_dir / "app" / "_internal" / "python313.dll").is_file())
        self.assertTrue(
            (self.potplayer / "Extension" / "Media" / "UrlList" / "MediaUrlList - Quark网盘.as").is_file()
        )
        self.assertTrue(
            (self.potplayer / "Extension" / "Media" / "UrlList" / "MediaUrlList - Quark网盘.ico").is_file()
        )
        self.assertTrue(
            (self.potplayer / "Extension" / "Media" / "PlayParse" / "MediaPlayParse - Quark网盘.as").is_file()
        )
        manifest = core.read_manifest(self.install_dir)
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["install_dir"], str(self.install_dir))
        self.assertEqual(len(manifest["as_files"]), 3)
        self.assertFalse(record.upgraded)

    def test_launcher_lands_next_to_the_extension_folder(self) -> None:
        # The extensions call HostGetScriptFolder()\..\..\quark-potplayer.exe.
        self.install()
        launcher, path_file = core.launcher_paths(self.potplayer)
        self.assertEqual(launcher.parent, self.potplayer / "Extension")
        self.assertEqual(launcher.read_bytes(), b"MZ launcher")
        self.assertEqual(path_file.read_text(encoding="utf-8"), str(self.install_dir / "app" / core.COMPONENT_EXE))
        manifest = core.read_manifest(self.install_dir)
        self.assertEqual(manifest["launcher_files"], [str(launcher), str(path_file)])

    def test_missing_launcher_in_the_payload_is_refused(self) -> None:
        payload = make_payload(self.root)
        (payload / "launcher" / core.LAUNCHER_NAME).unlink()
        with self.assertRaises(core.InstallError):
            core.install(
                payload_dir=payload,
                install_dir=self.install_dir,
                potplayer_dir=self.potplayer,
                version="1.0.0",
                as_files=self.as_files,
                start_menu_dir=None,
                make_shortcut=False,
                registry=False,
            )

    def test_install_keeps_only_the_lightweight_uninstaller_not_the_setup(self) -> None:
        fake_setup = self.root / "QuarkPotPlayer-Setup.exe"
        fake_setup.write_bytes(b"MZ setup")
        core.install(
            payload_dir=self.payload,
            install_dir=self.install_dir,
            potplayer_dir=self.potplayer,
            version="1.0.0",
            as_files=self.as_files,
            self_exe=fake_setup,
            start_menu_dir=None,
            make_shortcut=False,
            registry=False,
        )
        self.assertFalse((self.install_dir / core.SETUP_EXE_NAME).exists())
        self.assertEqual((self.install_dir / core.UNINSTALL_EXE_NAME).read_bytes(), b'MZ launcher')

    def test_upgrade_replaces_component_and_keeps_credentials(self) -> None:
        config_dir = self.root / "config"
        credentials = core.plugin_credential_dir(config_dir)
        credentials.mkdir(parents=True)
        (credentials / "cookie").write_text("[]", encoding="utf-8")
        self.install("1.0.0")
        stale = self.install_dir / "app" / "stale.bin"
        stale.write_bytes(b"old")
        record = self.install("2.0.0")
        self.assertTrue(record.upgraded)
        self.assertFalse(stale.exists())
        self.assertEqual(core.read_manifest(self.install_dir)["version"], "2.0.0")
        self.assertTrue((credentials / "cookie").is_file())

    def test_payload_zip_traversal_is_refused(self) -> None:
        payload = self.root / "evil"
        payload.mkdir()
        with zipfile.ZipFile(payload / "component.zip", "w") as archive:
            archive.writestr("../escaped.exe", b"MZ")
        with self.assertRaises(core.InstallError):
            core.install(
                payload_dir=payload,
                install_dir=self.install_dir,
                potplayer_dir=self.potplayer,
                version="1.0.0",
                as_files=self.as_files,
                start_menu_dir=None,
                make_shortcut=False,
                registry=False,
            )
        self.assertFalse((self.root / "escaped.exe").exists())

    def test_missing_component_is_refused(self) -> None:
        with self.assertRaises(core.InstallError):
            core.install(
                payload_dir=self.root / "nowhere",
                install_dir=self.install_dir,
                potplayer_dir=self.potplayer,
                version="1.0.0",
                as_files=[],
                start_menu_dir=None,
                make_shortcut=False,
                registry=False,
            )

    def test_install_refuses_a_populated_directory_it_does_not_own(self) -> None:
        foreign = self.root / "family"
        foreign.mkdir()
        sentinel = foreign / "photos.txt"
        sentinel.write_text("mine", encoding="utf-8")
        with self.assertRaises(core.InstallError) as caught:
            core.install(
                payload_dir=self.payload,
                install_dir=foreign,
                potplayer_dir=self.potplayer,
                version="1.0.0",
                as_files=self.as_files,
                start_menu_dir=None,
                make_shortcut=False,
                registry=False,
            )
        self.assertIn("非空", str(caught.exception))
        self.assertTrue(sentinel.is_file())
        self.assertEqual([entry.name for entry in foreign.iterdir()], ["photos.txt"])

    def test_install_accepts_an_empty_directory(self) -> None:
        empty = self.root / "target"
        empty.mkdir()
        core.install(
            payload_dir=self.payload,
            install_dir=empty,
            potplayer_dir=self.potplayer,
            version="1.0.0",
            as_files=self.as_files,
            start_menu_dir=None,
            make_shortcut=False,
            registry=False,
        )
        self.assertTrue((empty / "app" / core.COMPONENT_EXE).is_file())

    def test_missing_component_never_deletes_the_install_directory(self) -> None:
        # Upgrading a directory the user has since dropped their own files into: a
        # payload without the component must not wipe those files.
        self.install()
        keep = self.install_dir / "keep.txt"
        keep.write_text("keep", encoding="utf-8")
        broken = self.root / "broken"
        broken.mkdir()
        with zipfile.ZipFile(broken / "component.zip", "w") as archive:
            archive.writestr("_internal/python313.dll", b"dll")
        with self.assertRaises(core.InstallError):
            core.install(
                payload_dir=broken,
                install_dir=self.install_dir,
                potplayer_dir=self.potplayer,
                version="2.0.0",
                as_files=self.as_files,
                start_menu_dir=None,
                make_shortcut=False,
                registry=False,
            )
        self.assertTrue(keep.is_file())
        self.assertTrue(self.install_dir.is_dir())
        self.assertFalse((self.install_dir / "app").exists())

    def test_shortcut_opens_the_player_not_the_bridge(self) -> None:
        # The bridge needs an action argument, so the Start Menu entry must launch
        # PotPlayer itself (the extension lives in its URL list).
        start_menu = self.root / "StartMenu"
        with mock.patch.object(core, "write_shortcut") as write:
            record = core.install(
                payload_dir=self.payload,
                install_dir=self.install_dir,
                potplayer_dir=self.potplayer,
                version="1.0.0",
                as_files=self.as_files,
                start_menu_dir=start_menu,
                make_shortcut=True,
                registry=False,
            )
        write.assert_called_once()
        self.assertEqual(write.call_args.kwargs["target"], self.potplayer / "PotPlayerMini64.exe")
        self.assertEqual(write.call_args.kwargs["icon"], self.install_dir / "app" / core.COMPONENT_EXE)
        self.assertEqual(record.shortcut, str(start_menu / core.START_MENU_NAME))


class ReplacementTests(unittest.TestCase):
    def test_locked_component_is_reported_and_later_removable(self) -> None:
        # A running bridge holds its own exe; Windows then refuses the delete.
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "app"
            app_dir.mkdir()
            executable = app_dir / core.COMPONENT_EXE
            executable.write_bytes(b"MZ")
            handle = executable.open("rb")
            try:
                with self.assertRaises(core.InstallError) as caught:
                    core._remove_tree(app_dir, attempts=2, delay=0)
            finally:
                handle.close()
            self.assertIn("被占用", str(caught.exception))
            core._remove_tree(app_dir, attempts=5, delay=0.05)
            self.assertFalse(app_dir.exists())


class UninstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.payload = make_payload(self.root)
        self.potplayer = make_potplayer(self.root)
        self.install_dir = self.root / "install"
        self.config_dir = self.root / "config"
        self.as_files = make_as(self.root)
        core.install(
            payload_dir=self.payload,
            install_dir=self.install_dir,
            potplayer_dir=self.potplayer,
            version="1.0.0",
            as_files=self.as_files,
            start_menu_dir=None,
            make_shortcut=False,
            registry=False,
            config_dir=self.config_dir,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def uninstall(self, *, clear_credentials: bool = False) -> core.UninstallReport:
        return core.uninstall(
            install_dir=self.install_dir,
            clear_credentials=clear_credentials,
            config_dir=self.config_dir,
            registry=False,
        )

    def test_uninstall_removes_its_own_files_only(self) -> None:
        (self.potplayer / "Extension" / "Media" / "UrlList" / "MediaUrlList - Other.as").write_text(
            "// someone else", encoding="utf-8"
        )
        (self.potplayer / "Extension" / "Media" / "PlayParse" / "MediaPlayParse - YouTube.as").write_text(
            "// bundled", encoding="utf-8"
        )
        foreign = self.potplayer / "Extension" / "other-tool.exe"
        foreign.write_bytes(b"MZ")
        launcher, path_file = core.launcher_paths(self.potplayer)
        report = self.uninstall()
        self.assertFalse(self.install_dir.exists())
        self.assertFalse(
            (self.potplayer / "Extension" / "Media" / "UrlList" / "MediaUrlList - Quark网盘.as").exists()
        )
        self.assertFalse(launcher.exists())
        self.assertFalse(path_file.exists())
        self.assertTrue(
            (self.potplayer / "Extension" / "Media" / "UrlList" / "MediaUrlList - Other.as").is_file()
        )
        self.assertTrue(
            (self.potplayer / "Extension" / "Media" / "PlayParse" / "MediaPlayParse - YouTube.as").is_file()
        )
        self.assertTrue(foreign.is_file())
        self.assertTrue(report.removed)

    def test_uninstall_keeps_files_added_to_the_install_directory(self) -> None:
        kept_file = self.install_dir / "notes.txt"
        kept_file.write_text("mine", encoding="utf-8")
        kept_dir = self.install_dir / "my-data"
        kept_dir.mkdir()
        (kept_dir / "payload.bin").write_bytes(b"x")
        report = self.uninstall()
        self.assertTrue(kept_file.is_file())
        self.assertTrue((kept_dir / "payload.bin").is_file())
        self.assertFalse((self.install_dir / "app").exists())
        self.assertFalse((self.install_dir / core.MANIFEST_NAME).exists())
        self.assertTrue(self.install_dir.is_dir())
        self.assertTrue(report.removed)

    def test_uninstall_keeps_plugin_credentials_by_default(self) -> None:
        credentials = core.plugin_credential_dir(self.config_dir)
        credentials.mkdir(parents=True)
        (credentials / "cookie").write_text("[]", encoding="utf-8")
        (self.config_dir / "cookie").write_text("cli", encoding="utf-8")
        report = self.uninstall()
        self.assertTrue(report.kept_credentials)
        self.assertTrue((credentials / "cookie").is_file())
        self.assertTrue((self.config_dir / "cookie").is_file())

    def test_clear_credentials_never_touches_the_cli_cookie(self) -> None:
        credentials = core.plugin_credential_dir(self.config_dir)
        credentials.mkdir(parents=True)
        (credentials / "cookie").write_text("[]", encoding="utf-8")
        cli_cookie = self.config_dir / "cookie"
        cli_cookie.write_text("cli", encoding="utf-8")
        report = self.uninstall(clear_credentials=True)
        self.assertTrue(report.credentials_removed)
        self.assertFalse(credentials.exists())
        self.assertEqual(cli_cookie.read_text(encoding="utf-8"), "cli")

    def test_uninstall_without_manifest_deletes_nothing(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        sentinel = plain / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(core.InstallError):
            core.uninstall(install_dir=plain, registry=False)
        self.assertTrue(sentinel.is_file())

    def test_uninstall_refuses_a_tampered_manifest_target(self) -> None:
        manifest_path = self.install_dir / core.MANIFEST_NAME
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        victim = self.root / "victim"
        victim.mkdir()
        sentinel = victim / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        data["install_dir"] = str(victim)
        manifest_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(core.InstallError):
            core.uninstall(install_dir=self.install_dir, registry=False)
        self.assertTrue(sentinel.is_file())
        self.assertTrue(self.install_dir.exists())

    def test_manifest_path_forms_are_equivalent(self) -> None:
        # 8.3 short paths (%TEMP% is often E:\CPANQI~1\Temp) must not block removal.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(core._same_path(root / "install", root / "." / "install"))
            self.assertFalse(core._same_path(root / "install", root / "other"))
            manifest_path = self.install_dir / core.MANIFEST_NAME
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["install_dir"] = os.path.relpath(self.install_dir)
            manifest_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            report = core.uninstall(install_dir=self.install_dir, registry=False)
            self.assertFalse(self.install_dir.exists())
            self.assertTrue(report.removed)

    def test_entries_outside_the_player_directory_are_kept(self) -> None:
        outside = self.root / "outside.as"
        outside.write_text("// untouched", encoding="utf-8")
        manifest_path = self.install_dir / core.MANIFEST_NAME
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["as_files"] = [str(outside)]
        manifest_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        report = core.uninstall(install_dir=self.install_dir, registry=False)
        self.assertTrue(outside.is_file())
        self.assertTrue(any("越界" in note for note in report.notes))


class LauncherEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.component = self.root / "app" / core.COMPONENT_EXE
        self.component.parent.mkdir(parents=True)
        self.component.write_bytes(b"MZ component")
        self.extension = self.root / "PotPlayer" / "Extension"
        self.extension.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_recorded_path_wins_over_the_default_location(self) -> None:
        (self.extension / core.LAUNCHER_PATH_NAME).write_text(str(self.component), encoding="utf-8")
        env = {"LOCALAPPDATA": str(self.root / "nonexistent")}
        self.assertEqual(launcher_entry.component_path(self.extension, env), self.component)

    def test_default_location_and_override(self) -> None:
        local = self.root / "LocalAppData" / "QuarkPotPlayer" / "app"
        local.mkdir(parents=True)
        (local / core.COMPONENT_EXE).write_bytes(b"MZ")
        env = {"LOCALAPPDATA": str(self.root / "LocalAppData")}
        self.assertEqual(launcher_entry.component_path(self.extension, env), local / core.COMPONENT_EXE)
        override = self.root / "override.exe"
        override.write_bytes(b"MZ")
        env[launcher_entry.ENV_OVERRIDE] = str(override)
        self.assertEqual(launcher_entry.component_path(self.extension, env), override)
        self.assertIsNone(launcher_entry.component_path(self.extension, {}))

    def test_forwards_arguments_and_exit_code(self) -> None:
        (self.extension / core.LAUNCHER_PATH_NAME).write_text(str(self.component), encoding="utf-8")
        completed = subprocess.CompletedProcess([], 7)
        with mock.patch.object(launcher_entry, "launcher_directory", return_value=self.extension), mock.patch.object(
            launcher_entry.subprocess, "run", return_value=completed
        ) as run:
            code = launcher_entry.main(["quark-potplayer.exe", "--ensure-service"])
        self.assertEqual(code, 7)
        self.assertEqual(run.call_args.args[0], [str(self.component), "--ensure-service"])

    def test_missing_component_reports_cleanly(self) -> None:
        # No recorded path and no override: hide the machine's real
        # %LOCALAPPDATA% so the default-location lookup cannot find (and run) a
        # user's installed bridge.
        env = {
            key: value for key, value in os.environ.items()
            if key not in ("LOCALAPPDATA", launcher_entry.ENV_OVERRIDE)
        }
        with mock.patch.object(launcher_entry, "launcher_directory", return_value=self.extension), \
             mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(launcher_entry.main(["quark-potplayer.exe", "--ensure-service"]), 3)

    def test_bridge_reuses_the_launcher_stdio(self) -> None:
        # Windowed launcher, no console: without explicit handles Windows would hand
        # the console bridge its own console and swallow the --ensure-service JSON.
        (self.extension / core.LAUNCHER_PATH_NAME).write_text(str(self.component), encoding="utf-8")
        completed = subprocess.CompletedProcess([], 0)
        with mock.patch.object(launcher_entry, "launcher_directory", return_value=self.extension), mock.patch.object(
            launcher_entry.subprocess, "run", return_value=completed
        ) as run:
            launcher_entry.main(["quark-potplayer.exe", "--ensure-service"])
        self.assertIs(run.call_args.kwargs["stdout"], sys.stdout)
        self.assertIs(run.call_args.kwargs["stderr"], sys.stderr)


class EntryPointTests(unittest.TestCase):
    def test_spawn_helper_argument_shape_is_accepted(self) -> None:
        argv = ["C:/app/QuarkPotPlayer.exe", "-m", "quark_cli.potplayer", "--serve"]
        self.assertEqual(service_entry.normalize_argv(argv), ["C:/app/QuarkPotPlayer.exe", "--serve"])

    def test_plain_arguments_pass_through(self) -> None:
        argv = ["C:/app/QuarkPotPlayer.exe", "--ensure-service"]
        self.assertEqual(service_entry.normalize_argv(argv), argv)
        self.assertEqual(service_entry.normalize_argv(["-m", "something.else", "--serve"]), ["-m", "something.else", "--serve"])


class InstallerCliTests(unittest.TestCase):
    def test_payload_version_comes_from_build_info(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = make_payload(Path(tmp))
            (payload / "as").mkdir()
            (payload / "as" / "MediaUrlList - Quark.as").write_text("// a", encoding="utf-8")
            args = installer.build_parser().parse_args(["--payload", str(payload)])
            loaded = installer.load_payload(args)
            self.assertEqual(loaded.version, "9.9.9")
            self.assertEqual(loaded.as_dir, payload / "as")
            self.assertEqual([item.name for item in loaded.as_files()], ["MediaUrlList - Quark.as"])

    def test_empty_payload_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = make_payload(Path(tmp))
            (payload / "as").mkdir()
            args = installer.build_parser().parse_args(["--payload", str(payload)])
            with self.assertRaises(core.InstallError):
                installer.load_payload(args).as_files()

    def test_options_default_to_local_app_data_and_saved_player(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_dir = root / "install"
            potplayer = make_potplayer(root)
            core.install(
                payload_dir=make_payload(root),
                install_dir=install_dir,
                potplayer_dir=potplayer,
                version="1.0.0",
                as_files=make_as(root),
                start_menu_dir=None,
                make_shortcut=False,
                registry=False,
            )
            args = installer.build_parser().parse_args(["--install-dir", str(install_dir)])
            options = installer.options_from(args)
            self.assertEqual(options.potplayer_dir, potplayer)
            self.assertTrue(options.make_shortcut)
            self.assertFalse(options.clear_credentials)

    def test_unpacked_runs_never_copy_a_python_interpreter(self) -> None:
        self.assertIsNone(installer.self_executable())

    def test_uninstall_target_is_the_directory_holding_the_setup_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_dir = root / "custom"
            install_dir.mkdir()
            core.install(
                payload_dir=make_payload(root),
                install_dir=install_dir,
                potplayer_dir=make_potplayer(root),
                version="1.0.0",
                as_files=make_as(root),
                start_menu_dir=None,
                make_shortcut=False,
                registry=False,
            )
            setup = install_dir / core.SETUP_EXE_NAME
            setup.write_bytes(b"MZ setup")
            with mock.patch.object(installer.sys, "frozen", True, create=True), mock.patch.object(
                installer.sys, "executable", str(setup)
            ):
                self.assertEqual(installer.installed_location(), install_dir)
            with mock.patch.object(installer.sys, "frozen", True, create=True), mock.patch.object(
                installer.sys, "executable", str(root / "elsewhere" / core.SETUP_EXE_NAME)
            ):
                self.assertEqual(installer.installed_location(), core.default_install_dir())

    def test_installed_copy_uninstalls_without_extra_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_dir = root / "custom"
            install_dir.mkdir()
            player = make_potplayer(root)
            bundle = make_payload(root)
            (bundle / "as").mkdir()
            (bundle / "as" / "MediaUrlList - Quark网盘.as").write_text("// a", encoding="utf-8")
            core.install(
                payload_dir=bundle,
                install_dir=install_dir,
                potplayer_dir=player,
                version="1.0.0",
                as_files=make_as(root),
                start_menu_dir=None,
                make_shortcut=False,
                registry=False,
            )
            setup = install_dir / core.SETUP_EXE_NAME
            setup.write_bytes(b"MZ setup")
            with mock.patch.object(installer.sys, "frozen", True, create=True), mock.patch.object(
                installer.sys, "executable", str(setup)
            ), mock.patch.object(installer.sys, "_MEIPASS", str(bundle), create=True):
                args = installer.build_parser().parse_args(["--uninstall", "--silent", "--no-registry"])
                exit_code = installer.run_silent(args, installer.load_payload(args))
            self.assertEqual(exit_code, 0)
            self.assertFalse(install_dir.exists())
            self.assertFalse(
                (player / "Extension" / "Media" / "UrlList" / "MediaUrlList - Quark网盘.as").exists()
            )


if __name__ == "__main__":
    unittest.main()
