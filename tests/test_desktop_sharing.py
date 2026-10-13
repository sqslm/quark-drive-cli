import json
import unittest
from unittest.mock import patch

from quark_cli.client import QuarkClient
from quark_cli.desktop.sharing import Sharing
from quark_cli.downloader.share import ShareSource
from quark_cli.errors import UsageError
from share_management_fixture import ManagementFixture
from share_fixture import ShareFixture, SHARE_URL, SOURCE_PATH
from tests.test_desktop_files import entry
from drive_fixture import item


class SharingTests(unittest.TestCase):
    def operations(self, drive):
        client = QuarkClient("sid=offline", transport=drive.transport())
        self.addCleanup(client.close)
        return Sharing(client)

    def test_directory_create_request_count_does_not_grow_with_descendants(self):
        drive = ManagementFixture()
        drive.items.extend(
            item(f"child-{n}", f"{n}.bin", "child", size=4) for n in range(20)
        )
        with patch("quark_cli.share_management.time.sleep"):
            result = self.operations(drive).create(entry("child", "中文 空格", True))
        self.assertEqual(result["result"], "created")
        self.assertLessEqual(
            len(drive.calls),
            25,
            f"{len(drive.calls)} metadata requests for one selected folder",
        )

    def test_fast_create_still_rejects_wrong_root_owner_and_partial_results(self):
        for case in ("wrong-root", "not-owner", "partial", "source-changed"):
            with self.subTest(case=case):
                drive = ManagementFixture()
                if case == "wrong-root":
                    drive.detail_transform = (
                        lambda rows, parent: [{**rows[0], "fid": "unexpected"}]
                        if parent == "0"
                        else rows
                    )
                elif case == "not-owner":
                    drive.owner = 0
                elif case == "partial":
                    drive.submit_snapshot = {"success_count": 0, "failed_count": 1}
                else:
                    drive.after_create = lambda: next(
                        row for row in drive.items if row["fid"] == "child"
                    ).update(file_name="changed")
                with (
                    patch("quark_cli.share_management.time.sleep"),
                    self.assertRaises(Exception) as caught,
                ):
                    self.operations(drive).create(entry("child", "中文 空格", True))
                self.assertIn(
                    caught.exception.share_evidence["result"], ("unknown", "partial")
                )
                self.assertEqual(len(drive.creates), 1)

    def test_progress_reports_phases_without_credentials(self):
        drive = ManagementFixture()
        stages = []
        with patch("quark_cli.share_management.time.sleep"):
            self.operations(drive).create(
                entry("child", "中文 空格", True), progress=stages.append
            )
        self.assertEqual(len(stages), 4)
        self.assertNotIn("secret", str(stages))

    def test_create_has_verified_members_options_and_copy_has_no_tokens(self):
        drive = ManagementFixture()
        operations = self.operations(drive)
        row = next(row for row in drive.items if row["fid"] == "file")
        result = operations.create(
            entry("file", row["file_name"], size=row["size"]),
            passcode="abcd",
            expire_days=30,
        )
        self.assertEqual(result["result"], "created")
        self.assertEqual(drive.creates[0]["expired_type"], 4)
        self.assertEqual(drive.creates[0]["passcode"], "abcd")
        copied = operations.link(result["share_id"])
        self.assertEqual(copied["passcode"], "abcd")
        self.assertNotIn("secret", json.dumps(copied))

    def test_changed_source_rejected_before_create(self):
        drive = ManagementFixture()
        with self.assertRaises(UsageError):
            self.operations(drive).create(entry("file", "wrong.txt", size=4))
        self.assertEqual(drive.creates, [])

    def test_permanent_save_submits_only_selected_fid_even_with_pattern_name(self):
        drive = ShareFixture()
        drive.items.append(item("dest", "目标", "0", directory=True))
        drive.share_directories["source-child"][0]["file_name"] = "[note].txt"
        result = self.operations(drive).save(
            ShareSource(SHARE_URL, "1234"),
            SOURCE_PATH,
            [entry("note", "[note].txt", size=4)],
            "dest",
        )
        self.assertTrue(result["saved"])
        self.assertEqual(drive.saves[0]["fid_list"], ["note"])
        self.assertFalse(
            any(row["fid"].startswith("saved-skip") for row in drive.items)
        )
        self.assertEqual(len(drive.saves), 1)

    def test_permanent_save_conflict_and_stale_selection_never_write(self):
        for stale in (True, False):
            with self.subTest(stale=stale):
                drive = ShareFixture()
                drive.items.append(item("dest", "目标", "0", directory=True))
                if not stale:
                    drive.items.append(item("conflict", "NOTE.TXT", "dest"))
                with self.assertRaises(UsageError):
                    self.operations(drive).save(
                        ShareSource(SHARE_URL),
                        SOURCE_PATH,
                        [entry("note", "stale.txt" if stale else "note.txt", size=4)],
                        "dest",
                    )
                self.assertEqual(drive.saves, [])

    def test_unknown_create_does_not_repeat_submission(self):
        drive = ManagementFixture()
        drive.submit_timeout = True
        operations = self.operations(drive)
        row = next(row for row in drive.items if row["fid"] == "file")
        with self.assertRaises(Exception) as caught:
            operations.create(entry("file", row["file_name"], size=row["size"]))
        self.assertEqual(caught.exception.share_evidence["result"], "unknown")
        self.assertEqual(len(drive.creates), 1)


if __name__ == "__main__":
    unittest.main()
