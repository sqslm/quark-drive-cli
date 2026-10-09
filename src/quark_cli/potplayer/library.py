"""Directory pages and natural-sorted playlists built on the project's Files API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import RemoteError, UsageError
from ..files import Files
from ..paths import is_directory, valid_fid
from .naming import is_video, order_key

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
PLAYLIST_PAGE_SIZE = 100


def require_fid(value: Any, *, label: str, allow_root: bool = False) -> str:
    """Validate a caller-supplied FID, reporting caller mistakes as usage errors."""
    try:
        fid = valid_fid(value)
    except RemoteError:
        raise UsageError(f"{label} 必须是有效 FID") from None
    if not allow_root and fid == "0":
        raise UsageError(f"{label} 不能是根目录")
    return fid


def _record(item: Mapping[str, Any]) -> dict[str, Any]:
    fid = valid_fid(item.get("fid"))
    name = item.get("file_name")
    if not isinstance(name, str) or not name:
        raise RemoteError("远端记录缺少有效文件名")
    directory = is_directory(item)
    if directory:
        size = 0
    else:
        raw = item.get("size")
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise RemoteError("远端文件缺少有效大小")
        size = raw
    return {"fid": fid, "name": name, "is_dir": directory, "size": size}


def _total(payload: Mapping[str, Any]) -> int | None:
    data = payload.get("data")
    metadata = payload.get("metadata")
    for source in (data if isinstance(data, Mapping) else {}, metadata if isinstance(metadata, Mapping) else {}):
        value = source.get("_total", source.get("total"))
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def page(files: Files, parent: str, page_number: int, size: int) -> dict[str, Any]:
    """Return one remote page exactly as the server orders it."""
    payload = files.list_page(parent, page_number, size)
    data = payload["data"]
    items = [_record(item) for item in data["list"]]
    total = _total(payload)
    if total is None:
        has_more = len(items) >= size
    else:
        has_more = (page_number - 1) * size + len(items) < total
    return {"items": items, "page": page_number, "has_more": has_more}


def playlist(files: Files, parent: str | None, start: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Return same-directory videos in natural order; ``start`` trims to its tail.

    ``parent`` may be omitted for a file FID, in which case the containing
    directory is read from the file itself before ordering.
    """
    if parent is None:
        if start is None:
            raise UsageError("需要 parent 或 start")
        info = files.info(start)
        parent = require_fid(info.get("pdir_fid"), label="pdir_fid", allow_root=True)
    videos = _videos(files, parent)
    if start is None:
        return parent, videos
    position = next((index for index, entry in enumerate(videos) if entry["fid"] == start), None)
    if position is None:
        raise UsageError("start 不是该目录中的视频文件")
    return parent, videos[position:]


def _videos(files: Files, parent: str) -> list[dict[str, Any]]:
    snapshot = files.list_all(parent, 1, PLAYLIST_PAGE_SIZE)
    entries = [_record(item) for item in snapshot["data"]["list"] if not is_directory(item)]
    videos = [entry for entry in entries if is_video(entry["name"])]
    videos.sort(key=order_key)
    return videos
