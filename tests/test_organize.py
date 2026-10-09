import copy
import json
import math
import unittest
from unittest.mock import patch

import httpx

from drive_fixture import DriveFixture, item
from quark_cli.client import QuarkClient
from quark_cli.errors import QuarkError, RemoteError, UsageError
from quark_cli.organize import Organize, safe_operation_result
from quark_cli.paths import Scope


class OrganizeFixture(DriveFixture):
    """Mutable exact-FID rename server with controlled acknowledgement loss."""

    def __init__(self, records=None):
        super().__init__(records if records is not None else [
            item("scope", "范围", directory=True),
            item("folder", "整理", "scope", directory=True),
            item("one", "旧 一.txt", "folder", size=3, updated_at=100),
            item("two", "旧 二.txt", "folder", size=4, updated_at=100),
            item("empty", "空目录", "folder", directory=True),
            item("outside", "范围外", directory=True),
        ])
        self.loss = None
        self.after_rename = None
        self.rename_requests = []
        self.sort_hook = None
        self.info_hook = None
        self.lag = 0
        self.lag_record = None

    def handler(self, request):
        if request.url.path.endswith("/file/info"):
            self.calls.append(request)
            fid = request.url.params["fid"]
            if self.info_hook:
                self.info_hook(fid)
            data = self.info_overrides.get(fid, next((record for record in self.items if record["fid"] == fid), {}))
            return httpx.Response(200, content=json.dumps({"status": 200, "code": 0, "data": data}).encode("utf-8"), request=request)
        if request.url.path.endswith("/file/sort"):
            if self.sort_hook:
                self.sort_hook(request)
            if self.lag and request.url.params["pdir_fid"] == "folder":
                self.lag -= 1
                children = [copy.deepcopy(record) for record in self.items if record.get("pdir_fid") == "folder"]
                children = [copy.deepcopy(self.lag_record) if record["fid"] == self.lag_record["fid"] else record for record in children]
                self.calls.append(request)
                return httpx.Response(200, json={"status": 200, "code": 0, "data": {"list": children, "_total": len(children)}}, request=request)
            self.calls.append(request)
            parent = request.url.params["pdir_fid"]
            children = self.list_overrides.get(parent, [record for record in self.items if record.get("pdir_fid") == parent])
            page, size = int(request.url.params["_page"]), int(request.url.params["_size"])
            data = {"list": children[(page - 1) * size:page * size], "_total": len(children)}
            # Raw bodies deliberately allow nonfinite numeric malformed metadata.
            return httpx.Response(200, content=json.dumps({"status": 200, "code": 0, "data": data}).encode("utf-8"), request=request)
        if request.url.path.endswith("/file/rename"):
            self.calls.append(request)
            body = json.loads(request.read())
            self.rename_requests.append(body)
            if self.loss == "before":
                raise httpx.ReadError("private-canary-secret", request=request)
            record = next(record for record in self.items if record["fid"] == body["fid"])
            self.lag_record = copy.deepcopy(record)
            record["file_name"] = body["file_name"]
            if record["file_type"] == 1:
                record["updated_at"] += 1
            if self.after_rename:
                self.after_rename(record)
            if self.loss == "after":
                raise httpx.ReadError("private-canary-secret", request=request)
            if self.loss == "malformed":
                return httpx.Response(200, content=b"private-canary-secret", request=request)
            return httpx.Response(200, json={"status": 200, "code": 0, "data": {"finish": True}}, request=request)
        return super().handler(request)


