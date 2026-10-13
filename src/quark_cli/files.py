"""File and directory metadata operations."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .client import QuarkClient
from .errors import RemoteError, UsageError


_LIST_READ_RETRY_DELAYS = (0.25, 0.75)


class _PaginationInconsistency(RemoteError):
    """A list/total snapshot mismatch that may settle after a remote write."""


def _data(payload: Mapping[str, Any], operation: str) -> Any:
    if "data" not in payload:
        raise RemoteError(f"{operation}响应缺少 data 字段")
    return payload["data"]


class Files:
    def __init__(self, client: QuarkClient) -> None:
        self.client = client

    def search_page(self, query: str, page: int = 1, size: int = 100) -> dict[str, Any]:
        if not query.strip() or page < 1 or not 1 <= size <= 100:
            raise UsageError("搜索关键词或分页参数无效")
        payload = self.client.get("file/search", params={"q": query.strip(), "_page": page,
            "_size": size, "_fetch_total": 1, "_sort": "file_name:asc", "_is_hl": 0})
        data = _data(payload, "搜索")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise RemoteError("搜索响应缺少 data.list")
        return payload

    def list_page(self, folder_id: str = "0", page: int = 1, size: int = 50) -> dict[str, Any]:
        if page < 1:
            raise UsageError("page 必须大于 0")
        if size < 1 or size > 100:
            raise UsageError("size 必须在 1 到 100 之间")
        payload = self.client.get(
            "file/sort",
            params={
                "pdir_fid": folder_id,
                "_page": page,
                "_size": size,
                "_fetch_total": 1,
                "_sort": "file_name:asc",
            },
        )
        data = _data(payload, "文件列表")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise RemoteError("文件列表响应缺少 data.list")
        if any(not isinstance(item, dict) for item in data["list"]):
            raise RemoteError("文件列表包含无效记录")
        return payload

    def list_all(self, folder_id: str = "0", page: int = 1, size: int = 50) -> dict[str, Any]:
        """Read one complete snapshot, restarting briefly when totals lag."""
        if page < 1:
            raise UsageError("page 必须大于 0")
        if size < 1 or size > 100:
            raise UsageError("size 必须在 1 到 100 之间")
        for attempt in range(len(_LIST_READ_RETRY_DELAYS) + 1):
            try:
                return self._list_all_once(folder_id, page, size)
            except _PaginationInconsistency as exc:
                if attempt == len(_LIST_READ_RETRY_DELAYS):
                    raise RemoteError(f"{exc}；有限只读重读后仍无法确认完整列表") from None
                time.sleep(_LIST_READ_RETRY_DELAYS[attempt])
        raise AssertionError("unreachable pagination retry state")

    def _list_all_once(self, folder_id: str, page: int, size: int) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        seen_pages: set[tuple[str, ...]] = set()
        seen_fids: set[str] = set()
        total: int | None = None
        current = page
        metadata: dict[str, Any] = {}
        while True:
            payload = self.list_page(folder_id, current, size)
            data = payload["data"]
            batch = data["list"]
            metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
            total_value = data.get("_total", data.get("total", metadata.get("_total", metadata.get("total"))))
            total_changed = False
            if total_value is not None:
                if isinstance(total_value, bool) or not isinstance(total_value, int) or total_value < 0:
                    raise RemoteError("分页总数无效")
                total_changed = total is not None and total != total_value
                total = total_value
            if not batch:
                if total_changed:
                    raise _PaginationInconsistency("分页总数不一致")
                if total is not None and len(files) != max(0, total - (page - 1) * size):
                    raise _PaginationInconsistency("分页提前结束")
                break
            fingerprint = tuple(str(item.get("fid", "")) for item in batch if isinstance(item, dict))
            if fingerprint and fingerprint in seen_pages:
                raise RemoteError("分页响应重复，已停止以避免循环")
            if fingerprint:
                seen_pages.add(fingerprint)
            new_items = []
            for item in batch:
                fid = str(item.get("fid", "")) if isinstance(item, dict) else ""
                if fid and fid in seen_fids:
                    raise RemoteError("分页包含重复 FID")
                if fid:
                    seen_fids.add(fid)
                new_items.append(item)
            if not new_items:
                raise RemoteError("分页没有返回新文件，已停止以避免循环")
            if total_changed:
                raise _PaginationInconsistency("分页总数不一致")
            files.extend(new_items)
            expected = max(0, total - (page - 1) * size) if total is not None else None
            if expected is not None and len(files) >= expected:
                if len(files) != expected:
                    raise _PaginationInconsistency("分页超出声明总数")
                break
            if len(batch) < size:
                if expected is not None:
                    raise _PaginationInconsistency("分页提前结束")
                break
            current += 1
            if current - page > 10000:
                raise RemoteError("分页数量超过安全上限")
        return {"status": 200, "code": 0, "data": {"list": files, "_total": total if total is not None else len(files)}, "metadata": metadata}

    def info(self, fid: str) -> dict[str, Any]:
        if not fid or fid == "0":
            raise UsageError("FID 无效")
        payload = self.client.get("file/info", params={"fid": fid, "_fetch_full_path": 0})
        data = _data(payload, "文件详情")
        if not isinstance(data, dict) or str(data.get("fid", "")) != fid:
            raise RemoteError(f"找不到 FID {fid}")
        return data

    def mkdir(self, name: str, parent: str = "0") -> Any:
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise UsageError("目录名不能为空，也不能包含路径分隔符")
        payload = self.client.post(
            "file",
            body={"pdir_fid": parent, "file_name": name, "dir_init_lock": False, "dir_path": ""},
        )
        return _data(payload, "创建目录")

    def rename(self, fid: str, name: str) -> Any:
        if not fid or fid == "0":
            raise UsageError("FID 无效")
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise UsageError("名称不能为空，也不能包含路径分隔符")
        payload = self.client.post("file/rename", body={"fid": fid, "file_name": name})
        return _data(payload, "重命名")

    def move(self, fid: str, target: str, *, poll_interval: float = 0.5, max_polls: int = 60) -> Any:
        if not fid or fid == "0" or not target:
            raise UsageError("源 FID 或目标目录 FID 无效")
        return self.move_many([fid],target,poll_interval=poll_interval,max_polls=max_polls)

    def move_many(self, fids: list[str], target: str, *, poll_interval: float = 0.5, max_polls: int = 60) -> Any:
        if not fids or any(not fid or fid == '0' for fid in fids) or not target or len(set(fids)) != len(fids):
            raise UsageError('源 FID 列表或目标目录 FID 无效')
        payload = self.client.post(
            "file/move",
            body={"action_type": 1, "to_pdir_fid": target, "filelist": fids, "exclude_fids": []},
        )
        data = _data(payload, "移动文件")
        if not isinstance(data, dict):
            raise RemoteError("移动响应格式无效")
        if data.get("finish") is True:
            return data
        task_id = data.get("task_id")
        if not task_id:
            raise RemoteError("移动响应缺少完成状态或 task_id")
        gap = payload.get("metadata", {}).get("tq_gap") if isinstance(payload.get("metadata"), dict) else None
        delay = max(0.0, min(float(gap) / 1000, 5.0)) if isinstance(gap, (int, float)) else poll_interval
        for retry_index in range(max_polls):
            if delay:
                time.sleep(delay)
            task_payload = self.client.get("task", params={"task_id": task_id, "retry_index": retry_index})
            task = _data(task_payload, "移动任务")
            if not isinstance(task, dict):
                raise RemoteError("移动任务响应格式无效")
            state = task.get("status")
            if state in ('0','1','2','3','4'):
                state = int(state)
            if state == 2 or task.get("finish") is True:
                return task
            if state == 3:
                raise RemoteError("夸克拒绝了移动操作")
            if state == 4:
                raise RemoteError("移动任务已暂停，请核实结果；未重新提交")
        raise RemoteError("等待移动任务超时")

    def delete(self, fid: str) -> Any:
        if not fid or fid == "0":
            raise UsageError("FID 无效")
        return self.delete_many([fid])

    def delete_many(self, fids: list[str]) -> Any:
        if not fids or any(not fid or fid == "0" for fid in fids) or len(set(fids)) != len(fids):
            raise UsageError("FID 列表无效")
        payload = self.client.post(
            "file/delete",
            body={"action_type": 2, "filelist": fids, "exclude_fids": []},
        )
        return _data(payload, "删除")
