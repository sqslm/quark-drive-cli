"""Browse share directories and save a validated selection exactly once."""

from __future__ import annotations

import re
import time
from fnmatch import fnmatchcase
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit
from collections.abc import Mapping
from typing import Any

from .client import QuarkClient
from .errors import (
    AccessRestrictedError,
    AuthenticationError,
    QuarkError,
    RateLimitError,
    RemoteError,
    UsageError,
    contextual_error,
)
from .files import Files
from .paths import DirectoryTarget, RemotePaths, Scope, is_directory, path_components, valid_fid, validate_name


_SHARE_PATH = re.compile(r"/s/([A-Za-z0-9_-]{1,128})\Z")
_PAGE_SIZE = 100
_MAX_PAGES = 10000
_CREATE_READBACK_ATTEMPTS = 6
_CREATE_READBACK_INTERVAL = 0.5
_SAVE_READBACK_ATTEMPTS = 6
_SAVE_READBACK_INTERVAL = 0.5
_SHARE_ITEM_FIELDS = ("fid", "file_name", "file_type", "size", "pdir_fid", "format_type", "updated_at")


@dataclass(frozen=True)
class ShareLink:
    pwd_id: str
    passcode: str = field(repr=False)


@dataclass(frozen=True)
class ShareSelection:
    """An in-memory selection; credential-bearing fields are never serialized."""

    pwd_id: str
    source_fid: str
    source_path: str
    items: list[dict[str, Any]] = field(repr=False)
    stoken: str = field(repr=False)


def _safe_share_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in _SHARE_ITEM_FIELDS if key in item and isinstance(item[key], (str, int, float, bool))}


def validate_patterns(patterns: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    values = tuple(patterns)
    if any(not isinstance(pattern, str) or not pattern or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in pattern) for pattern in values):
        raise UsageError("过滤模式不能为空，也不能包含目录分隔符或控制字符；仅匹配直接项目名")
    return values


def parse_share_url(url: str, passcode: str | None = None) -> ShareLink:
    """Accept only a direct HTTPS pan.quark.cn share URL."""
    if not isinstance(url, str) or not url or url != url.strip():
        raise UsageError("分享链接 URL 无效")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise UsageError("分享链接 URL 无效") from None
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != "pan.quark.cn"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise UsageError("分享链接必须是 https://pan.quark.cn/s/<分享ID>")
    match = _SHARE_PATH.fullmatch(parsed.path)
    if not match:
        raise UsageError("分享链接路径必须是 /s/<分享ID>")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True) if parsed.query else {}
    except ValueError:
        raise UsageError("分享链接查询参数无效") from None
    if set(query) - {"pwd"} or len(query.get("pwd", [])) > 1:
        raise UsageError("分享链接只允许包含一个 pwd 提取码参数")
    linked_passcode = query.get("pwd", [""])[0]
    selected_passcode = linked_passcode if passcode is None else passcode
    if not isinstance(selected_passcode, str):
        raise UsageError("提取码无效")
    return ShareLink(match.group(1), selected_passcode)


def _is_dir(item: Mapping[str, Any]) -> bool:
    return is_directory(item)


