"""The loopback HTTP bridge that the PotPlayer AngelScript extensions call.

The service binds ``127.0.0.1`` only, keeps one instance per user, and never
hands the player a Cookie or a signed Quark URL: control endpoints require the
runtime Bearer token, and playback URLs carry an unguessable session id.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from ..auth import LoginCancelled
from ..client import QuarkClient, default_guard
from ..errors import (
    AccessRestrictedError,
    AuthenticationError,
    LocalIOError,
    NetworkError,
    QuarkError,
    RateLimitError,
    RemoteError,
    UsageError,
)
from ..files import Files
from ..paths import is_directory
from ..request_guard import RequestGuard
from . import SERVICE_VERSION
from .library import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, page, playlist, require_fid, search
from .library_state import Library
from .login_gui import QrLoginWindow
from .naming import content_type
from .plugin_state import (
    acquire_service_lock,
    clear_cookie,
    clear_runtime,
    credential_path,
    load_cookie,
    read_runtime,
    write_runtime,
)
from .stream import (
    MediaChanged,
    PlaySession,
    RangeNotSatisfiable,
    Sessions,
    content_range,
    open_content,
    resolve,
)
from .subtitles import UnknownSession, match_subtitles, read_subtitle

HOST = "127.0.0.1"
HEALTH_TIMEOUT = 2.0
START_TIMEOUT = 20.0
MAX_BODY = 64 * 1024
CHUNK_SIZE = 64 * 1024
# Quark throttles a single large media GET (measured ~0.1 MiB/s for an
# open-ended request versus ~5 MiB/s at 4 MiB), so a bigger ask is split.
MEDIA_SEGMENT = 4 * 1024 * 1024
MAX_PAGE_NUMBER = 1_000_000
EXIT_OK = 0
EXIT_ALREADY_RUNNING = 3
EXIT_UNAVAILABLE = 5

_CONTROL_ROUTES = frozenset({
    "/v1/auth", "/v1/auth/login", "/v1/auth/logout",
    "/v1/files", "/v1/playlist", "/v1/play", "/v1/shutdown",
    "/v1/search", "/v1/favorites", "/v1/favorites/remove",
    "/v1/recent", "/v1/recent/clear",
    "/v1/subtitles", "/v1/subtitle",
})


def _failure(kind: str, message: str) -> dict[str, Any]:
    return {"error": {"type": kind, "message": message}}


def _status_for(exc: BaseException) -> tuple[int, str]:
    if isinstance(exc, AuthenticationError):
        return 401, "login_required"
    # Typed cooldown errors are never credential failures: the plugin must not
    # clear its Cookie, library or login state for them.
    if isinstance(exc, RateLimitError):
        return 429, "rate_limited"
    if isinstance(exc, AccessRestrictedError):
        return 403, "access_restricted"
    if isinstance(exc, MediaChanged):
        return 409, "media_changed"
    if isinstance(exc, NetworkError):
        return 504, "network_error"
    if isinstance(exc, RemoteError):
        return 502, "remote_error"
    if isinstance(exc, UsageError):
        return 400, "bad_request"
    if isinstance(exc, LocalIOError):
        return 500, "local_io_error"
    return 500, "internal_error"


def _first(query: Mapping[str, list[str]], key: str, default: str | None) -> str | None:
    values = query.get(key)
    if not values or values[0] == "":
        return default
    return values[0]


def _int_param(query: Mapping[str, list[str]], key: str, default: int, minimum: int, maximum: int) -> int:
    raw = _first(query, key, None)
    if raw is None:
        return default
    if not raw.isdigit():
        raise UsageError(f"{key} 必须是整数")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise UsageError(f"{key} 允许范围是 {minimum} 到 {maximum}")
    return value


def _content_range_header(value: str | None) -> tuple[int, int, int] | None:
    if not value or not value.casefold().startswith("bytes "):
        return None
    span, separator, total = value[6:].strip().partition("/")
    if not separator:
        return None
    start, dash, end = span.partition("-")
    if not dash or not start.isdigit() or not end.isdigit() or not total.isdigit():
        return None
    return int(start), int(end), int(total)


def _upstream_plan(
    response: httpx.Response,
    session: PlaySession,
    byte_range: tuple[int, int] | None,
) -> tuple[int, int, str | None]:
    """Validate what the origin sent against the session before any header goes out."""
    if response.status_code not in (200, 206):
        raise RemoteError(f"下载源返回了异常状态 {response.status_code}，已中止播放")
    encoding = response.headers.get("content-encoding")
    if encoding and encoding.casefold() != "identity":
        raise RemoteError("下载源启用了压缩编码，已中止播放")
    announced = response.headers.get("content-length")
    announced_length = int(announced) if announced is not None and announced.isdigit() else None
    if response.status_code == 206:
        parsed = _content_range_header(response.headers.get("content-range"))
        if parsed is None:
            raise MediaChanged("夸克未返回可校验的范围信息，已中止播放")
        start, end, total = parsed
        if start > end or end >= total:
            raise RemoteError("下载源返回的范围信息无效，已中止播放")
        if total != session.size or (byte_range is not None and (start, end) != byte_range):
            raise MediaChanged("文件大小或版本已变化，已中止播放")
        length = end - start + 1
        if announced_length is not None and announced_length != length:
            raise RemoteError("下载源返回的长度与范围不一致，已中止播放")
        return 206, length, f"bytes {start}-{end}/{total}"
    if byte_range is not None:
        raise RemoteError("下载源忽略了分段请求，已中止播放")
    if announced_length is not None and announced_length != session.size:
        raise MediaChanged("文件大小或版本已变化，已中止播放")
    return 200, session.size, None


class Service:
    """Owns the instance token, the Quark session, login state, and play sessions."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        cookie: str | None = None,
        login_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        guard: RequestGuard | None = None,
    ) -> None:
        self.instance = secrets.token_hex(8)
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self.server: ThreadingHTTPServer | None = None
        self.sessions = Sessions(clock)
        self.library = Library()
        self._transport = transport
        self._cookie = cookie
        # One guard is shared by the bridge, its Quark client and the QR login it
        # starts, so a cooldown survives client rebuilds within this process.
        self._guard = guard if guard is not None else default_guard(transport, None)
        self._login_factory = login_factory or (
            lambda: QrLoginWindow(transport=transport, cookie_path=credential_path(), guard=self._guard)
        )
        self._client: QuarkClient | None = None
        self._client_cookie: str | None = None
        self._client_lock = threading.Lock()
        self._login_lock = threading.Lock()
        self._login_thread: threading.Thread | None = None
        self._login_window: QrLoginWindow | None = None
        self.login_state = "idle"
        self.login_message: str | None = None
        self.qr_png: str | None = None

    # -- Quark session ---------------------------------------------------

    def authorized(self, header: str | None) -> bool:
        if not header:
            return False
        scheme, _, value = header.partition(" ")
        return scheme.casefold() == "bearer" and hmac.compare_digest(value.strip(), self.token)

    def client(self) -> QuarkClient:
        """Reuse the Quark session, rebuilding it when the saved credential changed."""
        cookie = self._cookie or load_cookie()
        if not cookie:
            raise AuthenticationError("插件尚未登录夸克网盘，请先扫码登录")
        with self._client_lock:
            if self._client is None or self._client_cookie != cookie:
                if self._client is not None:
                    self._client.close()
                self._client = QuarkClient(cookie, transport=self._transport, guard=self._guard)
                self._client_cookie = cookie
            return self._client

    def files(self) -> Files:
        return Files(self.client())

    def require_login(self) -> None:
        """Reject a control call whose data belongs to a signed-in plugin."""
        if not self.authenticated():
            raise AuthenticationError("插件尚未登录夸克网盘，请先扫码登录")

    def drop_client(self) -> None:
        with self._client_lock:
            client, self._client = self._client, None
            self._client_cookie = None
        if client is not None:
            client.close()

    def close(self) -> None:
        """Stop the bridge: close a QR window still on screen, then drop the client."""
        with self._login_lock:
            window, self._login_window = self._login_window, None
        if isinstance(window, QrLoginWindow):
            window.request_close()
        self.drop_client()

    def authenticated(self) -> bool:
        if self._cookie:
            return True
        try:
            return load_cookie() is not None
        except LocalIOError:
            return False

    # -- login -----------------------------------------------------------

    def _login_payload_locked(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated(),
            "login_state": self.login_state,
            "login_message": self.login_message,
            "qr_png": self.qr_png,
        }

    def login_payload(self) -> dict[str, Any]:
        with self._login_lock:
            return self._login_payload_locked()

    def start_login(self) -> dict[str, Any]:
        """Start one QR login; a repeated call while it runs reports the same state."""
        # Refuse before any GUI is created: a shared cooldown means the account
        # or upstream is currently rejecting requests, not that login is broken.
        self._guard.check_active_cooldown()
        with self._login_lock:
            if self._login_thread is not None and self._login_thread.is_alive():
                return self._login_payload_locked()
            self.login_state = "waiting"
            self.login_message = None
            self.qr_png = None
            thread = threading.Thread(target=self._run_login, name="quark-potplayer-login", daemon=True)
            self._login_thread = thread
        thread.start()
        return self.login_payload()

    def _run_login(self) -> None:
        state, message = "failed", None
        try:
            login = self._login_factory()
            with self._login_lock:
                self._login_window = login
            login.run(on_qr_ready=self._qr_ready)
        except LoginCancelled:
            state = "idle"
        except QuarkError as exc:
            message = str(exc) or "扫码登录失败"
        except OSError:
            message = "扫码登录过程失败"
        else:
            state = "success"
        with self._login_lock:
            self.login_state = state
            self.login_message = message
            self.qr_png = None
            self._login_thread = None
            self._login_window = None
        if state == "success":
            self.drop_client()

    def _qr_ready(self, path: Any) -> None:
        with self._login_lock:
            self.qr_png = str(path)

    def invalidate_credential(self) -> None:
        """Forget a plugin credential the account API proved stale.

        Covers both the in-process injected Cookie and the saved plugin file, so
        ``authenticated`` turns false and the extension re-opens the QR login.
        Only the bridge's own credential is removed; the ``quark`` CLI Cookie is
        never touched.
        """
        self._cookie = None
        self.drop_client()
        try:
            clear_cookie()
        except LocalIOError:
            pass

    def logout(self) -> dict[str, Any]:
        """Clear only the bridge's own credential; the CLI Cookie stays untouched.

        An explicit logout also drops the favorites and recent lists; a merely
        expired credential (``invalidate_credential``) keeps them.
        """
        self.invalidate_credential()
        self.library.clear()
        with self._login_lock:
            self.login_state = "idle"
            self.login_message = None
            self.qr_png = None
        return {"authenticated": False}

    # -- lifecycle -------------------------------------------------------

    def build_server(self) -> ThreadingHTTPServer:
        server = _Server((HOST, 0), _Handler, self)
        self.server = server
        self.port = int(server.server_address[1])
        return server

    def stop(self) -> None:
        server = self.server
        if server is None:
            return
        threading.Thread(target=server.shutdown, name="quark-potplayer-stop", daemon=True).start()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], service: Service) -> None:
        self.service = service
        super().__init__(address, handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"quark-potplayer/{SERVICE_VERSION}"
    sys_version = ""

    @property
    def service(self) -> Service:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        """Stay silent: request lines can contain session ids and file names."""

    def do_GET(self) -> None:  # noqa: N802 - http.server dispatch name
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    # -- routing ---------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        raw = self._consume_body()
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            return self._json(400, _failure("bad_request", "请求地址无效"))
        route, query = parsed.path, parse_qs(parsed.query, keep_blank_values=True)
        try:
            if route == "/v1/health":
                if method not in ("GET", "HEAD"):
                    return self._json(405, _failure("method_not_allowed", "该接口只接受 GET 或 HEAD"))
                return self._json(200, {
                    "status": "ok",
                    "version": SERVICE_VERSION,
                    "instance": self.service.instance,
                })
            if route.startswith("/v1/stream/"):
                if method not in ("GET", "HEAD"):
                    return self._json(405, _failure("method_not_allowed", "该接口只接受 GET 或 HEAD"))
                return self._stream(route)
            if not self.service.authorized(self.headers.get("Authorization")):
                return self._json(401, _failure("unauthorized", "本地访问令牌无效"))
            return self._control(route, method, query, raw)
        except QuarkError as exc:
            self._failure_response(exc)
        except httpx.HTTPError:
            self._failure_response(RemoteError("上游传输失败"))

    def _control(self, route: str, method: str, query: Mapping[str, list[str]], raw: bytes | None) -> None:
        service = self.service
        if route == "/v1/auth":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            return self._json(200, service.login_payload())
        if route == "/v1/auth/login":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            return self._json(202, service.start_login())
        if route == "/v1/auth/logout":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            return self._json(200, service.logout())
        if route == "/v1/files":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            parent = require_fid(_first(query, "parent", "0"), label="parent", allow_root=True)
            number = _int_param(query, "page", 1, 1, MAX_PAGE_NUMBER)
            size = _int_param(query, "size", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
            return self._json(200, page(service.files(), parent, number, size))
        if route == "/v1/playlist":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            parent_value = _first(query, "parent", None)
            start_value = _first(query, "start", None)
            parent = require_fid(parent_value, label="parent", allow_root=True) if parent_value is not None else None
            start = require_fid(start_value, label="start") if start_value is not None else None
            resolved, items = playlist(service.files(), parent, start)
            return self._json(200, {"parent": resolved, "items": items})
        if route == "/v1/play":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            body = self._body(raw)
            fid = require_fid(body.get("fid"), label="fid")
            media = resolve(service.files(), service.client(), fid)
            session = service.sessions.create(media)
            return self._json(200, {
                "url": f"/v1/stream/{session.session}/{quote(session.name, safe='')}",
                "title": session.name,
            })
        if route == "/v1/shutdown":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            self._json(200, {"status": "stopping"})
            self.wfile.flush()
            service.stop()
            return
        if route == "/v1/search":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            service.require_login()
            keyword = _first(query, "q", None)
            if keyword is None or not keyword.strip():
                raise UsageError("q 不能为空")
            number = _int_param(query, "page", 1, 1, MAX_PAGE_NUMBER)
            size = _int_param(query, "size", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
            return self._json(200, search(service.files(), keyword.strip(), number, size))
        if route == "/v1/favorites":
            if method in ("GET", "HEAD"):
                service.require_login()
                return self._json(200, {"items": service.library.favorites()})
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET 或 POST"))
            service.require_login()
            fid = require_fid(self._body(raw).get("fid"), label="fid")
            info = service.files().info(fid)
            if not is_directory(info):
                raise UsageError("fid 不是网盘目录")
            name = info.get("file_name")
            if not isinstance(name, str) or not name:
                raise RemoteError("目录详情缺少有效名称")
            return self._json(200, {"items": service.library.add_favorite(fid, name)})
        if route == "/v1/favorites/remove":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            service.require_login()
            fid = require_fid(self._body(raw).get("fid"), label="fid")
            return self._json(200, {"items": service.library.remove_favorite(fid)})
        if route == "/v1/recent":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            service.require_login()
            return self._json(200, {"items": service.library.recent()})
        if route == "/v1/recent/clear":
            if method != "POST":
                return self._json(405, _failure("method_not_allowed", "该接口只接受 POST"))
            service.require_login()
            service.library.clear_recent()
            return self._json(200, {"items": []})
        if route == "/v1/subtitles":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            service.require_login()
            try:
                payload = match_subtitles(
                    service.files(), service.sessions,
                    _first(query, "fid", None), _first(query, "session", None),
                )
            except UnknownSession:
                return self._json(404, _failure("not_found", "播放会话不存在或已失效"))
            return self._json(200, payload)
        if route == "/v1/subtitle":
            if method not in ("GET", "HEAD"):
                return self._json(405, _failure("method_not_allowed", "该接口只接受 GET"))
            service.require_login()
            fid = require_fid(_first(query, "fid", None), label="fid")
            return self._text(200, read_subtitle(service.files(), service.client(), fid))
        if route in _CONTROL_ROUTES:
            return self._json(405, _failure("method_not_allowed", "请求方法不受支持"))
        return self._json(404, _failure("not_found", "未知接口"))

    # -- playback --------------------------------------------------------

    def _stream(self, route: str) -> None:
        parts = route.split("/")
        if len(parts) < 4 or not parts[3]:
            return self._json(400, _failure("bad_request", "播放地址无效"))
        session = self.service.sessions.get(parts[3])
        if session is None:
            return self._json(404, _failure("not_found", "播放会话不存在或已失效"))
        try:
            byte_range = content_range(self.headers.get("Range"), session.size)
        except RangeNotSatisfiable:
            return self._json(
                416,
                _failure("range_not_satisfiable", "请求范围超出文件大小"),
                extra={"Content-Range": f"bytes */{session.size}"},
            )
        if self.command == "HEAD":
            length = session.size if byte_range is None else byte_range[1] - byte_range[0] + 1
            self._stream_headers(session, byte_range, length)
            return
        self._stream_body(session, byte_range)

    def _stream_body(self, session: PlaySession, byte_range: tuple[int, int] | None) -> None:
        service = self.service

        def refetch() -> None:
            media = resolve(service.files(), service.client(), session.fid)
            if media.size != session.size:
                service.sessions.forget(session.session)
                raise MediaChanged("文件大小或版本已变化，已中止播放")
            service.sessions.refresh(session.session, media)

        # The player keeps one GET open for the whole episode, so a half-watched
        # title still counts as played. Record the moment the first chunk leaves
        # the socket; one flag shared by every segment keeps it to one write, and
        # a later disconnect or truncation never erases the entry.
        recorded = False

        def record_once() -> None:
            nonlocal recorded
            if not recorded:
                recorded = True
                self._record_played(session)

        sent = False
        start, end = byte_range if byte_range is not None else (0, session.size - 1)
        try:
            if end - start + 1 > MEDIA_SEGMENT:
                # One large ask is throttled by the origin; take the same bytes
                # in serial bounded ranges and forward each as it arrives.
                position = start
                while position <= end:
                    stop = min(position + MEDIA_SEGMENT - 1, end)
                    with open_content(service.client(), session, (position, stop), refetch) as response:
                        _, length, _ = _upstream_plan(response, session, (position, stop))
                        self._pin_version(session, response)
                        if not sent:
                            self._stream_headers(session, byte_range, end - start + 1)
                            sent = True
                        if not self._forward(response, length, on_started=record_once):
                            return
                    position = stop + 1
                return
            with open_content(service.client(), session, byte_range, refetch) as response:
                status, length, header = _upstream_plan(response, session, byte_range)
                self._pin_version(session, response)
                self._stream_headers(session, byte_range if status == 206 else None, length, status=status, header=header)
                sent = True
                self._forward(response, length, on_started=record_once)
        except (QuarkError, httpx.HTTPError) as exc:
            if sent:
                # The body is already in flight; only an aborted connection is honest here.
                self.close_connection = True
            else:
                self._failure_response(exc)

    def _record_played(self, session: PlaySession) -> None:
        """One disk write per started media response, never per chunk or segment.

        A corrupt library raises ``LocalIOError`` here; it is left to propagate
        so the original ``QuarkError`` path aborts the stream instead of the
        library being silently reset, and ``/v1/recent``/``/v1/favorites`` keep
        reporting the stored document as broken.
        """
        self.service.library.record_recent(session.fid, session.name, session.size)

    def _pin_version(self, session: PlaySession, response: httpx.Response) -> None:
        """Bind the first strong ETag; any later different strong ETag aborts."""
        etag = response.headers.get("etag")
        if etag and not etag.startswith("W/") and not self.service.sessions.pin_etag(session.session, etag):
            raise MediaChanged("文件版本已变化，已中止播放")

    def _stream_headers(
        self,
        session: PlaySession,
        byte_range: tuple[int, int] | None,
        length: int,
        *,
        status: int | None = None,
        header: str | None = None,
    ) -> None:
        if status is None:
            status = 206 if byte_range is not None else 200
        self.send_response(status)
        self.send_header("Content-Type", content_type(session.name))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if header is not None:
            self.send_header("Content-Range", header)
        elif byte_range is not None:
            self.send_header("Content-Range", f"bytes {byte_range[0]}-{byte_range[1]}/{session.size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _forward(
        self,
        response: httpx.Response,
        length: int,
        *,
        on_started: Callable[[], None] | None = None,
    ) -> bool:
        """Copy exactly ``length`` bytes; ``False`` means the body stopped short.

        ``on_started`` runs once, right after the first non-empty chunk is
        written to the socket, so callers can react before the body completes.
        """
        written = 0
        try:
            for chunk in response.iter_raw(CHUNK_SIZE):
                if not chunk:
                    continue
                remaining = length - written
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                self.wfile.write(chunk)
                written += len(chunk)
                if on_started is not None:
                    on_started()
                    on_started = None
                if written >= length:
                    break
        except (httpx.HTTPError, OSError):
            self.close_connection = True
            return False
        if written < length:
            # The origin ended early; the announced Content-Length can no longer hold.
            self.close_connection = True
            return False
        return True

    # -- primitives ------------------------------------------------------

    def _consume_body(self) -> bytes | None:
        """Read the announced body once, bounded, so keep-alive stays clean.

        Control requests (``auth/login``, ``logout``, ``shutdown``) ignore their
        body, but leaving it unread corrupts the next request on the same
        connection. A body over ``MAX_BODY`` is refused without draining and the
        connection is closed instead, since the remainder cannot be trusted.
        """
        header = self.headers.get("Content-Length")
        if header is None:
            return None
        if not header.isdigit():
            self.close_connection = True
            return None
        length = int(header)
        if length <= 0:
            return b""
        if length > MAX_BODY:
            self.close_connection = True
            return None
        return self.rfile.read(length)

    def _body(self, raw: bytes | None) -> dict[str, Any]:
        if raw is None:
            raise UsageError("请求体长度无效")
        if not raw:
            raise UsageError("请求体不能为空")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise UsageError("请求体必须是 JSON 对象") from None
        if not isinstance(payload, dict):
            raise UsageError("请求体必须是 JSON 对象")
        return payload

    def _json(self, status: int, payload: Mapping[str, Any], extra: Mapping[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _failure_response(self, exc: BaseException) -> None:
        status, kind = _status_for(exc)
        if kind == "login_required":
            # The account API (not the origin) rejected the Cookie: the plugin's
            # own credential is stale, so drop it and ask the player to re-scan.
            self.service.invalidate_credential()
            message = "插件未登录或登录已失效，请在播放器中重新扫码登录"
        else:
            message = str(exc) or "服务内部错误"
        retry_after = getattr(exc, "retry_after", None)
        extra = None
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) and retry_after > 0:
            extra = {"Retry-After": str(math.ceil(retry_after))}
        try:
            self._json(status, _failure(kind, message), extra=extra)
        except OSError:
            self.close_connection = True


def run_service(*, transport: httpx.BaseTransport | None = None, cookie: str | None = None) -> int:
    """Serve in this process; returns ``EXIT_ALREADY_RUNNING`` when one is live."""
    with ExitStack() as stack:
        lock = acquire_service_lock()
        if lock is None:
            return EXIT_ALREADY_RUNNING
        stack.push(lock)
        service = Service(transport=transport, cookie=cookie)
        server = service.build_server()
        write_runtime({
            "version": SERVICE_VERSION,
            "instance": service.instance,
            "port": service.port,
            "token": service.token,
            "pid": os.getpid(),
            "started_at": int(time.time()),
        })
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            clear_runtime(service.instance)
            service.close()
    return EXIT_OK


def _healthy(port: int, instance: str) -> bool:
    try:
        response = httpx.get(f"http://{HOST}:{port}/v1/health", timeout=HEALTH_TIMEOUT)
        if response.status_code != 200:
            return False
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok" and payload.get("instance") == instance


def _healthy_runtime() -> dict[str, Any] | None:
    entry = read_runtime()
    if not entry:
        return None
    port, token, instance = entry.get("port"), entry.get("token"), entry.get("instance")
    valid = (
        isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535
        and isinstance(token, str) and token
        and isinstance(instance, str) and instance
    )
    if not valid or not _healthy(port, instance):
        return None
    return entry


def _spawn_service(executable: str) -> None:
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        options["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    try:
        subprocess.Popen([executable, "-m", "quark_cli.potplayer", "--serve"], **options)
    except OSError:
        raise LocalIOError("无法启动夸克网盘本地服务进程") from None


def ensure_service(
    *,
    executable: str | None = None,
    timeout: float = START_TIMEOUT,
    spawn: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    """Return the live instance's ``{base_url, token}``, starting it when needed."""
    entry = _healthy_runtime()
    if entry is None:
        (spawn or _spawn_service)(executable or sys.executable or "python")
        deadline = time.monotonic() + timeout
        while entry is None:
            if time.monotonic() >= deadline:
                raise LocalIOError("夸克网盘本地服务未能在限定时间内就绪")
            sleep(0.1)
            entry = _healthy_runtime()
    return {"base_url": f"http://{HOST}:{entry['port']}", "token": entry["token"]}


def stop_service() -> int:
    """Ask the recorded instance to stop; a missing instance counts as stopped."""
    entry = read_runtime()
    if not entry:
        return EXIT_OK
    port, token = entry.get("port"), entry.get("token")
    if not isinstance(port, int) or not isinstance(token, str):
        return EXIT_UNAVAILABLE
    try:
        response = httpx.post(
            f"http://{HOST}:{port}/v1/shutdown",
            headers={"Authorization": f"Bearer {token}"},
            timeout=HEALTH_TIMEOUT,
        )
    except httpx.HTTPError:
        return EXIT_UNAVAILABLE
    return EXIT_OK if response.status_code == 200 else EXIT_UNAVAILABLE
