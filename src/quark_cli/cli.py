"""Argument parsing and command output for ``quark``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import httpx

from .client import QuarkClient
from .auth import QRLogin, get_cookie, logout as logout_cookie
from .errors import AuthenticationError, LocalIOError, QuarkError, UsageError, error_details
from . import __version__
from .files import Files
from .paths import RemotePaths, Scope, is_directory, validate_name
from .shares import ShareSaver
from .share_management import ShareManager, safe_catalog_result, safe_operation_result
from .batch import BatchSaver
from .jobs import Jobs
from .transfer import DEFAULT_DOWNLOAD_CONNECTIONS, MAX_DOWNLOAD_CONNECTIONS, Transfer
from .recursive import RecursiveTransfer, _safe_path, local_name
from .transfer_progress import render_progress
from .organize import Organize, compile_rename_expression, safe_operation_result as safe_organize_result

_JSON_ERROR_MODE = False


class _Parser(argparse.ArgumentParser):
    json_requested = False

    def error(self, message: str) -> None:
        if _JSON_ERROR_MODE or self.json_requested or "--json" in sys.argv:
            print(json.dumps({"error": {"type": "usage_error", "message": message, "exit_code": 2}}, ensure_ascii=False), file=sys.stderr)
        else:
            self.print_usage(sys.stderr)
            print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(2)


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出 JSON")
    parser.add_argument("--scope", default=argparse.SUPPRESS, help="限制文件读写范围（FID 或绝对路径）")


def _add_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="预览操作，仅执行本地检查和远程读取")


def _login_timeout(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("超时必须是 1 到 300 之间的整数秒") from None
    if not 1 <= seconds <= 300:
        raise argparse.ArgumentTypeError("超时必须是 1 到 300 之间的整数秒")
    return seconds


def _positive_rate(value: str) -> int:
    try:
        rate = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("限速必须是正整数字节/秒") from None
    if rate <= 0:
        raise argparse.ArgumentTypeError("限速必须是正整数字节/秒")
    return rate


def _download_connections(value: str) -> int:
    try:
        connections = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"并发连接数必须是 1 到 {MAX_DOWNLOAD_CONNECTIONS} 之间的整数") from None
    if not 1 <= connections <= MAX_DOWNLOAD_CONNECTIONS:
        raise argparse.ArgumentTypeError(f"并发连接数必须是 1 到 {MAX_DOWNLOAD_CONNECTIONS} 之间的整数")
    return connections


def _add_transfer_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--recursive", action="store_true", help="传输完整目录树（上传到 parent/本地目录名，下载内容到 output）")
    parser.add_argument("--resume", action="store_true", help="使用私有账本核验并恢复原传输，未知写不重发")
    parser.add_argument("--progress", action="store_true", help="将安全字节进度和速度写入 stderr")
    parser.add_argument("--limit-rate", type=_positive_rate, metavar="BYTES_PER_SECOND", help="正整数字节/秒；目录共享总预算")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="quark", description="夸克网盘文件管理命令行工具")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--scope", default=argparse.SUPPRESS, help="限制文件读写范围；默认读取 QUARK_SCOPE")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    auth = commands.add_parser("auth", help="认证状态")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True, parser_class=_Parser)
    auth_status = auth_commands.add_parser("status", help="校验 Cookie")
    _add_json(auth_status)
    auth_login = auth_commands.add_parser("login", help="使用夸克 APP 扫码登录")
    auth_login.add_argument("--qr-file", help="二维码 PNG 输出路径；登录结束后会删除")
    auth_login.add_argument("--qr-terminal", action="store_true", help="将扫码矩阵打印到 stderr，适用于 SSH；需要 UTF-8 终端")
    auth_login.add_argument("--timeout", type=_login_timeout, default=300, help="扫码等待秒数（1 到 300，默认 300）")
    _add_json(auth_login)
    auth_logout = auth_commands.add_parser("logout", help="删除本机保存的扫码 Cookie")
    _add_json(auth_logout)

    ls = commands.add_parser("ls", help="列出目录内容")
    ls.add_argument("fid", nargs="?", default="0", metavar="REF", help="目录 FID 或绝对路径，根目录为 / 或 0")
    ls.add_argument("--all", action="store_true", help="拉取全部分页")
    ls.add_argument("--page", type=int, default=1, help="页码，从 1 开始")
    ls.add_argument("--size", type=int, default=50, help="每页数量，1 到 100")
    _add_json(ls)

    info = commands.add_parser("info", help="查看文件或目录详情")
    info.add_argument("fid")
    _add_json(info)

    tree = commands.add_parser("tree", help="递归浏览目录树")
    tree.add_argument("fid", nargs="?", default="/", metavar="REF")
    tree.add_argument("--depth", type=int, default=3, help="浏览深度（0 到 1024，默认 3）")
    _add_json(tree)
    search = commands.add_parser("search", help="在指定子树按文件名搜索")
    search.add_argument("query")
    search.add_argument("--in", dest="folder", default="/", metavar="REF", help="目录 FID 或绝对路径")
    _add_json(search)

    for command, help_text in (("du", "统计完整子树的已知大小与未知大小文件"), ("duplicates", "按可靠哈希报告确定重复和同大小/同名候选；不删除")):
        report = commands.add_parser(command, help=help_text, description=help_text)
        report.add_argument("ref", metavar="REF", help="文件或目录 FID/绝对路径")
        _add_json(report)

    rename_batch = commands.add_parser("rename-batch", help="完整预检直接子项的正则改名；默认预览",
        description="只处理目录直接子项；默认预览。--apply --dry-run 仍预览。每个 FID 仅提交一次，未知写不重发。")
    rename_batch.add_argument("ref", metavar="REF", help="目录 FID 或绝对路径")
    rename_batch.add_argument("--pattern", required=True, metavar="REGEX", help="Python 正则表达式")
    rename_batch.add_argument("--replacement", required=True, metavar="TEXT", help="Python re.sub 替换文本")
    rename_batch.add_argument("--apply", action="store_true", help="显式提交已预检改名；未知写不重发")
    _add_dry_run(rename_batch)
    _add_json(rename_batch)

    backup = commands.add_parser("backup", help="单向备份目录内容，默认预览；不删除远端对象",
        description="将本地目录内容备份到已存在的目标目录；默认预览。--apply --dry-run 仍预览。保留远端额外对象和旧内容。")
    backup.add_argument("source", metavar="LOCAL_DIR")
    backup.add_argument("--to", required=True, metavar="REF", help="已存在目标目录 FID 或绝对路径")
    backup.add_argument("--apply", action="store_true", help="实际上传新增；需要用户私有清单")
    backup.add_argument("--update", action="store_true", help="允许上传新版本，先核验后保留旧内容为唯一归档")
    backup.add_argument("--progress", action="store_true", help="安全字节进度仅输出到 stderr")
    backup.add_argument("--limit-rate", type=_positive_rate, metavar="BYTES_PER_SECOND", help="正整数字节/秒；所有文件共享预算")
    _add_dry_run(backup)
    _add_json(backup)

    save = commands.add_parser("save", help="转存夸克分享链接")
    save.add_argument("url", help="https://pan.quark.cn/s/<分享ID>")
    save.add_argument("--to", required=True, help="网盘目标绝对路径；仅允许创建缺失的末级目录")
    save.add_argument("--passcode", help="分享提取码；优先于 URL 中的 pwd")
    save.add_argument("--source", default="/", help="分享内源目录绝对路径，默认 /")
    save.add_argument("--include", action="append", default=[], metavar="GLOB", help="包含直接项目名（可重复、区分大小写）")
    save.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="排除直接项目名（可重复、优先于包含规则）")
    _add_json(save)
    _add_dry_run(save)

    batch = commands.add_parser("save-batch", help="从 UTF-8 TXT/CSV 串行转存并报告独立结果")
    batch.add_argument("file", help=".txt 每行分享链接；.csv 支持 url,to,passcode,source 列")
    batch.add_argument("--to", help="TXT 统一目标或 CSV 缺少 to 时的默认目标")
    batch.add_argument("--source", default="/", help="默认分享内源路径；CSV 行 source 优先")
    batch.add_argument("--include", action="append", default=[], metavar="GLOB", help="各行包含直接项目名，可重复、区分大小写")
    batch.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="各行排除直接项目名，可重复、优先于包含规则")
    batch.add_argument("--report", help="原子写入安全 JSON 报告；父目录必须存在")
    _add_json(batch)
    _add_dry_run(batch)

    job = commands.add_parser("job", help="持久增量转存任务，供系统定时器调用")
    _add_json(job)
    job_commands = job.add_subparsers(dest="job_command", required=True, parser_class=_Parser)
    job_add = job_commands.add_parser("add", help="保存来源选择、范围和目标绑定")
    job_add.add_argument("name")
    job_add.add_argument("url")
    job_add.add_argument("--to", required=True)
    job_add.add_argument("--passcode")
    job_add.add_argument("--source", default="/")
    job_add.add_argument("--include", action="append", default=[], metavar="GLOB")
    job_add.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    _add_json(job_add)
    job_list = job_commands.add_parser("list", help="本地列出安全任务字段，无需 Cookie")
    _add_json(job_list)
    job_run = job_commands.add_parser("run", help="按身份核验增量；省略 NAME 执行全部任务")
    job_run.add_argument("name", nargs="?")
    _add_json(job_run)
    _add_dry_run(job_run)
    job_remove = job_commands.add_parser("remove", help="只移除本地配置，保留账本及未知意图")
    job_remove.add_argument("name")
    _add_json(job_remove)

    share = commands.add_parser("share", help="查看分享及管理自己的分享")
    _add_json(share)
    share_commands = share.add_subparsers(dest="share_command", required=True, parser_class=_Parser)
    inspect = share_commands.add_parser("inspect", help="读取分享目录的全部分页")
    inspect.add_argument("url", help="https://pan.quark.cn/s/<分享ID>")
    inspect.add_argument("--passcode", help="分享提取码；优先于 URL 中的 pwd")
    inspect.add_argument("--path", default="/", help="分享内目录绝对路径，默认 /")
    _add_json(inspect)
    share_create = share_commands.add_parser("create", help="创建一个文件或目录的分享；仅提交一次")
    share_create.add_argument("ref", metavar="REF", help="账户对象 FID 或绝对路径；不能为账户根")
    share_create.add_argument("--passcode", help="四位 ASCII 字母数字；省略时公开分享")
    share_create.add_argument("--expire-days", type=int, choices=(0, 1, 7, 30), default=7, help="有效天数，0 为永久，默认 7")
    _add_json(share_create)
    _add_dry_run(share_create)
    share_list = share_commands.add_parser("list", help="本人分享目录；scope 下仅显示完整核验的范围内分享")
    share_list.add_argument("--all", action="store_true", help="完整分页；scope 下显示全部已核验条目")
    _add_json(share_list)
    share_revoke = share_commands.add_parser("revoke", help="取消本人内部 share_id 对应分享，需 --yes")
    share_revoke.add_argument("share_id", help="内部 share_id，不能使用公开链接 ID")
    share_revoke.add_argument("--yes", action="store_true", help="确认取消分享")
    _add_json(share_revoke)
    _add_dry_run(share_revoke)

    mkdir = commands.add_parser("mkdir", help="创建目录")
    mkdir.add_argument("name")
    mkdir.add_argument("--parent", default=None, help="父目录 FID 或绝对路径，默认 0；完整路径不能同时指定")
    _add_json(mkdir)
    _add_dry_run(mkdir)

    upload = commands.add_parser("upload", help="上传文件，--recursive 上传目录")
    upload.add_argument("local")
    upload.add_argument("--parent", default="0", help="父目录 FID 或绝对路径，默认 0")
    _add_transfer_options(upload)
    _add_json(upload)
    _add_dry_run(upload)

    download = commands.add_parser("download", help="下载文件，--recursive 下载目录内容")
    download.add_argument("fid")
    download.add_argument("--output", required=True, help="本地目标路径")
    download.add_argument("--overwrite", action="store_true", help="允许覆盖已有文件")
    _add_transfer_options(download)
    download.add_argument("--connections", type=_download_connections, default=DEFAULT_DOWNLOAD_CONNECTIONS, metavar="N",
                          help=f"单文件 Range 并发连接数（1–{MAX_DOWNLOAD_CONNECTIONS}，默认 {DEFAULT_DOWNLOAD_CONNECTIONS}；--resume 使用安全串行恢复）")
    _add_json(download)

    rename = commands.add_parser("rename", help="重命名文件或目录")
    rename.add_argument("fid")
    rename.add_argument("name")
    _add_json(rename)
    _add_dry_run(rename)

    move = commands.add_parser("mv", help="移动文件或目录")
    move.add_argument("fid")
    move.add_argument("--to", required=True, help="目标目录 FID 或绝对路径")
    _add_json(move)
    _add_dry_run(move)

    delete = commands.add_parser("rm", help="将文件或目录移到回收站")
    delete.add_argument("fid")
    delete.add_argument("--yes", action="store_true", help="确认删除")
    _add_json(delete)
    _add_dry_run(delete)
    return parser


def _emit_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


_FILE_JSON_FIELDS = ("fid", "file_name", "file_type", "size", "pdir_fid", "format_type", "updated_at")
_SCALAR_JSON_TYPES = (str, int, float, bool)


def _scalar_fields(value: Any, fields: Sequence[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in fields
        if key in value and isinstance(value[key], _SCALAR_JSON_TYPES)
    }


def _safe_file_record(item: dict[str, Any]) -> dict[str, Any]:
    return _scalar_fields(item, _FILE_JSON_FIELDS)


def _safe_list_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload["data"]
    safe_data: dict[str, Any] = {
        "list": [_safe_file_record(item) for item in data["list"]],
    }
    total = data.get("_total", data.get("total"))
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        safe_data["_total"] = total
    safe_payload: dict[str, Any] = {"data": safe_data}
    safe_payload.update(_scalar_fields(payload, ("status", "code")))
    return safe_payload


def _is_dir(item: dict[str, Any]) -> bool:
    return is_directory(item)


def _human_size(value: Any) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return "?"
    if size < 0:
        return "?"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    display = float(size)
    for unit in units:
        if display < 1024 or unit == units[-1]:
            return f"{display:.0f} {unit}" if unit == "B" else f"{display:.1f} {unit}"
        display /= 1024
    return f"{size} B"


def _safe_cell(value: Any) -> str:
    return "".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in str(value if value is not None else ""))


def _output_list(payload: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        _emit_json(_safe_list_payload(payload))
        return
    items = payload["data"]["list"]
    if not items:
        print("(空目录)")
        return
    print(f"{'FID':<24} {'TYPE':<9} {'SIZE':>12}  NAME")
    for item in items:
        is_dir = _is_dir(item)
        fid = _safe_cell(item.get("fid", ""))[:24]
        kind = "目录" if is_dir else "文件"
        size = "—" if is_dir else _human_size(item.get("size"))
        name = _safe_cell(item.get("file_name", ""))
        print(f"{fid:<24} {kind:<9} {size:>12}  {name}")


def _output_info(item: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        _emit_json(_safe_file_record(item))
        return
    print(f"FID: {_safe_cell(item.get('fid'))}")
    print(f"名称: {_safe_cell(item.get('file_name'))}")
    print(f"类型: {'目录' if _is_dir(item) else '文件'}")
    if not _is_dir(item):
        print(f"大小: {_human_size(item.get('size'))}")
    for key, label in (("pdir_fid", "父目录 FID"), ("format_type", "格式"), ("updated_at", "更新时间")):
        if item.get(key) is not None:
            print(f"{label}: {_safe_cell(item[key])}")


def _confirm_delete(fid: str, yes: bool, json_mode: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        raise UsageError("非交互运行删除命令时必须提供 --yes")
    print(f"将对象 {_safe_cell(fid)} 移到回收站。继续？[y/N] ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline().strip().lower()
    if answer not in ("y", "yes"):
        if json_mode:
            _emit_json({"cancelled": True, "fid": fid})
        else:
            print("已取消")
        return False
    return True


def _dispatch(args: argparse.Namespace, client: QuarkClient) -> int:
    files = Files(client)
    transfer = Transfer(client)
    paths = RemotePaths(files)
    json_mode = bool(getattr(args, "json", False))
    dry_run = bool(getattr(args, "dry_run", False))
    scope_ref = getattr(args, "scope", os.environ.get("QUARK_SCOPE"))
    try:
        scope = Scope(paths, scope_ref) if scope_ref is not None and args.command != "auth" else None
    except QuarkError as exc:
        if args.command in {"du", "duplicates", "rename-batch", "backup"}:
            raise type(exc)("范围预检未完成：路径、身份或远端读取检查失败") from None
        raise

    def resolve(ref: str, *, directory: bool = False, mutate_object: bool = False, write: bool = False):
        obj = paths.resolve(ref, directory=directory, inspect_fid=dry_run)
        if scope:
            scope.check(obj.fid, mutate_object=mutate_object, write=write)
        return obj

    def preview(action: str, **fields: Any) -> int:
        result = {"dry_run": True, "action": action, **fields}
        if json_mode:
            _emit_json(result)
        else:
            print("预览：" + json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "auth":
        client.auth_status()
        if json_mode:
            _emit_json({"authenticated": True})
        else:
            print("认证有效")
        return 0
    if args.command in {"du", "duplicates", "rename-batch"}:
        organize = Organize(client)
        if args.command == "du":
            result = organize.du(args.ref, scope=scope)
        elif args.command == "duplicates":
            result = organize.duplicates(args.ref, scope=scope)
        else:
            result = organize.rename_batch(args.ref, args.pattern, args.replacement, apply=args.apply, dry_run=dry_run, scope=scope)
        result = safe_organize_result(result)
        if json_mode:
            _emit_json(result)
        elif args.command == "du":
            print(f"文件 {result['files']}，目录 {result['directories']}，已知 {result['known_bytes']} B，大小未知 {result['unknown_size_files']}，完整={result['complete']}")
        elif args.command == "duplicates":
            for category in ("confirmed", "candidates"):
                for group in result[category]:
                    print(f"{'确定重复' if category == 'confirmed' else '候选 ' + group['reason']}：")
                    for item in group["items"]:
                        print(f"  {_safe_cell(item['fid'])}\t{_safe_cell(item.get('path', ''))}")
            print(f"无可靠哈希文件 {result['hash_unavailable_files']}；未删除任何对象")
        else:
            print(f"{'预览' if result['dry_run'] else '已完成'}：改名 {result['renamed']}，未变 {result['unchanged']}")
            for item in result["items"]:
                print(f"{_safe_cell(item['fid'])}\t{_safe_cell(item['old_name'])} -> {_safe_cell(item['new_name'])}\t{item['result']}")
        return 0
    if args.command == "backup":
        # Read-only commands do not need the backup orchestration module.
        from .backup import Backup, safe_operation_result as safe_backup_result
        result = safe_backup_result(Backup(client).run(args.source, args.to, apply=args.apply and not dry_run,
            update=args.update, limit_rate=args.limit_rate, progress=render_progress if args.progress else None, scope=scope))
        if json_mode:
            _emit_json(result)
        else:
            print(f"{'预览' if result['dry_run'] else '备份完成'}：新增 {result['uploaded_files']}，更新 {result['updated_files']}，跳过 {result['skipped_files']}，新目录 {result['created_directories']}")
            for archive in result["archives"]:
                print(f"保留旧内容：{_safe_cell(archive['fid'])}\t{_safe_cell(archive['name'])}")
        return 0
    if args.command == "ls":
        # Preserve the original direct-FID listing when no scope is requested.
        obj = resolve(args.fid, directory=args.fid.startswith("/") or scope is not None)
        payload = files.list_all(obj.fid, args.page, args.size) if args.all else files.list_page(obj.fid, args.page, args.size)
        if scope:
            for item in payload["data"]["list"]:
                scope.check(str(item.get("fid", "")))
        _output_list(payload, json_mode)
        return 0
    if args.command == "info":
        obj = resolve(args.fid)
        _output_info(obj.item if obj.fid == "0" else files.info(obj.fid), json_mode)
        return 0
    if args.command in {"tree", "search"}:
        ref = args.fid if args.command == "tree" else args.folder
        resolve(ref, directory=True)
        if args.command == "search" and not args.query:
            raise UsageError("搜索内容不能为空")
        records = []
        for item in paths.walk(ref, depth=args.depth if args.command == "tree" else None):
            if scope:
                scope.check(item["fid"])
            if args.command == "search" and (item["depth"] == 0 or args.query.casefold() not in item["file_name"].casefold()):
                continue
            record = _safe_file_record(item)
            record.update(_scalar_fields(item, ("path", "depth")))
            records.append(record)
        if json_mode:
            _emit_json({"items": records})
        else:
            for item in records:
                print(f"{_safe_cell(item['fid'])}\t{'目录' if item['file_type'] == 0 else '文件'}\t{_safe_cell(item['path'])}")
        return 0
    if args.command == "save-batch":
        result = BatchSaver(ShareSaver(client)).run(args.file, destination=args.to, report=args.report, scope=scope, dry_run=dry_run,
                                                   source=args.source, include=args.include, exclude=args.exclude)
        if json_mode:
            _emit_json(result.output)
        else:
            for row in result.output["rows"]:
                print(f"第 {row['line']} 行：{row['result']}\t{_safe_cell(row['destination_path'])}")
            print(f"共 {result.output['total']} 行，成功 {result.output['succeeded']}，跳过 {result.output['skipped']}，失败 {result.output['failed']}")
        if result.output.get("report", {}).get("written") is False:
            return _report_error(LocalIOError("批量操作已结束，但报告原子写入失败；未重新提交转存"), json_mode)
        return result.exit_code
    if args.command == "job":
        jobs = Jobs(ShareSaver(client))
        if args.job_command == "add":
            result = jobs.add(args.name, args.url, args.to, source=args.source, passcode=args.passcode,
                              include=args.include, exclude=args.exclude, scope=scope)
            code = 0
        else:
            result, code = jobs.run(args.name, scope=scope, dry_run=dry_run)
        if json_mode:
            _emit_json(result)
        elif args.job_command == "add":
            print(f"已创建任务：{_safe_cell(args.name)}")
        else:
            for item in result["jobs"]:
                print(f"{_safe_cell(item['name'])}\t{item['result']}\t新增 {item.get('new_items', 0)}")
        return code
    if args.command == "save":
        result = ShareSaver(client).save(
            args.url, args.to, passcode=args.passcode, scope=scope, dry_run=dry_run,
            source=args.source, include=args.include, exclude=args.exclude,
        )
        if result.get("skipped"):
            if json_mode:
                _emit_json({key: result[key] for key in ("saved", "skipped", "reason", "destination_path", "source_path", "item_count", "created_directory", "dry_run") if key in result})
            else:
                print("未选中任何分享项目；已跳过转存")
            return 0
        if dry_run:
            return preview("save", **{key: result[key] for key in ("destination_fid", "destination_path", "item_count", "create_directory", "parent_fid", "source_path", "source_fid", "items")})
        if json_mode:
            output = {
                key: result[key]
                for key in (
                    "saved", "task_id", "destination_fid", "destination_path",
                    "item_count", "created_directory", "status",
                )
            }
            if args.source != "/" or args.include or args.exclude:
                output.update({key: result[key] for key in ("source_path", "source_fid")})
            _emit_json(output)
        else:
            created = "（已创建末级目录）" if result["created_directory"] else ""
            print(
                f"已转存 {result['item_count']} 项到“{_safe_cell(result['destination_path'])}”"
                f"（目录 FID {result['destination_fid']}）{created}"
            )
        return 0
    if args.command == "share":
        if args.share_command != "inspect":
            manager = ShareManager(client)
            if args.share_command == "create":
                result = safe_operation_result(manager.create(args.ref, passcode=args.passcode,
                    expire_days=args.expire_days, dry_run=dry_run, scope=scope))
            elif args.share_command == "list":
                result = safe_catalog_result(manager.list_own(all_pages=args.all, scope=scope))
            else:
                result = safe_operation_result(manager.revoke(args.share_id, dry_run=dry_run, scope=scope))
            if json_mode:
                _emit_json(result)
            elif args.share_command == "list":
                for record in result["items"]:
                    print(f"{_safe_cell(record['share_id'])}\t{_safe_cell(record.get('title', ''))}\t{_safe_cell(record.get('share_url', ''))}")
                if result["scope_applied"]:
                    print(f"已核验范围内分享 {result['verified_total']}；范围外 {result['outside_scope_count']}；无法核验 {result['unverifiable_count']}")
                else:
                    print(f"已读取 {result['returned_count']} 条；本人目录完整={result['catalog_complete']}")
            elif dry_run:
                print("预览：" + json.dumps(result, ensure_ascii=False))
            elif args.share_command == "create":
                print(f"已创建分享：{_safe_cell(result['share_url'])}（内部 ID {_safe_cell(result['share_id'])}）")
                if result.get("passcode"):
                    print(f"提取码：{result['passcode']}")
            else:
                print(f"已取消分享：{_safe_cell(result['share_id'])}")
            return 0
        result = ShareSaver(client).inspect(args.url, path=args.path, passcode=args.passcode)
        if json_mode:
            _emit_json({
                **_scalar_fields(result, ("share_id", "source_path", "source_fid")),
                "items": [_safe_file_record(item) for item in result["items"]],
            })
        else:
            print(f"分享 {_safe_cell(result['share_id'])}：{_safe_cell(result['source_path'])}")
            _output_list({"data": {"list": result["items"]}}, False)
        return 0
    if args.command == "mkdir":
        if args.name.startswith("/"):
            if args.parent is not None:
                raise UsageError("mkdir 完整路径不能同时指定 --parent")
            target = paths.directory_target(args.name, allow_missing_leaf=True)
            if not target.missing:
                raise UsageError("目标目录已存在")
            parent, name = target.parent_fid, target.name
            if scope:
                scope.check_target(target)
        else:
            name = validate_name(args.name)
            parent = resolve(args.parent or "0", directory=(args.parent or "0") != "0", write=True).fid
            if dry_run and any(item.get("file_name") == name for item in paths.children(parent)):
                raise UsageError("父目录已存在同名内容")
        if dry_run:
            return preview("mkdir", name=name, parent=parent)
        result = files.mkdir(name, parent)
        if json_mode:
            output = {"created": True, "name": name, "parent": parent}
            output.update(_scalar_fields(result, ("fid", "task_id")))
            _emit_json(output)
        else:
            print(f"已创建目录：{args.name}")
        return 0
    if args.command == "upload":
        parent = resolve(args.parent, directory=args.parent != "0", write=True).fid
        options = {}
        if args.resume:
            options["resume"] = True
        if args.limit_rate is not None:
            options["limit_rate"] = args.limit_rate
        if args.progress:
            options["progress"] = render_progress
        if scope:
            options["scope"] = scope
        if args.recursive:
            result = RecursiveTransfer(client, transfer=transfer).upload_tree(args.local, parent, dry_run=dry_run, **options)
            if result.get("callback_warning"):
                print("警告：目录中有文件的 OSS callback 未成功；夸克完成及目标身份已确认。", file=sys.stderr)
            if json_mode:
                _emit_json(_scalar_fields(result, ("uploaded", "recursive", "resumed", "dry_run", "destination_fid", "parent", "files", "directories", "size", "uploaded_files", "skipped_files", "callback_warning")))
            else:
                print(f"{'预览' if dry_run else '已上传目录'}：{result['files']} 个文件，{result['directories']} 个目录，{_human_size(result['size'])}")
            return 0
        if Path(args.local).is_dir():
            raise UsageError("上传目录需要 --recursive")
        if dry_run:
            source = Path(args.local)
            _safe_path(source.absolute())
            local_name(source.name)
            try:
                size = source.stat().st_size
                if not source.is_file():
                    raise UsageError("上传路径必须是普通文件")
                if size == 0:
                    raise UsageError("夸克网页版不支持零字节上传")
                with source.open("rb"):
                    pass
            except OSError:
                raise LocalIOError("无法读取本地上传文件") from None
            return preview("upload", file_name=source.name, file_size=size, parent=parent)
        result = transfer.upload(args.local, parent, **options)
        if result.get("callback_warning"):
            print("警告：对象已存储，但 OSS callback 未成功；夸克 finish 已确认完成。", file=sys.stderr)
        if json_mode:
            _emit_json(_scalar_fields(result, ("fid", "file_name", "file_size", "task_id", "parts", "callback_warning", "resumed", "skipped_parts", "sha256")))
        else:
            print(f"已上传：{result['file_name']}（{_human_size(result['file_size'])}，{result['parts']} 个分片）")
        return 0
    if args.command == "download":
        obj = resolve(args.fid)
        options = {}
        if args.resume:
            options["resume"] = True
        if args.limit_rate is not None:
            options["limit_rate"] = args.limit_rate
        if args.progress:
            options["progress"] = render_progress
        options["connections"] = args.connections
        if scope:
            options["scope"] = scope
        if args.recursive:
            result = RecursiveTransfer(client, transfer=transfer).download_tree(obj.fid, args.output, overwrite=args.overwrite, **options)
            if json_mode:
                _emit_json(_scalar_fields(result, ("downloaded", "recursive", "resumed", "fid", "path", "files", "directories", "size", "downloaded_files", "skipped_files")))
            else:
                print(f"已下载目录：{result['path']}（{result['files']} 个文件，{result['directories']} 个目录，{_human_size(result['size'])}）")
            return 0
        if obj.item and is_directory(obj.item):
            raise UsageError("下载目录需要 --recursive")
        result = transfer.download(obj.fid, args.output, overwrite=args.overwrite, **options)
        if json_mode:
            _emit_json(_scalar_fields(result, ("fid", "path", "size", "resumed", "sha256")))
        else:
            print(f"已下载：{result['path']}（{_human_size(result['size'])}）")
        return 0
    if args.command == "rename":
        name = validate_name(args.name)
        obj = resolve(args.fid, mutate_object=True, write=True)
        if obj.fid == "0":
            raise UsageError("不能重命名根目录")
        if dry_run:
            return preview("rename", fid=obj.fid, name=name)
        result = files.rename(obj.fid, name)
        if json_mode:
            output = {"renamed": True, "fid": obj.fid, "name": name}
            output.update(_scalar_fields(result, ("task_id",)))
            _emit_json(output)
        else:
            print(f"已重命名 FID {obj.fid}：{name}")
        return 0
    if args.command == "mv":
        obj = resolve(args.fid, mutate_object=True, write=True)
        if obj.fid == "0":
            raise UsageError("不能移动根目录")
        target = resolve(args.to, directory=True, write=True)
        if obj.fid in {ancestor.fid for ancestor in paths.ancestry(target.fid)}:
            raise UsageError("不能将目录移到自身或后代")
        if dry_run:
            return preview("mv", fid=obj.fid, parent=target.fid)
        result = files.move(obj.fid, target.fid)
        if json_mode:
            output = {"moved": True, "fid": obj.fid, "parent": target.fid}
            output.update(_scalar_fields(result, ("task_id",)))
            _emit_json(output)
        else:
            print(f"已移动 FID {obj.fid} 到目录 {target.fid}")
        return 0
    if args.command == "rm":
        if not dry_run and not _confirm_delete(args.fid, args.yes, json_mode):
            return 0
        obj = resolve(args.fid, mutate_object=True, write=True)
        if obj.fid == "0":
            raise UsageError("不能删除根目录")
        if dry_run:
            return preview("rm", fid=obj.fid, semantics="moved_to_recycle_bin")
        result = files.delete(obj.fid)
        if json_mode:
            output = {"deleted": True, "fid": obj.fid, "semantics": "moved_to_recycle_bin"}
            output.update(_scalar_fields(result, ("task_id",)))
            _emit_json(output)
        else:
            print(f"已将 FID {obj.fid} 移到回收站")
        return 0
    raise UsageError("未知命令")


def _dispatch_auth_without_client(
    args: argparse.Namespace,
    *,
    transport: httpx.BaseTransport | None,
    json_mode: bool,
) -> int | None:
    if args.command != "auth" or args.auth_command not in {"login", "logout"}:
        return None
    if args.auth_command == "logout":
        removed = logout_cookie()
        result = {"logged_out": True, "saved_cookie_removed": removed}
        if json_mode:
            _emit_json(result)
        elif removed:
            print("已删除本机保存的扫码 Cookie；环境变量凭证不受影响")
        else:
            print("本机没有保存的扫码 Cookie；环境变量凭证不受影响")
        return 0

    def show_qr(path: Path) -> None:
        print(f"二维码已生成：{path}", file=sys.stderr)
        print("这是夸克网页版登录二维码，请使用手机夸克扫码并确认；正在等待登录……", file=sys.stderr, flush=True)

    def show_terminal_qr(rendered: str) -> None:
        try:
            print(rendered, file=sys.stderr, flush=True)
        except UnicodeEncodeError:
            raise LocalIOError("终端不支持二维码字符，请使用 UTF-8 终端或 --qr-file") from None

    try:
        options = {"on_qr_terminal": show_terminal_qr} if args.qr_terminal else {}
        QRLogin(transport=transport, timeout=args.timeout).run(
            qr_file=Path(args.qr_file) if args.qr_file else None,
            on_qr_ready=show_qr,
            **options,
        )
    except KeyboardInterrupt:
        raise AuthenticationError("扫码登录已取消") from None
    result = {"authenticated": True, "credential_saved": True}
    if json_mode:
        _emit_json(result)
    else:
        print("扫码登录成功，Cookie 已安全保存在本机")
    return 0


def main(argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    global _JSON_ERROR_MODE
    parser = build_parser()
    arguments = list(argv) if argv is not None else None
    if arguments is not None:
        parser.json_requested = "--json" in arguments
    else:
        parser.json_requested = "--json" in sys.argv[1:]
    _JSON_ERROR_MODE = parser.json_requested
    try:
        args = parser.parse_args(arguments)
        json_mode = parser.json_requested or bool(getattr(args, "json", False))
        if args.command == "rename-batch":
            compile_rename_expression(args.pattern, args.replacement)
        if args.command == "share" and args.share_command == "revoke" and not args.dry_run and not args.yes:
            raise UsageError("取消分享必须提供 --yes；预览可使用 --dry-run")
        auth_result = _dispatch_auth_without_client(args, transport=transport, json_mode=json_mode)
        if auth_result is not None:
            return auth_result
        if args.command == "job" and args.job_command in ("list", "remove"):
            jobs = Jobs()
            result = jobs.list() if args.job_command == "list" else jobs.remove(args.name)
            if json_mode:
                _emit_json(result)
            elif args.job_command == "list":
                for item in result["jobs"]:
                    print(f"{_safe_cell(item['name'])}\t{_safe_cell(item['destination_path'])}\tpending={item['pending']}")
            else:
                print(f"已移除本地任务：{_safe_cell(args.name)}；账本保留")
            return 0
        try:
            cookie = get_cookie()
        except QuarkError as exc:
            return _report_error(exc, json_mode)
        with QuarkClient(cookie, transport=transport) as client:
            return _dispatch(args, client)
    except QuarkError as exc:
        return _report_error(exc, parser.json_requested or bool(getattr(locals().get("args"), "json", False)))
    finally:
        _JSON_ERROR_MODE = False


def _report_error(error: QuarkError, json_mode: bool) -> int:
    evidence = safe_operation_result(getattr(error, "share_evidence", {}))
    if hasattr(error, "organize_evidence"):
        evidence = safe_organize_result(error.organize_evidence)
    if hasattr(error, "backup_evidence"):
        from .backup import safe_operation_result as safe_backup_result
        evidence = safe_backup_result(error.backup_evidence)
    if json_mode:
        document = error_details(error)
        if evidence:
            document["operation"] = evidence
        print(
            json.dumps(
                {"error": document},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
    else:
        print(f"错误：{error}", file=sys.stderr)
        if evidence:
            print("操作证据：" + json.dumps(evidence, ensure_ascii=False), file=sys.stderr)
    return error.exit_code
