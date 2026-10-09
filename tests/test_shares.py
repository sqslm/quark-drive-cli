import contextlib
import io
import json
import unittest
from collections import defaultdict
from unittest.mock import patch

import httpx

from quark_cli.cli import main
from quark_cli.client import QuarkClient
from quark_cli.errors import RemoteError, UsageError
from quark_cli.shares import ShareSaver, parse_share_url


def ok(data, **extra):
    payload = {"status": 200, "code": 0, "data": data}
    payload.update(extra)
    return payload


def directory(fid, name):
    return {"fid": fid, "file_name": name, "file_type": 0, "dir": True}


class ShareUrlTests(unittest.TestCase):
    def test_accepts_direct_share_url_and_extracts_passcode(self):
        link = parse_share_url("https://pan.quark.cn/s/47f48410260c?pwd=abcd")
        self.assertEqual(link.pwd_id, "47f48410260c")
        self.assertEqual(link.passcode, "abcd")
        self.assertEqual(
            parse_share_url("https://pan.quark.cn/s/abc?pwd=from-url", "override").passcode,
            "override",
        )

    def test_rejects_other_origins_paths_and_query_parameters(self):
        urls = (
            "http://pan.quark.cn/s/abc",
            "https://evil.example/s/abc",
            "https://pan.quark.cn.evil.example/s/abc",
            "https://user@pan.quark.cn/s/abc",
            "https://pan.quark.cn:444/s/abc",
            "https://pan.quark.cn/s/abc/extra",
            "https://pan.quark.cn/s/abc#fragment",
            "https://pan.quark.cn/s/abc?token=secret",
            "https://pan.quark.cn/s/abc?pwd=one&pwd=two",
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(UsageError):
                parse_share_url(url)


class ShareSaverTests(unittest.TestCase):
    def workflow(
        self,
        *,
        task_statuses=(2,),
        leaf_exists=False,
        conflict=False,
        ambiguous_root=False,
        readback_delay=0,
        mkdir_response_fid="target",
    ):
        calls = []
        saved_requests = []
        directories = defaultdict(list)
        directories["0"] = [directory("media", "影视")]
        if ambiguous_root:
            directories["0"].append(directory("media-duplicate", "影视"))
        directories["media"] = [directory("tv", "电视剧")]
        if leaf_exists:
            directories["tv"] = [directory("target", "测试")]
        if conflict:
            directories["target"] = [directory("already", "兰香")]
        statuses = iter(task_statuses)
        saved_destination_fids = ["saved-extra", "saved-top", "saved-file"]
        created = {"value": False}
        readback_attempts = {"count": 0}

        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.host, "drive-pc.quark.cn")
            path = request.url.path
            if path.endswith("/share/sharepage/token"):
                self.assertEqual(json.loads(request.content), {"pwd_id": "47f48410260c", "passcode": ""})
                return httpx.Response(200, json=ok({"stoken": "stoken-private"}), request=request)
            if path.endswith("/share/sharepage/detail"):
                self.assertEqual(request.url.params["pdir_fid"], "0")
                self.assertEqual(request.url.params["_fetch_total"], "1")
                self.assertEqual(request.url.params["stoken"], "stoken-private")
                page = int(request.url.params["_page"])
                if page == 1:
                    records = [
                        {"fid": "share-folder", "file_name": "兰香", "file_type": 0,
                         "dir": True, "share_fid_token": "share-token-private"},
                        {"fid": "share-file", "file_name": "番外.mkv", "file_type": 1,
                         "dir": False, "share_fid_token": "share-token-file"},
                    ]
                elif page == 2:
                    records = [{"fid": "share-extra", "file_name": "字幕.srt", "file_type": 1,
                                "dir": False, "share_fid_token": "share-token-extra"}]
                else:
                    records = []
                return httpx.Response(
                    200,
                    json=ok({"list": records}, metadata={"_total": 3, "_size": 2, "_count": len(records)}),
                    request=request,
                )
            if path.endswith("/file/sort"):
                parent = request.url.params["pdir_fid"]
                current = directories[parent]
                if parent == "tv" and created["value"]:
                    readback_attempts["count"] += 1
                    if readback_attempts["count"] <= readback_delay:
                        current = []
                page = int(request.url.params["_page"])
                size = int(request.url.params["_size"])
                offset = (page - 1) * size
                return httpx.Response(
                    200,
                    json=ok({"list": current[offset:offset + size], "_total": len(current)}),
                    request=request,
                )
            if path.endswith("/file") and request.method == "POST":
                body = json.loads(request.content)
                self.assertEqual(body["file_name"], "测试")
                self.assertEqual(body["pdir_fid"], "tv")
                directories["tv"] = [directory("target", "测试")]
                directories["target"] = []
                created["value"] = True
                return httpx.Response(200, json=ok({"folder": {"fid": mkdir_response_fid}}), request=request)
            if path.endswith("/share/sharepage/save"):
                body = json.loads(request.content)
                saved_requests.append(body)
                self.assertEqual(body, {
                    "fid_list": ["share-folder", "share-file", "share-extra"],
                    "fid_token_list": ["share-token-private", "share-token-file", "share-token-extra"],
                    "to_pdir_fid": "target",
                    "pwd_id": "47f48410260c",
                    "stoken": "stoken-private",
                    "pdir_fid": "0",
                    "scene": "link",
                })
                return httpx.Response(200, json=ok({"task_id": "task-123"}), request=request)
            if path.endswith("/task"):
                self.assertEqual(request.url.params["task_id"], "task-123")
                status = next(statuses)
                data = {"status": status}
                if status == 2:
                    data["save_as"] = {
                        "to_pdir_fid": "target",
                        "save_as_top_fids": saved_destination_fids,
                    }
                    directories["target"] = [
                        directory("saved-top", "兰香"),
                        {"fid": "saved-file", "file_name": "番外.mkv", "file_type": 1},
                        {"fid": "saved-extra", "file_name": "字幕.srt", "file_type": 1},
                    ]
                return httpx.Response(200, json=ok(data), request=request)
            return httpx.Response(404, request=request)

        return httpx.MockTransport(handler), calls, saved_requests, created

    def test_reads_all_share_pages_creates_only_leaf_and_saves_once(self):
        transport, calls, saves, created = self.workflow()
        with patch("quark_cli.shares._PAGE_SIZE", 2):
            with QuarkClient("sid=offline", transport=transport) as client:
                result = ShareSaver(client).save(
                    "https://pan.quark.cn/s/47f48410260c",
                    "/影视/电视剧/测试",
                    poll_interval=0,
                )
        self.assertEqual(result["status"], 2)
        self.assertEqual(result["item_count"], 3)
        self.assertTrue(result["created_directory"])
        self.assertTrue(created["value"])
        self.assertEqual(len(saves), 1)
        detail_pages = [r for r in calls if r.url.path.endswith("/share/sharepage/detail")]
        self.assertEqual([r.url.params["_page"] for r in detail_pages], ["1", "2"])
        task_queries = [r for r in calls if r.url.path.endswith("/task")]
        self.assertEqual(len(task_queries), 1)

    def test_creation_readback_retries_cache_lag_and_uses_listed_fid(self):
        transport, _, saves, _ = self.workflow(readback_delay=2, mkdir_response_fid="stale-response-fid")
        with patch("quark_cli.shares._PAGE_SIZE", 2), patch("quark_cli.shares.time.sleep") as sleep:
            with QuarkClient("sid=offline", transport=transport) as client:
                result = ShareSaver(client).save(
                    "https://pan.quark.cn/s/47f48410260c",
                    "/影视/电视剧/测试",
                    poll_interval=0,
                )
        self.assertEqual(result["destination_fid"], "target")
        self.assertEqual(saves[0]["to_pdir_fid"], "target")
        self.assertEqual(sleep.call_count, 2)

    def test_creation_readback_stops_after_bounded_retries_without_saving(self):
        transport, _, saves, _ = self.workflow(readback_delay=20)
        with patch("quark_cli.shares._PAGE_SIZE", 2), patch("quark_cli.shares.time.sleep") as sleep:
            with QuarkClient("sid=offline", transport=transport) as client:
                with self.assertRaisesRegex(RemoteError, "读回超时"):
                    ShareSaver(client).save(
                        "https://pan.quark.cn/s/47f48410260c",
                        "/影视/电视剧/测试",
                        poll_interval=0,
                    )
        self.assertEqual(sleep.call_count, 5)
        self.assertEqual(saves, [])

    def test_missing_intermediate_path_never_creates_or_submits(self):
        transport, calls, saves, created = self.workflow()
        # The first root listing has no "影视" directory.
        def handler(request):
            if request.url.path.endswith("/file/sort"):
                return httpx.Response(200, json=ok({"list": [], "_total": 0}), request=request)
            return transport.handle_request(request)

        missing_parent = httpx.MockTransport(handler)
        with patch("quark_cli.shares._PAGE_SIZE", 2):
            with QuarkClient("sid=offline", transport=missing_parent) as client:
                with self.assertRaisesRegex(UsageError, "目标路径不存在"):
                    ShareSaver(client).save("https://pan.quark.cn/s/47f48410260c", "/影视/电视剧/测试", poll_interval=0)
        self.assertFalse(created["value"])
        self.assertEqual(saves, [])

    def test_existing_target_name_conflict_stops_before_save(self):
        transport, calls, saves, _ = self.workflow(leaf_exists=True, conflict=True)
        with patch("quark_cli.shares._PAGE_SIZE", 2):
            with QuarkClient("sid=offline", transport=transport) as client:
                with self.assertRaisesRegex(UsageError, "同名"):
                    ShareSaver(client).save("https://pan.quark.cn/s/47f48410260c", "/影视/电视剧/测试", poll_interval=0)
        self.assertEqual(saves, [])

    def test_ambiguous_path_component_stops_without_creating_or_saving(self):
        transport, _, saves, created = self.workflow(ambiguous_root=True)
        with patch("quark_cli.shares._PAGE_SIZE", 2):
            with QuarkClient("sid=offline", transport=transport) as client:
                with self.assertRaisesRegex(UsageError, "不唯一"):
                    ShareSaver(client).save("https://pan.quark.cn/s/47f48410260c", "/影视/电视剧/测试", poll_interval=0)
        self.assertFalse(created["value"])
        self.assertEqual(saves, [])

    def test_repeated_share_page_stops_without_submit(self):
        transport, calls, saves, _ = self.workflow()

        def handler(request):
            if request.url.path.endswith("/share/sharepage/detail") and request.url.params["_page"] == "2":
                return httpx.Response(200, json=ok({"list": [
                    {"fid": "share-folder", "file_name": "兰香", "file_type": 0,
                     "dir": True, "share_fid_token": "share-token-private"},
                    {"fid": "share-file", "file_name": "番外.mkv", "file_type": 1,
                     "dir": False, "share_fid_token": "share-token-file"},
                ]}, metadata={"_total": 3, "_size": 2, "_count": 2}), request=request)
            return transport.handle_request(request)

        with patch("quark_cli.shares._PAGE_SIZE", 2):
            with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(RemoteError, "重复 FID"):
                    ShareSaver(client).save("https://pan.quark.cn/s/47f48410260c", "/影视/电视剧/测试", poll_interval=0)
        self.assertEqual(saves, [])

    def test_failed_task_reports_id_and_never_resubmits(self):
        transport, calls, saves, _ = self.workflow(task_statuses=(3,))
        with patch("quark_cli.shares._PAGE_SIZE", 2):
            with QuarkClient("sid=offline", transport=transport) as client:
                with self.assertRaisesRegex(RemoteError, "task-123"):
                    ShareSaver(client).save("https://pan.quark.cn/s/47f48410260c", "/影视/电视剧/测试", poll_interval=0)
        self.assertEqual(len(saves), 1)
        self.assertEqual(len([r for r in calls if r.url.path.endswith("/share/sharepage/save")]), 1)

    def test_pending_task_is_polled_until_success_without_resubmitting(self):
        transport, calls, saves, _ = self.workflow(task_statuses=(1, 2))
        with patch("quark_cli.shares._PAGE_SIZE", 2):
            with QuarkClient("sid=offline", transport=transport) as client:
                result = ShareSaver(client).save(
                    "https://pan.quark.cn/s/47f48410260c", "/影视/电视剧/测试", poll_interval=0,
                )
        self.assertEqual(result["status"], 2)
        self.assertEqual(len(saves), 1)
        task_queries = [r for r in calls if r.url.path.endswith("/task")]
        self.assertEqual([r.url.params["retry_index"] for r in task_queries], ["0", "1"])

    def test_paused_and_timed_out_tasks_report_id_without_resubmitting(self):
        cases = ((4, 1, "暂停"), (1, 1, "超时"))
        for status, max_polls, expected_message in cases:
            with self.subTest(status=status):
                transport, _, saves, _ = self.workflow(task_statuses=(status,))
                with patch("quark_cli.shares._PAGE_SIZE", 2):
                    with QuarkClient("sid=offline", transport=transport) as client:
                        with self.assertRaisesRegex(RemoteError, f"{expected_message}.*task-123"):
                            ShareSaver(client).save(
                                "https://pan.quark.cn/s/47f48410260c",
                                "/影视/电视剧/测试",
                                poll_interval=0,
                                max_polls=max_polls,
                            )
                self.assertEqual(len(saves), 1)

    def test_json_output_does_not_contain_share_tokens(self):
        transport, _, _, _ = self.workflow()
        output = io.StringIO()
        with patch("quark_cli.shares._PAGE_SIZE", 2), patch("quark_cli.shares.time.sleep"):
            with patch.dict("os.environ", {"QUARK_COOKIE": "sid=offline"}), contextlib.redirect_stdout(output):
                status = main([
                    "--json", "save", "https://pan.quark.cn/s/47f48410260c",
                    "--to", "/影视/电视剧/测试",
                ], transport=transport)
        self.assertEqual(status, 0)
        serialized = output.getvalue()
        self.assertNotIn("stoken-private", serialized)
        self.assertNotIn("share-token-private", serialized)
        result = json.loads(serialized)
        self.assertEqual(result["item_count"], 3)
        self.assertEqual(result["destination_path"], "/影视/电视剧/测试")


if __name__ == "__main__":
    unittest.main()
