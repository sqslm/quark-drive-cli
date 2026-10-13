"""Serial directory transfers with portable names and durable FID bindings."""

from __future__ import annotations

import hashlib
import json
import math
import stat
import tempfile
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Any

from .errors import LocalIOError, QuarkError, RemoteError, UsageError
from .files import Files
from .paths import RemotePaths, Scope, is_directory, valid_fid
from .shares import ShareSaver, _optional_created_fid
from .state import StateLock, config_directory, read_json, secure_directory, write_json
from .transfer import DEFAULT_DOWNLOAD_CONNECTIONS, DownloadStopped, Transfer, safe_transfer_name


def local_name(name: str) -> str:
    """Use portable filesystem names on both operating systems."""
    return safe_transfer_name(name)


def _names(names) -> None:
    seen = set()
    for name in names:
        local_name(name)
        key = unicodedata.normalize("NFC", name).casefold()
        if key in seen:
            raise UsageError("同级名称存在大小写或规范化冲突")
        seen.add(key)


def _safe_path(path: Path) -> None:
    for part in (path, *path.parents):
        if part.name:
            local_name(part.name)
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise LocalIOError("无法检查本地目录传输路径") from None
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise UsageError("目录传输拒绝符号链接、junction 和重解析点")


def _hashes(path: Path, should_stop=None) -> dict[str, str]:
    digests = {name: hashlib.new(name) for name in ("sha256", "md5", "sha1")}
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                if should_stop is not None and should_stop():
                    from .transfer import UploadStopped
                    raise UploadStopped("已暂停目录上传，可继续")
                for digest in digests.values():
                    digest.update(block)
    except OSError:
        raise LocalIOError("无法读取目录传输文件") from None
    return {name: digest.hexdigest() for name, digest in digests.items()}


