"""Desktop selections over the existing, verified share protocols."""

from dataclasses import replace

from ..errors import UsageError
from ..paths import valid_fid
from ..shares import ShareSaver
from ..share_management import ShareManager, safe_operation_result
from .file_operations import FileOperations


class Sharing:
    def __init__(self, client):
        self.client = client
        self.manager = ShareManager(client)
        self.files = FileOperations(client)

    def link(self, share_id):
        record = self.manager._own_record(share_id)
        return safe_operation_result(self.manager._password(share_id, record))

    def create(self, entry, *, passcode=None, expire_days=7, progress=None):
        actual, _ = self.files.checked(entry)
        return self.manager.create(
            valid_fid(actual["fid"]),
            passcode=passcode,
            expire_days=expire_days,
            verify_tree=False,
            progress=progress,
        )

    def save(self, source, path, entries, target):
        if not entries:
            raise UsageError("请先勾选分享文件")
        saver = ShareSaver(self.client)
        selection = saver.select(source.url, source=path, passcode=source.passcode)
        members = {str(row["fid"]): row for row in selection.items}
        ids = [valid_fid(row["fid"]) for row in entries]
        if len(set(ids)) != len(ids):
            raise UsageError("重复选择分享成员")
        chosen = []
        for entry, fid in zip(entries, ids):
            row = members.get(fid)
            if (
                row is None
                or row["file_name"] != entry["name"]
                or (row["file_type"] == 0) != entry["directory"]
                or (not entry["directory"] and row.get("size") != entry.get("size"))
            ):
                raise UsageError("分享成员已变化，请重新打开后选择")
            chosen.append(row)
        target = self.files.paths.resolve(str(target), directory=True).fid
        names = [row["file_name"].casefold() for row in chosen]
        if len(set(names)) != len(names):
            raise UsageError("选中分享成员存在同名内容，未转存")
        for row in chosen:
            self.files.available(target, row["file_name"])
        selection = replace(selection, items=chosen)
        task_id = saver._submit_selection(selection, target, created=False)
        task = saver._poll_task(task_id, poll_interval=0.5, max_polls=120)
        saver._confirm_saved_items(selection, target, task_id, task)
        return {
            "saved": True,
            "item_count": len(chosen),
            "destination_fid": target,
            "destination_path": self.files.paths.absolute_path(target),
        }
