import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli.client import QuarkClient
from quark_cli.desktop.recycle import Recycle
from quark_cli.errors import RemoteError
from tests.test_desktop_files import Drive
from tests.test_downloader_worker import item


class RecycleDrive(Drive):
    def __init__(self, records):
        super().__init__([])
        self.recycle = records
        self.unknown = False
        self.duplicate = False
        self.retained = set()
        self.read_failure = False

    def transport(self):
        other = super().transport()

        def handle(request):
            path = request.url.path
            if '/file/recycle/' in path:
                self.requests.append(request)
            if path.endswith("file/recycle/list"):
                if self.read_failure and self.writes:
                    return httpx.Response(500,request=request)
                page, size = (
                    int(request.url.params["_page"]),
                    int(request.url.params["_size"]),
                )
                records = self.recycle * 2 if self.duplicate else self.recycle
                data = {
                    "list": records[(page - 1) * size : page * size],
                    "_total": len(records),
                }
            elif "/file/recycle/" in path:
                body = json.loads(request.content)
                self.writes.append((path, body))
                if self.unknown:
                    raise httpx.ReadTimeout("unknown transport", request=request)
                if path.endswith("/remove"):
                    self.recycle = [
                        row
                        for row in self.recycle
                        if row["record_id"] not in body["record_list"] or row["record_id"] in self.retained
                    ]
                elif path.endswith("/recover"):
                    for row in self.recycle:
                        if row["fid"] in body["fid_list"] and row['record_id'] not in self.retained:
                            self.records[row["fid"]] = dict(row)
                    self.recycle = [
                        row
                        for row in self.recycle
                        if row["fid"] not in body["fid_list"] or row['record_id'] in self.retained
                    ]
                else:
                    raise AssertionError(path)
                data = {}
            else:
                return other.handle_request(request)
            return httpx.Response(200, json={"status": 200, "code": 0, "data": data})

        return httpx.MockTransport(handle)


def record(record_id, fid, name, parent="0"):
    return {
        **item(fid, name, parent, size=4),
        "record_id": record_id,
        "deleted_at": 123,
        "secret": "do-not-expose",
    }


