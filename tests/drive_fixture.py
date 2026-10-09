"""In-memory remote drive for path and scope behavioral regression tests."""

import json

import httpx


def item(fid, name, parent="0", *, directory=False, **fields):
    return {"fid": fid, "file_name": name, "pdir_fid": parent, "file_type": 0 if directory else 1, **fields}


class DriveFixture:
    def __init__(self, records=None):
        self.items = records if records is not None else [
            item("scope", "范围", directory=True),
            item("child", "中文 空格", "scope", directory=True),
            item("file", "报告.PDF", "child", size=4),
            item("target", "目标", "scope", directory=True),
            item("outside", "范围外", directory=True),
            item("outside-file", "别人的.txt", "outside", size=2),
        ]
        self.calls = []
        self.info_overrides = {}
        self.list_overrides = {}
        self.share_items = [{"fid": "shared", "file_name": "公开测试.txt", "share_fid_token": "share-token-private"}]

    @property
    def writes(self):
        return [request for request in self.calls if request.method != "GET" and not request.url.path.endswith("/share/sharepage/token")]

    def handler(self, request):
        self.calls.append(request)
        path = request.url.path
        data = {}
        if path.endswith("/file/info"):
            fid = request.url.params["fid"]
            data = self.info_overrides.get(fid, next((entry for entry in self.items if entry["fid"] == fid), {}))
        elif path.endswith("/file/sort"):
            parent = request.url.params["pdir_fid"]
            children = self.list_overrides.get(parent, [entry for entry in self.items if entry.get("pdir_fid") == parent])
            page, size = int(request.url.params["_page"]), int(request.url.params["_size"])
            offset = (page - 1) * size
            data = {"list": children[offset:offset + size], "_total": len(children)}
        elif path.endswith("/share/sharepage/token"):
            data = {"stoken": "stoken-private"}
        elif path.endswith("/share/sharepage/detail"):
            data = {"list": self.share_items, "_total": len(self.share_items)}
        elif path.endswith("/file") and request.method == "POST":
            body = json.loads(request.read())
            created = item("new-folder", body["file_name"], body["pdir_fid"], directory=True)
            self.items.append(created)
            data = created
        elif path.endswith(("/file/move", "/file/rename", "/file/delete")):
            data = {"finish": True, "task_id": "safe-task"}
        else:
            return httpx.Response(404, request=request)
        return httpx.Response(200, json={"status": 200, "code": 0, "data": data}, request=request)

    def transport(self):
        return httpx.MockTransport(self.handler)
