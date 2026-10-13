import tempfile
import unittest
from pathlib import Path

from quark_cli.desktop.favorites import Favorites
from quark_cli.errors import LocalIOError, UsageError
from tests import test_desktop_cut
from tests.test_desktop_files import Drive
from tests.test_downloader_worker import item, COOKIE


class FavoriteTests(unittest.TestCase):
    service = test_desktop_cut.CutTests.service
    call = test_desktop_cut.CutTests.call

    def test_add_remove_are_local_persistent_and_account_bound(self):
        drive = Drive([item("folder", "资料", directory=True)])
        service = self.service(drive)
        trail = [["0", "我的网盘"], ["folder", "资料"]]
        rows = self.call(service, "favorites.add", {"trail":trail})
        self.assertEqual(rows, [{"fid":"folder", "name":"资料"}])
        self.assertEqual(drive.requests, [])
        self.assertEqual(Favorites(service.favorites.path).list(service.context()), rows)
        service.saved_cookie = "sid=other"
        self.assertEqual(self.call(service, "favorites.list", {}), [])
        with self.assertRaises(UsageError):
            self.call(service, "favorites.open", {"fid":"folder"})
        self.assertEqual(drive.requests, [])
        service.saved_cookie = COOKIE
        self.assertEqual(self.call(service, "favorites.remove", {"fid":"folder"}), [])
        self.assertEqual(drive.requests, [])

    def test_open_relocates_renamed_moved_folder_and_checks_ownership(self):
        drive = Drive([item("folder", "资料", directory=True), item("parent", "新位置", directory=True)])
        service = self.service(drive)
        self.call(service, "favorites.add", {"trail":[["0","我的网盘"],["folder","资料"]]})
        drive.records['folder'].update(file_name='新名称',pdir_fid='parent')
        chain = self.call(service, "favorites.open", {"fid":"folder"})
        self.assertEqual(chain, [["0","我的网盘"],["parent","新位置"],["folder","新名称"]])
        self.assertEqual(self.call(service,"favorites.list",{}),[{"fid":"folder","name":"新名称"}])
        self.assertEqual(drive.writes, [])
        drive.records['folder']['file_type'] = 1
        drive.records['folder']['dir'] = False
        with self.assertRaises(UsageError):
            self.call(service,"favorites.open",{"fid":"folder"})

    def test_share_invalid_chain_and_corrupt_file_do_not_overwrite(self):
        service = self.service(Drive([]))
        with self.assertRaises(UsageError):
            self.call(service,"favorites.add",{"handle":"share","trail":[["0","我的网盘"],["a","A"]]})
        with self.assertRaises(UsageError):
            self.call(service,"favorites.add",{"trail":[["0","我的网盘"],["0","A"]]})
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'favorites.json'
            path.write_text('{"version":99}',encoding='utf-8')
            with self.assertRaises(LocalIOError):
                Favorites(path).add('account',[["0","我的网盘"],["a","A"]])
            self.assertEqual(path.read_text(encoding='utf-8'),'{"version":99}')
