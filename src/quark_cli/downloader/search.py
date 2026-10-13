"""Incremental, cancellable drive search without an eager path lookup per hit."""
from __future__ import annotations

from dataclasses import replace
from collections.abc import Callable
from collections import deque

from ..errors import RemoteError, UsageError
from ..files import Files
from ..paths import RemotePaths, valid_fid, validate_name
from .core import Entry, entry_of, ROOT_FID, ROOT_NAME


class SearchCancelled(Exception):
    pass


def pages(files: Files, read: Callable, stopped: Callable[[], bool]):
    seen: set[str] = set()
    total = None
    for page in range(1, 10002):
        if stopped():
            raise SearchCancelled()
        payload = read(page)
        if stopped():
            raise SearchCancelled()
        rows = payload["data"]["list"]
        meta = payload.get("metadata", {})
        count = payload["data"].get("_total", meta.get("_total") if isinstance(meta, dict) else None)
        if count is not None:
            if type(count) is not int or count < 0 or (total is not None and count != total):
                raise RemoteError("搜索分页总数无效或发生变化，请重新搜索")
            total = count
        for row in rows:
            fid = valid_fid(row.get("fid") if isinstance(row, dict) else None)
            if fid in seen:
                raise RemoteError("搜索分页包含重复 FID")
            seen.add(fid)
        yield rows
        if total is not None and len(seen) >= total:
            if len(seen) != total:
                raise RemoteError("搜索分页超出声明总数")
            return
        if len(rows) < 100:
            if total is not None and len(seen) != total:
                raise RemoteError("搜索分页提前结束")
            return
    raise RemoteError("搜索分页超过安全上限")


def stream(files: Files, fid: str, query: str, recursive: bool,
           report: Callable[[tuple[Entry, ...], int], None], stopped: Callable[[], bool]) -> None:
    query = query.strip()
    if not query:
        raise UsageError("请先输入搜索关键词")
    if not recursive:
        for rows in pages(files, lambda page: files.search_page(query, page), stopped):
            report(tuple(entry_of(row) for row in rows), 0)
        return
    paths = RemotePaths(files)
    origin = paths.ancestry(fid)
    chain = tuple((obj.fid, (obj.item or {}).get("file_name", ROOT_NAME))
                  for obj in reversed(origin))
    # The root item is named '/' in the library, but the UI uses 我的网盘.
    chain = ((ROOT_FID, ROOT_NAME),) + chain[1:]
    root_path = "/" + "/".join(name for _, name in chain[1:])
    pending = deque([(fid, root_path.rstrip("/"), chain)])
    visited: set[str] = set()
    objects = {fid}
    scanned = 0
    while pending:
        if stopped():
            raise SearchCancelled()
        parent, path, location = pending.popleft()
        if parent in visited or len(visited) >= 100000 or len(location) > 1024:
            raise RemoteError("搜索发现循环目录或超过安全上限")
        visited.add(parent)
        for rows in pages(files, lambda page: files.list_page(parent, page, 100), stopped):
            found = []
            for row in rows:
                if "pdir_fid" in row and valid_fid(row["pdir_fid"]) != parent:
                    raise RemoteError("搜索目录列表的父 FID 与请求不符")
                item = entry_of(row)
                if item.fid in objects:
                    raise RemoteError("搜索发现重复或循环 FID")
                objects.add(item.fid)
                validate_name(item.name)
                item_path = path + "/" + item.name
                item_chain = location + ((item.fid, item.name),) if item.directory else location
                item = replace(item, path=item_path, location=item_chain)
                if item.directory:
                    pending.append((item.fid, item_path, item_chain))
                if query.casefold() in item.name.casefold():
                    found.append(item)
            report(tuple(found), scanned)
        scanned += 1
        report((), scanned)
