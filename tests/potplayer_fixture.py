"""In-memory Quark API plus signed-origin stand-in for PotPlayer bridge tests."""

from __future__ import annotations

import json

import httpx

VIDEO_BYTES = bytes(range(256)) * 8
API_HOST = "drive-pc.quark.cn"
ORIGIN_HOST = "origin.pds.quark.cn"
COOKIE = "b=1; __puus=secret"

# A 4096-byte period makes a virtual object's every byte cheap to generate.
_PATTERN = bytes(range(256)) * 16
SEGMENT_LIMIT = 4 * 1024 * 1024


def consuming_clock(service):
    """Protocol clients consume bytes immediately; native monitor stays real.

    Buffer policy tests instead drive real monitor samples and never use this
    stand-in. It changes only the proxy's read-ahead view, not saved progress.
    """
    actual_state = service.monitor.buffer_state

    def state(session_id):
        current = actual_state(session_id)
        if current is not None and current['state'] == 'stopped':
            return current
        media = service.sessions.get(session_id)
        if media is None:
            return None
        with media.buffer_budget.lock:
            head = max([media.buffer_budget.head, *(end for _, end in media.buffer_budget.ranges)])
        return {'state': 'playing', 'position_ms': head, 'duration_ms': media.size}

    return state


def virtual_slice(start: int, length: int) -> bytes:
    """The virtual object's bytes at ``[start, start + length)`` without materializing it."""
    if length <= 0:
        return b""
    offset = start % len(_PATTERN)
    repeats = (offset + length + len(_PATTERN) - 1) // len(_PATTERN)
    return (_PATTERN * repeats)[offset:offset + length]


class _ByteStream(httpx.SyncByteStream):
    """Deliver bytes as a real stream so the proxy's ``iter_raw`` reads them."""

    def __init__(self, data: bytes, chunk_size: int = 4096) -> None:
        self._data = data
        self._chunk_size = chunk_size

    def __iter__(self):
        for index in range(0, len(self._data), self._chunk_size):
            yield self._data[index:index + self._chunk_size]


class _VirtualStream(httpx.SyncByteStream):
    """Stream a generated range so a large virtual object never uses real memory."""

    def __init__(self, start: int, length: int, chunk_size: int = 64 * 1024) -> None:
        self._start = start
        self._length = length
        self._chunk_size = chunk_size

    def __iter__(self):
        for offset in range(0, self._length, self._chunk_size):
            size = min(self._chunk_size, self._length - offset)
            yield virtual_slice(self._start + offset, size)


def record(fid, name, parent="0", *, directory=False, size=None, **fields):
    entry = {
        "fid": fid,
        "file_name": name,
        "pdir_fid": parent,
        "file_type": 0 if directory else 1,
        "dir": directory,
        **fields,
    }
    if not directory:
        entry["size"] = len(VIDEO_BYTES) if size is None else size
    return entry


def default_records():
    return [
        record("root-dir", "剧集", directory=True),
        record("empty-dir", "空目录", directory=True),
        record("v10", "第10集.mp4", "root-dir"),
        record("v2", "第2集.mp4", "root-dir"),
        record("v9", "第9集.mkv", "root-dir"),
        record("poster", "封面.jpg", "root-dir", size=32),
        record("notes", "说明.txt", "root-dir", size=8),
    ]


