"""Headless rules behind the standalone downloader window.

Nothing here imports Tkinter. Listing rows, folder navigation, the frozen
selection of one click, the local target of a download, progress text and the
user-facing error line are plain values, so they are covered by tests without a
display. The window in :mod:`quark_cli.downloader.app` only renders what these
functions return.

Byte and rate formatting is the same rule the PotPlayer status window uses
(``status_gui.format_bytes``/``format_speed``): one unit convention for every
front-end instead of a second one here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from ..errors import LocalIOError, QuarkError, RemoteError, UsageError
from ..paths import is_directory, valid_fid
from ..potplayer.status_gui import format_bytes, format_speed
from ..transfer import safe_transfer_name
from ..transfer_progress import ProgressEvent

ROOT_FID = "0"
ROOT_NAME = "我的网盘"
DIRECTORY_LABEL = "目录"
FILE_LABEL = "文件"
UNKNOWN_BYTES = "—"

BROWSE_ACTION = "浏览"
DOWNLOAD_ACTION = "下载"
SEARCH_ACTION = "搜索"

SEARCH_KINDS = ("all", "files", "directories")


def display_text(value: Any) -> str:
    """Single-line cell text: control characters can never disturb the window."""
    return "".join(
        " " if ord(char) < 32 or ord(char) == 127 else char
        for char in str(value if value is not None else "")
    )


@dataclass(frozen=True)
class Entry:
    """One child of the folder the window shows.

    ``name`` is the remote name exactly as the API returned it: it is the name
    the local file gets, so it is validated when the download is planned rather
    than sanitized for display here.

    ``path`` and ``location`` are filled in by search results only: an ordinary
    folder listing leaves them at their defaults, because a listing row already
    knows the folder it was read from. ``path`` is the drive absolute path of the
    remote object and ``location`` is the full ``(fid, name)`` chain from the
    root down to (and including) the folder that holds it, so the window can
    show where a hit lives and jump there without re-deriving a parent FID.
    """

    fid: str
    name: str
    directory: bool
    size: int | None = None
    path: str = ""
    location: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        return display_text(self.name)

    @property
    def type_label(self) -> str:
        return DIRECTORY_LABEL if self.directory else FILE_LABEL

    @property
    def size_label(self) -> str:
        return UNKNOWN_BYTES if self.directory else format_bytes(self.size)

    @property
    def row(self) -> tuple[str, str, str]:
        return (self.label, self.type_label, self.size_label)


def entry_of(record: Mapping[str, Any]) -> Entry:
    """One listing record as a row; malformed records fail the whole listing."""
    fid = valid_fid(record.get("fid"))
    name = record.get("file_name")
    if not isinstance(name, str) or not name:
        raise RemoteError("远端记录缺少有效名称")
    directory = is_directory(record)
    size = record.get("size")
    if directory or type(size) is not int or size < 0:
        size = None
    return Entry(fid, name, directory, size)


def entries(records: Iterable[Mapping[str, Any]]) -> list[Entry]:
    """Listing rows: folders first, then names, both case-insensitively."""
    rows = [entry_of(record) for record in records]
    rows.sort(key=lambda entry: (not entry.directory, entry.name.casefold()))
    return rows


def filter_entries(rows: Iterable[Entry], query: str = "", kind: str = "all") -> list[Entry]:
    """Local narrowing of an already-read list: name substring and object kind.

    The query is stripped and matched case-insensitively as a substring of the
    remote name; an empty query narrows by kind only. Input order is kept, so the
    window can filter a listing or a set of search hits without re-sorting it.
    ``kind`` is one of :data:`SEARCH_KINDS`; anything else is a usage error
    rather than a silently ignored filter.
    """
    if kind not in SEARCH_KINDS:
        raise UsageError("筛选类型必须是 all、files 或 directories")
    if not isinstance(query, str):
        raise UsageError("搜索关键词必须是文本")
    needle = query.strip().casefold()
    selected: list[Entry] = []
    for entry in rows:
        if kind == "files" and entry.directory:
            continue
        if kind == "directories" and not entry.directory:
            continue
        if needle and needle not in entry.name.casefold():
            continue
        selected.append(entry)
    return selected


class Location:
    """Which folder the window shows, built from the folders it entered.

    The breadcrumb is this chain rather than a remote ancestry walk: entering a
    folder already established every name above it, so navigating never costs an
    extra request.
    """

    def __init__(self) -> None:
        self._chain: list[tuple[str, str]] = [(ROOT_FID, ROOT_NAME)]

    @property
    def fid(self) -> str:
        return self._chain[-1][0]

    @property
    def title(self) -> str:
        if len(self._chain) == 1:
            return "/"
        return "/" + "/".join(display_text(name) for _, name in self._chain[1:])

    def enter(self, fid: str, name: str) -> None:
        self._chain.append((valid_fid(fid), name))

    def up(self) -> bool:
        """Leave the current folder; ``False`` at the drive root."""
        if len(self._chain) == 1:
            return False
        self._chain.pop()
        return True

    def replace(self, chain: Iterable[tuple[str, str]]) -> None:
        """Jump to a folder described by a full root-down ``(fid, name)`` chain.

        Search results carry the real chain of the folder they were found in, so
        the window restores that exact place instead of guessing a parent FID: a
        chain that does not start at the drive root, or that carries a malformed
        FID or a repeated root, is refused and leaves the current folder alone.
        """
        try:
            items = [(fid, name) for fid, name in chain]
        except (TypeError, ValueError):
            raise UsageError("定位链必须是 (FID, 名称) 序列") from None
        if not items or items[0] != (ROOT_FID, ROOT_NAME):
            raise UsageError("定位链必须从网盘根开始")
        rest: list[tuple[str, str]] = []
        for fid, name in items[1:]:
            if not isinstance(name, str) or not name:
                raise UsageError("定位链包含无效名称")
            if valid_fid(fid) == ROOT_FID:
                raise UsageError("定位链包含重复的网盘根")
            rest.append((fid, name))
        self._chain = [(ROOT_FID, ROOT_NAME), *rest]

    def reset(self) -> None:
        del self._chain[1:]


@dataclass(frozen=True)
class Plan:
    """The validated local target of one download."""

    fid: str
    target: Path
    recursive: bool


def output_folder(output_dir: str) -> Path:
    """Validate the chosen output folder once, before any item is planned."""
    if not output_dir.strip():
        raise LocalIOError("请先选择本地输出目录")
    folder = Path(output_dir).expanduser()
    if not folder.is_dir():
        raise LocalIOError("本地输出目录不存在或不是目录")
    return folder


def download_plan(output_dir: str, entry: Entry) -> Plan:
    """Validate the chosen output folder and name the local target.

    A folder selection downloads *into* a new folder of the same name, exactly
    like ``quark download --recursive``. The name is checked with the library's
    own Windows-safe rule; unsafe names fail before any request is made.
    """
    folder = output_folder(output_dir)
    return Plan(entry.fid, folder / safe_transfer_name(entry.name), entry.directory)


class Outcome(Enum):
    """How the item in flight ended, as the worker reported it.

    ``STOPPED`` is its own outcome: a transfer the user ended at a persisted
    checkpoint neither published a file (not a success) nor failed (not an
    error), so counting it either way would misreport the click.
    """

    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"


class Batch:
    """One click's frozen selection: the entries in list order, one at a time.

    The item in flight stays :attr:`current` until the worker reports its
    outcome, so :meth:`take` never hands out the same entry twice and a stop
    between two items can neither lose nor double-count one. Planning stays per
    item: an entry the library refuses fails alone while the rest continue.
    Counting is by outcome, so the closing line can always name what really
    happened to every selected entry.
    """

    def __init__(self, output_dir: str, entries: Iterable[Entry]) -> None:
        self.output_dir = output_dir
        self.entries = tuple(entries)
        self._pending = list(self.entries)
        self.current: Entry | None = None
        self.succeeded = 0
        self.failed = 0
        self.stopped = 0
        self.skipped = 0

    @property
    def total(self) -> int:
        """How many entries this click selected."""
        return len(self.entries)

    @property
    def position(self) -> int:
        """The 1-based number of the item in flight, or of the next one."""
        return self.succeeded + self.failed + self.stopped + 1

    def take(self) -> Entry | None:
        """The next entry in list order, or ``None`` while one is already out."""
        if self.current is not None or not self._pending:
            return None
        self.current = self._pending.pop(0)
        return self.current

    def settle(self, outcome: Outcome) -> None:
        """Count the item in flight by how it ended; the worker reported it."""
        if self.current is None:
            return
        if outcome is Outcome.DONE:
            self.succeeded += 1
        elif outcome is Outcome.STOPPED:
            self.stopped += 1
        else:
            self.failed += 1
        self.current = None

    def clear(self) -> int:
        """Drop every entry not yet started; the item in flight stays current.

        The dropped entries stay counted as skipped, so a stop still reports how
        many were cancelled even though they are gone from the queue.
        """
        dropped = len(self._pending)
        self._pending.clear()
        self.skipped += dropped
        return dropped


def item_status(position: int, total: int, text: str) -> str:
    """One batch line: which item of how many, then the item's own text."""
    return f"（{position}/{total}）{text}"


