import json
import unittest

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

    def transport(self):
        other = super().transport()

        def handle(request):
            path = request.url.path
            if path.endswith("file/recycle/list"):
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
                        if row["record_id"] not in body["record_list"]
                    ]
                elif path.endswith("/recover"):
                    for row in self.recycle:
                        if row["fid"] in body["fid_list"]:
                            self.records[row["fid"]] = dict(row)
                    self.recycle = [
                        row
                        for row in self.recycle
                        if row["fid"] not in body["fid_list"]
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
