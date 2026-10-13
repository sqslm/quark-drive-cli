"""Small desktop adapters around existing verified file operations."""

from ..errors import RemoteError, UsageError
from ..files import Files
from ..paths import RemotePaths, is_directory, valid_fid, validate_name
from ..shares import _optional_created_fid, ShareSaver
from ..downloader.core import describe_error


class FileOperations:
    def __init__(self, client):
        self.files = Files(client)
        self.paths = RemotePaths(self.files)
        self.client = client

    def checked(self, entry):
        fid = valid_fid(entry["fid"])
        if fid == "0":
            raise UsageError("不能修改账户根目录")
        chain = self.paths.ancestry(fid)
        actual = chain[0].item
        if (
            actual.get("file_name") != entry["name"]
            or is_directory(actual) != entry["directory"]
        ):
            raise UsageError("选中对象已变化，请刷新后重新选择")
        if (
            not is_directory(actual)
            and entry.get("size") is not None
            and actual.get("size") != entry["size"]
        ):
            raise UsageError("选中文件大小已变化，请刷新后重新选择")
        return actual, chain

    def available(self, parent, name, excluding=None):
        validate_name(name)
        rows = self.paths.children(parent)
        if any(
            row.get("file_name", "").casefold() == name.casefold()
            and str(row.get("fid")) != excluding
            for row in rows
        ):
            raise UsageError("目标目录存在同名文件或目录，未覆盖")

    def info(self, entry):
        actual, chain = self.checked(entry)
        safe = {
            key: actual[key]
            for key in (
                "fid",
                "file_name",
                "size",
                "pdir_fid",
                "updated_at",
                "created_at",
            )
            if key in actual and isinstance(actual[key], (str, int, float, bool))
        }
        safe["directory"] = is_directory(actual)
        safe["path"] = "/" + "/".join(
            obj.item["file_name"] for obj in reversed(chain[:-1])
        )
        return safe

    def mkdir(self, parent, name):
        parent = self.paths.resolve(parent, directory=True).fid
        self.available(parent, name)
        result = self.files.mkdir(name, parent)
        fid = _optional_created_fid(result)
        if fid is None:
            raise RemoteError(
                "创建目录已提交，但响应缺少 FID；结果未确认，请刷新查看，不要直接重复创建"
            )
        ShareSaver(self.client)._confirm_created_directory(parent, name, fid)
        return {"fid": fid, "name": name, "state": "done"}

    def apply(self, operation, entries, *, name=None, target=None):
        if operation not in ("rename", "move", "delete"):
            raise UsageError("不支持的文件操作")
        if operation == "rename" and len(entries) != 1:
            raise UsageError("重命名只允许选择一项")
        target_chain = None
        if operation == "move":
            target = self.paths.resolve(str(target), directory=True).fid
            target_chain = self.paths.ancestry(target)
        results, seen = [], set()
        for entry in entries:
            try:
                actual, _chain = self.checked(entry)
                fid = valid_fid(actual["fid"])
                if fid in seen:
                    raise UsageError("重复选择同一对象")
                seen.add(fid)
                if operation == "rename":
                    self.available(valid_fid(actual["pdir_fid"]), name, excluding=fid)
                    self.files.rename(fid, name)
                    after = self.files.info(fid)
                    if after.get("file_name") != name or after.get(
                        "pdir_fid"
                    ) != actual.get("pdir_fid"):
                        raise RemoteError("重命名已提交，但结果未确认，请刷新查看")
                elif operation == "move":
                    if any(obj.fid == fid for obj in target_chain):
                        raise UsageError("不能移动到自身或子目录")
                    self.available(target, actual["file_name"], excluding=fid)
                    self.files.move(fid, target)
                    after = self.files.info(fid)
                    if (
                        str(after.get("pdir_fid")) != target
                        or after.get("file_name") != actual["file_name"]
                    ):
                        raise RemoteError("移动已提交，但结果未确认，请刷新查看")
                else:
                    self.files.delete(fid)
                    if any(str(row.get("fid")) == fid for row in self.paths.children(str(actual["pdir_fid"]))):
                        raise RemoteError("删除已提交，但结果未确认，请刷新查看")
                results.append(
                    {
                        "fid": fid,
                        "name": entry["name"],
                        "state": "done",
                        "detail": "已移入回收站"
                        if operation == "delete"
                        else "操作完成",
                    }
                )
            except Exception as error:
                results.append(
                    {
                        "fid": entry.get("fid"),
                        "name": entry.get("name", ""),
                        "state": "failed",
                        "detail": describe_error(error, "文件操作"),
                    }
                )
        return results
