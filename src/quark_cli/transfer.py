"""Streaming upload and download workflows."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import stat
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .client import QUARK_DESKTOP_USER_AGENT, QuarkClient
from .errors import AuthenticationError, LocalIOError, QuarkError, RemoteError, UsageError, contextual_error
from .files import Files
from .paths import RemotePaths, Scope, is_directory, valid_fid, validate_name
from .state import private_path
from .transfer_progress import Progress, ProgressEvent, RateLimiter
from .transfer_state import CALLBACK_FIELDS, DownloadLedger, UploadLedger, safe_local_path, valid_version


# QuarkPan's MIT-licensed upload reference uses 4 MiB parts when the server
# does not provide a part size. Prefer any explicit size sent by pre-upload.
DEFAULT_PART_SIZE = 4 * 1024 * 1024
OSS_MIN_PART_SIZE = 100 * 1024
OSS_MAX_PART_SIZE = 5 * 1024 * 1024 * 1024
STREAM_CHUNK_SIZE = 1024 * 1024
PARALLEL_DOWNLOAD_MIN_SIZE = 8 * 1024 * 1024
PARALLEL_DOWNLOAD_MIN_CHUNK = 4 * 1024 * 1024
PARALLEL_DOWNLOAD_MAX_CHUNK = 64 * 1024 * 1024
MAX_DOWNLOAD_CONNECTIONS = 16
DEFAULT_DOWNLOAD_CONNECTIONS = 4
OSS_USER_AGENT = "aliyun-sdk-js/1.0.0 Chrome Mobile 139.0.0.0 on Google Nexus 5 (Android 6.0)"
OSS_COMPLETE_USER_AGENT = "aliyun-sdk-js/1.0.0 Chrome 139.0.0.0 on OS X 10.15.7 64-bit"
UPLOAD_READBACK_ATTEMPTS = 6
UPLOAD_READBACK_INTERVAL = 0.5


class DownloadStopped(QuarkError):
    """A download the caller asked to stop ended at a durable checkpoint.

    Raised only through the ``should_stop`` hook, and only where a resumed
    download has already persisted the progress it ends at: the ledger and its
    part file stay exactly as written, nothing is reset or deleted, and no
    partial target is published. The same request with ``resume=True`` continues
    from that checkpoint.
    """

    label = "download_stopped"

    def __init__(self, message: str, *, resume_available: bool = True):
        super().__init__(message)
        self.resume_available = resume_available


def _data(payload: Mapping[str, Any], operation: str) -> Any:
    if "data" not in payload:
        raise RemoteError(f"{operation}响应缺少 data 字段")
    return payload["data"]


def trusted_url(url: str, *, upload: bool = False) -> str:
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        hostname = ""
        parsed = urlsplit("")
    pds_host = re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+pds\.quark\.cn", hostname) is not None
    allowed = pds_host if upload else any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in ("quark.cn", "aliyuncs.com", "aliyun.com")
    )
    if parsed.scheme != "https" or not hostname or not allowed or parsed.username or parsed.password:
        raise RemoteError("服务端返回了不受信任的传输地址")
    return url


def successor_url(current: str, location: str) -> str:
    next_url = urljoin(current, location)
    return trusted_url(next_url)


@dataclass(frozen=True)
class DownloadAddress:
    """One file's trusted transfer URL and the metadata returned beside it."""

    url: str
    size: int | None
    name: str | None


def download_address(client: QuarkClient, fid: str) -> DownloadAddress:
    """Request the desktop download URL for one FID using only the API session."""
    payload = client.post(
        "file/download",
        params={"sys": "win32", "ve": "2.5.56", "ut": "", "guid": ""},
        body={"fids": [fid]},
        user_agent=QUARK_DESKTOP_USER_AGENT,
    )
    data = _data(payload, "获取下载地址")
    records = data.get("list") if isinstance(data, dict) else data if isinstance(data, list) else None
    if not isinstance(records, list):
        raise RemoteError("下载地址响应缺少文件记录")
    item = next((row for row in records if isinstance(row, dict) and str(row.get("fid", fid)) == fid), None)
    if item is None or not isinstance(item.get("download_url"), str):
        raise RemoteError("请求文件缺少下载地址")
    size, name = item.get("size"), item.get("file_name")
    return DownloadAddress(
        url=trusted_url(item["download_url"]),
        size=size if type(size) is int and size >= 0 else None,
        name=name if isinstance(name, str) and name else None,
    )


def _stream_file(path: Path, offset: int, length: int) -> Iterator[bytes]:
    remaining = length
    try:
        with path.open("rb") as source:
            source.seek(offset)
            while remaining:
                block = source.read(min(STREAM_CHUNK_SIZE, remaining))
                if not block:
                    raise LocalIOError("上传文件在传输期间发生变化或读取不完整")
                remaining -= len(block)
                yield block
    except OSError:
        raise LocalIOError("读取本地上传文件失败") from None


def _rotl32(value: int, count: int) -> int:
    return ((value << count) | (value >> (32 - count))) & 0xFFFFFFFF


class _SHA1State:
    """SHA-1 compression state, used for OSS continuation hash contexts."""

    def __init__(self) -> None:
        self.h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
        self.length = 0

    def update_aligned(self, data: bytes) -> None:
        if len(data) % 64:
            raise RemoteError("服务端分片大小不能生成有效的 SHA-1 增量上下文")
        self.length += len(data)
        for start in range(0, len(data), 64):
            block = data[start : start + 64]
            words = [int.from_bytes(block[index : index + 4], "big") for index in range(0, 64, 4)]
            for index in range(16, 80):
                words.append(_rotl32(words[index - 3] ^ words[index - 8] ^ words[index - 14] ^ words[index - 16], 1))
            a, b, c, d, e = self.h
            for index in range(80):
                if index < 20:
                    function, constant = (b & c) | ((~b) & d), 0x5A827999
                elif index < 40:
                    function, constant = b ^ c ^ d, 0x6ED9EBA1
                elif index < 60:
                    function, constant = (b & c) | (b & d) | (c & d), 0x8F1BBCDC
                else:
                    function, constant = b ^ c ^ d, 0xCA62C1D6
                temporary = (_rotl32(a, 5) + function + e + constant + words[index]) & 0xFFFFFFFF
                a, b, c, d, e = temporary, a, _rotl32(b, 30), c, d
            self.h = [(old + value) & 0xFFFFFFFF for old, value in zip(self.h, (a, b, c, d, e))]

    def context(self) -> str:
        bits = self.length * 8
        value = {
            "hash_type": "sha1",
            "h0": str(self.h[0]),
            "h1": str(self.h[1]),
            "h2": str(self.h[2]),
            "h3": str(self.h[3]),
            "h4": str(self.h[4]),
            "Nl": str(bits & 0xFFFFFFFF),
            "Nh": str((bits >> 32) & 0xFFFFFFFF),
            "data": "",
            "num": "0",
        }
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(encoded).decode("ascii")


def _part_contexts(path: Path, size: int, part_size: int, count: int) -> dict[int, str]:
    """Compute every later part's SHA-1 continuation state in one pass."""
    contexts: dict[int, str] = {}
    state = _SHA1State()
    try:
        with path.open("rb") as source:
            for part_number in range(1, count):
                offset = min(part_number * part_size, size)
                remaining = offset - state.length
                if remaining % 64:
                    raise RemoteError("服务端分片边界不能生成有效的 SHA-1 增量上下文")
                while remaining:
                    block = source.read(min(STREAM_CHUNK_SIZE, remaining))
                    if not block:
                        raise LocalIOError("上传文件在读取期间发生变化")
                    # The configured and advertised part boundaries are 64-byte
                    # aligned. STREAM_CHUNK_SIZE is also block aligned.
                    state.update_aligned(block)
                    remaining -= len(block)
                contexts[part_number + 1] = state.context()
        return contexts
    except OSError:
        raise LocalIOError("读取本地上传文件失败") from None


