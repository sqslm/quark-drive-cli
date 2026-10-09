"""Exact remote paths, verified FID ancestry, and bounded subtree browsing."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import RemoteError, UsageError
from .files import Files


def is_directory(item: Mapping[str, Any]) -> bool:
    kind, directory = item.get("file_type"), item.get("dir")
    if isinstance(kind, bool) or (directory is not None and not isinstance(directory, bool)):
        raise RemoteError("远端文件类型无效")
    if kind in (0, 1) and directory is not None and directory != (kind == 0):
        raise RemoteError("远端目录类型字段不一致")
    return directory is True or kind == 0


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not name or name in (".", "..") or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in name):
        raise UsageError("名称不能为空，也不能包含路径分隔符、.、.. 或控制字符")
    return name


def path_components(path: str) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/") or "\\" in path or "//" in path:
        raise UsageError("路径必须是以 / 开头的网盘绝对路径，且不能包含反斜杠或重复分隔符")
    if path == "/":
        return []
    components = path[1:].split("/")
    for name in components:
        validate_name(name)
    return components


def valid_fid(value: Any) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise RemoteError("远端记录缺少有效 FID")
    fid = str(value)
    if not fid or fid != fid.strip() or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in fid) or fid in (".", ".."):
        raise RemoteError("远端记录缺少有效 FID")
    return fid


@dataclass(frozen=True)
class RemoteObject:
    fid: str
    item: dict[str, Any] | None = None
    path: str | None = None


@dataclass(frozen=True)
class DirectoryTarget:
    """Existing directory or one missing leaf under a verified parent."""

    path: str
    fid: str | None
    parent_fid: str
    name: str

    @property
    def missing(self) -> bool:
        return self.fid is None


class RemotePaths:
    def __init__(self, files: Files) -> None:
        self.files = files

    @staticmethod
    def root() -> RemoteObject:
        return RemoteObject("0", {"fid": "0", "file_name": "/", "file_type": 0}, "/")

    def children(self, fid: str) -> list[dict[str, Any]]:
        records = self.files.list_all(fid)["data"]["list"]
        for item in records:
            child_fid = valid_fid(item.get("fid"))
            if child_fid == "0" or ("pdir_fid" in item and valid_fid(item["pdir_fid"]) != fid):
                raise RemoteError("目录列表的父 FID 与请求不符")
        return records

    def _match(self, fid: str, name: str) -> dict[str, Any] | None:
        matches = [item for item in self.children(fid) if item.get("file_name") == name]
        if len(matches) > 1:
            raise UsageError(f"路径名称“{name}”不唯一，已停止")
        return matches[0] if matches else None

    def resolve(self, ref: str, *, directory: bool = False, inspect_fid: bool = False) -> RemoteObject:
        if not isinstance(ref, str) or not ref:
            raise UsageError("必须指定 FID 或网盘绝对路径")
        if ref in ("/", "0"):
            return self.root()
        if not ref.startswith("/"):
            try:
                fid = valid_fid(ref)
            except RemoteError:
                raise UsageError("FID 或网盘绝对路径无效") from None
            item = self.files.info(fid) if directory or inspect_fid else None
            if directory and not is_directory(item or {}):
                raise UsageError("指定对象不是目录")
            return RemoteObject(fid, item)
        components = path_components(ref)
        current = self.root()
        seen = {"0"}
        for index, name in enumerate(components):
            item = self._match(current.fid, name)
            if item is None:
                raise UsageError(f"路径不存在：{name}")
            fid = valid_fid(item.get("fid"))
            if fid in seen:
                raise RemoteError("路径包含循环 FID")
            seen.add(fid)
            if (index < len(components) - 1 or directory) and not is_directory(item):
                raise UsageError(f"路径组件“{name}”不是目录")
            current = RemoteObject(fid, item, ref)
        return current

    def directory_target(self, path: str, *, allow_missing_leaf: bool = False) -> DirectoryTarget:
        components = path_components(path)
        if not components:
            return DirectoryTarget("/", "0", "0", "")
        parent_path = "/" + "/".join(components[:-1]) if len(components) > 1 else "/"
        try:
            parent = self.resolve(parent_path, directory=True)
        except UsageError as exc:
            raise UsageError(f"目标{exc}") from None
        name = components[-1]
        item = self._match(parent.fid, name)
        if item is None:
            if not allow_missing_leaf:
                raise UsageError(f"目标路径不存在：{name}")
            return DirectoryTarget(path, None, parent.fid, name)
        if not is_directory(item):
            raise UsageError(f"目标路径组件“{name}”不是目录")
        # Resolve the entire existing path with the same cycle checks used by
        # all other references; a leaf can point at any higher ancestor.
        fid = self.resolve(path, directory=True).fid
        return DirectoryTarget(path, fid, parent.fid, name)

    def ancestry(self, fid: str) -> list[RemoteObject]:
        """Verify each info parent against that parent's actual child listing."""
        chain: list[RemoteObject] = []
        seen: set[str] = set()
        current = valid_fid(fid)
        for _ in range(1024):
            if current == "0":
                chain.append(self.root())
                return chain
            if current in seen:
                raise UsageError("父目录链出现循环，无法确认对象范围")
            seen.add(current)
            item = self.files.info(current)
            if chain and not is_directory(item):
                raise UsageError("父目录链包含非目录对象")
            try:
                parent = valid_fid(item.get("pdir_fid"))
            except RemoteError:
                raise UsageError("父目录信息缺失，无法确认对象范围") from None
            if parent == current:
                raise UsageError("父目录链出现循环，无法确认对象范围")
            matches = [child for child in self.children(parent) if valid_fid(child.get("fid")) == current]
            if len(matches) != 1:
                raise UsageError("对象不在其声明的父目录中，无法确认对象范围")
            listed = matches[0]
            if listed.get("file_name") != item.get("file_name") or is_directory(listed) != is_directory(item):
                raise UsageError("对象详情与父目录列表不一致，无法确认对象范围")
            chain.append(RemoteObject(current, item))
            current = parent
        raise UsageError("父目录链超过安全上限，无法确认对象范围")

    def absolute_path(self, fid: str) -> str:
        components = []
        for obj in reversed(self.ancestry(fid)[:-1]):
            try:
                components.append(validate_name((obj.item or {}).get("file_name")))
            except UsageError:
                raise RemoteError("远端名称无法表示为安全绝对路径") from None
        return "/" + "/".join(components)

    def walk(self, ref: str = "/", *, depth: int | None = None) -> Iterator[dict[str, Any]]:
        if depth is not None and (isinstance(depth, bool) or depth < 0 or depth > 1024):
            raise UsageError("depth 必须在 0 到 1024 之间")
        obj = self.resolve(ref, directory=True)
        root_path = obj.path or self.absolute_path(obj.fid)
        stack = [(obj.fid, obj.item or {}, root_path, 0)]
        seen: set[str] = set()
        while stack:
            fid, item, path, level = stack.pop()
            if fid in seen:
                raise RemoteError("遍历发现重复或循环 FID，已停止")
            seen.add(fid)
            if len(seen) > 100000 or level > 1024:
                raise RemoteError("遍历超过安全上限")
            yield {**item, "fid": fid, "path": path, "depth": level, "file_type": 0 if is_directory(item) else 1}
            if not is_directory(item) or (depth is not None and level >= depth):
                continue
            children = self.children(fid)
            for child in reversed(children):
                try:
                    name = validate_name(child.get("file_name"))
                except UsageError:
                    raise RemoteError("远端名称无法表示为安全绝对路径") from None
                child_path = path.rstrip("/") + "/" + name
                stack.append((valid_fid(child.get("fid")), child, child_path, level + 1))


class Scope:
    """One invocation's immutable remote directory boundary."""

    def __init__(self, paths: RemotePaths, ref: str) -> None:
        self.paths = paths
        anchor = paths.resolve(ref, directory=True)
        chain = paths.ancestry(anchor.fid)
        if anchor.fid != "0" and not is_directory(chain[0].item or {}):
            raise UsageError("scope 必须是目录")
        self.fid = anchor.fid

    def check(self, fid: str, *, mutate_object: bool = False, write: bool = False) -> None:
        if write and fid == "0":
            raise UsageError("受限范围内不能写入根目录")
        if mutate_object and fid == self.fid:
            raise UsageError("不能删除、移动或重命名范围锚点")
        chain = self.paths.ancestry(fid)
        if self.fid not in {obj.fid for obj in chain}:
            raise UsageError("对象超出 --scope 限定范围")

    def check_target(self, target: DirectoryTarget) -> None:
        self.check(target.parent_fid if target.missing else target.fid or "0", write=True)
