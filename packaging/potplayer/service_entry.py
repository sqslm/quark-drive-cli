"""Frozen entry point for the Quark Drive bridge component (``QuarkPotPlayer.exe``).

The bridge is packaged **windowed** (``console=False``) so an AS call can never
flash a console window. ``HostExecuteProgram`` returns the child's stdout, so the
extension always calls us with a pipe; a windowed PyInstaller build keeps
``sys.stdout`` bound to that pipe (verified: ``print`` and ``os.write(1)`` both
deliver). This module only restores the stream if the interpreter dropped it, and
forwards the arguments unchanged.

``service._spawn_service`` starts the background service as
``[sys.executable, "-m", "quark_cli.potplayer", "--serve"]``. A frozen build has
the package compiled in, so the ``-m quark_cli.potplayer`` selector is dropped
before dispatch; that keeps the business module free of freeze-specific branches.
"""

from __future__ import annotations

import os
import sys
from typing import Sequence

MODULE_SELECTOR = ("-m", "quark_cli.potplayer")


def restore_stdout() -> None:
    """Re-bind ``sys.stdout``/``sys.stderr`` when windowed packaging dropped them."""
    for name, fileno in (("stdout", 1), ("stderr", 2)):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                stream = os.fdopen(fileno, "w", encoding="utf-8", errors="replace", newline="\n", buffering=1)
            except OSError:
                continue
            setattr(sys, name, stream)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            pass


def normalize_argv(argv: Sequence[str]) -> list[str]:
    """Drop a ``-m quark_cli.potplayer`` selector produced by the spawn helper."""
    args = list(argv)
    if len(args) >= 3 and tuple(args[1:3]) == MODULE_SELECTOR:
        del args[1:3]
    return args


def main(argv: Sequence[str] | None = None) -> int:
    restore_stdout()
    args = normalize_argv(sys.argv if argv is None else argv)
    # The installed onedir executable starts without onefile extraction. Keep
    # this local action ahead of every bridge/GUI/account import.
    if args[1:] == ['--player-pid']:
        from launcher_entry import emit_player_pid, resolve_player_pid
        emit_player_pid(resolve_player_pid())
        return 0
    from quark_cli.potplayer.__main__ import main as bridge_main

    return bridge_main(args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
