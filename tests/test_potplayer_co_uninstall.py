"""Isolated registry and fake native-uninstaller tests; never run PotPlayer uninstall."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'packaging/potplayer'))
import install_core as core
import player_uninstall as linked


class Registry:
    HKEY_LOCAL_MACHINE='machine'
    HKEY_CURRENT_USER='user'
    KEY_READ=1
    KEY_SET_VALUE=2
    KEY_WOW64_64KEY=256
    KEY_WOW64_32KEY=512
    REG_SZ=1
    def __init__(self):
        self.items={}
        self.denied=False
    @contextmanager
    def OpenKey(self,hive,key,zero=0,access=1):
        if (hive,key,access&768) not in self.items:raise FileNotFoundError()
        if self.denied and access&self.KEY_SET_VALUE:raise PermissionError()
        yield self.items[hive,key,access&768]
    def QueryValueEx(self,handle,name):
        if name not in handle:raise FileNotFoundError()
        return handle[name]
    def SetValueEx(self,handle,name,zero,kind,value):handle[name]=(value,kind)
    def DeleteValue(self,handle,name):
        if name not in handle:raise FileNotFoundError()
        del handle[name]


class CoUninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='quark-linked-test-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.player=self.root/'Player with spaces';self.player.mkdir()
        (self.player/'Extension').mkdir()
        (self.player/'PotPlayerMini64.exe').write_bytes(b'fixture')
        (self.player/'uninstall.exe').write_bytes(b'MZ\0\xef\xbe\xad\xdeNullsoftInst fixture')
        self.install=self.player/'Plugin/QuarkPotPlayer';self.install.mkdir(parents=True)
        self.reg=Registry();self.patch=patch.object(linked,'winreg',self.reg);self.patch.start();self.addCleanup(self.patch.stop)
        self.values={'UninstallString':(str(self.player/'uninstall.exe'),1)}
        self.reg.items['machine',linked.PREFIX+'PotPlayer64',256]=self.values
        self.data=core.plugin_credential_dir(self.root/'config');self.data.mkdir(parents=True)
        (self.data/'accounts.json').write_text('private fixture')
        self.env=patch.dict(__import__('os').environ,{'QUARK_CONFIG_DIR':str(self.root/'config')});self.env.start();self.addCleanup(self.env.stop)

    def metadata(self):
        meta=linked.prepare(self.player)
        meta['command']=linked.command(self.install)
        return meta

    def manifest(self):
        value={'schema':1,'version':'test','install_dir':str(self.install),'potplayer_dir':str(self.player),
               'created':[],'as_files':[],'launcher_files':[],'credential_dir':str(self.data),
               'player_uninstall':self.metadata()}
        (self.install/'install.json').write_text(json.dumps(value),'utf-8')
        return value

    def test_link_upgrade_and_restore_preserve_original_commands(self):
        meta=self.metadata();original=self.values['UninstallString']
        linked.attach(meta,self.install)
        self.assertIn('--uninstall-player',self.values['UninstallString'][0])
        upgraded=linked.prepare(self.player,meta)
        self.assertEqual(upgraded['original'],original[0])
        linked.restore(meta)
        self.assertEqual(self.values,{'UninstallString':original})

    def test_restore_does_not_overwrite_a_later_user_change(self):
        meta=self.metadata();linked.attach(meta,self.install)
        self.values['UninstallString']=('user changed command',1)
        linked.restore(meta)
        self.assertEqual(self.values['UninstallString'][0],'user changed command')

    def test_restore_preserves_an_independently_changed_quiet_command(self):
        meta=self.metadata();linked.attach(meta,self.install)
        self.values['QuietUninstallString']=('user quiet change',1)
        linked.restore(meta)
        self.assertEqual(self.values['QuietUninstallString'][0],'user quiet change')
        self.assertEqual(self.values['UninstallString'][0],meta['original'])

    def test_missing_or_other_install_is_not_linked(self):
        self.values['UninstallString']=(r'C:\OtherPlayer\uninstall.exe',1)
        self.assertIsNone(linked.prepare(self.player))

    def test_permission_failure_happens_before_install_changes(self):
        self.reg.denied=True
        with self.assertRaises(PermissionError):linked.prepare(self.player)
        self.assertNotIn('QuietUninstallString',self.values)

    def test_native_cancel_keeps_plugin_and_all_account_data(self):
        self.manifest()
        with patch.object(core,'stop_service'),patch.object(linked,'close_plugin_windows'),patch.object(
            linked.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as native:
            self.assertEqual(linked.run(self.install,registry=False),0)
        self.assertTrue((self.data/'accounts.json').exists())
        self.assertTrue((self.install/'install.json').exists())
        self.assertIn(' _?='+str(self.player),native.call_args.args[0])

    def test_completed_native_uninstall_cleans_external_data_without_recreating_player(self):
        self.manifest()
        def native(*args,**kwargs):
            shutil.rmtree(self.player)
            return SimpleNamespace(returncode=0)
        with patch.object(core,'stop_service'),patch.object(linked,'close_plugin_windows'),patch.object(
            linked.subprocess,'run',side_effect=native):
            self.assertEqual(linked.run(self.install,registry=False),0)
        self.assertFalse(self.data.exists())
        self.assertFalse(self.player.exists())

    def test_unrelated_configuration_is_never_removed(self):
        value=self.manifest();value['credential_dir']=str(self.root/'unrelated/potplayer')
        notes=core.UninstallReport()
        core.cleanup_external(value,notes,clear_credentials=True,registry=False)
        self.assertTrue(self.data.exists())
        self.assertTrue(notes.notes)

    def test_shortcuts_are_disabled_even_when_an_old_caller_requests_one(self):
        from test_potplayer_installer import make_payload,make_as
        payload=make_payload(self.root)
        with patch.object(core,'stop_service'):
            core.install(payload_dir=payload,install_dir=self.install,potplayer_dir=self.player,
                         version='test',as_files=make_as(self.root),make_shortcut=True,
                         start_menu_dir=self.root/'menu',registry=False)
        self.assertFalse((self.root/'menu').exists())
