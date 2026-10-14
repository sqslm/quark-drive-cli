import json
import unittest
from unittest.mock import patch

import httpx

from quark_cli.client import QuarkClient
from quark_cli.errors import AuthenticationError, NetworkError, RemoteError
from quark_cli.files import Files


def ok(data, **extra):
    payload = {"status": 200, "code": 0, "data": data}
    payload.update(extra)
    return payload


class FilesTests(unittest.TestCase):
    def test_stale_total_retries_reads_and_returns_only_consistent_snapshot(self):
        cases = [
            ({"list": [], "_total": 1}, {"list": [], "_total": 0}),
            ({"list": [{"fid": "new"}], "_total": 0}, {"list": [{"fid": "new"}], "_total": 1}),
        ]
        for first, settled in cases:
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(200, json=ok(first if len(calls) == 1 else settled), request=request)
            with self.subTest(first=first), patch("quark_cli.files.time.sleep") as sleep:
                with QuarkClient("sid=mock", transport=httpx.MockTransport(handler)) as client:
                    result = Files(client).list_all("source")
            self.assertEqual(result["data"], settled)
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(request.method == "GET" for request in calls))
            sleep.assert_called_once_with(0.25)

    def test_recovery_restarts_all_pages_and_discards_partial_snapshot(self):
        responses = [
            {"list": [{"fid": "old-a"}], "_total": 2},
            {"list": [{"fid": "old-b"}], "_total": 3},
            {"list": [{"fid": "new-a"}], "_total": 2},
            {"list": [{"fid": "new-b"}], "_total": 2},
        ]
        pages = []
        def handler(request):
            self.assertEqual(request.method, "GET")
            pages.append(int(request.url.params["_page"]))
            return httpx.Response(200, json=ok(responses[len(pages) - 1]), request=request)
        with patch("quark_cli.files.time.sleep"), QuarkClient("sid=mock", transport=httpx.MockTransport(handler)) as client:
            result = Files(client).list_all("source", size=1)
        self.assertEqual(pages, [1, 2, 1, 2])
        self.assertEqual([record["fid"] for record in result["data"]["list"]], ["new-a", "new-b"])

    def test_persistent_inconsistency_fails_after_three_read_attempts(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=ok({"list": [], "_total": 1}), request=request)
        with patch("quark_cli.files.time.sleep") as sleep, QuarkClient("sid=mock", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(RemoteError, "分页提前结束.*只读重读"):
                Files(client).list_all("source")
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.75])
        self.assertTrue(all(request.method == "GET" for request in calls))

    def test_malformed_duplicate_network_and_auth_errors_are_not_retried(self):
        cases = [
            ("invalid", {"list": [], "_total": True}, RemoteError),
            ("duplicate", {"list": [{"fid": "same"}, {"fid": "same"}], "_total": 2}, RemoteError),
            ("network", None, NetworkError),
            ("auth", None, AuthenticationError),
        ]
        for label, response, error in cases:
            calls = []
            def handler(request):
                calls.append(request)
                if label == "network":
                    raise httpx.ReadError("mock connection ended", request=request)
                if label == "auth":
                    return httpx.Response(401, request=request)
                return httpx.Response(200, json=ok(response), request=request)
            with self.subTest(label=label), patch("quark_cli.files.time.sleep") as sleep:
                with QuarkClient("sid=mock", transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(error):
                        Files(client).list_all("source")
            self.assertEqual(len(calls), 1)
            sleep.assert_not_called()

    def test_repeated_page_is_not_retried_even_when_total_also_changes(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=ok({"list": [{"fid": "same"}], "_total": 2 + len(calls)}), request=request)
        with patch("quark_cli.files.time.sleep") as sleep, QuarkClient("sid=mock", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(RemoteError, "分页响应重复"):
                Files(client).list_all("source", size=1)
        self.assertEqual(len(calls), 2)
        sleep.assert_not_called()

    def test_requests_real_total_instead_of_accepting_unrequested_zero_placeholder(self):
        records = [{"fid": "one", "file_name": "one"}, {"fid": "two", "file_name": "two"}]

        def handler(request):
            fetched = request.url.params.get("_fetch_total") == "1"
            page = int(request.url.params["_page"])
            # The web API leaves metadata._total=0 unless it was requested.
            return httpx.Response(200, json=ok({"list": records[page - 1:page]}, metadata={"_total": 2 if fetched else 0}), request=request)

        with QuarkClient("sid=mock", transport=httpx.MockTransport(handler)) as client:
            result = Files(client).list_all(size=1)
        self.assertEqual([item["fid"] for item in result["data"]["list"]], ["one", "two"])

    def test_all_pages_and_empty_directory(self):
        requests = []

        def handler(request):
            requests.append(request)
            page = int(request.url.params["_page"])
            records = ([{"fid": "a", "file_name": "one"}, {"fid": "b", "file_name": "two"}]
                       if page == 1 else [{"fid": "c", "file_name": "three"}] if page == 2 else [])
            return httpx.Response(200, json=ok({"list": records, "_total": 3}), request=request)

        with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
            result = Files(client).list_all("0", size=2)
        self.assertEqual([item["fid"] for item in result["data"]["list"]], ["a", "b", "c"])
        self.assertEqual(len(requests), 2)

        def empty_handler(request):
            return httpx.Response(200, json=ok({"list": [], "_total": 0}), request=request)

        with QuarkClient("sid=x", transport=httpx.MockTransport(empty_handler)) as client:
            result = Files(client).list_all()
        self.assertEqual(result["data"]["list"], [])

    def test_repeated_page_stops_without_looping(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=ok({"list": [{"fid": "a"}], "_total": 9}), request=request)

        with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(RemoteError, "分页响应重复"):
                Files(client).list_all(size=1)
        self.assertEqual(calls, 2)

    def test_info_matches_exact_fid(self):
        def handler(request):
            self.assertEqual(request.url.path, "/1/clouddrive/file/info")
            self.assertEqual(request.url.params["fid"], "wanted")
            self.assertEqual(request.url.params["_fetch_full_path"], "0")
            return httpx.Response(
                200,
                json=ok({"fid": "wanted", "file_name": "right", "pdir_fid": "parent"}),
                request=request,
            )

        with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
            item = Files(client).info("wanted")
        self.assertEqual(item["file_name"], "right")
        self.assertEqual(item["pdir_fid"], "parent")

    def test_info_does_not_guess_on_fid_mismatch(self):
        def handler(request):
            return httpx.Response(200, json=ok({"fid": "other"}), request=request)

        with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(RemoteError, "找不到 FID"):
                Files(client).info("wanted")

    def test_crud_bodies_and_single_delete(self):
        calls = []

        def handler(request):
            body = request.read() if request.method == "POST" else b""
            calls.append((request.url.path, body))
            if request.url.path.endswith("/file/move"):
                data = {"finish": True}
            else:
                data = {"fid": "created"} if request.url.path.endswith("/file") else {}
            return httpx.Response(200, json=ok(data), request=request)

        with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
            files = Files(client)
            files.mkdir("new folder", "parent")
            files.rename("file-id", "renamed")
            files.move("file-id", "target")
            files.delete("file-id")
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0][0], "/1/clouddrive/file")
        self.assertEqual(json.loads(calls[0][1])["pdir_fid"], "parent")
        self.assertEqual(calls[1][0], "/1/clouddrive/file/rename")
        self.assertEqual(calls[2][0], "/1/clouddrive/file/move")
        self.assertEqual(calls[3][0], "/1/clouddrive/file/delete")
        delete_body = json.loads(calls[3][1])
        self.assertEqual(delete_body, {"action_type": 2, "filelist": ["file-id"], "exclude_fids": []})

    def test_move_task_polling_and_failure(self):
        seen = []

        def handler(request):
            seen.append(request.url.path)
            if request.url.path.endswith("/file/move"):
                payload = ok({"task_id": "task", "finish": False}, metadata={"tq_gap": 0})
            else:
                payload = ok({"status": 2})
            return httpx.Response(200, json=payload, request=request)

        with QuarkClient("sid=x", transport=httpx.MockTransport(handler)) as client:
            result = Files(client).move("source", "target", poll_interval=0)
        self.assertEqual(result["status"], 2)
        self.assertEqual(seen, ["/1/clouddrive/file/move", "/1/clouddrive/task"])

        def rejected(request):
            if request.url.path.endswith("/file/move"):
                return httpx.Response(200, json=ok({"task_id": "task"}), request=request)
            return httpx.Response(200, json=ok({"status": 3, "message": "failure"}), request=request)

        with QuarkClient("sid=x", transport=httpx.MockTransport(rejected)) as client:
            with self.assertRaisesRegex(RemoteError, "拒绝"):
                Files(client).move("source", "target", poll_interval=0)

    def test_move_checks_immediately_unless_server_requires_a_gap(self):
        for gap in (None,250):
            with self.subTest(gap=gap):
                def handler(request):
                    payload=ok({'task_id':'task'},metadata={} if gap is None else {'tq_gap':gap}) if request.method == 'POST' else ok({'status':2})
                    return httpx.Response(200,json=payload,request=request)
                with patch('quark_cli.files.time.sleep') as sleep, QuarkClient('sid=x',transport=httpx.MockTransport(handler)) as client:
                    Files(client).move('source','target')
                if gap is None:sleep.assert_not_called()
                else:sleep.assert_called_once_with(.25)


if __name__ == "__main__":
    unittest.main()