class DriveFixture:
    """Serves ``file/sort``, ``file/info``, ``file/download`` and ranged origin bytes."""

    def __init__(self, records=None):
        self.records = default_records() if records is None else records
        self.requests: list[httpx.Request] = []
        self.bodies: dict[str, bytes] = {}
        self.download_calls = 0
        self.origin_epoch = 0
        self.origin_failures = 0
        self.announced_size: int | None = None
        self.omit_download_name = False
        # Fault injection for the account API and the signed origin.
        self.api_unauthorized = False
        self.origin_status: int | None = None
        self.origin_encoding: str | None = None
        self.origin_etag: str | None = None
        self.ignore_range = False
        self.content_range_override: str | None = None
        self.content_length_override: int | None = None
        # Virtual large objects: fid -> size, streamed from a generated pattern.
        self.virtual: dict[str, int] = {}
        self.segment_limit: int | None = None
        self.over_limit: list[int] = []
        self.origin_ordinal = 0
        self.truncate_at: int | None = None
        self.switch_etag_at: int | None = None
        self.etag_after: str | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == API_HOST:
            return self._api(request)
        return self._origin(request)

    def _data(self, request: httpx.Request, data) -> httpx.Response:
        return httpx.Response(200, json={"status": 200, "code": 0, "data": data}, request=request)

    def _find(self, fid: str):
        return next((row for row in self.records if row["fid"] == fid), None)

    def _api(self, request: httpx.Request) -> httpx.Response:
        if self.api_unauthorized:
            return httpx.Response(401, json={"status": 401, "message": "登录已失效"}, request=request)
        path = request.url.path
        if path.endswith("/file/sort"):
            parent = request.url.params["pdir_fid"]
            page, size = int(request.url.params["_page"]), int(request.url.params["_size"])
            known = (
                {row.get("pdir_fid") for row in self.records}
                | {row["fid"] for row in self.records if row.get("dir")}
                | {"0"}
            )
            if parent not in known:
                return httpx.Response(
                    200, json={"status": 400, "code": 0, "message": "目录不存在"}, request=request
                )
            children = [row for row in self.records if row.get("pdir_fid") == parent]
            return self._data(request, {"list": children[(page - 1) * size: page * size], "_total": len(children)})
        if path.endswith("/file/search"):
            keyword = request.url.params.get("q", "")
            page, size = int(request.url.params["_page"]), int(request.url.params["_size"])
            matched = [row for row in self.records if keyword in str(row.get("file_name", ""))]
            # The real endpoint sorts by file_type:asc,file_name:asc: directories first.
            matched.sort(key=lambda row: (row.get("file_type", 1), str(row.get("file_name", ""))))
            return self._data(request, {
                "list": matched[(page - 1) * size: page * size],
                "_total": len(matched),
            })
        if path.endswith("/file/info"):
            return self._data(request, self._find(request.url.params["fid"]) or {})
        if path.endswith("/file/download"):
            body = json.loads(request.read())
            fid = body["fids"][0]
            found = self._find(fid) or {}
            self.download_calls += 1
            if found.get("dir"):
                return self._data(request, {"list": []})
            address = {"fid": fid, "download_url": f"https://{ORIGIN_HOST}/{fid}?epoch={self.origin_epoch}"}
            if self.announced_size is not None:
                address["size"] = self.announced_size
            elif "size" in found:
                address["size"] = found["size"]
            if not self.omit_download_name and "file_name" in found:
                address["file_name"] = found["file_name"]
            return self._data(request, {"list": [address]})
        return httpx.Response(404, request=request)

    def _origin(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != ORIGIN_HOST:
            return httpx.Response(404, request=request)
        if self.origin_status is not None:
            return httpx.Response(self.origin_status, text="origin error", request=request)
        if self.origin_failures > 0:
            self.origin_failures -= 1
            return httpx.Response(403, text="expired", request=request)
        if request.url.params.get("epoch") != str(self.origin_epoch):
            return httpx.Response(403, text="expired", request=request)
        self.origin_ordinal += 1
        name = request.url.path.lstrip("/")
        if name in self.virtual:
            return self._virtual(request, self.virtual[name])
        body = self.bodies.get(name, VIDEO_BYTES)
        base = {}
        if self.origin_etag is not None:
            base["ETag"] = self.origin_etag
        if self.origin_encoding is not None:
            base["Content-Encoding"] = self.origin_encoding
        header = request.headers.get("range")
        if self.ignore_range or not header:
            announced = len(body) if self.content_length_override is None else self.content_length_override
            return httpx.Response(
                200,
                stream=_ByteStream(body),
                headers={"Content-Length": str(announced), **base},
                request=request,
            )
        first, _, last = header[6:].partition("-")
        start = int(first) if first else max(0, len(body) - int(last))
        end = min(int(last), len(body) - 1) if last else len(body) - 1
        payload = body[start:end + 1]
        content_range = self.content_range_override or f"bytes {start}-{end}/{len(body)}"
        if self.content_length_override is not None:
            content_length = self.content_length_override
        else:
            content_length = len(payload)
        return httpx.Response(
            206,
            stream=_ByteStream(payload),
            headers={
                "Content-Range": content_range,
                "Content-Length": str(content_length),
                **base,
            },
            request=request,
        )

    def _virtual(self, request: httpx.Request, total: int) -> httpx.Response:
        """Serve a generated large object, refusing any request that exceeds the limit."""
        header = request.headers.get("range")
        if header is None:
            start, end = 0, total - 1
        else:
            first, _, last = header[6:].partition("-")
            start = int(first) if first else max(0, total - int(last))
            end = min(int(last), total - 1) if last else total - 1
        length = end - start + 1
        if self.segment_limit is not None and length > self.segment_limit:
            self.over_limit.append(length)
            return httpx.Response(413, text="range too large", request=request)
        etag = self.origin_etag
        if self.switch_etag_at is not None and self.origin_ordinal >= self.switch_etag_at:
            etag = self.etag_after
        base = {}
        if etag is not None:
            base["ETag"] = etag
        if self.origin_encoding is not None:
            base["Content-Encoding"] = self.origin_encoding
        served = length
        if self.truncate_at == self.origin_ordinal:
            served = max(1, length // 2)
        if header is None:
            return httpx.Response(
                200,
                stream=_VirtualStream(start, served),
                headers={"Content-Length": str(length), **base},
                request=request,
            )
        return httpx.Response(
            206,
            stream=_VirtualStream(start, served),
            headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Content-Length": str(length),
                **base,
            },
            request=request,
        )

    def origin_requests(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.host == ORIGIN_HOST]
