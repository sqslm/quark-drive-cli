"""One background thread that browses and downloads for the window.

The window never performs I/O: it puts a :class:`Browse` or :class:`Download`
request on a queue and receives :class:`Listing`, :class:`Progress`,
:class:`Finished`, :class:`Failed` or :class:`Stopped` events back. Requests are
served one at a time, so a download can never race a folder listing or another
download, and every library error becomes one readable line instead of an
exception that would kill the worker thread silently.

A download is not cut off mid-stream: the library publishes a file atomically, so
an aborted transfer never leaves a partial target file. :meth:`Worker.request_stop`
therefore asks the library to end at the checkpoint it has already persisted
(:class:`~quark_cli.transfer.DownloadStopped`), which is reported as
:class:`Stopped`: the part file and ledger stay exactly as written so the same
request resumes, and the window still refuses to close until that outcome
arrives. A file that finished publishing before the stop took effect is reported
by its own :class:`Finished`, never retroactively as a stop.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from ..client import QuarkClient
from ..files import Files
from ..recursive import RecursiveTransfer
from ..transfer import DownloadStopped, Transfer
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


@dataclass(frozen=True)
class Stopped:
    """One download ended at a durable checkpoint because the window asked it to.

    The target is unpublished and the library's part file and ledger are
    untouched, so the same request can be submitted again to continue.
    """

    action: str
    message: str


class Backend(Protocol):
    """The library calls the window drives; injectable for offline tests."""

    def listing(self, fid: str) -> list[Entry]: ...

    def download(
        self,
        request: Download,
        progress: Callable[[ProgressEvent], None] | None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Mapping[str, Any]: ...


class DriveBackend:
    """Browsing and downloading through the project library, one client per request.

    A client is built per request from the current credential, so a login
    performed while the window is open is picked up without any session
    bookkeeping here. The download path is the CLI's: the same request guard,
    the same existing-file refusal, the same atomic publish, and the same
    durable tree state under ``QUARK_CONFIG_DIR``. Every download is submitted
    with ``resume=True``, which is what makes a stopped transfer continue from
    its checkpoint and what gives the library a safe point to stop at.
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
        self,
        request: Download,
        progress: Callable[[ProgressEvent], None] | None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {"overwrite": request.overwrite, "resume": True}
        if progress is not None:
            options["progress"] = progress
        if should_stop is not None:
            options["should_stop"] = should_stop
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
        # The stop request belongs to the batch the window is running: it is set
        # by the Tk thread while a download is in flight and cleared by the
        # window when the next batch starts, so a stop can never leak into a
        # later click while it still ends the item it was aimed at.
        self._stop = threading.Event()

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

    def request_stop(self) -> None:
        """Ask the download in flight to end at its persisted checkpoint.

        Safe to call from the Tk thread at any time: the library reads it before
        the first byte and after every durable checkpoint, and the request ends
        with :class:`Stopped` instead of a failure line.
        """
        self._stop.set()

    def clear_stop(self) -> None:
        """Drop a previous stop request; the window calls this as a batch starts."""
        self._stop.clear()

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
            result = self._backend.download(request, report, self._stop.is_set)
        except DownloadStopped as stopped:
            # The library ended at a checkpoint it had already persisted, so the
            # outcome is a stop, not a failure: nothing was published, nothing
            # is reset, and the same request continues from there.
            self.events.put(Stopped(DOWNLOAD_ACTION, str(stopped) or "下载已停止：安全检查点已保留"))
            return
        except Exception as error:
            self.events.put(Failed(DOWNLOAD_ACTION, describe_error(error, DOWNLOAD_ACTION)))
            return
        self.events.put(Finished(DOWNLOAD_ACTION, summary_text(result, request.name)))
