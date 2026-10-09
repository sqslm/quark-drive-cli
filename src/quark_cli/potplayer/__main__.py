"""``python -m quark_cli.potplayer``: start, reuse, stop, or log in the PotPlayer bridge."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from ..errors import QuarkError
from . import SERVICE_VERSION
from .login_gui import QrLoginWindow
from .plugin_state import credential_path
from .service import EXIT_OK, ensure_service, run_service, stop_service


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m quark_cli.potplayer",
        description="夸克网盘 PotPlayer 本地桥接服务（仅绑定 127.0.0.1）",
    )
    parser.add_argument("--version", action="version", version=f"quark-potplayer {SERVICE_VERSION}")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--serve", action="store_true", help="在当前进程运行服务")
    action.add_argument("--ensure-service", action="store_true", help="确保后台服务已运行，输出 {base_url, token}")
    action.add_argument("--stop", action="store_true", help="停止当前用户的后台服务")
    action.add_argument("--login", action="store_true", help="打开扫码窗口，登录插件自己的凭证（不改动 CLI Cookie）")
    return parser


def login() -> int:
    """Open the QR window and store the bridge's own credential.

    The window shares :class:`QRLogin` with the CLI but writes only the plugin
    credential, so the ``quark`` Cookie is never read or replaced.
    """
    QrLoginWindow(cookie_path=credential_path()).run()
    print("插件凭证已保存；quark CLI 的 Cookie 不受影响")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.serve:
        return run_service()
    if arguments.login:
        try:
            return login()
        except (QuarkError, OSError) as exc:
            print(str(exc) or "扫码登录失败", file=sys.stderr)
            return 1
    if arguments.ensure_service:
        try:
            info = ensure_service()
        except (QuarkError, OSError) as exc:
            print(str(exc) or "无法启动夸克网盘本地服务", file=sys.stderr)
            return 1
        print(json.dumps(info, ensure_ascii=False, separators=(",", ":")))
        return EXIT_OK
    return stop_service()


if __name__ == "__main__":
    raise SystemExit(main())
