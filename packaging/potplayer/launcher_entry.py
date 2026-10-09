"""``quark-potplayer.exe``: the launcher the AngelScript extensions find and call.

The extensions compute the launcher path themselves::

    folder = HostGetScriptFolder()                  # <PotPlayer>\\Extension\\Media\\UrlList
    launcher = folder + "\\\\..\\\\..\\\\quark-potplayer.exe"     # <PotPlayer>\\Extension
    result = HostExecuteProgram(launcher, "--ensure-service")

The bridge itself is installed per user under ``%LOCALAPPDATA%\\QuarkPotPlayer\\app``,
so this small executable sits next to the player's ``Extension`` folder and forwards
``argv`` to the bridge, re-passing our own standard handles so the bridge's stdout
(the ``--ensure-service`` JSON line) reaches PotPlayer unchanged; the exit code is
passed through. Passing the handles explicitly is required: this launcher is a
windowed build with no console, and Windows would otherwise hand a console child its
own invisible console instead of PotPlayer's pipe (``inherit_stdio``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

COMPONENT_NAME = "QuarkPotPlayer.exe"
PATH_FILE_NAME = "quark-potplayer.path"
ENV_OVERRIDE = "QUARK_POTPLAYER_COMPONENT"


def launcher_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def component_path(launcher_dir: Path | None = None, env: dict[str, str] | None = None) -> Path | None:
    """Resolve the installed bridge: explicit override, recorded path, then default."""
    source = os.environ if env is None else env
    directory = launcher_directory() if launcher_dir is None else Path(launcher_dir)

    override = source.get(ENV_OVERRIDE, "").strip()
    if override and Path(override).is_file():
        return Path(override)

    recorded = directory / PATH_FILE_NAME
    try:
        candidate = Path(recorded.read_text(encoding="utf-8").strip())
    except OSError:
        candidate = None
    if candidate is not None and candidate.is_file():
        return candidate

    base = source.get("LOCALAPPDATA", "").strip()
    if base:
        default = Path(base) / "QuarkPotPlayer" / "app" / COMPONENT_NAME
        if default.is_file():
            return default
    return None


def inherit_stdio() -> dict[str, object]:
    """Reuse our own standard handles when spawning the bridge.

    A windowed launcher has no console. Windows gives a console child its own
    (invisible) console unless ``STARTF_USESTDHANDLES`` is set, which would
    swallow the ``--ensure-service`` JSON. Passing the streams we were handed
    keeps the bridge on PotPlayer's pipe.
    """
    streams: dict[str, object] = {}
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None:
            streams[name] = stream
    return streams


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    component = component_path()
    if component is None:
        print("未找到夸克网盘桥接组件，请重新运行插件安装程序。", file=sys.stderr)
        return 3
    return subprocess.run([str(component), *args[1:]], **inherit_stdio()).returncode


if __name__ == "__main__":
    raise SystemExit(main())
