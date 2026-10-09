"""Versioned private transfer ledgers, keyed and locked by destination."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

from .errors import LocalIOError
from .state import StateLock, config_directory, read_json, secure_directory, write_json


UPLOAD_PHASES = {"prepared", "pre_submitting", "pre_confirmed", "hash_submitting", "hash_confirmed", "parts", "flash_confirmed",
                 "merge_submitting", "merge_confirmed", "finish_submitting", "finish_waiting", "finish_confirmed", "completed"}
DOWNLOAD_PHASES = {"prepared", "streaming", "payload_verified", "publishing", "completed"}
CALLBACK_FIELDS = {"callbackUrl", "callbackHost", "callbackBody", "callbackBodyType", "callbackSNI"}


def safe_local_path(value: str | Path) -> Path:
    """Reject links before resolving, including Windows directory junctions."""
    path = Path(value).expanduser()
    if "\0" in str(path):
        raise LocalIOError("传输本地路径不能包含 NUL")
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        for candidate in (path, *path.parents):
            if candidate.is_symlink() or (candidate.exists() and getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400):
                raise LocalIOError("传输本地路径不能包含符号链接或重解析点")
        return path.resolve()
    except OSError:
        raise LocalIOError("无法检查传输本地路径") from None


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not any(ord(char) < 32 or ord(char) == 127 for char in value)


def _digest(value: Any, length: int = 64) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def _key(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_upload(data: dict[str, Any]) -> None:
    expected = {"version", "direction", "phase", "identity", "local", "scope_fid", "upload", "parts", "flash",
                "upload_task_id", "finish_task_id", "final_fid", "final_identity", "callback_warning", "created_at", "updated_at"}
    if set(data) != expected or data["direction"] != "upload" or data["phase"] not in UPLOAD_PHASES:
        raise ValueError()
    identity, local, upload = data["identity"], data["local"], data["upload"]
    if not isinstance(identity, dict) or set(identity) != {"source", "parent", "name"} or not all(_text(v) for v in identity.values()):
        raise ValueError()
    if not Path(identity["source"]).is_absolute() or any(c in identity["name"] for c in "/\\") or identity["name"] in (".", ".."):
        raise ValueError()
    if not isinstance(local, dict) or set(local) != {"size", "mtime_ns", "device", "inode", "sha256", "md5", "sha1"}:
        raise ValueError()
    if not all(_integer(local[k]) for k in ("size", "mtime_ns", "device", "inode")) or not all(_digest(local[k], n) for k, n in (("sha256", 64), ("md5", 32), ("sha1", 40))):
        raise ValueError()
    if not isinstance(upload, dict):
        raise ValueError()
    if upload:
        if set(upload) != {"task_id", "upload_id", "bucket", "obj_key", "auth_info", "callback", "mime_type", "part_size", "part_count"}:
            raise ValueError()
        if not all(_text(upload[k]) for k in ("task_id", "upload_id", "bucket", "obj_key", "auth_info", "mime_type")):
            raise ValueError()
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*", upload["bucket"]) is None or not _integer(upload["part_size"], minimum=1) or not _integer(upload["part_count"], minimum=1) or upload["part_count"] > 10000:
            raise ValueError()
        callback = upload["callback"]
        if not isinstance(callback, dict) or set(callback) - CALLBACK_FIELDS or not callback or not all(isinstance(v, str) for v in callback.values()):
            raise ValueError()
    if data["phase"] not in ("prepared", "pre_submitting", "flash_confirmed", "completed") and not upload:
        raise ValueError()
    if not isinstance(data["parts"], list) or len(data["parts"]) > 10000:
        raise ValueError()
    seen = set()
    for part in data["parts"]:
        if not isinstance(part, dict) or set(part) != {"number", "offset", "length", "md5", "etag", "confirmed"}:
            raise ValueError()
        if not _integer(part["number"], minimum=1) or part["number"] in seen or not _integer(part["offset"]) or not _integer(part["length"]) or not _digest(part["md5"], 32) or type(part["confirmed"]) is not bool:
            raise ValueError()
        if part["etag"] is not None and (not isinstance(part["etag"], str) or re.fullmatch(r"[0-9a-fA-F]{32}", part["etag"]) is None):
            raise ValueError()
        if part["confirmed"] and (part["etag"] is None or part["etag"].lower() != part["md5"]):
            raise ValueError()
        if upload and (part["number"] > upload["part_count"] or part["offset"] != (part["number"] - 1) * upload["part_size"] or part["length"] != min(upload["part_size"], max(0, local["size"] - part["offset"]))):
            raise ValueError()
        seen.add(part["number"])
    if upload and (len(data["parts"]) != upload["part_count"] or upload["part_count"] != max(1, (local["size"] + upload["part_size"] - 1) // upload["part_size"])):
        raise ValueError()
    if type(data["flash"]) is not bool or type(data["callback_warning"]) is not bool:
        raise ValueError()
    for name in ("upload_task_id", "finish_task_id", "final_fid"):
        if data[name] is not None and not _text(data[name]):
            raise ValueError()
    if upload and data["upload_task_id"] != upload["task_id"]:
        raise ValueError()
    if data["phase"] == "finish_waiting" and data["finish_task_id"] is None:
        raise ValueError()
    if data["phase"] == "finish_confirmed" and data["final_fid"] is None:
        raise ValueError()
    if data["phase"] == "flash_confirmed" and not data["flash"]:
        raise ValueError()
    if data["phase"] in ("prepared", "pre_submitting") and (upload or data["parts"] or data["upload_task_id"] is not None or data["final_fid"] is not None):
        raise ValueError()
    if data["final_identity"] is not None:
        _file_identity(data["final_identity"])
        final = data["final_identity"]
        if (final["fid"], final["parent"], final["name"], final["size"]) != (data["final_fid"], identity["parent"], identity["name"], local["size"]):
            raise ValueError()
    if data["phase"] == "completed" and (upload or not data["final_fid"] or data["final_identity"] is None):
        raise ValueError()
    if data["phase"] == "completed" and not data["flash"]:
        ordered = sorted(data["parts"], key=lambda part: part["number"])
        offset = 0
        for number, part in enumerate(ordered, 1):
            if part["number"] != number or part["offset"] != offset or not part["confirmed"] or part["length"] <= 0:
                raise ValueError()
            offset += part["length"]
        if not ordered or offset != local["size"]:
            raise ValueError()


def _file_identity(value: Any) -> None:
    if not isinstance(value, dict) or set(value) - {"fid", "name", "parent", "size", "updated_at"} or not {"fid", "name", "parent", "size"} <= set(value):
        raise ValueError()
    if not all(_text(value[k]) for k in ("fid", "name", "parent")) or not _integer(value["size"]):
        raise ValueError()
    if "updated_at" in value and not valid_version(value["updated_at"]):
        raise ValueError()


def valid_version(value: Any) -> bool:
    try:
        return not isinstance(value, bool) and isinstance(value, (int, float, str)) and math.isfinite(float(value)) and float(value) > 0
    except (ValueError, OverflowError):
        return False


def validate_download(data: dict[str, Any]) -> None:
    expected = {"version", "direction", "phase", "identity", "scope_fid", "remote", "etag", "offset", "prefix_sha256",
                "payload_sha256", "created_at", "updated_at"}
    if set(data) != expected or data["direction"] != "download" or data["phase"] not in DOWNLOAD_PHASES:
        raise ValueError()
    identity = data["identity"]
    if not isinstance(identity, dict) or set(identity) != {"fid", "output"} or not all(_text(v) for v in identity.values()) or not Path(identity["output"]).is_absolute():
        raise ValueError()
    _file_identity(data["remote"])
    if data["remote"]["fid"] != identity["fid"] or not _integer(data["offset"]) or data["offset"] > data["remote"]["size"] or not _digest(data["prefix_sha256"]):
        raise ValueError()
    if data["etag"] is not None and (not isinstance(data["etag"], str) or re.fullmatch(r'"[\x21\x23-\x7e]*"', data["etag"]) is None):
        raise ValueError()
    if data["offset"] and data["etag"] is None:
        raise ValueError()
    if data["payload_sha256"] is not None and not _digest(data["payload_sha256"]):
        raise ValueError()
    if data["phase"] in ("payload_verified", "publishing", "completed") and (data["offset"] != data["remote"]["size"] or data["payload_sha256"] != data["prefix_sha256"]):
        raise ValueError()


class TransferLedger:
    """Persist is the durable checkpoint boundary, intentionally wrappable."""

    direction = ""

    def __init__(self, identity: dict[str, str], *, directory: Path | None = None) -> None:
        self.identity = identity.copy()
        if directory is None:
            root = config_directory()
            secure_directory(root)
            self.directory = root / "transfers"
        else:
            self.directory = directory
        # Upload locking is independent of the local path: two sources cannot
        # simultaneously write the same remote name. The ledger binds source
        # identity explicitly, and also blocks stale intent from another source.
        destination = {"direction": self.direction, "parent": identity["parent"], "name": identity["name"].casefold()} if self.direction == "upload" else {"direction": self.direction, "output": os.path.normcase(identity["output"])}
        self.key = _key(destination)
        self.path = self.directory / f"{self.direction}-{self.key}.json"
        self.lock = StateLock(self.directory, f"{self.direction}-{self.key}.lock")
        self.data: dict[str, Any] | None = None

    def _validate(self, data: dict[str, Any]) -> None:
        try:
            if type(data.get("version")) is not int or data["version"] != 1:
                raise ValueError()
            if data.get("scope_fid") is not None and not _text(data["scope_fid"]):
                raise ValueError()
            for name in ("created_at", "updated_at"):
                if isinstance(data.get(name), bool) or not isinstance(data.get(name), (int, float)) or not math.isfinite(data[name]) or data[name] < 0:
                    raise ValueError()
            (validate_upload if self.direction == "upload" else validate_download)(data)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise LocalIOError("传输账本格式无效或包含未允许字段，已停止") from None

    def load(self, *, read_only: bool = False) -> dict[str, Any] | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        data = read_json(self.path, read_only=read_only)
        self._validate(data)
        if data["identity"] != self.identity:
            raise LocalIOError("目标已有其他本地来源的传输账本，不能复用或覆盖")
        self.data = data
        return copy.deepcopy(data)

    def persist(self, data: dict[str, Any]) -> None:
        snapshot = copy.deepcopy(data)
        snapshot["updated_at"] = time.time()
        self._validate(snapshot)
        if snapshot["identity"] != self.identity:
            raise LocalIOError("传输账本与目标身份不符")
        write_json(self.path, snapshot)
        self.data = snapshot


class UploadLedger(TransferLedger):
    direction = "upload"


class DownloadLedger(TransferLedger):
    direction = "download"

    @property
    def part_path(self) -> Path:
        return Path(self.identity["output"]).parent / f".quark-{self.key}.part"
