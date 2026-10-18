"""Per-playback sessions and the on-demand byte-range proxy to Quark.

Nothing here mirrors a file to disk or prefetches beyond the requested range:
each `GET` forwards the player's range and streams the response through.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from ..client import QuarkClient
from ..errors import AccessRestrictedError, RemoteError
from ..files import Files
from .buffering import BufferBudget
from ..request_guard import REASON_ACCESS_RESTRICTED
from ..transfer import download_address, successor_url, trusted_url

MAX_SESSIONS = 128
REDIRECT_LIMIT = 6
# A repeated denial after one fresh signed-URL refetch pauses new media
# requests briefly so the player cannot hammer the same failing URL.
DENIED_COOLDOWN_SECONDS = 60.0


class RangeNotSatisfiable(Exception):
    """The player asked for bytes this object cannot supply."""


class MediaChanged(RemoteError):
    """The remote object no longer matches the session the player started."""


def content_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Return the inclusive byte range to serve, or ``None`` for the whole object.

    A malformed, multi-part, or unsatisfiable request raises
    :class:`RangeNotSatisfiable`; the caller answers ``416`` with the real size.
    """
    if value is None or not value.strip():
        return None
    spec = value.strip()
    if not spec.casefold().startswith("bytes="):
        raise RangeNotSatisfiable()
    spec = spec[len("bytes="):].strip()
    if "," in spec:
        raise RangeNotSatisfiable()
    first, separator, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    if not first and not last:
        raise RangeNotSatisfiable()
    if not separator or (first and not first.isdigit()) or (last and not last.isdigit()):
        raise RangeNotSatisfiable()
    if not first:
        suffix = int(last)
        if suffix == 0 or size == 0:
            raise RangeNotSatisfiable()
        return (max(0, size - suffix), size - 1)
    start = int(first)
    if start >= size:
        raise RangeNotSatisfiable()
    if not last:
        return (start, size - 1)
    end = int(last)
    if end < start:
        raise RangeNotSatisfiable()
    return (start, min(end, size - 1))


@dataclass
class PlaySession:
    """State the player does not get to see: the current signed transfer URL."""

    session: str
    fid: str
    name: str
    size: int
    url: str
    created: float
    last_used: float
    etag: str | None = None
    #: Set by ``Sessions.remove_history`` once ``/v1/recent/remove`` dropped this
    #: FID's history, so a later range GET on the *same* session cannot record it
    #: again. A brand-new session (a new explicit ``/v1/play``) starts ``False``.
    history_removed: bool = False
    bookmark_id: str | None = None
    parent: str = ''
    parent_name: str = ''
    context: dict[str, str] = field(default_factory=dict)
    buffer_budget: BufferBudget = field(default_factory=BufferBudget)


@dataclass(frozen=True)
class Media:
    fid: str
    name: str
    size: int
    url: str


def resolve(files: Files, client: QuarkClient, fid: str) -> Media:
    """Fetch a private signed URL plus the identity the player will stream."""
    address = download_address(client, fid)
    name, size = address.name, address.size
    if name is None or size is None:
        info = files.info(fid)
        if name is None:
            candidate = info.get("file_name")
            if not isinstance(candidate, str) or not candidate:
                raise RemoteError("文件详情缺少有效文件名")
            name = candidate
        if size is None:
            candidate = info.get("size")
            if not isinstance(candidate, int) or isinstance(candidate, bool) or candidate < 0:
                raise RemoteError("文件详情缺少有效大小")
            size = candidate
    return Media(fid=fid, name=name, size=size, url=address.url)


