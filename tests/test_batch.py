import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli.batch import BatchSaver, load_batch
from quark_cli.cli import main
from quark_cli.client import QuarkClient
from quark_cli.errors import LocalIOError, UsageError
from quark_cli.paths import Scope
from quark_cli.shares import ShareSaver

from drive_fixture import item
from share_fixture import SHARE_URL, SOURCE_PATH, ShareFixture


SECOND_URL = "https://pan.quark.cn/s/offline-second"
THIRD_URL = "https://pan.quark.cn/s/offline-third"


class BatchProtocolFixture(ShareFixture):
    def __init__(self):
        super().__init__()
        self.failed_sources = set()
        self.failed_submits = set()
        self.failed_tasks = set()
        self.failed_mkdir_names = set()

    def handler(self, request):
        path = request.url.path
        if path.endswith("/share/sharepage/detail") and request.url.params["pwd_id"] in self.failed_sources:
            self.calls.append(request)
            return httpx.Response(500, json={"status": 500, "code": 1,
                "message": "stoken-private private-passcode private-signature"}, request=request)
        if path.endswith("/share/sharepage/save") and json.loads(request.content)["pwd_id"] in self.failed_submits:
            self.calls.append(request)
            self.saves.append(json.loads(request.content))
            raise httpx.ReadTimeout("private-passcode stoken-private", request=request)
        if path.endswith("/task") and self.saves[-1]["pwd_id"] in self.failed_tasks:
            self.calls.append(request)
            return self.response(request, {"status": 3})
        if path.endswith("/file") and request.method == "POST" and json.loads(request.content)["file_name"] in self.failed_mkdir_names:
            self.calls.append(request)
            raise httpx.ReadTimeout("private-passcode stoken-private", request=request)
        return super().handler(request)


