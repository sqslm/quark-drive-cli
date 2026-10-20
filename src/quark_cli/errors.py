"""Public, sanitized errors and their command-line exit codes."""

import math


class QuarkError(Exception):
    exit_code = 4
    label = "remote_error"
    task_id: str | None = None


class UsageError(QuarkError):
    exit_code = 2
    label = "usage_error"


class AuthenticationError(QuarkError):
    exit_code = 3
    label = "authentication_error"


class RemoteError(QuarkError):
    exit_code = 4
    label = "remote_error"
    http_status: int | None = None


class NetworkError(RemoteError):
    label = "network_error"


class RateLimitError(RemoteError):
    """The upstream throttled us (HTTP 429, or a throttling 503/rejection)."""

    label = "rate_limited"

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AccessRestrictedError(RemoteError):
    """The upstream refused access (raw HTTP 403 or an explicit restriction).

    This is deliberately not a credential failure: the Cookie is preserved and
    callers must not clear it or reopen the login flow.
    """

    label = "access_restricted"

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LocalIOError(QuarkError):
    exit_code = 5
    label = "local_io_error"


def error_details(error: QuarkError) -> dict:
    """Stable public fields for CLI errors and per-job failures."""
    result = {"type": error.label, "message": str(error), "exit_code": error.exit_code,
              "next_action": {"authentication_error": "authenticate", "rate_limited": "wait",
                              "access_restricted": "review_access", "local_io_error": "check_local_state"}
                             .get(error.label, "manual_review")}
    delay = getattr(error, "retry_after", None)
    if isinstance(delay, (int, float)) and not isinstance(delay, bool) and math.isfinite(delay) and delay >= 0:
        result["retry_after_seconds"] = delay
    return result


def contextual_error(error: BaseException, message: str) -> QuarkError:
    """Wrap ``error`` with caller context without discarding a cooldown type.

    ``RateLimitError``/``AccessRestrictedError`` keep their type and
    ``retry_after`` so a generic wrapper never hides why requests are paused.
    Every other failure keeps the historical generic ``RemoteError`` context.
    """
    if isinstance(error, (RateLimitError, AccessRestrictedError)):
        original = str(error)
        return type(error)(f"{original}；{message}" if original else message, retry_after=error.retry_after)
    return RemoteError(message)
