import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from quark_cli.cli import main
from quark_cli.client import QuarkClient
from quark_cli.errors import RemoteError, UsageError
from quark_cli.paths import Scope
from quark_cli.shares import ShareSaver

from drive_fixture import item
from share_fixture import SHARE_URL, SOURCE_PATH, ShareFixture, shared


class ShareSelectionTests(unittest.TestCase):
    def setUp(self):
        self.drive = ShareFixture()
        self.pages = patch("quark_cli.shares._PAGE_SIZE", 2)
        self.pages.start()
        self.addCleanup(self.pages.stop)
        self.sleep = patch("quark_cli.shares.time.sleep")
        self.sleep_mock = self.sleep.start()
        self.addCleanup(self.sleep.stop)

    @contextlib.contextmanager
    def service(self):
        with QuarkClient("sid=offline", transport=self.drive.transport()) as client:
            yield ShareSaver(client)

    def save(self, **options):
        arguments = {"source": SOURCE_PATH, "poll_interval": 0, **options}
        target = arguments.pop("target", "/范围/目标")
        with self.service() as service:
            return service.save(SHARE_URL, target, **arguments)

    def run_cli(self, arguments, *, code=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"QUARK_COOKIE": "sid=offline"}, clear=True):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = main(["--json", *arguments], transport=self.drive.transport())
        self.assertEqual(status, code, stderr.getvalue())
        serialized = stdout.getvalue() + stderr.getvalue()
        for secret in ("stoken-private", "private-token-", "private-signature", "private-nested-stoken", "private-auth", "private-passcode"):
            self.assertNotIn(secret, serialized)
        return json.loads(stdout.getvalue() if stdout.getvalue() else stderr.getvalue())

    def test_inspect_nested_chinese_path_paginates_and_retains_real_root_parent(self):
        with self.service() as service:
            root = service.inspect(SHARE_URL)
            result = service.inspect(SHARE_URL, path=SOURCE_PATH)
        self.assertEqual(root["items"][0]["pdir_fid"], "owner-account-parent")
        self.assertEqual(result["source_fid"], "source-child")
        self.assertEqual([entry["file_name"] for entry in result["items"]], ["note.txt", "skip.log", "资料", "UPPER.TXT"])
        pages = [request.url.params["_page"] for request in self.drive.calls
                 if request.url.path.endswith("/share/sharepage/detail") and request.url.params["pdir_fid"] == "source-child"]
        self.assertEqual(pages, ["1", "2"])
        self.assertEqual(self.drive.writes, [])
        self.assertEqual(set(result["items"][0]), {"fid", "file_name", "file_type", "pdir_fid", "size", "updated_at"})

    def test_inspect_json_hides_credentials_and_explicit_passcode_overrides_url(self):
        result = self.run_cli(["share", "inspect", SHARE_URL + "?pwd=unused", "--passcode", "private-passcode", "--path", SOURCE_PATH])
        self.assertEqual(result["source_path"], SOURCE_PATH)
        self.assertEqual(self.drive.token_bodies, [{"pwd_id": "offline-share", "passcode": "private-passcode"}])
        self.assertEqual(self.drive.writes, [])

    def test_inspect_external_share_is_separate_from_account_scope_at_all_flag_positions(self):
        for arguments in (["--scope", "scope", "share", "inspect", SHARE_URL, "--path", SOURCE_PATH],
                          ["share", "--scope", "/范围", "inspect", SHARE_URL, "--path", SOURCE_PATH],
                          ["share", "inspect", SHARE_URL, "--path", SOURCE_PATH, "--scope", "scope"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.run_cli(arguments)["source_fid"], "source-child")
        self.assertEqual(self.drive.writes, [])

    def test_save_uses_source_parent_and_selected_tokens_without_fetching_selected_directory(self):
        result = self.save(include=["*.txt", "资料"])
        self.assertEqual(result["item_count"], 2)
        self.assertEqual(self.drive.saves, [{"fid_list": ["note", "folder"],
            "fid_token_list": ["private-token-note", "private-token-folder"], "to_pdir_fid": "target",
            "pwd_id": "offline-share", "stoken": "stoken-private", "pdir_fid": "source-child", "scene": "link"}])
        self.assertFalse(any(request.url.params.get("pdir_fid") == "folder" for request in self.drive.calls))

    def test_include_union_exclude_priority_and_platform_independent_case_sensitivity(self):
        with self.service() as service:
            selected = service.select(SHARE_URL, source=SOURCE_PATH,
                                      include=["*.txt", "*.log", "资料"], exclude=["skip*", "资料"])
        self.assertEqual([entry["fid"] for entry in selected.items], ["note"])
        self.assertEqual(self.drive.writes, [])
        self.assertNotIn("private-token-", repr(selected))
        self.assertNotIn("stoken-private", repr(selected))

    def test_default_selection_includes_all_direct_items(self):
        result = self.save()
        self.assertEqual(result["item_count"], 4)
        self.assertEqual(self.drive.saves[0]["fid_list"], ["note", "skip", "folder", "upper"])

    def test_empty_selection_skips_nonexistent_target_without_account_reads_or_writes(self):
        result = self.save(include=["absent*"], target="/不存在/不创建", dry_run=True)
        self.assertEqual(result, {"saved": False, "skipped": True, "reason": "no_selected_items",
            "destination_path": "/不存在/不创建", "source_path": SOURCE_PATH, "item_count": 0,
            "created_directory": False, "dry_run": True})
        self.assertFalse(any("/file" in request.url.path for request in self.drive.calls))
        self.assertEqual(self.drive.writes, [])

    def test_empty_shared_directory_has_explicit_skip_result(self):
        self.drive.share_directories["source-child"] = []
        self.assertTrue(self.save(target="/不存在/不创建")["skipped"])
        self.assertEqual(self.drive.writes, [])

    def test_empty_selection_still_rejects_invalid_destination_syntax_locally(self):
        for target in ("relative", "/范围//新目录", "/范围/..", "/范围/新目录/", "/范围/\t"):
            with self.subTest(target=target), self.assertRaises(UsageError):
                self.save(include=["absent*"], target=target)
        self.assertEqual(self.drive.calls, [])

    def test_empty_pattern_separator_or_control_rejected_before_requests(self):
        for pattern in ("", "dir/*.txt", "dir\\*.txt", "*.txt\x1b"):
            for option in ("include", "exclude"):
                with self.subTest(pattern=pattern, option=option), self.assertRaises(UsageError):
                    self.save(**{option: [pattern]})
        self.assertEqual(self.drive.calls, [])

    def test_invalid_source_syntax_rejected_before_requests(self):
        for source in ("relative", "/公开源//中文 子目录", "/公开源/..", "/公开源/", "/公开源\\中文 子目录"):
            with self.subTest(source=source), self.assertRaises(UsageError):
                self.save(source=source)
        self.assertEqual(self.drive.calls, [])

    def test_missing_file_and_ambiguous_source_components_never_write(self):
        cases = [("/缺失", "不存在"), ("/公开源/root-note.txt", "不是目录"), ("/公开源/中文 子目录", "不唯一")]
        for source, message in cases:
            with self.subTest(source=source):
                if message == "不唯一":
                    self.drive.share_directories["source-root"].append(shared("duplicate-child", "中文 子目录", "source-root", directory=True))
                with self.assertRaisesRegex(UsageError, message):
                    self.save(source=source, target="/范围/不创建")
        self.assertEqual(self.drive.writes, [])

    def test_nested_parent_mismatch_stops_before_target_resolution(self):
        self.drive.share_directories["source-child"][0]["pdir_fid"] = "source-root"
        with self.assertRaisesRegex(RemoteError, "父 FID"):
            self.save(target="/范围/不创建")
        self.assertEqual(self.drive.writes, [])

    def test_source_path_cycle_and_final_contents_ancestor_are_rejected(self):
        with self.subTest(location="path"):
            self.drive.share_directories["source-child"] = [shared("source-root", "循环", "source-child", directory=True)]
            with self.assertRaisesRegex(RemoteError, "循环"):
                self.save(source=SOURCE_PATH + "/循环")
        with self.subTest(location="contents"):
            with self.assertRaisesRegex(RemoteError, "循环"):
                self.save()
        self.assertEqual(self.drive.writes, [])

    def test_inspect_preserves_duplicate_names_but_selected_duplicates_stop_before_mkdir(self):
        self.drive.share_directories["source-child"].append(shared("same-name", "note.txt", "source-child"))
        with self.service() as service:
            result = service.inspect(SHARE_URL, path=SOURCE_PATH)
        self.assertEqual(sum(entry["file_name"] == "note.txt" for entry in result["items"]), 2)
        with self.assertRaisesRegex(UsageError, "名称不唯一"):
            self.save(include=["note.txt"], target="/范围/不创建")
        self.assertEqual(self.drive.writes, [])

    def test_invalid_selected_name_stops_before_mkdir(self):
        self.drive.share_directories["source-child"][0]["file_name"] = "bad\x1bname"
        with self.assertRaisesRegex(RemoteError, "名称无效"):
            self.save(target="/范围/不创建")
        self.assertEqual(self.drive.writes, [])

    def test_missing_selected_token_stops_before_mkdir_but_excluded_token_is_not_required(self):
        del self.drive.share_directories["source-child"][1]["share_fid_token"]
        with self.assertRaisesRegex(RemoteError, "必要文件字段"):
            self.save(target="/范围/不创建")
        self.assertEqual(self.drive.writes, [])
        self.assertTrue(self.save(exclude=["*.log"])["saved"])

    def test_selected_save_outside_scope_refuses_implicit_mkdir(self):
        with self.service() as service:
            scope = Scope(service.paths, "scope")
            with self.assertRaisesRegex(UsageError, "超出"):
                service.save(SHARE_URL, "/范围外/不创建", source=SOURCE_PATH, scope=scope, poll_interval=0)
        self.assertEqual(self.drive.writes, [])

    def test_selected_save_checks_existing_destination_conflict_before_submit(self):
        self.drive.items.append(item("old-note", "note.txt", "target", size=4))
        with self.assertRaisesRegex(UsageError, "同名"):
            self.save(include=["*.txt"])
        self.assertEqual(self.drive.writes, [])

    def test_preview_missing_leaf_is_safe_and_zero_write(self):
        result = self.run_cli(["save", SHARE_URL, "--source", SOURCE_PATH, "--include", "*.txt",
                               "--to", "/范围/不创建", "--dry-run", "--scope", "scope"])
        self.assertTrue(result["create_directory"])
        self.assertEqual(result["source_fid"], "source-child")
        self.assertEqual([entry["file_name"] for entry in result["items"]], ["note.txt"])
        self.assertEqual(self.drive.writes, [])

    def test_empty_cli_json_reports_skip_and_zero_write(self):
        result = self.run_cli(["save", SHARE_URL, "--source", SOURCE_PATH, "--include", "absent*", "--to", "/不存在/不创建"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["item_count"], 0)
        self.assertEqual(self.drive.writes, [])

    def test_root_save_json_preserves_schema_and_selected_save_extends_explicitly(self):
        root = self.run_cli(["save", SHARE_URL, "--to", "/范围/目标"])
        self.assertEqual(set(root), {"saved", "task_id", "destination_fid", "destination_path", "item_count", "created_directory", "status"})
        self.drive = ShareFixture()
        selected = self.run_cli(["save", SHARE_URL, "--source", SOURCE_PATH, "--include", "*.txt", "--to", "/范围/目标"])
        self.assertEqual(set(selected), set(root) | {"source_path", "source_fid"})

    def test_save_readback_waits_for_index_without_resubmitting(self):
        self.drive.saved_read_delay = 2
        self.assertTrue(self.save(include=["*.txt"])["saved"])
        self.assertEqual(self.drive.saved_read_count, 3)
        self.assertEqual(self.sleep_mock.call_count, 2)
        self.assertEqual(len(self.drive.saves), 1)

    def test_persistent_save_readback_lag_fails_bounded_and_never_resubmits(self):
        self.drive.saved_read_delay = 20
        with self.assertRaisesRegex(RemoteError, "读回超时.*未重新提交"):
            self.save(include=["*.txt"])
        self.assertEqual(self.drive.saved_read_count, 6)
        self.assertEqual(self.sleep_mock.call_count, 5)
        self.assertEqual(len(self.drive.saves), 1)

    def test_task_fids_are_checked_as_members_without_positional_mapping(self):
        self.assertTrue(self.save(include=["*.txt", "*.log"])["saved"])
        self.assertEqual(self.drive.saves[0]["fid_list"], ["note", "skip"])
        self.assertEqual(len(self.drive.saves), 1)

    def test_unrelated_task_fid_cannot_validate_same_name_destination(self):
        self.drive.task_fids = ["unrelated"]
        self.drive.items.append(item("unrelated", "其他.txt", "target", size=4))
        with self.assertRaisesRegex(RemoteError, "不在任务返回的 FID"):
            self.save(include=["*.txt"])
        self.assertEqual(self.drive.saved_read_count, 1)
        self.assertEqual(len(self.drive.saves), 1)

    def test_task_target_mismatch_stops_before_readback(self):
        self.drive.task_target = "outside"
        with self.assertRaisesRegex(RemoteError, "目标目录与请求不符"):
            self.save(include=["*.txt"])
        self.assertEqual(self.drive.saved_read_count, 0)
        self.assertEqual(len(self.drive.saves), 1)

    def test_reported_fid_missing_from_destination_fails_after_bounded_reads(self):
        self.drive.task_fids = ["not-yet-visible"]
        with self.assertRaisesRegex(RemoteError, "读回超时"):
            self.save(include=["*.txt"])
        self.assertEqual(self.drive.saved_read_count, 6)
        self.assertEqual(len(self.drive.saves), 1)

    def test_duplicate_task_fids_fail_before_readback(self):
        self.drive.task_fids = ["saved-note", "saved-note"]
        with self.assertRaisesRegex(RemoteError, "FID 列表无效"):
            self.save(include=["*.txt"])
        self.assertEqual(self.drive.saved_read_count, 0)
        self.assertEqual(len(self.drive.saves), 1)

    def test_explicit_empty_task_fids_are_contradictory_and_never_resubmitted(self):
        self.drive.task_fids = []
        with self.assertRaisesRegex(RemoteError, "FID 列表无效"):
            self.save(include=["*.txt"])
        self.assertEqual(self.drive.saved_read_count, 0)
        self.assertEqual(len(self.drive.saves), 1)

    def test_invalid_parent_in_target_readback_fails_immediately_without_resubmit(self):
        def invalid_parent(records):
            self.drive.list_overrides["target"] = [{**records[0], "pdir_fid": "outside"}]
            return records

        self.drive.result_transform = invalid_parent
        with self.assertRaisesRegex(RemoteError, "无法确认目标目录内容"):
            self.save(include=["*.txt"])
        self.assertEqual(self.drive.saved_read_count, 1)
        self.assertEqual(len(self.drive.saves), 1)
        self.sleep_mock.assert_not_called()

    def test_destination_type_size_and_duplicate_mismatch_fail_without_resubmit(self):
        for transform, message in ((lambda records: [{**records[0], "file_type": 0}], "类型不符"),
                                   (lambda records: [{**records[0], "size": 999}], "大小不符"),
                                   (lambda records: records + [{**records[0], "fid": "duplicate-note"}], "同名项目")):
            with self.subTest(message=message):
                self.drive = ShareFixture()
                self.drive.result_transform = transform
                with self.assertRaisesRegex(RemoteError, message):
                    self.save(include=["*.txt"])
                self.assertEqual(self.drive.saved_read_count, 1)
                self.assertEqual(len(self.drive.saves), 1)

    def test_share_error_does_not_expose_remote_credentials_or_passcode(self):
        self.drive.detail_error = True
        self.run_cli(["share", "inspect", SHARE_URL, "--passcode", "private-passcode"], code=4)
        self.assertEqual(self.drive.writes, [])


if __name__ == "__main__":
    unittest.main()
