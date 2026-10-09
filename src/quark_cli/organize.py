"""Complete read-only reports and conservative, single-submit batch renames."""

from __future__ import annotations

import math
import re
import time
import unicodedata
from collections import defaultdict
from typing import Any

from .errors import QuarkError, RemoteError, UsageError
from .files import Files
from .paths import RemotePaths, Scope, is_directory, valid_fid, validate_name
from .recursive import _names, local_name


def _size(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _hashes(item: dict[str, Any]) -> dict[str, str]:
    return {key: value.lower() for key, length in (("md5", 32), ("sha1", 40))
            if isinstance(value := item.get(key), str) and re.fullmatch(r"[0-9a-fA-F]{" + str(length) + r"}", value)}


def _kind(item: dict[str, Any]) -> int:
    if type(item.get("file_type")) is not int or item["file_type"] not in (0, 1):
        raise RemoteError("整理记录缺少明确文件类型")
    return 0 if is_directory(item) else 1


def _key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _file(item: dict[str, Any]) -> dict[str, Any]:
    result = {key: item[key] for key in ("fid", "path", "file_name") if isinstance(item.get(key), str)}
    if _size(item.get("size")) is not None:
        result["size"] = item["size"]
    result.update(_hashes(item))
    return result


def safe_operation_result(value: Any) -> dict[str, Any]:
    """Filter service results/evidence again at the CLI trust boundary."""
    if not isinstance(value, dict):
        return {}
    output = {}
    for key in ("action", "result", "parent_fid", "path"):
        if isinstance(value.get(key), str):
            output[key] = value[key]
    for key in ("dry_run", "complete"):
        if type(value.get(key)) is bool:
            output[key] = value[key]
    for key in ("files", "directories", "known_bytes", "unknown_size_files", "hash_unavailable_files", "renamed", "unchanged"):
        if _size(value.get(key)) is not None:
            output[key] = value[key]
    if isinstance(value.get("items"), list):
        output["items"] = [{key: item[key] for key in ("fid", "old_name", "new_name", "kind", "result")
                            if isinstance(item, dict) and isinstance(item.get(key), str)} for item in value["items"] if isinstance(item, dict)]
    for key in ("confirmed", "candidates"):
        if isinstance(value.get(key), list):
            groups = []
            for group in value[key]:
                if not isinstance(group, dict) or not isinstance(group.get("items"), list):
                    continue
                safe = {"items": [_file(item) for item in group["items"] if isinstance(item, dict)]}
                if group.get("reason") in ("size", "name"):
                    safe["reason"] = group["reason"]
                if isinstance(group.get("hashes"), dict):
                    safe["hashes"] = _hashes(group["hashes"])
                if _size(group.get("size")) is not None:
                    safe["size"] = group["size"]
                groups.append(safe)
            output[key] = groups
    return output


def _error(error: QuarkError, message: str, evidence=None) -> QuarkError:
    result = type(error)(message)
    if evidence is not None:
        result.organize_evidence = safe_operation_result(evidence)
    return result


def compile_rename_expression(pattern: str, replacement: str) -> re.Pattern[str]:
    """Validate regex and replacement references without credentials or I/O."""
    if not isinstance(pattern, str) or not isinstance(replacement, str):
        raise UsageError("正则和替换文本必须为字符串")
    try:
        expression = re.compile(pattern)
        expression.sub(replacement, "")  # Validate references even for an empty directory.
    except (re.error, IndexError, OverflowError):
        raise UsageError("正则表达式或替换引用无效") from None
    return expression


class _ReadLag(RemoteError):
    pass


class Organize:
    def __init__(self, client) -> None:
        self.files = Files(client)
        self.paths = RemotePaths(self.files)

    def _checked(self, listed: dict[str, Any], parent: str, scope: Scope | None) -> dict[str, Any]:
        fid = valid_fid(listed.get("fid"))
        if fid == "0" or valid_fid(listed.get("pdir_fid")) != parent:
            raise RemoteError("整理目录成员父关系无效")
        exact = self.files.info(fid)
        # Refuse a moved/forged child before following its declared ancestry.
        if valid_fid(exact.get("pdir_fid")) != parent or exact.get("file_name") != listed.get("file_name") or _kind(exact) != _kind(listed):
            raise RemoteError("整理对象详情与目录成员不一致")
        if scope:
            scope.check(fid)
        validate_name(exact.get("file_name"))
        if _kind(exact) == 1:
            if _size(exact.get("size")) is not None and _size(listed.get("size")) is not None and exact["size"] != listed["size"]:
                raise RemoteError("整理文件大小证据不一致")
        result = dict(exact)
        if _size(exact.get("size")) is None or _size(listed.get("size")) is None:
            result.pop("size", None)
        exact_hashes, listed_hashes = _hashes(exact), _hashes(listed)
        result["_hash_conflict"] = bool(listed.get("_hash_conflict")) or any(exact_hashes[key] != listed_hashes[key] for key in exact_hashes.keys() & listed_hashes.keys())
        return result

    def _root(self, ref: str, scope: Scope | None):
        obj = self.paths.resolve(ref, inspect_fid=True)
        if scope:
            scope.check(obj.fid)
        chain = self.paths.ancestry(obj.fid)
        exact = dict(chain[0].item or {})
        if obj.item and obj.fid != "0" and (obj.item.get("file_name") != exact.get("file_name") or _kind(obj.item) != _kind(exact)):
            raise RemoteError("整理根对象身份不一致")
        if obj.fid != "0":
            parent = valid_fid(exact.get("pdir_fid"))
            matches = [item for item in self.paths.children(parent) if valid_fid(item.get("fid")) == obj.fid]
            if len(matches) != 1:
                raise RemoteError("整理根对象父目录成员无效")
            exact = self._checked(matches[0], parent, scope)
        exact["path"] = "/" + "/".join(validate_name((ancestor.item or {}).get("file_name")) for ancestor in reversed(chain[:-1]))
        return exact

    def _walk(self, ref: str, scope: Scope | None) -> list[dict[str, Any]]:
        root = self._root(ref, scope)
        stack = [(root, 0)]
        seen, records = set(), []
        while stack:
            item, depth = stack.pop()
            fid = valid_fid(item.get("fid"))
            if fid in seen or depth > 1024 or len(seen) >= 100000:
                raise RemoteError("整理遍历发现重复、循环或超过安全上限")
            seen.add(fid)
            if fid != "0":
                path = item["path"]
                item = self._checked(item, valid_fid(item.get("pdir_fid")), scope)
                item["path"] = path
            records.append(item)
            if _kind(item) == 0:
                children = self.paths.children(fid)
                if len(seen) + len(stack) + len(children) > 100000:
                    raise RemoteError("整理遍历超过对象数量上限")
                for child in reversed(children):
                    child_fid = valid_fid(child.get("fid"))
                    if child_fid in seen:
                        raise RemoteError("整理遍历发现重复或循环 FID")
                    checked = self._checked(child, fid, scope)
                    checked["path"] = item["path"].rstrip("/") + "/" + checked["file_name"]
                    stack.append((checked, depth + 1))
        return records

    def du(self, ref: str, scope: Scope | None = None) -> dict[str, Any]:
        try:
            records = self._walk(ref, scope)
            files = [item for item in records if _kind(item) == 1]
            unknown = sum(_size(item.get("size")) is None for item in files)
            return {"action": "du", "path": records[0]["path"], "files": len(files), "directories": len(records) - len(files),
                    "known_bytes": sum(_size(item.get("size")) or 0 for item in files), "unknown_size_files": unknown, "complete": unknown == 0}
        except QuarkError as exc:
            raise _error(exc, "容量统计未完成：路径、身份、范围或读取检查失败") from None

    def duplicates(self, ref: str, scope: Scope | None = None) -> dict[str, Any]:
        try:
            records = self._walk(ref, scope)
            files = [item for item in records if _kind(item) == 1]
            buckets = defaultdict(list)
            for item in files:
                if _size(item.get("size")) is not None and not item.get("_hash_conflict"):
                    for algorithm, digest in _hashes(item).items():
                        buckets[(item["size"], algorithm, digest)].append(item)
            confirmed, confirmed_sets = [], set()
            for (size, algorithm, digest), members in buckets.items():
                ids = frozenset(item["fid"] for item in members)
                if len(members) < 2 or ids in confirmed_sets:
                    continue
                # A missing second hash must not bridge two contradictory hashes.
                evidence = {key: {h[key] for item in members if key in (h := _hashes(item))} for key in ("md5", "sha1")}
                if any(len(values) > 1 for values in evidence.values()):
                    continue
                confirmed_sets.add(ids)
                confirmed.append({"size": size, "hashes": {algorithm: digest}, "items": [_file(item) for item in members]})
            candidates = []
            for reason in ("size", "name"):
                possible = defaultdict(list)
                for item in files:
                    value = _size(item.get("size")) if reason == "size" else item["file_name"]
                    if value is not None:
                        possible[value].append(item)
                for members in possible.values():
                    if len(members) > 1 and frozenset(item["fid"] for item in members) not in confirmed_sets:
                        candidates.append({"reason": reason, "items": [_file(item) for item in members]})
            return safe_operation_result({"action": "duplicates", "path": records[0]["path"], "files": len(files), "complete": True,
                "hash_unavailable_files": sum(not _hashes(item) or bool(item.get("_hash_conflict")) for item in files),
                "confirmed": confirmed, "candidates": candidates})
        except QuarkError as exc:
            raise _error(exc, "重复文件检查未完成：路径、身份、范围或读取检查失败") from None

    @staticmethod
    def _signature(item: dict[str, Any]) -> dict[str, Any]:
        signature = {"fid": valid_fid(item.get("fid")), "pdir_fid": valid_fid(item.get("pdir_fid")),
                     "file_name": validate_name(item.get("file_name")), "file_type": _kind(item)}
        if signature["file_type"] == 1:
            version = item.get("updated_at")
            try:
                if _size(item.get("size")) is None or isinstance(version, bool) or not isinstance(version, (int, float, str)) or (isinstance(version, str) and (not version or version != version.strip())) or not math.isfinite(float(version)) or float(version) <= 0:
                    raise ValueError()
            except (TypeError, ValueError, OverflowError):
                raise UsageError("批量改名需要可核验文件大小和版本更新时间") from None
            signature.update(size=item["size"], updated_at=version)
        return signature

    def _siblings(self, parent: str, scope: Scope | None) -> dict[str, dict[str, Any]]:
        result = {}
        for listed in self.paths.children(parent):
            fid = valid_fid(listed.get("fid"))
            if fid in result:
                raise RemoteError("改名目录存在重复 FID")
            signature = self._signature(self._checked(listed, parent, scope))
            if self._signature(listed) != signature:
                raise RemoteError("改名对象列表与详情版本不一致")
            result[fid] = signature
        final = {valid_fid(item.get("fid")): self._signature(item) for item in self.paths.children(parent)}
        if final != result:
            raise UsageError("改名目录在核验期间变化，已停止")
        return result

    def _confirm(self, fid: str, expected: dict[str, Any], old_name: str, new_name: str, scope: Scope | None, *, accepted: bool) -> dict[str, Any]:
        for attempt in range(6):
            try:
                exact = self.files.info(fid)
                signature = self._signature(exact)
                if any(signature[key] != expected[key] for key in ("fid", "pdir_fid", "file_type")) or (signature["file_type"] == 1 and signature["size"] != expected["size"]):
                    raise RemoteError("改名确认的确切对象身份变化")
                if signature["file_name"] == old_name and accepted:
                    raise _ReadLag("改名详情尚未读回")
                if signature["file_name"] != new_name:
                    raise RemoteError("改名未能确认计划名称")
                matches = [item for item in self.paths.children(expected["pdir_fid"]) if valid_fid(item.get("fid")) == fid]
                if not matches or (len(matches) == 1 and matches[0].get("file_name") == old_name):
                    raise _ReadLag("改名父目录索引尚未读回")
                if len(matches) != 1:
                    raise RemoteError("改名父目录成员不唯一")
                listed = self._signature(matches[0])
                if listed != signature:
                    if all(listed.get(key) == signature.get(key) for key in ("fid", "pdir_fid", "file_name", "file_type", "size")):
                        raise _ReadLag("改名版本索引尚未读回")
                    raise RemoteError("改名父目录身份不一致")
                if scope:
                    scope.check(fid)
                return signature
            except _ReadLag:
                if attempt == 5:
                    raise RemoteError("改名读回在有限等待后仍未一致") from None
                time.sleep(0.5)
        raise RemoteError("改名无法确认")

    def rename_batch(self, ref: str, pattern: str, replacement: str, apply: bool = False, dry_run: bool = False, scope: Scope | None = None) -> dict[str, Any]:
        evidence = {"action": "rename-batch", "result": "preview", "dry_run": not apply or dry_run, "renamed": 0, "unchanged": 0, "items": []}
        try:
            expression = compile_rename_expression(pattern, replacement)
            root = self._root(ref, scope)
            if _kind(root) != 0:
                raise UsageError("批量改名需要目录")
            parent = valid_fid(root["fid"])
            evidence.update(parent_fid=parent, path=root["path"])
            original = self._siblings(parent, scope)
            _names(item["file_name"] for item in original.values())
            old_names = {_key(item["file_name"]): fid for fid, item in original.items()}
            plans = []
            for fid, item in original.items():
                new_name = local_name(expression.sub(replacement, item["file_name"]))
                changed = new_name != item["file_name"]
                if changed and _key(new_name) in old_names and old_names[_key(new_name)] != fid:
                    raise UsageError("改名目标已占用或依赖交换/改名链")
                if changed and scope:
                    scope.check(fid, mutate_object=True, write=True)
                plans.append({"fid": fid, "old_name": item["file_name"], "new_name": new_name,
                              "kind": "directory" if item["file_type"] == 0 else "file", "result": "pending" if changed else "unchanged"})
            _names(item["new_name"] for item in plans)
            evidence["items"] = plans
            evidence["unchanged"] = sum(item["result"] == "unchanged" for item in plans)
            if not apply or dry_run:
                return safe_operation_result(evidence)
            expected = {fid: dict(item) for fid, item in original.items()}
            for plan in plans:
                if plan["result"] == "unchanged":
                    continue
                # Check the root and full namespace again before every mutation.
                current_root = self._root(parent, scope)
                if any(current_root.get(key) != root.get(key) for key in ("fid", "pdir_fid", "file_name", "file_type")):
                    raise UsageError("改名目录身份发生变化，已停止后续提交")
                if self._siblings(parent, scope) != expected:
                    raise UsageError("改名目录集合或文件版本发生变化，已停止后续提交")
                if scope:
                    scope.check(plan["fid"], mutate_object=True, write=True)
                if self._signature(self.files.info(plan["fid"])) != expected[plan["fid"]]:
                    raise UsageError("改名确切对象在提交前变化，已停止")
                plan["result"] = "unknown"
                submitted_error = None
                try:
                    self.files.rename(plan["fid"], plan["new_name"])
                except QuarkError as exc:
                    if getattr(exc, "api_rejected", False):
                        plan["result"] = "failed"
                        raise
                    submitted_error = exc
                try:
                    confirmed = self._confirm(plan["fid"], expected[plan["fid"]], plan["old_name"], plan["new_name"], scope, accepted=submitted_error is None)
                except QuarkError as exc:
                    raise submitted_error or exc
                plan["result"] = "confirmed"
                evidence["renamed"] += 1
                expected[plan["fid"]] = confirmed
            evidence["result"] = "completed"
            return safe_operation_result(evidence)
        except QuarkError as exc:
            evidence["result"] = "partial" if evidence["renamed"] else ("unknown" if any(item["result"] == "unknown" for item in evidence["items"]) else "failed")
            raise _error(exc, "批量改名未完成；已停止后续提交，未重发未知写操作", evidence) from None