def _local_tree(source: Path, should_stop=None) -> tuple[Path, list[dict[str, Any]]]:
    source = source.absolute()
    _safe_path(source)
    if not source.is_dir():
        raise UsageError("递归上传来源必须是目录")
    local_name(source.name)
    nodes, stack = [], [(source, "", 0)]
    while stack:
        if should_stop is not None and should_stop():
            from .transfer import UploadStopped
            raise UploadStopped("已暂停目录上传，可继续")
        path, relative, depth = stack.pop()
        if depth > 1024 or len(nodes) >= 100000:
            raise UsageError("本地目录树超过安全上限")
        _safe_path(path)
        try:
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                nodes.append({"path": relative, "kind": "directory"})
                children = sorted(path.iterdir(), key=lambda child: child.name)
                _names(child.name for child in children)
                stack.extend((child, relative + "/" + child.name if relative else child.name, depth + 1) for child in reversed(children))
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_size == 0:
                    raise UsageError("夸克网页版不支持零字节上传；空目录可以递归传输")
                identity = {"size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns, "device": metadata.st_dev, "inode": metadata.st_ino, **_hashes(path, should_stop)}
                if any(identity[key] < 0 for key in ("size", "mtime_ns", "device", "inode")):
                    raise UsageError("本地文件身份字段不支持安全续传")
                after = path.lstat()
                if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    raise UsageError("本地文件在预检期间发生变化")
                nodes.append({"path": relative, "kind": "file", "identity": identity})
            else:
                raise UsageError("目录传输只接受普通文件和目录")
        except OSError:
            raise LocalIOError("无法扫描本地上传目录") from None
    return source, nodes


def _remote_identity(item, *, root=False):
    fid = valid_fid(item.get("fid"))
    directory = is_directory(item)
    output = {"fid": fid, "name": "" if root and fid == "0" else local_name(item.get("file_name")), "kind": "directory" if directory else "file"}
    if fid != "0":
        output["parent"] = valid_fid(item.get("pdir_fid"))
    if not directory:
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RemoteError("远端文件缺少有效大小，不能确认目录传输")
        output["size"] = size
        for key in ("updated_at", "md5", "sha1"):
            value = item.get(key)
            if value is None or (key in ("md5", "sha1") and value == ""):
                continue
            if key == "updated_at":
                try:
                    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
                        raise ValueError()
                except (ValueError, TypeError, OverflowError):
                    raise RemoteError("远端文件版本字段无效") from None
            elif not isinstance(value, str) or len(value) != (32 if key == "md5" else 40) or any(character not in "0123456789abcdefABCDEF" for character in value):
                raise RemoteError("远端文件哈希字段无效")
            output[key] = value
    return output


class _Boundaries:
    def __init__(self, paths, saved: str | None, current: Scope | None):
        self.paths = paths
        self.scopes = ([Scope(paths, saved)] if saved is not None else []) + ([current] if current else [])
        self.fid = saved if saved is not None else current.fid if current else None

    def check(self, fid: str, **options):
        for scope in self.scopes:
            scope.check(fid, **options)


class RecursiveTransfer:
    def __init__(self, client, *, transfer=None, state_dir: Path | None = None):
        self.client = client
        self.transfer = transfer or Transfer(client, state_dir=state_dir / "transfers" if state_dir is not None else None)
        self.files = Files(client)
        self.paths = RemotePaths(self.files)
        self.saver = ShareSaver(client)
        self.state_dir = state_dir

    def _state(self, direction, source, target, root_name):
        directory = (self.state_dir if self.state_dir is not None else config_directory()) / "transfers" / "trees"
        # Lock the destination, including competing sources, before any mkdir.
        destination = target if direction == "upload" else unicodedata.normalize("NFC", target).casefold()
        key = hashlib.sha256(json.dumps([direction, destination, unicodedata.normalize("NFC", root_name).casefold()], ensure_ascii=False).encode()).hexdigest()
        return directory, directory / (key + ".json"), key + ".lock"

    @staticmethod
    def _load(path):
        missing = {}
        document = read_json(path, default=missing)
        if document is missing:
            return None
        try:
            if set(document) != {"version", "direction", "source", "target", "root_name", "scope_fid", "nodes", "directories", "files", "pending", "completed"} or type(document["version"]) is not int or document["version"] != 1 or document["direction"] not in ("upload", "download") or type(document["completed"]) is not bool or not isinstance(document["nodes"], list) or not isinstance(document["directories"], dict) or not isinstance(document["files"], dict):
                raise ValueError()
            if document["scope_fid"] is not None:
                valid_fid(document["scope_fid"])
            paths = set()
            for node in document["nodes"]:
                relative = node["path"]
                if not isinstance(relative, str) or relative in paths or node["kind"] not in ("file", "directory"):
                    raise ValueError()
                if relative:
                    for name in relative.split("/"):
                        local_name(name)
                paths.add(relative)
            if "" not in paths or set(document["directories"]) - paths or set(document["files"]) - paths:
                raise ValueError()
            kinds = {node["path"]: node["kind"] for node in document["nodes"]}
            by_path = {node["path"]: node for node in document["nodes"]}
            if kinds[""] != "directory" or any(kinds[relative] != "directory" for relative in document["directories"]) or any(kinds[relative] != "file" for relative in document["files"]):
                raise ValueError()
            if document["direction"] == "upload":
                if not isinstance(document["source"], str) or not Path(document["source"]).is_absolute():
                    raise ValueError()
                valid_fid(document["target"])
                local_name(document["root_name"])
            else:
                valid_fid(document["source"])
                if not isinstance(document["target"], str) or not Path(document["target"]).is_absolute() or document["root_name"] != "" or document["directories"]:
                    raise ValueError()
            remote_ids = set()
            for relative, node in by_path.items():
                parent_relative = relative.rsplit("/", 1)[0] if "/" in relative else ""
                if relative and kinds.get(parent_relative) != "directory":
                    raise ValueError()
                if document["direction"] == "upload":
                    if set(node) != ({"path", "kind"} if node["kind"] == "directory" else {"path", "kind", "identity"}):
                        raise ValueError()
                    if node["kind"] == "file":
                        identity = node["identity"]
                        if not isinstance(identity, dict) or set(identity) != {"size", "mtime_ns", "device", "inode", "sha256", "md5", "sha1"} or any(type(identity[key]) is not int or identity[key] < 0 for key in ("size", "mtime_ns", "device", "inode")):
                            raise ValueError()
                        for key, length in (("sha256", 64), ("md5", 32), ("sha1", 40)):
                            if not isinstance(identity[key], str) or len(identity[key]) != length or any(character not in "0123456789abcdef" for character in identity[key]):
                                raise ValueError()
                else:
                    if set(node) != {"path", "kind", "identity"}:
                        raise ValueError()
                    identity = node["identity"]
                    record = {"fid": identity["fid"], "file_name": identity["name"], "pdir_fid": identity.get("parent", "0"), "file_type": 0 if node["kind"] == "directory" else 1, **{key: value for key, value in identity.items() if key in {"size", "updated_at", "md5", "sha1"}}}
                    if identity != _remote_identity(record, root=relative == "") or identity["fid"] in remote_ids or (relative and identity["parent"] != by_path[parent_relative]["identity"]["fid"]) or (not relative and identity["fid"] != document["source"]):
                        raise ValueError()
                    remote_ids.add(identity["fid"])
            for relative, binding in document["directories"].items():
                if set(binding) != {"fid", "parent", "name"}:
                    raise ValueError()
                valid_fid(binding["fid"])
                valid_fid(binding["parent"])
                local_name(binding["name"])
                parent_relative = relative.rsplit("/", 1)[0] if "/" in relative else ""
                expected_parent = document["target"] if not relative else document["directories"][parent_relative]["fid"]
                expected_name = document["root_name"] if not relative else relative.rsplit("/", 1)[-1]
                if binding["parent"] != expected_parent or binding["name"] != expected_name:
                    raise ValueError()
            target_ids = [binding["fid"] for binding in document["directories"].values()]
            for relative, receipt in document["files"].items():
                if set(receipt) != {"identity", "sha256", "callback_warning"} or type(receipt["callback_warning"]) is not bool or not isinstance(receipt["sha256"], str) or len(receipt["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in receipt["sha256"]):
                    raise ValueError()
                identity = receipt["identity"]
                if identity != _remote_identity({"fid": identity["fid"], "file_name": identity["name"], "pdir_fid": identity["parent"], "file_type": 1, **{k: v for k, v in identity.items() if k in {"size", "updated_at", "md5", "sha1"}}}):
                    raise ValueError()
                if document["direction"] == "upload":
                    parent_relative = relative.rsplit("/", 1)[0] if "/" in relative else ""
                    if identity["parent"] != document["directories"][parent_relative]["fid"] or identity["name"] != relative.rsplit("/", 1)[-1] or identity["size"] != by_path[relative]["identity"]["size"] or receipt["sha256"] != by_path[relative]["identity"]["sha256"]:
                        raise ValueError()
                elif identity != by_path[relative]["identity"]:
                    raise ValueError()
                target_ids.append(identity["fid"])
            if len(set(target_ids)) != len(target_ids):
                raise ValueError()
            pending = document["pending"]
            if document["completed"] and pending is not None:
                raise ValueError()
            if pending is not None:
                if set(pending) != {"kind", "path", "parent", "name", "fid", "before"} or pending["kind"] not in ("mkdir", "upload", "download") or pending["path"] not in paths:
                    raise ValueError()
                valid_fid(pending["parent"])
                local_name(pending["name"])
                if pending["fid"] is not None:
                    valid_fid(pending["fid"])
                if not isinstance(pending["before"], list):
                    raise ValueError()
                if len(set(pending["before"])) != len(pending["before"]) or (pending["kind"] == "mkdir") != (kinds[pending["path"]] == "directory"):
                    raise ValueError()
                for fid in pending["before"]:
                    valid_fid(fid)
                if document["direction"] == "upload":
                    if pending["kind"] not in ("mkdir", "upload"):
                        raise ValueError()
                    relative = pending["path"]
                    parent_relative = relative.rsplit("/", 1)[0] if "/" in relative else ""
                    parent = document["target"] if not relative else document["directories"][parent_relative]["fid"]
                    name = document["root_name"] if not relative else relative.rsplit("/", 1)[-1]
                    if pending["parent"] != parent or pending["name"] != name or (pending["kind"] == "mkdir" and relative in document["directories"]) or (pending["kind"] == "upload" and (pending["fid"] is not None or pending["before"] or relative in document["files"])):
                        raise ValueError()
                elif pending["kind"] != "download" or pending["before"] or any(pending[key] != by_path[pending["path"]]["identity"][key] for key in ("fid", "parent", "name")) or pending["path"] in document["files"]:
                    raise ValueError()
            if document["completed"] and (set(document["files"]) != {relative for relative, kind in kinds.items() if kind == "file"} or (document["direction"] == "upload" and set(document["directories"]) != {relative for relative, kind in kinds.items() if kind == "directory"})):
                raise ValueError()
        except (KeyError, TypeError, ValueError, QuarkError):
            raise LocalIOError("目录传输账本损坏，已停止且未重置") from None
        return document

    @staticmethod
    def _new(direction, source, target, root_name, scope, nodes):
        return {"version": 1, "direction": direction, "source": source, "target": target, "root_name": root_name,
                "scope_fid": scope.fid if scope else None, "nodes": nodes, "directories": {}, "files": {}, "pending": None, "completed": False}

    @staticmethod
    def _compatible(document, direction, source, target, root_name, nodes):
        if [document[key] for key in ("direction", "source", "target", "root_name", "nodes")] != [direction, source, target, root_name, nodes]:
            raise UsageError("目录续传的固定树、来源或目标身份已变化")

    def _directory(self, binding, boundaries):
        chain = self.paths.ancestry(binding["fid"])
        actual = chain[0].item or {}
        if not is_directory(actual) or actual.get("file_name") != binding["name"] or valid_fid(actual.get("pdir_fid")) != binding["parent"]:
            raise UsageError("目录精确 FID、名称或父关系已变化，禁止同名重新绑定")
        boundaries.check(binding["fid"], write=True)

    def _file(self, identity, boundaries):
        boundaries.check(identity["fid"])
        actual = _remote_identity(self.paths.ancestry(identity["fid"])[0].item or {})
        if actual != identity:
            raise UsageError("完成文件的远端 FID、身份或父关系已变化")

    def _content(self, identity, expected, boundaries, limiter):
        self._file(identity, boundaries)
        matched_hash = False
        for algorithm in ("md5", "sha1"):
            value = identity.get(algorithm)
            if isinstance(value, str):
                if value.lower() != expected.get(algorithm):
                    raise UsageError("完成文件的可靠远端哈希与源内容不符")
                matched_hash = True
        if matched_hash:
            return
        # FID, timestamps and lengths alone cannot prove unchanged contents.
        with tempfile.TemporaryDirectory(prefix="quark-tree-verify-") as temporary:
            output = Path(temporary) / "payload"
            self.transfer.download(identity["fid"], output, limiter=limiter, scope=boundaries if boundaries.scopes else None)
            if _hashes(output)["sha256"] != expected["sha256"]:
                raise UsageError("完成文件内容哈希变化，目录续传冲突")
        self._file(identity, boundaries)

    @staticmethod
    def _progress(callback, relative, done, total):
        if callback is None:
            return None
        def emit(event):
            callback(replace(event, name=relative, transferred=done + event.transferred, confirmed=done + event.confirmed, total=total))
        return emit

    def _upload_preflight(self, nodes, parent, root_name, document, boundaries, *, allow_existing=True):
        self.paths.ancestry(parent)
        boundaries.check(parent, write=True)
        planned = {}
        for node in nodes:
            relative = node["path"]
            parent_relative = relative.rsplit("/", 1)[0] if "/" in relative else ""
            container = parent if not relative else planned[parent_relative]
            name = root_name if not relative else relative.rsplit("/", 1)[-1]
            listed = self.paths.children(container) if container is not None else []
            _names([name] + [record["file_name"] for record in listed if record["file_name"] != name])
            matches = [record for record in listed if record["file_name"] == name]
            if node["kind"] == "directory":
                existing = document["directories"].get(relative)
                if existing:
                    self._directory(existing, boundaries)
                    if existing["parent"] != container or len(matches) != 1 or matches[0]["fid"] != existing["fid"]:
                        raise UsageError("目录目标路径与原 FID 绑定不一致")
                    planned[relative] = existing["fid"]
                elif matches:
                    if not allow_existing:
                        raise UsageError("续传出现未绑定的同名目录，禁止收编")
                    if len(matches) != 1 or not is_directory(matches[0]):
                        raise UsageError("目录目标同名冲突")
                    binding = {"fid": valid_fid(matches[0]["fid"]), "parent": container, "name": name}
                    self._directory(binding, boundaries)
                    planned[relative] = binding["fid"]
                    document["directories"][relative] = binding
                else:
                    planned[relative] = None
            elif matches:
                receipt = document["files"].get(relative)
                pending = document["pending"]
                if not receipt and not (pending and pending["kind"] == "upload" and pending["path"] == relative):
                    raise UsageError("上传目标文件已存在，默认不覆盖")
                if receipt and (len(matches) != 1 or matches[0]["fid"] != receipt["identity"]["fid"]):
                    raise UsageError("完成目标被同名新 FID 替换")
        return planned

    def upload_tree(self, source, parent="0", *, resume=False, dry_run=False, limit_rate=None, progress=None, scope=None,
                    should_stop=None):
        from .transfer_progress import RateLimiter
        from .transfer import UploadStopped
        if should_stop is not None and not resume:
            raise UsageError("可暂停目录上传必须启用续传")
        def check_stop():
            if should_stop is not None and should_stop():
                raise UploadStopped("已暂停目录上传，已确认状态保留，可继续")
        check_stop()
        limiter = RateLimiter(limit_rate)
        source, nodes = _local_tree(Path(source), should_stop)
        parent = self.paths.resolve(parent, directory=True).fid
        directory, state_path, lock_name = self._state("upload", str(source), parent, source.name)
        total = sum(node.get("identity", {}).get("size", 0) for node in nodes)
        preview = {"recursive": True, "dry_run": True, "files": sum(node["kind"] == "file" for node in nodes), "directories": sum(node["kind"] == "directory" for node in nodes), "size": total, "parent": parent}
        if dry_run:
            document = self._new("upload", str(source), parent, source.name, scope, nodes)
            old = self._load(state_path)
            if old and not resume and not old["completed"]:
                raise UsageError("已有未完成目录传输；使用 --resume 核验原状态")
            if old and resume:
                self._compatible(old, "upload", str(source), parent, source.name, nodes)
                document = old
            boundaries = _Boundaries(self.paths, document["scope_fid"], scope)
            pending = document["pending"]
            if pending and pending["kind"] == "mkdir":
                if pending["fid"] is None:
                    raise UsageError("目录创建受理未知且无 FID，禁止自动重发")
                boundaries.check(pending["parent"], write=True)
                actual = self.saver._confirm_created_directory(pending["parent"], pending["name"], pending["fid"])
                if actual != pending["fid"] or actual in pending["before"]:
                    raise UsageError("未知目录无法与原创建结果对应")
                binding = {"fid": actual, "parent": pending["parent"], "name": pending["name"]}
                self._directory(binding, boundaries)
                document["directories"][pending["path"]], document["pending"] = binding, None
            self._upload_preflight(nodes, parent, source.name, document, boundaries, allow_existing=not (old and resume))
            for node in nodes:
                if node["path"] in document["files"]:
                    self._file(document["files"][node["path"]]["identity"], boundaries)
            return preview
        secure_directory(directory.parent.parent)
        secure_directory(directory.parent)
        with StateLock(directory, lock_name):
            document = self._load(state_path)
            resuming = document is not None and resume
            if document and not resume and not document["completed"]:
                raise UsageError("已有未完成目录传输；使用 --resume 核验原状态")
            if document and resume:
                self._compatible(document, "upload", str(source), parent, source.name, nodes)
            else:
                document = self._new("upload", str(source), parent, source.name, scope, nodes)
            boundaries = _Boundaries(self.paths, document["scope_fid"], scope)
            pending = document["pending"]
            if pending and pending["kind"] == "mkdir":
                if pending["fid"] is None:
                    raise UsageError("目录创建受理未知且无 FID，禁止自动重发")
                boundaries.check(pending["parent"], write=True)
                actual = self.saver._confirm_created_directory(pending["parent"], pending["name"], pending["fid"])
                if actual != pending["fid"] or actual in pending["before"]:
                    raise UsageError("未知目录无法与原创建结果对应")
                binding = {"fid": actual, "parent": pending["parent"], "name": pending["name"]}
                self._directory(binding, boundaries)
                document["directories"][pending["path"]], document["pending"] = binding, None
                write_json(state_path, document)
            self._upload_preflight(nodes, parent, source.name, document, boundaries, allow_existing=not resuming)
            for node in nodes:
                if node["path"] in document["files"]:
                    receipt = document["files"][node["path"]]
                    self._content(receipt["identity"], node["identity"], boundaries, limiter)
            write_json(state_path, document)
            done, saved = sum(node["identity"]["size"] for node in nodes if node["path"] in document["files"]), 0
            for node in nodes:
                relative = node["path"]
                check_stop()
                parent_relative = relative.rsplit("/", 1)[0] if "/" in relative else ""
                container = parent if not relative else document["directories"][parent_relative]["fid"]
                name = source.name if not relative else relative.rsplit("/", 1)[-1]
                boundaries.check(container, write=True)
                self.paths.ancestry(container)
                if node["kind"] == "directory":
                    if relative in document["directories"]:
                        self._directory(document["directories"][relative], boundaries)
                        continue
                    before = self.paths.children(container)
                    if any(record["file_name"] == name for record in before):
                        raise UsageError("目录执行期间出现同名目标，未收编")
                    document["pending"] = {"kind": "mkdir", "path": relative, "parent": container, "name": name, "fid": None, "before": [record["fid"] for record in before]}
                    write_json(state_path, document)
                    response = self.files.mkdir(name, container)
                    fid = _optional_created_fid(response)
                    if fid is None:
                        raise RemoteError("目录创建响应缺少 FID，保留未知意图")
                    document["pending"]["fid"] = valid_fid(fid)
                    write_json(state_path, document)
                    actual = self.saver._confirm_created_directory(container, name, fid)
                    if actual != fid or actual in document["pending"]["before"]:
                        raise UsageError("目录响应不能唯一对应新目标")
                    binding = {"fid": actual, "parent": container, "name": name}
                    self._directory(binding, boundaries)
                    document["directories"][relative], document["pending"] = binding, None
                    write_json(state_path, document)
                elif relative not in document["files"]:
                    if _local_tree(source, should_stop)[1] != nodes:
                        raise UsageError("上传固定树在执行期间发生变化")
                    document["pending"] = {"kind": "upload", "path": relative, "parent": container, "name": name, "fid": None, "before": []}
                    write_json(state_path, document)
                    result = self.transfer.upload(source / relative, container, resume=resume, remote_name=name, limiter=limiter,
                                                  progress=self._progress(progress, relative, done, total), scope=boundaries if boundaries.scopes else None,
                                                  **({"should_stop": should_stop} if should_stop is not None else {}))
                    fid = valid_fid(result.get("fid"))
                    identity = _remote_identity(self.paths.ancestry(fid)[0].item or {})
                    if identity["parent"] != container or identity["name"] != name or identity["size"] != node["identity"]["size"]:
                        raise RemoteError("目录上传结果与精确目标身份不符")
                    if any(key in identity and identity[key].lower() != node["identity"][key] for key in ("md5", "sha1")):
                        raise RemoteError("目录上传结果的可靠远端哈希不符")
                    boundaries.check(fid)
                    document["files"][relative] = {"identity": identity, "sha256": node["identity"]["sha256"], "callback_warning": bool(result.get("callback_warning"))}
                    document["pending"] = None
                    write_json(state_path, document)
                    done += node["identity"]["size"]
                    saved += 1
            document["completed"] = True
            write_json(state_path, document)
            return {"uploaded": True, "recursive": True, "resumed": bool(resume), "destination_fid": document["directories"][""]["fid"], "files": preview["files"], "directories": preview["directories"], "size": total, "uploaded_files": saved, "skipped_files": preview["files"] - saved, "callback_warning": any(receipt["callback_warning"] for receipt in document["files"].values())}

    def _download_tree(self, fid, boundaries):
        root = self.paths.resolve(fid, directory=True)
        root_path = self.paths.absolute_path(root.fid)
        nodes = []
        for record in self.paths.walk(root.fid, depth=None):
            boundaries.check(record["fid"])
            absolute = record["path"]
            relative = "" if record["depth"] == 0 else absolute[len(root_path.rstrip("/")) + 1:]
            if relative:
                for component in relative.split("/"):
                    local_name(component)
            actual = self.paths.ancestry(record["fid"])[0].item or self.paths.root().item
            identity = _remote_identity(actual, root=record["depth"] == 0)
            if _remote_identity(record, root=record["depth"] == 0) != identity:
                raise UsageError("远端目录列表与精确详情身份不一致")
            nodes.append({"path": relative, "kind": identity["kind"], "identity": identity})
        siblings = {}
        for node in nodes[1:]:
            parent = node["path"].rsplit("/", 1)[0] if "/" in node["path"] else ""
            siblings.setdefault(parent, []).append(node["path"].rsplit("/", 1)[-1])
        for names in siblings.values():
            _names(names)
        return root.fid, nodes

    @staticmethod
    def _download_preflight(output, nodes, document, overwrite):
        for node in nodes:
            path = output / node["path"]
            _safe_path(path)
            if not path.exists():
                continue
            if node["kind"] == "directory":
                if not path.is_dir():
                    raise UsageError("下载目录与本地同名文件冲突")
                expected = [child["path"].rsplit("/", 1)[-1] for child in nodes if child["path"] and (child["path"].rsplit("/", 1)[0] if "/" in child["path"] else "") == node["path"]]
                _names(expected + [child.name for child in path.iterdir() if child.name not in expected])
            elif not path.is_file():
                raise UsageError("下载目标不是普通文件")
            elif node["path"] not in document["files"] and not overwrite:
                pending = document["pending"]
                if not pending or pending["kind"] != "download" or pending["path"] != node["path"]:
                    raise UsageError("下载目标已存在，默认不覆盖")

    def download_tree(self, fid, output, *, resume=False, overwrite=False, limit_rate=None, progress=None, scope=None,
                      connections=DEFAULT_DOWNLOAD_CONNECTIONS, should_stop=None):
        """Download one remote tree serially, at a durable point per file.

        ``should_stop`` is asked before the tree is walked, again before every
        node, and by each file's own transfer: a true answer raises
        :class:`DownloadStopped` with the tree ledger still holding every
        completed file, so a later ``resume=True`` continues instead of starting
        over. It needs ``resume=True``, the same rule a single file download
        has: the durable checkpoints are what make stopping safe.
        """
        from .transfer_progress import RateLimiter
        if should_stop is not None and not resume:
            raise UsageError("停止下载需要 resume=True 的持久化检查点")
        if should_stop is not None and should_stop():
            raise DownloadStopped("下载已停止：尚未传输任何数据")
        limiter = RateLimiter(limit_rate)
        output = Path(output).absolute()
        _safe_path(output)
        directory, state_path, lock_name = self._state("download", "", str(output), "")
        secure_directory(directory.parent.parent)
        secure_directory(directory.parent)
        with StateLock(directory, lock_name):
            document = self._load(state_path)
            if document and not resume and not document["completed"]:
                raise UsageError("已有未完成目录下载；使用 --resume 核验原状态")
            saved_scope = document["scope_fid"] if document and resume else None
            boundaries = _Boundaries(self.paths, saved_scope, scope)
            fid = self.paths.resolve(fid, directory=True).fid
            boundaries.check(fid)
            if document and resume and [document[key] for key in ("direction", "source", "target", "root_name")] != ["download", fid, str(output), ""]:
                raise UsageError("目录续传的来源或目标绑定已变化")
            fid, nodes = self._download_tree(fid, boundaries)
            if document and resume:
                self._compatible(document, "download", fid, str(output), "", nodes)
            else:
                document = self._new("download", fid, str(output), "", scope, nodes)
            boundaries = _Boundaries(self.paths, document["scope_fid"], scope)
            for node in nodes:
                boundaries.check(node["identity"]["fid"])
            try:
                self._download_preflight(output, nodes, document, overwrite)
            except OSError:
                raise LocalIOError("无法预检本地目录下载目标") from None
            for relative, receipt in document["files"].items():
                local = output / relative
                if not local.is_file() or _hashes(local)["sha256"] != receipt["sha256"]:
                    raise UsageError("完成下载的本地文件丢失或内容变化")
                self._content(receipt["identity"], _hashes(local), boundaries, limiter)
            write_json(state_path, document)
            total = sum(node["identity"].get("size", 0) for node in nodes)
            done = sum(receipt["identity"]["size"] for receipt in document["files"].values())
            downloaded = 0
            try:
                for node in nodes:
                    relative, identity = node["path"], node["identity"]
                    if should_stop is not None and should_stop():
                        # Durable point: every file finished so far is recorded in
                        # the tree ledger, and the one in flight keeps its own
                        # download checkpoint when it is asked to stop.
                        raise DownloadStopped("目录下载已停止：已完成的项目已保留，再次提交可继续")
                    path = output / relative
                    _safe_path(path)
                    boundaries.check(identity["fid"])
                    if node["kind"] == "directory":
                        path.mkdir(parents=True, exist_ok=True)
                    elif relative not in document["files"]:
                        self._file(identity, boundaries)
                        document["pending"] = {"kind": "download", "path": relative, "parent": identity["parent"], "name": identity["name"], "fid": identity["fid"], "before": []}
                        write_json(state_path, document)
                        self.transfer.download(identity["fid"], path, resume=resume, overwrite=overwrite, limiter=limiter,
                                               progress=self._progress(progress, relative, done, total), scope=boundaries if boundaries.scopes else None,
                                               connections=connections, should_stop=should_stop)
                        self._file(identity, boundaries)
                        hashes = _hashes(path)
                        if path.stat().st_size != identity["size"]:
                            raise RemoteError("目录下载结果大小不符")
                        if any(key in identity and identity[key].lower() != hashes[key] for key in ("md5", "sha1")):
                            raise RemoteError("目录下载内容与可靠远端哈希不符")
                        document["files"][relative], document["pending"] = {"identity": identity, "sha256": hashes["sha256"], "callback_warning": False}, None
                        write_json(state_path, document)
                        done += identity["size"]
                        downloaded += 1
            except OSError:
                raise LocalIOError("无法创建目录下载的本地对象") from None
            document["completed"] = True
            write_json(state_path, document)
            return {"downloaded": True, "recursive": True, "resumed": bool(resume), "fid": fid, "path": str(output), "files": sum(node["kind"] == "file" for node in nodes), "directories": sum(node["kind"] == "directory" for node in nodes), "size": total, "downloaded_files": downloaded, "skipped_files": len(document["files"]) - downloaded}
