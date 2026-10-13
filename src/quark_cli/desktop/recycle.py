"""Exact recycle records, with one-shot writes and readback verification."""

import time

from ..errors import RemoteError, UsageError
from ..paths import valid_fid, is_directory, validate_name
from ..downloader.share import _published_total
from ..downloader.core import describe_error
from .file_operations import FileOperations


class Recycle:
    def __init__(self, client):
        self.client = client
        self.files = FileOperations(client)

    def listing(self):
        records, seen, expected = [], set(), None
        for page in range(1, 10001):
            payload = self.client.get(
                "file/recycle/list", params={"_page": page, "_size": 100}
            )
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise RemoteError("回收站列表响应无效")
            batch = data["list"]
            if len(batch) > 100:
                raise RemoteError("回收站页大小无效")
            for row in batch:
                record_id = valid_fid(row.get("record_id"))
                if record_id in seen:
                    raise RemoteError("回收站分页出现重复记录")
                seen.add(record_id)
                name = validate_name(row.get("file_name"))
                directory = is_directory(row)
                size = row.get("size")
                if not directory and (type(size) is not int or size < 0):
                    raise RemoteError("回收站文件大小无效")
                record = {
                    "record_id": record_id,
                    "fid": valid_fid(row.get("fid")),
                    "name": name,
                    "directory": directory,
                    "size": None if directory else size,
                    "parent": valid_fid(row["pdir_fid"])
                    if row.get("pdir_fid") is not None
                    else None,
                }
                for field in ("deleted_at", "expired_at"):
                    value = row.get(field)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        record[field] = value
                records.append(record)
            total = _published_total(payload, data)
            if total is not None:
                if expected is not None and total != expected:
                    raise RemoteError("回收站分页总数变化，请刷新")
                expected = total
            if expected is not None and len(records) > expected:
                raise RemoteError("回收站记录数与总数不符")
            if len(batch) < 100 or len(records) == expected:
                if expected is not None and len(records) != expected:
                    raise RemoteError("回收站分页提前结束")
                return records
        raise RemoteError("回收站分页超过上限")

    def apply(self, operation, selected):
        if operation not in ("restore", "remove") or not selected:
            raise UsageError("请明确选择回收站记录")
        results, seen = [], set()
        for entry in selected:
            submitted = False
            try:
                record_id = valid_fid(entry["record_id"])
                if record_id in seen:
                    raise UsageError("重复选择回收站记录")
                seen.add(record_id)
                records = self.listing()
                actual = next(
                    (row for row in records if row["record_id"] == record_id), None
                )
                if actual is None or any(
                    actual.get(key) != entry.get(key)
                    for key in ("fid", "name", "directory", "size", "parent")
                ):
                    raise UsageError("回收站记录已变化，请刷新后选择")
                if operation == "restore":
                    if (
                        actual["parent"] is None
                        or sum(row["fid"] == actual["fid"] for row in records) != 1
                    ):
                        raise UsageError(
                            "无法核实原目录或唯一文件身份，请使用官方客户端恢复"
                        )
                    parent = self.files.paths.resolve(
                        actual["parent"], directory=True
                    ).fid
                    self.files.available(parent, actual["name"])
                    # Recover uses exact FIDs; refuse ambiguous multiple recycle records.
                    endpoint, body = (
                        "file/recycle/recover",
                        {"fid_list": [actual["fid"]]},
                    )
                else:
                    endpoint, body = (
                        "file/recycle/remove",
                        {"select_mode": 2, "record_list": [record_id]},
                    )
                submitted = True
                self.client.post(endpoint, body=body)
                for attempt in range(6):
                    if not any(row["record_id"] == record_id for row in self.listing()):
                        if operation == "restore":
                            row = self.files.files.info(actual["fid"])
                            if (
                                row.get("file_name") != actual["name"]
                                or str(row.get("pdir_fid")) != parent
                                or is_directory(row) != actual["directory"]
                                or (
                                    not actual["directory"]
                                    and row.get("size") != actual["size"]
                                )
                            ):
                                raise RemoteError(
                                    "恢复后的对象与原记录不符，结果未确认"
                                )
                        break
                    if attempt < 5:
                        time.sleep(0.5)
                else:
                    raise RemoteError("已提交，但回收站结果尚未确认")
                results.append(
                    {
                        "record_id": record_id,
                        "name": entry["name"],
                        "state": "done",
                        "detail": "已恢复到原目录"
                        if operation == "restore"
                        else "已永久删除",
                    }
                )
            except Exception as error:
                results.append(
                    {
                        "record_id": entry.get("record_id"),
                        "name": entry.get("name", ""),
                        "state": "unknown" if submitted else "failed",
                        "detail": describe_error(error, "回收站操作")
                        + ("；已提交，未重发，请刷新核实" if submitted else ""),
                    }
                )
        return results
