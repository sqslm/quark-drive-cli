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
    ROOT_NAME,
    Batch,
    Entry,
    Location,
    Outcome,
    batch_summary,
    describe_error,
    display_text,
    download_plan,
    entries,
    entry_of,
    filter_entries,
    item_status,
    output_folder,
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
    def test_modified_time_uses_only_valid_server_milliseconds(self):
        record = _record("f", "报告.txt")
        for value in (1791952440000, 1791952440000.5):
            with self.subTest(value=value):
                self.assertEqual(entry_of({**record, "updated_at": value}).modified_at, value)
        for value in (None, 0, -1, True, "1791952440000", float("nan"), float("inf"), 10**400):
            with self.subTest(value=value):
                self.assertIsNone(entry_of({**record, "updated_at": value, "created_at": 1791952440000}).modified_at)
        self.assertIsNone(entry_of(record).modified_at)
        self.assertEqual(entry_of({**_record("d", "目录", directory=True), "updated_at": 1791952440000}).modified_at, 1791952440000)

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

    def test_a_plain_listing_row_carries_no_search_location(self):
        entry = entry_of(_record(7, "报告.PDF", size=4))
        self.assertEqual((entry.path, entry.location), ("", ()))


class FilterTests(unittest.TestCase):
    @staticmethod
    def _rows() -> list[Entry]:
        return [
            entry_of(_record("d1", "报告 目录", directory=True)),
            entry_of(_record("f1", "年度报告.txt", size=1)),
            entry_of(_record("f2", "Report.md", size=2)),
            entry_of(_record("f3", "其他.bin", size=3)),
        ]

    def test_query_is_stripped_and_matched_case_insensitively(self):
        rows = self._rows()
        self.assertEqual([entry.fid for entry in filter_entries(rows, "  报告  ")], ["d1", "f1"])
        self.assertEqual([entry.fid for entry in filter_entries(rows, "report")], ["f2"])

    def test_kind_narrows_the_same_rows_without_reordering_them(self):
        rows = self._rows()
        self.assertEqual([entry.fid for entry in filter_entries(rows, "报告", "directories")], ["d1"])
        self.assertEqual([entry.fid for entry in filter_entries(rows, "报告", "files")], ["f1"])
        self.assertEqual([entry.fid for entry in filter_entries(rows, "", "directories")], ["d1"])
        self.assertEqual([entry.fid for entry in filter_entries(rows)], ["d1", "f1", "f2", "f3"])

    def test_the_input_order_survives_even_when_it_is_not_the_listing_order(self):
        rows = [
            entry_of(_record("f1", "报告.txt", size=1)),
            entry_of(_record("d1", "报告 目录", directory=True)),
        ]
        self.assertEqual([entry.fid for entry in filter_entries(rows, "报告")], ["f1", "d1"])

    def test_an_unknown_kind_is_a_usage_error(self):
        with self.assertRaises(UsageError):
            filter_entries(self._rows(), "报告", "folders")


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

    def test_replace_restores_a_full_chain_and_navigates_from_it(self):
        location = Location()
        location.enter("stale", "旧")
        location.replace(((ROOT_FID, ROOT_NAME), ("a", "下载"), ("b", "子目录")))
        self.assertEqual((location.fid, location.title), ("b", "/下载/子目录"))
        self.assertTrue(location.up())
        self.assertEqual((location.fid, location.title), ("a", "/下载"))
        location.replace(((ROOT_FID, ROOT_NAME),))
        self.assertEqual((location.fid, location.title), (ROOT_FID, "/"))
        self.assertFalse(location.up())

    def test_replace_refuses_a_chain_that_is_not_rooted_or_is_malformed(self):
        location = Location()
        location.enter("child", "子")
        for chain in (
            (),
            (("a", "下载"),),
            ((ROOT_FID, "别的根"),),
            ((ROOT_FID, ROOT_NAME), (ROOT_FID, "重复根")),
            ((ROOT_FID, ROOT_NAME), ("b", "")),
            ((ROOT_FID, ROOT_NAME), "not-a-pair"),
        ):
            with self.subTest(chain=chain), self.assertRaises(UsageError):
                location.replace(chain)
        with self.assertRaises(RemoteError):
            location.replace(((ROOT_FID, ROOT_NAME), ("../escape", "坏")))
        self.assertEqual((location.fid, location.title), ("child", "/子"), "a refused chain must not move the window")