def batch_summary(*, succeeded: int, failed: int, stopped: int, skipped: int) -> str:
    """The closing line of one click: totals, and what a stop left behind.

    A stop is named only when it did something: an item ended at its checkpoint
    or one was cancelled before it started. A stop that arrived after the last
    item had already published changed nothing, so the click is reported as the
    completion it turned out to be, and a click that only failed is never
    reported as completed.
    """
    if stopped or skipped:
        parts = [f"成功 {succeeded} 项"]
        if failed:
            parts.append(f"失败 {failed} 项")
        if stopped:
            parts.append(f"{stopped} 项已停止（检查点已保留）")
        if skipped:
            parts.append(f"{skipped} 项未开始")
        return "已停止：" + "，".join(parts)
    if failed:
        return f"下载结束：共 {succeeded + failed} 项，成功 {succeeded} 项，失败 {failed} 项"
    return f"全部下载完成：共 {succeeded} 项"


def progress_text(event: ProgressEvent) -> str:
    """One status line for a transfer event; never contains a URL or a Cookie."""
    name = display_text(event.name)
    if event.total is None:
        return f"{name}：已传输 {format_bytes(event.transferred)} · {format_speed(event.rate)}"
    return (
        f"{name}：{format_bytes(event.transferred)} / {format_bytes(event.total)}"
        f" · {format_speed(event.rate)}"
    )


