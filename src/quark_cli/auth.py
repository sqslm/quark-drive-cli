"""Quark QR login and local credential storage."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urljoin, urlparse

import httpx

from .client import DEFAULT_HEADERS, DEFAULT_PARAMS, QuarkClient
from .errors import AuthenticationError, LocalIOError, NetworkError, QuarkError, RemoteError
from .state import config_directory, private_path, restrict_windows_acl, secure_directory


TOKEN_URL = "https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin"
POLL_URL = "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken"
ACCOUNT_INFO_URL = "https://pan.quark.cn/account/info"
PAN_LIST_URL = "https://pan.quark.cn/list"
DRIVE_SORT_URL = "https://drive-pc.quark.cn/1/clouddrive/file/sort"
DRIVE_MEMBER_URL = "https://drive.quark.cn/1/clouddrive/member"
QR_BASE_URL = "https://su.quark.cn/4_eMHBJ"
_TERMINAL_QR_STATUSES = {50004002, 50004003, 50004004}
_MAX_REDIRECTS = 5


class LoginCancelled(AuthenticationError):
    """The user closed the QR window; the current poll is abandoned unsaved."""


class QRExpired(AuthenticationError):
    """The QR code expired or the wait ran out; a fresh code may be requested."""


def credential_path() -> Path:
    """Return the current user's Quark CLI credential file path."""
    return config_directory() / "cookie"


def _restrict_windows_acl(path: Path, *, directory: bool) -> None:
    restrict_windows_acl(path, directory=directory)


def _secure_directory(path: Path) -> None:
    secure_directory(path)


def _validate_cookie(cookie: str) -> str:
    value = cookie.strip()
    if not value:
        raise AuthenticationError("没有可用的夸克 Cookie，请执行 quark auth login")
    if "\r" in value or "\n" in value:
        raise AuthenticationError("Cookie 格式无效")
    return value


