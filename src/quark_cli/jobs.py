"""Durable incremental share jobs with verified FID mappings and intents."""

from __future__ import annotations

import copy
import math
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from .errors import LocalIOError, QuarkError, RemoteError, UsageError
from .paths import Scope, is_directory, path_components, valid_fid, validate_name
from .shares import ShareSaver, ShareSelection, _optional_created_fid, _required_text, parse_share_url, validate_patterns
from .state import StateLock, config_directory, read_json, write_json


_IDENTITY_FIELDS = {"fid", "file_name", "file_type", "size", "updated_at"}


class _SubtreeLag(RemoteError):
    pass


def _identity(item: dict[str, Any]) -> dict[str, Any]:
    result = {"fid": valid_fid(item.get("fid")), "file_name": validate_name(item.get("file_name")),
              "file_type": 0 if is_directory(item) else 1}
    if result["fid"] == "0":
        raise UsageError("来源或目标成员 FID 无效")
    if result["file_type"] == 1:
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise UsageError("文件缺少可核验大小，不能建立增量身份")
        result["size"] = size
        version = item.get("updated_at")
        try:
            if isinstance(version, bool) or not isinstance(version, (int, float, str)) or (isinstance(version, str) and (not version or version != version.strip())) or not math.isfinite(float(version)) or float(version) <= 0:
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            raise UsageError("文件缺少有效版本更新时间，不能建立增量身份") from None
        result["updated_at"] = version
    return result


def _valid_identity(value: Any) -> None:
    if not isinstance(value, dict) or set(value) - _IDENTITY_FIELDS or _identity(value) != value:
        raise ValueError()


class _Scopes:
    def __init__(self, scopes: list[Scope]) -> None:
        self.scopes = scopes

    def check(self, fid: str, **options) -> None:
        for scope in self.scopes:
            scope.check(fid, **options)

    def check_target(self, target) -> None:
        for scope in self.scopes:
            scope.check_target(target)


