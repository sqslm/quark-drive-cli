import json
import unittest
from unittest.mock import patch

from quark_cli.client import QuarkClient
from quark_cli.errors import AuthenticationError, QuarkError, RateLimitError, RemoteError, UsageError
from quark_cli.files import Files
from quark_cli.paths import RemotePaths, Scope
from quark_cli.share_management import ShareManager
from share_management_fixture import ManagementFixture
from drive_fixture import item


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.drive = ManagementFixture()
        self.client = QuarkClient("sid=offline", transport=self.drive.transport())
        self.addCleanup(self.client.close)
        self.manager = ShareManager(self.client)
        self.sleep = patch("quark_cli.share_management.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def scope(self):
        return Scope(RemotePaths(Files(self.client)), "scope")

    def assert_no_writes(self):
        self.assertEqual(self.drive.writes, [])

    def test_catalog_default_page_is_explicitly_incomplete_and_safe(self):
        for number in range(51):
            self.drive.add_share(f"share-{number}")
        result = self.manager.list_own()
        self.assertFalse(result["catalog_complete"])
        self.assertEqual(result["catalog_total"], 51)
        self.assertEqual(result["returned_count"], 50)
        self.assertTrue(result["has_more"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertTrue(all("passcode" not in entry and "first_file" not in entry for entry in result["items"]))
        self.assert_no_writes()

    def test_catalog_all_pages_and_protocol_parameters(self):
        for number in range(51):
            self.drive.add_share(f"share-{number}")
        result = self.manager.list_own(all_pages=True)
        self.assertTrue(result["catalog_complete"])
        self.assertEqual(result["returned_count"], 51)
        self.assertFalse(result["has_more"])
        calls = [request for request in self.drive.calls if request.url.path.endswith("/share/mypage/detail")]
        self.assertEqual([request.url.params["_page"] for request in calls], ["1", "2"])
        for request in calls:
            for key, value in {"_size": "50", "_fetch_total": "1", "_fetch_notify_follow": "1",
                               "_order_field": "created_at", "_order_type": "desc", "_filter": "all", "_share_type": "0,1"}.items():
                self.assertEqual(request.url.params[key], value)

    def test_catalog_duplicate_and_missing_ids_reject(self):
        self.drive.add_share()
        for records in ([self.drive.catalog[0]] * 2, [{"title": "bad"}], [{"share_id": True}]):
            with self.subTest(records=records):
                self.drive.catalog = records
                with self.assertRaises(RemoteError):
                    self.manager.list_own(all_pages=True)
                self.assert_no_writes()

    def test_catalog_url_rebuilt_without_query_and_invalid_link_omitted(self):
        self.drive.add_share()
        self.drive.catalog[0]["share_url"] += "?pwd=Ab12"
        result = self.manager.list_own()["items"][0]
        self.assertNotIn("?", result["share_url"])
        self.drive.catalog[0]["share_url"] = "https://evil.invalid/private?token=secret"
        result = self.manager.list_own()["items"][0]
        self.assertNotIn("share_url", result)

    def test_scope_lists_only_verified_roots_without_outside_account_reads(self):
        self.drive.add_share("inside", ("file",))
        self.drive.add_share("mixed", ("file", "outside-file"))
        self.drive.add_share("outside", ("outside-file",))
        result = self.manager.list_own(scope=self.scope())
        self.assertEqual([record["share_id"] for record in result["items"]], ["inside"])
        self.assertEqual((result["verified_total"], result["outside_scope_count"], result["unverifiable_count"]), (1, 2, 0))
        self.assertNotIn("标题 mixed", json.dumps(result, ensure_ascii=False))
        self.assertFalse(any(request.url.path.endswith("/file/info") and request.url.params["fid"] == "outside-file"
                             for request in self.drive.calls))
        self.assertFalse(any(request.url.path.endswith("/file/sort") and request.url.params["pdir_fid"] == "outside"
                             for request in self.drive.calls))
        self.assert_no_writes()

    def test_scope_catalog_page_51_candidate_is_not_silently_lost(self):
        for number in range(50):
            self.drive.add_share(f"outside-{number}", ("outside-file",))
        self.drive.add_share("inside", ("file",))
        result = self.manager.list_own(scope=self.scope())
        self.assertEqual([record["share_id"] for record in result["items"]], ["inside"])
        self.assertTrue(result["catalog_complete"])
        self.assertEqual(result["outside_scope_count"], 50)

    def test_owner_must_be_numeric_one_on_every_page(self):
        self.drive.add_share()
        for owner in (True, "1", 0, None):
            self.drive.owner = owner
            with self.subTest(owner=owner):
                result = self.manager.list_own(scope=self.scope())
                self.assertEqual(result["items"], [])
                self.assertEqual(result["unverifiable_count"], 1)
        self.drive.owner = 1
        self.assertEqual(self.manager.list_own(scope=self.scope())["verified_total"], 1)
        with patch("quark_cli.shares._PAGE_SIZE", 1):
            self.drive.add_share("two", ("file", "target"))
            self.drive.owner = lambda page: 1 if page == 1 else 0
            result = self.manager.list_own(scope=self.scope())
            self.assertEqual(result["unverifiable_count"], 1)
        self.assert_no_writes()

    def test_scope_empty_or_unreadable_roots_are_unverifiable(self):
        self.drive.add_share("empty", ())
        result = self.manager.list_own(scope=self.scope())
        self.assertEqual((result["verified_total"], result["unverifiable_count"]), (0, 1))
        self.drive.add_share("broken")
        self.drive.password_error = True
        result = self.manager.list_own(scope=self.scope())
        self.assertEqual(result["unverifiable_count"], 2)

    def test_scope_parent_change_stops_before_following_new_outside_ancestry(self):
        self.drive.add_share()
        original = self.drive.handler
        def handler(request):
            if request.url.path.endswith("/share/password"):
                self.drive.info_overrides["file"] = item("file", "报告.PDF", "outside", size=4)
            return original(request)
        self.client.close()
        import httpx
        self.client = QuarkClient("sid=offline", transport=httpx.MockTransport(handler))
        self.addCleanup(self.client.close)
        self.manager = ShareManager(self.client)
        result = self.manager.list_own(scope=self.scope())
        self.assertEqual(result["unverifiable_count"], 1)
        self.assertFalse(any(request.url.path.endswith("/file/sort") and request.url.params["pdir_fid"] == "outside"
                             for request in self.drive.calls))

    def test_scope_index_list_and_exact_info_identity_must_agree(self):
        self.drive.add_share()
        self.drive.list_overrides["child"] = [item("file", "报告.PDF", "child", size=9)]
        with self.assertRaises(QuarkError):
            self.manager.list_own(scope=self.scope())
        self.assert_no_writes()

    def test_scope_share_root_real_parent_must_agree_with_account(self):
        self.drive.add_share()
        self.drive.detail_transform = lambda records, parent: [{**record, "pdir_fid": "target"} for record in records]
        result = self.manager.list_own(scope=self.scope())
        self.assertEqual(result["unverifiable_count"], 1)
        self.assert_no_writes()

    def test_create_private_path_validates_members_password_and_catalog(self):
        result = self.manager.create("/范围/中文 空格/报告.PDF", passcode="Ab12", expire_days=1, scope=self.scope())
        self.assertEqual(result["result"], "created")
        self.assertEqual((result["share_id"], result["pwd_id"], result["passcode"]), ("own-created", "public-own-created", "Ab12"))
        self.assertEqual(result["title"], "报告.PDF")
        self.assertEqual(self.drive.creates, [{"fid_list": ["file"], "title": "报告.PDF", "passcode": "Ab12",
                                             "url_type": 2, "expired_type": 2, "support_error_code": ["41060"]}])
        self.assertNotIn("secret", json.dumps(result))

    def test_create_public_body_and_expiry_mapping(self):
        for days, expired in ((0, 1), (1, 2), (7, 3), (30, 4)):
            with self.subTest(days=days):
                self.drive.catalog = []
                result = self.manager.create("file", expire_days=days)
                self.assertEqual(result["expired_type"], expired)
                self.assertNotIn("passcode", self.drive.creates[-1])
                self.assertEqual(self.drive.creates[-1]["url_type"], 1)

    def test_create_invalid_parameters_and_root_are_zero_write(self):
        for ref, options in (("0", {}), ("/", {}),
                             ("file", {"passcode": "中文12"}), ("file", {"passcode": ""}), ("file", {"expire_days": True}),
                             ("file", {"expire_days": 3}), ("missing", {})):
            with self.subTest(ref=ref, options=options), self.assertRaises(QuarkError):
                self.manager.create(ref, **options)
            self.assert_no_writes()

    def test_create_scope_anchor_directory_has_full_snapshot_and_preview_zero_write(self):
        result = self.manager.create("scope", scope=self.scope(), dry_run=True)
        self.assertEqual(result["source_fids"], ["scope"])
        self.assertTrue(result["dry_run"])
        self.assert_no_writes()
        result = self.manager.create("scope", scope=self.scope())
        self.assertEqual(result["result"], "created")
        parents = {request.url.params["pdir_fid"] for request in self.drive.calls if request.url.path.endswith("/share/sharepage/detail")}
        self.assertTrue({"scope", "child", "target"} <= parents)

    def test_create_directory_root_only_does_not_prove_full_success(self):
        self.drive.detail_transform = lambda records, parent: [] if parent == "child" else records
        with self.assertRaises(RemoteError) as caught:
            self.manager.create("scope", scope=self.scope())
        self.assertEqual(caught.exception.share_evidence["result"], "unknown")
        self.assertEqual(caught.exception.share_evidence["share_id"], "own-created")
        self.assertEqual(len(self.drive.creates), 1)
        self.assertEqual(self.drive.deletes, [])

    def test_create_root_missing_extra_or_same_name_different_fid_reject(self):
        for mutation in (lambda records: [], lambda records: records + [item("extra", "extra.txt", "child", size=1)],
                         lambda records: [{**records[0], "fid": "swapped"}]):
            with self.subTest(mutation=mutation):
                self.drive.catalog = []
                self.drive.detail_transform = lambda records, parent: mutation(records) if parent == "0" else records
                with self.assertRaises(RemoteError):
                    self.manager.create("file")
        self.assertEqual(len(self.drive.creates), 3)
        self.assertEqual(self.drive.deletes, [])

    def test_create_password_fields_must_match_request_and_catalog(self):
        for change in ({"share_id": "different"}, {"pwd_id": "wrong"}, {"share_url": "https://evil.invalid/private"},
                       {"passcode": "Cd34"}, {"url_type": 1}, {"expired_type": 1}):
            with self.subTest(change=change):
                self.drive.catalog = []
                self.drive.password_transform = lambda data: {**data, **change}
                with self.assertRaises(RemoteError) as caught:
                    self.manager.create("file", passcode="Ab12", expire_days=1)
                if "passcode" in change:
                    self.assertEqual(caught.exception.share_evidence["passcode"], change["passcode"])
                    self.assertEqual(caught.exception.share_evidence["result"], "unknown")
        self.assertEqual(len(self.drive.creates), 6)

    def test_create_snapshot_partial_preserves_verified_link_without_retry_or_delete(self):
        self.drive.task_data["creation_snapshot"] = {"success_count": 1, "failed_count": 1, "invalid_fids": ["bad-fid"]}
        with self.assertRaises(RemoteError) as caught:
            self.manager.create("file", passcode="Ab12")
        evidence = caught.exception.share_evidence
        self.assertEqual(evidence["result"], "partial")
        self.assertEqual((evidence["task_id"], evidence["share_id"], evidence["passcode"]), ("create-task", "own-created", "Ab12"))
        self.assertIn("share_url", evidence)
        self.assertEqual(len(self.drive.creates), 1)
        self.assertEqual(self.drive.deletes, [])

    def test_create_snapshot_shapes_and_invalid_fids_fail(self):
        for snapshot in ({"success_count": True, "failed_count": 0}, {"success_count": 0, "failed_count": 0},
                         {"success_count": 1, "failed_count": 0, "invalid_fids": ["bad"]}, {"unknown": 1}, [],
                         {"success_count": 1, "failed_count": 0, "invalid_fid_reasons": {"bad": "private-reason-secret"}}):
            with self.subTest(snapshot=snapshot):
                self.drive.catalog = []
                self.drive.submit_snapshot = snapshot
                with self.assertRaises(RemoteError) as caught:
                    self.manager.create("file")
                self.assertNotIn("private-reason-secret", str(caught.exception) + json.dumps(caught.exception.share_evidence))

    def test_create_submit_unknown_or_missing_task_never_retries(self):
        for timeout in (True, False):
            with self.subTest(timeout=timeout):
                self.drive.catalog = []
                self.drive.submit_timeout = timeout
                self.drive.submit_data = {}
                before = len(self.drive.creates)
                with self.assertRaises(RemoteError) as caught:
                    self.manager.create("file")
                self.assertEqual(caught.exception.share_evidence["result"], "unknown")
                self.assertNotIn("task_id", caught.exception.share_evidence)
                self.assertEqual(len(self.drive.creates), before + 1)

    def test_create_task_unknown_paused_failed_or_missing_share_id_nonzero(self):
        for task in ({"status": 2}, {"status": True}, {"status": 99}, {"status": 2, "finish": False},
                     {"status": 3, "finish": True}, {"status": 4}):
            with self.subTest(task=task):
                self.drive.catalog = []
                self.drive.task_data = task
                with self.assertRaises(RemoteError) as caught:
                    self.manager.create("file")
                self.assertEqual(caught.exception.share_evidence["task_id"], "create-task")
        self.assertEqual(len(self.drive.creates), 6)

    def test_create_task_and_password_read_fail_keep_safe_task_evidence(self):
        for stage in ("task", "password"):
            with self.subTest(stage=stage):
                self.drive.catalog = []
                self.drive.task_timeout = stage == "task"
                self.drive.password_error = stage == "password"
                with self.assertRaises(RemoteError) as caught:
                    self.manager.create("file")
                self.assertEqual(caught.exception.share_evidence["task_id"], "create-task")
                self.assertNotIn("secret", str(caught.exception) + json.dumps(caught.exception.share_evidence))

    def test_create_source_changed_after_submission_cannot_be_confirmed(self):
        self.drive.after_create = lambda: self.drive.info_overrides.update({"file": item("file", "报告.PDF", "child", size=9)})
        with self.assertRaises(RemoteError):
            self.manager.create("file")
        self.assertEqual(len(self.drive.creates), 1)

    def test_create_poll_delay_is_finite_milliseconds_and_bounded(self):
        from quark_cli.share_management import _poll_delay
        self.assertEqual((_poll_delay({"tq_gap": 2000}), _poll_delay({"tq_gap": 50000}), _poll_delay({"tq_gap": 1})), (2, 5, 0.1))
        for value in (True, 0, -1, float("nan"), float("inf"), "1000", None):
            self.assertEqual(_poll_delay({"tq_gap": value}), 0.5)

    def test_scoped_account_root_is_synthetic_without_info_zero(self):
        self.drive.add_share()
        scope = Scope(RemotePaths(Files(self.client)), "0")
        self.assertEqual(self.manager.list_own(scope=scope)["verified_total"], 1)
        self.assertFalse(any(request.url.path.endswith("/file/info") and request.url.params["fid"] == "0"
                             for request in self.drive.calls))

    def test_huge_optional_numeric_fields_are_omitted_without_overflow(self):
        from quark_cli.share_management import safe_operation_result, _poll_delay
        self.drive.add_share()
        self.drive.catalog[0]["created_at"] = 10**400
        self.assertNotIn("created_at", self.manager.list_own()["items"][0])
        self.assertEqual(safe_operation_result({"expire_days": 10**400}), {})
        self.assertEqual(_poll_delay({"tq_gap": 10**400}), 0.5)

    def test_task_malformed_snapshot_still_preserves_confirmed_cleanup_link(self):
        self.drive.task_data["creation_snapshot"] = {"success_count": True, "failed_count": 0}
        with self.assertRaises(RemoteError) as caught:
            self.manager.create("file")
        self.assertEqual(caught.exception.share_evidence["result"], "partial")
        self.assertIn("share_url", caught.exception.share_evidence)

    def test_revoke_second_catalog_page_exact_internal_id_and_one_submit(self):
        for number in range(51):
            self.drive.add_share(f"share-{number}")
        result = self.manager.revoke("share-50")
        self.assertEqual(result, {"result": "revoked", "share_id": "share-50", "revoked": True})
        self.assertEqual(self.drive.deletes, [{"share_ids": ["share-50"]}])
        self.assertEqual(len(self.drive.catalog), 50)

    def test_revoke_preview_scope_proof_zero_write(self):
        self.drive.add_share()
        result = self.manager.revoke("own-share", scope=self.scope(), dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["revoked"])
        self.assert_no_writes()

    def test_revoke_unowned_public_url_or_invalid_id_zero_write(self):
        self.drive.add_share()
        for share_id in ("absent", "public-own-share", "https://pan.quark.cn/s/public-own-share", "", "bad\x1b"):
            with self.subTest(share_id=share_id), self.assertRaises(UsageError):
                self.manager.revoke(share_id)
            self.assert_no_writes()

    def test_revoke_scoped_mixed_roots_in_second_share_page_refuses_without_outside_reads(self):
        self.drive.add_share("mixed", ("file", "outside-file"))
        with patch("quark_cli.shares._PAGE_SIZE", 1), self.assertRaises(UsageError):
            self.manager.revoke("mixed", scope=self.scope())
        self.assert_no_writes()
        self.assertFalse(any(request.url.path.endswith("/file/info") and request.url.params["fid"] == "outside-file"
                             for request in self.drive.calls))

    def test_revoke_scope_requires_owner_but_unscoped_expired_own_share_allowed(self):
        self.drive.add_share()
        self.drive.owner = 0
        with self.assertRaises(RemoteError):
            self.manager.revoke("own-share", scope=self.scope())
        self.assert_no_writes()
        self.drive.password_error = True
        self.assertTrue(self.manager.revoke("own-share")["revoked"])

    def test_revoke_read_confirmation_retries_lag_not_write(self):
        self.drive.add_share()
        self.drive.delete_delay = 2
        self.assertTrue(self.manager.revoke("own-share")["revoked"])
        self.assertEqual(len(self.drive.deletes), 1)
        self.assertEqual(self.drive.delete_reads, 3)

    def test_revoke_timeout_can_only_disambiguate_by_full_catalog(self):
        self.drive.add_share()
        self.drive.delete_timeout = True
        self.assertTrue(self.manager.revoke("own-share")["revoked"])
        self.assertEqual(len(self.drive.deletes), 1)

    def test_revoke_still_present_is_unknown_without_resubmit(self):
        self.drive.add_share()
        self.drive.delete_delay = 100
        with self.assertRaises(RemoteError) as caught:
            self.manager.revoke("own-share")
        self.assertEqual(caught.exception.share_evidence, {"result": "unknown", "share_id": "own-share", "revoked": False})
        self.assertEqual(len(self.drive.deletes), 1)
        self.assertEqual(self.drive.delete_reads, 6)

    def test_revoke_confirmation_incomplete_is_not_absent_proof(self):
        self.drive.add_share()
        self.drive.catalog_transform = lambda records, page: [{"share_id": True}] if self.drive.delete_ids else records
        with self.assertRaises(RemoteError):
            self.manager.revoke("own-share")
        self.assertEqual(len(self.drive.deletes), 1)

    def test_catalog_total_changing_or_early_end_and_page_limit_refuse_write(self):
        import httpx
        for variant in ("early", "changed", "excess", "limit", "duplicate"):
            with self.subTest(variant=variant):
                drive = ManagementFixture()
                for number in range(51):
                    drive.add_share(f"share-{number}")
                original = drive.handler
                def handler(request):
                    response = original(request)
                    if request.url.path.endswith("/share/mypage/detail"):
                        payload = response.json()
                        page = int(request.url.params["_page"])
                        if variant == "early":
                            payload["data"]["list"] = []
                        elif variant == "changed" and page == 2:
                            payload["metadata"]["_total"] = 52
                        elif variant == "excess":
                            payload["metadata"]["_total"] = 1
                        elif variant == "duplicate" and page == 2:
                            payload["data"]["list"] = [drive.catalog[0]]
                        return httpx.Response(200, json=payload, request=request)
                    return response
                with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client:
                    with patch("quark_cli.share_management._MAX_PAGES", 1 if variant == "limit" else 10000), self.assertRaises(RemoteError):
                        ShareManager(client).revoke("share-50")
                self.assertEqual(drive.writes, [])

    def test_catalog_without_total_reads_short_final_page(self):
        import httpx
        for number in range(51):
            self.drive.add_share(f"share-{number}")
        original = self.drive.handler
        def handler(request):
            response = original(request)
            payload = response.json()
            payload.pop("metadata", None)
            return httpx.Response(200, json=payload, request=request)
        self.client.close()
        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client:
            result = ShareManager(client).list_own(all_pages=True)
        self.assertEqual(result["returned_count"], 51)
        self.assertIsNone(result["catalog_total"])

    def test_catalog_first_inconsistent_read_restarts_whole_snapshot(self):
        import httpx
        self.drive.add_share()
        original = self.drive.handler
        count = 0
        def handler(request):
            nonlocal count
            response = original(request)
            if request.url.path.endswith("/share/mypage/detail"):
                count += 1
                if count == 1:
                    payload = response.json()
                    payload["metadata"]["_total"] = 2
                    return httpx.Response(200, json=payload, request=request)
            return response
        self.client.close()
        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client:
            result = ShareManager(client).list_own(all_pages=True)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(count, 2)

    def test_scope_default_returns_first_50_verified_and_has_more(self):
        for number in range(51):
            self.drive.add_share(f"inside-{number}")
        result = self.manager.list_own(scope=self.scope())
        self.assertEqual((result["verified_total"], result["returned_count"], result["has_more"]), (51, 50, True))
        result = self.manager.list_own(scope=self.scope(), all_pages=True)
        self.assertEqual((result["returned_count"], result["has_more"]), (51, False))

    def test_owned_member_malformed_type_cannot_be_normalized_into_file(self):
        self.drive.add_share()
        self.drive.detail_transform = lambda records, parent: [{**record, "file_type": 42} for record in records]
        result = self.manager.list_own(scope=self.scope())
        self.assertEqual(result["unverifiable_count"], 1)
        self.assert_no_writes()

    def test_create_full_snapshot_rejects_forged_child_info_parent_before_writing(self):
        self.drive.info_overrides["file"] = item("file", "报告.PDF", "target", size=4)
        with self.assertRaises(RemoteError):
            self.manager.create("scope", scope=self.scope())
        self.assert_no_writes()

    def test_create_source_subtree_added_after_submit_is_unconfirmed(self):
        self.drive.after_create = lambda: self.drive.items.append(item("new-source", "new.txt", "child", size=1))
        with self.assertRaises(RemoteError) as caught:
            self.manager.create("scope", scope=self.scope())
        self.assertEqual(caught.exception.share_evidence["result"], "unknown")
        self.assertEqual(len(self.drive.creates), 1)

    def test_create_task_timeout_bound_preserves_original_task(self):
        self.drive.task_data = {"status": 1}
        with patch("quark_cli.share_management._MAX_POLLS", 3), self.assertRaises(RemoteError) as caught:
            self.manager.create("file")
        self.assertEqual(caught.exception.share_evidence["task_id"], "create-task")
        task_requests = [request for request in self.drive.calls if request.url.path.endswith("/task")]
        self.assertEqual([request.url.params["retry_index"] for request in task_requests], ["0", "1", "2"])
        self.assertEqual(len(self.drive.creates), 1)

    def test_password_optional_internal_id_conflict_and_public_private_mismatch(self):
        self.drive.add_share()
        for fields in ({"share_id": "wrong"}, {"url_type": True}, {"expired_type": "3"}, {"passcode": "Ab12"}):
            self.drive.password_transform = lambda data: {**data, **fields}
            with self.subTest(fields=fields):
                self.assertEqual(self.manager.list_own(scope=self.scope())["unverifiable_count"], 1)

    def test_scoped_authentication_failure_does_not_return_fake_partial_catalog(self):
        import httpx
        self.drive.add_share()
        original = self.drive.handler
        def handler(request):
            if request.url.path.endswith("/share/password"):
                return httpx.Response(401, request=request)
            return original(request)
        self.client.close()
        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client, self.assertRaises(AuthenticationError):
            ShareManager(client).list_own(scope=Scope(RemotePaths(Files(client)), "scope"))

    def test_explicit_business_submit_rejection_failed_without_confirmation_reads(self):
        import httpx
        for action in ("create", "revoke"):
            drive = ManagementFixture()
            drive.add_share()
            original = drive.handler
            def handler(request):
                if request.method == "POST" and request.url.path.endswith(("/share", "/share/delete")):
                    drive.calls.append(request)
                    return httpx.Response(200, json={"status": 400, "code": 41001, "message": "business-secret"}, request=request)
                return original(request)
            with self.subTest(action=action), QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client, self.assertRaises(RemoteError) as caught:
                manager = ShareManager(client)
                manager.create("file") if action == "create" else manager.revoke("own-share")
            self.assertEqual(caught.exception.share_evidence["result"], "failed")
            self.assertNotIn("business-secret", str(caught.exception))
            self.assertEqual(len(drive.writes), 1)
            if action == "revoke":
                self.assertEqual(sum(request.url.path.endswith("/share/mypage/detail") for request in drive.calls), 1)

    def test_http200_partial_business_code_preserves_original_task_and_link(self):
        import httpx
        original = self.drive.handler
        def handler(request):
            response = original(request)
            if request.method == "POST" and request.url.path.endswith("/share"):
                payload = response.json()
                payload.update({"status": 400, "code": 41060})
                payload["data"]["task_resp"] = {"data": {"creation_snapshot": {"failed_count": 1, "success_count": 1}}}
                return httpx.Response(200, json=payload, request=request)
            return response
        self.client.close()
        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client, self.assertRaises(RemoteError) as caught:
            ShareManager(client).create("file", passcode="Ab12")
        evidence = caught.exception.share_evidence
        self.assertEqual((evidence["result"], evidence["task_id"], evidence["share_id"], evidence["passcode"]),
                         ("partial", "create-task", "own-created", "Ab12"))
        self.assertIn("share_url", evidence)
        self.assertEqual(len(self.drive.creates), 1)

    def test_post_acceptance_read_business_rejection_stays_unknown_not_failed(self):
        import httpx
        original = self.drive.handler
        def handler(request):
            if request.url.path.endswith("/share/password"):
                return httpx.Response(200, json={"status": 400, "code": 41001, "message": "password-secret"}, request=request)
            return original(request)
        self.client.close()
        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client, self.assertRaises(RemoteError) as caught:
            ShareManager(client).create("file")
        self.assertEqual(caught.exception.share_evidence["result"], "unknown")
        self.assertEqual(caught.exception.share_evidence["share_id"], "own-created")

    def test_server_5xx_or_accepted_receipt_is_never_definite_rejection(self):
        import httpx
        for http_status, payload in ((500, {"status": 500, "code": 41001}),
                                     (200, {"status": 500, "code": 41001}),
                                     (200, {"status": 400, "code": 41001, "data": {"task_id": "accepted"}})):
            def handler(request):
                if request.url.path.endswith("/share"):
                    return httpx.Response(http_status, json=payload, request=request)
                return self.drive.handler(request)
            with self.subTest(http_status=http_status, payload=payload), QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client, self.assertRaises(RemoteError) as caught:
                ShareManager(client).create("file")
            self.assertEqual(caught.exception.share_evidence["result"], "unknown")

    def test_explicit_auth_business_revoke_rejection_is_failed_with_exit_three(self):
        import httpx
        self.drive.add_share()
        original = self.drive.handler
        def handler(request):
            if request.url.path.endswith("/share/delete"):
                self.drive.calls.append(request)
                return httpx.Response(200, json={"status": 400, "code": 401, "message": "refused-secret"}, request=request)
            return original(request)
        self.client.close()
        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client, self.assertRaises(AuthenticationError) as caught:
            ShareManager(client).revoke("own-share")
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertEqual(caught.exception.share_evidence["result"], "failed")
        self.assertEqual(len(self.drive.writes), 1)
        self.assertNotIn("secret", str(caught.exception))

    def test_unknown_business_40301_is_not_treated_as_auth(self):
        import httpx
        self.drive.add_share()
        original = self.drive.handler
        def handler(request):
            if request.url.path.endswith("/share/delete"):
                self.drive.calls.append(request)
                return httpx.Response(200, json={"status": 400, "code": 40301, "message": "refused-secret"}, request=request)
            return original(request)
        self.client.close()
        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client, self.assertRaises(RemoteError) as caught:
            ShareManager(client).revoke("own-share")
        self.assertNotIsInstance(caught.exception, AuthenticationError)
        self.assertEqual(caught.exception.exit_code, 4)
        self.assertEqual(caught.exception.share_evidence["result"], "failed")
        self.assertEqual(len(self.drive.writes), 1)
        self.assertNotIn("secret", str(caught.exception))

    def test_revoke_rejection_classification_follows_receipt_evidence(self):
        import httpx
        cases = (
            ("exact_business_403_no_receipt",
             {"status_code": 200, "json": {"status": 400, "code": 403, "message": "refused-secret"}},
             RemoteError, "failed"),
            ("ambiguous_http_429",
             {"status_code": 429, "headers": {"Retry-After": "60"}},
             RateLimitError, "unknown"),
            ("business_403_with_receipt",
             {"status_code": 200, "json": {"status": 400, "code": 403, "message": "refused-secret",
                                           "data": {"task_id": "accepted"}}},
             RemoteError, "unknown"),
        )
        for label, reply, expected_error, expected_result in cases:
            with self.subTest(label=label):
                drive = ManagementFixture()
                drive.add_share()
                original = drive.handler
                def handler(request, reply=reply):
                    if request.url.path.endswith("/share/delete"):
                        drive.calls.append(request)
                        return httpx.Response(request=request, **reply)
                    return original(request)
                with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client, \
                        self.assertRaises(expected_error) as caught:
                    ShareManager(client).revoke("own-share")
                self.assertNotIsInstance(caught.exception, AuthenticationError)
                self.assertEqual(caught.exception.share_evidence["result"], expected_result)
                self.assertEqual(len(drive.writes), 1)
                # A refusal or an active cooldown never replays the write or
                # reads the catalog back: only the pre-submit catalog read runs.
                reads = sum(request.url.path.endswith("/share/mypage/detail") for request in drive.calls)
                self.assertEqual(reads, 1)
                if label == "ambiguous_http_429":
                    self.assertGreaterEqual(caught.exception.retry_after, 1)

    def test_partial_business_code_without_task_receipt_is_unknown(self):
        import httpx
        original = self.drive.handler
        def handler(request):
            if request.url.path.endswith("/share"):
                self.drive.calls.append(request)
                return httpx.Response(200, json={"status": 400, "code": 41060, "data": {}}, request=request)
            return original(request)
        self.client.close()
        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client, self.assertRaises(RemoteError) as caught:
            ShareManager(client).create("file")
        self.assertEqual(caught.exception.share_evidence["result"], "unknown")
        self.assertNotIn("task_id", caught.exception.share_evidence)
        self.assertEqual(len(self.drive.writes), 1)

    def test_partial_code_with_task_but_no_confirmed_link_does_not_claim_partial_created(self):
        import httpx
        original = self.drive.handler
        self.drive.task_timeout = True
        def handler(request):
            response = original(request)
            if request.url.path.endswith("/share"):
                payload = response.json()
                payload.update({"status": 400, "code": 41060})
                return httpx.Response(200, json=payload, request=request)
            return response
        self.client.close()
        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client, self.assertRaises(RemoteError) as caught:
            ShareManager(client).create("file")
        self.assertEqual(caught.exception.share_evidence["result"], "unknown")
        self.assertEqual(caught.exception.share_evidence["task_id"], "create-task")
        self.assertNotIn("share_url", caught.exception.share_evidence)
        self.assertEqual(len(self.drive.creates), 1)

    def test_backend_option_mismatch_retains_actual_associated_link_and_title(self):
        for field, actual in (("title", "actual backend title"), ("expired_type", 1), ("passcode", "Cd34")):
            with self.subTest(field=field):
                self.drive.catalog = []
                def change_actual():
                    self.drive.passwords["own-created"][field] = actual
                    self.drive.catalog[-1][field] = actual
                self.drive.after_create = change_actual
                with self.assertRaises(RemoteError) as caught:
                    self.manager.create("file", passcode="Ab12", expire_days=7)
                evidence = caught.exception.share_evidence
                self.assertEqual(evidence["result"], "unknown")
                self.assertEqual(evidence[field], actual)
                self.assertEqual(evidence["share_id"], "own-created")
                self.assertIn("share_url", evidence)
        self.assertEqual(len(self.drive.creates), 3)
        self.assertEqual(self.drive.deletes, [])

    def test_scoped_nested_shared_file_moved_outside_is_filtered_and_never_revoked(self):
        self.drive.add_share("stale", ("child",))
        next(record for record in self.drive.items if record["fid"] == "file")["pdir_fid"] = "outside"
        result = self.manager.list_own(scope=self.scope())
        self.assertEqual((result["verified_total"], result["outside_scope_count"], result["items"]), (0, 1, []))
        with self.assertRaises(UsageError):
            self.manager.revoke("stale", scope=self.scope())
        self.assert_no_writes()
        self.assertFalse(any(request.url.path.endswith("/file/info") and request.url.params["fid"] == "file"
                             for request in self.drive.calls))
        self.assertFalse(any(request.url.path.endswith("/file/sort") and request.url.params["pdir_fid"] == "outside"
                             for request in self.drive.calls))

    def test_scoped_directory_share_may_be_valid_subset_without_all_account_descendants(self):
        self.drive.add_share("subset", ("child",))
        self.drive.members["public-subset"]["child"] = []
        self.assertEqual(self.manager.list_own(scope=self.scope())["verified_total"], 1)
        self.assertTrue(self.manager.revoke("subset", scope=self.scope())["revoked"])

    def test_scoped_nested_owner_parent_or_cycle_failure_refuses_revoke(self):
        import httpx
        for failure in ("owner", "parent", "cycle", "page"):
            drive = ManagementFixture()
            drive.add_share("nested", ("child",))
            original = drive.handler
            def handler(request):
                response = original(request)
                if request.url.path.endswith("/share/sharepage/detail") and request.url.params["pdir_fid"] == "child":
                    payload = response.json()
                    if failure == "owner":
                        payload["data"]["is_owner"] = 0
                    elif failure == "parent":
                        payload["data"]["list"][0]["pdir_fid"] = "scope"
                    elif failure == "cycle":
                        payload["data"]["list"] = [item("child", "中文 空格", "child", directory=True)]
                    else:
                        payload["metadata"]["_total"] = 2
                    return httpx.Response(200, json=payload, request=request)
                return response
            with self.subTest(failure=failure), QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client:
                manager = ShareManager(client)
                scope = Scope(RemotePaths(Files(client)), "scope")
                result = manager.list_own(scope=scope)
                self.assertEqual(result["unverifiable_count"], 1)
                with self.assertRaises(RemoteError):
                    manager.revoke("nested", scope=scope)
            self.assertEqual(drive.writes, [])

    def test_scoped_revoke_rechecks_all_proved_members_before_submit(self):
        self.drive.add_share("nested", ("child",))
        scope = self.scope()
        original = self.manager._check_account
        file_checks = 0
        def check(identity, boundary):
            nonlocal file_checks
            if identity["fid"] == "file":
                file_checks += 1
                # First: scope snapshot. Second: shared descendant proof.
                # Third: the last check immediately before delete.
                if file_checks == 3:
                    self.drive.info_overrides["file"] = item("file", "报告.PDF", "outside", size=4)
            return original(identity, boundary)
        with patch.object(self.manager, "_check_account", side_effect=check), self.assertRaises(RemoteError):
            self.manager.revoke("nested", scope=scope)
        self.assert_no_writes()
        self.assertEqual(file_checks, 3)

    def test_scoped_member_global_duplicates_are_rejected(self):
        self.drive.add_share("two", ("child", "file"))
        self.assertEqual(self.manager.list_own(scope=self.scope())["unverifiable_count"], 1)
        with self.assertRaises(RemoteError):
            self.manager.revoke("two", scope=self.scope())
        self.assert_no_writes()

    def test_scoped_member_depth_and_item_limits_fail_before_write(self):
        self.drive.add_share("nested", ("child",))
        for constant, value in (("_MAX_MEMBER_DEPTH", 0), ("_MAX_MEMBER_ITEMS", 1)):
            with self.subTest(constant=constant), patch(f"quark_cli.share_management.{constant}", value):
                self.assertEqual(self.manager.list_own(scope=self.scope())["unverifiable_count"], 1)
                with self.assertRaises(RemoteError):
                    self.manager.revoke("nested", scope=self.scope())
            self.assert_no_writes()


if __name__ == "__main__":
    unittest.main()