class PlanTests(unittest.TestCase):
    def test_the_output_folder_is_validated_once_for_the_whole_click(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(output_folder(folder), Path(folder))
            for output in ("", "   ", str(Path(folder) / "missing"), str(Path(folder) / "f.bin")):
                with self.subTest(output=output), self.assertRaises(LocalIOError):
                    output_folder(output)

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


class BatchTests(unittest.TestCase):
    @staticmethod
    def _entries(*names: str) -> list[Entry]:
        return [entry_of(_record(str(index), name, size=4)) for index, name in enumerate(names)]

    def test_entries_are_served_in_list_order_one_at_a_time(self):
        batch = Batch("C:/out", self._entries("b.bin", "a.bin", "c.bin"))
        self.assertEqual(batch.total, 3)
        self.assertEqual((batch.position, batch.skipped, batch.succeeded, batch.failed), (1, 0, 0, 0))

        self.assertEqual(batch.take().name, "b.bin")
        self.assertIsNone(batch.take(), "a second item must not overtake the one in flight")
        self.assertEqual(batch.position, 1)
        batch.settle(Outcome.DONE)
        self.assertEqual((batch.position, batch.skipped, batch.succeeded), (2, 0, 1))

        self.assertEqual(batch.take().name, "a.bin")
        batch.settle(Outcome.FAILED)
        self.assertEqual((batch.position, batch.failed, batch.skipped), (3, 1, 0))

        self.assertEqual(batch.take().name, "c.bin")
        batch.settle(Outcome.DONE)
        self.assertEqual((batch.succeeded, batch.failed, batch.skipped), (2, 1, 0))
        self.assertIsNone(batch.take())

    def test_a_stopped_item_is_counted_as_neither_a_success_nor_a_failure(self):
        batch = Batch("C:/out", self._entries("one.bin", "two.bin"))
        batch.take()
        batch.settle(Outcome.STOPPED)
        self.assertEqual((batch.succeeded, batch.failed, batch.stopped), (0, 0, 1))
        # The stopped item keeps its place in the click, so the next one is 2/2.
        self.assertEqual((batch.position, batch.take().name), (2, "two.bin"))

    def test_clear_drops_only_what_has_not_started_and_keeps_the_count(self):
        batch = Batch("C:/out", self._entries("one.bin", "two.bin", "three.bin"))
        self.assertEqual(batch.take().name, "one.bin")
        self.assertEqual(batch.clear(), 2)
        self.assertEqual(batch.skipped, 2)
        self.assertEqual(batch.current.name, "one.bin")
        self.assertIsNone(batch.take())
        self.assertEqual(batch.clear(), 0, "clearing twice must not double-count")
        batch.settle(Outcome.FAILED)
        self.assertEqual((batch.succeeded, batch.failed, batch.skipped), (0, 1, 2))

    def test_settling_without_an_item_in_flight_counts_nothing(self):
        batch = Batch("C:/out", self._entries("one.bin"))
        batch.settle(Outcome.DONE)
        self.assertEqual((batch.succeeded, batch.failed, batch.stopped, batch.skipped, batch.position), (0, 0, 0, 0, 1))

    def test_summary_lines_report_totals_and_what_a_stop_left_untouched(self):
        self.assertEqual(
            batch_summary(succeeded=3, failed=0, stopped=0, skipped=0), "全部下载完成：共 3 项"
        )
        self.assertEqual(
            batch_summary(succeeded=1, failed=2, stopped=0, skipped=0),
            "下载结束：共 3 项，成功 1 项，失败 2 项",
        )
        self.assertEqual(
            batch_summary(succeeded=1, failed=1, stopped=1, skipped=2),
            "已停止：成功 1 项，失败 1 项，1 项已停止（检查点已保留），2 项未开始",
        )
        # Only the last item was in flight, so only it was stopped.
        self.assertEqual(
            batch_summary(succeeded=2, failed=0, stopped=1, skipped=0),
            "已停止：成功 2 项，1 项已停止（检查点已保留）",
        )
        self.assertEqual(
            batch_summary(succeeded=0, failed=0, stopped=1, skipped=1, resume_available=False),
            "已停止：成功 0 项，1 项已停止（需重新下载），1 项未开始",
        )
        # A stop that dropped nothing and stopped nothing is the completion it
        # turned out to be; a click that failed is never reported as completed.
        self.assertEqual(
            batch_summary(succeeded=2, failed=0, stopped=0, skipped=0), "全部下载完成：共 2 项"
        )
        self.assertEqual(
            batch_summary(succeeded=0, failed=1, stopped=0, skipped=0), "下载结束：共 1 项，成功 0 项，失败 1 项"
        )

    def test_item_status_names_the_place_in_the_click(self):
        self.assertEqual(item_status(2, 5, "报告.bin：1 B / 2 B"), "（2/5）报告.bin：1 B / 2 B")


class ProgressTests(unittest.TestCase):
    def test_share_preparation_and_cleanup_show_the_current_operation(self):
        for phase, action in (("saving", "正在临时转存"), ("cleaning", "正在清理临时目录")):
            event = ProgressEvent("download", phase, "报告.bin", 0, 0, None, 0.0)
            self.assertEqual(progress_text(event), f"报告.bin：{action}")

    def test_known_size_renders_bytes_and_rate(self):
        event = ProgressEvent("download", "transferring", "报告.bin", 512, 512, 1024, 1048576.0)
        self.assertEqual(progress_text(event), "报告.bin：512 B / 1.0 KiB · 1.0 MiB/s · 单线程")
        self.assertEqual(progress_fraction(event), 0.5)

    def test_unknown_size_renders_transferred_bytes_only(self):
        event = ProgressEvent("download", "transferring", "报告.bin", 3, 3, None, 1048576.0)
        self.assertEqual(progress_text(event), "报告.bin：已传输 3 B · 1.0 MiB/s · 单线程")
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
