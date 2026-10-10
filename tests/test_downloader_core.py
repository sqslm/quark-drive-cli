"""Headless rules of the standalone downloader window.

Nothing here needs a display: the listing rows, folder navigation, download
target, progress text, completion line and error line are plain functions.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quark_cli.downloader.core import (
    ROOT_FID,
    Location,
    describe_error,
    display_text,
    download_plan,
    entries,
    entry_of,
    progress_fraction,
    progress_text,
    summary_text,
)
from quark_cli.errors import LocalIOError, RemoteError, UsageError
from quark_cli.transfer_progress import ProgressEvent


def _record(fid: str, name: str, *, directory: bool = False, size: int = 0) -> dict:
    return {"fid": fid, "file_name": name, "file_type": 0 if directory else 1, "size": size}


class DisplayTests(unittest.TestCase):
    def test_control_characters_can_never_reach_a_widget(self):
        self.assertEqual(display_text("a\tb\nc"), "a b c")
        self.assertEqual(display_text("a\x00b\x7fc"), "a b c")
        self.assertEqual(display_text(None), "")
        self.assertEqual(display_text(12), "12")


class EntryTests(unittest.TestCase):
    def test_entry_of_keeps_the_remote_name_and_normalizes_an_unknown_size(self):
        entry = entry_of(_record(7, "报告.PDF", size=4))
        self.assertEqual((entry.fid, entry.name, entry.directory, entry.size), ("7", "报告.PDF", False, 4))

    def test_directories_and_malformed_sizes_report_no_size(self):
        self.assertIsNone(entry_of(_record("d", "目录", directory=True, size=9)).size)
        self.assertIsNone(entry_of({"fid": "f", "file_name": "f.bin", "file_type": 1, "size": "4"}).size)
        self.assertIsNone(entry_of({"fid": "f", "file_name": "f.bin", "file_type": 1, "size": -1}).size)

    def test_malformed_records_fail_the_whole_listing(self):
        with self.assertRaises(RemoteError):
            entry_of({"fid": "f", "file_type": 1, "size": 1})
        with self.assertRaises(RemoteError):
            entry_of({"file_name": "f.bin", "file_type": 1, "size": 1})
        with self.assertRaises(RemoteError):
            entry_of({"fid": "", "file_name": "f.bin", "file_type": 1, "size": 1})

    def test_labels_and_rows_are_display_safe(self):
        directory = entry_of(_record("d", "目\u0000录", directory=True))
        self.assertEqual(directory.row, ("目 录", "目录", "—"))
        self.assertEqual(entry_of(_record("f", "f.bin", size=10)).row, ("f.bin", "文件", "10 B"))

    def test_entries_put_folders_first_then_case_insensitive_names(self):
        rows = entries(
            [
                _record("f2", "b.txt", size=3),
                _record("d1", "Zeta", directory=True),
                _record("d2", "alpha", directory=True),
                _record("f1", "A.txt", size=1),
            ]
        )
        self.assertEqual([row.name for row in rows], ["alpha", "Zeta", "A.txt", "b.txt"])
        self.assertEqual([row.directory for row in rows], [True, True, False, False])


class LocationTests(unittest.TestCase):
    def test_navigation_is_a_chain_of_the_folders_entered(self):
        location = Location()
        self.assertEqual((location.fid, location.title), (ROOT_FID, "/"))
        location.enter("child", "中文 目录")
        self.assertEqual((location.fid, location.title), ("child", "/中文 目录"))
        location.enter("deep", "子目录")
        self.assertEqual(location.title, "/中文 目录/子目录")
        self.assertTrue(location.up())
        self.assertEqual((location.fid, location.title), ("child", "/中文 目录"))
        self.assertTrue(location.up())
        self.assertFalse(location.up())
        self.assertEqual((location.fid, location.title), (ROOT_FID, "/"))

    def test_reset_drops_every_entered_folder(self):
        location = Location()
        location.enter("child", "子")
        location.reset()
        self.assertEqual((location.fid, location.title), (ROOT_FID, "/"))

    def test_enter_rejects_a_malformed_fid(self):
        with self.assertRaises(RemoteError):
            Location().enter("../escape", "坏")


class PlanTests(unittest.TestCase):
    def test_output_folder_must_be_chosen_and_exist(self):
        with tempfile.TemporaryDirectory() as folder:
            file_entry = entry_of(_record("f", "报告.PDF", size=4))
            with self.assertRaises(LocalIOError):
                download_plan("", file_entry)
            with self.assertRaises(LocalIOError):
                download_plan("   ", file_entry)
            with self.assertRaises(LocalIOError):
                download_plan(str(Path(folder) / "missing"), file_entry)

    def test_a_file_keeps_its_remote_name_and_a_folder_downloads_recursively(self):
        with tempfile.TemporaryDirectory() as folder:
            file_entry = entry_of(_record("f", "报告.PDF", size=4))
            file_plan = download_plan(folder, file_entry)
            self.assertEqual(file_plan.target, Path(folder) / "报告.PDF")
            self.assertFalse(file_plan.recursive)

            directory = entry_of(_record("d", "中文 目录", directory=True))
            folder_plan = download_plan(folder, directory)
            self.assertEqual(folder_plan.target, Path(folder) / "中文 目录")
            self.assertTrue(folder_plan.recursive)

    def test_an_unsafe_remote_name_fails_before_any_request(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ("a<b.bin", "trailing.", "trailing ", "CON"):
                with self.subTest(name=name), self.assertRaises(UsageError):
                    download_plan(folder, entry_of(_record("f", name, size=4)))


class ProgressTests(unittest.TestCase):
    def test_known_size_renders_bytes_and_rate(self):
        event = ProgressEvent("download", "transferring", "报告.bin", 512, 512, 1024, 1048576.0)
        self.assertEqual(progress_text(event), "报告.bin：512 B / 1.0 KiB · 1.0 MiB/s")
        self.assertEqual(progress_fraction(event), 0.5)

    def test_unknown_size_renders_transferred_bytes_only(self):
        event = ProgressEvent("download", "transferring", "报告.bin", 3, 3, None, 1048576.0)
        self.assertEqual(progress_text(event), "报告.bin：已传输 3 B · 1.0 MiB/s")
        self.assertIsNone(progress_fraction(event))

    def test_fraction_is_bounded_and_refuses_malformed_sizes(self):
        cases = [
            (ProgressEvent("download", "p", "n", 2048, 2048, 1024, 0.0), 1.0),
            (ProgressEvent("download", "p", "n", 0, 0, 0, 0.0), None),
            (ProgressEvent("download", "p", "n", 0, 0, True, 0.0), None),
            (ProgressEvent("download", "p", "n", -1, 0, 10, 0.0), None),
        ]
        for event, expected in cases:
            with self.subTest(total=event.total):
                self.assertEqual(progress_fraction(event), expected)

    def test_progress_text_carries_no_link_or_credential(self):
        event = ProgressEvent("download", "transferring", "报告.bin", 1, 1, 2, 0.0)
        text = progress_text(event)
        self.assertNotIn("http", text)
        self.assertNotIn("=", text)


class SummaryTests(unittest.TestCase):
    def test_single_file_and_directory_completion_lines(self):
        self.assertEqual(summary_text({"path": "C:/out/f.bin", "size": 10}, "f.bin"), "已下载 f.bin：10 B")
        self.assertEqual(
            summary_text({"recursive": True, "path": "C:/out/目录", "files": 2, "directories": 3, "size": 2048}, "目录"),
            "已下载目录 C:/out/目录：2 个文件，3 个目录，2.0 KiB",
        )

    def test_missing_counts_are_unknown_not_zero(self):
        line = summary_text({"recursive": True, "path": "C:/out", "files": "2", "size": None}, "x")
        self.assertEqual(line, "已下载目录 C:/out：? 个文件，? 个目录，—")


class ErrorTests(unittest.TestCase):
    def test_library_messages_are_reported_verbatim(self):
        self.assertEqual(describe_error(UsageError("目标文件已存在；使用 --overwrite 显式覆盖"), "下载"),
                         "下载失败：目标文件已存在；使用 --overwrite 显式覆盖")

    def test_unexpected_and_local_failures_never_echo_raw_text(self):
        self.assertEqual(describe_error(RuntimeError("boom /tmp/secret"), "下载"), "下载失败：发生未预期的错误")
        self.assertEqual(describe_error(OSError("C:/private/path"), "下载"), "下载失败：本地文件操作失败")

    def test_empty_quark_message_still_names_the_action(self):
        self.assertEqual(describe_error(RemoteError(""), "浏览"), "浏览失败")
        self.assertEqual(describe_error(LocalIOError(""), "下载"), "下载失败")


if __name__ == "__main__":
    unittest.main()
