"""Unit tests for the bridge's private favorites/recent document."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from quark_cli.errors import LocalIOError
from quark_cli.potplayer.library_state import RECENT_LIMIT, Library, completed, unfinished
from quark_cli.state import write_json


class LibraryCase(unittest.TestCase):
    """One private document per test, written through the real atomic path."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="quark-library-"))
        self.path = self.directory / "library.json"
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def library(self) -> Library:
        return Library(self.path)

    # -- contract shape --------------------------------------------------

    def test_public_views_carry_the_bridge_contract(self):
        library = self.library()
        self.assertEqual(library.favorites(), [])
        self.assertEqual(library.recent(), [])
        self.assertEqual(
            library.add_favorite("dir-a", "甲"),
            [{"fid": "dir-a", "name": "甲", "is_dir": True, "size": 0}],
        )
        recorded = library.record_recent("v1", "第1集.mp4", 123)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(set(recorded[0]), {"fid", "name", "is_dir", "size", "played_at"})
        self.assertEqual(recorded[0]["fid"], "v1")
        self.assertEqual(recorded[0]["name"], "第1集.mp4")
        self.assertEqual(recorded[0]["is_dir"], False)
        self.assertEqual(recorded[0]["size"], 123)

    def test_played_at_is_a_plausible_unix_second(self):
        before = int(time.time())
        item = self.library().record_recent("v1", "a.mp4", 1)[0]
        after = int(time.time())
        self.assertLessEqual(before, item["played_at"])
        self.assertLessEqual(item["played_at"], after)

    # -- favorites -------------------------------------------------------

    def test_favorites_keep_newest_first_and_deduplicate(self):
        library = self.library()
        library.add_favorite("dir-a", "甲")
        items = library.add_favorite("dir-b", "乙")
        self.assertEqual([item["fid"] for item in items], ["dir-b", "dir-a"])
        again = library.add_favorite("dir-a", "甲")
        self.assertEqual([item["fid"] for item in again], ["dir-b", "dir-a"])
        self.assertEqual(len(again), 2)

    def test_remove_is_idempotent_and_state_survives_a_reload(self):
        library = self.library()
        library.add_favorite("dir-a", "甲")
        self.assertEqual(library.remove_favorite("dir-a"), [])
        self.assertEqual(library.remove_favorite("dir-a"), [])
        self.assertEqual(library.remove_favorite("dir-absent"), [])
        library.add_favorite("dir-a", "甲")
        self.assertEqual([item["fid"] for item in Library(self.path).favorites()], ["dir-a"])

    def test_recent_survives_a_reload_and_deduplicates(self):
        library = self.library()
        library.record_recent("v1", "a.mp4", 1)
        library.record_recent("v2", "b.mp4", 2)
        self.assertEqual([item["fid"] for item in Library(self.path).recent()], ["v2", "v1"])

    # -- recent ordering and cap -----------------------------------------

    def test_recent_moves_a_repeat_to_the_front(self):
        library = self.library()
        library.record_recent("v1", "a.mp4", 1)
        library.record_recent("v2", "b.mp4", 2)
        self.assertEqual([item["fid"] for item in library.recent()], ["v2", "v1"])
        library.record_recent("v1", "a.mp4", 1)
        self.assertEqual([item["fid"] for item in library.recent()], ["v1", "v2"])

    def test_recent_is_capped_at_one_hundred_without_duplicates(self):
        library = self.library()
        for index in range(RECENT_LIMIT + 20):
            library.record_recent(f"f{index}", f"{index}.mp4", index)
        items = library.recent()
        self.assertEqual(len(items), RECENT_LIMIT)
        self.assertEqual(items[0]["fid"], f"f{RECENT_LIMIT + 19}")
        self.assertEqual(len({item["fid"] for item in items}), RECENT_LIMIT)
        self.assertNotIn("f0", [item["fid"] for item in items])

    def test_clear_recent_keeps_favorites(self):
        library = self.library()
        library.add_favorite("dir-a", "甲")
        library.record_recent("v1", "a.mp4", 1)
        library.clear_recent()
        self.assertEqual(library.recent(), [])
        self.assertEqual([item["fid"] for item in library.favorites()], ["dir-a"])

    def test_clear_drops_both_lists_and_allows_fresh_use(self):
        library = self.library()
        library.add_favorite("dir-a", "甲")
        library.record_recent("v1", "a.mp4", 1)
        library.clear()
        self.assertEqual(library.favorites(), [])
        self.assertEqual(library.recent(), [])
        self.assertFalse(self.path.exists())
        library.clear()
        library.add_favorite("dir-b", "乙")
        self.assertEqual([item["fid"] for item in library.favorites()], ["dir-b"])

    # -- concurrency -----------------------------------------------------

    def test_concurrent_writers_do_not_lose_records(self):
        library = self.library()
        count = 20
        barrier = threading.Barrier(count)

        def worker(index: int) -> None:
            barrier.wait()
            library.add_favorite(f"dir-{index}", f"目录{index}")
            library.record_recent(f"v{index}", f"{index}.mp4", index)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual({item["fid"] for item in library.favorites()}, {f"dir-{index}" for index in range(count)})
        self.assertEqual({item["fid"] for item in library.recent()}, {f"v{index}" for index in range(count)})

    # -- progress (schema v2) --------------------------------------------

    def test_v1_document_reads_without_rewrite_and_upgrades_on_next_write(self):
        legacy = {
            "version": 1,
            "favorites": [{"fid": "dir-a", "name": "甲", "added_at": 7}],
            "recent": [{"fid": "v1", "name": "a.mp4", "size": 5, "played_at": 9}],
        }
        write_json(self.path, legacy)
        before = self.path.read_text(encoding="utf-8")
        library = self.library()
        self.assertEqual([item["fid"] for item in library.favorites()], ["dir-a"])
        self.assertEqual([item["fid"] for item in library.recent()], ["v1"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(library.progress("v1", 5), None)
        library.add_favorite("dir-b", "乙")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 3)
        self.assertEqual(stored["marks"], [])
        self.assertEqual([entry["fid"] for entry in stored["favorites"]], ["dir-b", "dir-a"])
        self.assertEqual([entry["fid"] for entry in stored["recent"]], ["v1"])
        self.assertEqual(stored["recent"][0]["position_ms"], 0)
        self.assertEqual(stored["recent"][0]["duration_ms"], 0)
        self.assertEqual(stored["recent"][0]["progress_at"], 0)

    def test_progress_round_trip_keeps_public_view_unchanged(self):
        library = self.library()
        library.record_recent("v1", "a.mp4", 5)
        self.assertEqual(library.progress("v1", 5), None)
        self.assertTrue(library.save_progress("v1", 5, 60_000, 1_800_000))
        self.assertEqual(library.progress("v1", 5)["position_ms"], 60_000)
        self.assertEqual(library.progress("v1", 5)["duration_ms"], 1_800_000)
        self.assertEqual(set(library.recent()[0]), {"fid", "name", "is_dir", "size", "played_at"})
        self.assertTrue(library.reset_progress("v1"))
        self.assertIsNone(library.progress("v1", 5))
        self.assertFalse(library.reset_progress("v-absent"))

    def test_save_progress_never_creates_or_reorders(self):
        library = self.library()
        library.record_recent("v1", "a.mp4", 5)
        library.record_recent("v2", "b.mp4", 6)
        self.assertFalse(library.save_progress("v-absent", 1, 60_000, 90_000))
        self.assertFalse(library.save_progress("v2", 7, 60_000, 90_000))
        self.assertTrue(library.save_progress("v1", 5, 30_000, 90_000))
        self.assertEqual([item["fid"] for item in library.recent()], ["v2", "v1"])
        self.assertIsNone(library.progress("v2", 6))
        self.assertEqual(library.progress("v1", 5)["position_ms"], 30_000)

    def test_record_recent_preserves_progress_for_same_size_and_resets_on_change(self):
        library = self.library()
        library.record_recent("v1", "a.mp4", 5)
        library.save_progress("v1", 5, 45_000, 90_000)
        library.record_recent("v1", "a.mp4", 5)
        self.assertEqual(library.progress("v1", 5)["position_ms"], 45_000)
        library.record_recent("v1", "a.mp4", 6)
        self.assertIsNone(library.progress("v1", 5))
        self.assertIsNone(library.progress("v1", 6))

    def test_clear_recent_and_clear_drop_progress(self):
        library = self.library()
        library.record_recent("v1", "a.mp4", 5)
        library.save_progress("v1", 5, 45_000, 90_000)
        library.clear_recent()
        self.assertIsNone(library.progress("v1", 5))
        library.record_recent("v1", "a.mp4", 5)
        library.save_progress("v1", 5, 45_000, 90_000)
        library.clear()
        self.assertIsNone(library.progress("v1", 5))

    def test_bad_progress_field_reports_clearly_and_keeps_the_file(self):
        document = {
            "version": 2,
            "favorites": [],
            "recent": [{
                "fid": "v1", "name": "a.mp4", "size": 5, "played_at": 9,
                "position_ms": -1, "duration_ms": 90_000, "progress_at": 0,
            }],
        }
        write_json(self.path, document)
        calls = (self.library().recent, lambda: self.library().progress("v1", 5))
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(LocalIOError):
                    call()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), document)

    # -- watch marks and completion (schema v3) --------------------------

    def test_v2_document_reads_without_rewrite_and_upgrades_on_next_write(self):
        progress = {
            "version": 2,
            "favorites": [{"fid": "dir-a", "name": "甲", "added_at": 7}],
            "recent": [{
                "fid": "v1", "name": "a.mp4", "size": 5, "played_at": 9,
                "position_ms": 60_000, "duration_ms": 1_800_000, "progress_at": 11,
            }],
        }
        write_json(self.path, progress)
        before = self.path.read_text(encoding="utf-8")
        library = self.library()
        self.assertEqual([item["fid"] for item in library.recent()], ["v1"])
        self.assertEqual(library.progress("v1", 5)["position_ms"], 60_000)
        self.assertFalse(library.is_watched("v1", 5))
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        # The first real write upgrades to v3: parentage defaults to unknown and
        # the manual marks list starts empty.
        library.record_recent("v2", "b.mp4", 6)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 3)
        self.assertEqual(stored["marks"], [])
        self.assertEqual(stored["recent"][0]["parent"], "")
        self.assertEqual(stored["recent"][1]["position_ms"], 60_000)

    def test_unfinished_and_completed_share_one_end_rule(self):
        self.assertFalse(unfinished(14_999, 100_000))
        self.assertFalse(completed(14_999, 100_000))
        self.assertTrue(unfinished(15_000, 100_000))
        self.assertFalse(completed(15_000, 100_000))
        self.assertTrue(unfinished(97_999, 100_000))
        self.assertFalse(completed(97_999, 100_000))
        self.assertFalse(unfinished(98_000, 100_000))
        self.assertTrue(completed(98_000, 100_000))
        self.assertTrue(completed(200_000, 100_000))
        # A long title uses the 30 s cap, a short one its 2 %.
        self.assertTrue(unfinished(3_569_999, 3_600_000))
        self.assertTrue(completed(3_570_000, 3_600_000))
        # No false completion on an unplayed or unknown-duration record.
        self.assertFalse(completed(0, 1_800_000))
        self.assertFalse(completed(100, 0))
        self.assertFalse(completed(0, 0))

    def test_marks_are_keyed_by_fid_and_size(self):
        library = self.library()
        library.record_recent("v1", "a.mp4", 5)
        library.mark_watched("v1", "a.mp4", 5)
        self.assertTrue(library.is_watched("v1", 5))
        self.assertFalse(library.is_watched("v1", 6))
        self.assertFalse(library.is_watched("other", 5))

    def test_mark_watched_is_idempotent_and_replaces_a_stale_size(self):
        library = self.library()
        library.mark_watched("v1", "a.mp4", 5)
        first = self.path.read_text(encoding="utf-8")
        library.mark_watched("v1", "a.mp4", 5)
        self.assertEqual(self.path.read_text(encoding="utf-8"), first)
        self.assertEqual(len(library.snapshot()["marks"]), 1)
        # The same FID at a new size is a new object: the old mark is replaced.
        library.mark_watched("v1", "a.mp4", 6)
        marks = library.snapshot()["marks"]
        self.assertEqual([(mark["fid"], mark["size"]) for mark in marks], [("v1", 6)])
        self.assertFalse(library.is_watched("v1", 5))
        self.assertTrue(library.is_watched("v1", 6))

    def test_a_different_size_play_never_inherits_watched_or_progress(self):
        library = self.library()
        library.record_recent("v1", "a.mp4", 5)
        library.save_progress("v1", 5, 45_000, 90_000)
        library.mark_watched("v1", "a.mp4", 5)
        # The FID is replaced by a same-named object with a different size.
        library.record_recent("v1", "a.mp4", 6)
        self.assertFalse(library.is_watched("v1", 5))
        self.assertFalse(library.is_watched("v1", 6))
        self.assertEqual(library.snapshot()["marks"], [])
        self.assertIsNone(library.progress("v1", 6))
        self.assertTrue(library.save_progress("v1", 6, 20_000, 90_000))

    def test_mark_suppresses_resume_until_unwatched_clears_it(self):
        library = self.library()
        library.record_recent("v1", "a.mp4", 5)
        library.save_progress("v1", 5, 45_000, 90_000)
        library.mark_watched("v1", "a.mp4", 5)
        self.assertIsNone(library.progress("v1", 5))
        self.assertFalse(library.save_progress("v1", 5, 50_000, 90_000))
        library.mark_unwatched("v1")
        self.assertFalse(library.is_watched("v1", 5))
        # The old saved point is gone; unwatching again with nothing left to
        # clear changes nothing.
        self.assertIsNone(library.progress("v1", 5))
        cleared = self.path.read_text(encoding="utf-8")
        library.mark_unwatched("v1")
        self.assertEqual(self.path.read_text(encoding="utf-8"), cleared)
        # A future real sample records a fresh resume point.
        self.assertTrue(library.save_progress("v1", 5, 55_000, 90_000))
        self.assertEqual(library.progress("v1", 5)["position_ms"], 55_000)

    def test_marks_survive_the_recent_cap_and_clear_recent(self):
        library = self.library()
        library.mark_watched("keep", "keep.mp4", 9)
        for index in range(RECENT_LIMIT + 20):
            library.record_recent(f"f{index}", f"{index}.mp4", index)
        self.assertTrue(library.is_watched("keep", 9))
        self.assertNotIn("keep", [item["fid"] for item in library.recent()])
        library.clear_recent()
        self.assertTrue(library.is_watched("keep", 9))
        library.clear()
        self.assertFalse(library.is_watched("keep", 9))

    # -- corruption ------------------------------------------------------

    def test_unreadable_document_reports_clearly_and_keeps_the_file(self):
        write_json(self.path, {"version": 1, "favorites": [], "recent": []})
        self.path.write_text("{not json", encoding="utf-8")
        library = self.library()
        calls = (
            library.favorites,
            library.recent,
            lambda: library.add_favorite("dir-a", "甲"),
            lambda: library.record_recent("v1", "a.mp4", 1),
            library.clear_recent,
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(LocalIOError):
                    call()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_structurally_wrong_document_reports_clearly_and_keeps_the_file(self):
        document = {"version": 1, "favorites": "甲", "recent": []}
        write_json(self.path, document)
        library = self.library()
        for call in (library.favorites, library.recent, lambda: library.add_favorite("dir-a", "甲")):
            with self.subTest(call=call):
                with self.assertRaises(LocalIOError):
                    call()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), document)

    def test_unknown_document_version_is_refused(self):
        write_json(self.path, {"version": 4, "favorites": [], "recent": [], "marks": []})
        with self.assertRaises(LocalIOError):
            self.library().favorites()


if __name__ == "__main__":
    unittest.main()
