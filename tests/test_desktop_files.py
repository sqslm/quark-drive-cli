"""File management checks exact selections and refuses conflicting targets."""

import json
import unittest

import httpx

from quark_cli.client import QuarkClient
from quark_cli.desktop.file_operations import FileOperations
from quark_cli.errors import UsageError
from tests.test_downloader_worker import COOKIE, item


class Drive:
    def __init__(self, records):
        self.records = {row["fid"]: dict(row) for row in records}
        self.writes = []

    def transport(self):
        def handle(request):
            path = request.url.path
            if request.method == "GET" and path.endswith("file/info"):
                data = self.records[request.url.params["fid"]]
            elif request.method == "GET" and path.endswith("file/sort"):
                rows = [
                    row
                    for row in self.records.values()
                    if row["pdir_fid"] == request.url.params["pdir_fid"]
                ]
                data = {"list": rows, "_total": len(rows)}
            else:
                body = json.loads(request.content)
                self.writes.append((path, body))
                if path.endswith("file/rename"):
                    self.records[body["fid"]]["file_name"] = body["file_name"]
                    data = {}
                elif path.endswith("file/move"):
                    self.records[body["filelist"][0]]["pdir_fid"] = body["to_pdir_fid"]
                    data = {"finish": True}
                elif path.endswith("file/delete"):
                    self.records.pop(body["filelist"][0])
                    data = {}
                elif path.endswith("/file"):
                    self.records["new"] = item(
                        "new", body["file_name"], body["pdir_fid"], directory=True
                    )
                    data = {"fid": "new"}
                else:
                    raise AssertionError(path)
            return httpx.Response(200, json={"status": 200, "code": 0, "data": data})

        return httpx.MockTransport(handle)


def entry(fid, name, directory=False, size=None):
    return {"fid": fid, "name": name, "directory": directory, "size": size}


class FileTests(unittest.TestCase):
    def operations(self, drive):
        client = QuarkClient(COOKIE, transport=drive.transport())
        self.addCleanup(client.close)
        return FileOperations(client)

    def test_conflicting_casefold_name_never_submits_rename(self):
        drive = Drive([item("a", "A.txt", size=3), item("b", "B.txt", size=4)])
        result = self.operations(drive).apply(
            "rename", [entry("a", "A.txt", size=3)], name="b.TXT"
        )
        self.assertEqual(result[0]["state"], "failed")
        self.assertIn("同名", result[0]["detail"])
        self.assertEqual(drive.writes, [])

    def test_batch_delete_is_exact_and_reports_partial_failure(self):
        drive = Drive(
            [
                item("a", "A.txt", size=3),
                item("b", "Changed.txt", size=4),
                item("keep", "Keep.txt"),
            ]
        )
        result = self.operations(drive).apply(
            "delete", [entry("a", "A.txt", size=3), entry("b", "B.txt", size=4)]
        )
        self.assertEqual([row["state"] for row in result], ["done", "failed"])
        self.assertEqual(drive.writes[0][1]["filelist"], ["a"])
        self.assertIn("keep", drive.records)
        self.assertIn("b", drive.records)

    def test_move_into_descendant_never_submits(self):
        drive = Drive(
            [item("a", "A", directory=True), item("sub", "Sub", "a", directory=True)]
        )
        result = self.operations(drive).apply(
            "move", [entry("a", "A", True)], target="sub"
        )
        self.assertIn("子目录", result[0]["detail"])
        self.assertEqual(drive.writes, [])

    def test_rename_move_and_property_return_verified_paths(self):
        drive = Drive(
            [item("a", "A.txt", size=3), item("folder", "目标", directory=True)]
        )
        ops = self.operations(drive)
        self.assertEqual(
            ops.apply("rename", [entry("a", "A.txt", size=3)], name="新名称.txt")[0][
                "state"
            ],
            "done",
        )
        self.assertEqual(
            ops.apply("move", [entry("a", "新名称.txt", size=3)], target="folder")[0][
                "state"
            ],
            "done",
        )
        self.assertEqual(
            ops.info(entry("a", "新名称.txt", size=3))["path"], "/目标/新名称.txt"
        )

    def test_mkdir_conflict_refuses_and_success_confirms_new_fid(self):
        drive = Drive([item("folder", "目标", directory=True)])
        ops = self.operations(drive)
        with self.assertRaises(UsageError):
            ops.mkdir("0", "目标")
        self.assertEqual(drive.writes, [])
        self.assertEqual(ops.mkdir("folder", "新建目录")["fid"], "new")
        self.assertEqual(drive.records["new"]["pdir_fid"], "folder")

    def test_root_cannot_be_deleted(self):
        drive = Drive([])
        result = self.operations(drive).apply("delete", [entry("0", "我的网盘", True)])
        self.assertEqual(result[0]["state"], "failed")
        self.assertEqual(drive.writes, [])