def _read_text_file(path: Path, *, saved_credential: bool = False) -> str:
    descriptor = None
    try:
        if saved_credential:
            private_path(path)
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            stream = os.fdopen(descriptor, "r", encoding="utf-8")
            descriptor = None
            with stream:
                return stream.read().strip()
        return path.read_text(encoding="utf-8").strip()
    except QuarkError:
        raise
    except (OSError, UnicodeError):
        message = "无法读取本地扫码凭证" if saved_credential else "无法读取 QUARK_COOKIE_FILE 指定的文件"
        raise LocalIOError(message) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def save_cookie(cookie: str, path: Path | None = None) -> None:
    """Write a verified Cookie with private permissions and atomic replacement."""
    value = _validate_cookie(cookie)
    destination = path or credential_path()
    directory = destination.parent
    _secure_directory(directory)
    temporary_path: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".cookie-", dir=directory)
        temporary_path = Path(temporary_name)
        if sys.platform == "win32":
            os.close(descriptor)
            descriptor = None
            _restrict_windows_acl(temporary_path, directory=False)
            with temporary_path.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        else:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                descriptor = None
                stream.write(value + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
    except QuarkError:
        raise
    except OSError:
        raise LocalIOError("无法安全保存扫码登录凭证") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def load_saved_cookie(path: Path | None = None) -> str | None:
    destination = path or credential_path()
    if not destination.exists():
        return None
    _secure_directory(destination.parent)
    value = _read_text_file(destination, saved_credential=True)
    return _validate_cookie(value)


def logout(path: Path | None = None) -> bool:
    """Delete only this tool's saved Cookie; environment variables are untouched."""
    destination = path or credential_path()
    try:
        if destination.is_symlink():
            destination.unlink()
            return True
        if not destination.exists():
            return False
        _secure_directory(destination.parent)
        destination.unlink()
        return True
    except QuarkError:
        raise
    except OSError:
        raise LocalIOError("无法删除本地扫码凭证") from None


def get_cookie(
    environment: Mapping[str, str] | None = None,
    *,
    path: Path | None = None,
) -> str:
    """Read credentials by precedence: environment, env file, saved QR login."""
    source = os.environ if environment is None else environment
    environment_cookie = source.get("QUARK_COOKIE", "").strip()
    if environment_cookie:
        return _validate_cookie(environment_cookie)
    cookie_file = source.get("QUARK_COOKIE_FILE", "").strip()
    if cookie_file:
        value = _read_text_file(Path(cookie_file).expanduser())
        return _validate_cookie(value)
    try:
        saved = load_saved_cookie(path)
    except LocalIOError:
        if path is None:
            raise AuthenticationError("请设置 QUARK_COOKIE、QUARK_COOKIE_FILE，或执行 quark auth login") from None
        raise
    if saved:
        return saved
    raise AuthenticationError("请设置 QUARK_COOKIE、QUARK_COOKIE_FILE，或执行 quark auth login")


def build_qr_url(token: str) -> str:
    # `ssb=weblogin` selects Quark's web-login QR page. The CLI never invokes
    # the standalone Quark desktop client.
    query = urlencode(
        {
            "token": token,
            "client_id": "532",
            "ssb": "weblogin",
            "uc_param_str": "",
            "uc_biz_str": "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0",
        }
    )
    return f"{QR_BASE_URL}?{query}"


def write_qr_png(value: str, path: Path | None = None) -> Path:
    """Create an exclusive, private PNG that encodes the supplied QR URL."""
    try:
        import qrcode
    except ImportError:
        raise LocalIOError("缺少二维码依赖，请重新安装 quark-drive-cli") from None

    destination = path
    descriptor: int | None = None
    created = False
    completed = False
    try:
        if destination is None:
            descriptor, name = tempfile.mkstemp(prefix="quark-login-", suffix=".png")
            destination = Path(name)
        else:
            destination = destination.expanduser().absolute()
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = True
        if sys.platform == "win32":
            os.close(descriptor)
            descriptor = None
            _restrict_windows_acl(destination, directory=False)
            stream_context = destination.open("wb")
        else:
            os.fchmod(descriptor, 0o600)
            stream_context = os.fdopen(descriptor, "wb")
            descriptor = None

        with stream_context as stream:
            qrcode.make(value).save(stream, format="PNG")
            stream.flush()
            os.fsync(stream.fileno())
        completed = True
        return destination
    except QuarkError:
        raise
    except OSError:
        raise LocalIOError("无法安全生成二维码文件") from None
    except Exception:
        raise LocalIOError("无法生成二维码图片") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created and not completed and destination is not None:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass


def render_terminal_qr(value: str) -> str:
    """Render black modules on a white background with a four-module quiet zone."""
    try:
        import qrcode
    except ImportError:
        raise LocalIOError("缺少二维码依赖，请重新安装 quark-drive-cli") from None
    qr = qrcode.QRCode(border=4)
    qr.add_data(value)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    width = len(matrix)
    if len(matrix) % 2:
        matrix.append([False] * width)
    symbols = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}
    return "\n".join(
        "\x1b[30;47m" + "".join(symbols[(bool(top), bool(bottom))] for top, bottom in zip(matrix[index], matrix[index + 1])) + "\x1b[0m"
        for index in range(0, len(matrix), 2)
    )


