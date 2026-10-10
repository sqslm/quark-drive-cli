"""Tests for the launcher's ``--player-pid`` local action.

The frozen launcher answers this action itself (no bridge, no cookie, no
network): it walks its own parent chain, skips a ``--onefile`` bootloader
process and reports the first ancestor that really owns a PotPlayer window.
Everything here is pure logic with the Win32 calls replaced by stubs, so it runs
on any platform and never touches a real player.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging" / "potplayer"))

import launcher_entry  # noqa: E402

OWN = r"C:\Pot\Extension\quark-potplayer.exe"
PLAYER = r"C:\Pot\PotPlayerMini64.exe"


class ResolvePlayerPidTests(unittest.TestCase):
    """The ancestry walk, with every OS call stubbed."""

    def _resolve(self, *, ppid, images, parents=(), windows=()):
        images = dict(images)
        parents = dict(parents)
        windows = set(windows)

        def process_image(pid):
            return images.get(pid)

        def toolhelp_parent(pid):
            return parents.get(pid)

        def owns_window(pid):
            return pid in windows

        with mock.patch.object(launcher_entry, "SUPPORTED", True), mock.patch.object(
            sys, "executable", OWN
        ), mock.patch.object(launcher_entry.os, "getppid", return_value=ppid), mock.patch.object(
            launcher_entry, "_process_image", side_effect=process_image
        ), mock.patch.object(
            launcher_entry, "_toolhelp_parent", side_effect=toolhelp_parent
        ), mock.patch.object(
            launcher_entry, "_owns_player_window", side_effect=owns_window
        ):
            return launcher_entry.resolve_player_pid()

    def test_direct_player_parent_is_reported(self):
        self.assertEqual(
            self._resolve(ppid=77, images={77: PLAYER}, windows={77}),
            77,
        )

    def test_onefile_bootloader_is_skipped_to_the_player(self):
        # getppid is the bootloader (same image as us); its parent is the player.
        self.assertEqual(
            self._resolve(
                ppid=900,
                images={900: OWN, 77: PLAYER},
                parents={900: 77},
                windows={77},
            ),
            77,
        )

    def test_a_non_player_parent_is_refused(self):
        # A shell parent owns no PotPlayer window, so it is refused even though a
        # real player window exists elsewhere: the walk never adopts another
        # process's window, and the image name (here cmd.exe) is never a whitelist.
        self.assertEqual(
            self._resolve(
                ppid=50,
                images={50: r"C:\Windows\System32\cmd.exe", 77: PLAYER},
                windows={77},
            ),
            0,
        )

    def test_a_player_image_without_a_window_is_refused(self):
        # Only a window whose owner is the candidate counts; the image name alone
        # is never enough.
        self.assertEqual(self._resolve(ppid=77, images={77: PLAYER}, windows=set()), 0)

    def test_unreadable_parent_image_is_zero(self):
        self.assertEqual(self._resolve(ppid=77, images={}), 0)

    def test_dead_or_reused_parent_is_zero(self):
        self.assertEqual(self._resolve(ppid=0, images={77: PLAYER}, windows={77}), 0)

    def test_a_bootloader_whose_parent_is_unknown_is_zero(self):
        self.assertEqual(self._resolve(ppid=900, images={900: OWN}, parents={}), 0)

    def test_an_endless_self_image_chain_stops_at_the_bound(self):
        images = {pid: OWN for pid in (900, 901, 902, 903, 904)}
        parents = {900: 901, 901: 902, 902: 903, 903: 904, 904: 905}
        self.assertEqual(self._resolve(ppid=900, images=images, parents=parents), 0)

    def test_a_skipped_bootloader_then_a_non_player_is_refused(self):
        self.assertEqual(
            self._resolve(
                ppid=900, images={900: OWN, 40: r"C:\Windows\explorer.exe"}, parents={900: 40}
            ),
            0,
        )

    def test_unsupported_platform_is_zero(self):
        with mock.patch.object(launcher_entry, "SUPPORTED", False):
            self.assertEqual(launcher_entry.resolve_player_pid(), 0)


class EmitPlayerPidTests(unittest.TestCase):
    def test_emits_one_json_line(self):
        stream = io.StringIO()
        with mock.patch.object(launcher_entry, "_stdout", return_value=stream):
            launcher_entry.emit_player_pid(4242)
        self.assertEqual(stream.getvalue(), '{"player_pid": 4242}\n')
        self.assertEqual(json.loads(stream.getvalue()), {"player_pid": 4242})

    def test_zero_is_emitted_as_a_valid_json_line(self):
        stream = io.StringIO()
        with mock.patch.object(launcher_entry, "_stdout", return_value=stream):
            launcher_entry.emit_player_pid(0)
        self.assertEqual(stream.getvalue(), '{"player_pid": 0}\n')

    def test_missing_stdout_is_not_an_error(self):
        with mock.patch.object(launcher_entry, "_stdout", return_value=None):
            launcher_entry.emit_player_pid(1)  # must not raise


class MainDispatchTests(unittest.TestCase):
    def test_player_pid_is_answered_locally_and_never_reaches_the_bridge(self):
        stream = io.StringIO()
        with mock.patch.object(
            launcher_entry, "resolve_player_pid", return_value=777
        ), mock.patch.object(launcher_entry, "component_path") as component, mock.patch.object(
            launcher_entry.subprocess, "run"
        ) as run, mock.patch.object(launcher_entry, "_stdout", return_value=stream):
            code = launcher_entry.main([OWN, launcher_entry.PID_ACTION])
        self.assertEqual(code, 0)
        component.assert_not_called()
        run.assert_not_called()
        self.assertEqual(stream.getvalue(), '{"player_pid": 777}\n')

    def test_other_arguments_still_forward_to_the_bridge(self):
        completed = mock.Mock(returncode=0)
        with mock.patch.object(
            launcher_entry, "component_path", return_value=Path(r"C:\bridge\QuarkPotPlayer.exe")
        ), mock.patch.object(launcher_entry.subprocess, "run", return_value=completed) as run:
            code = launcher_entry.main([OWN, "--ensure-service"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0][1:], ["--ensure-service"])
