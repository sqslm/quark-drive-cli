"""Own-share management with complete membership proof and one-shot writes."""

from __future__ import annotations

import math
import re
import time
import unicodedata
from collections.abc import Mapping, Callable
from typing import Any
from urllib.parse import urlsplit

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
from .paths import RemotePaths, Scope, is_directory, valid_fid, validate_name
from .shares import ShareLink, ShareSaver

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_CODE = re.compile(r"[A-Za-z0-9]{4}\Z")
_PAGE_SIZE = 50
_MAX_PAGES = 10000
_READ_ATTEMPTS = 3
_CONFIRM_ATTEMPTS = 6
_MAX_POLLS = 120
_MAX_MEMBER_DEPTH = 1024
_MAX_MEMBER_ITEMS = 100000
_EXPIRED = {0: 1, 1: 2, 7: 3, 30: 4}
_LIST_FIELDS = ("share_id", "pwd_id", "title", "share_url", "url_type", "created_at",
                "expired_type", "expired_at", "status", "audit_status", "partial_violation", "violation_cnt")
_RESULT_FIELDS = ("result", "task_id", "share_id", "pwd_id", "share_url", "passcode", "title",
                  "url_type", "expire_days", "expired_type", "source_fids", "revoked", "dry_run")


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RemoteError("分享响应缺少有效标识")
    return value


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not any(unicodedata.category(c) in ("Cc", "Cf") for c in value)


def _number(value: Any) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except (OverflowError, TypeError):
        return False


def _share_url(value: Any, pwd_id: str) -> str:
    if not isinstance(value, str):
        raise RemoteError("分享链接响应无效")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname == "pan.quark.cn"
                 and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
                 and not parsed.fragment and parsed.path == f"/s/{pwd_id}")
    except ValueError:
        valid = False
    if not valid:
        raise RemoteError("分享链接与公开标识不符")
    return f"https://pan.quark.cn/s/{pwd_id}"


def safe_share_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Public catalog fields only; unsafe optional fields are omitted."""
    result = {"share_id": _identifier(record.get("share_id"))}
    for field in _LIST_FIELDS[1:]:
        value = record.get(field)
        if field == "pwd_id":
            if isinstance(value, str) and _ID.fullmatch(value):
                result[field] = value
        elif field == "title":
            if _text(value):
                result[field] = value
        elif field == "share_url":
            try:
                result[field] = _share_url(value, _identifier(record.get("pwd_id")))
            except RemoteError:
                pass
        elif field == "partial_violation":
            if isinstance(value, bool) or (_number(value) and value in (0, 1)):
                result[field] = value
        elif _number(value):
            result[field] = value
    return result


def safe_operation_result(value: Any) -> dict[str, Any]:
    """Apply the public operation schema at both service and CLI boundaries."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for field in _RESULT_FIELDS:
        entry = value.get(field)
        if field in ("task_id", "share_id", "pwd_id"):
            if isinstance(entry, str) and _ID.fullmatch(entry):
                result[field] = entry
        elif field == "share_url":
            try:
                result[field] = _share_url(entry, _identifier(value.get("pwd_id")))
            except RemoteError:
                pass
        elif field == "passcode":
            if isinstance(entry, str) and (not entry or _CODE.fullmatch(entry)):
                result[field] = entry
        elif field == "title":
            if _text(entry):
                result[field] = entry
        elif field == "result":
            if entry in ("created", "revoked", "preview", "partial", "unknown", "failed"):
                result[field] = entry
        elif field == "source_fids":
            if isinstance(entry, list) and all(isinstance(fid, str) and _ID.fullmatch(fid) for fid in entry):
                result[field] = list(entry)
        elif field in ("dry_run", "revoked"):
            if isinstance(entry, bool):
                result[field] = entry
        elif _number(entry):
            result[field] = entry
    return result