class Jobs:
    def __init__(self, saver: ShareSaver | None = None, *, state_dir: Path | None = None) -> None:
        self.saver = saver
        self.directory = state_dir if state_dir is not None else config_directory()
        self.config_path, self.ledger_path = self.directory / "jobs.json", self.directory / "jobs-ledger.json"

    def _load(self) -> tuple[dict[str, Any], dict[str, Any]]:
        config = read_json(self.config_path, default={"version": 1, "jobs": {}})
        ledger = read_json(self.ledger_path, default={"version": 1, "jobs": {}})
        try:
            for document in (config, ledger):
                if set(document) != {"version", "jobs"} or type(document["version"]) is not int or document["version"] != 1 or not isinstance(document["jobs"], dict):
                    raise ValueError()
            for name, job in config["jobs"].items():
                validate_name(name)
                if not isinstance(job, dict) or set(job) != {"id", "url", "share_id", "passcode", "target", "source", "source_fid", "include", "exclude", "scope_fid"}:
                    raise ValueError()
                if not isinstance(job["id"], str) or len(job["id"]) != 32 or any(c not in "0123456789abcdef" for c in job["id"]):
                    raise ValueError()
                link = parse_share_url(job["url"], job["passcode"])
                if link.pwd_id != job["share_id"] or "?" in job["url"] or job["id"] not in ledger["jobs"]:
                    raise ValueError()
                path_components(job["target"])
                path_components(job["source"])
                valid_fid(job["source_fid"])
                for key in ("include", "exclude"):
                    if not isinstance(job[key], list):
                        raise ValueError()
                    validate_patterns(job[key])
                if job["scope_fid"] is not None:
                    valid_fid(job["scope_fid"])
                if ledger["jobs"][job["id"]]["binding"]["path"] != job["target"]:
                    raise ValueError()
            for entry in ledger["jobs"].values():
                if not isinstance(entry, dict) or set(entry) != {"binding", "mappings", "pending"}:
                    raise ValueError()
                binding = entry["binding"]
                if set(binding) != {"path", "fid", "parent_fid", "name"}:
                    raise ValueError()
                path_components(binding["path"])
                valid_fid(binding["parent_fid"])
                if binding["name"]:
                    validate_name(binding["name"])
                if binding["fid"] is not None:
                    valid_fid(binding["fid"])
                if not isinstance(entry["mappings"], dict):
                    raise ValueError()
                for fid, mapping in entry["mappings"].items():
                    if set(mapping) != {"source", "target", "source_parent", "target_parent"}:
                        raise ValueError()
                    _valid_identity(mapping["source"])
                    _valid_identity(mapping["target"])
                    if fid != mapping["source"]["fid"]:
                        raise ValueError()
                    valid_fid(mapping["source_parent"])
                    valid_fid(mapping["target_parent"])
                pending = entry["pending"]
                if pending is not None:
                    if not isinstance(pending, dict) or set(pending) != {"kind", "target_fid", "parent_fid", "name", "source_parent", "source_ids", "before_fids", "identities", "parents", "task_id", "created_fid", "confirmation_started"} or pending["kind"] not in ("mkdir", "save") or type(pending["confirmation_started"]) is not bool:
                        raise ValueError()
                    valid_fid(pending["parent_fid"])
                    valid_fid(pending["source_parent"])
                    for key in ("target_fid", "task_id", "created_fid"):
                        if pending[key] is not None:
                            valid_fid(pending[key])
                    if pending["kind"] == "save" and (pending["target_fid"] is None or not pending["source_ids"]):
                        raise ValueError()
                    if pending["kind"] == "mkdir":
                        validate_name(pending["name"])
                    for key in ("source_ids", "before_fids"):
                        if not isinstance(pending[key], list) or len(set(pending[key])) != len(pending[key]):
                            raise ValueError()
                        for fid in pending[key]:
                            valid_fid(fid)
                    if not isinstance(pending["identities"], dict):
                        raise ValueError()
                    for fid, identity in pending["identities"].items():
                        _valid_identity(identity)
                        if fid != identity["fid"]:
                            raise ValueError()
                    if not isinstance(pending["parents"], dict) or set(pending["parents"]) != set(pending["identities"]):
                        raise ValueError()
                    for parent_fid in pending["parents"].values():
                        valid_fid(parent_fid)
                    if pending["kind"] == "mkdir":
                        if binding["fid"] is not None or pending["parent_fid"] != binding["parent_fid"] or pending["name"] != binding["name"] or pending["target_fid"] is not None or pending["task_id"] is not None or pending["source_ids"] or pending["identities"] or pending["confirmation_started"]:
                            raise ValueError()
                    elif not set(pending["source_ids"]).issubset(pending["identities"]) or pending["parent_fid"] != pending["target_fid"] or pending["created_fid"] is not None:
                        raise ValueError()
        except (QuarkError, ValueError, TypeError, KeyError):
            raise LocalIOError("任务配置或账本损坏，已停止且未重置") from None
        return config, ledger

    @staticmethod
    def _public(name: str, job: dict[str, Any]) -> dict[str, Any]:
        return {"name": name, "job_id": job["id"], "share_id": job["share_id"], "destination_path": job["target"],
                "source_path": job["source"], "scope_fid": job["scope_fid"], "include": job["include"], "exclude": job["exclude"]}

    def list(self) -> dict[str, Any]:
        with StateLock(self.directory):
            config, ledger = self._load()
            return {"jobs": [{**self._public(name, job), "pending": ledger["jobs"][job["id"]]["pending"] is not None}
                             for name, job in config["jobs"].items()]}

    def remove(self, name: str) -> dict[str, Any]:
        validate_name(name)
        with StateLock(self.directory):
            config, _ = self._load()
            if name not in config["jobs"]:
                raise UsageError("任务不存在")
            del config["jobs"][name]
            write_json(self.config_path, config)
            return {"name": name, "removed": True, "ledger_retained": True}

    def add(self, name: str, url: str, target_path: str, *, source: str = "/", passcode: str | None = None,
            include: list[str] | tuple[str, ...] = (), exclude: list[str] | tuple[str, ...] = (), scope: Scope | None = None) -> dict[str, Any]:
        validate_name(name)
        path_components(target_path)
        path_components(source)
        includes, excludes = validate_patterns(include), validate_patterns(exclude)
        link = parse_share_url(url, passcode)
        with StateLock(self.directory):
            config, ledger = self._load()
            if name in config["jobs"]:
                raise UsageError("同名任务已存在")
            target = self.saver.paths.directory_target(target_path, allow_missing_leaf=True)
            self.saver.paths.ancestry(target.parent_fid if target.missing else target.fid)
            if scope:
                scope.check_target(target)
            selection = self.saver.select(url, source=source, passcode=passcode, include=includes, exclude=excludes)
            identifier = uuid.uuid4().hex
            job = {"id": identifier, "url": f"https://pan.quark.cn/s/{link.pwd_id}", "share_id": link.pwd_id,
                   "passcode": link.passcode or None, "target": target_path, "source": source, "source_fid": selection.source_fid,
                   "include": list(includes), "exclude": list(excludes), "scope_fid": scope.fid if scope else None}
            ledger["jobs"][identifier] = {"binding": {"path": target_path, "fid": target.fid, "parent_fid": target.parent_fid, "name": target.name}, "mappings": {}, "pending": None}
            write_json(self.ledger_path, ledger)
            config["jobs"][name] = job
            write_json(self.config_path, config)
            return {"created": True, **self._public(name, job)}

    def _persist(self, ledger: dict[str, Any], dry_run: bool) -> None:
        if not dry_run:
            write_json(self.ledger_path, ledger)

    def _inventory(self, selection: ShareSelection) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
        nodes, children = {}, {}
        stack = [(selection.source_fid, selection.items, 0)]
        seen = {selection.source_fid}
        while stack:
            parent, items, depth = stack.pop()
            if depth > 1024 or len(nodes) > 100000:
                raise RemoteError("增量来源遍历超过安全上限")
            if any(count > 1 for count in Counter(item["file_name"] for item in items).values()):
                raise UsageError("增量来源同级名称不唯一")
            children[parent] = []
            for item in items:
                identity = _identity(item)
                fid = identity["fid"]
                if fid in seen:
                    raise RemoteError("增量来源出现重复或循环 FID")
                seen.add(fid)
                _required_text(item, "share_fid_token")
                nodes[fid] = {"item": item, "identity": identity, "parent": parent}
                children[parent].append(fid)
                if identity["file_type"] == 0:
                    descendants = self.saver._share_items(selection.pwd_id, selection.stoken, fid)
                    stack.append((fid, descendants, depth + 1))
        return nodes, children

    def _target(self, entry: dict[str, Any], scopes: _Scopes) -> None:
        binding = entry["binding"]
        current = self.saver.paths.directory_target(binding["path"], allow_missing_leaf=True)
        if current.fid != binding["fid"] or current.parent_fid != binding["parent_fid"] or current.name != binding["name"]:
            raise UsageError("目标绑定已变化，禁止按同名目录重新绑定")
        self.saver.paths.ancestry(current.parent_fid if current.missing else current.fid)
        scopes.check_target(current)

    def _guard(self, identifier: str, target_fid: str, ledger: dict[str, Any]) -> None:
        ancestors = {obj.fid for obj in self.saver.paths.ancestry(target_fid)}
        for other_id, entry in ledger["jobs"].items():
            pending = entry["pending"]
            if other_id != identifier and pending is not None:
                blocked_fid = pending["parent_fid"] if pending["kind"] == "mkdir" else pending["target_fid"]
                if blocked_fid in ancestors:
                    raise UsageError("该目标存在其他任务的未确认意图，禁止继续写入")

    def _check_mappings(self, entry, nodes, scopes) -> None:
        seen_targets = set()
        cached = {}
        for fid, mapping in entry["mappings"].items():
            node = nodes.get(fid)
            if node is None or node["identity"] != mapping["source"] or node["parent"] != mapping["source_parent"]:
                raise UsageError("已记录来源消失或身份变化，增量冲突")
            parent = mapping["target_parent"]
            scopes.check(parent, write=True)
            self.saver.paths.ancestry(parent)
            if parent not in cached:
                cached[parent] = self.saver._list_children(parent)
            matches = [item for item in cached[parent] if item.get("fid") == mapping["target"]["fid"]]
            if len(matches) != 1 or _identity(matches[0]) != mapping["target"] or mapping["target"]["fid"] in seen_targets:
                raise UsageError("已记录目标丢失、移动或身份变化，禁止同名重新绑定")
            seen_targets.add(mapping["target"]["fid"])
            actual = self.saver.paths.ancestry(mapping["target"]["fid"])[0].item or {}
            if valid_fid(actual.get("pdir_fid")) != parent or _identity(actual) != mapping["target"]:
                raise UsageError("目标精确详情与账本身份或父关系不一致")
            expected_parent = entry["binding"]["fid"] if node["parent"] not in entry["mappings"] else entry["mappings"][node["parent"]]["target"]["fid"]
            if parent != expected_parent:
                raise UsageError("目标账本父关系不一致")

    @staticmethod
    def _subtree(roots, children) -> list[str]:
        output, stack = [], list(roots)
        while stack:
            fid = stack.pop()
            output.append(fid)
            stack.extend(children.get(fid, []))
        return output

    def _map_new(self, entry, pending, confirmed, nodes, children, scopes):
        staged = {}
        known_targets = {mapping["target"]["fid"] for mapping in entry["mappings"].values()}
        roots = {item["file_name"]: item for item in confirmed}
        stack = [(fid, roots[nodes[fid]["identity"]["file_name"]], pending["target_fid"]) for fid in pending["source_ids"]]
        while stack:
            fid, target, parent = stack.pop()
            source = nodes[fid]["identity"]
            destination = _identity(target)
            target_fid = destination["fid"]
            if target_fid in known_targets or (parent == pending["target_fid"] and target_fid in pending["before_fids"]):
                raise UsageError("转存结果不是唯一的新目标 FID，不能建立映射")
            if source["file_name"] != destination["file_name"] or source["file_type"] != destination["file_type"] or (source["file_type"] == 1 and source["size"] != destination["size"]):
                raise UsageError("转存结果的名称、类型或大小无法对应来源")
            known_targets.add(target_fid)
            scopes.check(target_fid, write=True)
            actual = self.saver.paths.ancestry(target_fid)[0].item or {}
            if valid_fid(actual.get("pdir_fid")) != parent or _identity(actual) != destination:
                raise UsageError("新目标精确详情与转存身份或父关系不一致")
            staged[fid] = {"source": source, "target": destination, "source_parent": nodes[fid]["parent"], "target_parent": parent}
            if source["file_type"] == 0:
                listed = self.saver._list_children(target_fid)
                expected = children[fid]
                by_name = {item["file_name"]: item for item in listed}
                expected_names = {nodes[child]["identity"]["file_name"] for child in expected}
                if len(by_name) != len(listed) or set(by_name) - expected_names:
                    raise UsageError("新目录子树无法完整唯一对应，保留未确认意图")
                if expected_names - set(by_name):
                    raise _SubtreeLag("新目录子树索引尚未完整可见")
                stack.extend((child, by_name[nodes[child]["identity"]["file_name"]], target_fid) for child in expected)
        return staged

    def _finish_save(self, entry, pending, selection, nodes, children, scopes, before_mapping):
        expected_target = entry["binding"]["fid"] if pending["source_parent"] == selection.source_fid else entry["mappings"].get(pending["source_parent"], {}).get("target", {}).get("fid")
        if pending["target_fid"] != expected_target or any(fid in entry["mappings"] or fid not in nodes or nodes[fid]["parent"] != pending["source_parent"] for fid in pending["source_ids"]) or set(pending["identities"]) != set(self._subtree(pending["source_ids"], children)):
            raise UsageError("未确认意图的来源或目标父关系与绑定不符")
        for fid, identity in pending["identities"].items():
            if fid not in nodes or nodes[fid]["identity"] != identity or nodes[fid]["parent"] != pending["parents"][fid]:
                raise UsageError("未确认任务的来源身份已变化，禁止重新提交")
        if pending["task_id"] is None:
            raise UsageError("转存受理未知且没有 task_id，禁止自动重发")
        has_directory = any(nodes[fid]["identity"]["file_type"] == 0 for fid in pending["source_ids"])
        if has_directory and pending["confirmation_started"]:
            raise UsageError("目录映射确认已中断，需要人工核验，禁止自动补绑定或重发")
        task = self.saver._poll_task(pending["task_id"], poll_interval=0.5, max_polls=120)
        subset = ShareSelection(selection.pwd_id, pending["source_parent"], selection.source_path,
                                [nodes[fid]["item"] for fid in pending["source_ids"]], selection.stoken)
        confirmed = self.saver._confirm_saved_items(subset, pending["target_fid"], pending["task_id"], task)
        if has_directory:
            pending["confirmation_started"] = True
            before_mapping()
        for attempt in range(6):
            if attempt:
                time.sleep(0.5)
            try:
                return self._map_new(entry, pending, confirmed, nodes, children, scopes)
            except _SubtreeLag:
                if attempt == 5:
                    raise RemoteError("新目录子树读回超时，保留未确认意图且未重发") from None

    def _new_intent(self, kind, target_fid, parent_fid, name, source_parent, source_ids, before_fids, identities, parents=None):
        return {"kind": kind, "target_fid": target_fid, "parent_fid": parent_fid, "name": name,
                "source_parent": source_parent, "source_ids": source_ids, "before_fids": before_fids,
                "identities": identities, "parents": parents or {}, "task_id": None, "created_fid": None, "confirmation_started": False}

    def _run_job(self, job, ledger, current_scope, dry_run):
        entry = ledger["jobs"][job["id"]]
        scopes = _Scopes(([Scope(self.saver.paths, job["scope_fid"])] if job["scope_fid"] is not None else []) + ([current_scope] if current_scope else []))
        selection = self.saver.select(job["url"], source=job["source"], passcode=job["passcode"], include=job["include"], exclude=job["exclude"])
        if selection.source_fid != job["source_fid"]:
            raise UsageError("分享源目录 FID 已变化，禁止同名重新绑定")
        nodes, children = self._inventory(selection)
        pending = entry["pending"]
        if pending is not None and pending["kind"] == "mkdir":
            if pending["created_fid"] is None:
                raise UsageError("建目录受理未知且没有 FID，禁止自动重发")
            scopes.check(pending["parent_fid"], write=True)
            created = self.saver._confirm_created_directory(pending["parent_fid"], pending["name"], pending["created_fid"])
            if created != pending["created_fid"] or created in pending["before_fids"]:
                raise UsageError("未知目录无法唯一对应创建结果")
            scopes.check(created, write=True)
            entry["binding"]["fid"], entry["pending"] = created, None
            self._persist(ledger, dry_run)
        self._target(entry, scopes)
        self._check_mappings(entry, nodes, scopes)
        initial_count = len(entry["mappings"])
        pending = entry["pending"]
        if pending is not None:
            scopes.check(pending["target_fid"], write=True)
            staged = self._finish_save(entry, pending, selection, nodes, children, scopes, lambda: self._persist(ledger, dry_run))
            entry["mappings"].update(staged)
            entry["pending"] = None
            self._persist(ledger, dry_run)

        # Validate every known destination and planned name conflict before any
        # new mutation. Unmapped directories are copied whole by their parent.
        groups = []
        for parent, source_ids in children.items():
            if parent == selection.source_fid:
                target = entry["binding"]["fid"]
            elif parent in entry["mappings"]:
                target = entry["mappings"][parent]["target"]["fid"]
            else:
                continue
            fresh = [fid for fid in source_ids if fid not in entry["mappings"]]
            if not fresh:
                continue
            before = [] if target is None else self.saver._list_children(target)
            names = {item["file_name"] for item in before}
            if any(nodes[fid]["identity"]["file_name"] in names for fid in fresh):
                raise UsageError("新来源与历史目标同名，禁止按名称跳过或绑定")
            guard_target = target or entry["binding"]["parent_fid"]
            scopes.check(guard_target, write=True)
            self._guard(job["id"], guard_target, ledger)
            groups.append((parent, target, fresh, before))
        new_items = sum(len(self._subtree(fresh, children)) for _, _, fresh, _ in groups)
        if dry_run:
            return {"result": "preview", "new_items": new_items, "destination_fid": entry["binding"]["fid"]}
        if new_items and entry["binding"]["fid"] is None:
            binding = entry["binding"]
            before = self.saver._list_children(binding["parent_fid"])
            entry["pending"] = self._new_intent("mkdir", None, binding["parent_fid"], binding["name"], selection.source_fid, [], [item["fid"] for item in before], {})
            self._persist(ledger, False)
            response = self.saver.files.mkdir(binding["name"], binding["parent_fid"])
            created_fid = _optional_created_fid(response)
            if created_fid is None:
                raise RemoteError("建目录响应没有可核验 FID，保留未知意图")
            entry["pending"]["created_fid"] = valid_fid(created_fid)
            self._persist(ledger, False)
            actual = self.saver._confirm_created_directory(binding["parent_fid"], binding["name"], created_fid)
            if actual != created_fid or actual in entry["pending"]["before_fids"]:
                raise UsageError("创建响应与新目录不一致，保留未知意图")
            scopes.check(actual, write=True)
            binding["fid"], entry["pending"] = actual, None
            self._persist(ledger, False)
        for parent, target, fresh, before in groups:
            target = target or entry["binding"]["fid"]
            scopes.check(target, write=True)
            self._guard(job["id"], target, ledger)
            current = self.saver._list_children(target)
            if any(item["file_name"] in {nodes[fid]["identity"]["file_name"] for fid in fresh} for item in current):
                raise UsageError("提交前发现新的同名目标冲突")
            identities = {fid: nodes[fid]["identity"] for fid in self._subtree(fresh, children)}
            pending = self._new_intent("save", target, target, "", parent, fresh, [item["fid"] for item in current], identities,
                                       {fid: nodes[fid]["parent"] for fid in identities})
            entry["pending"] = pending
            self._persist(ledger, False)
            subset = ShareSelection(selection.pwd_id, parent, selection.source_path, [nodes[fid]["item"] for fid in fresh], selection.stoken)
            pending["task_id"] = self.saver._submit_selection(subset, target, created=False)
            self._persist(ledger, False)
            staged = self._finish_save(entry, pending, selection, nodes, children, scopes, lambda: self._persist(ledger, False))
            entry["mappings"].update(staged)
            entry["pending"] = None
            self._persist(ledger, False)
        return {"result": "updated" if len(entry["mappings"]) > initial_count else "unchanged", "new_items": new_items,
                "saved_items": len(entry["mappings"]) - initial_count, "destination_fid": entry["binding"]["fid"]}

    def run(self, name: str | None = None, *, scope: Scope | None = None, dry_run: bool = False) -> tuple[dict[str, Any], int]:
        if name is not None:
            validate_name(name)
        with StateLock(self.directory):
            config, ledger = self._load()
            if name is not None and name not in config["jobs"]:
                raise UsageError("任务不存在")
            if dry_run:
                ledger = copy.deepcopy(ledger)
            results, exit_code, state_failed = [], 0, False
            for job_name, job in config["jobs"].items():
                if name is not None and job_name != name:
                    continue
                result = self._public(job_name, job)
                if state_failed:
                    result.update({"result": "failed", "error_type": "local_io_error", "exit_code": 5, "reason": "previous_state_failure"})
                    results.append(result)
                    continue
                try:
                    result.update(self._run_job(job, ledger, scope, dry_run))
                except QuarkError as error:
                    result.update({"result": "failed", "error_type": error.label, "exit_code": error.exit_code})
                    pending = ledger["jobs"][job["id"]]["pending"]
                    if pending is not None:
                        result["pending"] = True
                        if pending["task_id"] is not None:
                            result["task_id"] = pending["task_id"]
                    exit_code = max(exit_code, error.exit_code)
                    state_failed = isinstance(error, LocalIOError)
                results.append(result)
            return {"dry_run": dry_run, "jobs": results, "succeeded": sum(item["result"] != "failed" for item in results),
                    "failed": sum(item["result"] == "failed" for item in results)}, exit_code
