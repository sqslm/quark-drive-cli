import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quark_cli.cli import main

from drive_fixture import DriveFixture, item


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.drive = DriveFixture()
        self.environment = patch.dict(os.environ, {"QUARK_COOKIE": "sid=mock"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def run_cli(self, arguments, *, code=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(["--json", *arguments], transport=self.drive.transport())
        self.assertEqual(result, code, stderr.getvalue())
        self.assertNotIn("stoken-private", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("share-token-private", stdout.getvalue() + stderr.getvalue())
        return json.loads(stdout.getvalue()) if stdout.getvalue() else json.loads(stderr.getvalue())

    def test_scope_flag_before_and_after_command_overrides_environment(self):
        with patch.dict(os.environ, {"QUARK_SCOPE": "outside"}):
            for arguments in (["--scope", "/范围", "info", "file"], ["info", "file", "--scope", "scope"]):
                with self.subTest(arguments=arguments):
                    self.assertEqual(self.run_cli(arguments)["fid"], "file")
            self.run_cli(["info", "file"], code=2)
        with patch.dict(os.environ, {"QUARK_SCOPE": ""}):
            self.run_cli(["info", "file"], code=2)

    def test_all_reads_refuse_objects_outside_scope(self):
        for arguments in (["info", "outside-file"], ["ls", "outside"], ["tree", "outside"], ["search", "txt", "--in", "outside"], ["download", "outside-file", "--output", "unused.bin"]):
            with self.subTest(arguments=arguments):
                self.run_cli(["--scope", "scope", *arguments], code=2)
        self.assertEqual(self.drive.writes, [])

    def test_scope_does_not_default_missing_reference_to_root(self):
        self.run_cli(["--scope", "scope", "ls"], code=2)
        self.run_cli(["--scope", "scope", "tree"], code=2)

    def test_all_write_previews_make_no_remote_mutation_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "本地测试.txt"
            source.write_text("test", encoding="utf-8")
            commands = [
                ["mkdir", "/范围/新目录"],
                ["upload", str(source), "--parent", "/范围"],
                ["rename", "/范围/中文 空格/报告.PDF", "新名字.pdf"],
                ["mv", "file", "--to", "target"],
                ["rm", "file"],
                ["save", "https://pan.quark.cn/s/mock", "--to", "/范围/新目录"],
            ]
            for arguments in commands:
                with self.subTest(arguments=arguments):
                    preview = self.run_cli(["--scope", "scope", *arguments, "--dry-run"])
                    self.assertTrue(preview["dry_run"])
                    self.assertEqual(self.drive.writes, [])
        token_calls = [request for request in self.drive.calls if request.url.path.endswith("/share/sharepage/token")]
        self.assertEqual(len(token_calls), 1)

    def test_scope_anchor_allows_new_contents_and_rejects_object_mutations(self):
        preview = self.run_cli(["--scope", "scope", "mkdir", "新目录", "--parent", "scope", "--dry-run"])
        self.assertEqual(preview["parent"], "scope")
        for arguments in (["rename", "scope", "别名"], ["mv", "scope", "--to", "target"], ["rm", "scope"]):
            with self.subTest(arguments=arguments):
                self.run_cli(["--scope", "scope", *arguments, "--dry-run"], code=2)
        for command in (["mkdir", "新目录"], ["save", "https://pan.quark.cn/s/mock", "--to", "/"]):
            self.run_cli(["--scope", "/", *command, "--dry-run"], code=2)
        self.assertEqual(self.drive.writes, [])

    def test_move_checks_both_source_and_target_and_rejects_self_descendant(self):
        for source, target, scope in (("file", "outside", "scope"), ("outside-file", "target", "scope"), ("child", "child", None), ("scope", "child", None)):
            with self.subTest(source=source, target=target):
                arguments = (["--scope", scope] if scope else []) + ["mv", source, "--to", target]
                self.run_cli(arguments, code=2)
        self.assertEqual(self.drive.writes, [])

    def test_forged_parent_chain_is_refused_before_write(self):
        self.drive.info_overrides["outside-file"] = item("outside-file", "别人的.txt", "scope")
        self.run_cli(["--scope", "scope", "rm", "outside-file", "--yes"], code=2)
        self.assertEqual(self.drive.writes, [])

    def test_missing_or_mismatched_parent_information_refuses_write(self):
        records = [{"fid": "file", "file_name": "报告.PDF", "file_type": 1}, item("different", "报告.PDF", "child"), item("file", "不一致", "child")]
        for record in records:
            with self.subTest(record=record):
                self.drive.info_overrides["file"] = record
                self.run_cli(["--scope", "scope", "rename", "file", "新名字"], code=4 if record["fid"] == "different" else 2)
        self.assertEqual(self.drive.writes, [])

    def test_cyclic_parent_chain_refuses_write(self):
        self.drive.items.extend([item("cycle-a", "循环A", "cycle-b", directory=True), item("cycle-b", "循环B", "cycle-a", directory=True)])
        self.run_cli(["--scope", "scope", "rm", "cycle-a", "--yes"], code=2)
        self.assertEqual(self.drive.writes, [])

    def test_save_implicit_mkdir_outside_scope_is_refused_before_submission(self):
        self.run_cli(["--scope", "scope", "save", "https://pan.quark.cn/s/mock", "--to", "/范围外/末级"], code=2)
        self.assertEqual(self.drive.writes, [])

    def test_save_existing_target_conflict_preview_has_no_write(self):
        self.drive.items.append(item("conflict", "公开测试.txt", "target"))
        self.run_cli(["--scope", "scope", "save", "https://pan.quark.cn/s/mock", "--to", "/范围/目标", "--dry-run"], code=2)
        self.assertEqual(self.drive.writes, [])

    def test_confirmed_scoped_write_and_fid_reference_preserve_request_shape(self):
        result = self.run_cli(["--scope", "scope", "rename", "file", "新名称"])
        self.assertEqual(result["fid"], "file")
        self.assertEqual(len(self.drive.writes), 1)
        self.assertEqual(json.loads(self.drive.writes[0].read()), {"fid": "file", "file_name": "新名称"})


if __name__ == "__main__":
    unittest.main()
