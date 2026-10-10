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
from quark_cli.potplayer.library_state import RECENT_LIMIT, Library
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
        write_json(self.path, {"version": 2, "favorites": [], "recent": []})
        with self.assertRaises(LocalIOError):
            self.library().favorites()


if __name__ == "__main__":
    unittest.main()