class Sessions:
    """Bounded in-memory registry of play sessions; no media bytes are retained."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, PlaySession] = {}

    def create(self, media: Media) -> PlaySession:
        now = self._clock()
        entry = PlaySession(
            session=secrets.token_urlsafe(32),
            fid=media.fid,
            name=media.name,
            size=media.size,
            url=media.url,
            created=now,
            last_used=now,
        )
        with self._lock:
            if len(self._items) >= MAX_SESSIONS:
                self._items.pop(min(self._items.values(), key=lambda item: item.last_used).session, None)
            self._items[entry.session] = entry
        return entry

    def get(self, session: str) -> PlaySession | None:
        with self._lock:
            entry = self._items.get(session)
            if entry is not None:
                entry.last_used = self._clock()
            return entry

    def refresh(self, session: str, media: Media) -> None:
        with self._lock:
            entry = self._items.get(session)
            if entry is None:
                return
            entry.url = media.url
            entry.last_used = self._clock()

    def pin_etag(self, session: str, etag: str) -> bool:
        """Atomically bind the first strong ETag seen to a session.

        Returns ``False`` only when a *different* strong ETag arrives, so the
        caller can abort the session as version-changed. Concurrent first ranges
        serialize on this pin, so a same-size replacement cannot slip past.
        """
        with self._lock:
            entry = self._items.get(session)
            if entry is None:
                return True
            if entry.etag is None:
                entry.etag = etag
                return True
            return entry.etag == etag

    def forget(self, session: str) -> None:
        with self._lock:
            self._items.pop(session, None)

    def remove_history(self, fid: str) -> None:
        """Mark every live session of ``fid`` so it can never record history again.

        Bounded by the session registry itself: a fresh session from a new
        explicit ``/v1/play`` is never marked, so no unbounded tombstone list is
        kept and a re-play records normally.
        """
        with self._lock:
            for entry in self._items.values():
                if entry.fid == fid:
                    entry.history_removed = True


class _Upstream:
    """One streaming GET with trusted redirect hops, closed on context exit.

    Every stream owns a fresh transfer session, so concurrent ranges cannot race
    on a shared cookie jar; only the required ``__puus`` cookie is ever sent, and
    the account Cookie is never installed.
    """

    def __init__(
        self,
        client: QuarkClient,
        url: str,
        headers: dict[str, str],
        on_origin: Callable[[int, int], None] | None = None,
    ) -> None:
        self._client = client
        self._url = url
        self._headers = headers
        self._on_origin = on_origin
        self._stack: ExitStack | None = None
        self._storage: httpx.Client | None = None

    def __enter__(self) -> httpx.Response:
        stack = self._stack = ExitStack()
        storage = self._storage = self._client.new_transfer_client()
        try:
            current = self._url
            for _ in range(REDIRECT_LIMIT):
                # No byte pacing: only refuse to open a new storage request or
                # follow a redirect while a shared cooldown is active.
                self._client.check_request_cooldown()
                current = trusted_url(current)
                headers = dict(self._headers)
                cookie = self._client.transfer_cookie_header(urlsplit(current).hostname or "")
                if cookie is not None:
                    headers["Cookie"] = cookie
                started = time.monotonic()
                response = stack.enter_context(storage.stream("GET", current, headers=headers))
                # Report every real origin response (including a redirect or a
                # 429/403) before the guard classifies it; no URL is involved.
                if self._on_origin is not None:
                    self._on_origin(response.status_code, max(0, int((time.monotonic() - started) * 1000)))
                self._client.observe_transfer_response(response)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise RemoteError("下载地址重定向无效")
                    current = successor_url(current, location)
                    continue
                return response
            raise RemoteError("下载地址重定向次数过多")
        except BaseException:
            self._release()
            raise

    def _release(self) -> None:
        stack, self._stack = self._stack, None
        storage, self._storage = self._storage, None
        if stack is not None:
            stack.close()
        if storage is not None:
            self._client.release_transfer_clients([storage])

    def __exit__(self, *exc_info: object) -> None:
        self._release()


@contextmanager
def open_content(
    client: QuarkClient,
    session: PlaySession,
    byte_range: tuple[int, int] | None,
    refetch: Callable[[], None],
    *,
    on_origin: Callable[[int, int], None] | None = None,
) -> Iterator[httpx.Response]:
    """Stream one range, retrying once with a freshly signed URL after an auth failure.

    ``on_origin`` (optional) is called with ``(status, elapsed_ms)`` for every
    real origin response, including redirects and statuses the guard rejects.
    """
    headers = {"Accept-Encoding": "identity"}
    if byte_range is not None:
        headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
    for attempt in (0, 1):
        with _Upstream(client, session.url, headers, on_origin) as response:
            if response.status_code not in (401, 403):
                yield response
                return
            if attempt == 0:
                refetch()
                continue
            break
    # A fresh signed URL was refused too: pause new media requests briefly so a
    # player cannot hammer the same failing range/URL, then stop with a
    # sanitized, non-credential error. The Cookie is deliberately preserved.
    cooldown = client.guard.record_cooldown(DENIED_COOLDOWN_SECONDS, REASON_ACCESS_RESTRICTED)
    remaining = max(0.0, cooldown.until - client.guard.now())
    raise AccessRestrictedError(
        f"取链失效且重新获取后仍无法播放，已暂停 {max(0, int(remaining + 0.999))} 秒的新播放请求；"
        "请在官方客户端确认账号状态后重试",
        retry_after=remaining,
    )
