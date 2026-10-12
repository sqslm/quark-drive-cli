"""One background thread that browses and downloads for the window.

The window never performs I/O: it puts a :class:`Browse`, a :class:`BrowseShare`
(a share folder, read-only), a :class:`Search` or a :class:`Download` request on
a queue and receives :class:`Listing`, :class:`ShareListing`, :class:`Progress`,
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
from dataclasses import dataclass, replace
from typing import Any, Protocol

import httpx

from ..client import QuarkClient
from ..errors import RemoteError, UsageError
from ..files import Files
from ..paths import RemotePaths, is_directory
from ..recursive import RecursiveTransfer
from ..shares import ShareSaver
from ..transfer import DEFAULT_DOWNLOAD_CONNECTIONS, DownloadStopped, Transfer
from ..transfer_progress import ProgressEvent
from .core import (
    BROWSE_ACTION,
    DOWNLOAD_ACTION,
    ROOT_FID,
    ROOT_NAME,
    SEARCH_ACTION,
    Entry,
    describe_error,
    entries,
    entry_of,
    progress_fraction,
    progress_text,
    summary_text,
)
from .share import ShareDownloader, ShareSource

SHARE_BROWSE_ACTION = "读取分享"


@dataclass(frozen=True)
class Browse:
    """Read one folder's complete listing."""

    fid: str


@dataclass(frozen=True)
class BrowseShare:
    """Read one folder of a share link, without saving anything."""

    source: ShareSource
    path: str = "/"


@dataclass(frozen=True)
class Search:
    """Find every name matching ``query`` under ``fid`` and its subfolders."""

    fid: str
    query: str


@dataclass(frozen=True)
class Download:
    """Download one file, or one folder tree, to an already-planned target.

    ``share_source`` is set only for a selection inside a share link: the item
    is transferred into a temporary drive folder and downloaded from there, and
    the temporary copy is removed again when the request ends. ``fid`` then
    identifies the item inside the share and ``share_path`` is the share folder
    that holds it. An ordinary account download leaves both at their defaults
    and behaves exactly as before.
    """

    fid: str
    target: str
    recursive: bool
    overwrite: bool
    name: str
    share_source: ShareSource | None = None
    share_path: str = "/"
    connections: int = DEFAULT_DOWNLOAD_CONNECTIONS


@dataclass(frozen=True)
class Listing:
    fid: str
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class ShareListing:
    """One folder of a share link, as rows the window can show.

    ``path`` is the share folder that was read and every row's ``path`` is its
    absolute path inside the share. ``location`` stays empty: share rows live in
    the share's own namespace, not in the account's folder chain.
    """

    source: ShareSource
    path: str
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class SearchResults:
    """Every hit of one :class:`Search`, each carrying its own drive location."""

    fid: str
    query: str
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
    resume_available: bool = True