def progress_fraction(event: ProgressEvent) -> float | None:
    """Confirmed progress as 0.0-1.0, or ``None`` while the size is unknown."""
    total, done = event.total, event.transferred
    if type(total) is not int or total <= 0 or type(done) is not int or done < 0:
        return None
    return min(1.0, done / total)


def summary_text(result: Mapping[str, Any], name: str) -> str:
    """The completion line for a finished download result."""
    if result.get("recursive"):
        return (
            f"已下载目录 {display_text(result.get('path'))}："
            f"{_count(result.get('files'))} 个文件，{_count(result.get('directories'))} 个目录，"
            f"{format_bytes(result.get('size'))}"
        )
    return f"已下载 {display_text(name)}：{format_bytes(result.get('size'))}"


def _count(value: Any) -> str:
    return str(value) if type(value) is int and value >= 0 else "?"


def describe_error(error: BaseException, action: str) -> str:
    """One readable line for a failure, without ever echoing a credential.

    Library errors already carry a safe, redacted Chinese message; anything else
    is reported generically instead of leaking a raw exception through the
    window.
    """
    if isinstance(error, QuarkError):
        detail = str(error).strip()
    elif isinstance(error, OSError):
        detail = "本地文件操作失败"
    else:
        detail = "发生未预期的错误"
    return f"{action}失败：{detail}" if detail else f"{action}失败"
