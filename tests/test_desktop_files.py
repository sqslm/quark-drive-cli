"""File management checks exact selections and refuses conflicting targets."""

import json
import unittest
from unittest.mock import patch

import httpx

from quark_cli.client import QuarkClient
from quark_cli.desktop.file_operations import FileOperations
from quark_cli.errors import UsageError
from tests.test_downloader_worker import COOKIE, item


class Drive:
    def __init__(self, records):
        self.records = {row["fid"]: dict(row) for row in records}
        self.writes = []
        self.requests = []

    def transport(self):
        def handle(request):
            self.requests.append(request)
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
                    for fid in body['filelist']:
                        self.records[fid]["pdir_fid"] = body["to_pdir_fid"]
                    data = {"finish": True}
                elif path.endswith('file/copy'):
                    def clone(fid,parent):
                        children=[dict(row) for row in self.records.values() if row['pdir_fid'] == fid]
                        new_fid='copy-'+fid
                        suffix=2
                        while new_fid in self.records:
                            new_fid=f'copy-{fid}-{suffix}';suffix+=1
                        self.records[new_fid]={**self.records[fid],'fid':new_fid,'pdir_fid':parent}
                        for child in children:clone(child['fid'],new_fid)
                    for fid in body['filelist']:
                        clone(fid,body['to_pdir_fid'])
                    data = {'finish':True}
                elif path.endswith("file/delete"):
                    for fid in body["filelist"]:
                        self.records.pop(fid)
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
    def test_batch_properties_share_parent_reads_and_discard_old_cache(self):
        drive = Drive([item('folder','Folder',directory=True)] +
                      [item(f'f{n}',f'{n}.txt','folder',size=4) for n in range(40)])
        operations = self.operations(drive)
        selected = [entry(f'f{n}',f'{n}.txt',size=4) for n in range(40)]
        result = operations.info_many(selected)
        self.assertEqual([row['path'] for row in result],[f'/Folder/{n}.txt' for n in range(40)])
        self.assertEqual(len(drive.requests),43)  # 40 identities, 1 parent identity, 2 parent lists.
        drive.records['f0']['file_name'] = 'changed.txt'
        with self.assertRaises(UsageError):
            operations.info_many(selected)

    def test_rename_unknown_response_never_claims_failure_or_replays(self):
        drive = Drive([item('a','A.txt',size=3)])
        operations = self.operations(drive)
        with patch.object(operations.files,'rename',side_effect=httpx.ReadTimeout('lost response')) as write:
            result = operations.apply('rename',[entry('a','A.txt',size=3)],name='New.txt')
        self.assertEqual(result[0]['state'],'unknown')
        self.assertIn('未重发',result[0]['detail'])
        write.assert_called_once()

    def test_batch_delete_reuses_preflight_and_submits_once(self):
        drive = Drive([item("a","A.txt",size=3),item("b","B.txt",size=4),item("keep","Keep.txt")])
        result = self.operations(drive).apply("delete",[entry("a","A.txt",size=3),entry("b","B.txt",size=4)])
        self.assertEqual([row['state'] for row in result],['done','done'])
        self.assertEqual(len(drive.writes),1)
        self.assertEqual(drive.writes[0][1]['filelist'],['a','b'])
        self.assertLessEqual(len(drive.requests),5)
        self.assertIn('keep',drive.records)

    def test_rename_reads_back_fresh_metadata_and_later_info_is_fresh(self):
        drive = Drive([item("a","A.txt",size=3)])
        operations=self.operations(drive)
        result=operations.apply('rename',[entry('a','A.txt',size=3)],name='New.txt')
        self.assertEqual(result[0]['state'],'done')
        self.assertEqual(len(drive.requests),4)
        drive.records['a']['file_name']='Changed.txt'
        with self.assertRaises(UsageError):
            operations.info(entry('a','New.txt',size=3))

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