class BatchFiles(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def txt(self, content, *, encoding="utf-8"):
        path = self.root / "输入.txt"
        path.write_text(content, encoding=encoding)
        return path

    def csv(self, rows):
        path = self.root / "输入.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            csv.writer(stream).writerows(rows)
        return path


class BatchInputTests(BatchFiles):
    def test_txt_bom_comments_and_physical_line_numbers(self):
        path = self.txt(f"# 注释\n\n{SHARE_URL}\n  # 第二注释\n{SECOND_URL}\n", encoding="utf-8-sig")
        rows = load_batch(path, destination="/范围/目标", source=SOURCE_PATH)
        self.assertEqual([row.line for row in rows], [3, 5])
        self.assertEqual([row.share_id for row in rows], ["offline-share", "offline-second"])
        self.assertTrue(all(row.source == SOURCE_PATH for row in rows))

    def test_csv_bom_quotes_chinese_paths_and_passcode_repr_privacy(self):
        path = self.csv([["url", "to", "passcode", "source"],
                         [SHARE_URL + "?pwd=private-passcode", "/范围/目标, 二", "explicit-private-passcode", "/公开源/中文, 子目录"]])
        row = load_batch(path)[0]
        self.assertEqual((row.line, row.destination, row.source, row.passcode), (2, "/范围/目标, 二", "/公开源/中文, 子目录", "explicit-private-passcode"))
        self.assertNotIn("private-passcode", repr(row))
        self.assertNotIn("?pwd=", repr(row))

    def test_csv_row_target_and_source_override_cli_defaults(self):
        path = self.csv([["url", "to", "source"], [SHARE_URL, "/范围/目标", SOURCE_PATH], [SECOND_URL, "", ""]])
        rows = load_batch(path, destination="/范围/中文 空格", source="/公开源")
        self.assertEqual([(row.destination, row.source) for row in rows], [("/范围/目标", SOURCE_PATH), ("/范围/中文 空格", "/公开源")])

    def test_txt_requires_uniform_target_and_csv_requires_row_or_default_target(self):
        with self.assertRaisesRegex(UsageError, "TXT"):
            load_batch(self.txt(SHARE_URL))
        with self.assertRaisesRegex(UsageError, "缺少目标"):
            load_batch(self.csv([["url"], [SHARE_URL]]))

    def test_all_rows_are_validated_without_disclosing_bad_input(self):
        with self.assertRaisesRegex(UsageError, "第 2 行") as failure:
            load_batch(self.txt(SHARE_URL + "\nhttps://evil.invalid/s/private-passcode\n"), destination="/范围/目标")
        self.assertNotIn("private-passcode", str(failure.exception))

    def test_csv_unknown_duplicate_missing_headers_and_wrong_field_counts(self):
        cases = ([["url", "secret"], [SHARE_URL, "private-passcode"]],
                 [["url", "url"], [SHARE_URL, SHARE_URL]], [["to"], ["/范围/目标"]],
                 [["url", "to"], [SHARE_URL]], [["url"], [SHARE_URL, "extra"]])
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(UsageError):
                load_batch(self.csv(rows), destination="/范围/目标")

    def test_malformed_csv_quoting_is_rejected(self):
        path = self.root / "输入.csv"
        path.write_text('url,to\n"https://pan.quark.cn/s/offline-share,/范围/目标\n', encoding="utf-8")
        with self.assertRaisesRegex(UsageError, "CSV"):
            load_batch(path)

    def test_duplicate_operations_with_different_passcodes_are_rejected(self):
        path = self.txt(SHARE_URL + "?pwd=one\n" + SHARE_URL + "?pwd=two\n")
        with self.assertRaisesRegex(UsageError, "重复指定"):
            load_batch(path, destination="/范围/目标")

    def test_invalid_encoding_missing_file_and_unsupported_extension(self):
        path = self.root / "输入.txt"
        path.write_bytes(b"\xff\xfe\x00")
        with self.assertRaisesRegex(UsageError, "UTF-8"):
            load_batch(path, destination="/范围/目标")
        with self.assertRaises(LocalIOError):
            load_batch(self.root / "missing.csv")
        with self.assertRaisesRegex(UsageError, "扩展名"):
            load_batch(self.root / "input.json")

    def test_invalid_source_and_target_syntax_are_rejected(self):
        for target, source in (("relative", "/"), ("/范围/目标", "/../"), ("/范围/目标/", "/")):
            with self.subTest(target=target, source=source), self.assertRaises(UsageError):
                load_batch(self.txt(SHARE_URL), destination=target, source=source)


class BatchWorkflowTests(BatchFiles):
    def setUp(self):
        super().setUp()
        self.drive = BatchProtocolFixture()
        for name in ("_PAGE_SIZE",):
            context = patch(f"quark_cli.shares.{name}", 2)
            context.start()
            self.addCleanup(context.stop)
        context = patch("quark_cli.shares.time.sleep")
        context.start()
        self.addCleanup(context.stop)

    def run_cli(self, arguments, *, code=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"QUARK_COOKIE": "sid=offline"}, clear=True):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = main(["--json", "save-batch", *map(str, arguments)], transport=self.drive.transport())
        self.assertEqual(status, code, stderr.getvalue())
        for secret in ("stoken-private", "private-token-", "private-passcode", "private-signature", "private-auth", "?pwd="):
            self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())
        return json.loads(stdout.getvalue() if stdout.getvalue() else stderr.getvalue())

    def two_targets(self):
        return self.csv([["url", "to", "source"], [SHARE_URL, "/范围/目标", SOURCE_PATH], [SECOND_URL, "/范围/中文 空格", SOURCE_PATH]])

    def test_serial_success_preflights_every_source_before_first_remote_write(self):
        result = self.run_cli([self.two_targets(), "--scope", "scope"])
        self.assertEqual((result["total"], result["succeeded"], result["failed"]), (2, 2, 0))
        self.assertEqual([row["result"] for row in result["rows"]], ["saved", "saved"])
        self.assertEqual([body["to_pdir_fid"] for body in self.drive.saves], ["target", "child"])
        first_write = next(index for index, request in enumerate(self.drive.calls) if request in self.drive.writes)
        inspected = {request.url.params["pwd_id"] for request in self.drive.calls[:first_write] if request.url.path.endswith("/share/sharepage/detail")}
        self.assertEqual(inspected, {"offline-share", "offline-second"})
        events = ["submit" if request.url.path.endswith("/share/sharepage/save") else "task" for request in self.drive.calls if request.url.path.endswith(("/share/sharepage/save", "/task"))]
        self.assertEqual(events, ["submit", "task", "submit", "task"])

    def test_late_invalid_input_aborts_entire_batch_before_remote_writes(self):
        result = self.run_cli([self.txt(SHARE_URL + "\nhttps://invalid.example/private-passcode\n"), "--to", "/范围/目标"], code=2)
        self.assertEqual(result["error"]["type"], "usage_error")
        self.assertEqual(self.drive.writes, [])
        self.assertEqual(self.drive.calls, [])

    def test_late_outside_scope_target_aborts_entire_batch_before_source_reads(self):
        path = self.csv([["url", "to"], [SHARE_URL, "/范围/新目录"], [SECOND_URL, "/范围外/新目录"]])
        self.run_cli([path, "--scope", "scope"], code=2)
        self.assertEqual(self.drive.writes, [])
        self.assertFalse(any(request.url.path.endswith("/share/sharepage/token") for request in self.drive.calls))

    def test_forged_scope_parent_stops_entire_batch_before_any_write(self):
        self.drive.info_overrides["outside"] = item("outside", "范围外", "scope", directory=True)
        path = self.csv([["url", "to"], [SHARE_URL, "/范围/新目录"], [SECOND_URL, "/范围外"]])
        self.run_cli([path, "--scope", "scope"], code=2)
        self.assertEqual(self.drive.writes, [])

    def test_preflight_source_failure_is_reported_and_independent_row_executes(self):
        self.drive.failed_sources.add("offline-share")
        result = self.run_cli([self.two_targets()], code=4)
        self.assertEqual([row["result"] for row in result["rows"]], ["failed", "saved"])
        self.assertEqual(result["rows"][0]["phase"], "preflight")
        self.assertEqual(result["rows"][0]["error_type"], "remote_error")
        self.assertEqual(len(self.drive.saves), 1)

    def test_unknown_submit_failure_does_not_retry_and_independent_target_still_executes(self):
        self.drive.failed_submits.add("offline-share")
        result = self.run_cli([self.two_targets()], code=4)
        self.assertEqual([row["result"] for row in result["rows"]], ["failed", "saved"])
        self.assertNotIn("task_id", result["rows"][0])
        self.assertEqual(len(self.drive.saves), 2)
        self.assertEqual(sum(body["pwd_id"] == "offline-share" for body in self.drive.saves), 1)

    def test_task_failure_has_structured_task_id_and_no_resubmission(self):
        self.drive.failed_tasks.add("offline-share")
        result = self.run_cli([self.two_targets()], code=4)
        self.assertEqual(result["rows"][0]["task_id"], "selection-task")
        self.assertEqual(result["rows"][1]["result"], "saved")
        self.assertEqual(len(self.drive.saves), 2)

    def test_unconfirmed_destination_blocks_next_row_but_other_destination_executes(self):
        self.drive.failed_submits.add("offline-share")
        path = self.csv([["url", "to", "source"], [SHARE_URL, "/范围/目标", SOURCE_PATH],
                         [SECOND_URL, "/范围/目标", "/"], [THIRD_URL, "/范围/中文 空格", SOURCE_PATH]])
        result = self.run_cli([path], code=4)
        self.assertEqual([row["result"] for row in result["rows"]], ["failed", "blocked", "saved"])
        self.assertEqual(result["rows"][1]["reason"], "previous_destination_failure")
        self.assertEqual([body["pwd_id"] for body in self.drive.saves], ["offline-share", "offline-third"])

    def test_unconfirmed_mkdir_blocks_same_missing_destination_without_save_retry(self):
        self.drive.failed_mkdir_names.add("新目录")
        path = self.csv([["url", "to", "source"], [SHARE_URL, "/范围/新目录", SOURCE_PATH], [SECOND_URL, "/范围/新目录", "/"]])
        result = self.run_cli([path], code=4)
        self.assertEqual([row["result"] for row in result["rows"]], ["failed", "blocked"])
        self.assertEqual(len(self.drive.writes), 1)
        self.assertEqual(self.drive.saves, [])

    def test_known_same_destination_name_conflict_is_detected_before_execution(self):
        path = self.txt(SHARE_URL + "\n" + SECOND_URL + "\n")
        result = self.run_cli([path, "--to", "/范围/目标"], code=2)
        self.assertEqual([row["result"] for row in result["rows"]], ["saved", "failed"])
        self.assertEqual(result["rows"][1]["phase"], "preflight")
        self.assertEqual(len(self.drive.saves), 1)

    def test_existing_target_conflict_reports_only_failed_row(self):
        self.drive.items.append(item("old-source", "公开源", "target", directory=True))
        path = self.csv([["url", "to"], [SHARE_URL, "/范围/目标"], [SECOND_URL, "/范围/中文 空格"]])
        result = self.run_cli([path], code=2)
        self.assertEqual([row["result"] for row in result["rows"]], ["failed", "saved"])
        self.assertEqual(len(self.drive.saves), 1)

    def test_global_source_include_and_exclude_reuse_single_save_rules(self):
        result = self.run_cli([self.two_targets(), "--include", "*.txt", "--include", "*.log", "--exclude", "skip*", "--dry-run"])
        self.assertEqual([row["item_count"] for row in result["rows"]], [1, 1])
        self.assertEqual(self.drive.writes, [])

    def test_txt_uses_cli_source_and_csv_source_overrides_it(self):
        result = self.run_cli([self.txt(SHARE_URL), "--to", "/范围/目标", "--source", SOURCE_PATH, "--include", "*.txt", "--dry-run"])
        self.assertEqual(result["rows"][0]["source_path"], SOURCE_PATH)
        self.assertEqual(result["rows"][0]["item_count"], 1)
        path = self.csv([["url", "to", "source"], [SHARE_URL, "/范围/目标", "/"], [SECOND_URL, "/范围/中文 空格", ""]])
        result = self.run_cli([path, "--source", SOURCE_PATH, "--include", "*.txt", "--dry-run"])
        self.assertEqual([row["result"] for row in result["rows"]], ["skipped", "preview"])
        self.assertEqual(self.drive.writes, [])

    def test_global_invalid_glob_rejected_before_remote_reads(self):
        self.run_cli([self.txt(SHARE_URL), "--to", "/范围/目标", "--include", "dir/*.txt"], code=2)
        self.assertEqual(self.drive.calls, [])

    def test_dry_run_zero_write_report_has_safe_fields_and_original_line_numbers(self):
        path = self.txt("# comment\n" + SHARE_URL + "?pwd=private-passcode\n")
        report = self.root / "报告.json"
        result = self.run_cli([path, "--to", "/范围/新目录", "--report", report, "--dry-run", "--scope", "scope"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["rows"][0]["line"], 2)
        self.assertTrue(result["rows"][0]["create_directory"])
        self.assertEqual(json.loads(report.read_text(encoding="utf-8")), result)
        self.assertEqual(self.drive.writes, [])
        serialized = report.read_text(encoding="utf-8")
        for secret in ("private-passcode", "stoken", "private-token", "?pwd=", "download_url"):
            self.assertNotIn(secret, serialized)

    def test_atomic_report_replaces_only_after_flush_and_sync(self):
        report = self.root / "报告.json"
        report.write_text("old-report", encoding="utf-8")
        original_replace = os.replace
        original_fsync = os.fsync
        events = []

        def sync(fd):
            events.append("fsync")
            return original_fsync(fd)

        def replace(source, target):
            events.append("replace")
            self.assertEqual(report.read_text(encoding="utf-8"), "old-report")
            self.assertEqual(Path(source).parent, report.parent)
            return original_replace(source, target)

        with patch("quark_cli.batch.os.fsync", side_effect=sync), patch("quark_cli.batch.os.replace", side_effect=replace):
            result = self.run_cli([self.txt(SHARE_URL), "--to", "/范围/目标", "--report", report])
        self.assertEqual(events, ["fsync", "replace"])
        self.assertEqual(json.loads(report.read_text(encoding="utf-8")), result)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_report_prepare_failure_makes_zero_remote_writes(self):
        report = self.root / "missing-parent" / "报告.json"
        self.run_cli([self.txt(SHARE_URL), "--to", "/范围/目标", "--report", report], code=5)
        self.assertEqual(self.drive.calls, [])

    def test_report_fdopen_failure_closes_descriptor_and_removes_temporary_before_any_write(self):
        original_mkstemp = tempfile.mkstemp
        captured = []

        def create(*args, **options):
            result = original_mkstemp(*args, **options)
            captured.append(result)
            return result

        path = self.txt(SHARE_URL)
        with patch("quark_cli.batch.tempfile.mkstemp", side_effect=create), patch("quark_cli.batch.os.fdopen", side_effect=OSError("private-passcode")):
            self.run_cli([path, "--to", "/范围/目标", "--report", self.root / "报告.json"], code=5)
        self.assertEqual(len(captured), 1)
        descriptor, temporary = captured[0]
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        self.assertFalse(Path(temporary).exists())
        self.assertEqual(self.drive.calls, [])

    def test_report_cannot_overwrite_input(self):
        path = self.txt(SHARE_URL)
        self.run_cli([path, "--to", "/范围/目标", "--report", path], code=2)
        self.assertEqual(path.read_text(encoding="utf-8"), SHARE_URL)
        self.assertEqual(self.drive.calls, [])

    def test_report_replace_failure_preserves_prior_report_and_successful_result_without_resubmit(self):
        report = self.root / "报告.json"
        report.write_text("old-report", encoding="utf-8")
        with patch("quark_cli.batch.os.replace", side_effect=OSError("private-passcode")):
            result = self.run_cli([self.txt(SHARE_URL), "--to", "/范围/目标", "--report", report], code=5)
        self.assertFalse(result["report"]["written"])
        self.assertEqual(result["rows"][0]["result"], "saved")
        self.assertEqual(report.read_text(encoding="utf-8"), "old-report")
        self.assertEqual(len(self.drive.saves), 1)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_report_temporary_is_cleaned_after_late_scope_rejection(self):
        path = self.csv([["url", "to"], [SHARE_URL, "/范围/新目录"], [SECOND_URL, "/范围外/新目录"]])
        report = self.root / "报告.json"
        self.run_cli([path, "--scope", "scope", "--report", report], code=2)
        self.assertFalse(report.exists())
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        self.assertEqual(self.drive.writes, [])

    def test_empty_input_and_empty_selection_are_explicit_noop_results(self):
        empty = self.run_cli([self.txt("# comment\n"), "--to", "/范围/目标"])
        self.assertEqual((empty["total"], empty["succeeded"], empty["skipped"], empty["failed"]), (0, 0, 0, 0))
        result = self.run_cli([self.txt(SHARE_URL), "--to", "/范围/新目录", "--include", "absent*"])
        self.assertEqual(result["rows"][0]["result"], "skipped")
        self.assertEqual(self.drive.writes, [])

    def test_empty_selection_still_checks_batch_target_scope(self):
        self.run_cli([self.txt(SHARE_URL), "--to", "/范围外/新目录", "--include", "absent*", "--scope", "scope"], code=2)
        self.assertEqual(self.drive.writes, [])

    def test_service_accepts_scope_before_serial_execution(self):
        with QuarkClient("sid=offline", transport=self.drive.transport()) as client:
            saver = ShareSaver(client)
            result = BatchSaver(saver).run(self.two_targets(), scope=Scope(saver.paths, "scope"), dry_run=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output["succeeded"], 2)
        self.assertEqual(self.drive.writes, [])


if __name__ == "__main__":
    unittest.main()