def _json_response(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except (ValueError, UnicodeError):
        raise RemoteError("扫码登录服务返回了无效响应") from None
    if not isinstance(payload, dict):
        raise RemoteError("扫码登录服务响应格式无效")
    return payload


def _require_http_success(response: httpx.Response, *, operation: str) -> None:
    if response.status_code in (401, 403):
        raise AuthenticationError("扫码登录会话无效，请重新执行 quark auth login")
    if not 200 <= response.status_code < 300:
        raise RemoteError(f"夸克{operation}请求失败（HTTP {response.status_code}）")


def _get_without_redirects(
    session: httpx.Client,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    allowed_host: str,
    operation: str,
) -> httpx.Response:
    current_url = url
    current_params = params
    for redirect_count in range(_MAX_REDIRECTS + 1):
        try:
            response = session.get(current_url, params=current_params, headers=headers)
        except httpx.TimeoutException:
            raise NetworkError(f"夸克{operation}请求超时") from None
        except httpx.RequestError:
            raise NetworkError(f"无法连接夸克{operation}服务") from None
        current_params = None
        if response.status_code not in (301, 302, 303, 307, 308):
            _require_http_success(response, operation=operation)
            return response
        if redirect_count >= _MAX_REDIRECTS:
            raise RemoteError(f"夸克{operation}重定向次数过多")
        location = response.headers.get("location")
        if not location:
            raise RemoteError(f"夸克{operation}响应缺少重定向地址")
        next_url = urljoin(str(response.url), location)
        parsed = urlparse(next_url)
        if parsed.scheme != "https" or parsed.hostname != allowed_host or parsed.username or parsed.password:
            raise RemoteError(f"夸克{operation}返回了不安全的重定向")
        current_url = next_url
    raise RemoteError(f"夸克{operation}重定向失败")


def _cookie_from_jar(session: httpx.Client) -> str:
    selected: dict[str, tuple[int, int, str]] = {}
    for index, cookie in enumerate(session.cookies.jar):
        domain = (cookie.domain or "").lower().lstrip(".")
        if domain not in {"quark.cn", "pan.quark.cn", "drive-pc.quark.cn", "drive.quark.cn"}:
            continue
        if not cookie.name or not cookie.value:
            continue
        # Prefer cookies scoped to all Quark subdomains when duplicate names
        # occur; those are the ones a browser would send to drive-pc.quark.cn.
        scope_rank = 2 if domain == "quark.cn" else (1 if domain == "drive-pc.quark.cn" else 0)
        existing = selected.get(cookie.name)
        if existing is None or scope_rank > existing[0] or (scope_rank == existing[0] and index > existing[1]):
            selected[cookie.name] = (scope_rank, index, cookie.value)
    return "; ".join(f"{name}={row[2]}" for name, row in selected.items())


class QRLogin:
    """Runs one QR login, validates the resulting Cookie, and stores it."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: int = 300,
        poll_interval: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        cookie_path: Path | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if timeout < 1 or timeout > 300:
            raise AuthenticationError("扫码等待时间必须在 1 到 300 秒之间")
        self._transport = transport
        self._timeout = timeout
        self._poll_interval = max(0.0, poll_interval)
        self._sleep = sleep
        self._monotonic = monotonic
        self._cookie_path = cookie_path
        self._cancelled = cancelled

    def _check_cancelled(self) -> None:
        if self._cancelled is not None and self._cancelled():
            raise LoginCancelled("登录已取消")

    def _request(self, session: httpx.Client, url: str, *, params: Mapping[str, str]) -> httpx.Response:
        try:
            response = session.get(url, params=params)
        except httpx.TimeoutException:
            raise NetworkError("夸克扫码登录请求超时") from None
        except httpx.RequestError:
            raise NetworkError("无法连接夸克扫码登录服务") from None
        _require_http_success(response, operation="扫码登录")
        return response

    def _get_qr_token(self, session: httpx.Client) -> str:
        payload = _json_response(
            self._request(
                session,
                TOKEN_URL,
                params={"client_id": "532", "v": "1.2", "request_id": str(uuid.uuid4())},
            )
        )
        data = payload.get("data")
        members = data.get("members") if isinstance(data, dict) else None
        token = members.get("token") if isinstance(members, dict) else None
        if payload.get("status") != 2000000 or not isinstance(token, str) or not token:
            raise RemoteError("夸克未返回有效二维码凭证")
        return token

    def _wait_for_ticket(self, session: httpx.Client, token: str) -> str:
        deadline = self._monotonic() + self._timeout
        while True:
            self._check_cancelled()
            payload = _json_response(
                self._request(
                    session,
                    POLL_URL,
                    params={"client_id": "532", "v": "1.2", "token": token, "request_id": str(uuid.uuid4())},
                )
            )
            status = payload.get("status")
            if status == 2000000:
                data = payload.get("data")
                members = data.get("members") if isinstance(data, dict) else None
                ticket = members.get("service_ticket") if isinstance(members, dict) else None
                if not isinstance(ticket, str) or not ticket:
                    raise RemoteError("扫码成功响应缺少登录凭证")
                return ticket
            if isinstance(status, int) and not isinstance(status, bool) and status in _TERMINAL_QR_STATUSES:
                raise QRExpired("二维码已过期或登录已取消，请重新获取二维码")
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise QRExpired("扫码登录超时，请重新获取二维码")
            self._sleep(min(self._poll_interval, remaining))
            self._check_cancelled()

    def _exchange_ticket(self, session: httpx.Client, ticket: str) -> None:
        _get_without_redirects(
            session,
            ACCOUNT_INFO_URL,
            params={"st": ticket, "lw": "scan"},
            allowed_host="pan.quark.cn",
            operation="账号验证",
        )

    @staticmethod
    def _has_puus(cookie: str) -> bool:
        return any(part.strip().partition("=")[0] == "__puus" for part in cookie.split(";"))

    def _complete_cookie(self, session: httpx.Client) -> None:
        # Fresh QR sessions may not receive __puus from /account/info. The
        # sequence is read-only; each request is restricted to its fixed host,
        # and neither QR token nor service ticket is sent to these endpoints.
        warmups = (
            (PAN_LIST_URL, "pan.quark.cn", None),
            (
                DRIVE_SORT_URL,
                "drive-pc.quark.cn",
                {
                    "pdir_fid": "0",
                    "_page": "1",
                    "_size": "50",
                    "_fetch_total": "1",
                    "_sort": "file_type:asc,updated_at:desc",
                },
            ),
            (
                DRIVE_MEMBER_URL,
                "drive.quark.cn",
                {"fetch_subscribe": "true"},
            ),
        )
        for url, host, extra_params in warmups:
            if self._has_puus(_cookie_from_jar(session)):
                return
            params = dict(extra_params or {})
            if url != PAN_LIST_URL:
                params = {**DEFAULT_PARAMS, **params}
            try:
                cookie = _cookie_from_jar(session)
                _get_without_redirects(
                    session,
                    url,
                    params=params or None,
                    headers={"cookie": cookie} if cookie else None,
                    allowed_host=host,
                    operation="Cookie 补全",
                )
            except (AuthenticationError, NetworkError, RemoteError):
                # Later read-only hosts may still provide __puus. Final API
                # validation below remains mandatory.
                continue

    def _validate_session(self, session: httpx.Client, cookie: str) -> str:
        # Reuse the login session so Set-Cookie returned by the real read-only
        # auth check is merged into the same cookie jar before persistence.
        with QuarkClient(cookie, transport=self._transport, api_client=session) as client:
            client.auth_status()
        refreshed_cookie = _cookie_from_jar(session)
        if not self._has_puus(refreshed_cookie):
            raise AuthenticationError("夸克未返回完整登录 Cookie，未保存凭证")
        return refreshed_cookie

    def run(
        self,
        *,
        qr_file: Path | None = None,
        on_qr_ready: Callable[[Path], None] | None = None,
        on_qr_terminal: Callable[[str], None] | None = None,
    ) -> None:
        qr_path: Path | None = None
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=20.0,
                headers=DEFAULT_HEADERS,
                follow_redirects=False,
            ) as session:
                token = self._get_qr_token(session)
                qr_url = build_qr_url(token)
                qr_path = write_qr_png(qr_url, qr_file)
                if on_qr_ready is not None:
                    on_qr_ready(qr_path)
                if on_qr_terminal is not None:
                    on_qr_terminal(render_terminal_qr(qr_url))
                ticket = self._wait_for_ticket(session, token)
                self._exchange_ticket(session, ticket)
                self._complete_cookie(session)
                cookie = _cookie_from_jar(session)
                if not cookie:
                    raise AuthenticationError("扫码已确认，但夸克未返回可用 Cookie")
                cookie = self._validate_session(session, cookie)
                self._check_cancelled()
                save_cookie(cookie, self._cookie_path)
        finally:
            if qr_path is not None:
                try:
                    qr_path.unlink(missing_ok=True)
                except OSError:
                    raise LocalIOError("登录结束后无法清理二维码文件") from None