class OrganizeTests(unittest.TestCase):
    def service(self, drive=None):
        drive = drive or OrganizeFixture()
        client = QuarkClient("sid=mock", transport=drive.transport())
        self.addCleanup(client.close)
        return drive, Organize(client)

    def test_du_directory_includes_root_and_empty_directories(self):
        drive, organize = self.service()
        result = organize.du("folder", scope=Scope(organize.paths, "scope"))
        self.assertEqual((result["files"], result["directories"], result["known_bytes"], result["unknown_size_files"], result["complete"]), (2, 2, 7, 0, True))
        self.assertEqual(result["path"], "/范围/整理")
        self.assertEqual(drive.writes, [])

    def test_du_single_file_and_synthetic_account_root(self):
        drive, organize = self.service()
        self.assertEqual(organize.du("one")["directories"], 0)
        self.assertEqual(organize.du("/")["directories"], 5)
        self.assertFalse(any(request.url.path.endswith("/file/info") and request.url.params["fid"] == "0" for request in drive.calls))

    def test_du_unknown_sizes_are_counted_not_zero_and_versions_irrelevant(self):
        for value in (None, -1, True, "3", 1.5, math.nan):
            with self.subTest(value=value):
                drive, organize = self.service()
                drive.items[2]["size"] = value
                drive.items[2]["updated_at"] = math.nan
                result = organize.du("folder")
                self.assertEqual((result["known_bytes"], result["unknown_size_files"], result["complete"]), (4, 1, False))
                self.assertEqual(organize.du("one")["unknown_size_files"], 1)

    def test_du_missing_size_on_either_read_is_unknown(self):
        drive, organize = self.service()
        drive.info_overrides["one"] = {key: value for key, value in drive.items[2].items() if key != "size"}
        self.assertEqual(organize.du("folder")["unknown_size_files"], 1)

    def test_complete_pagination_counts_every_file(self):
        records = [item("folder", "many", directory=True)] + [item(f"f{i}", f"{i}.txt", "folder", size=i, updated_at=100) for i in range(105)]
        drive, organize = self.service(OrganizeFixture(records))
        result = organize.du("folder")
        self.assertEqual((result["files"], result["known_bytes"]), (105, sum(range(105))))
        self.assertTrue(any(request.url.path.endswith("/file/sort") and request.url.params["pdir_fid"] == "folder" and int(request.url.params["_page"]) > 1 for request in drive.calls))
        self.assertTrue(all(request.url.params.get("_fetch_total") == "1" for request in drive.calls if request.url.path.endswith("/file/sort")))

    def test_forged_list_parent_and_info_parent_fail_without_outside_traversal(self):
        for kind in ("list", "info"):
            with self.subTest(kind=kind):
                drive, organize = self.service()
                scope = Scope(organize.paths, "scope")
                if kind == "list":
                    drive.list_overrides["folder"] = [item("one", "旧 一.txt", "outside", size=3, updated_at=100)]
                else:
                    drive.info_overrides["one"] = {**drive.items[2], "pdir_fid": "outside"}
                drive.calls.clear()
                with self.assertRaises(QuarkError):
                    organize.du("folder", scope=scope)
                self.assertFalse(any(request.url.params.get("pdir_fid") == "outside" or request.url.params.get("fid") == "outside" for request in drive.calls))
                self.assertEqual(drive.writes, [])

    def test_repeated_fid_and_cycle_fail(self):
        drive, organize = self.service()
        drive.list_overrides["empty"] = [item("one", "旧 一.txt", "empty", size=3, updated_at=100)]
        with self.assertRaises(QuarkError):
            organize.du("folder")
        drive.list_overrides["empty"] = [item("folder", "整理", "empty", directory=True)]
        with self.assertRaises(QuarkError):
            organize.duplicates("folder")

    def test_changed_type_and_size_evidence_fail(self):
        for fields in ({"file_type": 0}, {"size": 99}):
            drive, organize = self.service()
            drive.info_overrides["one"] = {**drive.items[2], **fields}
            with self.assertRaises(QuarkError):
                organize.du("folder")

    def test_traversal_limits_stop_before_unbounded_child_inspection(self):
        drive, organize = self.service()
        with patch.object(organize, "_root", return_value={"fid": "0", "file_name": "/", "file_type": 0, "path": "/"}), \
             patch.object(organize.paths, "children", return_value=[{}] * 100000), self.assertRaises(RemoteError):
            organize.du("/")
        self.assertEqual(drive.calls, [])
        records = [item("folder", "deep", directory=True)]
        parent = "folder"
        for depth in range(1025):
            fid = f"dir{depth}"
            records.append(item(fid, f"level{depth}", parent, directory=True))
            parent = fid
        drive, organize = self.service(OrganizeFixture(records))
        with self.assertRaises(RemoteError):
            organize.du("folder")
        self.assertFalse(any(request.url.params.get("pdir_fid") == "dir1024" for request in drive.calls))

    def test_scope_outside_input_zero_content_reads(self):
        drive, organize = self.service()
        scope = Scope(organize.paths, "scope")
        with self.assertRaises(UsageError):
            organize.duplicates("outside", scope=scope)
        self.assertFalse(any(request.url.params.get("pdir_fid") == "outside" for request in drive.calls))

    def test_queued_directory_moved_outside_is_rechecked_before_its_first_content_read(self):
        for command in ("du", "duplicates"):
            drive, organize = self.service()
            scope = Scope(organize.paths, "scope")
            def moved(fid):
                if fid == "one":
                    drive.items[4]["pdir_fid"] = "outside"
            drive.info_hook = moved
            drive.calls.clear()
            with self.assertRaises(QuarkError):
                getattr(organize, command)("folder", scope=scope)
            self.assertFalse(any(request.url.params.get("pdir_fid") in ("empty", "outside") or request.url.params.get("fid") == "outside" for request in drive.calls))
            self.assertEqual(drive.writes, [])

    def test_du_remote_failure_is_not_partial_success_or_raw_error(self):
        drive, organize = self.service()
        with patch.object(organize.paths, "children", side_effect=RemoteError("private-canary-secret")), self.assertRaises(RemoteError) as caught:
            organize.du("folder")
        self.assertNotIn("canary", str(caught.exception))

    def test_duplicates_valid_md5_and_sha1_confirm_without_duplicate_candidates(self):
        drive, organize = self.service()
        drive.items[3].update(size=3, md5="A" * 32, sha1="b" * 40)
        drive.items[2].update(md5="a" * 32, sha1="b" * 40, _extra={"secret": "private-canary-secret"})
        result = organize.duplicates("folder")
        self.assertEqual(len(result["confirmed"]), 1)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["hash_unavailable_files"], 0)
        self.assertNotIn("canary", json.dumps(result))
        self.assertEqual(drive.writes, [])

    def test_duplicates_conflicting_second_hash_cannot_bridge_missing_hash(self):
        drive, organize = self.service()
        drive.items[2].update(md5="a" * 32, sha1="b" * 40)
        drive.items[3].update(size=3, md5="a" * 32, sha1="c" * 40)
        drive.items.append(item("three", "三.txt", "folder", size=3, md5="a" * 32, updated_at=100))
        result = organize.duplicates("folder")
        self.assertEqual(result["confirmed"], [])
        self.assertEqual(len(result["candidates"][0]["items"]), 3)

    def test_duplicates_matching_sha1_but_conflicting_md5_not_confirmed(self):
        drive, organize = self.service()
        drive.items[2].update(md5="a" * 32, sha1="b" * 40)
        drive.items[3].update(size=3, md5="c" * 32, sha1="b" * 40)
        self.assertEqual(organize.duplicates("folder")["confirmed"], [])

    def test_duplicates_invalid_size_hash_extra_and_multipart_etag_are_candidates_only(self):
        drive, organize = self.service()
        drive.items[2].update(size=True, md5="invalid", _extra={"md5": "a" * 32}, etag="a" * 32 + "-2")
        drive.items[3].update(size=True, md5="invalid", _extra={"md5": "a" * 32}, etag="a" * 32 + "-2", file_name="旧 一.txt")
        result = organize.duplicates("folder")
        self.assertEqual(result["confirmed"], [])
        self.assertEqual(result["hash_unavailable_files"], 2)
        self.assertEqual(result["candidates"][0]["reason"], "name")
        self.assertNotIn("size", result["candidates"][0]["items"][0])

    def test_duplicates_list_and_info_hash_disagreement_not_confirmed(self):
        drive, organize = self.service()
        drive.items[2].update(md5="a" * 32)
        drive.items[3].update(size=3, md5="a" * 32)
        drive.info_overrides["one"] = {**drive.items[2], "md5": "b" * 32}
        self.assertEqual(organize.duplicates("folder")["confirmed"], [])

    def test_duplicates_same_name_across_directories_candidates(self):
        drive, organize = self.service()
        drive.items.append(item("three", "旧 一.txt", "empty", size=99, updated_at=100))
        result = organize.duplicates("folder")
        self.assertEqual(result["candidates"][0]["reason"], "name")

    def test_rename_default_and_apply_dry_run_zero_write(self):
        for options in ({}, {"apply": True, "dry_run": True}):
            drive, organize = self.service()
            result = organize.rename_batch("folder", "^旧", "新", scope=Scope(organize.paths, "scope"), **options)
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["unchanged"], 1)
            self.assertEqual(drive.writes, [])
            self.assertEqual(drive.items[2]["file_name"], "旧 一.txt")

    def test_rename_applies_files_and_directory_once_and_confirms_exact_fids(self):
        drive, organize = self.service()
        result = organize.rename_batch("folder", "^", "新 ", apply=True, scope=Scope(organize.paths, "scope"))
        self.assertEqual((result["result"], result["renamed"]), ("completed", 3))
        self.assertEqual(len({request["fid"] for request in drive.rename_requests}), 3)
        self.assertEqual(drive.items[2]["fid"], "one")

    def test_invalid_regex_and_backreference_fail_before_any_network(self):
        for pattern, replacement in (("[", "x"), ("a", r"\2"), ("", r"\g<unknown>"), ("a{99999999999999999999}", "x")):
            drive, organize = self.service()
            with self.assertRaises(UsageError):
                organize.rename_batch("folder", pattern, replacement, apply=True)
            self.assertEqual(drive.calls, [])

    def test_empty_directory_still_validates_replacement(self):
        drive, organize = self.service()
        with self.assertRaises(UsageError):
            organize.rename_batch("empty", "(x)", r"\2", apply=True)
        self.assertEqual(drive.calls, [])

    def test_invalid_portable_result_names_rejected_before_write(self):
        for name in ("NUL.txt", "COM¹.log", "a:b", "a.", "a ", "a/b", "", "a\x1b"):
            drive, organize = self.service()
            with self.assertRaises(UsageError):
                organize.rename_batch("folder", "^.*$", name, apply=True)
            self.assertEqual(drive.writes, [])

    def test_duplicate_results_case_and_nfc_conflicts_zero_write(self):
        for names in (("a.txt", "A.txt"), ("é.txt", "e\u0301.txt")):
            drive, organize = self.service()
            drive.items[2]["file_name"], drive.items[3]["file_name"] = names
            with self.assertRaises(UsageError):
                organize.rename_batch("folder", "^", "new", apply=True)
            self.assertEqual(drive.writes, [])
        drive, organize = self.service()
        with self.assertRaises(UsageError):
            organize.rename_batch("folder", "旧 [一二]", "同名", apply=True)
        self.assertEqual(drive.writes, [])

    def test_occupied_target_swap_and_chain_rejected_as_whole_plan(self):
        for names, pattern, replacement in ((("ab", "ba"), "^(.)(.)$", r"\2\1"), (("a", "aa"), "^a", "aa"), (("a", "b"), "^a$", "b")):
            drive, organize = self.service()
            drive.items[2]["file_name"], drive.items[3]["file_name"] = names
            with self.assertRaises(UsageError):
                organize.rename_batch("folder", pattern, replacement, apply=True)
            self.assertEqual(drive.writes, [])

    def test_file_requires_reliable_size_and_version_for_rename(self):
        for fields in ({"size": None}, {"updated_at": None}, {"updated_at": ""}, {"updated_at": math.nan}, {"updated_at": 10 ** 400}):
            drive, organize = self.service()
            drive.items[2].update(fields)
            with self.assertRaises(UsageError):
                organize.rename_batch("folder", "旧", "新", apply=True)
            self.assertEqual(drive.writes, [])

    def test_anchor_cannot_be_renamed_and_file_input_not_directory(self):
        drive, organize = self.service()
        scope = Scope(organize.paths, "folder")
        with self.assertRaises(UsageError):
            organize.rename_batch("scope", "整理", "改名", apply=True, scope=scope)
        with self.assertRaises(UsageError):
            organize.rename_batch("one", "旧", "新", apply=True)
        self.assertEqual(drive.writes, [])

    def test_changed_sibling_version_after_first_write_stops_partial(self):
        drive, organize = self.service()
        drive.after_rename = lambda record: drive.items[3].update(updated_at=101) if record["fid"] == "one" else None
        with self.assertRaises(UsageError) as caught:
            organize.rename_batch("folder", "旧", "新", apply=True)
        evidence = caught.exception.organize_evidence
        self.assertEqual((evidence["result"], evidence["renamed"]), ("partial", 1))
        self.assertEqual(len(drive.rename_requests), 1)

    def test_new_or_replaced_sibling_after_first_write_stops_later_writes(self):
        for change in ("new", "replacement", "move"):
            drive, organize = self.service()
            def mutate(record):
                if record["fid"] == "one":
                    if change == "new":
                        drive.items.append(item("third", "其他.txt", "folder", size=1, updated_at=100))
                    elif change == "replacement":
                        drive.items[3]["fid"] = "replacement"
                    else:
                        drive.items[3]["pdir_fid"] = "outside"
            drive.after_rename = mutate
            with self.assertRaises(QuarkError) as caught:
                organize.rename_batch("folder", "旧", "新", apply=True)
            self.assertEqual(caught.exception.organize_evidence["result"], "partial")
            self.assertEqual(len(drive.rename_requests), 1)

    def test_changes_before_first_write_zero_submit(self):
        drive, organize = self.service()
        original = organize._siblings
        count = 0
        def changing(parent, scope):
            nonlocal count
            count += 1
            if count == 2:
                drive.items[3]["size"] = 8
            return original(parent, scope)
        with patch.object(organize, "_siblings", side_effect=changing), self.assertRaises(UsageError):
            organize.rename_batch("folder", "旧", "新", apply=True)
        self.assertEqual(drive.writes, [])

    def test_namespace_changes_during_info_validation_are_detected_before_write(self):
        drive, organize = self.service()
        changed = False
        def mutate(fid):
            nonlocal changed
            if fid == "two" and not changed:
                changed = True
                drive.items[2]["updated_at"] = 200
                drive.items.append(item("new", "其他.txt", "folder", size=1, updated_at=100))
        drive.info_hook = mutate
        with self.assertRaises(QuarkError):
            organize.rename_batch("folder", "旧", "新", apply=True)
        self.assertEqual(drive.writes, [])

    def test_leaf_version_changes_during_last_scope_proof_stop_before_write(self):
        drive, organize = self.service()
        scope = Scope(organize.paths, "scope")
        check = scope.check
        writes = 0
        def changing(fid, **options):
            nonlocal writes
            check(fid, **options)
            if options.get("write"):
                writes += 1
                # Two changed leaves are scope-checked in preflight, then the
                # first leaf is checked again directly before its submission.
                if writes == 3:
                    drive.items[2]["updated_at"] = 200
        with patch.object(scope, "check", side_effect=changing), self.assertRaises(UsageError):
            organize.rename_batch("folder", "旧", "新", apply=True, scope=scope)
        self.assertEqual(drive.writes, [])

    def test_directory_aggregate_time_change_does_not_block_rename(self):
        drive, organize = self.service()
        drive.after_rename = lambda record: drive.items[4].update(updated_at=1000)
        self.assertEqual(organize.rename_batch("folder", "旧", "新", apply=True)["renamed"], 2)

    def test_lost_response_after_apply_read_only_confirms_no_resubmit(self):
        for loss in ("after", "malformed"):
            drive, organize = self.service()
            drive.loss = loss
            result = organize.rename_batch("folder", "旧", "新", apply=True)
            self.assertEqual(result["renamed"], 2)
            self.assertEqual(len(drive.rename_requests), 2)
            self.assertNotIn("canary", json.dumps(result))

    def test_lost_response_before_apply_unknown_once_and_stops(self):
        drive, organize = self.service()
        drive.loss = "before"
        with self.assertRaises(QuarkError) as caught:
            organize.rename_batch("folder", "旧", "新", apply=True)
        self.assertEqual(caught.exception.organize_evidence["result"], "unknown")
        self.assertEqual(len(drive.rename_requests), 1)
        self.assertNotIn("canary", str(caught.exception))

    def test_successful_acknowledgement_but_unknown_name_stops(self):
        drive, organize = self.service()
        drive.after_rename = lambda record: record.update(file_name="第三方名")
        with self.assertRaises(QuarkError) as caught:
            organize.rename_batch("folder", "旧", "新", apply=True)
        self.assertEqual(caught.exception.organize_evidence["result"], "unknown")
        self.assertEqual(len(drive.rename_requests), 1)

    def test_bounded_index_readback_lag_recovers_or_fails_without_repeating_write(self):
        for lag, succeeds in ((2, True), (100, False)):
            drive, organize = self.service()
            drive.after_rename = lambda record: setattr(drive, "lag", lag)
            with patch("quark_cli.organize.time.sleep") as sleep:
                if succeeds:
                    self.assertEqual(organize.rename_batch("folder", "旧", "新", apply=True)["renamed"], 2)
                else:
                    with self.assertRaises(QuarkError):
                        organize.rename_batch("folder", "旧", "新", apply=True)
                    self.assertEqual(len(drive.rename_requests), 1)
                    self.assertEqual(sleep.call_count, 5)

    def test_success_response_followed_by_read_failure_is_unknown(self):
        drive, organize = self.service()
        drive.after_rename = lambda record: drive.info_overrides.update({record["fid"]: {}})
        with self.assertRaises(QuarkError) as caught:
            organize.rename_batch("folder", "旧", "新", apply=True)
        self.assertEqual(caught.exception.organize_evidence["result"], "unknown")
        self.assertEqual(len(drive.rename_requests), 1)

    def test_safe_result_discards_credentials_nested_unknown_fields_and_nan(self):
        value = {"action": "du", "complete": True, "files": 10 ** 400, "known_bytes": math.nan, "cookie": "private-canary-secret",
                 "confirmed": [{"size": 1, "hashes": {"md5": "a" * 32, "token": "private-canary-secret"},
                                "items": [{"fid": "one", "size": 1, "auth_key": "private-canary-secret", "md5": "a" * 32}]}],
                 "items": [{"fid": "one", "new_name": "新.txt", "token": "private-canary-secret"}]}
        result = safe_operation_result(value)
        self.assertNotIn("canary", json.dumps(result))
        self.assertNotIn("known_bytes", result)
        self.assertEqual(result["files"], 10 ** 400)


if __name__ == "__main__":
    unittest.main()
