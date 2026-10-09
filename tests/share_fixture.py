"""Protocol fixture for paginated share selections and single-submit saves."""

import json

import httpx

from drive_fixture import DriveFixture, item


SHARE_URL = "https://pan.quark.cn/s/offline-share"
SOURCE_PATH = "/公开源/中文 子目录"


def shared(fid, name, parent, *, directory=False, size=4):
    return item(fid, name, parent, directory=directory, size=size, updated_at=123,
                share_fid_token=f"private-token-{fid}",
                download_url="https://example.invalid/?signature=private-signature",
                stoken="private-nested-stoken", auth_info={"token": "private-auth"})


class ShareFixture(DriveFixture):
    def __init__(self):
        super().__init__()
        self.share_directories = {
            # A shared root retains the owner's real account parent FID.
            "0": [shared("source-root", "公开源", "owner-account-parent", directory=True)],
            "source-root": [shared("source-child", "中文 子目录", "source-root", directory=True),
                            shared("root-note", "root-note.txt", "source-root")],
            "source-child": [shared("note", "note.txt", "source-child"),
                             shared("skip", "skip.log", "source-child"),
                             shared("folder", "资料", "source-child", directory=True),
                             shared("upper", "UPPER.TXT", "source-child")],
            "folder": [shared("deep", "inside.txt", "folder")],
        }
        self.saves = []
        self.token_bodies = []
        self.done = False
        self.destination = None
        self.saved_read_count = 0
        self.saved_read_delay = 0
        self.result_transform = lambda records: records
        self.task_fids = None
        self.task_target = None
        self.detail_error = False
        self.tasks = {}

    def handler(self, request):
        path = request.url.path
        if path.endswith("/file/sort") and self.done and request.url.params["pdir_fid"] == self.destination:
            self.saved_read_count += 1
            if self.saved_read_count <= self.saved_read_delay:
                self.calls.append(request)
                return self.response(request, {"list": [], "_total": 0})
        if not path.endswith(("/share/sharepage/token", "/share/sharepage/detail", "/share/sharepage/save", "/task")):
            return super().handler(request)
        self.calls.append(request)
        if path.endswith("/share/sharepage/token"):
            self.token_bodies.append(json.loads(request.content))
            return self.response(request, {"stoken": "stoken-private"})
        if path.endswith("/share/sharepage/detail"):
            if self.detail_error:
                return httpx.Response(500, json={"status": 500, "code": 1,
                    "message": "stoken-private private-signature private-passcode"}, request=request)
            # Missing this flag produces the real API's placeholder total.
            if request.url.params.get("_fetch_total") != "1":
                return self.response(request, {"list": self.share_directories["0"]}, total=0)
            parent = request.url.params["pdir_fid"]
            records = self.share_directories.get(parent, [])
            page, size = int(request.url.params["_page"]), int(request.url.params["_size"])
            offset = (page - 1) * size
            return self.response(request, {"list": records[offset:offset + size]}, total=len(records))
        if path.endswith("/share/sharepage/save"):
            body = json.loads(request.content)
            self.saves.append(body)
            self.destination = body["to_pdir_fid"]
            self.done = False
            task_id = "selection-task" if len(self.saves) == 1 else f"selection-task-{len(self.saves)}"
            self.tasks[task_id] = {"body": body, "done": False, "number": len(self.saves)}
            return self.response(request, {"task_id": task_id})
        if path.endswith("/task"):
            task = self.tasks[request.url.params["task_id"]]
            body = task["body"]
            self.destination = body["to_pdir_fid"]
            sources = {entry["fid"]: entry for entry in self.share_directories[body["pdir_fid"]]}
            suffix = "" if task["number"] == 1 else f"{task['number']}-"
            def clone(source, parent):
                return item(f"saved-{suffix}{source['fid']}", source["file_name"], parent,
                            directory=source["file_type"] == 0, size=source["size"], updated_at=source["updated_at"])
            records = [clone(sources[fid], self.destination) for fid in body["fid_list"]]
            all_records = list(records)
            stack = [(sources[fid], records[index]) for index, fid in enumerate(body["fid_list"])]
            while stack:
                source, target = stack.pop()
                if source["file_type"] == 0:
                    for child in self.share_directories.get(source["fid"], []):
                        copied = clone(child, target["fid"])
                        all_records.append(copied)
                        stack.append((child, copied))
            if not task["done"]:
                self.items.extend(self.result_transform(all_records))
            task["done"] = True
            self.done = True
            return self.response(request, {"status": 2, "save_as": {
                "to_pdir_fid": self.task_target or self.destination,
                "save_as_top_fids": self.task_fids if self.task_fids is not None else [entry["fid"] for entry in reversed(records)],
            }})
        raise AssertionError("Unexpected fixture request")

    @staticmethod
    def response(request, data, *, total=None):
        payload = {"status": 200, "code": 0, "data": data}
        if total is not None:
            payload["metadata"] = {"_total": total}
        return httpx.Response(200, json=payload, request=request)