def _required_text(item: Mapping[str, Any], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
        raise RemoteError("分享详情缺少必要文件字段，已停止转存")
    return str(value)


def _optional_created_fid(result: Any) -> str | None:
    """Read common mkdir response shapes without trusting them as the target."""
    if not isinstance(result, dict):
        return None
    candidates = [result]
    candidates.extend(
        nested
        for key in ("file", "item", "folder", "data")
        if isinstance((nested := result.get(key)), dict)
    )
    values = {
        str(item["fid"])
        for item in candidates
        if isinstance(item.get("fid"), (str, int))
        and not isinstance(item.get("fid"), bool)
        and str(item["fid"]).strip()
        and str(item["fid"]) != "0"
    }
    return next(iter(values)) if len(values) == 1 else None


class ShareSaver:
    """Implements share lookup, safe target resolution, and one-shot save."""

    def __init__(self, client: QuarkClient) -> None:
        self.client = client
        self.files = Files(client)
        self.paths = RemotePaths(self.files)

    def _token(self, link: ShareLink) -> str:
        try:
            payload = self.client.post(
                "share/sharepage/token",
                body={"pwd_id": link.pwd_id, "passcode": link.passcode},
            )
        except AuthenticationError:
            raise AuthenticationError("获取分享令牌未获授权") from None
        except Exception as original:
            raise contextual_error(original, "获取分享令牌失败") from None
        data = payload.get("data")
        token = data.get("stoken") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise RemoteError("分享令牌响应缺少 stoken")
        return token

    def _share_items(self, pwd_id: str, stoken: str, parent_fid: str = "0", *, require_owner: bool = False) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen_page_fids: set[tuple[str, ...]] = set()
        seen_fids: set[str] = set()
        expected_total: int | None = None

        for page in range(1, _MAX_PAGES + 1):
            try:
                payload = self.client.get(
                    "share/sharepage/detail",
                    params={
                        "pwd_id": pwd_id,
                        "stoken": stoken,
                        "ver": 2,
                        "pdir_fid": parent_fid,
                        "force": "0",
                        "_page": page,
                        "_size": _PAGE_SIZE,
                        "_fetch_total": 1,
                        "_fetch_share": 1,
                        "fetch_share_full_path": 1,
                    },
                )
            except AuthenticationError:
                raise AuthenticationError("读取分享详情未获授权，已停止转存") from None
            except Exception as original:
                raise contextual_error(original, "读取分享详情失败，已停止转存") from None

            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise RemoteError("分享详情响应缺少 data.list")
            if require_owner and (isinstance(data.get("is_owner"), bool) or not isinstance(data.get("is_owner"), (int, float)) or data["is_owner"] != 1):
                raise RemoteError("分享所有权未得到明确确认")
            records = data["list"]
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            total_value = metadata.get("_total", data.get("_total", data.get("total")))
            if total_value is not None:
                if isinstance(total_value, bool) or not isinstance(total_value, int) or total_value < 0:
                    raise RemoteError("分享详情分页总数无效")
                if expected_total is not None and expected_total != total_value:
                    raise RemoteError("分享详情分页总数不一致")
                expected_total = total_value

            if not records:
                if expected_total is not None and len(items) != expected_total:
                    raise RemoteError("分享详情分页提前结束，已停止转存")
                break

            page_fids: list[str] = []
            for record in records:
                if not isinstance(record, dict):
                    raise RemoteError("分享详情包含无效文件记录")
                if require_owner and (not isinstance(record.get("file_type"), int) or isinstance(record["file_type"], bool) or record["file_type"] not in (0, 1)):
                    raise RemoteError("本人分享成员类型无效")
                fid = valid_fid(record.get("fid"))
                recorded_parent = valid_fid(record["pdir_fid"]) if "pdir_fid" in record else parent_fid
                # The share root is a virtual view: its entries retain their
                # account parent FIDs. Nested pages use actual directory FIDs.
                if fid == "0" or (parent_fid != "0" and recorded_parent != parent_fid):
                    raise RemoteError("分享详情的父 FID 与请求目录不符")
                name = _required_text(record, "file_name")
                if fid in seen_fids:
                    raise RemoteError("分享详情包含重复 FID，已停止转存")
                seen_fids.add(fid)
                page_fids.append(fid)
                normalized = _safe_share_item(record)
                normalized.update({"fid": fid, "file_name": name, "pdir_fid": recorded_parent, "file_type": 0 if _is_dir(record) else 1})
                if "share_fid_token" in record:
                    normalized["share_fid_token"] = record["share_fid_token"]
                items.append(normalized)

            fingerprint = tuple(page_fids)
            if fingerprint in seen_page_fids:
                raise RemoteError("分享详情分页重复，已停止转存")
            seen_page_fids.add(fingerprint)

            if expected_total is not None:
                if len(items) >= expected_total:
                    if len(items) != expected_total:
                        raise RemoteError("分享详情分页超出声明总数，已停止转存")
                    break
                if len(items) > expected_total:
                    raise RemoteError("分享详情分页超出声明总数，已停止转存")
                if len(records) < _PAGE_SIZE:
                    raise RemoteError("分享详情分页提前结束，已停止转存")
            elif len(records) < _PAGE_SIZE:
                break
        else:
            raise RemoteError("分享详情分页超过安全上限")

        return items

    def _source(self, url: str, source_path: str, passcode: str | None) -> ShareSelection:
        components = path_components(source_path)
        if len(components) > 1024:
            raise UsageError("分享路径超过安全深度")
        link = parse_share_url(url, passcode)
        stoken = self._token(link)
        parent_fid = "0"
        seen = {parent_fid}
        items = self._share_items(link.pwd_id, stoken, parent_fid)
        for name in components:
            matches = [item for item in items if item["file_name"] == name]
            if not matches:
                raise UsageError(f"分享路径不存在：{name}")
            if len(matches) != 1:
                raise UsageError(f"分享路径名称“{name}”不唯一")
            item = matches[0]
            if not _is_dir(item):
                raise UsageError(f"分享路径组件“{name}”不是目录")
            parent_fid = item["fid"]
            if parent_fid in seen:
                raise RemoteError("分享路径包含循环 FID")
            seen.add(parent_fid)
            items = self._share_items(link.pwd_id, stoken, parent_fid)
        if any(item["fid"] in seen for item in items):
            raise RemoteError("分享目录包含祖先或循环 FID")
        return ShareSelection(link.pwd_id, parent_fid, source_path, items, stoken)

    def inspect(self, url: str, *, path: str = "/", passcode: str | None = None) -> dict[str, Any]:
        selection = self._source(url, path, passcode)
        return {
            "share_id": selection.pwd_id,
            "source_path": selection.source_path,
            "source_fid": selection.source_fid,
            "items": [_safe_share_item(item) for item in selection.items],
        }

    def select(
        self, url: str, *, source: str = "/", passcode: str | None = None,
        include: list[str] | tuple[str, ...] = (), exclude: list[str] | tuple[str, ...] = (),
    ) -> ShareSelection:
        """Read and select direct children without resolving or creating a target."""
        includes, excludes = validate_patterns(include), validate_patterns(exclude)
        selection = self._source(url, source, passcode)
        selected = [
            item for item in selection.items
            if (not includes or any(fnmatchcase(item["file_name"], pattern) for pattern in includes))
            and not any(fnmatchcase(item["file_name"], pattern) for pattern in excludes)
        ]
        for item in selected:
            try:
                validate_name(item["file_name"])
            except UsageError:
                raise RemoteError("选中分享项目的名称无效，已停止转存") from None
            _required_text(item, "share_fid_token")
        if any(count > 1 for count in Counter(item["file_name"] for item in selected).values()):
            raise UsageError("选中的分享项目名称不唯一，已停止转存")
        return ShareSelection(selection.pwd_id, selection.source_fid, selection.source_path, selected, selection.stoken)

    def _list_children(self, parent_fid: str) -> list[dict[str, Any]]:
        return self.paths.children(parent_fid)

    def _confirm_created_directory(self, parent_fid: str, name: str, response_fid: str | None) -> str:
        """Retry read-only listing briefly while Quark's directory index catches up."""
        for attempt in range(_CREATE_READBACK_ATTEMPTS):
            if attempt:
                time.sleep(_CREATE_READBACK_INTERVAL)
            try:
                children = self._list_children(parent_fid)
            except (RateLimitError, AccessRestrictedError):
                # A throttle/restriction must stop the readback, not retry it.
                raise
            except AuthenticationError:
                raise AuthenticationError("新建目标目录读回未获授权；未提交转存") from None
            except Exception:
                # Listing can lag or fail transiently immediately after mkdir.
                continue

            matches = [item for item in children if item.get("file_name") == name]
            if not matches:
                continue
            if len(matches) != 1 or not _is_dir(matches[0]):
                raise RemoteError("新建目标目录无法唯一确认；未提交转存")
            listed_fid = matches[0].get("fid")
            if (
                not isinstance(listed_fid, (str, int))
                or isinstance(listed_fid, bool)
                or not str(listed_fid).strip()
                or str(listed_fid) == "0"
            ):
                continue
            listed_fid = str(listed_fid)
            # Quark may wrap or disagree on the mkdir response FID. Prefer it
            # only when it agrees; the exact unique child listing is canonical.
            if response_fid is not None and response_fid == listed_fid:
                return response_fid
            return listed_fid
        raise RemoteError("新建目标目录读回超时，无法唯一确认；未提交转存")

    def _resolve_target(self, target: DirectoryTarget, scope: Scope | None) -> tuple[str, bool, list[dict[str, Any]]]:
        if scope:
            scope.check_target(target)
        if not target.missing:
            return target.fid or "0", False, self._list_children(target.fid or "0")
        try:
            result = self.files.mkdir(target.name, target.parent_fid)
        except AuthenticationError:
            raise AuthenticationError("创建目标末级目录未获授权；未提交转存") from None
        except Exception as original:
            raise contextual_error(original, "创建目标末级目录失败；未提交转存") from None
        new_fid = self._confirm_created_directory(target.parent_fid, target.name, _optional_created_fid(result))
        if scope:
            scope.check(new_fid, write=True)
        return new_fid, True, self._list_children(new_fid)

    def _submit_selection(self, selection: ShareSelection, target_fid: str, *, created: bool) -> str:
        """Submit once; an unknown acceptance state must never be retried."""
        try:
            submit = self.client.post(
                "share/sharepage/save",
                body={
                    "fid_list": [item["fid"] for item in selection.items],
                    "fid_token_list": [_required_text(item, "share_fid_token") for item in selection.items],
                    "to_pdir_fid": target_fid,
                    "pwd_id": selection.pwd_id,
                    "stoken": selection.stoken,
                    "pdir_fid": selection.source_fid,
                    "scene": "link",
                },
            )
        except AuthenticationError:
            note = "；目标目录已创建" if created else ""
            raise AuthenticationError(f"提交转存未获授权，受理状态无法确认且未自动重试{note}") from None
        except Exception as original:
            note = "；目标目录已创建" if created else ""
            raise contextual_error(original, f"提交转存失败，受理状态无法确认且未自动重试{note}") from None
        data = submit.get("data")
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not isinstance(task_id, (str, int)) or isinstance(task_id, bool) or not str(task_id).strip():
            note = "；目标目录已创建" if created else ""
            raise RemoteError(f"转存响应缺少 task_id，受理状态无法确认且未自动重试{note}")
        return str(task_id)

    def _confirm_saved_items(self, selection: ShareSelection, target_fid: str, task_id: str, task: dict[str, Any]) -> list[dict[str, Any]]:
        save_as = task.get("save_as") if isinstance(task.get("save_as"), dict) else {}
        reported_target = save_as.get("to_pdir_fid", task.get("to_pdir_fid"))
        if reported_target is not None and str(reported_target) != target_fid:
            raise RemoteError(f"转存任务 {task_id} 已完成，但目标目录与请求不符；请人工核对")
        expected = {item["file_name"]: item for item in selection.items}
        saved_fids = save_as.get("save_as_top_fids")
        reported_fids = None
        if saved_fids is not None:
            if not isinstance(saved_fids, list) or (selection.items and not saved_fids):
                raise RemoteError(f"转存任务 {task_id} 已完成，但目标 FID 列表无效")
            if saved_fids:
                reported_fids = {valid_fid(fid) for fid in saved_fids}
                if "0" in reported_fids or len(reported_fids) != len(saved_fids):
                    raise RemoteError(f"转存任务 {task_id} 已完成，但目标 FID 列表无效")
        for attempt in range(_SAVE_READBACK_ATTEMPTS):
            if attempt:
                time.sleep(_SAVE_READBACK_INTERVAL)
            try:
                after_items = self._list_children(target_fid)
            except AuthenticationError:
                raise AuthenticationError(f"转存任务 {task_id} 已完成，但查询目标目录未获授权") from None
            except Exception as original:
                raise contextual_error(original, f"转存任务 {task_id} 已完成，但无法确认目标目录内容") from None
            matches = {name: [item for item in after_items if item.get("file_name") == name] for name in expected}
            if any(len(items) > 1 for items in matches.values()):
                raise RemoteError(f"转存任务 {task_id} 已完成，但目标目录同名项目无法唯一确认")
            if any(not items for items in matches.values()):
                continue
            listed_fids = {str(item.get("fid", "")) for item in after_items}
            if reported_fids and not reported_fids.issubset(listed_fids):
                continue
            confirmed = [matches[name][0] for name in expected]
            if reported_fids and any(valid_fid(item.get("fid")) not in reported_fids for item in confirmed):
                raise RemoteError(f"转存任务 {task_id} 已完成，但同名目标项目不在任务返回的 FID 列表中")
            for item in confirmed:
                source = expected[item["file_name"]]
                if _is_dir(source) != _is_dir(item):
                    raise RemoteError(f"转存任务 {task_id} 已完成，但目标项目类型不符")
                if isinstance(source.get("size"), int) and not isinstance(source["size"], bool) and isinstance(item.get("size"), int) and source["size"] != item["size"]:
                    raise RemoteError(f"转存任务 {task_id} 已完成，但目标项目大小不符")
            return confirmed
        raise RemoteError(f"转存任务 {task_id} 已完成，但目标目录读回超时；未重新提交")

    def save(
        self,
        url: str,
        target_path: str,
        *,
        passcode: str | None = None,
        poll_interval: float = 0.5,
        max_polls: int = 120,
        scope: Scope | None = None,
        dry_run: bool = False,
        source: str = "/",
        include: list[str] | tuple[str, ...] = (),
        exclude: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if max_polls < 1 or poll_interval < 0:
            raise UsageError("任务轮询参数无效")
        path_components(target_path)
        selection = self.select(url, source=source, passcode=passcode, include=include, exclude=exclude)
        items = selection.items
        if not items:
            result = {
                "saved": False, "skipped": True, "reason": "no_selected_items",
                "destination_path": target_path, "source_path": source,
                "item_count": 0, "created_directory": False,
            }
            if dry_run:
                result["dry_run"] = True
            return result
        target = self.paths.directory_target(target_path, allow_missing_leaf=True)
        if scope:
            scope.check_target(target)

        if dry_run:
            target_fid, created = target.fid, False
            before_items = [] if target.missing else self._list_children(target.fid or "0")
        else:
            target_fid, created, before_items = self._resolve_target(target, scope)
        requested_names = Counter(item["file_name"] for item in items)
        existing_names = Counter(str(item.get("file_name", "")) for item in before_items)
        conflicts = [name for name, count in requested_names.items() if existing_names[name] > 0]
        if conflicts:
            raise UsageError("目标目录已存在同名内容，已停止以避免重复转存")
        if dry_run:
            return {
                "dry_run": True,
                "destination_fid": target_fid,
                "destination_path": target_path,
                "parent_fid": target.parent_fid,
                "item_count": len(items),
                "create_directory": target.missing,
                "source_path": selection.source_path,
                "source_fid": selection.source_fid,
                "items": [_safe_share_item(item) for item in items],
            }

        task_id = self._submit_selection(selection, target_fid or "0", created=created)
        try:
            task = self._poll_task(task_id, poll_interval=poll_interval, max_polls=max_polls)
            self._confirm_saved_items(selection, target_fid or "0", task_id, task)
        except QuarkError as error:
            error.task_id = task_id
            raise

        return {
            "saved": True,
            "task_id": task_id,
            "destination_fid": target_fid,
            "destination_path": target_path,
            "item_count": len(items),
            "created_directory": created,
            "status": 2,
            "source_path": selection.source_path,
            "source_fid": selection.source_fid,
        }

    def _poll_task(self, task_id: str, *, poll_interval: float, max_polls: int, immediate: bool = False) -> dict[str, Any]:
        for retry_index in range(max_polls):
            if poll_interval and (retry_index or not immediate):
                time.sleep(poll_interval)
            try:
                payload = self.client.get("task", params={"task_id": task_id, "retry_index": retry_index})
            except AuthenticationError:
                raise AuthenticationError(f"查询转存任务未获授权，状态无法确认（task_id={task_id}）；未重新提交") from None
            except Exception as original:
                raise contextual_error(original, f"查询转存任务失败，状态无法确认（task_id={task_id}）；未重新提交") from None
            data = payload.get("data")
            if not isinstance(data, dict):
                raise RemoteError(f"转存任务响应缺少 data（task_id={task_id}）；未重新提交")
            status = data.get("status")
            if isinstance(status, str) and status.isdigit():
                status = int(status)
            if isinstance(status, bool) or not isinstance(status, int):
                raise RemoteError(f"转存任务状态无效（task_id={task_id}）；未重新提交")
            if status == 2:
                return data
            if status in (3, 4):
                label = "失败" if status == 3 else "暂停"
                raise RemoteError(f"转存任务{label}（task_id={task_id}）；未重新提交")
            if status not in (0, 1):
                raise RemoteError(f"转存任务返回未知状态 {status}（task_id={task_id}）；未重新提交")
        raise RemoteError(f"等待转存任务超时，状态无法确认（task_id={task_id}）；未重新提交")
