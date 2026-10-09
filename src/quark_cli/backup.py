"""One-way backups with exact FID receipts and durable, non-destructive updates."""

from __future__ import annotations

import copy
import hashlib
import math
import stat
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Mapping

from .errors import LocalIOError, QuarkError, RemoteError, UsageError
from .files import Files
from .paths import RemotePaths, valid_fid
from .recursive import _Boundaries, _hashes, _local_tree, _names, _remote_identity, _safe_path, RecursiveTransfer
from .shares import _optional_created_fid
from .state import StateLock, config_directory, read_json, secure_directory, write_json
from .transfer import Transfer, safe_transfer_name
from .transfer_progress import RateLimiter
from .transfer_state import UploadLedger


_LOCAL = {"size", "mtime_ns", "device", "inode", "sha256", "md5", "sha1"}
_PENDING = {"kind", "path", "parent", "name", "phase", "local", "old", "new", "fid", "temporary_name", "archive_name", "archive"}
_PHASES = {
    "mkdir": {"mkdir_submitting", "mkdir_confirmed"},
    "upload": {"upload_prepared", "upload_submitting", "upload_confirmed"},
    "update": {"upload_prepared", "upload_submitting", "upload_confirmed", "archive_submitting", "archive_confirmed", "promote_submitting", "promote_confirmed"},
}


def _text(value):
    return isinstance(value, str) and not any(unicodedata.category(c) in ("Cc", "Cf") for c in value)


def _relative(value, *, root=False):
    if not isinstance(value, str) or (not value and not root):
        raise ValueError()
    if value:
        for part in value.split("/"):
            safe_transfer_name(part)
    return value


def _parent(relative):
    return relative.rsplit("/", 1)[0] if "/" in relative else ""


def _name(relative):
    return relative.rsplit("/", 1)[-1]


def _name_key(name):
    return unicodedata.normalize("NFC", name).casefold()


def _local(value):
    if not isinstance(value, dict) or set(value) != _LOCAL:
        raise ValueError()
    if any(type(value[k]) is not int or value[k] < 0 for k in ("size", "mtime_ns", "device", "inode")) or value["size"] == 0:
        raise ValueError()
    for key, length in (("sha256", 64), ("md5", 32), ("sha1", 40)):
        if not isinstance(value[key], str) or len(value[key]) != length or any(c not in "0123456789abcdef" for c in value[key]):
            raise ValueError()


def _identity(value, *, root=False):
    if not isinstance(value, dict) or not {"fid", "name", "kind"} <= set(value) or set(value) - {"fid", "name", "kind", "parent", "size", "updated_at", "md5", "sha1"}:
        raise ValueError()
    record = {"fid": value["fid"], "file_name": value["name"], "pdir_fid": value.get("parent", "0"),
              "file_type": 0 if value["kind"] == "directory" else 1,
              **{k: value[k] for k in ("size", "updated_at", "md5", "sha1") if k in value}}
    if _remote_identity(record, root=root) != value:
        raise ValueError()


def _entry(value):
    if not isinstance(value, dict) or set(value) != {"kind", "local", "remote"} or value["kind"] not in ("file", "directory"):
        raise ValueError()
    _identity(value["remote"])
    if value["remote"]["kind"] != value["kind"]:
        raise ValueError()
    if value["kind"] == "file":
        _local(value["local"])
        if value["local"]["size"] != value["remote"]["size"]:
            raise ValueError()
    elif value["local"] is not None:
        raise ValueError()


def safe_operation_result(value: Any) -> dict[str, Any]:
    """Whitelist successful results and attached failure evidence at the CLI boundary."""
    if not isinstance(value, Mapping):
        return {}
    result = {}
    if isinstance(value.get("result"), str) and value["result"] in {"preview", "completed", "partial", "unknown", "failed"}:
        result["result"] = value["result"]
    if type(value.get("dry_run")) is bool:
        result["dry_run"] = value["dry_run"]
    for key in ("source", "target_fid"):
        if _text(value.get(key)):
            result[key] = value[key]
    for key in ("files", "directories", "uploaded_files", "updated_files", "skipped_files", "created_directories"):
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    for key in ("items", "archives"):
        if isinstance(value.get(key), list):
            clean = []
            for item in value[key]:
                if not isinstance(item, Mapping):
                    continue
                row = {k: item[k] for k in ("path", "fid", "name") if _text(item.get(k))}
                if key == "items":
                    for field, allowed in (("kind", {"file", "directory"}), ("action", {"mkdir", "upload", "update", "unchanged", "retained"}),
                                           ("result", {"planned", "confirmed", "unchanged", "retained"})):
                        if isinstance(item.get(field), str) and item[field] in allowed:
                            row[field] = item[field]
                clean.append(row)
            result[key] = clean
    pending = value.get("pending")
    if pending is None and "pending" in value:
        result["pending"] = None
    elif isinstance(pending, Mapping):
        row = {}
        if isinstance(pending.get("kind"), str) and pending["kind"] in _PHASES:
            row["kind"] = pending["kind"]
        if isinstance(pending.get("phase"), str) and pending["phase"] in set().union(*_PHASES.values()):
            row["phase"] = pending["phase"]
        for key in ("path", "parent_fid", "name", "fid", "new_fid", "temporary_name", "archive_name", "unpersisted_fid"):
            if _text(pending.get(key)):
                row[key] = pending[key]
        result["pending"] = row
    return result


