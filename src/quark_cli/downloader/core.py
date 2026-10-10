"""Headless rules behind the standalone downloader window.

Nothing here imports Tkinter. Listing rows, folder navigation, the local target
of a download, progress text and the user-facing error line are plain values, so
they are covered by tests without a display. The window in
:mod:`quark_cli.downloader.app` only renders what these functions return.

Byte and rate formatting is the same rule the PotPlayer status window uses
(``status_gui.format_bytes``/``format_speed``): one unit convention for every
front-end instead of a second one here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import LocalIOError, QuarkError, RemoteError
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
    """

    fid: str
    name: str
    directory: bool
    size: int | None = None

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

    def reset(self) -> None:
        del self._chain[1:]


@dataclass(frozen=True)
class Plan:
    """The validated local target of one download."""

    fid: str
    target: Path
    recursive: bool


def download_plan(output_dir: str, entry: Entry) -> Plan:
    """Validate the chosen output folder and name the local target.

    A folder selection downloads *into* a new folder of the same name, exactly
    like ``quark download --recursive``. The name is checked with the library's
    own Windows-safe rule; unsafe names fail before any request is made.
    """
    if not output_dir.strip():
        raise LocalIOError("请先选择本地输出目录")
    folder = Path(output_dir).expanduser()
    if not folder.is_dir():
        raise LocalIOError("本地输出目录不存在或不是目录")
    return Plan(entry.fid, folder / safe_transfer_name(entry.name), entry.directory)


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
