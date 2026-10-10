"""One background thread that browses and downloads for the window.

The window never performs I/O: it puts a :class:`Browse` or :class:`Download`
request on a queue and receives :class:`Listing`, :class:`Progress`,
:class:`Finished` or :class:`Failed` events back. Requests are served one at a
time, so a download can never race a folder listing or another download, and
every library error becomes one readable line instead of an exception that would
kill the worker thread silently.

Downloads are not cancellable: the library publishes a file atomically, so an
aborted transfer never leaves a partial target file, and the window therefore
refuses to close while one is running instead of cutting the process off
underneath it (a forced kill can still leave the library's temporary part file).
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from ..client import QuarkClient
from ..errors import QuarkError
from ..files import Files
from ..recursive import RecursiveTransfer
from ..transfer import Transfer
from ..transfer_progress import ProgressEvent
from .core import (
    BROWSE_ACTION,
    DOWNLOAD_ACTION,
    Entry,
    describe_error,
    entries,
    progress_fraction,
    progress_text,
    summary_text,
)


@dataclass(frozen=True)
class Browse:
    """Read one folder's complete listing."""

    fid: str


@dataclass(frozen=True)
class Download:
    """Download one file, or one folder tree, to an already-planned target."""

    fid: str
    target: str
    recursive: bool
    overwrite: bool
    name: str


@dataclass(frozen=True)
class Listing:
    fid: str
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class Progress:
    text: str
    fraction: float | None


@dataclass(frozen=True)
class Finished:
    action: str
    summary: str


@dataclass(frozen=True)
class Failed:
    action: str
    message: str


class Backend(Protocol):
    """The library calls the window drives; injectable for offline tests."""

    def listing(self, fid: str) -> list[Entry]: ...

    def download(
        self, request: Download, progress: Callable[[ProgressEvent], None] | None
    ) -> Mapping[str, Any]: ...


class DriveBackend:
    """Browsing and downloading through the project library, one client per request.

    A client is built per request from the current credential, so a login
    performed while the window is open is picked up without any session
    bookkeeping here. The download path is the CLI's: the same request guard,
    the same existing-file refusal, the same atomic publish, and the same
    durable tree state under ``QUARK_CONFIG_DIR``.
    """

    def __init__(
        self,
        cookie: Callable[[], str],
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._cookie = cookie
        self._transport = transport

    def _client(self) -> QuarkClient:
        return QuarkClient(self._cookie(), transport=self._transport)

    def listing(self, fid: str) -> list[Entry]:
        with self._client() as client:
            payload = Files(client).list_all(fid)
        return entries(payload["data"]["list"])

    def download(
        self, request: Download, progress: Callable[[ProgressEvent], None] | None
    ) -> dict[str, Any]:
        options: dict[str, Any] = {"overwrite": request.overwrite}
        if progress is not None:
            options["progress"] = progress
        with self._client() as client:
            transfer = Transfer(client)
            if request.recursive:
                return RecursiveTransfer(client, transfer=transfer).download_tree(
                    request.fid, request.target, **options
                )
            return transfer.download(request.fid, request.target, **options)


class Worker:
    """Serialize :class:`Browse`/:class:`Download` work onto one daemon thread."""

    def __init__(self, backend: Backend, *, events: "queue.Queue[Any]" | None = None) -> None:
        self._backend = backend
        self.events: queue.Queue[Any] = events if events is not None else queue.Queue()
        self._requests: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._serve, name="quark-downloader", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the worker to finish after the current request; bounded join.

        A running download is not interrupted: if it outlasts ``timeout`` the
        daemon thread simply ends with the process, and the library's atomic
        publish means no half-written target file is left behind.
        """
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._requests.put(None)
        thread.join(timeout)

    def submit(self, request: Browse | Download) -> None:
        self._requests.put(request)

    def _serve(self) -> None:
        while True:
            request = self._requests.get()
            if request is None:
                return
            if isinstance(request, Browse):
                self._browse(request)
            else:
                self._download(request)

    def _browse(self, request: Browse) -> None:
        try:
            listing = self._backend.listing(request.fid)
        except Exception as error:
            # Every failure type maps through one rule; an unexpected exception
            # never kills the worker and never prints its own text in the window.
            self.events.put(Failed(BROWSE_ACTION, describe_error(error, BROWSE_ACTION)))
            return
        self.events.put(Listing(request.fid, tuple(listing)))

    def _download(self, request: Download) -> None:
        def report(event: ProgressEvent) -> None:
            self.events.put(Progress(progress_text(event), progress_fraction(event)))

        try:
            result = self._backend.download(request, report)
        except Exception as error:
            self.events.put(Failed(DOWNLOAD_ACTION, describe_error(error, DOWNLOAD_ACTION)))
            return
        self.events.put(Finished(DOWNLOAD_ACTION, summary_text(result, request.name)))
