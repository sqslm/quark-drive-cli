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
from .gui_common import EXIT_OK
from .status_gui import run_status_window
from .highlights_gui import run_highlights_window


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
    action.add_argument("--status-window", action="store_true", help="打开播放状态与诊断窗口（不启动本地服务）")
    action.add_argument('--highlights-window', action='store_true', help='仅打开精彩标记窗口（不启动本地服务）')
    action.add_argument('--series-settings-window', action='store_true', help='打开独立剧集片头片尾设置窗口')
    parser.add_argument('--series-fid')
    parser.add_argument('--account-id')
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
        from .service import run_service
        return run_service()
    if arguments.series_settings_window:
        from .series_gui import run_series_settings
        return run_series_settings(arguments.series_fid, arguments.account_id)
    if arguments.highlights_window:
        return run_highlights_window()
    if arguments.status_window:
        # Independent GUI process only: never starts the service or a login.
        return run_status_window()
    if arguments.login:
        try:
            return login()
        except (QuarkError, OSError) as exc:
            print(str(exc) or "扫码登录失败", file=sys.stderr)
            return 1
    if arguments.ensure_service:
        from .service import ensure_service
        try:
            info = ensure_service()
        except (QuarkError, OSError) as exc:
            print(str(exc) or "无法启动夸克网盘本地服务", file=sys.stderr)
            return 1
        print(json.dumps(info, ensure_ascii=False, separators=(",", ":")))
        return EXIT_OK
    from .service import stop_service
    return stop_service()


if __name__ == "__main__":
    raise SystemExit(main())
