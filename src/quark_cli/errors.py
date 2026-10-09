"""Public, sanitized errors and their command-line exit codes."""


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


class NetworkError(RemoteError):
    label = "network_error"


class LocalIOError(QuarkError):
    exit_code = 5
    label = "local_io_error"
