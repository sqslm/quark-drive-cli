"""HTTP and authentication handling for Quark Drive's web API."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from typing import Any

import httpx

from .errors import (
    AccessRestrictedError,
    AuthenticationError,
    NetworkError,
    QuarkError,
    RateLimitError,
    RemoteError,
)
from .request_guard import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_RESTRICTION_COOLDOWN_SECONDS,
    REASON_ACCESS_RESTRICTED,
    REASON_RATE_LIMITED,
    REASON_SERVICE_UNAVAILABLE,
    RequestGuard,
    retry_after_seconds,
)


API_BASE = "https://drive-pc.quark.cn/1/clouddrive"
API_HOST = "drive-pc.quark.cn"
DEFAULT_PARAMS = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
DEFAULT_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "origin": "https://pan.quark.cn",
    "referer": "https://pan.quark.cn/",
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
}
QUARK_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/2.5.56 Chrome/100.0.4896.160 "
    "Electron/18.3.5.12-a038f7b798 Safari/537.36 Channel/pckk_other_ch"
)

# Explicit upstream restriction, restriction-challenge and throttling wording.
# These take precedence over broad authentication keywords so a restricted
# account is not mistaken for a stale Cookie. No undocumented numeric risk codes
# are invented here.
_RATE_LIMIT_PHRASES = (
    "访问频繁",
    "操作频繁",
    "请求过于频繁",
    "操作过于频繁",
    "请求频繁",
    "rate limit",
    "too many requests",
)
_RESTRICTION_PHRASES = (
    "访问受限",
    "访问被限制",
    "限制访问",
    "账号受限",
    "账户受限",
    "账号封禁",
    "账户封禁",
    "验证码",
    "人机验证",
    "风控",
    "access restricted",
    "access denied",
)


def explicit_rejection_reason(text: str) -> str | None:
    """Classify explicit upstream wording as a restriction or a throttle.

    Returns ``REASON_ACCESS_RESTRICTED``, ``REASON_RATE_LIMITED`` or ``None``.
    A verification/account-restriction phrase wins over a bare throttling
    phrase: a restricted account must not be retried as if it were merely rate
    limited. Only documented neutral wording is matched; upstream text is never
    echoed back to the caller.
    """
    lowered = text.lower()
    if any(phrase in lowered for phrase in _RESTRICTION_PHRASES):
        return REASON_ACCESS_RESTRICTED
    if any(phrase in lowered for phrase in _RATE_LIMIT_PHRASES):
        return REASON_RATE_LIMITED
    return None


def default_guard(transport: httpx.BaseTransport | None, api_client: httpx.Client | None) -> RequestGuard:
    """Keep mocked transports isolated; real transports share durable state."""
    if isinstance(transport, httpx.MockTransport) or isinstance(
        getattr(api_client, "_transport", None), httpx.MockTransport
    ):
        return RequestGuard.memory()
    return RequestGuard.durable()


class QuarkClient:
    """Owns separate authenticated API and unauthenticated transfer sessions.

    ``transport`` is intended for offline MockTransport tests. Production API
    requests always target the fixed Quark API host; callers cannot redirect
    the Cookie to an arbitrary server. ``api_client`` lets QR login reuse its
    cookie jar for the final read-only authentication check.
    """

    def __init__(
        self,
        cookie: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
        api_client: httpx.Client | None = None,
        guard: RequestGuard | None = None,
    ) -> None:
        if not cookie or not cookie.strip():
            raise AuthenticationError("请设置 QUARK_COOKIE 或 QUARK_COOKIE_FILE")
        self._cookie = cookie.strip()
        self._cookie_secrets: set[tuple[str, str]] = set()
        for component in self._cookie.split(";"):
            name, separator, value = component.strip().partition("=")
            if separator and value:
                self._cookie_secrets.add((name, value))
        self._transport = transport
        self._guard = guard if guard is not None else default_guard(transport, api_client)
        self._worker_transfers: list[httpx.Client] = []
        self._worker_transfers_lock = threading.Lock()
        self._owns_api = api_client is None
        if api_client is not None:
            self._api = api_client
        else:
            self._api = httpx.Client(
                transport=transport,
                timeout=timeout,
                headers=DEFAULT_HEADERS,
                follow_redirects=False,
            )
        # Never install the full account Cookie on the transfer client.
        self._transfer = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_api:
            self._api.close()
        for storage in self._worker_transfers:
            storage.close()
        self._transfer.close()

    def __enter__(self) -> "QuarkClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _redact(self, message: str) -> str:
        """Mask the account Cookie and any refreshed credential value."""
        if self._cookie:
            message = message.replace(self._cookie, "[redacted]")
        for name, value in sorted(self._cookie_secrets, key=lambda pair: len(pair[1]), reverse=True):
            message = message.replace(f"{name}={value}", f"{name}=[redacted]")
            if len(value) >= 4:
                message = message.replace(value, "[redacted]")
        return message

    def _safe(self, message: str) -> str:
        """Expose only a masked, truncated message; never echo credentials or URLs."""
        message = self._redact(message)
        message = re.sub(r"https?://\S+", "[redacted URL]", message, flags=re.IGNORECASE)
        message = re.sub(r"(?i)(authorization|auth_key|access_token|signature|sig)\s*[:=]\s*\S+", r"\1=[redacted]", message)
        return message[:240]

    def _refresh_puus(self, response: httpx.Response) -> None:
        """Keep the account-wide PDS credential current in this process only."""
        if response.request.url.scheme != "https" or response.request.url.host != API_HOST:
            return
        for cookie in response.cookies.jar:
            if (
                cookie.name != "__puus"
                or cookie.domain.lower().lstrip(".") != "quark.cn"
                or cookie.path != "/"
                or cookie.is_expired()
            ):
                continue
            value = cookie.value or ""
            if value:
                self._cookie_secrets.add(("__puus", value))
            if not value or not all(0x21 <= ord(char) <= 0x7E and char not in ",;" for char in value):
                continue
            components = [
                component.strip()
                for component in self._cookie.split(";")
                if component.strip() and component.strip().partition("=")[0].strip() != "__puus"
            ]
            components.append(f"__puus={value}")
            self._cookie = "; ".join(components)

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        user_agent: str | None = None,
        accepted_business_codes: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        url = f"{API_BASE}/{endpoint.lstrip('/')}"
        query = dict(DEFAULT_PARAMS)
        if params:
            query.update(params)
        with self._guard.request():
            # Build the Cookie header only after the guard is held: a queued
            # request must use the newest __puus another request just refreshed.
            headers = {"cookie": self._cookie, "content-type": "application/json"}
            if user_agent is not None:
                headers["user-agent"] = user_agent
            try:
                response = self._api.request(
                    method,
                    url,
                    params=query,
                    json=dict(body) if body is not None else None,
                    headers=headers,
                )
            except httpx.TimeoutException:
                raise NetworkError("夸克 API 请求超时") from None
            except httpx.RequestError:
                raise NetworkError("无法连接夸克 API") from None

            self._refresh_puus(response)
            self._raise_for_http_status(response)
            try:
                payload = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise RemoteError("夸克 API 返回了非 JSON 响应") from None
            if not isinstance(payload, dict):
                raise RemoteError("夸克 API 响应格式无效")

            status = payload.get("status")
            code = payload.get("code")
            if status is None and code is None:
                raise RemoteError("夸克 API 响应缺少明确的成功状态")
            # Opt-in partial-result protocols can retain the original task receipt.
            # This is never enabled by ordinary file/transfer/save callers.
            numeric_code = self._numeric_code(code)
            numeric_status = self._numeric_code(status)
            if (numeric_code in accepted_business_codes and numeric_status is not None
                    and 100 <= numeric_status < 500 and isinstance(payload.get("data"), dict)):
                return payload
            if isinstance(status, bool):
                self._raise_api_error(payload)
            if status is not None and not isinstance(status, (int, str)):
                self._raise_api_error(payload)
            if isinstance(status, int) and not isinstance(status, bool) and status != 200:
                self._raise_api_error(payload)
            if isinstance(status, str) and status.lower() not in ("success", "ok", "200"):
                self._raise_api_error(payload)
            if code not in (None, 0, "0"):
                self._raise_api_error(payload)
            return payload

    def _raise_for_http_status(self, response: httpx.Response) -> None:
        """Classify transport status codes; record cooldown before raising."""
        status = response.status_code
        if status == 401:
            # 401 is the only status that proves the Cookie itself is stale.
            raise AuthenticationError("Cookie 已失效或无权访问，请重新获取网页端 Cookie")
        if status == 403:
            # A raw 403 means access is restricted, not that the Cookie is stale.
            delay = self._retry_after_seconds(response)
            cooldown = max(DEFAULT_RESTRICTION_COOLDOWN_SECONDS, delay if delay is not None else 0.0)
            recorded = self._guard.record_cooldown(cooldown, REASON_ACCESS_RESTRICTED)
            remaining = max(0.0, recorded.until - self._guard.now())
            raise AccessRestrictedError(
                f"夸克 API 拒绝访问（HTTP 403），已暂停后续请求且未清除凭据，约 {max(0, int(remaining + 0.999))} 秒后再试；"
                "请在官方客户端确认账号状态后重试",
                retry_after=remaining,
            )
        if status == 429:
            delay = self._retry_after_seconds(response)
            cooldown = max(DEFAULT_COOLDOWN_SECONDS, delay if delay is not None else 0.0)
            recorded = self._guard.record_cooldown(cooldown, REASON_RATE_LIMITED)
            remaining = max(0.0, recorded.until - self._guard.now())
            raise RateLimitError(
                f"夸克 API 请求过于频繁（HTTP 429），已暂停后续请求，约 {max(0, int(remaining + 0.999))} 秒后再试；"
                "请在官方客户端确认账号状态后重试",
                retry_after=remaining,
            )
        if status == 503:
            delay = self._retry_after_seconds(response)
            if delay is not None:
                recorded = self._guard.record_cooldown(delay, REASON_SERVICE_UNAVAILABLE)
                remaining = max(0.0, recorded.until - self._guard.now())
                raise RateLimitError(
                    f"夸克 API 暂时不可用（HTTP 503），已暂停后续请求，约 {max(0, int(remaining + 0.999))} 秒后再试；"
                    "请在官方客户端确认账号状态后重试",
                    retry_after=remaining,
                )
        if status < 200 or status >= 300:
            raise RemoteError(f"夸克 API 返回 HTTP {status}")

    def _retry_after_seconds(self, response: httpx.Response) -> float | None:
        """Parse Retry-After (delay-seconds or HTTP-date); None when absent/invalid."""
        return retry_after_seconds(response.headers.get("retry-after"), self._guard.now())

    def _raise_api_error(self, payload: Mapping[str, Any]) -> None:
        # Classify the full, untruncated message with only credentials masked, so
        # a restriction/verification phrase cannot be lost to truncation or to
        # an echoed Cookie value. Only the sanitized form is ever exposed.
        raw = str(payload.get("message") or payload.get("msg") or "夸克 API 操作失败")
        detection = self._redact(raw)
        message = self._safe(raw)
        lowered = detection.lower()
        code = payload.get("code")
        numeric_code = self._numeric_code(code)
        numeric_status = self._numeric_code(payload.get("status"))
        reason = explicit_rejection_reason(detection)
        restricted = reason == REASON_ACCESS_RESTRICTED or numeric_code == 403 or numeric_status == 403
        throttled = reason == REASON_RATE_LIMITED or numeric_code == 429 or numeric_status == 429
        if restricted:
            # An explicit restriction/verification phrase or an exact business
            # 403 wins over broad authentication keywords: neither is evidence
            # of a stale Cookie. A substring "403" elsewhere never is.
            cooldown = self._guard.record_cooldown(DEFAULT_RESTRICTION_COOLDOWN_SECONDS, REASON_ACCESS_RESTRICTED)
            remaining = max(0.0, cooldown.until - self._guard.now())
            error: QuarkError = AccessRestrictedError(
                f"{message}；已暂停 {max(0, int(remaining + 0.999))} 秒内的后续请求，"
                "请在官方客户端确认账号状态后重试",
                retry_after=remaining,
            )
        elif throttled:
            # Only an exact 429 code/status is throttling; a "429" substring in
            # a message or a longer code such as 14290 never is.
            cooldown = self._guard.record_cooldown(DEFAULT_COOLDOWN_SECONDS, REASON_RATE_LIMITED)
            remaining = max(0.0, cooldown.until - self._guard.now())
            error = RateLimitError(
                f"{message}；已暂停 {max(0, int(remaining + 0.999))} 秒内的后续请求，"
                "请在官方客户端确认账号状态后重试",
                retry_after=remaining,
            )
        elif (
            numeric_code == 401
            or numeric_status == 401
            or str(code).lower() == "token_expired"
            or any(word in lowered for word in ("login", "cookie", "auth", "登录", "认证"))
        ):
            error = AuthenticationError(message)
        else:
            error = RemoteError(message)
        if (numeric_code is not None and numeric_code != 0 and numeric_code != 41060
                and (payload.get("status") is None or (isinstance(payload.get("status"), str) and payload["status"].lower() in ("success", "ok"))
                     or (numeric_status is not None and 100 <= numeric_status < 500))
                and not self._has_acceptance_evidence(payload.get("data"))):
            error.api_rejected = True
        raise error

    @staticmethod
    def _numeric_code(value: Any) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 10:
            return int(value)
        return None

    @staticmethod
    def _has_acceptance_evidence(value: Any) -> bool:
        if not isinstance(value, dict):
            return value is not None
        stack = [(value, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > 8:
                return True
            if "creation_snapshot" in current or any(current.get(field) for field in ("task_id", "share_id", "task_sync")):
                return True
            for field in ("data", "task_resp", "taskResult", "task_result", "result"):
                if isinstance(current.get(field), dict):
                    stack.append((current[field], depth + 1))
                elif field in current and current[field] is not None:
                    return True
        return False

    def get(self, endpoint: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", endpoint, params=params)

    def post(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any],
        user_agent: str | None = None,
        accepted_business_codes: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        return self.request("POST", endpoint, params=params, body=body, user_agent=user_agent,
                            accepted_business_codes=accepted_business_codes)

    def transfer_client(self) -> httpx.Client:
        """Return the isolated session used for signed transfer URLs."""
        return self._transfer

    def check_request_cooldown(self) -> None:
        """Refuse to open a new storage/origin request while a cooldown is active."""
        self._guard.check_active_cooldown()

    @property
    def guard(self) -> RequestGuard:
        """The shared pacing/cooldown guard; use only its public methods."""
        return self._guard

    def observe_transfer_response(self, response: httpx.Response) -> None:
        """Apply the shared cooldown for 429 / 503+Retry-After transfer responses.

        Storage-level 401/403 never reach a credential-clearing decision here.
        """
        self._guard.observe_transfer_response(response)

    def new_transfer_client(self) -> httpx.Client:
        """Create an isolated signed-URL session for one download worker."""
        storage = httpx.Client(transport=self._transport, timeout=self._transfer.timeout, follow_redirects=False)
        with self._worker_transfers_lock:
            self._worker_transfers.append(storage)
        return storage

    def release_transfer_clients(self, clients: list[httpx.Client]) -> None:
        """Close worker pools that own their transports; injected transports live with this client."""
        if self._transport is not None:
            return
        for storage in clients:
            storage.close()
        with self._worker_transfers_lock:
            self._worker_transfers = [storage for storage in self._worker_transfers if storage not in clients]

    def pds_cookie_header(self, host: str) -> str | None:
        """Return only the required ``__puus`` cookie for a trusted PDS host."""
        normalized_host = host.lower().rstrip(".")
        if re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+pds\.quark\.cn", normalized_host) is None:
            return None
        return self.transfer_cookie_header(normalized_host)

    def transfer_cookie_header(self, host: str) -> str | None:
        """Return only ``__puus`` for Quark PDS or drive storage subdomains."""
        normalized_host = host.lower().rstrip(".")
        pds = re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+pds\.quark\.cn", normalized_host)
        drive = re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+drive\.quark\.cn", normalized_host)
        if pds is None and drive is None:
            return None
        for component in self._cookie.split(";"):
            name, separator, value = component.strip().partition("=")
            if name.strip() == "__puus" and separator and value.strip():
                cookie_value = value.strip()
                if all(0x21 <= ord(char) <= 0x7E and char not in ",;" for char in cookie_value):
                    return f"__puus={cookie_value}"
        return None

    def auth_status(self) -> dict[str, Any]:
        payload = self.get(
            "file/sort",
            params={
                "pdir_fid": "0",
                "_page": 1,
                "_size": 1,
                "_sort": "file_name:asc",
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise RemoteError("认证校验响应缺少 data.list")
        return {"authenticated": True}
