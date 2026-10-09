import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli.cli import main
from drive_fixture import DriveFixture


def ok(data):
    return {"status": 200, "code": 0, "data": data}


class TTYInput(io.StringIO):
    def isatty(self):
        return True


class CliTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"QUARK_COOKIE": "sid=cli-mock"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_organize_du_and_duplicates_json_and_scope(self):
        from test_organize import OrganizeFixture
        drive = OrganizeFixture()
        for arguments in (["--scope", "scope", "du", "folder"], ["duplicates", "folder", "--scope", "scope"]):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main([*arguments, "--json"], transport=drive.transport()), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["files"], 2)
        self.assertEqual(drive.writes, [])

    def test_organize_human_reports_and_default_rename_preview_no_state(self):
        from test_organize import OrganizeFixture
        drive = OrganizeFixture()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "absent-config"
            with patch.dict(os.environ, {"QUARK_CONFIG_DIR": str(state)}):
                for arguments in (["du", "folder"], ["duplicates", "folder"], ["rename-batch", "folder", "--pattern", "旧", "--replacement", "新"]):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(main(arguments, transport=drive.transport()), 0)
                    self.assertTrue(output.getvalue())
            self.assertFalse(state.exists())
        self.assertEqual(drive.writes, [])

    def test_rename_batch_apply_and_apply_dry_run_contract(self):
        from test_organize import OrganizeFixture
        for dry_run in (False, True):
            drive = OrganizeFixture()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = main(["rename-batch", "folder", "--pattern", "^旧", "--replacement", "新", "--apply", "--json",
                             *(["--dry-run"] if dry_run else [])], transport=drive.transport())
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            self.assertEqual((result["dry_run"], result["renamed"]), (dry_run, 0 if dry_run else 2))
            self.assertEqual(len(drive.rename_requests), 0 if dry_run else 2)

    def test_rename_unknown_is_nonzero_safe_operation_on_stderr(self):
        from test_organize import OrganizeFixture
        drive = OrganizeFixture()
        drive.loss = "before"
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            code = main(["rename-batch", "folder", "--pattern", "旧", "--replacement", "新", "--apply", "--json"], transport=drive.transport())
        self.assertEqual(code, 4)
        self.assertEqual(output.getvalue(), "")
        operation = json.loads(error.getvalue())["error"]["operation"]
        self.assertEqual(operation["result"], "unknown")
        self.assertEqual(operation["items"][0]["fid"], "one")
        self.assertNotIn("canary", error.getvalue())
        self.assertEqual(len(drive.rename_requests), 1)

    def test_rename_batch_partial_failure_has_confirmed_and_pending_items(self):
        from test_organize import OrganizeFixture
        drive = OrganizeFixture()
        drive.after_rename = lambda record: drive.items[3].update(updated_at=200)
        with contextlib.redirect_stderr(io.StringIO()) as error, contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["rename-batch", "folder", "--pattern", "旧", "--replacement", "新", "--apply", "--json"], transport=drive.transport())
        self.assertEqual(code, 2)
        operation = json.loads(error.getvalue())["error"]["operation"]
        self.assertEqual((operation["result"], operation["renamed"]), ("partial", 1))
        self.assertEqual([item["result"] for item in operation["items"]], ["confirmed", "pending", "unchanged"])
        self.assertEqual(output.getvalue(), "")

    def test_organize_cli_result_and_error_evidence_white_list(self):
        from quark_cli.errors import RemoteError
        result = {"action": "du", "files": 1, "directories": 0, "known_bytes": 1, "unknown_size_files": 0, "complete": True, "auth_key": "private-canary-secret"}
        with patch("quark_cli.cli.Organize.du", return_value=result), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["du", "one", "--json"], transport=DriveFixture().transport()), 0)
        self.assertNotIn("canary", output.getvalue())
        failure = RemoteError("已停止")
        failure.organize_evidence = {"action": "rename-batch", "result": "unknown", "token": "private-canary-secret", "items": [{"fid": "one", "result": "unknown", "auth_key": "private-canary-secret"}]}
        with patch("quark_cli.cli.Organize.rename_batch", side_effect=failure), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["rename-batch", "folder", "--pattern", "x", "--replacement", "y", "--apply", "--json"], transport=DriveFixture().transport()), 4)
        self.assertNotIn("canary", error.getvalue())

    def test_rename_invalid_regex_zero_network_and_safe_usage(self):
        with contextlib.redirect_stderr(io.StringIO()) as error:
            code = main(["rename-batch", "folder", "--pattern", "[private-canary-secret", "--replacement", "x", "--apply", "--json"],
                        transport=httpx.MockTransport(lambda request: self.fail("invalid regex performed network")))
        self.assertEqual(code, 2)
        self.assertNotIn("canary", error.getvalue())

    def test_scoped_rename_invalid_replacement_rejected_before_auth_or_network(self):
        for scope_before in (False, True):
            arguments = ["rename-batch", "empty", "--pattern", "(x)", "--replacement", r"\2", "--apply", "--json"]
            arguments = ["--scope", "scope", *arguments] if scope_before else [*arguments, "--scope", "scope"]
            with patch("quark_cli.cli.get_cookie", return_value="sid=cli-mock") as cookie, contextlib.redirect_stderr(io.StringIO()) as error:
                code = main(arguments, transport=httpx.MockTransport(lambda request: self.fail("invalid replacement performed network")))
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(error.getvalue())["error"]["type"], "usage_error")
            cookie.assert_not_called()

    def test_stage9_help_explains_read_only_reports_and_default_preview(self):
        expectations = {"du": ("未知大小", "统计"), "duplicates": ("候选", "不删除"),
                        "rename-batch": ("默认预览", "--apply --dry-run"), "backup": ("默认预览", "--apply --dry-run")}
        for command, descriptions in expectations.items():
            with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as caught:
                main([command, "--help"], transport=httpx.MockTransport(lambda request: self.fail("help performed network")))
            self.assertEqual(caught.exception.code, 0)
            for description in descriptions:
                self.assertIn(description, output.getvalue())

    def test_stage9_scope_failure_does_not_expose_raw_remote_message(self):
        arguments = (["du", "folder"], ["duplicates", "folder"],
                     ["rename-batch", "folder", "--pattern", "x", "--replacement", "y", "--apply"],
                     ["backup", "local", "--to", "folder", "--apply"])
        for command in arguments:
            for remote_code, exit_code, label in ((5001, 4, "remote_error"), (401, 3, "authentication_error")):
                calls = []
                def fail_scope(request):
                    calls.append(request)
                    return httpx.Response(200, json={"status": 200, "code": remote_code, "message": "private-canary-secret", "data": {}})
                with contextlib.redirect_stderr(io.StringIO()) as error, contextlib.redirect_stdout(io.StringIO()) as output:
                    code = main([*command, "--scope", "scope", "--json"], transport=httpx.MockTransport(fail_scope))
                self.assertEqual(code, exit_code)
                self.assertEqual(json.loads(error.getvalue())["error"]["type"], label)
                self.assertNotIn("canary", error.getvalue())
                self.assertEqual(output.getvalue(), "")
                self.assertTrue(calls)
                self.assertTrue(all(request.method == "GET" for request in calls))

    def test_backup_flags_default_preview_apply_dry_run_progress_and_limit_contract(self):
        from quark_cli.transfer_progress import ProgressEvent
        result = {"result": "preview", "dry_run": True, "source": "local", "target_fid": "folder", "files": 1, "directories": 1,
                  "uploaded_files": 0, "updated_files": 0, "skipped_files": 0, "created_directories": 0, "items": [], "archives": [], "pending": None,
                  "auth_key": "private-canary-secret"}
        for flags, applied in (([], False), (["--apply"], True), (["--apply", "--dry-run"], False)):
            with patch("quark_cli.backup.Backup.__init__", return_value=None), patch("quark_cli.backup.Backup.run", return_value=result) as run, contextlib.redirect_stdout(io.StringIO()) as output:
                code = main(["backup", "local", "--to", "folder", "--update", "--progress", "--limit-rate", "2048", "--json", *flags], transport=DriveFixture().transport())
            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.kwargs["apply"], applied)
            self.assertTrue(run.call_args.kwargs["update"])
            self.assertEqual(run.call_args.kwargs["limit_rate"], 2048)
            self.assertNotIn("canary", output.getvalue())
            with contextlib.redirect_stderr(io.StringIO()) as error, contextlib.redirect_stdout(io.StringIO()) as progress_out:
                run.call_args.kwargs["progress"](ProgressEvent("upload", "part_confirmed", "测试.txt", 1, 1, 1, 1.0))
            self.assertIn("测试.txt", error.getvalue())
            self.assertEqual(progress_out.getvalue(), "")

    def test_backup_error_evidence_white_list_and_human_archive(self):
        from quark_cli.errors import RemoteError
        failure = RemoteError("备份未完成")
        failure.backup_evidence = {"result": "unknown", "target_fid": "folder", "auth_key": "private-canary-secret",
            "pending": {"kind": "rename", "path": "测试.txt", "phase": "archive_submitting", "fid": "one", "signature": "private-canary-secret"}}
        with patch("quark_cli.backup.Backup.__init__", return_value=None), patch("quark_cli.backup.Backup.run", side_effect=failure), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["backup", "local", "--to", "folder", "--apply", "--json"], transport=DriveFixture().transport()), 4)
        self.assertEqual(json.loads(error.getvalue())["error"]["operation"]["pending"]["fid"], "one")
        self.assertNotIn("canary", error.getvalue())
        result = {"result": "completed", "dry_run": False, "uploaded_files": 0, "updated_files": 1, "skipped_files": 0,
                  "created_directories": 0, "archives": [{"fid": "one", "name": "测试.archive.txt", "path": "测试.txt"}], "items": []}
        with patch("quark_cli.backup.Backup.__init__", return_value=None), patch("quark_cli.backup.Backup.run", return_value=result), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["backup", "local", "--to", "folder", "--apply", "--update"], transport=DriveFixture().transport()), 0)
        self.assertIn("测试.archive.txt", output.getvalue())

    def test_backup_rate_and_recursive_flag_usage_rejected_before_network(self):
        for flags in (["--limit-rate", "0"], ["--limit-rate", "-2"], ["--limit-rate", "1.2"], ["--recursive"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(["backup", "local", "--to", "folder", "--json", *flags],
                     transport=httpx.MockTransport(lambda request: self.fail("invalid arguments performed network")))
            self.assertEqual(caught.exception.code, 2)

    def test_share_management_create_and_list_safe_json(self):
        from share_management_fixture import ManagementFixture
        drive = ManagementFixture()
        with patch("quark_cli.share_management.time.sleep"), contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["share", "create", "/范围/中文 空格/报告.PDF", "--passcode", "Ab12", "--expire-days", "1",
                         "--scope", "scope", "--json"], transport=drive.transport())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["passcode"], "Ab12")
        self.assertNotIn("secret", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["--scope", "scope", "share", "list", "--all", "--json"], transport=drive.transport())
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["verified_total"], 1)
        self.assertNotIn("Ab12", output.getvalue())
        self.assertNotIn("secret", output.getvalue())

    def test_share_revoke_yes_barrier_before_scope_or_cookie_network(self):
        with contextlib.redirect_stderr(io.StringIO()) as error:
            code = main(["share", "revoke", "own-share", "--scope", "scope", "--json"],
                        transport=httpx.MockTransport(lambda request: self.fail("no yes must be zero network")))
        self.assertEqual(code, 2)
        self.assertIn("--yes", json.loads(error.getvalue())["error"]["message"])

    def test_share_create_revoke_preview_remote_zero_write_and_no_state_change(self):
        from share_management_fixture import ManagementFixture
        drive = ManagementFixture()
        drive.add_share()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "private-state"
            state.mkdir()
            ledger = state / "ledger.json"
            ledger.write_bytes(b"existing-state")
            before = (ledger.read_bytes(), ledger.stat().st_mtime_ns)
            for arguments in (["share", "create", "file"], ["share", "revoke", "own-share"]):
                with patch.dict(os.environ, {"QUARK_CONFIG_DIR": str(state)}), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main([*arguments, "--scope", "scope", "--dry-run", "--json"], transport=drive.transport()), 0)
                self.assertTrue(json.loads(output.getvalue())["dry_run"])
            self.assertEqual(before, (ledger.read_bytes(), ledger.stat().st_mtime_ns))
            self.assertEqual(list(state.iterdir()), [ledger])
            self.assertEqual(drive.writes, [])

    def test_share_revoke_real_confirmation_and_error_evidence_json(self):
        from share_management_fixture import ManagementFixture
        drive = ManagementFixture()
        drive.add_share()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["share", "revoke", "own-share", "--yes", "--json"], transport=drive.transport()), 0)
        self.assertTrue(json.loads(output.getvalue())["revoked"])
        drive.add_share("still-present")
        drive.delete_delay = 100
        with patch("quark_cli.share_management.time.sleep"), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            code = main(["share", "revoke", "still-present", "--yes", "--json"], transport=drive.transport())
        self.assertEqual(code, 4)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(error.getvalue())["error"]["operation"]["share_id"], "still-present")

    def test_share_partial_error_retains_verified_cleanup_link_not_raw_snapshot(self):
        from share_management_fixture import ManagementFixture
        drive = ManagementFixture()
        drive.task_data["creation_snapshot"] = {"success_count": 1, "failed_count": 1, "invalid_reason": "private-reason-secret"}
        with patch("quark_cli.share_management.time.sleep"), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            code = main(["share", "create", "file", "--passcode", "Ab12", "--json"], transport=drive.transport())
        self.assertEqual(code, 4)
        self.assertEqual(output.getvalue(), "")
        operation = json.loads(error.getvalue())["error"]["operation"]
        self.assertEqual((operation["result"], operation["task_id"], operation["passcode"]), ("partial", "create-task", "Ab12"))
        self.assertIn("share_url", operation)
        self.assertNotIn("secret", error.getvalue())

    def test_share_unknown_human_error_preserves_task_and_sanitizes_remote_message(self):
        from share_management_fixture import ManagementFixture
        drive = ManagementFixture()
        drive.task_timeout = True
        with patch("quark_cli.share_management.time.sleep"), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            code = main(["share", "create", "file"], transport=drive.transport())
        self.assertEqual(code, 4)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("create-task", error.getvalue())
        self.assertNotIn("secret", error.getvalue())

    def test_share_invalid_passcode_not_echoed_zero_write(self):
        from share_management_fixture import ManagementFixture
        for options in (["--passcode", "private-code-secret"],):
            drive = ManagementFixture()
            with contextlib.redirect_stderr(io.StringIO()) as error:
                code = main(["share", "create", "file", *options, "--json"], transport=drive.transport())
            self.assertEqual(code, 2)
            self.assertNotIn("private-code-secret", error.getvalue())
            self.assertEqual(drive.calls, [])

    def test_share_custom_title_is_unsupported_and_rejected_before_network(self):
        with contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as caught:
            main(["share", "create", "file", "--title", "custom title", "--json"],
                 transport=httpx.MockTransport(lambda request: self.fail("unsupported title made a request")))
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(json.loads(error.getvalue())["error"]["exit_code"], 2)

    def test_share_cli_whitelists_service_result_and_error_evidence(self):
        from quark_cli.share_management import _operation_error
        raw = {"result": "created", "pwd_id": "public", "share_url": "https://pan.quark.cn/s/public?pwd=Ab12",
               "passcode": "Ab12", "token": "secret", "nested": {"auth": "secret"}}
        with patch("quark_cli.cli.ShareManager.create", return_value=raw), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["share", "create", "file", "--json"], transport=DriveFixture().transport()), 0)
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("?", output.getvalue())
        error = _operation_error("unknown", {**raw, "result": "unknown"})
        error.share_evidence["token"] = "secret"
        with patch("quark_cli.cli.ShareManager.create", side_effect=error), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["share", "create", "file", "--json"], transport=DriveFixture().transport()), 4)
        self.assertNotIn("secret", stderr.getvalue())

    def test_transfer_rate_rejects_nonpositive_or_noninteger_before_requests(self):
        for command in ("upload", "download"):
            for value in ("0", "-1", "1.2", "abc"):
                with self.subTest(command=command, value=value), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                    arguments = [command, "source", "--limit-rate", value, "--json"]
                    if command == "download":
                        arguments += ["--output", "target"]
                    with self.assertRaises(SystemExit) as caught:
                        main(arguments, transport=httpx.MockTransport(lambda request: self.fail("invalid rate made a request")))
                    self.assertEqual(caught.exception.code, 2)
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual(json.loads(error.getvalue())["error"]["type"], "usage_error")

    def test_download_connections_are_bounded_and_forwarded(self):
        for value in ("0", "17", "1.5", "many"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as caught:
                    main(["download", "file", "--output", "target", "--connections", value],
                         transport=httpx.MockTransport(lambda request: self.fail("invalid connections made a request")))
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("并发连接数", error.getvalue())

        with patch("quark_cli.cli.Transfer.download", return_value={"fid": "fid-1", "path": "target", "size": 1}) as download, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["download", "file", "--output", "target", "--connections", "16"],
                                  transport=DriveFixture().transport()), 0)
        self.assertEqual(download.call_args.kwargs["connections"], 16)

    def test_single_transfer_new_options_and_progress_keep_json_stdout_clean(self):
        from quark_cli.transfer_progress import ProgressEvent
        def uploaded(path, parent, **options):
            self.assertEqual((options["resume"], options["limit_rate"]), (True, 123))
            options["progress"](ProgressEvent("upload", "completed", "safe\x1b name", 5, 5, 5, 2.0))
            return {"fid": "new-file", "file_name": "safe.txt", "file_size": 5, "task_id": "task", "parts": 1,
                    "resumed": True, "skipped_parts": 1, "sha256": "a" * 64, "auth_info": {"token": "secret"}}
        with patch("quark_cli.cli.Transfer.upload", side_effect=uploaded), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["upload", "file.txt", "--resume", "--progress", "--limit-rate", "123", "--json"], transport=httpx.MockTransport(lambda request: self.fail("unexpected request"))), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["resumed"])
        self.assertNotIn("auth_info", result)
        self.assertNotIn("upload completed", output.getvalue())
        self.assertIn("upload completed safe? name", error.getvalue())
        self.assertNotIn("\x1b", error.getvalue())

    def test_recursive_cli_dispatches_flags_scope_and_filters_result(self):
        drive = DriveFixture()
        def uploaded(source, parent, **options):
            self.assertEqual((source, parent, options["resume"], options["limit_rate"], options["dry_run"]), ("dir", "target", True, 10, False))
            self.assertEqual(options["scope"].fid, "scope")
            self.assertTrue(callable(options["progress"]))
            return {"uploaded": True, "recursive": True, "files": 1, "directories": 2, "size": 4, "destination_fid": "root", "token": "secret", "nested": {"url": "signature"}}
        with patch("quark_cli.cli.RecursiveTransfer.upload_tree", side_effect=uploaded), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["upload", "dir", "--recursive", "--resume", "--progress", "--limit-rate", "10", "--parent", "/范围/目标", "--scope", "scope", "--json"], transport=drive.transport()), 0)
        self.assertEqual(set(json.loads(output.getvalue())), {"uploaded", "recursive", "files", "directories", "size", "destination_fid"})
        with patch("quark_cli.cli.RecursiveTransfer.download_tree", return_value={"downloaded": True, "recursive": True, "fid": "target", "path": "output", "files": 0, "directories": 1, "size": 0}) as download, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["download", "/范围/目标", "--output", "output", "--recursive", "--overwrite", "--resume", "--json"], transport=drive.transport()), 0)
        download.assert_called_once_with("target", "output", overwrite=True, resume=True, connections=16)

    def test_recursive_upload_preview_real_scan_zero_writes_and_no_state(self):
        drive = DriveFixture()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "中文源"
            source.mkdir()
            (source / "空目录").mkdir()
            (source / "文件.txt").write_bytes(b"test")
            state = Path(temporary) / "state"
            with patch.dict(os.environ, {"QUARK_CONFIG_DIR": str(state)}), contextlib.redirect_stdout(io.StringIO()) as output:
                code = main(["upload", str(source), "--parent", "scope", "--scope", "scope", "--recursive", "--dry-run", "--json"], transport=drive.transport())
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["files"], 1)
            self.assertEqual(drive.writes, [])
            self.assertFalse(state.exists())

    def test_directories_require_explicit_recursive_flag(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["upload", temporary], transport=httpx.MockTransport(lambda request: self.fail("directory required flag"))), 2)
            self.assertIn("--recursive", error.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["download", "/", "--output", "output"], transport=httpx.MockTransport(lambda request: self.fail("unexpected request"))), 2)
            self.assertIn("--recursive", error.getvalue())

    def test_missing_cookie_does_not_access_network(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=ok({"list": []}), request=request)

        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()) as errors:
            result = main(["ls"], transport=httpx.MockTransport(handler))
        self.assertEqual(result, 3)
        self.assertEqual(calls, [])
        self.assertIn("QUARK_COOKIE", errors.getvalue())

    def test_json_flag_and_list_command(self):
        def handler(request):
            return httpx.Response(200, json=ok({"list": [{"fid": "one", "file_name": "a.txt", "file_type": 1, "size": 2}], "_total": 1}), request=request)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["--json", "ls", "0"], transport=httpx.MockTransport(handler))
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["data"]["list"][0]["fid"], "one")

    def test_json_outputs_project_safe_fields_from_remote_responses(self):
        signed_url = "https://dl-pc-sz.pds.quark.cn/file?auth_key=signed-url-private"
        secrets = ("signed-url-private", "remote-auth-private", "remote-token-private")

        def unsafe_fields():
            return {
                "preview_url": signed_url,
                "download_url": signed_url,
                "auth_key": "remote-auth-private",
                "nested": {"token": "remote-token-private", "url": signed_url},
            }

        def handler(request):
            path = request.url.path
            if path.endswith("/file/sort"):
                return httpx.Response(
                    200,
                    json={"status": 200, "code": 0, "data": {
                        "list": [{"fid": "listed", "file_name": "listed.txt", "file_type": 1, "size": 3, "pdir_fid": "0", **unsafe_fields()}],
                        "_total": 1,
                        **unsafe_fields(),
                    }, "metadata": unsafe_fields()},
                    request=request,
                )
            if path.endswith("/file/info"):
                return httpx.Response(
                    200,
                    json=ok({"fid": "detail", "file_name": "detail.txt", "file_type": 1, "size": 4, "pdir_fid": "folder", **unsafe_fields()}),
                    request=request,
                )
            if path.endswith("/file") or path.endswith(("/file/rename", "/file/move", "/file/delete")):
                return httpx.Response(200, json=ok({"fid": "created", "task_id": "task-safe", "finish": True, **unsafe_fields()}), request=request)
            return httpx.Response(404, request=request)

        outputs = {}

        def run_json(name, args):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = main(["--json", *args], transport=httpx.MockTransport(handler))
            self.assertEqual(result, 0, name)
            output = stdout.getvalue() + stderr.getvalue()
            for secret in secrets:
                self.assertNotIn(secret, output, name)
            outputs[name] = json.loads(stdout.getvalue())

        run_json("ls", ["ls", "0"])
        run_json("info", ["info", "detail"])
        run_json("mkdir", ["mkdir", "new-dir"])
        run_json("rename", ["rename", "fid-1", "renamed.txt"])
        run_json("move", ["mv", "fid-1", "--to", "0"])
        run_json("delete", ["rm", "fid-1", "--yes"])

        with patch("quark_cli.cli.Transfer.upload", return_value={
            "fid": "uploaded", "file_name": "upload.txt", "file_size": 5, "task_id": "task-safe",
            "parts": 1, "callback_warning": False, "result": unsafe_fields(), **unsafe_fields(),
        }):
            run_json("upload", ["upload", "local.txt"])
        with patch("quark_cli.cli.Transfer.download", return_value={
            "fid": "downloaded", "path": "local.txt", "size": 6, "result": unsafe_fields(), **unsafe_fields(),
        }):
            run_json("download", ["download", "fid-1", "--output", "local.txt"])

        self.assertEqual(set(outputs["upload"]), {"fid", "file_name", "file_size", "task_id", "parts", "callback_warning"})
        self.assertEqual(set(outputs["download"]), {"fid", "path", "size"})
        self.assertEqual(set(outputs["mkdir"]), {"created", "name", "parent", "fid", "task_id"})
        self.assertNotIn("result", outputs["rename"])
        self.assertNotIn("result", outputs["move"])
        self.assertNotIn("result", outputs["delete"])
        self.assertNotIn("preview_url", json.dumps(outputs, ensure_ascii=False))

    def test_json_argument_error_is_machine_readable(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as caught:
                main(["--json", "download", "fid"], transport=httpx.MockTransport(lambda r: httpx.Response(500, request=r)))
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(json.loads(errors.getvalue())["error"]["exit_code"], 2)

    def test_delete_requires_yes_in_noninteractive_mode(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=ok({}), request=request)

        with patch("sys.stdin", io.StringIO("")), contextlib.redirect_stderr(io.StringIO()):
            result = main(["rm", "fid-1"], transport=httpx.MockTransport(handler))
        self.assertEqual(result, 2)
        self.assertEqual(calls, [])

    def test_cancelled_delete_sends_no_request(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=ok({}), request=request)

        with patch("sys.stdin", TTYInput("n\n")), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            result = main(["rm", "fid-1"], transport=httpx.MockTransport(handler))
        self.assertEqual(result, 0)
        self.assertEqual(calls, [])

    def test_confirmed_delete_calls_delete_once(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=ok({}), request=request)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["rm", "fid-1", "--yes", "--json"], transport=httpx.MockTransport(handler))
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(output.getvalue())["semantics"], "moved_to_recycle_bin")

    def test_cookie_file_is_read_without_network_side_effects(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=ok({"list": []}), request=request)

        with tempfile.TemporaryDirectory() as directory:
            cookie_file = os.path.join(directory, "cookie.txt")
            with open(cookie_file, "w", encoding="utf-8") as file:
                file.write("sid=file-cookie\n")
            with patch.dict(os.environ, {"QUARK_COOKIE_FILE": cookie_file}, clear=True):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = main(["auth", "status"], transport=httpx.MockTransport(handler))
        self.assertEqual(result, 0)
        self.assertEqual(calls[0].headers["cookie"], "sid=file-cookie")

    def test_qr_login_command_emits_json_without_credentials(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        def fake_run(*, qr_file, on_qr_ready):
            on_qr_ready(Path("temporary-qr.png"))

        with patch("quark_cli.cli.QRLogin") as qr_login, patch("quark_cli.cli.get_cookie", side_effect=AssertionError):
            qr_login.return_value.run.side_effect = fake_run
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = main(["--json", "auth", "login", "--timeout", "15"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"authenticated": True, "credential_saved": True})
        self.assertIn("temporary-qr.png", stderr.getvalue())
        self.assertIn("网页版登录二维码", stderr.getvalue())
        output = stdout.getvalue() + stderr.getvalue()
        leaked = any(secret in output for secret in ("qr-token-private", "service-ticket-private", "cookie-private"))
        self.assertFalse(leaked, "login credentials reached CLI output")

    def test_auth_logout_does_not_read_or_change_environment_credentials(self):
        stdout = io.StringIO()
        with patch.dict(os.environ, {"QUARK_COOKIE": "sid=environment-private"}), patch(
            "quark_cli.cli.get_cookie", side_effect=AssertionError
        ), patch("quark_cli.cli.logout_cookie", return_value=True):
            with contextlib.redirect_stdout(stdout):
                result = main(["auth", "logout", "--json"])
            self.assertEqual(os.environ["QUARK_COOKIE"], "sid=environment-private")
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"logged_out": True, "saved_cookie_removed": True})
        self.assertNotIn("environment-private", stdout.getvalue())

    def test_login_timeout_is_bounded(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["auth", "login", "--timeout", "301"])
        self.assertEqual(caught.exception.code, 2)

    def test_existing_file_commands_resolve_absolute_paths_to_fids(self):
        commands = [
            (["ls", "/范围/中文 空格"], "file/sort", "child"),
            (["info", "/范围/中文 空格/报告.PDF"], "file/info", "file"),
            (["rename", "/范围/中文 空格/报告.PDF", "新名"], "file/rename", "file"),
            (["mv", "/范围/中文 空格/报告.PDF", "--to", "/范围/目标"], "file/move", "file"),
            (["rm", "/范围/中文 空格/报告.PDF", "--yes"], "file/delete", "file"),
            (["mkdir", "新目录", "--parent", "/范围/目标"], "file", "target"),
            (["mkdir", "/范围/目标/新目录"], "file", "target"),
        ]
        for arguments, endpoint, expected in commands:
            with self.subTest(arguments=arguments):
                drive = DriveFixture()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
                    result = main([*arguments, "--json"], transport=drive.transport())
                self.assertEqual(result, 0, stderr.getvalue())
                request = drive.calls[-1]
                self.assertTrue(request.url.path.endswith("/" + endpoint))
                if request.method == "GET":
                    self.assertEqual(request.url.params.get("fid", request.url.params.get("pdir_fid")), expected)
                else:
                    body = json.loads(request.read())
                    self.assertEqual(body.get("fid", body.get("pdir_fid", body.get("filelist", [None])[0])), expected)
                    if endpoint == "file/move":
                        self.assertEqual(body["to_pdir_fid"], "target")

    def test_upload_download_paths_are_resolved_before_transfer(self):
        drive = DriveFixture()
        with patch("quark_cli.cli.Transfer.upload", return_value={"file_name": "file.txt", "file_size": 3, "parts": 1}) as upload:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["upload", "file.txt", "--parent", "/范围/目标"], transport=drive.transport()), 0)
            upload.assert_called_once_with("file.txt", "target")
        with patch("quark_cli.cli.Transfer.download", return_value={"fid": "file", "path": "local.pdf", "size": 4}) as download:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["download", "/范围/中文 空格/报告.PDF", "--output", "local.pdf"], transport=drive.transport()), 0)
            download.assert_called_once_with("file", "local.pdf", overwrite=False, connections=16)

    def test_mkdir_complete_path_rejects_parent_option_and_existing_directory(self):
        for args in (["mkdir", "/范围/新目录", "--parent", "0"], ["mkdir", "/范围"], ["mkdir", "/缺失/中间/末级"]):
            drive = DriveFixture()
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args, transport=drive.transport()), 2)
            self.assertEqual(drive.writes, [])

    def test_tree_search_use_subtree_names_and_safe_json_fields(self):
        drive = DriveFixture()
        drive.items[2].update({"download_url": "https://cdn.quark.cn/mock?signature=private", "nested": {"token": "secret"}})
        for arguments in (["tree", "/范围", "--depth", "2"], ["search", "pdf", "--in", "/范围"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main([*arguments, "--json"], transport=drive.transport()), 0)
            result = json.loads(stdout.getvalue())
            self.assertNotIn("private", stdout.getvalue())
            self.assertNotIn("secret", stdout.getvalue())
            if arguments[0] == "search":
                self.assertEqual([record["path"] for record in result["items"]], ["/范围/中文 空格/报告.PDF"])
            self.assertTrue(all({"fid", "file_type", "path", "depth"} <= set(record) for record in result["items"]))

    def test_qr_terminal_is_explicit_and_only_emits_matrix_to_stderr(self):
        from quark_cli.auth import build_qr_url, render_terminal_qr

        for terminal in (False, True):
            def fake_run(*, qr_file, on_qr_ready, on_qr_terminal=None):
                on_qr_ready(Path("temporary-qr.png"))
                if on_qr_terminal:
                    on_qr_terminal(render_terminal_qr(build_qr_url("qr-token-private")))

            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(terminal=terminal), patch("quark_cli.cli.QRLogin") as login:
                login.return_value.run.side_effect = fake_run
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = main(["auth", "login", "--json", *(["--qr-terminal"] if terminal else [])])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stdout.getvalue()), {"authenticated": True, "credential_saved": True})
            self.assertEqual("\x1b[30;47m" in stderr.getvalue(), terminal)
            self.assertNotIn("qr-token-private", stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
