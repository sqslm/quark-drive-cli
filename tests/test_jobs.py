import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quark_cli.cli import main
from quark_cli.client import QuarkClient
from quark_cli.errors import LocalIOError, RateLimitError, RemoteError, UsageError
from quark_cli.jobs import Jobs
from quark_cli.paths import Scope
from quark_cli.shares import ShareSaver
from quark_cli.state import read_json, write_json

from drive_fixture import item
from share_fixture import SHARE_URL, SOURCE_PATH, ShareFixture, shared


class ProcessStopped(BaseException):
    """A process exit, rather than a handled CLI error."""


class JobsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "private"
        self.fixture = ShareFixture()
        self.client = QuarkClient("private-cookie", transport=self.fixture.transport())
        self.addCleanup(self.client.close)
        self.saver = ShareSaver(self.client)
        self.jobs = Jobs(self.saver, state_dir=self.directory)
        self.scope = Scope(self.saver.paths, "scope")
        for target in ("quark_cli.jobs.time.sleep", "quark_cli.shares.time.sleep"):
            sleeper = patch(target)
            sleeper.start()
            self.addCleanup(sleeper.stop)

    def add(self, name="中文任务", target="/范围/目标", *, include=("*.txt",), source=SOURCE_PATH, **options):
        return self.jobs.add(name, SHARE_URL, target, source=source, include=include, scope=self.scope, **options)

    def entry(self, name="中文任务"):
        config = read_json(self.jobs.config_path)
        ledger = read_json(self.jobs.ledger_path)
        return ledger["jobs"][config["jobs"][name]["id"]]

    def run_ok(self, name="中文任务", **options):
        report, code = self.jobs.run(name, **options)
        self.assertEqual(code, 0, report)
        return report["jobs"][0]

    def run_failed(self, name="中文任务", **options):
        report, code = self.jobs.run(name, **options)
        self.assertNotEqual(code, 0, report)
        self.assertEqual(report["jobs"][0]["result"], "failed")
        return report["jobs"][0]

    def source(self, fid):
        return next(record for records in self.fixture.share_directories.values() for record in records if record["fid"] == fid)

    def copied(self, source_fid):
        fid = self.entry()["mappings"][source_fid]["target"]["fid"]
        return next(record for record in self.fixture.items if record["fid"] == fid)

    def crash_after_submit(self):
        with patch.object(self.saver, "_poll_task", side_effect=ProcessStopped), self.assertRaises(ProcessStopped):
            self.jobs.run("中文任务")
        self.assertIsNotNone(self.entry()["pending"]["task_id"])

    def test_unchanged_twenty_episodes_reuse_parents_but_check_every_exact_fid(self):
        self.fixture.share_directories["source-child"] = [
            shared(f"ep-{index}", f"{index}.mkv", "source-child") for index in range(20)]
        self.add(include=("*.mkv",))
        first = self.run_ok()
        self.assertEqual(len(first["saved_entries"]), 20)
        self.assertEqual({record["source_fid"] for record in first["saved_entries"]},
                         {f"ep-{index}" for index in range(20)})
        self.fixture.calls.clear()
        writes = len(self.fixture.writes)
        with patch.object(self.saver.files, "info", wraps=self.saver.files.info) as info:
            result = self.run_ok()
        self.assertEqual(result["result"], "unchanged")
        self.assertEqual(result["saved_entries"], [])
        self.assertEqual(len(self.fixture.writes), writes)
        self.assertLessEqual(len(self.fixture.calls), 50)
        checked = {call.args[0] for call in info.call_args_list}
        self.assertTrue({entry["target_fid"] for entry in first["saved_entries"]}.issubset(checked))
        # No metadata survives the invocation: a moved target must still stop.
        self.copied("ep-19")["pdir_fid"] = "child"
        self.run_failed()
        self.assertEqual(len(self.fixture.writes), writes)

    def test_cooldown_and_unknown_write_have_actionable_safe_output(self):
        self.add()
        with patch.object(self.saver, "select", side_effect=RateLimitError("冷却中", retry_after=42)):
            result = self.run_failed()
        self.assertEqual((result["next_action"], result["retry_after_seconds"]), ("wait", 42))
        with patch.object(self.saver, "_submit_selection", side_effect=RemoteError("受理未知")):
            result = self.run_failed()
        self.assertTrue(result["pending"])
        self.assertEqual(result["next_action"], "manual_review")
        self.assertEqual(result["message"], "受理未知")
        with patch.object(self.saver, "_submit_selection") as submit:
            self.assertEqual(self.run_failed()["next_action"], "manual_review")
        submit.assert_not_called()

    def test_known_task_failure_reports_original_task_recovery(self):
        self.add()
        with patch.object(self.saver, "_poll_task", side_effect=RateLimitError("冷却中", retry_after=9)):
            result = self.run_failed()
        self.assertEqual(result["next_action"], "query_original_task")
        self.assertEqual(result["task_id"], "selection-task")
        self.assertEqual(result["retry_after_seconds"], 9)
        recovered = self.run_ok()
        self.assertEqual(len(recovered["saved_entries"]), 1)
        self.assertEqual(len(self.fixture.saves), 1)

    def test_partial_job_reports_confirmed_entries_before_later_group_failure(self):
        self.add(include=("*",))
        self.run_ok()
        self.fixture.share_directories["source-child"].append(shared("fresh-root", "fresh.txt", "source-child"))
        self.fixture.share_directories["folder"].append(shared("fresh-child", "fresh-child.txt", "folder"))
        original = self.saver._submit_selection
        def submit(selection, *args, **kwargs):
            if selection.source_fid == "folder":
                raise RemoteError("第二组受理未知")
            return original(selection, *args, **kwargs)
        with patch.object(self.saver, "_submit_selection", side_effect=submit):
            result = self.run_failed()
        self.assertEqual(result["saved_items"], 1)
        self.assertEqual(result["saved_entries"][0]["source_fid"], "fresh-root")
        self.assertTrue(result["pending"])
        self.assertEqual(result["next_action"], "manual_review")

    def test_failed_preview_never_reports_recovered_entries_as_saved(self):
        self.add()
        self.crash_after_submit()
        self.fixture.share_directories["source-child"].append(shared("fresh", "fresh.txt", "source-child"))
        self.fixture.items.append(item("conflicting", "fresh.txt", "target", size=4, updated_at=123))
        snapshot = self.jobs.ledger_path.read_bytes()
        result = self.run_failed(dry_run=True)
        self.assertEqual((result["saved_items"], result["saved_entries"]), (0, []))
        self.assertEqual(self.jobs.ledger_path.read_bytes(), snapshot)
        self.assertEqual(len(self.fixture.saves), 1)

    def test_normalized_private_configuration_and_safe_public_whitelist(self):
        created = self.jobs.add("任务", SHARE_URL + "?pwd=private-passcode", "/范围/目标", source=SOURCE_PATH,
                                include=["*.txt"], exclude=["skip*"], scope=self.scope)
        config = read_json(self.jobs.config_path)["jobs"]["任务"]
        self.assertEqual(config["url"], SHARE_URL)
        self.assertEqual(config["passcode"], "private-passcode")
        self.assertEqual(config["scope_fid"], "scope")
        for output in (created, self.jobs.list(), self.jobs.run("任务", dry_run=True)[0]):
            rendered = json.dumps(output)
            for secret in ("private-passcode", "private-cookie", "stoken-private", "private-token", "signature", "https://"):
                self.assertNotIn(secret, rendered)
        self.assertEqual(set(created), {"created", "name", "job_id", "share_id", "destination_path", "source_path", "scope_fid", "include", "exclude"})

    def test_first_run_exact_mapping_then_second_run_zero_writes(self):
        self.add()
        first = self.run_ok()
        self.assertEqual((first["result"], first["saved_items"], first["new_items"]), ("updated", 1, 1))
        mapping = self.entry()["mappings"]["note"]
        self.assertEqual((mapping["source_parent"], mapping["target_parent"], mapping["target"]["fid"]), ("source-child", "target", "saved-note"))
        count = len(self.fixture.writes)
        second = self.run_ok()
        self.assertEqual((second["result"], second["new_items"], second["saved_items"]), ("unchanged", 0, 0))
        self.assertEqual(len(self.fixture.writes), count)

    def test_new_file_is_incremental_and_uses_parent_tokens(self):
        self.add()
        self.run_ok()
        self.fixture.share_directories["source-child"].append(shared("next", "next.txt", "source-child"))
        report = self.run_ok()
        self.assertEqual(report["saved_items"], 1)
        self.assertEqual(self.fixture.saves[-1]["fid_list"], ["next"])
        self.assertEqual(self.fixture.saves[-1]["fid_token_list"], ["private-token-next"])
        self.assertEqual(self.fixture.saves[-1]["pdir_fid"], "source-child")

    def test_selected_directory_whole_subtree_and_recursive_incremental(self):
        self.add(include=("资料",))
        self.run_ok()
        self.assertEqual(set(self.entry()["mappings"]), {"folder", "deep"})
        self.source("folder")["updated_at"] = 999
        self.source("folder")["size"] = 999
        self.copied("folder")["updated_at"] = 1000
        self.copied("folder")["size"] = 888
        self.assertEqual(self.run_ok()["result"], "unchanged")
        self.fixture.share_directories["folder"].append(shared("new-child", "new.log", "folder"))
        self.assertEqual(self.run_ok()["saved_items"], 1)
        self.assertEqual(self.fixture.saves[-1]["pdir_fid"], "folder")
        self.assertEqual(self.fixture.saves[-1]["to_pdir_fid"], "saved-folder")
        self.assertEqual(self.fixture.saves[-1]["fid_list"], ["new-child"])

    def test_new_nested_directory_maps_all_descendants_and_ignores_top_filters(self):
        self.add(include=("资料",))
        self.run_ok()
        self.fixture.share_directories["folder"].append(shared("nested", "子目录", "folder", directory=True))
        self.fixture.share_directories["nested"] = [shared("nested-file", "other.bin", "nested")]
        self.assertEqual(self.run_ok()["saved_items"], 2)
        self.assertEqual(self.entry()["mappings"]["nested-file"]["target_parent"], "saved-2-nested")
        self.assertEqual(self.run_ok()["result"], "unchanged")

    def test_file_same_fid_and_size_but_version_change_conflicts_without_write(self):
        self.add()
        self.run_ok()
        self.source("note")["updated_at"] += 1
        count = len(self.fixture.writes)
        self.run_failed()
        self.assertEqual(len(self.fixture.writes), count)

    def test_missing_empty_nonfinite_or_invalid_version_refuses_first_write(self):
        self.add()
        for version in (None, "", " ", float("nan"), float("inf"), "NaN", "Infinity", True, 0, -1, "yesterday"):
            with self.subTest(version=repr(version)):
                self.source("note")["updated_at"] = version
                self.run_failed()
                self.assertEqual(self.fixture.writes, [])
                self.assertEqual(self.entry()["mappings"], {})

    def test_historical_same_name_is_not_adopted(self):
        self.add()
        self.fixture.items.append(item("historical", "note.txt", "target", size=4, updated_at=123))
        self.run_failed()
        self.assertEqual(self.fixture.writes, [])
        self.assertEqual(self.entry()["mappings"], {})

    def test_target_file_identity_change_or_same_name_replacement_conflicts(self):
        self.add()
        self.run_ok()
        original = copy.deepcopy(self.copied("note"))
        for change in ({"size": 8}, {"updated_at": 456}, {"file_name": "renamed.txt"}, {"fid": "replacement"}, {"pdir_fid": "child"}):
            with self.subTest(change=change):
                target = next(record for record in self.fixture.items if record["file_name"] in ("note.txt", "renamed.txt"))
                target.clear()
                target.update({**original, **change})
                count = len(self.fixture.writes)
                self.run_failed()
                self.assertEqual(len(self.fixture.writes), count)

    def test_source_missing_or_same_name_new_fid_conflicts(self):
        self.add()
        self.run_ok()
        self.source("note")["fid"] = "different-source"
        count = len(self.fixture.writes)
        self.run_failed()
        self.assertEqual(len(self.fixture.writes), count)

    def test_bound_destination_directory_cannot_rebind_same_name_new_fid(self):
        self.add()
        next(record for record in self.fixture.items if record["fid"] == "target")["fid"] = "replacement-directory"
        self.run_failed()
        self.assertEqual(self.fixture.writes, [])

    def test_source_directory_fid_cannot_rebind_same_name(self):
        self.add()
        self.source("source-child")["fid"] = "replacement-source-directory"
        self.run_failed()
        self.assertEqual(self.fixture.writes, [])

    def test_saved_scope_cannot_be_widened_and_current_scope_intersects(self):
        self.add()
        target = next(record for record in self.fixture.items if record["fid"] == "target")
        target["pdir_fid"] = "outside"
        self.run_failed(scope=Scope(self.saver.paths, "0"))
        self.assertEqual(self.fixture.writes, [])
        target["pdir_fid"] = "scope"
        self.run_failed(scope=Scope(self.saver.paths, "child"))
        self.assertEqual(self.fixture.writes, [])
        self.assertEqual(self.run_ok(scope=Scope(self.saver.paths, "target"))["result"], "updated")

    def test_saved_scope_fid_does_not_rebind_same_name_scope(self):
        self.add()
        next(record for record in self.fixture.items if record["fid"] == "scope")["fid"] = "replacement-scope"
        next(record for record in self.fixture.items if record["fid"] == "target")["pdir_fid"] = "replacement-scope"
        self.run_failed(scope=Scope(self.saver.paths, "0"))
        self.assertEqual(self.fixture.writes, [])

    def test_forged_parent_info_cannot_authorize_job_write(self):
        self.add()
        actual = next(record for record in self.fixture.items if record["fid"] == "target")
        self.fixture.info_overrides["target"] = {**actual, "pdir_fid": "child"}
        self.run_failed()
        self.assertEqual(self.fixture.writes, [])

    def test_dry_run_preserves_configuration_and_ledger_bytes_and_mtimes(self):
        self.add(target="/范围/新任务目标", include=("资料",))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (self.jobs.config_path, self.jobs.ledger_path)}
        self.assertEqual(self.run_ok(dry_run=True)["new_items"], 2)
        self.assertEqual(self.fixture.writes, [])
        for path, snapshot in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), snapshot)

    def test_intents_durable_before_mkdir_and_save_taskid_before_poll(self):
        self.add(target="/范围/新任务目标")
        original = self.fixture.handler
        observed = []
        def handler(request):
            if request.url.path.endswith("/file") and request.method == "POST":
                pending = self.entry()["pending"]
                self.assertEqual((pending["kind"], pending["created_fid"]), ("mkdir", None))
                observed.append("mkdir")
            if request.url.path.endswith("/share/sharepage/save"):
                pending = self.entry()["pending"]
                self.assertEqual((pending["kind"], pending["task_id"]), ("save", None))
                self.assertEqual(pending["parents"], {"note": "source-child"})
                observed.append("save")
            if request.url.path.endswith("/task"):
                self.assertEqual(self.entry()["pending"]["task_id"], request.url.params["task_id"])
                observed.append("task")
            return original(request)
        self.client._api._transport = httpx.MockTransport(handler)
        self.run_ok()
        self.assertEqual(observed, ["mkdir", "save", "task"])
        self.assertIsNone(self.entry()["pending"])

    def test_unknown_save_without_taskid_is_never_resubmitted(self):
        self.add()
        def unknown(selection, target, **options):
            self.fixture.saves.append({"uncertain": True})
            raise RemoteError("unknown")
        with patch.object(self.saver, "_submit_selection", side_effect=unknown):
            self.run_failed()
        self.assertIsNone(self.entry()["pending"]["task_id"])
        self.run_failed()
        self.assertEqual(len(self.fixture.saves), 1)

    def test_unknown_mkdir_without_fid_cannot_adopt_later_directory(self):
        self.add(target="/范围/新任务目标")
        def unknown(*args):
            self.fixture.items.append(item("uncertain-directory", "新任务目标", "scope", directory=True))
            raise RemoteError("unknown")
        with patch.object(self.saver.files, "mkdir", side_effect=unknown):
            self.run_failed()
        self.run_failed()
        self.assertIsNone(self.entry()["binding"]["fid"])
        self.assertEqual(self.fixture.saves, [])

    def test_interruption_after_taskid_recovers_original_task_readonly(self):
        self.add()
        self.crash_after_submit()
        self.assertEqual(len(self.fixture.saves), 1)
        recovered = self.run_ok()
        self.assertEqual(recovered["saved_items"], 1)
        self.assertEqual(len(self.fixture.saves), 1)
        self.assertIsNone(self.entry()["pending"])

    def test_known_directory_task_recovers_before_confirmation_started(self):
        self.add(include=("资料",))
        self.crash_after_submit()
        self.assertFalse(self.entry()["pending"]["confirmation_started"])
        self.assertEqual(self.run_ok()["saved_items"], 2)
        self.assertEqual(len(self.fixture.saves), 1)

    def test_terminal_failed_task_stays_pending_and_does_not_resubmit(self):
        self.add()
        with patch.object(self.saver, "_poll_task", side_effect=RemoteError("failed task")):
            self.run_failed()
            self.run_failed()
        self.assertEqual(len(self.fixture.saves), 1)
        self.assertEqual(self.entry()["pending"]["task_id"], "selection-task")

    def test_moved_top_source_after_submit_rejected_before_original_task_poll(self):
        self.add()
        self.crash_after_submit()
        record = self.source("note")
        self.fixture.share_directories["source-child"].remove(record)
        record["pdir_fid"] = "folder"
        self.fixture.share_directories["folder"].append(record)
        with patch.object(self.saver, "_poll_task") as poll:
            self.run_failed()
        poll.assert_not_called()
        self.assertEqual(len(self.fixture.saves), 1)

    def test_swapped_same_name_descendants_after_submit_rejected_before_poll(self):
        self.fixture.share_directories["source-child"] = [shared("dir-a", "A", "source-child", directory=True), shared("dir-b", "B", "source-child", directory=True)]
        first, second = shared("file-a", "same.txt", "dir-a"), shared("file-b", "same.txt", "dir-b")
        self.fixture.share_directories.update({"dir-a": [first], "dir-b": [second]})
        self.add(include=("*",))
        self.crash_after_submit()
        self.assertEqual(self.entry()["pending"]["parents"]["file-a"], "dir-a")
        first["pdir_fid"], second["pdir_fid"] = "dir-b", "dir-a"
        self.fixture.share_directories.update({"dir-a": [second], "dir-b": [first]})
        with patch.object(self.saver, "_poll_task") as poll:
            self.run_failed()
        poll.assert_not_called()
        self.assertEqual(len(self.fixture.saves), 1)
        self.assertEqual(self.entry()["mappings"], {})

    def test_directory_mapping_gap_does_not_adopt_later_historical_children(self):
        self.add(include=("资料",))
        self.fixture.result_transform = lambda records: [record for record in records if record["fid"] != "saved-deep"]
        self.run_failed()
        self.assertTrue(self.entry()["pending"]["confirmation_started"])
        self.assertEqual(self.entry()["mappings"], {})
        self.fixture.items.append(item("unrelated-later", "inside.txt", "saved-folder", size=4, updated_at=123))
        with patch.object(self.saver, "_poll_task") as poll:
            self.run_failed()
        poll.assert_not_called()
        self.assertEqual(self.entry()["mappings"], {})
        self.assertEqual(len(self.fixture.saves), 1)

    def test_directory_mapping_interruption_persists_guard_before_reading_children(self):
        self.add(include=("资料",))
        original = self.jobs._map_new
        def interrupted(*args):
            self.assertTrue(self.entry()["pending"]["confirmation_started"])
            raise ProcessStopped()
        with patch.object(self.jobs, "_map_new", side_effect=interrupted), self.assertRaises(ProcessStopped):
            self.jobs.run("中文任务")
        with patch.object(self.jobs, "_map_new", wraps=original) as mapper:
            self.run_failed()
        mapper.assert_not_called()
        self.assertEqual(len(self.fixture.saves), 1)

    def test_task_unrelated_fids_or_old_fid_cannot_establish_mapping(self):
        self.add()
        self.fixture.task_fids = ["target"]
        self.run_failed()
        self.assertEqual(self.entry()["mappings"], {})
        self.assertIsNotNone(self.entry()["pending"])
        self.assertEqual(len(self.fixture.saves), 1)

    def test_state_failure_before_intent_prevents_all_remote_writes(self):
        self.add(target="/范围/新任务目标")
        with patch("quark_cli.jobs.write_json", side_effect=LocalIOError("atomic failure")):
            self.run_failed()
        self.assertEqual(self.fixture.writes, [])
        self.assertIsNone(self.entry()["pending"])

    def test_state_failure_after_taskid_stops_remaining_jobs_and_unknown_retry(self):
        self.add()
        self.add("第二任务", "/范围/中文 空格")
        original = write_json
        def fail_known(path, document):
            if any(entry["pending"] and entry["pending"]["task_id"] for entry in document["jobs"].values()):
                raise LocalIOError("atomic failure")
            original(path, document)
        with patch("quark_cli.jobs.write_json", side_effect=fail_known):
            report, code = self.jobs.run()
        self.assertEqual(code, 5)
        self.assertEqual(report["jobs"][1]["reason"], "previous_state_failure")
        self.assertEqual(len(self.fixture.saves), 1)
        self.assertIsNone(self.entry()["pending"]["task_id"])
        self.run_failed()
        self.assertEqual(len(self.fixture.saves), 1)

    def test_corrupt_or_missing_ledger_stops_without_rebuilding(self):
        self.add()
        self.jobs.ledger_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(LocalIOError):
            self.jobs.run("中文任务")
        self.jobs.ledger_path.unlink()
        with self.assertRaises(LocalIOError):
            self.jobs.run("中文任务")
        self.assertEqual(self.fixture.writes, [])

    def test_remove_keeps_unknown_ledger_and_blocks_new_job_same_destination(self):
        self.add()
        with patch.object(self.saver, "_submit_selection", side_effect=RemoteError("unknown")):
            self.run_failed()
        original_bytes = self.jobs.ledger_path.read_bytes()
        self.jobs.remove("中文任务")
        self.assertEqual(self.jobs.ledger_path.read_bytes(), original_bytes)
        self.assertEqual(self.jobs.list(), {"jobs": []})
        self.add()
        self.run_failed()
        self.assertEqual(self.fixture.writes, [])

    def test_all_jobs_partial_failures_continue_independent_targets_and_nonzero(self):
        self.add("冲突任务")
        self.add("成功任务", "/范围/中文 空格")
        self.fixture.items.append(item("existing", "note.txt", "target", size=4, updated_at=123))
        report, code = self.jobs.run()
        self.assertNotEqual(code, 0)
        self.assertEqual((report["succeeded"], report["failed"]), (1, 1))
        self.assertEqual([record["result"] for record in report["jobs"]], ["failed", "updated"])
        self.assertEqual(len(self.fixture.saves), 1)

    def test_job_list_and_remove_without_cookie_and_cli_safe_json(self):
        self.add()
        with patch.dict(os.environ, {"QUARK_CONFIG_DIR": str(self.directory)}, clear=False), patch("quark_cli.cli.get_cookie", side_effect=AssertionError("must not read credentials")):
            for arguments in (["job", "list", "--json"], ["job", "remove", "中文任务", "--json"]):
                with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(main(arguments), 0, error.getvalue())
                self.assertNotIn("private", output.getvalue())
        self.assertTrue(self.jobs.ledger_path.exists())

    def test_cli_add_run_help_and_invalid_scope_preserve_zero_writes(self):
        with patch.dict(os.environ, {"QUARK_CONFIG_DIR": str(self.directory), "QUARK_COOKIE": "private-cookie"}, clear=False):
            with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                code = main(["job", "add", "任务", SHARE_URL, "--to", "/范围/目标", "--source", SOURCE_PATH,
                             "--include", "*.txt", "--scope", "scope", "--json"], transport=self.fixture.transport())
            self.assertEqual(code, 0, error.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                code = main(["job", "run", "任务", "--dry-run", "--scope", "child", "--json"], transport=self.fixture.transport())
            self.assertNotEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["jobs"][0]["result"], "failed")
        self.assertEqual(self.fixture.writes, [])

    def test_empty_selection_does_not_create_missing_destination(self):
        self.add(target="/范围/新任务目标", include=("missing*",))
        self.assertEqual(self.run_ok()["new_items"], 0)
        self.assertEqual(self.fixture.writes, [])
        self.assertIsNone(self.entry()["binding"]["fid"])

    def test_created_directory_index_lag_is_confirmed_before_scoped_save(self):
        self.add(target="/范围/新任务目标")
        original = self.saver._list_children
        reads = []
        def delayed(parent):
            records = original(parent)
            if parent == "scope" and any(record["fid"] == "new-folder" for record in records):
                reads.append(parent)
                if len(reads) <= 2:
                    return [record for record in records if record["fid"] != "new-folder"]
            return records
        with patch.object(self.saver, "_list_children", side_effect=delayed):
            self.run_ok()
        self.assertGreaterEqual(len(reads), 3)
        self.assertEqual(self.entry()["binding"]["fid"], "new-folder")
        self.assertEqual(self.fixture.saves[0]["to_pdir_fid"], "new-folder")

    def test_known_created_fid_survives_process_exit_without_new_mkdir(self):
        self.add(target="/范围/新任务目标")
        with patch.object(self.saver, "_confirm_created_directory", side_effect=ProcessStopped), self.assertRaises(ProcessStopped):
            self.jobs.run("中文任务")
        self.assertEqual(self.entry()["pending"]["created_fid"], "new-folder")
        count = sum(request.url.path.endswith("/file") for request in self.fixture.writes)
        self.run_ok()
        self.assertEqual(sum(request.url.path.endswith("/file") for request in self.fixture.writes), count)
        self.assertEqual(len(self.fixture.saves), 1)

    def test_short_lived_copied_subtree_index_lag_completes_in_same_attempt(self):
        self.add(include=("资料",))
        original = self.saver._list_children
        reads = []
        def delayed(parent):
            if parent == "saved-folder":
                reads.append(parent)
                if len(reads) <= 2:
                    return []
            return original(parent)
        with patch.object(self.saver, "_list_children", side_effect=delayed):
            self.assertEqual(self.run_ok()["saved_items"], 2)
        self.assertGreaterEqual(len(reads), 3)
        self.assertIsNone(self.entry()["pending"])
        self.assertEqual(len(self.fixture.saves), 1)

    def test_existing_recursive_destination_conflict_prevents_earlier_fresh_root_write(self):
        self.add(include=("*",))
        self.run_ok()
        self.fixture.share_directories["source-child"].append(shared("fresh-root", "fresh.txt", "source-child"))
        self.fixture.share_directories["folder"].append(shared("fresh-child", "fresh-child.txt", "folder"))
        self.fixture.items.append(item("historical-child", "fresh-child.txt", "saved-folder", size=4, updated_at=123))
        count = len(self.fixture.writes)
        self.run_failed()
        self.assertEqual(len(self.fixture.writes), count)

    def test_duplicate_fid_or_source_cycle_fails_before_writes(self):
        self.add(include=("资料",))
        original = copy.deepcopy(self.fixture.share_directories["folder"])
        for descendant in (shared("folder", "again", "folder", directory=True), shared("source-child", "cycle", "folder", directory=True)):
            with self.subTest(fid=descendant["fid"]):
                self.fixture.share_directories["folder"] = original + [descendant]
                self.run_failed()
                self.assertEqual(self.fixture.writes, [])

    def test_directory_result_extra_or_wrong_size_never_commits_partial_mapping(self):
        self.add(include=("资料",))
        def incorrect(records):
            for record in records:
                if record["fid"] == "saved-deep":
                    record["size"] = 999
            return records
        self.fixture.result_transform = incorrect
        self.run_failed()
        self.assertEqual(self.entry()["mappings"], {})
        self.assertTrue(self.entry()["pending"]["confirmation_started"])

    def test_corrupt_pending_parent_map_is_rejected_without_remote_requests(self):
        self.add()
        self.crash_after_submit()
        ledger = read_json(self.jobs.ledger_path)
        next(iter(ledger["jobs"].values()))["pending"]["parents"] = {}
        write_json(self.jobs.ledger_path, ledger)
        count = len(self.fixture.calls)
        with self.assertRaises(LocalIOError):
            self.jobs.run("中文任务")
        self.assertEqual(len(self.fixture.calls), count)

    def test_target_info_parent_must_equal_mapping_parent_even_when_list_omits_parent(self):
        self.add()
        self.run_ok()
        target = self.copied("note")
        stale = {key: value for key, value in target.items() if key != "pdir_fid"}
        self.fixture.list_overrides["target"] = [stale]
        target["pdir_fid"] = "child"
        count = len(self.fixture.writes)
        self.run_failed()
        self.assertEqual(len(self.fixture.writes), count)

    def test_new_mapping_requires_exact_info_parent_beyond_scope_membership(self):
        self.add()
        def wrong_parent(records):
            record = records[0]
            self.fixture.list_overrides["target"] = [{key: value for key, value in record.items() if key != "pdir_fid"}]
            record["pdir_fid"] = "child"
            return records
        self.fixture.result_transform = wrong_parent
        self.run_failed()
        self.assertEqual(self.entry()["mappings"], {})


if __name__ == "__main__":
    unittest.main()