class Backup:
    def __init__(self, client, transfer=None, state_dir=None):
        self.client = client
        self.state_dir = Path(state_dir) if state_dir is not None else config_directory() / "backups"
        if not self.state_dir.is_absolute():
            raise LocalIOError("备份状态目录必须是绝对路径")
        self.transfer = transfer or Transfer(client, state_dir=self.state_dir / "uploads" if state_dir is not None else None)
        self.files, self.paths = Files(client), RemotePaths(Files(client))
        self.document = None
        self._durable_document = None
        self._unpersisted_fid = None
        self._read_only = False
        self.manifest_path = None

    def _state(self, fid):
        key = hashlib.sha256(valid_fid(fid).encode()).hexdigest()
        return self.state_dir / (key + ".json"), key + ".lock"

    @staticmethod
    def _validate(document):
        try:
            if not isinstance(document, dict) or set(document) != {"version", "source", "target", "scope_fid", "entries", "archives", "pending", "created_at", "updated_at"} or type(document["version"]) is not int or document["version"] != 1:
                raise ValueError()
            if not _text(document["source"]) or not Path(document["source"]).is_absolute():
                raise ValueError()
            _identity(document["target"], root=True)
            if document["target"]["kind"] != "directory":
                raise ValueError()
            if document["scope_fid"] is not None:
                valid_fid(document["scope_fid"])
            for key in ("created_at", "updated_at"):
                if isinstance(document[key], bool) or not isinstance(document[key], (int, float)) or not math.isfinite(document[key]) or document[key] < 0:
                    raise ValueError()
            entries = document["entries"]
            if not isinstance(entries, dict) or len(entries) > 100000 or not isinstance(document["archives"], list):
                raise ValueError()
            ids = {document["target"]["fid"]}
            names = {}
            for relative, entry in entries.items():
                _relative(relative)
                _entry(entry)
                parent = _parent(relative)
                if parent and (parent not in entries or entries[parent]["kind"] != "directory"):
                    raise ValueError()
                parent_fid = entries[parent]["remote"]["fid"] if parent else document["target"]["fid"]
                remote = entry["remote"]
                if remote["parent"] != parent_fid or remote["name"] != _name(relative) or remote["fid"] in ids:
                    raise ValueError()
                ids.add(remote["fid"])
                names.setdefault(parent, []).append(remote["name"])
            for group in names.values():
                _names(group)
            for archive in document["archives"]:
                if not isinstance(archive, dict) or set(archive) != {"path", "local", "remote"}:
                    raise ValueError()
                _relative(archive["path"])
                _local(archive["local"])
                _identity(archive["remote"])
                remote = archive["remote"]
                parent = _parent(archive["path"])
                container = entries[parent]["remote"]["fid"] if parent else document["target"]["fid"]
                if remote["kind"] != "file" or remote["size"] != archive["local"]["size"] or remote["parent"] != container or remote["fid"] in ids:
                    raise ValueError()
                ids.add(remote["fid"])
            pending = document["pending"]
            if pending is not None:
                if not isinstance(pending, dict) or set(pending) != _PENDING or pending["kind"] not in _PHASES or pending["phase"] not in _PHASES[pending["kind"]]:
                    raise ValueError()
                _relative(pending["path"])
                safe_transfer_name(pending["name"])
                parent = _parent(pending["path"])
                if parent and (parent not in entries or entries[parent]["kind"] != "directory"):
                    raise ValueError()
                container = entries[parent]["remote"]["fid"] if parent else document["target"]["fid"]
                if pending["parent"] != container or pending["name"] != _name(pending["path"]):
                    raise ValueError()
                if pending["fid"] is not None:
                    valid_fid(pending["fid"])
                    if pending["fid"] in ids:
                        raise ValueError()
                if pending["kind"] == "mkdir":
                    if pending["path"] in entries or any(pending[k] is not None for k in ("local", "old", "new", "temporary_name", "archive_name", "archive")) or (pending["phase"] == "mkdir_confirmed" and pending["fid"] is None):
                        raise ValueError()
                else:
                    _local(pending["local"])
                    if pending["kind"] == "upload":
                        if pending["path"] in entries or any(pending[k] is not None for k in ("old", "temporary_name", "archive_name", "archive")):
                            raise ValueError()
                    else:
                        _entry(pending["old"])
                        if pending["old"] != entries.get(pending["path"]) or pending["old"]["kind"] != "file":
                            raise ValueError()
                        _names([pending["name"], pending["temporary_name"], pending["archive_name"]])
                    if pending["new"] is not None:
                        _identity(pending["new"])
                        new = pending["new"]
                        expected = pending["name"] if pending["phase"] == "promote_confirmed" or pending["kind"] == "upload" else pending["temporary_name"]
                        if new["fid"] != pending["fid"] or new["kind"] != "file" or new["parent"] != container or new["name"] != expected or new["size"] != pending["local"]["size"]:
                            raise ValueError()
                    if pending["phase"] not in {"upload_prepared", "upload_submitting"} and pending["new"] is None:
                        raise ValueError()
                    if pending["archive"] is not None:
                        _identity(pending["archive"])
                        archived, old = pending["archive"], pending["old"]["remote"]
                        if any(archived.get(k) != old.get(k) for k in ("fid", "parent", "kind", "size")) or archived["name"] != pending["archive_name"]:
                            raise ValueError()
                    if pending["phase"] in {"archive_confirmed", "promote_submitting", "promote_confirmed"} and pending["archive"] is None:
                        raise ValueError()
        except (KeyError, ValueError, TypeError, QuarkError, OverflowError):
            raise LocalIOError("备份清单格式损坏或身份关系无效，已停止且未重置") from None

    def _load(self, path, *, read_only=False):
        missing = {}
        document = read_json(path, default=missing, read_only=read_only)
        if document is missing:
            return None
        self._validate(document)
        return document

    def _persist(self):
        """Durable checkpoint, deliberately wrappable by process-exit acceptance harnesses."""
        snapshot = copy.deepcopy(self.document)
        snapshot["updated_at"] = time.time()
        try:
            self._validate(snapshot)
            write_json(self.manifest_path, snapshot)
        except Exception:
            self._restore_checkpoint()
            raise
        self.document = snapshot
        self._durable_document = copy.deepcopy(snapshot)
        self._unpersisted_fid = None

    def _restore_checkpoint(self):
        # Evidence must describe the last fsynced intent/receipt even when the
        # attempted final merge has already changed entries and cleared pending.
        self.document = copy.deepcopy(self._durable_document)

    def _exact_now(self, expected):
        actual = _remote_identity(self.files.info(expected["fid"])) if expected["fid"] != "0" else _remote_identity(self.paths.root().item, root=True)
        if actual != expected:
            raise UsageError("备份绑定对象的精确 FID、父目录、名称、类型或版本发生变化")
        return actual

    def _exact(self, expected, boundaries):
        actual = self._exact_now(expected)
        boundaries.check(actual["fid"])
        self.paths.ancestry(actual["fid"])
        return actual

    def _directory(self, fid):
        if self.document["target"]["fid"] == fid:
            return self.document["target"]
        for entry in self.document["entries"].values():
            if entry["kind"] == "directory" and entry["remote"]["fid"] == fid:
                return entry["remote"]
        pending = self.document["pending"]
        if pending and pending["kind"] == "mkdir" and pending["fid"] == fid:
            return {"fid": fid, "parent": pending["parent"], "name": pending["name"], "kind": "directory"}
        raise UsageError("备份目标父目录缺少确切绑定身份")

    def _children(self, fid, boundaries):
        # Check the queued directory before Scope.check follows its current
        # parent chain, which may now lead outside the saved scope.
        self._exact(self._directory(fid), boundaries)
        boundaries.check(fid, write=True)
        records = self.paths.children(fid)
        _names(record.get("file_name") for record in records)
        return records

    def _content(self, identity, expected, boundaries, limiter, *, force=False):
        self._exact(identity, boundaries)
        for algorithm in ("md5", "sha1"):
            if algorithm in identity and identity[algorithm].lower() != expected[algorithm]:
                raise UsageError("远端可靠哈希与备份内容证据矛盾")
        if not force and "updated_at" in identity:
            return
        # Reuse the frozen read-only HTTP boundary; public download also creates
        # a ledger lock, which is forbidden during a backup preview.
        url, size = self.transfer._download_address(identity["fid"])
        if size is not None and size != identity["size"]:
            raise RemoteError("备份内容回读大小不一致")
        digests = {key: hashlib.new(key) for key in ("sha256", "md5", "sha1")}
        count = 0
        self._exact_now(identity)
        with self.transfer._download_stream(url, {"Accept-Encoding": "identity"}) as response:
            if response.status_code != 200 or response.headers.get("Content-Encoding", "identity").lower() != "identity" or "Content-Range" in response.headers:
                raise RemoteError("备份内容回读响应无效")
            if "Content-Length" in response.headers:
                try:
                    if int(response.headers["Content-Length"]) != identity["size"]:
                        raise ValueError()
                except ValueError:
                    raise RemoteError("备份内容回读 Content-Length 无效") from None
            for block in response.iter_bytes(chunk_size=limiter.chunk_size):
                count += len(block)
                if count > identity["size"]:
                    raise RemoteError("备份内容回读超过预期大小")
                limiter.consume(len(block))
                for digest in digests.values():
                    digest.update(block)
        if count != identity["size"] or any(digests[key].hexdigest() != expected[key] for key in digests):
            raise UsageError("备份完整内容回读哈希不符")
        self._exact(identity, boundaries)

    def _known(self, fid, parent, name, kind, boundaries, *, expected=None):
        """Only a missing known-FID index membership can receive bounded read waits."""
        for attempt in range(6):
            actual = _remote_identity(self.files.info(fid))
            if (actual["fid"], actual.get("parent"), actual["name"], actual["kind"]) != (fid, parent, name, kind):
                raise UsageError("已知备份对象的精确身份不符")
            if expected is not None and actual != expected:
                raise UsageError("已知备份对象版本发生变化")
            records = self._children(parent, boundaries)
            matches = [r for r in records if str(r.get("fid")) == fid]
            if matches:
                if len(matches) != 1 or _remote_identity(matches[0]) != actual:
                    raise UsageError("已知备份对象详情和父目录不一致")
                boundaries.check(fid)
                self.paths.ancestry(fid)
                return actual
            if attempt < 5:
                time.sleep(0.5)
        raise RemoteError("已知备份对象未在父目录确认，已保留状态")

    def _fingerprint(self, path):
        _safe_path(path)
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise UsageError("备份来源已不再是普通文件")
            identity = {"size": before.st_size, "mtime_ns": before.st_mtime_ns, "device": before.st_dev, "inode": before.st_ino, **_hashes(path)}
            after = path.lstat()
        except OSError:
            raise LocalIOError("备份来源无法读取") from None
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise UsageError("备份来源在读取期间发生变化")
        _local(identity)
        return identity

    def _ledger(self, pending, source, boundaries):
        directory = getattr(self.transfer, "state_dir", None) or config_directory() / "transfers"
        name = pending["temporary_name"] if pending["kind"] == "update" else pending["name"]
        ledger = UploadLedger({"source": str(source / pending["path"]), "parent": pending["parent"], "name": name}, directory=directory)
        data = ledger.load(read_only=self._read_only)
        if data is not None:
            if data["local"] != pending["local"]:
                raise LocalIOError("原上传账本的来源完整指纹与备份意图不符")
            original = _Boundaries(self.paths, data["scope_fid"], boundaries)
            self._exact(self._directory(pending["parent"]), original)
            original.check(pending["parent"], write=True)
        return data

    def _receipt(self, pending, source, boundaries, limiter):
        data = self._ledger(pending, source, boundaries)
        if data is None or data["phase"] not in {"completed", "finish_confirmed"}:
            return None
        fid = valid_fid(data["final_fid"])
        name = pending["temporary_name"] if pending["kind"] == "update" else pending["name"]
        actual = self._known(fid, pending["parent"], name, "file", boundaries)
        if actual["size"] != pending["local"]["size"]:
            raise UsageError("原上传 receipt 大小与备份意图不符")
        if data["final_identity"] is not None:
            recorded = data["final_identity"]
            if any(actual.get(k) != recorded.get(k) for k in recorded):
                raise UsageError("原上传 receipt 的远端版本发生变化")
        self._content(actual, pending["local"], _Boundaries(self.paths, data["scope_fid"], boundaries), limiter, force=True)
        return actual

    def _controlled(self, identity, local, names, boundaries, limiter):
        actual = _remote_identity(self.files.info(identity["fid"]))
        if any(actual.get(k) != identity.get(k) for k in ("fid", "parent", "kind", "size")) or actual["name"] not in names:
            raise UsageError("受控改名对象的精确身份或计划名称不符")
        if actual["name"] == identity["name"] and actual != identity:
            raise UsageError("受控改名前对象版本发生变化")
        self._content(actual, local, boundaries, limiter, force=True)
        return actual

    def _pending_view(self, source, nodes, boundaries, limiter):
        pending = self.document["pending"]
        if pending is None:
            return {}
        current = nodes.get(pending["path"])
        kind = "directory" if pending["kind"] == "mkdir" else "file"
        if current is None or current["kind"] != kind or (kind == "file" and current["identity"] != pending["local"]):
            raise UsageError("未完成备份的来源路径或完整指纹变化")
        if pending["kind"] == "mkdir":
            if pending["fid"] is None:
                raise RemoteError("创建目录受理未知且无确切 FID；未按名称认领或重发")
            actual = self._known(pending["fid"], pending["parent"], pending["name"], "directory", boundaries)
            return {pending["name"]: actual["fid"]}
        name = pending["temporary_name"] if pending["kind"] == "update" else pending["name"]
        known = {}
        if pending["new"] is not None:
            names = {pending["new"]["name"]}
            if pending["phase"] == "promote_submitting":
                names.add(pending["name"])
            actual = self._controlled(pending["new"], pending["local"], names, boundaries, limiter)
            known[actual["name"]] = actual["fid"]
        elif pending["phase"] == "upload_submitting":
            actual = self._receipt(pending, source, boundaries, limiter)
            if actual:
                known[name] = actual["fid"]
            else:
                data = self._ledger(pending, source, boundaries)
                if data is None:
                    raise RemoteError("备份上传意图已提交但原传输账本缺失，受理未知")
                # The original Transfer alone may resolve finish_waiting's task;
                # this marker permits no name claim or other backup write.
                if data["phase"] == "finish_waiting":
                    known[name] = None
        if pending["kind"] == "update":
            old = pending["old"]
            if pending["phase"] in {"upload_prepared", "upload_submitting", "upload_confirmed"}:
                self._exact(old["remote"], boundaries)
                self._content(old["remote"], old["local"], boundaries, limiter)
                known[pending["name"]] = old["remote"]["fid"]
            else:
                identity = pending["archive"] or old["remote"]
                names = {pending["archive_name"]}
                if pending["phase"] == "archive_submitting":
                    names.add(pending["name"])
                actual = self._controlled(identity, old["local"], names, boundaries, limiter)
                known[actual["name"]] = actual["fid"]
        return known

    def _preflight(self, source, node_list, target, update, scope, limiter):
        document = self.document
        if document["source"] != str(source) or document["target"] != target:
            raise UsageError("备份清单来源或目标根身份发生变化")
        boundaries = _Boundaries(self.paths, document["scope_fid"], scope)
        self._exact(document["target"], boundaries)
        boundaries.check(target["fid"], write=True)
        nodes = {node["path"]: node for node in node_list}
        pending = document["pending"]
        known = self._pending_view(source, nodes, boundaries, limiter)
        for relative, entry in document["entries"].items():
            if pending and pending["path"] == relative:
                continue
            self._exact(entry["remote"], boundaries)
            if entry["kind"] == "file":
                self._content(entry["remote"], entry["local"], boundaries, limiter)
        for archived in document["archives"]:
            self._content(archived["remote"], archived["local"], boundaries, limiter)
        planned = {"": target["fid"]}
        listings, actions = {}, []
        for node in node_list:
            relative = node["path"]
            if not relative:
                continue
            parent = planned[_parent(relative)]
            if parent is not None and parent not in listings:
                listings[parent] = self._children(parent, boundaries)
            rows = listings.get(parent, [])
            matches = [row for row in rows if _name_key(row["file_name"]) == _name_key(_name(relative))]
            entry = document["entries"].get(relative)
            if pending and pending["path"] == relative:
                if any(row["file_name"] not in known or (known[row["file_name"]] is not None and str(row["fid"]) != known[row["file_name"]]) for row in matches):
                    raise UsageError("未完成备份目标出现第三方同名对象")
                if pending["kind"] == "update":
                    for reserved in (pending["temporary_name"], pending["archive_name"]):
                        reserved_matches = [r for r in rows if _name_key(r["file_name"]) == _name_key(reserved)]
                        if any(r["file_name"] not in known or str(r["fid"]) != known[r["file_name"]] for r in reserved_matches):
                            raise UsageError("备份临时名或归档名被第三方占用")
                action = pending["kind"]
                if node["kind"] == "directory":
                    planned[relative] = pending["fid"]
            elif entry:
                if entry["kind"] != node["kind"] or len(matches) != 1 or str(matches[0]["fid"]) != entry["remote"]["fid"]:
                    raise UsageError("备份路径类型或原 FID 归属变化")
                if node["kind"] == "directory":
                    planned[relative], action = entry["remote"]["fid"], "unchanged"
                elif node["identity"]["sha256"] == entry["local"]["sha256"]:
                    action = "unchanged"
                elif update:
                    action = "update"
                else:
                    raise UsageError("备份来源内容已变化；需要 --update 保留旧版本")
            else:
                if matches:
                    raise UsageError("备份目标存在未管理同名对象，禁止按名称收编")
                action = "mkdir" if node["kind"] == "directory" else "upload"
                if node["kind"] == "directory":
                    planned[relative] = None
            actions.append({"path": relative, "kind": node["kind"], "action": action})
        return boundaries, actions

    def _vacant(self, parent, name, boundaries, allowed=None):
        rows = self._children(parent, boundaries)
        if any(_name_key(row["file_name"]) == _name_key(name) and (row["file_name"] != name or str(row["fid"]) != allowed) for row in rows):
            raise UsageError("备份目标名称被第三方占用，已停止")
        return rows

    def _make_pending(self, node, parent, update):
        relative = node["path"]
        kind = "update" if update else "mkdir" if node["kind"] == "directory" else "upload"
        pending = {"kind": kind, "path": relative, "parent": parent, "name": _name(relative),
                   "phase": "mkdir_submitting" if kind == "mkdir" else "upload_prepared",
                   "local": node.get("identity"), "old": copy.deepcopy(self.document["entries"].get(relative)) if update else None,
                   "new": None, "fid": None, "temporary_name": None, "archive_name": None, "archive": None}
        if update:
            prefix = pending["name"][:48]
            nonce = uuid.uuid4().hex
            pending["temporary_name"] = safe_transfer_name(prefix + ".new-" + nonce)
            pending["archive_name"] = safe_transfer_name(prefix + ".old-" + nonce)
        self.document["pending"] = pending
        self._persist()

    def _upload_pending(self, source, boundaries, limiter, progress, done, total):
        pending = self.document["pending"]
        path = source / pending["path"]
        if self._fingerprint(path) != pending["local"]:
            raise UsageError("备份上传前来源完整指纹变化")
        parent, name = pending["parent"], pending["temporary_name"] if pending["kind"] == "update" else pending["name"]
        self._exact(self._directory(parent), boundaries)
        boundaries.check(parent, write=True)
        if pending["phase"] == "upload_submitting":
            receipt = self._receipt(pending, source, boundaries, limiter)
            if receipt is not None:
                self._unpersisted_fid = receipt["fid"]
                pending.update({"new": receipt, "fid": receipt["fid"], "phase": "upload_confirmed"})
                self._persist()
                return
            if self._ledger(pending, source, boundaries) is None:
                raise RemoteError("原上传账本缺失；未重新提交备份上传")
        else:
            self._vacant(parent, name, boundaries)
            pending["phase"] = "upload_submitting"
            self._persist()
        self._exact_now(self._directory(parent))
        result = self.transfer.upload(path, parent, resume=True, remote_name=name, limiter=limiter,
                                      progress=RecursiveTransfer._progress(progress, pending["path"], done, total), scope=boundaries)
        fid = valid_fid(result.get("fid"))
        pending = self.document["pending"]
        self._unpersisted_fid = fid
        pending["fid"] = fid
        # Persist the response FID before read-back; failed verification does
        # not erase a receipt or allow a fresh upload on the next process.
        self._persist()
        actual = self._known(fid, parent, name, "file", boundaries)
        if actual["size"] != pending["local"]["size"]:
            raise UsageError("新备份对象大小不符")
        self._content(actual, pending["local"], boundaries, limiter, force=True)
        pending = self.document["pending"]
        pending.update({"new": actual, "phase": "upload_confirmed"})
        self._persist()

    def _rename_pending(self, phase, boundaries, limiter):
        pending = self.document["pending"]
        archive = phase == "archive"
        entry = pending["old"] if archive else {"remote": pending["new"], "local": pending["local"]}
        identity, local = entry["remote"], entry["local"]
        name = pending["archive_name"] if archive else pending["name"]
        submitting, confirmed = phase + "_submitting", phase + "_confirmed"
        if pending["phase"] == submitting:
            actual = self._controlled(identity, local, {identity["name"], name}, boundaries, limiter)
            if actual["name"] != name:
                raise RemoteError("原改名尚未只读确认；受理未知且未重发")
            self._vacant(pending["parent"], name, boundaries, allowed=identity["fid"])
        else:
            self._content(identity, local, boundaries, limiter, force=True)
            boundaries.check(identity["fid"], mutate_object=True, write=True)
            self._vacant(pending["parent"], name, boundaries)
            pending["phase"] = submitting
            self._persist()
            # Parent listing and scope proofs can race with a move/version
            # change. The final read before rename must use the bound identity.
            self._exact_now(self._directory(pending["parent"]))
            self._exact_now(identity)
            error = None
            try:
                self.files.rename(identity["fid"], name)
            except QuarkError as failure:
                error = failure
            actual = self._controlled(identity, local, {identity["name"], name}, boundaries, limiter)
            if actual["name"] != name:
                if error is not None:
                    raise error
                raise RemoteError("改名结果未确认，已保留原步骤且未重发")
            self._vacant(pending["parent"], name, boundaries, allowed=identity["fid"])
        pending = self.document["pending"]
        pending["archive" if archive else "new"] = actual
        pending["phase"] = confirmed
        self._persist()

    def _execute_pending(self, source, boundaries, limiter, progress, done, total):
        pending = self.document["pending"]
        relative, kind = pending["path"], pending["kind"]
        if kind == "mkdir":
            if pending["fid"] is None:
                # New callers enter here immediately after fsync of intention.
                self._vacant(pending["parent"], pending["name"], boundaries)
                self._exact_now(self._directory(pending["parent"]))
                response = self.files.mkdir(pending["name"], pending["parent"])
                fid = _optional_created_fid(response)
                if fid is None:
                    raise RemoteError("创建目录未返回确切 FID；受理未知，未按名称认领")
                self._unpersisted_fid = valid_fid(fid)
                pending["fid"], pending["phase"] = self._unpersisted_fid, "mkdir_confirmed"
                self._persist()
            pending = self.document["pending"]
            actual = self._known(pending["fid"], pending["parent"], pending["name"], "directory", boundaries)
            self.document["entries"][relative] = {"kind": "directory", "local": None, "remote": actual}
        else:
            if pending["phase"] in {"upload_prepared", "upload_submitting"}:
                self._upload_pending(source, boundaries, limiter, progress, done, total)
            if kind == "update":
                if self.document["pending"]["phase"] in {"upload_confirmed", "archive_submitting"}:
                    self._rename_pending("archive", boundaries, limiter)
                if self.document["pending"]["phase"] in {"archive_confirmed", "promote_submitting"}:
                    self._rename_pending("promote", boundaries, limiter)
            pending = self.document["pending"]
            self._content(pending["new"], pending["local"], boundaries, limiter, force=True)
            self.document["entries"][relative] = {"kind": "file", "local": pending["local"], "remote": pending["new"]}
            if kind == "update":
                self.document["archives"].append({"path": relative, "local": pending["old"]["local"], "remote": pending["archive"]})
        self.document["pending"] = None
        self._persist()
        return {"path": relative, "kind": "directory" if kind == "mkdir" else "file", "action": kind,
                "result": "confirmed", "fid": actual["fid"] if kind == "mkdir" else pending["new"]["fid"], "name": _name(relative)}

    def _evidence(self, result):
        output = copy.deepcopy(result)
        document = self.document
        if document is not None:
            output["archives"] = [{"path": row["path"], "fid": row["remote"]["fid"], "name": row["remote"]["name"]} for row in document["archives"]]
            pending = document["pending"]
            output["pending"] = None if pending is None else {"kind": pending["kind"], "path": pending["path"], "phase": pending["phase"],
                "parent_fid": pending["parent"], "name": pending["name"], "fid": pending["old"]["remote"]["fid"] if pending["old"] else pending["fid"],
                "new_fid": pending["fid"] if pending["kind"] == "update" else None,
                "temporary_name": pending["temporary_name"], "archive_name": pending["archive_name"]}
            if pending is not None and self._unpersisted_fid is not None:
                output["pending"]["unpersisted_fid"] = self._unpersisted_fid
        return safe_operation_result(output)

    def run(self, source, target, apply=False, update=False, limit_rate=None, progress=None, scope=None):
        self.document, self._durable_document, self.manifest_path = None, None, None
        self._unpersisted_fid = None
        self._read_only = not apply
        result = {"result": "preview" if not apply else "completed", "dry_run": not apply, "items": [], "archives": [], "pending": None,
                  "uploaded_files": 0, "updated_files": 0, "skipped_files": 0, "created_directories": 0}
        try:
            limiter = RateLimiter(limit_rate)
            source, nodes = _local_tree(Path(source))
            source = source.resolve()
            result.update({"source": str(source), "files": sum(n["kind"] == "file" for n in nodes),
                           "directories": sum(n["kind"] == "directory" for n in nodes)})
            obj = self.paths.resolve(target, directory=True)
            root = _remote_identity(obj.item or {}, root=True)
            result["target_fid"] = root["fid"]
            self.manifest_path, lock_name = self._state(root["fid"])

            def prepare():
                fresh_source, fresh_nodes = _local_tree(source)
                current_obj = self.paths.resolve(target, directory=True)
                current_root = _remote_identity(current_obj.item or {}, root=True)
                if current_root != root:
                    raise UsageError("加锁前后备份目标根身份变化")
                document = self._load(self.manifest_path, read_only=self._read_only)
                self._durable_document = copy.deepcopy(document)
                if document is None:
                    now = time.time()
                    document = {"version": 1, "source": str(fresh_source.resolve()), "target": root, "scope_fid": scope.fid if scope else None,
                                "entries": {}, "archives": [], "pending": None, "created_at": now, "updated_at": now}
                self.document = document
                boundaries, actions = self._preflight(source, fresh_nodes, root, update, scope, limiter)
                return fresh_nodes, boundaries, actions

            if not apply:
                nodes, boundaries, actions = prepare()
                result["items"] = [{**action, "result": "planned" if action["action"] != "unchanged" else "unchanged"} for action in actions]
                return self._evidence(result)
            if self.state_dir == config_directory() / "backups":
                secure_directory(config_directory())
            with StateLock(self.state_dir, lock_name):
                nodes, boundaries, actions = prepare()
                if not self.manifest_path.exists():
                    self._persist()
                by_path = {node["path"]: node for node in nodes}
                total, done = sum(n.get("identity", {}).get("size", 0) for n in nodes), 0
                if self.document["pending"] is not None:
                    pending_path = self.document["pending"]["path"]
                    row = self._execute_pending(source, boundaries, limiter, progress, done, total)
                    result["items"].append(row)
                    result[{"mkdir": "created_directories", "upload": "uploaded_files", "update": "updated_files"}[row["action"]]] += 1
                    if row["kind"] == "file":
                        done += by_path[pending_path]["identity"]["size"]
                    actions = [action for action in actions if action["path"] != pending_path]
                for action in actions:
                    relative, kind = action["path"], action["action"]
                    if kind == "unchanged":
                        entry = self.document["entries"][relative]
                        self._exact(entry["remote"], boundaries)
                        result["items"].append({**action, "result": "unchanged", "fid": entry["remote"]["fid"], "name": _name(relative)})
                        if action["kind"] == "file":
                            result["skipped_files"] += 1
                            done += by_path[relative]["identity"]["size"]
                        continue
                    if self.document["pending"] is None:
                        parent_path = _parent(relative)
                        parent = self.document["entries"][parent_path]["remote"]["fid"] if parent_path else root["fid"]
                        self._exact(self.document["entries"][parent_path]["remote"] if parent_path else root, boundaries)
                        self._vacant(parent, _name(relative), boundaries, allowed=self.document["entries"][relative]["remote"]["fid"] if kind == "update" else None)
                        self._make_pending(by_path[relative], parent, kind == "update")
                    row = self._execute_pending(source, boundaries, limiter, progress, done, total)
                    result["items"].append(row)
                    result[{"mkdir": "created_directories", "upload": "uploaded_files", "update": "updated_files"}[row["action"]]] += 1
                    if action["kind"] == "file":
                        done += by_path[relative]["identity"]["size"]
                return self._evidence(result)
        except QuarkError as error:
            self._restore_checkpoint()
            result["result"] = "partial" if any(item.get("result") == "confirmed" for item in result["items"]) else "unknown" if self.document and self.document.get("pending") else "failed"
            safe = type(error)("备份未完整完成；已保留确认结果与原操作状态，未自动重发或删除")
            safe.backup_evidence = self._evidence(result)
            raise safe from None
        except (OSError, ValueError, TypeError):
            self._restore_checkpoint()
            result["result"] = "unknown" if self.document and self.document.get("pending") else "failed"
            error = LocalIOError("备份本地处理失败，已停止后续写入")
            error.backup_evidence = self._evidence(result)
            raise error from None
        except Exception:
            self._restore_checkpoint()
            result["result"] = "unknown" if self.document and self.document.get("pending") else "failed"
            error = RemoteError("备份操作结果未完整确认；已保留原状态")
            error.backup_evidence = self._evidence(result)
            raise error from None