class Backend(Protocol):
    """The library calls the window drives; injectable for offline tests."""

    def listing(self, fid: str) -> list[Entry]: ...

    def share_listing(self, source: ShareSource, path: str) -> list[Entry]: ...

    def search(self, fid: str, query: str) -> list[Entry]: ...

    def download(
        self,
        request: Download,
        progress: Callable[[ProgressEvent], None] | None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Mapping[str, Any]: ...


class DriveBackend:
    """Browsing, searching and downloading through the project library, one client per request.

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

    def share_listing(self, source: ShareSource, path: str) -> list[Entry]:
        """Read one share folder, read-only: nothing is saved and nothing changes.

        The rows carry the share's own FIDs, so a later download re-reads the
        share and matches its selection by that FID. Each row's ``path`` is its
        absolute path inside the share, which is what the download request needs
        as the source folder.
        """
        with self._client() as client:
            payload = ShareSaver(client).inspect(source.url, path=path, passcode=source.passcode)
        parent = path.rstrip("/") if path not in ("", "/") else ""
        return [replace(row, path=f"{parent}/{row.name}") for row in entries(payload["items"])]

    def search(self, fid: str, query: str) -> list[Entry]:
        """Find matches by name under ``fid``, each with its verified location.

        The walk is the library's existing bounded subtree traversal, so paging,
        repeated/cyclic FIDs and malformed names are refused by the same rules as
        every other consumer. The scanned folder itself (depth 0) is never a hit:
        "search inside" means its contents. Same-name objects in different
        folders are all kept, each sorted directories-first and then by absolute
        path, and every hit carries the real ``(fid, name)`` chain of the folder
        that holds it, taken from the verified ancestry above ``fid`` and the
        directories the walk actually visited — never a guessed or re-searched
        parent.
        """
        keyword = query.strip()
        if not keyword:
            raise UsageError("请先输入搜索关键词")
        needle = keyword.casefold()
        with self._client() as client:
            paths = RemotePaths(Files(client))
            directories = self._search_directories(paths, fid)
            found: list[Entry] = []
            for record in paths.walk(fid):
                name = record.get("file_name")
                directory = is_directory(record)
                if directory and record["path"] != "/":
                    directories[record["path"]] = (record["fid"], name)
                if record["depth"] == 0 or not isinstance(name, str):
                    continue
                if needle not in name.casefold():
                    continue
                containing = record["path"] if directory else _parent_path(record["path"])
                found.append(
                    replace(
                        entry_of(record),
                        path=record["path"],
                        location=_directory_chain(directories, containing),
                    )
                )
        found.sort(key=lambda entry: (not entry.directory, entry.path.casefold()))
        return found

    @staticmethod
    def _search_directories(paths: RemotePaths, fid: str) -> dict[str, tuple[str, str]]:
        """Verified ``path -> (fid, name)`` of every folder at or above ``fid``.

        The folders above the searched one come from the library's ancestry walk,
        which checks each parent against its actual child listing, so the prefix
        of every hit's location is established by data, not by a name lookup.
        """
        directories: dict[str, tuple[str, str]] = {"/": (ROOT_FID, ROOT_NAME)}
        path = ""
        for obj in reversed(paths.ancestry(fid)[:-1]):
            name = (obj.item or {}).get("file_name")
            if not isinstance(name, str) or not name:
                raise RemoteError("无法确认搜索结果所在目录")
            path = f"{path}/{name}"
            directories[path] = (obj.fid, name)
        return directories

    def download(
        self,
        request: Download,
        progress: Callable[[ProgressEvent], None] | None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {"overwrite": request.overwrite, "resume": True,
                                  "connections": request.connections}
        if progress is not None:
            options["progress"] = progress
        if should_stop is not None:
            options["should_stop"] = should_stop
        with self._client() as client:
            if request.share_source is not None:
                # A share item is not an account FID: it cannot be handed to the
                # account download or the transfer library directly, and its
                # content may not stay in the drive, so it goes through the
                # temporary-copy path instead.
                if request.overwrite:
                    raise UsageError("分享下载不覆盖已有目标")
                return ShareDownloader(client).download(
                    request.share_source,
                    fid=request.fid,
                    name=request.name,
                    target=request.target,
                    recursive=request.recursive,
                    share_path=request.share_path,
                    progress=progress,
                    should_stop=should_stop,
                    connections=request.connections,
                )
            transfer = Transfer(client)
            if request.recursive:
                return RecursiveTransfer(client, transfer=transfer).download_tree(
                    request.fid, request.target, **options
                )
            return transfer.download(request.fid, request.target, **options)


def _parent_path(path: str) -> str:
    """The folder that holds ``path``; ``/`` for an object in the drive root."""
    cut = path.rfind("/")
    return path[:cut] if cut > 0 else "/"


def _directory_chain(directories: Mapping[str, tuple[str, str]], path: str) -> tuple[tuple[str, str], ...]:
    """The root-down ``(fid, name)`` chain of one drive folder path.

    Every component must already be known from a verified ancestry or from a
    visited walk directory; a missing one means the walk and the path disagree,
    so it is reported instead of inventing a parent FID.
    """
    chain: list[tuple[str, str]] = [directories["/"]]
    names = [] if path == "/" else path[1:].split("/")
    current = ""
    for name in names:
        current = f"{current}/{name}"
        try:
            chain.append(directories[current])
        except KeyError:
            raise RemoteError("无法确认搜索结果所在目录") from None
    return tuple(chain)


class Worker:
    """Serialize :class:`Browse`/:class:`BrowseShare`/:class:`Search`/:class:`Download` work onto one daemon thread."""

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

    def submit(self, request: Browse | BrowseShare | Search | Download) -> None:
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
            elif isinstance(request, BrowseShare):
                self._browse_share(request)
            elif isinstance(request, Search):
                self._search(request)
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

    def _browse_share(self, request: BrowseShare) -> None:
        try:
            listing = self._backend.share_listing(request.source, request.path)
        except Exception as error:
            # A share that cannot be read is a read failure like any other: the
            # link, its passcode and the share token never reach the window.
            self.events.put(Failed(SHARE_BROWSE_ACTION, describe_error(error, SHARE_BROWSE_ACTION)))
            return
        self.events.put(ShareListing(request.source, request.path, tuple(listing)))

    def _search(self, request: Search) -> None:
        try:
            found = self._backend.search(request.fid, request.query)
        except Exception as error:
            # A failed search is reported like any other read failure: it never
            # turns into a download completion or a stop, and the worker keeps
            # serving the next request.
            self.events.put(Failed(SEARCH_ACTION, describe_error(error, SEARCH_ACTION)))
            return
        self.events.put(SearchResults(request.fid, request.query, tuple(found)))

    def _download(self, request: Download) -> None:
        def report(event: ProgressEvent) -> None:
            self.events.put(Progress(progress_text(event), progress_fraction(event)))

        try:
            result = self._backend.download(request, report, self._stop.is_set)
        except DownloadStopped as stopped:
            # The library ended at a checkpoint it had already persisted, so the
            # outcome is a stop, not a failure: nothing was published, nothing
            # is reset, and the same request continues from there.
            message = (
                "分享下载已停止：临时内容已清理，需重新下载"
                if request.share_source is not None
                else str(stopped) or "下载已停止：安全检查点已保留"
            )
            self.events.put(Stopped(DOWNLOAD_ACTION, message,
                                   request.share_source is None and stopped.resume_available))
            return
        except Exception as error:
            self.events.put(Failed(DOWNLOAD_ACTION, describe_error(error, DOWNLOAD_ACTION)))
            return
        self.events.put(Finished(DOWNLOAD_ACTION, summary_text(result, request.name)))