def safe_catalog_result(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {"items": [safe_share_record(record) for record in value["items"]]}
    for field in ("scope_applied", "catalog_complete", "has_more"):
        if isinstance(value.get(field), bool):
            result[field] = value[field]
    for field in ("catalog_total", "verified_total", "outside_scope_count", "unverifiable_count", "returned_count"):
        entry = value.get(field)
        if isinstance(entry, int) and not isinstance(entry, bool) and entry >= 0:
            result[field] = entry
        elif field == "catalog_total" and entry is None:
            result[field] = None
    return result


def _operation_error(message: str, evidence: Mapping[str, Any], *, authentication: bool = False) -> QuarkError:
    error = AuthenticationError(message) if authentication else RemoteError(message)
    error.share_evidence = safe_operation_result(evidence)
    return error


def _poll_delay(metadata: Any) -> float:
    value = metadata.get("tq_gap") if isinstance(metadata, dict) else None
    return min(5.0, max(0.1, value / 1000)) if _number(value) and value > 0 else 0.5


def _incomplete_snapshot(payload: Mapping[str, Any]) -> bool:
    """Inspect known task wrappers without publishing raw failure details."""
    incomplete = False
    stack = [(payload, 0)]
    while stack:
        container, depth = stack.pop()
        if depth > 8:
            raise RemoteError("分享创建快照嵌套格式无效")
        if "creation_snapshot" in container:
            snapshot = container["creation_snapshot"]
            if not isinstance(snapshot, dict) or not set(snapshot).intersection({"success_count", "failed_count", "invalid_fids", "invalid_fid_reasons", "invalid_reason"}):
                raise RemoteError("分享创建快照格式无法核验")
            for field in ("success_count", "failed_count"):
                if field in snapshot and (not isinstance(snapshot[field], int) or isinstance(snapshot[field], bool) or snapshot[field] < 0):
                    raise RemoteError("分享创建快照计数无效")
            if snapshot.get("success_count") == 0 and snapshot.get("failed_count") == 0:
                raise RemoteError("分享创建快照计数与非空请求矛盾")
            incomplete = incomplete or snapshot.get("failed_count", 0) > 0
            if "invalid_fids" in snapshot:
                values = snapshot["invalid_fids"]
                if not isinstance(values, list) or any(not isinstance(fid, str) or not _ID.fullmatch(fid) for fid in values):
                    raise RemoteError("分享创建无效成员快照格式错误")
                incomplete = incomplete or bool(values)
            if "invalid_fid_reasons" in snapshot:
                value = snapshot["invalid_fid_reasons"]
                if not isinstance(value, (dict, list)):
                    raise RemoteError("分享创建失败原因快照格式错误")
                incomplete = incomplete or bool(value)
            if "invalid_reason" in snapshot:
                value = snapshot["invalid_reason"]
                if not isinstance(value, (str, int)) or isinstance(value, bool):
                    raise RemoteError("分享创建失败原因快照格式错误")
                incomplete = incomplete or bool(value)
            if "typical_filenames" in snapshot and (not isinstance(snapshot["typical_filenames"], list)
                    or any(not isinstance(name, str) for name in snapshot["typical_filenames"])):
                raise RemoteError("分享创建文件名快照格式错误")
        for field in ("data", "task_resp", "taskResult", "task_result", "result"):
            if isinstance(container.get(field), dict):
                stack.append((container[field], depth + 1))
    return incomplete


class _CatalogConsistency(RemoteError):
    """Only a complete read snapshot inconsistency may restart pagination."""


class _OutsideScope(UsageError):
    pass


class ShareManager:
    def __init__(self, client: QuarkClient) -> None:
        self.client = client
        self.files = Files(client)
        self.paths = RemotePaths(self.files)
        self.saver = ShareSaver(client)

    def _call(self, method: str, endpoint: str, **options: Any) -> dict[str, Any]:
        try:
            return getattr(self.client, method)(endpoint, **options)
        except Exception as original:
            if isinstance(original, AuthenticationError):
                error: QuarkError = AuthenticationError("分享管理读取或操作未获授权")
            else:
                # contextual_error keeps RateLimitError/AccessRestrictedError (with
                # their retry_after) instead of hiding the cooldown in a generic error.
                error = contextual_error(original, "分享管理读取或操作失败")
            if getattr(original, "api_rejected", False) is True:
                error.api_rejected = True
            raise error from None

    def _catalog_once(self, *, all_pages: bool) -> tuple[list[dict[str, Any]], int | None, bool]:
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        fingerprints: set[tuple[str, ...]] = set()
        total: int | None = None
        for page in range(1, _MAX_PAGES + 1):
            payload = self._call("get", "share/mypage/detail", params={
                "_page": page, "_size": _PAGE_SIZE, "_order_field": "created_at", "_order_type": "desc",
                "_fetch_total": 1, "_fetch_notify_follow": 1, "_filter": "all", "_share_type": "0,1"})
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise RemoteError("本人分享目录响应格式无效")
            batch = data["list"]
            if len(batch) > _PAGE_SIZE:
                raise RemoteError("本人分享目录页大小无效")
            ids = []
            for record in batch:
                if not isinstance(record, dict):
                    raise RemoteError("本人分享目录记录格式无效")
                share_id = _identifier(record.get("share_id"))
                if share_id in seen:
                    raise RemoteError("本人分享目录包含重复标识")
                seen.add(share_id)
                ids.append(share_id)
            fingerprint = tuple(ids)
            if batch and fingerprint in fingerprints:
                raise RemoteError("本人分享目录重复分页")
            fingerprints.add(fingerprint)
            metadata = payload.get("metadata")
            new_total = metadata.get("_total") if isinstance(metadata, dict) else None
            if new_total is not None:
                if not isinstance(new_total, int) or isinstance(new_total, bool) or new_total < 0:
                    raise RemoteError("本人分享目录总数无效")
                if total is not None and total != new_total:
                    raise _CatalogConsistency("本人分享目录分页总数变化")
                total = new_total
            records.extend(batch)
            if total is not None and len(records) > total:
                raise _CatalogConsistency("本人分享目录超出声明总数")
            if not all_pages:
                return records, total, len(records) < total if total is not None else len(batch) == _PAGE_SIZE
            if total is not None:
                if len(records) == total:
                    return records, total, False
                if len(batch) < _PAGE_SIZE:
                    raise _CatalogConsistency("本人分享目录分页提前结束")
            elif len(batch) < _PAGE_SIZE:
                return records, None, False
        raise RemoteError("本人分享目录超过分页安全上限")

    def _catalog(self, *, all_pages: bool = True) -> tuple[list[dict[str, Any]], int | None, bool]:
        for attempt in range(_READ_ATTEMPTS):
            try:
                return self._catalog_once(all_pages=all_pages)
            except _CatalogConsistency:
                if attempt + 1 == _READ_ATTEMPTS:
                    raise RemoteError("本人分享目录持续不一致，无法完整核验") from None
                time.sleep((0.25, 0.75)[attempt])
        raise AssertionError("bounded catalog read")

    def list_own(self, *, all_pages: bool = False, scope: Scope | None = None) -> dict[str, Any]:
        records, total, more = self._catalog(all_pages=all_pages or scope is not None)
        if scope is None:
            return {"items": [safe_share_record(record) for record in records], "scope_applied": False,
                    "catalog_complete": all_pages, "catalog_total": total,
                    "returned_count": len(records), "has_more": more}
        index = self._account_snapshot(scope.fid, scope)
        verified, outside, unavailable = [], 0, 0
        for record in records:
            try:
                self._prove_roots(record["share_id"], index, scope, record=record)
            except AuthenticationError:
                raise
            except _OutsideScope:
                outside += 1
            except QuarkError:
                unavailable += 1
            else:
                verified.append(safe_share_record(record))
        selected = verified if all_pages else verified[:_PAGE_SIZE]
        return {"items": selected, "scope_applied": True, "catalog_complete": True, "catalog_total": total,
                "verified_total": len(verified), "outside_scope_count": outside, "unverifiable_count": unavailable,
                "returned_count": len(selected), "has_more": len(selected) < len(verified)}

    @staticmethod
    def _identity(record: Mapping[str, Any]) -> dict[str, Any]:
        fid = valid_fid(record.get("fid"))
        if fid == "0":
            return {"fid": "0", "file_name": "/", "file_type": 0}
        try:
            name = validate_name(record.get("file_name"))
        except UsageError:
            raise RemoteError("账户或分享项目名称无法安全核验") from None
        kind = record.get("file_type")
        if not isinstance(kind, int) or isinstance(kind, bool) or kind not in (0, 1):
            raise RemoteError("账户或分享项目类型无效")
        is_directory(record)
        identity = {"fid": fid, "file_name": name, "file_type": kind, "pdir_fid": valid_fid(record.get("pdir_fid"))}
        if kind == 1:
            size = record.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise RemoteError("账户或分享文件大小无效")
            identity["size"] = size
            if record.get("updated_at") is not None:
                if not _number(record["updated_at"]) or record["updated_at"] <= 0:
                    raise RemoteError("账户或分享文件版本无效")
                identity["updated_at"] = record["updated_at"]
        return identity

    @staticmethod
    def _match_share(shared: Mapping[str, Any], expected: Mapping[str, Any], *, root: bool) -> None:
        identity = ShareManager._identity(shared)
        for field in ("fid", "file_name", "file_type", "size"):
            if identity.get(field) != expected.get(field):
                raise RemoteError("分享成员与原账户对象身份不符")
        if (not root or identity.get("pdir_fid") != "0") and identity.get("pdir_fid") != expected.get("pdir_fid"):
            raise RemoteError("分享成员父目录与原账户对象不符")
        if "updated_at" in identity and "updated_at" in expected and identity["updated_at"] != expected["updated_at"]:
            raise RemoteError("分享成员文件版本与原账户对象不符")

    def _exact(self, fid: str) -> dict[str, Any]:
        try:
            return self._identity(self.files.info(fid))
        except AuthenticationError:
            raise AuthenticationError("账户来源核验未获授权") from None
        except QuarkError as original:
            raise contextual_error(original, "账户来源身份无法核验") from None

    def _check_account(self, expected: Mapping[str, Any], scope: Scope | None) -> None:
        # Compare the exact parent before following any ancestry. A moved item
        # must not cause a scope proof to enumerate its new outside parent.
        if expected["fid"] != "0" and self._exact(expected["fid"]) != expected:
            raise RemoteError("账户来源已变化，无法继续分享操作")
        try:
            self.paths.ancestry(expected["fid"])
            if scope:
                scope.check(expected["fid"])
        except AuthenticationError:
            raise AuthenticationError("账户来源范围核验未获授权") from None
        except QuarkError as original:
            raise contextual_error(original, "账户来源父关系或范围无法核验") from None

    def _account_snapshot(self, root: str, scope: Scope | None) -> dict[str, dict[str, Any]]:
        root_identity = self._identity(self.paths.root().item) if root == "0" else self._exact(root)
        snapshot: dict[str, dict[str, Any]] = {}
        stack = [(root_identity, 0)]
        while stack:
            identity, depth = stack.pop()
            fid = identity["fid"]
            if fid in snapshot or len(snapshot) >= 100000 or depth > 1024:
                raise RemoteError("账户子树重复、循环或超过安全上限")
            self._check_account(identity, scope)
            snapshot[fid] = identity
            if identity["file_type"] == 0:
                try:
                    children = self.paths.children(fid)
                except AuthenticationError:
                    raise AuthenticationError("账户子树读取未获授权") from None
                except QuarkError as original:
                    raise contextual_error(original, "账户子树无法完整读取") from None
                for child in reversed(children):
                    child_identity = self._identity(child)
                    if child_identity["fid"] == "0" or child_identity["pdir_fid"] != fid:
                        raise RemoteError("账户子树父目录不符")
                    stack.append((child_identity, depth + 1))
        return snapshot

    def _own_record(self, share_id: str) -> dict[str, Any]:
        for attempt in range(_CONFIRM_ATTEMPTS):
            records, _, _ = self._catalog()
            matches = [record for record in records if record["share_id"] == share_id]
            if len(matches) == 1:
                return matches[0]
            if attempt + 1 < _CONFIRM_ATTEMPTS:
                time.sleep(0.5)
        raise RemoteError("本次分享尚未在完整本人目录中确认")

    def _password(self, share_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._call("post", "share/password", body={"share_id": share_id})
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RemoteError("分享链接响应格式无效")
        if "share_id" in data and data["share_id"] != share_id:
            raise RemoteError("分享链接的内部标识与请求不符")
        pwd_id = _identifier(data.get("pwd_id"))
        if record.get("pwd_id") != pwd_id or record.get("share_id") != share_id:
            raise RemoteError("分享链接与本人目录标识不符")
        url = _share_url(data.get("share_url"), pwd_id)
        if _share_url(record.get("share_url"), pwd_id) != url:
            raise RemoteError("分享链接与本人目录不符")
        result = {"share_id": share_id, "pwd_id": pwd_id, "share_url": url}
        if not _text(data.get("title")) or data["title"] != record.get("title"):
            raise RemoteError("分享标题与本人目录不符")
        result["title"] = data["title"]
        for field, allowed in (("url_type", (1, 2)), ("expired_type", (1, 2, 3, 4))):
            value = data.get(field, record.get(field))
            if not isinstance(value, int) or isinstance(value, bool) or value not in allowed:
                raise RemoteError("分享类型或期限尚未核验")
            if field in record and record[field] != value:
                raise RemoteError("分享类型或期限与本人目录不符")
            result[field] = value
        passcode = data.get("passcode", "")
        if not isinstance(passcode, str) or (passcode and not _CODE.fullmatch(passcode)):
            raise RemoteError("分享提取码响应格式无效")
        if (result["url_type"] == 1 and passcode) or (result["url_type"] == 2 and not passcode):
            raise RemoteError("分享类型与提取码响应矛盾")
        result["passcode"] = passcode
        return result

    def _members(self, password: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        token = self.saver._token(ShareLink(password["pwd_id"], password["passcode"]))
        roots = self.saver._share_items(password["pwd_id"], token, require_owner=True)
        if not roots:
            raise RemoteError("分享根集合为空，无法核验完整成员")
        return token, roots

    def _prove_roots(self, share_id: str, index: Mapping[str, dict[str, Any]], scope: Scope,
                     *, record: Mapping[str, Any]) -> list[dict[str, Any]]:
        password = self._password(share_id, record)
        token, roots = self._members(password)
        if any(root["fid"] not in index for root in roots):
            raise _OutsideScope("分享包含范围外根对象")
        expected = []
        seen: set[str] = set()
        stack = [(root, True, 0) for root in reversed(roots)]
        while stack:
            record, root, depth = stack.pop()
            fid = record["fid"]
            if fid in seen or depth > _MAX_MEMBER_DEPTH or len(seen) >= _MAX_MEMBER_ITEMS:
                raise RemoteError("分享成员重复、循环或超过安全上限")
            # A share can retain historical descendants that no longer belong
            # to the account scope. Never follow their account ancestry or use
            # such a directory as the parent of another share-page request.
            if fid not in index:
                raise _OutsideScope("分享包含范围外成员")
            identity = index[fid]
            self._match_share(record, identity, root=root)
            self._check_account(identity, scope)
            seen.add(fid)
            expected.append(identity)
            if is_directory(record):
                children = self.saver._share_items(password["pwd_id"], token, fid, require_owner=True)
                stack.extend((child, False, depth + 1) for child in reversed(children))
        return expected

    def _poll_creation(self, task_id: str, metadata: Any) -> tuple[dict[str, Any], bool]:
        delay, incomplete = _poll_delay(metadata), False
        for retry in range(_MAX_POLLS):
            time.sleep(delay)
            payload = self._call("get", "task", params={"task_id": task_id, "retry_index": retry})
            data = payload.get("data")
            if not isinstance(data, dict):
                raise RemoteError("创建分享任务响应格式无效")
            if "task_id" in data and str(data["task_id"]) != task_id:
                raise RemoteError("创建分享任务标识与请求不符")
            try:
                incomplete = _incomplete_snapshot(data) or incomplete
            except RemoteError:
                incomplete = True
            status = data.get("status")
            if isinstance(status, str) and status in ("0", "1", "2", "3", "4"):
                status = int(status)
            if not isinstance(status, int) or isinstance(status, bool) or status not in (0, 1, 2, 3, 4):
                raise RemoteError("创建分享任务状态无效")
            finish = data.get("finish")
            if "finish" in data and (not isinstance(finish, bool) or finish != (status == 2)):
                raise RemoteError("创建分享任务完成字段与状态矛盾")
            if status in (3, 4):
                raise _operation_error("创建分享任务失败或暂停；未重新提交", {"result": "failed", "task_id": task_id})
            if status == 2:
                _identifier(data.get("share_id"))
                return data, incomplete
            delay = _poll_delay(payload.get("metadata"))
        raise RemoteError("创建分享任务轮询超时；未重新提交")

    def _prove_created(self, password: Mapping[str, Any], source_fid: str,
                       snapshot: Mapping[str, dict[str, Any]], scope: Scope | None) -> None:
        token, roots = self._members(password)
        if len(roots) != 1 or roots[0]["fid"] != source_fid:
            raise RemoteError("本次分享根集合与请求来源不符")
        stack = [(roots[0], True, 0)]
        seen: set[str] = set()
        while stack:
            record, root, depth = stack.pop()
            fid = record["fid"]
            if fid in seen or fid not in snapshot or depth > 1024 or len(seen) >= 100000:
                raise RemoteError("本次分享包含多余、重复或循环成员")
            self._match_share(record, snapshot[fid], root=root)
            seen.add(fid)
            if is_directory(record):
                records = self.saver._share_items(password["pwd_id"], token, fid, require_owner=True)
                stack.extend((child, False, depth + 1) for child in reversed(records))
        if seen != set(snapshot):
            raise RemoteError("本次分享缺少原来源子树成员")
        for identity in snapshot.values():
            self._check_account(identity, scope)
        if self._account_snapshot(source_fid, scope) != snapshot:
            raise RemoteError("分享创建期间来源子树发生变化")

    def create(self, ref: str, *, passcode: str | None = None,
               expire_days: int = 7, dry_run: bool = False, scope: Scope | None = None,
               verify_tree: bool = True, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        if scope is not None and not verify_tree:
            raise UsageError('受限范围分享必须核验完整子树')
        def report(text):
            if progress is not None:
                progress(text)
        report('正在核对选中项目…')
        if not isinstance(expire_days, int) or isinstance(expire_days, bool) or expire_days not in _EXPIRED:
            raise UsageError("分享有效期必须是 0、1、7 或 30 天")
        if passcode is not None and (not isinstance(passcode, str) or not _CODE.fullmatch(passcode)):
            raise UsageError("私密分享提取码必须是四位 ASCII 字母或数字")
        try:
            obj = self.paths.resolve(ref, inspect_fid=True)
        except AuthenticationError:
            raise AuthenticationError("分享来源读取未获授权") from None
        except RemoteError:
            raise RemoteError("分享来源无法读取") from None
        if obj.fid == "0":
            raise UsageError("账户根目录不能作为分享来源")
        if scope:
            scope.check(obj.fid)
        snapshot = self._account_snapshot(obj.fid, scope) if verify_tree else {obj.fid:self._identity(obj.item)}
        title = snapshot[obj.fid]["file_name"]
        if not _text(title):
            raise UsageError("来源对象名称无法作为安全分享标题")
        plan = {"title": title, "url_type": 2 if passcode is not None else 1,
                "expire_days": expire_days, "expired_type": _EXPIRED[expire_days], "source_fids": [obj.fid]}
        if dry_run:
            return {"result": "preview", "dry_run": True, **plan}
        for identity in snapshot.values():
            self._check_account(identity, scope)
        if verify_tree and self._account_snapshot(obj.fid, scope) != snapshot:
            raise RemoteError("分享来源在提交前发生变化")
        body = {"fid_list": [obj.fid], "title": title, "url_type": plan["url_type"],
                "expired_type": plan["expired_type"], "support_error_code": ["41060"]}
        if passcode is not None:
            body["passcode"] = passcode
        evidence = {"result": "unknown", **plan}
        incomplete = False
        try:
            report('正在提交分享…')
            try:
                submit = self._call("post", "share", body=body, accepted_business_codes=(41060,))
            except QuarkError as error:
                if getattr(error, "api_rejected", False) is True:
                    evidence["result"] = "failed"
                raise
            data = submit.get("data")
            task_id = _identifier(data.get("task_id") if isinstance(data, dict) else None)
            evidence["task_id"] = task_id
            try:
                incomplete = submit.get("code") in (41060, "41060") or _incomplete_snapshot(submit)
            except RemoteError:
                # Still obtain the original task/link for cleanup; malformed
                # snapshot evidence can never become complete success.
                incomplete = True
            report('正在等待服务器生成链接…')
            task, task_incomplete = self._poll_creation(task_id, submit.get("metadata"))
            incomplete = incomplete or task_incomplete
            share_id = task["share_id"]
            evidence["share_id"] = share_id
            report('正在核验链接和分享参数…')
            record = self._own_record(share_id)
            password = self._password(share_id, record)
            # These fields are associated with the original task/internal ID
            # and exact catalog entry. Preserve the actual link and metadata
            # even when the backend did not apply a requested creation option.
            evidence.update(password)
            if password["url_type"] != plan["url_type"] or password["expired_type"] != plan["expired_type"]:
                raise RemoteError("本次分享类型或期限与请求不符")
            if password["passcode"] != (passcode or "") or password["title"] != title:
                raise RemoteError("本次分享提取码或标题与请求不符")
            if incomplete:
                evidence["result"] = "partial"
                raise _operation_error("分享未完整创建；已保留核验后的链接，请人工处理；未重新提交或自动取消", evidence)
            if verify_tree:
                self._prove_created(password, obj.fid, snapshot, scope)
            else:
                _token, roots = self._members(password)
                if len(roots) != 1 or roots[0]['fid'] != obj.fid:
                    raise RemoteError('本次分享根集合与选中项目不符')
                self._match_share(roots[0], snapshot[obj.fid], root=True)
                self._check_account(snapshot[obj.fid], None)
        except AuthenticationError:
            raise _operation_error("分享创建或读回未获授权，结果未确认；未重新提交", evidence, authentication=True) from None
        except (RateLimitError, AccessRestrictedError) as error:
            # A cooldown is not a rejected write: keep its type and delay, and
            # attach the operation evidence the CLI already understands.
            error.share_evidence = safe_operation_result(evidence)
            raise
        except QuarkError as error:
            preserved = getattr(error, "share_evidence", None)
            if preserved is not None:
                evidence.update(preserved)
            raise _operation_error("分享创建结果未完整确认；已保留可用操作证据，未重新提交或自动取消", evidence) from None
        return safe_operation_result({**evidence, "result": "created"})

    def revoke(self, share_id: str, *, dry_run: bool = False, scope: Scope | None = None) -> dict[str, Any]:
        if not isinstance(share_id, str) or not _ID.fullmatch(share_id):
            raise UsageError("取消分享必须指定有效内部 share_id，不能使用 URL 或公开 ID")
        records, _, _ = self._catalog()
        matches = [record for record in records if record["share_id"] == share_id]
        if len(matches) != 1:
            raise UsageError("指定内部 share_id 不在完整本人分享目录中")
        if scope:
            index = self._account_snapshot(scope.fid, scope)
            members = self._prove_roots(share_id, index, scope, record=matches[0])
            for identity in members:
                self._check_account(identity, scope)
        if dry_run:
            return {"result": "preview", "dry_run": True, "share_id": share_id, "revoked": False}
        evidence = {"result": "unknown", "share_id": share_id, "revoked": False}
        try:
            self._call("post", "share/delete", body={"share_ids": [share_id]})
        except AuthenticationError as error:
            if getattr(error, "api_rejected", False) is True:
                evidence["result"] = "failed"
            raise _operation_error("取消分享未获授权；未重新提交", evidence, authentication=True) from None
        except (RateLimitError, AccessRestrictedError) as error:
            # An explicit business rejection (``api_rejected``) is a definite
            # refusal, so record it as failed. An ambiguous transport 429/403
            # carries no such marker and stays unknown. A restriction can follow
            # a submitted write, so the typed error and its delay are preserved,
            # the operation evidence is attached, and the write is never closed
            # out, replayed or read back while the cooldown is active.
            if getattr(error, "api_rejected", False) is True:
                evidence["result"] = "failed"
            error.share_evidence = safe_operation_result(evidence)
            raise
        except RemoteError as error:
            if getattr(error, "api_rejected", False) is True:
                raise _operation_error("服务器明确拒绝取消分享；未重新提交", {**evidence, "result": "failed"}) from None
            # A lost response can only be resolved by this exact ID disappearing
            # from a complete own catalog; no delete request is repeated.
            pass
        try:
            for attempt in range(_CONFIRM_ATTEMPTS):
                records, _, _ = self._catalog()
                if not any(record["share_id"] == share_id for record in records):
                    return {"result": "revoked", "share_id": share_id, "revoked": True}
                if attempt + 1 < _CONFIRM_ATTEMPTS:
                    time.sleep(0.5)
        except AuthenticationError:
            raise _operation_error("取消分享后的本人目录读取未获授权，结果未确认；未重新提交", evidence, authentication=True) from None
        except (RateLimitError, AccessRestrictedError) as error:
            error.share_evidence = safe_operation_result(evidence)
            raise
        except QuarkError:
            raise _operation_error("取消分享后的本人目录无法完整读取，结果未知；未重新提交", evidence) from None
        raise _operation_error("取消分享未在完整本人目录中确认，结果未知；未重新提交", evidence)
