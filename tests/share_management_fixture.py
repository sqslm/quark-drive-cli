"""Independent own-share protocol fixture with safe, synthetic content."""

import copy
import json

import httpx

from drive_fixture import DriveFixture, item


class ManagementFixture(DriveFixture):
    def __init__(self):
        super().__init__()
        self.catalog = []
        self.passwords = {}
        self.members = {}
        self.owner = 1
        self.creates = []
        self.deletes = []
        self.task_data = {"status": 2, "share_id": "own-created"}
        self.submit_data = {"task_id": "create-task"}
        self.submit_snapshot = None
        self.catalog_transform = lambda records, page: records
        self.detail_transform = lambda records, parent: records
        self.password_transform = lambda data: data
        self.after_create = lambda: None
        self.delete_delay = 0
        self.delete_reads = 0
        self.delete_timeout = False
        self.submit_timeout = False
        self.task_timeout = False
        self.password_error = False
        self.delete_ids = set()

    @property
    def writes(self):
        return [request for request in self.calls if request.method == "POST"
                and request.url.path.endswith(("/share", "/share/delete"))]

    def add_share(self, share_id="own-share", roots=("file",), *, passcode="", expired_type=3):
        pwd_id = f"public-{share_id}"
        data = {"pwd_id": pwd_id,
                "share_url": f"https://pan.quark.cn/s/{pwd_id}", "title": f"标题 {share_id}",
                "url_type": 2 if passcode else 1, "expired_type": expired_type, "passcode": passcode}
        self.passwords[share_id] = data
        self.catalog.append({**data, "share_id": share_id, "created_at": 123, "first_file": {"fid": roots[0] if roots else ""},
                             "stoken": "catalog-secret", "nested": {"cookie": "nested-secret"}})
        roots = [copy.deepcopy(next(entry for entry in self.items if entry["fid"] == fid)) for fid in roots]
        directories = {"0": roots}
        stack = list(roots)
        while stack:
            root = stack.pop()
            if root["file_type"] == 0:
                children = [copy.deepcopy(entry) for entry in self.items if entry["pdir_fid"] == root["fid"]]
                directories[root["fid"]] = children
                stack.extend(children)
        for records in directories.values():
            for entry in records:
                entry.update({"share_fid_token": "member-secret", "download_url": "https://invalid/?signature=private"})
        self.members[pwd_id] = directories
        return data

    def handler(self, request):
        path = request.url.path
        if not path.endswith(("/share", "/share/password", "/share/mypage/detail", "/share/delete",
                              "/share/sharepage/token", "/share/sharepage/detail", "/task")):
            return super().handler(request)
        self.calls.append(request)
        if path.endswith("/share/mypage/detail"):
            if self.delete_ids:
                self.delete_reads += 1
                if self.delete_reads > self.delete_delay:
                    self.catalog = [record for record in self.catalog if record["share_id"] not in self.delete_ids]
            page, size = int(request.url.params["_page"]), int(request.url.params["_size"])
            records = self.catalog_transform(copy.deepcopy(self.catalog), page)
            start = (page - 1) * size
            total = len(records) if request.url.params.get("_fetch_total") == "1" else 0
            return self.response(request, {"list": records[start:start + size]}, total=total)
        if path.endswith("/share/password"):
            if self.password_error:
                return httpx.Response(500, json={"message": "password-secret member-secret"}, request=request)
            data = self.passwords[json.loads(request.content)["share_id"]]
            return self.response(request, self.password_transform(copy.deepcopy(data)))
        if path.endswith("/share/sharepage/token"):
            body = json.loads(request.content)
            password = next(record for record in self.passwords.values() if record["pwd_id"] == body["pwd_id"])
            assert body["passcode"] == password["passcode"]
            return self.response(request, {"stoken": "owner-token-secret"})
        if path.endswith("/share/sharepage/detail"):
            parent = request.url.params["pdir_fid"]
            records = self.detail_transform(copy.deepcopy(self.members[request.url.params["pwd_id"]].get(parent, [])), parent)
            page, size = int(request.url.params["_page"]), int(request.url.params["_size"])
            start = (page - 1) * size
            owner = self.owner(page) if callable(self.owner) else self.owner
            return self.response(request, {"list": records[start:start + size], "is_owner": owner}, total=len(records))
        if path.endswith("/share/delete"):
            body = json.loads(request.content)
            self.deletes.append(body)
            self.delete_ids.update(body["share_ids"])
            if self.delete_timeout:
                raise httpx.ReadTimeout("delete-secret", request=request)
            return self.response(request, {})
        if path.endswith("/share"):
            body = json.loads(request.content)
            self.creates.append(body)
            data = self.add_share("own-created", body["fid_list"], passcode=body.get("passcode", ""),
                                  expired_type=body["expired_type"])
            # The real service computes the title from the source object.
            data["title"] = next(entry["file_name"] for entry in self.items if entry["fid"] == body["fid_list"][0])
            self.catalog[-1]["title"] = data["title"]
            self.after_create()
            if self.submit_timeout:
                raise httpx.ReadTimeout("submit-secret", request=request)
            response = copy.deepcopy(self.submit_data)
            if self.submit_snapshot is not None:
                response["task_resp"] = {"data": {"creation_snapshot": self.submit_snapshot}}
            return self.response(request, response)
        if path.endswith("/task"):
            if self.task_timeout:
                raise httpx.ReadTimeout("task-secret", request=request)
            return self.response(request, copy.deepcopy(self.task_data))
        raise AssertionError("Unexpected own-share request")

    @staticmethod
    def response(request, data, *, total=None):
        payload = {"status": 200, "code": 0, "data": data}
        if total is not None:
            payload["metadata"] = {"_total": total}
        return httpx.Response(200, json=payload, request=request)