def _part_size(data: Mapping[str, Any]) -> int:
    value: Any = DEFAULT_PART_SIZE
    for key in ("part_size", "partsize", "chunk_size", "block_size"):
        if data.get(key) is not None:
            value = data[key]
            break
    if type(value) is not int and not (isinstance(value, str) and re.fullmatch(r"[0-9]+", value)):
        raise RemoteError("预上传返回的分片大小无效")
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        raise RemoteError("预上传返回的分片大小无效") from None
    if resolved <= 0:
        raise RemoteError("预上传返回的分片大小无效")
    if resolved > OSS_MAX_PART_SIZE:
        raise RemoteError("服务端分片大小超过对象存储上限 5 GB")
    return resolved


class Transfer:
    def __init__(self, client: QuarkClient, *, state_dir: Path | None = None) -> None:
        self.client = client
        self.state_dir = state_dir

    def _post(self, endpoint: str, body: Mapping[str, Any], label: str) -> dict[str, Any]:
        try:
            return self.client.post(endpoint, body=body)
        except AuthenticationError:
            raise AuthenticationError(f"{label}未获授权；已保留传输状态") from None
        except QuarkError as original:
            raise contextual_error(original, f"{label}失败；已保留传输状态且未自动重试") from None

    def _upload_auth(self, *, task_id: str, auth_info: str, bucket: str, obj_key: str, upload_id: str,
                     mime_type: str, part_number: int | None = None, hash_context: str | None = None,
                     completion_xml: bytes | None = None, callback: Mapping[str, Any] | None = None,
                     list_parts: bool = False, marker: int = 0) -> tuple[str, dict[str, str]]:
        now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        key, identifier = quote(obj_key, safe="/-_.~"), quote(upload_id, safe="-_.~")
        headers = {"Content-Type": mime_type, "x-oss-date": now, "x-oss-user-agent": OSS_USER_AGENT}
        if list_parts:
            canonical = ["GET", "", mime_type, now, f"x-oss-date:{now}", f"x-oss-user-agent:{OSS_USER_AGENT}",
                         f"/{bucket}/{key}?uploadId={identifier}"]
            query = f"uploadId={identifier}&max-parts=1000&part-number-marker={marker}"
        elif completion_xml is not None:
            if callback is None:
                raise RemoteError("预上传响应缺少 callback")
            digest = base64.b64encode(hashlib.md5(completion_xml, usedforsecurity=False).digest()).decode("ascii")
            callback_value = base64.b64encode(json.dumps(callback, separators=(",", ":")).encode()).decode("ascii")
            canonical = ["POST", digest, "application/xml", now, f"x-oss-callback:{callback_value}",
                         f"x-oss-date:{now}", f"x-oss-user-agent:{OSS_COMPLETE_USER_AGENT}",
                         f"/{bucket}/{key}?uploadId={identifier}"]
            headers.update({"Content-Type": "application/xml", "x-oss-user-agent": OSS_COMPLETE_USER_AGENT,
                            "x-oss-callback": callback_value, "Content-MD5": digest})
            query = f"uploadId={identifier}"
        else:
            if type(part_number) is not int or not 1 <= part_number <= 10000:
                raise RemoteError("上传分片编号无效")
            canonical = ["PUT", "", mime_type, now]
            if hash_context:
                canonical.append(f"X-Oss-Hash-Ctx:{hash_context}")
                headers["X-Oss-Hash-Ctx"] = hash_context
            canonical.extend((f"x-oss-date:{now}", f"x-oss-user-agent:{OSS_USER_AGENT}",
                              f"/{bucket}/{key}?partNumber={part_number}&uploadId={identifier}"))
            query = f"partNumber={part_number}&uploadId={identifier}"
        target = trusted_url(f"https://{bucket}.pds.quark.cn/{key}?{query}", upload=True)
        data = _data(self._post("file/upload/auth", {"task_id": task_id, "auth_info": auth_info,
                                                    "auth_meta": "\n".join(canonical)}, "取得上传授权"), "上传授权")
        if not isinstance(data, dict) or not isinstance(data.get("auth_key"), str) or not data["auth_key"]:
            raise RemoteError("上传授权响应缺少 auth_key；已保留传输状态")
        headers["authorization"] = data["auth_key"]
        return target, headers

    def _storage_request(self, method: str, url: str, **options: Any) -> httpx.Response:
        # Only a cooldown check gates each new storage request: there is no
        # per-request or per-byte pacing on uploads/downloads.
        self.client.check_request_cooldown()
        storage = self.client.transfer_client()
        storage.cookies.clear()
        try:
            response = storage.request(method, url, **options)
        except (httpx.TimeoutException, httpx.RequestError):
            raise RemoteError("对象存储请求中断；已保留传输状态") from None
        finally:
            storage.cookies.clear()
        self.client.observe_transfer_response(response)
        return response

    def _scope(self, fid: str, scope: Scope | None, saved: str | None = None, *, write: bool = False) -> None:
        paths = RemotePaths(Files(self.client))
        if saved is not None:
            Scope(paths, saved).check(fid, write=write)
        if scope is not None:
            scope.check(fid, write=write)

    def _parent(self, fid: str, scope: Scope | None, saved: str | None = None) -> None:
        RemotePaths(Files(self.client)).resolve(fid, directory=True)
        self._scope(fid, scope, saved, write=True)

    def _fingerprint(self, path: Path) -> dict[str, Any]:
        try:
            path = safe_local_path(path)
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                raise UsageError("上传路径必须是普通文件")
            hashes = (hashlib.md5(usedforsecurity=False), hashlib.sha1(usedforsecurity=False), hashlib.sha256())
            count = 0
            with path.open("rb") as source:
                if (os.fstat(source.fileno()).st_dev, os.fstat(source.fileno()).st_ino) != (before.st_dev, before.st_ino):
                    raise LocalIOError("上传源文件已被替换")
                while block := source.read(STREAM_CHUNK_SIZE):
                    count += len(block)
                    for digest in hashes:
                        digest.update(block)
            after = path.stat()
            if count != before.st_size or (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino):
                raise LocalIOError("计算哈希期间上传文件发生变化")
            return {"size": count, "mtime_ns": before.st_mtime_ns, "device": before.st_dev, "inode": before.st_ino,
                    "md5": hashes[0].hexdigest(), "sha1": hashes[1].hexdigest(), "sha256": hashes[2].hexdigest()}
        except OSError:
            raise LocalIOError("无法安全读取上传文件") from None

    def _part_md5(self, path: Path, offset: int, length: int) -> str:
        digest = hashlib.md5(usedforsecurity=False)
        for block in _stream_file(path, offset, length):
            digest.update(block)
        return digest.hexdigest()

    def _auth_options(self, upload: dict[str, Any]) -> dict[str, Any]:
        return {name: upload[name] for name in ("task_id", "auth_info", "bucket", "obj_key", "upload_id", "mime_type")}

    def _list_parts(self, upload: dict[str, Any], parts: list[dict[str, Any]]) -> dict[int, str]:
        expected = {part["number"]: part for part in parts}
        received: dict[int, str] = {}
        marker = 0
        listed_bucket: str | None = None
        for _ in range(11):
            url, headers = self._upload_auth(**self._auth_options(upload), list_parts=True, marker=marker)
            response = self._storage_request("GET", url, headers=headers)
            if response.status_code != 200:
                raise RemoteError(f"查询原上传分片失败（HTTP {response.status_code}）；未新建任务")
            root = _xml(response.content)
            if _tag(root) != "ListPartsResult":
                raise RemoteError("原上传任务不可查询；未新建任务")
            bucket = _xml_text(root, "Bucket")
            # PDS's signed public alias routes to an internal OSS bucket.
            # Verify its shape and page stability, never equate it to alias.
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket) is None or (listed_bucket is not None and bucket != listed_bucket):
                raise RemoteError("分片列表 bucket 无效或分页对象发生变化")
            listed_bucket = bucket
            if (_xml_text(root, "Key"), _xml_text(root, "UploadId")) != (upload["obj_key"], upload["upload_id"]):
                raise RemoteError("分片列表与原上传对象不符")
            truncated = _xml_text(root, "IsTruncated")
            if truncated not in ("true", "false"):
                raise RemoteError("分片分页标志无效")
            for element in root:
                if _tag(element) != "Part":
                    continue
                number = _decimal(_xml_text(element, "PartNumber"), "分片编号")
                length = _decimal(_xml_text(element, "Size"), "分片大小")
                etag = _etag_md5(_xml_text(element, "ETag"))
                if number <= marker or number in received or number not in expected:
                    raise RemoteError("分片列表含重复或未知分片")
                part = expected[number]
                if length != part["length"] or etag.lower() != part["md5"] or (part["confirmed"] and etag != part["etag"]):
                    raise RemoteError("已上传分片与本地文件不符，不能覆盖修复")
                received[number] = etag
            if truncated == "false":
                for part in parts:
                    if part["confirmed"] and part["number"] not in received:
                        raise RemoteError("已确认分片在服务端缺失，已停止")
                return received
            successor = _decimal(_xml_text(root, "NextPartNumberMarker"), "分页标记")
            if successor <= marker or successor > 10000 or not received or successor < max(received):
                raise RemoteError("分片分页没有前进")
            marker = successor
        raise RemoteError("分片分页超过安全上限")

    def _upload_result(self, data: dict[str, Any], part_count: int, resumed: bool, skipped: int,
                       *, include_resume: bool) -> dict[str, Any]:
        result = {"file_name": data["identity"]["name"], "file_size": data["local"]["size"],
                  "task_id": data["upload_task_id"], "parts": part_count,
                  "callback_warning": data["callback_warning"], "fid": data["final_fid"]}
        if include_resume:
            result.update({"resumed": resumed, "skipped_parts": skipped, "sha256": data["local"]["sha256"]})
        return result

    def _verify_uploaded(self, fid: str, parent: str, name: str, size: int, scope: Scope | None,
                         saved_scope: str | None, *, local: dict[str, Any] | None = None) -> dict[str, Any]:
        files = Files(self.client)
        for attempt in range(UPLOAD_READBACK_ATTEMPTS):
            if attempt:
                time.sleep(UPLOAD_READBACK_INTERVAL)
            # The expected parent predates this upload and is checked first.
            # Only its newly created child's missing index entry is retryable.
            self._scope(parent, scope, saved_scope, write=True)
            item = files.info(fid)
            identity = _remote_identity(item)
            if identity["parent"] != parent or identity["name"] != name or identity["size"] != size:
                raise RemoteError("上传完成对象与请求的父目录、名称或大小不符")
            for algorithm, length in (("md5", 32), ("sha1", 40)):
                value = item.get(algorithm)
                if value is not None and value != "" and local is not None:
                    if not isinstance(value, str) or re.fullmatch(r"[a-fA-F0-9]{%d}" % length, value) is None or value.lower() != local[algorithm]:
                        raise RemoteError("上传完成对象的可用哈希与本地文件不符")
            matches = [row for row in RemotePaths(files).children(parent) if str(row.get("fid", "")) == fid]
            if not matches:
                continue
            if len(matches) != 1 or _remote_identity(matches[0]) != identity:
                raise RemoteError("上传完成对象与父目录列表不一致")
            # Recheck the precise FID's current ancestry only after membership
            # exists; a genuine boundary/ancestry error is never retried.
            self._scope(fid, scope, saved_scope)
            return identity
        raise RemoteError("上传已明确完成，但精确 FID 的父目录索引读回超时；未重发写请求")

    def _finish_flash(self, data: dict[str, Any], ledger: UploadLedger, meter: Progress, scope: Scope | None,
                      resumed: bool, resume: bool) -> dict[str, Any]:
        # Official pre/hash finish means complete: never issue upload/finish.
        if data["final_fid"] is None:
            raise RemoteError("闪存已受理但响应缺少可关联 FID；无已证实只读消歧接口，未重发写请求")
        identity = data["identity"]
        data["final_identity"] = self._verify_uploaded(data["final_fid"], identity["parent"], identity["name"],
                                                      data["local"]["size"], scope, data["scope_fid"], local=data["local"])
        data.update({"phase": "completed", "upload": {}})
        ledger.persist(data)
        meter.confirmed = meter.transferred = data["local"]["size"]
        meter.emit("completed", force=True)
        return self._upload_result(data, 0, resumed, 0, include_resume=resume)

    def _confirm_finish_task(self, data: dict[str, Any], ledger: UploadLedger) -> None:
        finished = self._wait_task(data["finish_task_id"])
        fid = finished.get("fid", data["final_fid"])
        if not isinstance(fid, str) or not fid:
            raise RemoteError("上传完成任务缺少可关联 FID；状态已保留")
        fid = valid_fid(fid)
        if data["final_fid"] is not None and fid != data["final_fid"]:
            raise RemoteError("上传完成任务 FID 与原预上传关联不符")
        data.update({"final_fid": fid, "phase": "finish_confirmed"})
        ledger.persist(data)

    def upload(self, source: str | Path, parent: str = "0", *, resume: bool = False, limit_rate: int | None = None,
               progress: Callable[[ProgressEvent], None] | None = None, scope: Scope | None = None,
               remote_name: str | None = None, limiter: RateLimiter | None = None) -> dict[str, Any]:
        path = safe_local_path(source)
        name = safe_transfer_name(path.name if remote_name is None else remote_name)
        parent = valid_fid(parent)
        pacing = _limiter(limit_rate, limiter)
        ledger = UploadLedger({"source": str(path), "parent": parent, "name": name}, directory=self.state_dir)
        with ledger.lock:
            local = self._fingerprint(path)
            if local["size"] == 0:
                raise UsageError("夸克不支持上传空文件")
            data = ledger.load()
            resumed = data is not None
            if data is not None:
                if not resume:
                    raise UsageError("目标存在传输账本；使用 --resume 恢复原任务")
                if data["local"] != local:
                    raise LocalIOError("本地上传文件身份或完整哈希已变化，拒绝恢复")
                self._parent(parent, scope, data["scope_fid"])
                if data["phase"] == "completed":
                    identity = self._verify_uploaded(data["final_fid"], parent, name, local["size"], scope, data["scope_fid"], local=local)
                    if identity != data["final_identity"]:
                        raise RemoteError("已完成上传的远端身份发生变化")
                    if "updated_at" not in identity:
                        raise RemoteError("已完成对象缺少可验证版本，不能仅凭名称/大小跳过")
                    count = 0 if data["flash"] else len(data["parts"])
                    return self._upload_result(data, count, True, count, include_resume=True)
                if data["phase"] in ("pre_submitting", "hash_submitting", "merge_submitting", "finish_submitting"):
                    raise RemoteError(f"上传处于 {data['phase']} 未知受理状态；无已证实的只读消歧接口，未重发写请求")
                if data["phase"] == "finish_waiting":
                    # This task is explicitly linked by the finish response.
                    # Resolve its FID before testing same-name target conflicts.
                    self._confirm_finish_task(data, ledger)
            else:
                self._parent(parent, scope)
                rows = RemotePaths(Files(self.client)).children(parent)
                if any(str(row.get("file_name", "")).casefold() == name.casefold() for row in rows):
                    raise UsageError("远端目标同名文件或目录已存在，不能覆盖")
                now = time.time()
                data = {"version": 1, "direction": "upload", "phase": "prepared", "identity": ledger.identity,
                        "local": local, "scope_fid": scope.fid if scope else None, "upload": {}, "parts": [], "flash": False,
                        "upload_task_id": None, "finish_task_id": None, "final_fid": None, "final_identity": None, "callback_warning": False,
                        "created_at": now, "updated_at": now}
                ledger.persist(data)
            meter = Progress("upload", name, local["size"], pacing, progress)
            if data["phase"] == "flash_confirmed":
                return self._finish_flash(data, ledger, meter, scope, resumed, resume)
            if resumed:
                matches = [row for row in RemotePaths(Files(self.client)).children(parent)
                           if str(row.get("file_name", "")).casefold() == name.casefold()]
                if any(str(row.get("fid", "")) != data["final_fid"] for row in matches):
                    raise UsageError("恢复目标出现未关联本任务的同名对象，已停止")
            if data["phase"] == "prepared":
                self._parent(parent, scope, data["scope_fid"])
                data["phase"] = "pre_submitting"
                ledger.persist(data)
                now_ms = int(time.time() * 1000)
                pre = _data(self._post("file/upload/pre", {"ccp_hash_update": True, "parallel_upload": True,
                    "pdir_fid": parent, "dir_name": "", "size": local["size"], "file_name": name,
                    "format_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                    "l_updated_at": now_ms, "l_created_at": now_ms}, "预上传"), "预上传")
                if not isinstance(pre, dict):
                    raise RemoteError("预上传响应格式无效")
                if "finish" in pre and type(pre["finish"]) is not bool:
                    raise RemoteError("预上传完成状态类型无效")
                if pre.get("finish") is True:
                    data.update({"flash": True, "phase": "flash_confirmed",
                                 "upload_task_id": pre.get("task_id") if isinstance(pre.get("task_id"), str) else None,
                                 "final_fid": pre.get("fid") if isinstance(pre.get("fid"), str) and pre["fid"] else None})
                    ledger.persist(data)
                    return self._finish_flash(data, ledger, meter, scope, resumed, resume)
                required = ("task_id", "upload_id", "obj_key", "auth_info")
                if not all(isinstance(pre.get(key), str) and pre[key] for key in required):
                    raise RemoteError("预上传响应缺少恢复所需字段，受理状态未知")
                callback = pre.get("callback")
                if not isinstance(callback, dict) or not callback or set(callback) - CALLBACK_FIELDS or not all(isinstance(value, str) for value in callback.values()):
                    raise RemoteError("预上传 callback 含缺失或未支持字段，受理状态未知")
                size = _part_size(pre)
                count = max(1, (local["size"] + size - 1) // size)
                if size % 64 or count > 10000 or (count > 1 and size < OSS_MIN_PART_SIZE):
                    raise RemoteError("预上传分片大小或数量不满足协议要求")
                data["upload"] = {key: pre[key] for key in required}
                data["upload_task_id"] = pre["task_id"]
                if isinstance(pre.get("fid"), str) and pre["fid"]:
                    data["final_fid"] = valid_fid(pre["fid"])
                data["upload"].update({"bucket": pre.get("bucket") or "ul-zb", "callback": callback,
                                      "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                                      "part_size": size, "part_count": count})
                for number in range(1, count + 1):
                    offset, length = (number - 1) * size, min(size, max(0, local["size"] - (number - 1) * size))
                    data["parts"].append({"number": number, "offset": offset, "length": length,
                                          "md5": self._part_md5(path, offset, length), "etag": None, "confirmed": False})
                if self._fingerprint(path) != local:
                    raise LocalIOError("预上传期间本地文件发生变化")
                data["phase"] = "pre_confirmed"
                ledger.persist(data)
            upload = data["upload"]
            task_id, part_count = upload["task_id"], upload["part_count"]
            if data["phase"] == "pre_confirmed":
                self._parent(parent, scope, data["scope_fid"])
                data["phase"] = "hash_submitting"
                ledger.persist(data)
                hashed = _data(self._post("file/update/hash", {"task_id": task_id, "md5": local["md5"], "sha1": local["sha1"]},
                                         "确认上传哈希"), "上传哈希")
                if not isinstance(hashed, dict) or ("finish" in hashed and type(hashed["finish"]) is not bool):
                    raise RemoteError("哈希确认响应格式无效")
                data["flash"] = hashed.get("finish") is True
                data["phase"] = "flash_confirmed" if data["flash"] else "hash_confirmed"
                if data["flash"]:
                    if isinstance(hashed.get("fid"), str) and hashed["fid"]:
                        hashed_fid = valid_fid(hashed["fid"])
                        if data["final_fid"] is not None and hashed_fid != data["final_fid"]:
                            raise RemoteError("哈希完成 FID 与原预上传关联不符")
                        data["final_fid"] = hashed_fid
                ledger.persist(data)
                if data["flash"]:
                    return self._finish_flash(data, ledger, meter, scope, resumed, resume)
            skipped = 0
            if data["phase"] in ("hash_confirmed", "parts"):
                if resumed:
                    received = self._list_parts(upload, data["parts"])
                    for part in data["parts"]:
                        if part["number"] in received:
                            part.update({"etag": received[part["number"]], "confirmed": True})
                    skipped = len(received)
                    ledger.persist(data)
                data["phase"] = "parts"
                ledger.persist(data)
                meter.confirmed = sum(part["length"] for part in data["parts"] if part["confirmed"])
                meter.transferred = meter.confirmed
                meter.confirmed_parts = sum(part["confirmed"] for part in data["parts"])
                meter.emit("resuming" if resumed else "starting", force=True)
                contexts = _part_contexts(path, local["size"], upload["part_size"], part_count) if part_count > 1 else {}
                for part in data["parts"]:
                    if part["confirmed"]:
                        continue
                    self._parent(parent, scope, data["scope_fid"])
                    if self._fingerprint(path) != local:
                        raise LocalIOError("上传源文件发生变化；未继续上传")
                    url, headers = self._upload_auth(**self._auth_options(upload), part_number=part["number"],
                                                    hash_context=contexts.get(part["number"]))
                    headers["Content-Length"] = str(part["length"])
                    confirmed_before = meter.confirmed
                    def blocks() -> Iterator[bytes]:
                        for block in _stream_file(path, part["offset"], part["length"]):
                            for start in range(0, len(block), pacing.chunk_size):
                                chunk = block[start:start + pacing.chunk_size]
                                pacing.consume(len(chunk))
                                meter.transferred += len(chunk)
                                meter.emit("transferring")
                                yield chunk
                    response = self._storage_request("PUT", url, content=blocks() if part["length"] else b"", headers=headers)
                    if response.status_code != 200:
                        raise RemoteError(f"对象存储拒绝第 {part['number']} 个分片（HTTP {response.status_code}）")
                    etag = _etag_md5(response.headers.get("etag", ""))
                    if etag.lower() != part["md5"]:
                        raise RemoteError("上传分片 ETag 与本地分片 md5 不符")
                    part.update({"etag": etag, "confirmed": True})
                    ledger.persist(data)
                    meter.confirmed = confirmed_before + part["length"]
                    meter.confirmed_parts += 1
                    meter.emit("part_confirmed", force=True)
                if self._fingerprint(path) != local:
                    raise LocalIOError("上传期间本地文件发生变化；未合并")
                root = ET.Element("CompleteMultipartUpload")
                for part in sorted(data["parts"], key=lambda item: item["number"]):
                    element = ET.SubElement(root, "Part")
                    ET.SubElement(element, "PartNumber").text = str(part["number"])
                    ET.SubElement(element, "ETag").text = f'"{part["etag"]}"'
                complete = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                url, headers = self._upload_auth(**self._auth_options(upload), completion_xml=complete, callback=upload["callback"])
                self._parent(parent, scope, data["scope_fid"])
                data["phase"] = "merge_submitting"
                ledger.persist(data)
                response = self._storage_request("POST", url, content=complete, headers=headers)
                _check_merge(response)
                data["callback_warning"] = response.status_code == 203
                data["phase"] = "merge_confirmed"
                ledger.persist(data)
            if data["phase"] == "merge_confirmed":
                if self._fingerprint(path) != local:
                    raise LocalIOError("上传源文件发生变化；未确认完成")
                self._parent(parent, scope, data["scope_fid"])
                data["phase"] = "finish_submitting"
                ledger.persist(data)
                finish = _data(self._post("file/upload/finish", {"task_id": task_id, "obj_key": upload["obj_key"]},
                                          "确认上传完成"), "完成上传")
                if not isinstance(finish, dict) or type(finish.get("finish")) is not bool:
                    raise RemoteError("夸克没有明确确认上传完成")
                if finish["finish"] is False:
                    finish_task = finish.get("task_id")
                    if not isinstance(finish_task, (str, int)) or isinstance(finish_task, bool) or not str(finish_task):
                        raise RemoteError("上传完成响应缺少可轮询的任务 ID")
                    data["finish_task_id"] = str(finish_task)
                    data["phase"] = "finish_waiting"
                    ledger.persist(data)
                else:
                    if isinstance(finish.get("fid"), str) and finish["fid"]:
                        finished_fid = valid_fid(finish["fid"])
                        if data["final_fid"] is not None and finished_fid != data["final_fid"]:
                            raise RemoteError("上传完成 FID 与原预上传关联不符")
                        data["final_fid"] = finished_fid
                    if data["final_fid"] is None:
                        raise RemoteError("上传完成响应缺少可关联的 FID")
                    # A known FID can be verified after a process exits here;
                    # do not resubmit finish for a failed metadata read.
                    data["phase"] = "finish_confirmed"
                    ledger.persist(data)
            if data["phase"] in ("finish_waiting", "finish_confirmed"):
                if data["phase"] == "finish_waiting":
                    self._confirm_finish_task(data, ledger)
                final_identity = self._verify_uploaded(data["final_fid"], parent, name, local["size"], scope, data["scope_fid"], local=local)
                data["final_identity"] = final_identity
                data["phase"] = "completed"
                data["upload"] = {}
                ledger.persist(data)
                meter.confirmed = meter.transferred = local["size"]
                meter.confirmed_parts = 0 if data["flash"] else part_count
                meter.emit("completed", force=True)
                return self._upload_result(data, part_count,
                                           resumed, skipped, include_resume=resume)
            raise RemoteError("上传状态无法继续；已保留原任务")

    def _wait_task(self, task_id: str) -> dict[str, Any]:
        for retry_index in range(60):
            payload = self.client.get("task", params={"task_id": task_id, "retry_index": retry_index})
            data = _data(payload, "上传完成任务")
            if not isinstance(data, dict):
                raise RemoteError("上传完成任务响应格式无效")
            status, finish = data.get("status"), data.get("finish")
            if (status is not None and (type(status) is not int or status not in (0, 1, 2, 3))) or (finish is not None and type(finish) is not bool):
                raise RemoteError("上传完成任务状态无效")
            if status == 3 or (finish is True and status not in (None, 2)) or (status == 2 and finish is False):
                raise RemoteError("上传完成任务报告失败或矛盾状态")
            if status == 2 or finish is True:
                return data
            time.sleep(0.5)
        raise RemoteError("等待夸克确认上传完成超时；已保留任务")

    def _download_address(self, fid: str) -> tuple[str, int | None]:
        address = download_address(self.client, fid)
        return address.url, address.size

    @contextmanager
    def _download_stream(self, url: str, headers: dict[str, str], *, storage: httpx.Client | None = None) -> Iterator[httpx.Response]:
        storage = storage or self.client.transfer_client()
        current = url
        try:
            for index in range(6):
                # Check the shared cooldown before every new storage request or
                # redirect hop; no byte or request-start pacing is added here.
                self.client.check_request_cooldown()
                current = trusted_url(current)
                host = (urlsplit(current).hostname or "").lower().rstrip(".")
                request_headers = dict(headers)
                cookie = self.client.transfer_cookie_header(host)
                storage_host = (
                    host.endswith(".pds.quark.cn")
                    or re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+drive\.quark\.cn", host) is not None
                )
                if storage_host and cookie is None:
                    raise AuthenticationError("下载目标需要 __puus Cookie；请重新登录或提供完整 Cookie")
                if cookie is not None:
                    request_headers["Cookie"] = cookie
                storage.cookies.clear()
                try:
                    with storage.stream("GET", current, headers=request_headers) as response:
                        self.client.observe_transfer_response(response)
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location or index == 5:
                                raise RemoteError("下载地址重定向无效或次数过多")
                            current = successor_url(current, location)
                            continue
                        trusted_url(str(response.url))
                        yield response
                        return
                finally:
                    storage.cookies.clear()
        except (httpx.TimeoutException, httpx.RequestError):
            raise RemoteError("下载传输失败；可恢复检查点已保留") from None
        finally:
            storage.cookies.clear()

    def _download_parallel(self, fid: str, url: str, target: Path, expected: int | None, *,
                           overwrite: bool, connections: int, limiter: RateLimiter,
                           progress: Callable[[ProgressEvent], None] | None) -> dict[str, Any] | None:
        if connections <= 1 or expected is None or expected < PARALLEL_DOWNLOAD_MIN_SIZE:
            return None
        chunk_size = min(PARALLEL_DOWNLOAD_MAX_CHUNK,
                         max(PARALLEL_DOWNLOAD_MIN_CHUNK, (expected + connections * 4 - 1) // (connections * 4)))
        ranges = [(start, min(start + chunk_size, expected) - 1) for start in range(0, expected, chunk_size)]
        if len(ranges) < 2:
            return None

        descriptor, filename = tempfile.mkstemp(prefix=".quark-download-", dir=target.parent)
        os.close(descriptor)
        part = Path(filename)
        meter = Progress("download", target.name, expected, limiter, progress)
        progress_lock = threading.Lock()
        try:
            with part.open("r+b") as sink:
                sink.truncate(expected)

            first_start, first_end = ranges[0]
            headers = {"Accept-Encoding": "identity", "Range": f"bytes={first_start}-{first_end}"}
            with self._download_stream(url, headers) as response:
                if response.status_code == 200:
                    if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                        raise RemoteError("下载响应未采用 identity 编码，不能按字节校验")
                    declared = _content_length(response)
                    if declared is not None and declared != expected:
                        raise RemoteError("下载正文长度与网盘大小不符")
                    with part.open("r+b") as sink:
                        sink.truncate(0)
                        received = 0
                        chunks = response.iter_bytes if response.is_stream_consumed else response.iter_raw
                        for block in chunks(limiter.chunk_size):
                            if not block:
                                continue
                            if received + len(block) > expected:
                                raise RemoteError("下载正文超过网盘声明长度")
                            limiter.consume(len(block))
                            if sink.write(block) != len(block):
                                raise LocalIOError("下载临时文件写入不完整")
                            received += len(block)
                            with progress_lock:
                                meter.transferred += len(block)
                                meter.emit("transferring")
                        if received != expected:
                            raise RemoteError("下载正文与网盘大小不符；未发布")
                    return self._install_download_part(fid, part, target, expected, overwrite, meter)
                total, length = _range(response, first_start, expected, bounded_end=first_end)
                etag = _strong_etag(response.headers.get("etag"))
                if etag is None:
                    return None
                if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                    raise RemoteError("下载响应未采用 identity 编码，不能按字节分段")
                if total != expected:
                    raise RemoteError("分段下载总大小与网盘文件大小不符")
                with part.open("r+b") as sink:
                    self._write_download_range(response, sink, first_start, length, limiter, meter, progress_lock)

            thread_state = threading.local()
            worker_clients: list[httpx.Client] = []
            worker_clients_lock = threading.Lock()

            def worker_client() -> httpx.Client:
                client = getattr(thread_state, "client", None)
                if client is None:
                    client = self.client.new_transfer_client()
                    thread_state.client = client
                    with worker_clients_lock:
                        worker_clients.append(client)
                return client

            def fetch_range(start: int, end: int) -> None:
                with self._download_stream(url, {"Accept-Encoding": "identity", "Range": f"bytes={start}-{end}"},
                                           storage=worker_client()) as response:
                    _, length = _range(response, start, expected, bounded_end=end)
                    response_etag = _strong_etag(response.headers.get("etag"))
                    if response_etag != etag:
                        raise RemoteError("并发分段的强 ETag 不一致；拒绝拼接")
                    if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                        raise RemoteError("下载响应未采用 identity 编码，不能按字节分段")
                    with part.open("r+b") as sink:
                        self._write_download_range(response, sink, start, length, limiter, meter, progress_lock)

            try:
                with ThreadPoolExecutor(max_workers=min(connections, len(ranges))) as executor:
                    futures = [executor.submit(fetch_range, start, end) for start, end in ranges[1:]]
                    for future in as_completed(futures):
                        future.result()
            finally:
                self.client.release_transfer_clients(worker_clients)

            if part.stat().st_size != expected or meter.transferred != expected:
                raise RemoteError("并发分段下载总字节数不符；未发布")
            return self._install_download_part(fid, part, target, expected, overwrite, meter)
        except QuarkError:
            raise
        except OSError:
            raise LocalIOError("无法写入或发布并发下载文件") from None
        finally:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _install_download_part(fid: str, part: Path, target: Path, expected: int, overwrite: bool,
                               meter: Progress) -> dict[str, Any]:
        try:
            if part.stat().st_size != expected or meter.transferred != expected:
                raise RemoteError("下载总字节数不符；未发布")
            with part.open("r+b") as sink:
                sink.flush()
                os.fsync(sink.fileno())
            digest = hashlib.sha256()
            with part.open("rb") as source:
                while block := source.read(STREAM_CHUNK_SIZE):
                    digest.update(block)
        except OSError:
            raise LocalIOError("无法核验下载临时文件") from None
        digest_hex = digest.hexdigest()
        _publish(part, target, overwrite)
        meter.transferred = meter.confirmed = expected
        meter.emit("completed", force=True)
        return _download_result(fid, target, expected, False, False, digest_hex)

    @staticmethod
    def _write_download_range(response: httpx.Response, sink, offset: int, length: int,
                              limiter: RateLimiter, meter: Progress, progress_lock: threading.Lock) -> None:
        received = 0
        sink.seek(offset)
        chunks = response.iter_bytes if response.is_stream_consumed else response.iter_raw
        for block in chunks(limiter.chunk_size):
            if not block:
                continue
            if received + len(block) > length:
                raise RemoteError("分段下载正文超过声明范围")
            limiter.consume(len(block))
            if sink.write(block) != len(block):
                raise LocalIOError("并发下载临时文件写入不完整")
            received += len(block)
            with progress_lock:
                meter.transferred += len(block)
                meter.emit("transferring")
        if received != length:
            raise RemoteError("分段下载正文与声明范围不符")
        sink.flush()

    def _download_checkpointed_ranges(self, url, expected, part, data, ledger, digest, meter,
                                      connections, should_stop) -> bool:
        """Read a bounded window of ranges; only the writer advances the ledger."""
        offset = data["offset"]
        chunk = PARALLEL_DOWNLOAD_MIN_CHUNK
        if connections <= 1 or expected < PARALLEL_DOWNLOAD_MIN_SIZE or expected - offset <= chunk:
            return False
        headers = {"Accept-Encoding": "identity", "Range": f"bytes={offset}-{offset}"}
        if data["etag"] is not None:
            headers["If-Range"] = data["etag"]
        with self._download_stream(url, headers) as response:
            if response.status_code == 200 and offset == 0:
                return False
            _range(response, offset, expected, bounded_end=offset)
            etag = _strong_etag(response.headers.get("etag"))
            if etag is None:
                return False
            if data["etag"] is not None and etag != data["etag"]:
                raise RemoteError("远端强 ETag 已变化；未追加")
            if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                raise RemoteError("下载响应未采用 identity 编码，不能按字节分段")
            received = 0
            for block in response.iter_bytes():
                received += len(block)
                if received > 1:
                    raise RemoteError("分段探测正文长度不符")
            if received != 1:
                raise RemoteError("分段探测正文长度不符")
        data.update({"etag": etag, "phase": "streaming"})
        ledger.persist(data)
        if not part.exists():
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
        private_path(part)
        cancel = threading.Event()
        lock = threading.Lock()
        errors = []
        local = threading.local()
        clients = []
        workers = min(connections, (expected - offset + chunk - 1) // chunk)
        meter.connections = workers

        def check_stop():
            if cancel.is_set() or (should_stop is not None and should_stop()):
                raise DownloadStopped("下载已停止：已保留安全检查点，再次提交可继续")

        def fetch(start, end):
            try:
                check_stop()
                if not hasattr(local, "client"):
                    local.client = self.client.new_transfer_client()
                    with lock:
                        clients.append(local.client)
                with self._download_stream(url, {"Accept-Encoding": "identity",
                                                "Range": f"bytes={start}-{end}", "If-Range": etag},
                                           storage=local.client) as response:
                    _, length = _range(response, start, expected, bounded_end=end)
                    if _strong_etag(response.headers.get("etag")) != etag:
                        raise RemoteError("并发分段的强 ETag 不一致；拒绝拼接")
                    if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                        raise RemoteError("分段下载响应未采用 identity 编码")
                    body = bytearray()
                    chunks = response.iter_bytes if response.is_stream_consumed else response.iter_raw
                    for block in chunks(meter.limiter.chunk_size):
                        check_stop()
                        if len(body) + len(block) > length:
                            raise RemoteError("分段下载正文超过声明范围")
                        meter.limiter.consume(len(block))
                        body.extend(block)
                        with lock:
                            meter.transferred += len(block)
                            meter.emit("transferring")
                    if len(body) != length:
                        raise RemoteError("分段下载正文与声明范围不符")
                    return body
            except BaseException as error:
                if not isinstance(error, DownloadStopped):
                    with lock:
                        errors.append(error)
                cancel.set()
                raise

        pool = ThreadPoolExecutor(max_workers=workers)
        pending = deque()
        start = offset
        try:
            with part.open("ab" if offset else "wb") as sink:
                for _ in range(workers):
                    end = min(start + chunk, expected) - 1
                    pending.append(pool.submit(fetch, start, end))
                    start = end + 1
                while pending:
                    future = pending.popleft()
                    block = future.result()
                    check_stop()
                    if sink.write(block) != len(block):
                        raise LocalIOError("下载临时文件写入不完整")
                    digest.update(block)
                    sink.flush()
                    os.fsync(sink.fileno())
                    data.update({"offset": data["offset"] + len(block), "prefix_sha256": digest.hexdigest()})
                    ledger.persist(data)
                    with lock:
                        first_checkpoint = meter.confirmed == offset
                        meter.confirmed = data["offset"]
                        meter.emit("checkpoint", force=first_checkpoint)
                    del block, future
                    if start < expected:
                        end = min(start + chunk, expected) - 1
                        pending.append(pool.submit(fetch, start, end))
                        start = end + 1
            return True
        except BaseException:
            cancel.set()
            if errors:
                raise errors[0]
            raise
        finally:
            cancel.set()
            pool.shutdown(wait=True, cancel_futures=True)
            self.client.release_transfer_clients(clients)

    def _download_unversioned(self, response, fid, target, expected, length, overwrite, ledger, data,
                              meter, scope, should_stop):
        """A fresh single response may finish, but cannot create a resume prefix."""
        part = ledger.part_path
        if not part.exists():
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
        private_path(part)
        written = 0
        digest = hashlib.sha256()
        meter.emit("non_resumable", force=True)
        try:
            with part.open("wb") as sink:
                chunks = response.iter_bytes if response.is_stream_consumed else response.iter_raw
                for block in chunks(meter.limiter.chunk_size):
                    if should_stop is not None and should_stop():
                        raise DownloadStopped("下载已停止：该对象不支持安全续传，需重新下载", resume_available=False)
                    if (length is not None and written + len(block) > length) or written + len(block) > expected:
                        raise RemoteError("下载正文超过声明长度")
                    meter.limiter.consume(len(block))
                    if sink.write(block) != len(block):
                        raise LocalIOError("下载临时文件写入不完整")
                    digest.update(block)
                    written += len(block)
                    meter.transferred = written
                    meter.emit("non_resumable")
                sink.flush()
                os.fsync(sink.fileno())
            if written != expected or (length is not None and written != length):
                raise RemoteError("下载正文与声明长度不符")
            if _remote_identity(Files(self.client).info(fid)) != data["remote"]:
                raise RemoteError("传输期间远端文件身份变化；未发布")
            self._scope(fid, scope, data["scope_fid"])
            _publish(part, target, overwrite)
            # Only the empty, task-owned ledger exists; no offset was promised.
            ledger.path.unlink()
            meter.confirmed = written
            meter.emit("completed", force=True)
            result = _download_result(fid, target, written, True, False, digest.hexdigest())
            result["resumable"] = False
            return result
        finally:
            part.unlink(missing_ok=True)

    def download(self, fid: str, output: str | Path, *, overwrite: bool = False, resume: bool = False,
                 limit_rate: int | None = None, progress: Callable[[ProgressEvent], None] | None = None,
                 scope: Scope | None = None, limiter: RateLimiter | None = None,
                 connections: int = DEFAULT_DOWNLOAD_CONNECTIONS,
                 should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Stream one file to ``output`` and publish it atomically.

        ``should_stop`` is an optional caller hook, asked before the first byte
        and before every chunk of the body: a true answer raises
        :class:`DownloadStopped`, leaving the resumed state exactly at the bytes
        the ledger has already confirmed. A body that is already complete is
        published rather than reported as stopped. The hook is honored only with
        ``resume=True``, where that durable checkpoint exists.
        """
        fid = valid_fid(fid)
        if fid == "0":
            raise UsageError("FID 无效")
        if type(connections) is not int or not 1 <= connections <= MAX_DOWNLOAD_CONNECTIONS:
            raise UsageError(f"下载并发连接数必须是 1 到 {MAX_DOWNLOAD_CONNECTIONS} 之间的整数")
        if should_stop is not None and not resume:
            raise UsageError("停止下载需要 resume=True 的持久化检查点")
        if should_stop is not None and should_stop():
            raise DownloadStopped("下载已停止：尚未传输任何数据")
        target = safe_local_path(output)
        if not target.parent.is_dir():
            raise LocalIOError("下载目标目录不存在")
        pacing = _limiter(limit_rate, limiter)
        ledger = DownloadLedger({"fid": fid, "output": str(target)}, directory=self.state_dir)
        with ledger.lock:
            data = ledger.load() if resume else None
            resumed = data is not None
            self._scope(fid, scope, data["scope_fid"] if data else None)
            remote = _remote_identity(Files(self.client).info(fid)) if resume else None
            if data is not None and data["remote"] != remote:
                raise RemoteError("远端文件身份或版本已变化，拒绝恢复")
            published = data is not None and data["phase"] in ("publishing", "completed") and target.exists()
            if target.exists() and not overwrite and not published:
                raise UsageError("目标文件已存在；使用 --overwrite 显式覆盖")
            if target.exists() and not target.is_file():
                raise UsageError("下载目标不是普通文件")
            if resume and data is None:
                if ledger.part_path.exists() or ledger.part_path.is_symlink():
                    raise LocalIOError("已有 part 没有匹配账本，不能接管")
                now = time.time()
                data = {"version": 1, "direction": "download", "phase": "prepared", "identity": ledger.identity,
                        "scope_fid": scope.fid if scope else None, "remote": remote, "etag": None, "offset": 0,
                        "prefix_sha256": hashlib.sha256(b"").hexdigest(), "payload_sha256": None,
                        "created_at": now, "updated_at": now}
                ledger.persist(data)
            url, expected = self._download_address(fid)
            if remote is not None:
                if expected is not None and expected != remote["size"]:
                    raise RemoteError("下载地址大小与文件详情不一致")
                expected = remote["size"]
            if not resume and connections > 1:
                parallel_result = self._download_parallel(fid, url, target, expected, overwrite=overwrite,
                                                           connections=connections, limiter=pacing, progress=progress)
                if parallel_result is not None:
                    return parallel_result
            offset = data["offset"] if data else 0
            digest = hashlib.sha256()
            temporary: Path | None = ledger.part_path if resume else None
            if resume and (published or offset or temporary.exists()):
                candidate = target if published else safe_local_path(temporary)
                if not candidate.is_file():
                    raise LocalIOError("下载检查点数据文件缺失")
                if resume and not published:
                    private_path(candidate)
                try:
                    if candidate.stat().st_size < offset:
                        raise LocalIOError("part 比已确认检查点短")
                    with candidate.open("rb") as stream:
                        remaining = offset
                        while remaining:
                            block = stream.read(min(STREAM_CHUNK_SIZE, remaining))
                            if not block:
                                raise LocalIOError("下载检查点被截断")
                            digest.update(block)
                            remaining -= len(block)
                    if digest.hexdigest() != data["prefix_sha256"]:
                        raise LocalIOError("part 已确认前缀哈希不符")
                    if published and candidate.stat().st_size != expected:
                        raise LocalIOError("已发布文件大小不符")
                    if published and digest.hexdigest() != data["payload_sha256"]:
                        raise LocalIOError("已发布文件完整哈希不符")
                    if not published and candidate.stat().st_size > offset:
                        with candidate.open("r+b") as stream:
                            stream.truncate(offset)
                            stream.flush()
                            os.fsync(stream.fileno())
                except OSError:
                    raise LocalIOError("无法核验下载检查点") from None
            headers = {"Accept-Encoding": "identity"}
            if offset:
                if data["etag"] is None:
                    raise RemoteError("检查点没有强 ETag，不能安全续传")
                headers.update({"Range": "bytes=0-0" if published and expected else f"bytes={offset}-", "If-Range": data["etag"]})
            meter = Progress("download", target.name, expected, pacing, progress)
            meter.transferred = meter.confirmed = offset
            written = offset
            try:
                if resume and not published and self._download_checkpointed_ranges(
                    url, expected, temporary, data, ledger, digest, meter, connections, should_stop
                ):
                    fresh = _remote_identity(Files(self.client).info(fid))
                    self._scope(fid, scope, data["scope_fid"])
                    if fresh != data["remote"]:
                        raise RemoteError("传输期间远端文件身份变化；未发布")
                    data.update({"phase": "payload_verified", "payload_sha256": digest.hexdigest()})
                    ledger.persist(data)
                    data["phase"] = "publishing"
                    ledger.persist(data)
                    _publish(temporary, target, overwrite)
                    data["phase"] = "completed"
                    ledger.persist(data)
                    meter.emit("completed", force=True)
                    return _download_result(fid, target, data["offset"], True, resumed, digest.hexdigest())
                with self._download_stream(url, headers) as response:
                    etag = _strong_etag(response.headers.get("etag"))
                    if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                        raise RemoteError("下载响应未采用 identity 编码，不能按字节恢复")
                    if published:
                        if expected:
                            _range(response, 0, expected, bounded_end=0)
                        elif response.status_code != 200:
                            raise RemoteError("空文件身份不能确认")
                        if etag != data["etag"]:
                            raise RemoteError("已发布下载的远端强 ETag 已变化")
                        if temporary.exists():
                            remainder = safe_local_path(temporary)
                            private_path(remainder)
                            if remainder.stat().st_size != offset:
                                raise LocalIOError("发布残留 part 大小不符，未清理")
                            remaining_digest = hashlib.sha256()
                            with remainder.open("rb") as stream:
                                while block := stream.read(STREAM_CHUNK_SIZE):
                                    remaining_digest.update(block)
                            if remaining_digest.hexdigest() != data["payload_sha256"]:
                                raise LocalIOError("发布残留 part 哈希不符，未清理")
                            remainder.unlink()
                        data["phase"] = "completed"
                        ledger.persist(data)
                        meter.emit("completed", force=True)
                        return _download_result(fid, target, offset, resume, True, data["payload_sha256"])
                    if response.status_code == 416:
                        total = _unsatisfied_range(response)
                        if data is None or data["phase"] != "payload_verified" or total != offset or total != expected or etag != data["etag"] or digest.hexdigest() != data["payload_sha256"]:
                            raise RemoteError("416 不构成完整下载证据；已保留检查点")
                    else:
                        if response.status_code == 206:
                            total, length = _range(response, offset, expected)
                            if expected is None:
                                expected = total
                                meter.total = expected
                        elif response.status_code == 200 and offset == 0:
                            length = _content_length(response)
                        elif response.status_code == 200:
                            raise RemoteError("服务器忽略 Range 或文件已变化；未追加且已保留 part")
                        else:
                            raise RemoteError(f"下载失败（HTTP {response.status_code}）")
                        if resume:
                            if etag is None:
                                if offset == 0 and data["etag"] is None:
                                    return self._download_unversioned(response, fid, target, expected, length,
                                                                      overwrite, ledger, data, meter, scope, should_stop)
                                raise RemoteError("该对象没有强 ETag，无法安全建立续传检查点")
                            if data["etag"] is not None and etag != data["etag"]:
                                raise RemoteError("远端强 ETag 已变化；未追加")
                            data["etag"] = etag
                            data["phase"] = "streaming"
                            ledger.persist(data)
                        received = 0
                        if not resume:
                            descriptor, filename = tempfile.mkstemp(prefix=".quark-download-", dir=target.parent)
                            os.close(descriptor)
                            temporary = Path(filename)
                        elif not temporary.exists():
                            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                            os.close(descriptor)
                            private_path(temporary)
                        with temporary.open("ab" if offset else "wb") as sink:
                            chunks = response.iter_bytes if response.is_stream_consumed else response.iter_raw
                            for block in chunks(pacing.chunk_size):
                                if not block:
                                    continue
                                if (length is not None and received + len(block) > length) or (expected is not None and written + len(block) > expected):
                                    raise RemoteError("下载正文超过声明长度；检查点已保留")
                                if should_stop is not None and should_stop():
                                    # The part file holds exactly the prefix the ledger
                                    # has confirmed and this block is not written yet,
                                    # so ending here is an ordinary resumable prefix —
                                    # and a body that is already complete is published
                                    # instead of being reported as stopped.
                                    raise DownloadStopped("下载已停止：已保留安全检查点，再次提交可继续")
                                pacing.consume(len(block))
                                sink.write(block)
                                digest.update(block)
                                written += len(block)
                                received += len(block)
                                meter.transferred = written
                                if resume:
                                    sink.flush()
                                    os.fsync(sink.fileno())
                                    data.update({"offset": written, "prefix_sha256": digest.hexdigest()})
                                    ledger.persist(data)
                                    meter.confirmed = written
                                meter.emit("transferring")
                            sink.flush()
                            os.fsync(sink.fileno())
                        if length is not None and received != length:
                            raise RemoteError("下载正文与 HTTP 长度不符；检查点已保留")
                        if expected is not None and written != expected:
                            raise RemoteError("下载正文与网盘大小不符；检查点已保留")
                    if resume:
                        fresh = _remote_identity(Files(self.client).info(fid))
                        self._scope(fid, scope, data["scope_fid"])
                        if fresh != data["remote"]:
                            raise RemoteError("传输期间远端文件身份变化；未发布")
                        data.update({"phase": "payload_verified", "payload_sha256": digest.hexdigest()})
                        ledger.persist(data)
                if resume:
                    data["phase"] = "publishing"
                    ledger.persist(data)
                _publish(temporary, target, overwrite)
                temporary = None
                if resume:
                    data["phase"] = "completed"
                    ledger.persist(data)
                meter.transferred = meter.confirmed = written
                meter.emit("completed", force=True)
                return _download_result(fid, target, written, resume, resumed, digest.hexdigest())
            except QuarkError:
                raise
            except OSError:
                raise LocalIOError("无法写入或发布下载文件；检查点已保留") from None
            finally:
                if temporary is not None and not resume:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass


def safe_transfer_name(name: str) -> str:
    validate_name(name)
    if any(char in '<>:"|?*' for char in name) or name.endswith((".", " ")) or re.fullmatch(r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", name):
        raise UsageError("传输名称不兼容安全的 Windows 路径")
    return name


def _limiter(rate: int | None, limiter: RateLimiter | None) -> RateLimiter:
    if limiter is not None:
        if not isinstance(limiter, RateLimiter) or (rate is not None and rate != limiter.bytes_per_second):
            raise UsageError("共享限速器与 limit-rate 不一致")
        return limiter
    return RateLimiter(rate)


def _remote_identity(item: dict[str, Any]) -> dict[str, Any]:
    if is_directory(item) or not (item.get("file_type") == 1 or item.get("dir") is False) or type(item.get("size")) is not int or item["size"] < 0:
        raise RemoteError("远端对象不是具有已知大小的普通文件")
    result = {"fid": valid_fid(item.get("fid")), "name": validate_name(item.get("file_name")),
              "parent": valid_fid(item.get("pdir_fid")), "size": item["size"]}
    version = item.get("updated_at")
    if version is not None:
        if not valid_version(version):
            raise RemoteError("远端文件版本字段无效")
        result["updated_at"] = version
    return result


def _etag_md5(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if re.fullmatch(r"[0-9a-fA-F]{32}", value) is None:
        raise RemoteError("分片 ETag 不是可核验的 md5，不能安全恢复")
    return value


def _xml(body: bytes) -> ET.Element:
    if len(body) > 4 * 1024 * 1024 or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise RemoteError("对象存储 XML 响应不安全或过大")
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise RemoteError("对象存储 XML 响应格式无效") from None


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _xml_text(root: ET.Element, name: str) -> str:
    matches = [element for element in root if _tag(element) == name]
    if len(matches) != 1 or matches[0].text is None:
        raise RemoteError("对象存储 XML 响应缺少必要字段")
    return matches[0].text


def _decimal(value: str, label: str) -> int:
    if re.fullmatch(r"[0-9]+", value) is None:
        raise RemoteError(f"{label}无效")
    return int(value)


def _check_merge(response: httpx.Response) -> None:
    if response.status_code not in (200, 203):
        raise RemoteError(f"对象存储合并失败（HTTP {response.status_code}）；受理状态已保留")
    if response.status_code == 203:
        return
    body = response.content.strip()
    if not body:
        raise RemoteError("合并响应缺少完成证据；受理状态未知")
    if body.startswith(b"<"):
        root = _xml(body)
        if _tag(root) != "CompleteMultipartUploadResult":
            raise RemoteError("对象存储未明确完成合并")
    else:
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            raise RemoteError("合并回调响应格式无效") from None
        if isinstance(data, dict) and set(data) == {"Status"} and data["Status"] == "OK":
            return  # OSS forwards the callback application's JSON response.
        if not isinstance(data, dict) or type(data.get("status")) is not int or data["status"] != 200 or ("code" in data and (isinstance(data["code"], bool) or data["code"] not in (0, "0"))):
            raise RemoteError("合并回调没有明确成功状态")


def _strong_etag(value: str | None) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r'"[\x21\x23-\x7e]*"', value) is not None else None


def _content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    return _decimal(value, "Content-Length") if value is not None else None


def _range(response: httpx.Response, offset: int, expected: int | None, *, bounded_end: int | None = None) -> tuple[int, int]:
    if response.status_code != 206 or response.headers.get("content-type", "").lower().startswith("multipart/"):
        raise RemoteError("续传响应不是单一有效的 206")
    match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", response.headers.get("content-range", ""))
    if match is None:
        raise RemoteError("续传 Content-Range 无效")
    start, end, total = map(int, match.groups())
    if start != offset or end < start or end >= total or (expected is not None and total != expected) or (bounded_end is not None and end != bounded_end):
        raise RemoteError("续传范围与原文件不符")
    length = end - start + 1
    declared = _content_length(response)
    if declared is not None and declared != length:
        raise RemoteError("续传 Content-Length 与范围不符")
    return total, length


def _unsatisfied_range(response: httpx.Response) -> int:
    match = re.fullmatch(r"bytes \*/([0-9]+)", response.headers.get("content-range", ""))
    if match is None:
        raise RemoteError("416 Content-Range 无效")
    return int(match.group(1))


def _publish(part: Path, target: Path, overwrite: bool) -> None:
    safe_local_path(target)
    if overwrite:
        os.replace(part, target)
    elif os.name == "nt":
        try:
            os.rename(part, target)
        except FileExistsError:
            raise UsageError("目标在下载期间被创建，未覆盖") from None
    else:
        try:
            os.link(part, target)
        except FileExistsError:
            raise UsageError("目标在下载期间被创建，未覆盖") from None
        part.unlink()
    if os.name != "nt":
        descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _download_result(fid: str, target: Path, size: int, resume: bool, resumed: bool, digest: str) -> dict[str, Any]:
    result: dict[str, Any] = {"fid": fid, "path": str(target), "size": size}
    if resume:
        result.update({"resumed": resumed, "sha256": digest})
    return result
