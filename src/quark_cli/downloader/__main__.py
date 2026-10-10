"""``python -m quark_cli.downloader``: open the standalone download window.

The window browses the signed-in account's own drive and downloads one selected
file or one selected folder tree. It uses the same Cookie as the ``quark`` CLI
(``auth.credential_path()``), so ``quark auth login`` and the window's own
``扫码登录`` are interchangeable.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from ..errors import LocalIOError
from .app import DownloaderWindow


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="python -m quark_cli.downloader",
        description="夸克网盘下载器（图形窗口，仅处理自己的网盘）",
    )


def main(argv: Sequence[str] | None = None) -> int:
    _parser().parse_args(argv)
    try:
        DownloaderWindow().run()
    except LocalIOError as error:
        print(str(error) or "无法打开下载窗口", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
