import tempfile
import unittest
from pathlib import Path

import httpx

from quark_cli.desktop.service import DesktopService
from quark_cli.downloader.worker import DriveBackend
from quark_cli.errors import UsageError, RateLimitError
from quark_cli.request_guard import RequestGuard
from tests.test_desktop_files import Drive, entry
from tests.test_downloader_worker import item, COOKIE


class CutTests(unittest.TestCase):
    def test_fast_operations_and_normal_reads_share_durable_cooldown(self):
        drive = Drive([item("a","A.txt",size=3)])
        def reject(request):
            drive.requests.append(request)
            return httpx.Response(429)
        drive.transport = lambda:httpx.MockTransport(reject)
        service=self.service(drive)
        directory=service.directory/'guard'
        service.backend._operation_guard=RequestGuard.durable(directory=directory,pacing=.1)
        service.backend._guard=RequestGuard.durable(directory=directory,pacing=.5)
        with self.assertRaises(RateLimitError):
            self.call(service,'file.info',{'entry':entry('a','A.txt',size=3)})
        with self.assertRaises(RateLimitError):
            service.backend.listing('0')
        self.assertEqual(len(drive.requests),1)

    def test_interactive_paste_reuses_info_and_shortens_pacing(self):
        drive = Drive([item("a", "A.txt", size=3), item("target", "目标", directory=True)])
        service = self.service(drive)
        clock = [0.0]
        def sleep(delay):
            clock[0] += delay
        service.backend._guard = RequestGuard.memory(pacing=.5, clock=lambda:clock[0], sleep=sleep)
        service.backend._operation_guard = RequestGuard.memory(pacing=.1, clock=lambda:clock[0], sleep=sleep)
        cut = self.call(service, "file.cut", {"entries":[entry("a", "A.txt", size=3)]})
        result = self.call(service, "file.paste", {"id":cut["id"], "target":"target"})
        self.assertEqual(result["results"][0]["state"], "done")
        self.assertLessEqual(len(drive.requests), 6)
        self.assertLessEqual(clock[0], .6)

    def service(self, drive):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        backend = DriveBackend(lambda: COOKIE, transport=drive.transport())
        service = DesktopService(
            lambda message: None,
            backend=backend,
            directory=Path(directory.name) / "state",
        )
        service.saved_cookie = COOKIE
        self.addCleanup(service.shutdown)
        return service

    def call(self, service, method, params):
        return service.dispatch("test", method, params, None)

    def test_cut_is_local_and_paste_moves_exact_selection(self):
        drive = Drive(
            [
                item("a", "A.txt", size=3),
                item("keep", "Keep.txt", size=4),
                item("target", "目标", directory=True),
            ]
        )
        service = self.service(drive)
        cut = self.call(service, "file.cut", {"entries": [entry("a", "A.txt", size=3)]})
        self.assertEqual(drive.requests, [])
        result = self.call(service, "file.paste", {"id": cut["id"], "target": "target"})
        self.assertEqual(result["results"][0]["state"], "done")
        self.assertIsNone(result["clipboard"])
        self.assertEqual(drive.records["a"]["pdir_fid"], "target")
        self.assertEqual(drive.records["keep"]["pdir_fid"], "0")
        self.assertEqual(drive.writes[0][1]["filelist"], ["a"])
        self.assertNotIn("context", cut)

    def test_account_switch_stale_id_and_share_refuse_without_requests(self):
        for case in ("account", "id", "share"):
            with self.subTest(case=case):
                drive = Drive(
                    [item("a", "A.txt", size=3), item("target", "目标", directory=True)]
                )
                service = self.service(drive)
                cut = self.call(
                    service, "file.cut", {"entries": [entry("a", "A.txt", size=3)]}
                )
                params = {"id": cut["id"], "target": "target"}
                if case == "account":
                    service.saved_cookie = "sid=another-account"
                elif case == "id":
                    params["id"] = "old-id"
                else:
                    params["handle"] = "share"
                with self.assertRaises(UsageError):
                    self.call(service, "file.paste", params)
                self.assertEqual(drive.requests, [])

    def test_partial_conflict_keeps_only_failed_selection(self):
        drive = Drive(
            [
                item("a", "A.txt", size=3),
                item("b", "B.txt", size=4),
                item("target", "目标", directory=True),
                item("conflict", "b.TXT", "target", size=4),
            ]
        )
        service = self.service(drive)
        cut = self.call(
            service,
            "file.cut",
            {"entries": [entry("a", "A.txt", size=3), entry("b", "B.txt", size=4)]},
        )
        result = self.call(service, "file.paste", {"id": cut["id"], "target": "target"})
        self.assertEqual(
            [row["state"] for row in result["results"]], ["done", "failed"]
        )
        self.assertEqual([row["fid"] for row in result["clipboard"]["entries"]], ["b"])
        self.assertFalse(result["clipboard"]["blocked"])
        self.assertEqual(len(drive.writes), 1)

    def test_unknown_move_is_not_repeated(self):
        drive = Drive(
            [item("a", "A.txt", size=3), item("target", "目标", directory=True)]
        )
        transport = drive.transport()

        def fail(request):
            response = transport.handle_request(request)
            if request.url.path.endswith("file/move"):
                raise httpx.ReadTimeout("response lost", request=request)
            return response

        drive.transport = lambda: httpx.MockTransport(fail)
        service = self.service(drive)
        cut = self.call(service, "file.cut", {"entries": [entry("a", "A.txt", size=3)]})
        result = self.call(service, "file.paste", {"id": cut["id"], "target": "target"})
        self.assertEqual(result["results"][0]["state"], "unknown")
        self.assertTrue(result["clipboard"]["blocked"])
        before = len(drive.requests)
        with self.assertRaisesRegex(UsageError, "未重复提交"):
            self.call(service, "file.paste", {"id": cut["id"], "target": "target"})
        self.assertEqual(len(drive.requests), before)
        self.assertEqual(len(drive.writes), 1)

    def test_directory_paste_never_scans_descendants(self):
        drive = Drive(
            [
                item("a", "源目录", directory=True),
                item("target", "目标", directory=True),
            ]
            + [item(str(n), f"{n}.txt", "a", size=4) for n in range(1, 51)]
        )
        service = self.service(drive)
        cut = self.call(service, "file.cut", {"entries": [entry("a", "源目录", True)]})
        result = self.call(service, "file.paste", {"id": cut["id"], "target": "target"})
        self.assertEqual(result["results"][0]["state"], "done")
        self.assertLessEqual(len(drive.requests), 10)
        self.assertFalse(
            any(request.url.params.get("pdir_fid") == "a" for request in drive.requests)
        )

    def test_batch_paste_submits_once_and_reads_common_parent_once(self):
        drive = Drive(
            [
                item("a", "A.txt", size=3),
                item("b", "B.txt", size=4),
                item("target", "目标", directory=True),
            ]
        )
        service = self.service(drive)
        cut = self.call(
            service,
            "file.cut",
            {"entries": [entry("a", "A.txt", size=3), entry("b", "B.txt", size=4)]},
        )
        result = self.call(service, "file.paste", {"id": cut["id"], "target": "target"})
        self.assertTrue(all(row["state"] == "done" for row in result["results"]))
        self.assertEqual(len(drive.writes), 1)
        self.assertEqual(drive.writes[0][1]["filelist"], ["a", "b"])
        self.assertEqual(
            sum(
                request.url.params.get("pdir_fid") == "0" for request in drive.requests
            ),
            1,
        )
        self.assertLessEqual(len(drive.requests), 10)

    def test_same_parent_and_descendant_refuse_before_move(self):
        drive = Drive(
            [
                item("a", "源目录", directory=True),
                item("child", "子目录", "a", directory=True),
            ]
        )
        service = self.service(drive)
        cut = self.call(service, "file.cut", {"entries": [entry("a", "源目录", True)]})
        for target in ("0", "child"):
            result = self.call(
                service, "file.paste", {"id": cut["id"], "target": target}
            )
            self.assertEqual(result["results"][0]["state"], "failed")
        self.assertEqual(drive.writes, [])

    def test_cancel_cut_and_transfer_gate_are_local(self):
        drive = Drive([item("a", "A.txt", size=3)])
        service = self.service(drive)
        cut = self.call(service, "file.cut", {"entries": [entry("a", "A.txt", size=3)]})
        self.assertIsNone(self.call(service, "file.clearCut", {"id": cut["id"]}))
        service.busy = True
        with self.assertRaises(UsageError):
            self.call(service, "file.cut", {"entries": [entry("a", "A.txt", size=3)]})
        self.assertEqual(drive.requests, [])