class RecycleTests(unittest.TestCase):
    def test_restore_partial_and_incoming_name_conflict_are_precise(self):
        drive=RecycleDrive([record('a','fa','A.txt'),record('b','fb','B.txt'),record('c','fc','a.TXT')])
        operations=self.operations(drive);selected=operations.listing();drive.retained={'b'}
        with patch('quark_cli.desktop.recycle.time.sleep'):
            result=operations.apply('restore',selected)
        self.assertEqual([row['state'] for row in result],['done','unknown','failed'])
        self.assertEqual(drive.writes[0][1],{'fid_list':['fa','fb']})
        self.assertEqual(len(drive.writes),1)
        self.assertEqual([row['record_id'] for row in drive.recycle],['b','c'])

    def test_restore_readback_failure_or_lost_response_never_replays(self):
        for lost in (False,True):
            with self.subTest(lost=lost):
                drive=RecycleDrive([record('a','fa','A.txt')]);operations=self.operations(drive)
                selected=operations.listing();drive.unknown=lost;drive.read_failure=not lost
                result=operations.apply('restore',selected)
                self.assertEqual(result[0]['state'],'unknown')
                self.assertEqual(len(drive.writes),1)

    def test_restore_many_uses_one_write_and_shared_parent_readback(self):
        drive=RecycleDrive([record(str(n),f'f{n}',f'{n}.txt') for n in range(40)]+[record('keep','keep','Keep.txt')])
        operations=self.operations(drive);selected=operations.listing()[:-1];drive.requests.clear()
        result=operations.apply('restore',selected)
        self.assertTrue(all(row['state'] == 'done' for row in result))
        self.assertEqual(len(drive.writes),1)
        self.assertEqual(drive.writes[0][1],{'fid_list':[f'f{n}' for n in range(40)]})
        self.assertLessEqual(len(drive.requests),5)

    def test_remove_partial_batch_keeps_unconfirmed_items_and_never_replays(self):
        drive=RecycleDrive([record('a','fa','A.txt'),record('b','fb','B.txt'),record('keep','fk','Keep.txt')])
        operations=self.operations(drive);selected=operations.listing()[:2]
        drive.retained={'b'};stages=[]
        with patch('quark_cli.desktop.recycle.time.sleep') as sleep:
            results=operations.apply('remove',selected,progress=stages.append)
        self.assertEqual([row['state'] for row in results],['done','unknown'])
        self.assertEqual(len(drive.writes),1)
        self.assertEqual(drive.writes[0][1]['record_list'],['a','b'])
        self.assertEqual([row['record_id'] for row in drive.recycle],['b','keep'])
        self.assertEqual(sleep.call_count,5)
        self.assertTrue(any('1/2' in stage for stage in stages))

    def test_remove_duplicate_selection_writes_each_valid_record_once(self):
        drive=RecycleDrive([record('a','fa','A.txt'),record('b','fb','B.txt')])
        operations=self.operations(drive);rows=operations.listing()
        results=operations.apply('remove',[rows[0],rows[0],rows[1]])
        self.assertEqual([row['state'] for row in results],['done','failed','done'])
        self.assertEqual(drive.writes[0][1]['record_list'],['a','b'])
        self.assertEqual(len(drive.writes),1)

    def test_remove_readback_failure_does_not_claim_success_or_repeat_write(self):
        drive=RecycleDrive([record('a','fa','A.txt'),record('b','fb','B.txt')])
        operations=self.operations(drive);rows=operations.listing();drive.read_failure=True
        results=operations.apply('remove',rows)
        self.assertEqual([row['state'] for row in results],['unknown','unknown'])
        self.assertEqual(len(drive.writes),1)

    def test_remove_snapshot_failure_refuses_entire_batch_before_writing(self):
        drive=RecycleDrive([record('a','fa','A.txt'),record('b','fb','B.txt')])
        operations=self.operations(drive);rows=operations.listing();drive.duplicate=True
        results=operations.apply('remove',rows)
        self.assertEqual([row['state'] for row in results],['failed','failed'])
        self.assertEqual(drive.writes,[])

    def test_service_progress_carries_the_operation_request_id(self):
        from quark_cli.desktop.service import DesktopService
        from quark_cli.downloader.worker import DriveBackend
        from tests.test_downloader_worker import COOKIE
        drive=RecycleDrive([record('a','fa','A.txt'),record('keep','fk','Keep.txt')])
        selected=self.operations(drive).listing()[:1];messages=[]
        with tempfile.TemporaryDirectory() as directory:
            backend=DriveBackend(lambda:COOKIE,transport=drive.transport())
            service=DesktopService(messages.append,backend=backend,directory=Path(directory))
            try:
                result=service.dispatch('remove-id','recycle.remove',{'entries':selected},None)
                self.assertEqual(result[0]['state'],'done')
                events=[message for message in messages if message.get('event') == 'recycle.progress']
                self.assertGreaterEqual(len(events),3)
                self.assertTrue(all(message['id'] == 'remove-id' for message in events))
            finally:service.shutdown()

    def test_remove_many_uses_one_write_and_shared_readback(self):
        for count in (1,40,150):
            with self.subTest(count=count):
                drive=RecycleDrive([record(str(n),f'f{n}',f'{n}.txt') for n in range(count)]+[record('keep','keep','Keep.txt')])
                operations=self.operations(drive)
                selected=operations.listing()[:-1];drive.requests.clear()
                results=operations.apply('remove',selected)
                self.assertTrue(all(row['state'] == 'done' for row in results))
                self.assertEqual([row['record_id'] for row in drive.recycle],['keep'])
                self.assertEqual(len(drive.writes),1)
                self.assertEqual(drive.writes[0][1],{'select_mode':2,'record_list':[str(n) for n in range(count)]})
                self.assertEqual(len(drive.requests),3 if count < 100 else 4)

    def operations(self, drive):
        client = QuarkClient("sid=offline", transport=drive.transport())
        self.addCleanup(client.close)
        return Recycle(client)

    def test_pagination_is_complete_and_safe(self):
        operations = self.operations(
            RecycleDrive([record(str(n), f"f{n}", f"{n}.txt") for n in range(101)])
        )
        rows = operations.listing()
        self.assertEqual(len(rows), 101)
        self.assertNotIn("secret", json.dumps(rows))

    def test_remove_exact_record_and_restore_exact_fid(self):
        for mode in ("remove", "restore"):
            with self.subTest(mode=mode):
                drive = RecycleDrive(
                    [record("a", "a", "A.txt"), record("keep", "keep", "Keep.txt")]
                )
                operations = self.operations(drive)
                rows = operations.listing()
                result = operations.apply(mode, [rows[0]])
                self.assertEqual(result[0]["state"], "done")
                self.assertEqual([row["record_id"] for row in drive.recycle], ["keep"])
                expected = (
                    {"record_list": ["a"], "select_mode": 2}
                    if mode == "remove"
                    else {"fid_list": ["a"]}
                )
                self.assertEqual(drive.writes[0][1], expected)

    def test_conflict_missing_parent_or_ambiguous_fid_refuses_restore(self):
        for kind in ("conflict", "parent", "duplicate-fid"):
            with self.subTest(kind=kind):
                drive = RecycleDrive([record("a", "a", "A.txt")])
                if kind == "conflict":
                    drive.records["other"] = item("other", "a.TXT", size=4)
                if kind == "parent":
                    drive.recycle[0].pop("pdir_fid")
                if kind == "duplicate-fid":
                    drive.recycle.append(record("b", "a", "A.txt"))
                operations = self.operations(drive)
                result = operations.apply("restore", [operations.listing()[0]])
                self.assertEqual(result[0]["state"], "failed")
                self.assertEqual(drive.writes, [])

    def test_stale_record_and_partial_failure(self):
        drive = RecycleDrive([record("a", "a", "A.txt"), record("b", "b", "B.txt")])
        operations = self.operations(drive)
        rows = operations.listing()
        rows[1]["name"] = "wrong.txt"
        result = operations.apply("remove", rows)
        self.assertEqual([row["state"] for row in result], ["done", "failed"])
        self.assertEqual(len(drive.writes), 1)

    def test_unknown_result_never_replays_write(self):
        drive = RecycleDrive([record("a", "a", "A.txt")])
        drive.unknown = True
        operations = self.operations(drive)
        result = operations.apply("remove", operations.listing())
        self.assertEqual(result[0]["state"], "unknown")
        self.assertEqual(len(drive.writes), 1)

    def test_duplicate_pagination_fails_before_writing(self):
        drive = RecycleDrive([record("a", "a", "A.txt")])
        drive.duplicate = True
        with self.assertRaises(RemoteError):
            self.operations(drive).listing()
        self.assertEqual(drive.writes, [])


if __name__ == "__main__":
    unittest.main()
