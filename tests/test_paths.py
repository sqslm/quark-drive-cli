import unittest
from unittest.mock import patch

import httpx

from quark_cli.client import QuarkClient
from quark_cli.errors import RemoteError, UsageError
from quark_cli.files import Files
from quark_cli.paths import RemotePaths, Scope, is_directory, path_components, validate_name

from drive_fixture import DriveFixture, item


class PathsTests(unittest.TestCase):
    def setUp(self):
        self.drive = DriveFixture()
        self.client = QuarkClient("sid=mock", transport=self.drive.transport())
        self.addCleanup(self.client.close)
        self.paths = RemotePaths(Files(self.client))

    def test_exact_chinese_spaces_root_and_fid_compatibility(self):
        self.assertEqual(self.paths.resolve("/范围/中文 空格/报告.PDF").fid, "file")
        self.assertEqual(self.paths.resolve("/").fid, "0")
        self.assertEqual(self.paths.resolve("0").path, "/")
        before = len(self.drive.calls)
        self.assertEqual(self.paths.resolve("file").fid, "file")
        self.assertEqual(len(self.drive.calls), before)

    def test_path_resolution_reads_all_pages_before_matching(self):
        self.drive.items = [item(f"fid-{index}", f"项目{index}", directory=True) for index in range(55)]
        self.assertEqual(self.paths.resolve("/项目54", directory=True).fid, "fid-54")
        pages = [request.url.params["_page"] for request in self.drive.calls]
        self.assertEqual(pages, ["1", "2"])
        self.drive.items.append(item("duplicate", "项目54", directory=True))
        with self.assertRaisesRegex(UsageError, "不唯一"):
            self.paths.resolve("/项目54")

    def test_invalid_paths_and_names_are_rejected_before_network(self):
        for path in ("/a//b", "/a/.", "/a/..", "/a\\b", "/a/", "/a\x1bb", "/a\tb"):
            with self.subTest(path=path), self.assertRaises(UsageError):
                path_components(path)
        for name in ("", ".", "..", "a/b", "a\\b", "\x00", "\x7f", "\x01", "\t"):
            with self.subTest(name=name), self.assertRaises(UsageError):
                validate_name(name)
        self.assertEqual(self.drive.calls, [])
        with self.assertRaises(UsageError):
            self.paths.resolve("relative/path")

    def test_missing_intermediate_non_directory_and_missing_leaf(self):
        with self.assertRaisesRegex(UsageError, "不是目录"):
            self.paths.resolve("/范围/中文 空格/报告.PDF/子项")
        with self.assertRaisesRegex(UsageError, "不存在"):
            self.paths.directory_target("/范围/不存在/末级", allow_missing_leaf=True)
        target = self.paths.directory_target("/范围/末级", allow_missing_leaf=True)
        self.assertTrue(target.missing)
        self.assertEqual((target.parent_fid, target.name), ("scope", "末级"))
        with self.assertRaises(UsageError):
            self.paths.directory_target("/范围/末级")
        self.assertEqual(self.drive.writes, [])

    def test_wrong_parent_and_malformed_directory_type(self):
        self.drive.list_overrides["scope"] = [item("forged", "坏项", "outside", directory=True)]
        with self.assertRaisesRegex(RemoteError, "父 FID"):
            self.paths.resolve("/范围/坏项")
        for record in ({"file_type": False}, {"file_type": 1, "dir": True}, {"dir": "true"}):
            with self.subTest(record=record), self.assertRaises(RemoteError):
                is_directory(record)

    def test_walk_depth_paths_and_fid_ancestry(self):
        result = list(self.paths.walk("/范围", depth=1))
        self.assertEqual([entry["path"] for entry in result], ["/范围", "/范围/中文 空格", "/范围/目标"])
        self.assertEqual([entry["depth"] for entry in result], [0, 1, 1])
        self.assertEqual(len(list(self.paths.walk("/范围", depth=0))), 1)
        self.assertEqual(list(self.paths.walk("child"))[0]["path"], "/范围/中文 空格")
        self.assertEqual(self.paths.absolute_path("file"), "/范围/中文 空格/报告.PDF")
        self.assertEqual(self.paths.absolute_path("0"), "/")

    def test_walk_and_resolve_detect_cycles(self):
        cyclic = item("scope", "循环", "child", directory=True)
        self.drive.list_overrides["child"] = [cyclic]
        with self.assertRaisesRegex(RemoteError, "循环"):
            list(self.paths.walk("/范围"))
        with self.assertRaisesRegex(RemoteError, "循环"):
            self.paths.resolve("/范围/中文 空格/循环")

    def test_directory_target_rejects_leaf_pointing_to_higher_ancestor(self):
        self.drive.list_overrides["child"] = [{"fid": "scope", "file_name": "回到祖先", "file_type": 0}]
        for allow_missing in (False, True):
            with self.subTest(allow_missing=allow_missing), self.assertRaisesRegex(RemoteError, "循环"):
                self.paths.directory_target("/范围/中文 空格/回到祖先", allow_missing_leaf=allow_missing)
        self.assertEqual(self.drive.writes, [])

    def test_ancestry_cross_checks_claimed_parent_membership(self):
        self.drive.info_overrides["outside-file"] = item("outside-file", "别人的.txt", "scope")
        scope = Scope(self.paths, "scope")
        with self.assertRaisesRegex(UsageError, "声明的父目录"):
            scope.check("outside-file", write=True)
        self.assertEqual(self.drive.writes, [])

    def test_ancestry_rejects_missing_parent_cycles_and_non_directory_ancestors(self):
        self.drive.info_overrides["file"] = {"fid": "file", "file_name": "报告.PDF"}
        with self.assertRaisesRegex(UsageError, "信息缺失"):
            self.paths.ancestry("file")
        self.drive.info_overrides["file"] = item("file", "报告.PDF", "child")
        self.drive.info_overrides["child"] = item("child", "中文 空格", "file", directory=True)
        self.drive.list_overrides["file"] = [self.drive.info_overrides["child"]]
        with self.assertRaisesRegex(UsageError, "循环|非目录"):
            self.paths.ancestry("file")

    def test_pagination_inconsistent_totals_early_end_and_duplicates_fail(self):
        cases = [
            [{"list": [item("one", "一")], "_total": 3}],
            [{"list": [item("one", "一"), item("two", "二")], "_total": 1}],
            [{"list": [item("one", "一"), item("one", "一")], "_total": 2}],
            [{"list": [item("one", "一")], "_total": True}],
        ]
        for responses in cases:
            with self.subTest(responses=responses), patch("quark_cli.files.time.sleep"):
                def handler(request):
                    return httpx.Response(200, json={"status": 200, "data": responses[0]}, request=request)
                with QuarkClient("sid=mock", transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(RemoteError):
                        RemotePaths(Files(client)).children("0")


if __name__ == "__main__":
    unittest.main()
