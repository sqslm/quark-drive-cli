"""Versioned cloud edits must preserve originals and never replay uncertain writes."""
import codecs
import json
import tempfile
import unittest
from dataclasses import asdict
from unittest.mock import patch

import httpx

from quark_cli.client import QuarkClient
from quark_cli.desktop.service import DesktopService
from quark_cli.desktop.text_editor import TextEditor
from quark_cli.downloader.core import entry_of
from quark_cli.downloader.worker import DriveBackend
from quark_cli.errors import AuthenticationError, RateLimitError, RemoteError, UsageError
from tests.test_downloader_worker import COOKIE, item


class EditableDrive:
    def __init__(self, content=b'original', name='note.txt'):
        self.rows = {'original':item('original', name, 'folder', size=len(content), updated_at=123)}
        self.content = {'original':content}
        self.trash, self.writes = {}, []
        self.uploaded = []
        self.after_upload = lambda: None
        self.before_rename = lambda body: None
        self.fail_delete = False

    def transport(self):
        def handle(request):
            path = request.url.path
            if request.url.host == 'files.aliyuncs.com':
                return httpx.Response(200, content=self.content[request.url.params['fid']])
            if path.endswith('file/info'):
                data = self.rows.get(request.url.params['fid'])
                if data is None:
                    return httpx.Response(404, json={})
            elif path.endswith('file/sort'):
                rows = [dict(row) for row in self.rows.values() if row['pdir_fid']==request.url.params['pdir_fid']]
                data = {'list':rows, '_total':len(rows)}
            else:
                body = json.loads(request.content)
                if path.endswith('file/download'):
                    fid = body['fids'][0]
                    data = [{**self.rows[fid], 'download_url':f'https://files.aliyuncs.com/content?fid={fid}'}]
                elif path.endswith('file/rename'):
                    self.writes.append(('rename', dict(body)))
                    self.before_rename(body)
                    self.rows[body['fid']]['file_name'] = body['file_name']
                    data = {}
                elif path.endswith('file/delete'):
                    self.writes.append(('delete', dict(body)))
                    if self.fail_delete:
                        return httpx.Response(500, json={})
                    self.trash.update({fid:self.rows.pop(fid) for fid in body['filelist']})
                    data = {}
                else:
                    raise AssertionError(path)
            return httpx.Response(200, json={'status':200,'code':0,'data':data})
        return httpx.MockTransport(handle)

    def upload(self, source, parent, name):
        fid = 'new'+str(len(self.uploaded))
        data = source.read_bytes()
        self.uploaded.append(data)
        self.content[fid] = data
        self.rows[fid] = item(fid, name, parent, size=len(data), updated_at=456)
        self.after_upload()
        return {'fid':fid}


class EditorTests(unittest.TestCase):
    def opened(self, drive):
        client = QuarkClient(COOKIE, transport=drive.transport())
        self.addCleanup(client.close)
        editor = TextEditor()
        opened = editor.open(client, entry_of(drive.rows['original']), 'account')
        return editor, client, opened

    def save(self, editor, client, opened, drive, text='new content'):
        return editor.save(client, opened['editor'], opened['version'], text, 'account', upload=drive.upload)

    def test_verified_replacements_advance_fid_and_keep_each_old_version_in_trash(self):
        drive = EditableDrive()
        editor, client, opened = self.opened(drive)
        result = self.save(editor, client, opened, drive)
        self.assertFalse(result['blocked'])
        self.assertEqual(result['entry']['fid'], 'new0')
        self.assertEqual(drive.rows['new0']['file_name'], 'note.txt')
        self.assertEqual(drive.content['original'], b'original')
        self.assertIn('original', drive.trash)
        self.assertEqual(drive.writes[-1][1]['action_type'], 2)
        with self.assertRaisesRegex(UsageError, '版本'):
            self.save(editor, client, opened, drive)
        result2 = self.save(editor, client, {**opened,'version':result['version']}, drive, 'second revision')
        self.assertEqual(result2['entry']['fid'], 'new1')
        self.assertEqual(drive.content['new1'], b'second revision')
        self.assertEqual(set(drive.trash), {'original','new0'})
        self.assertEqual(len(drive.rows), 1)

    def test_encoding_bom_endian_and_windows_newlines_are_preserved(self):
        samples = [('utf-8',b''), ('utf-8',codecs.BOM_UTF8), ('utf-16-le',codecs.BOM_UTF16_LE),
                   ('utf-16-be',codecs.BOM_UTF16_BE), ('gb18030',b'')]
        for codec, bom in samples:
            with self.subTest(codec=codec, bom=bom):
                drive = EditableDrive(bom+'中文\r\n原文'.encode(codec))
                editor, client, opened = self.opened(drive)
                self.assertEqual(opened['text'], '中文\n原文')
                self.save(editor, client, opened, drive, '中文\n修改')
                self.assertEqual(drive.uploaded, [bom+'中文\r\n修改'.encode(codec)])

    def test_unchanged_document_needs_no_upload_or_cloud_mutation(self):
        drive = EditableDrive(b'original\r\ntext')
        editor, client, opened = self.opened(drive)
        result = self.save(editor, client, opened, drive, opened['text'])
        self.assertEqual(result['version'], opened['version'])
        self.assertEqual(drive.uploaded, [])
        self.assertEqual(drive.writes, [])

    def test_external_changes_before_and_during_upload_never_replace_original(self):
        for when in ('before','during'):
            with self.subTest(when=when):
                drive = EditableDrive()
                editor, client, opened = self.opened(drive)
                def change():
                    drive.content['original'] = b'external'
                    drive.rows['original']['size'] = 8
                if when=='before': change()
                else: drive.after_upload = change
                with self.assertRaises((UsageError, RemoteError)):
                    self.save(editor, client, opened, drive)
                self.assertEqual(drive.content['original'], b'external')
                self.assertEqual(drive.rows['original']['file_name'], 'note.txt')
                self.assertEqual(drive.writes, [])

    def test_move_rename_or_deleted_original_stops_without_upload(self):
        for change in ('parent','name','deleted'):
            with self.subTest(change=change):
                drive = EditableDrive()
                editor, client, opened = self.opened(drive)
                if change=='parent': drive.rows['original']['pdir_fid']='elsewhere'
                elif change=='name': drive.rows['original']['file_name']='changed.txt'
                else: drive.rows.pop('original')
                with self.assertRaises((UsageError, RemoteError)):
                    self.save(editor, client, opened, drive)
                self.assertEqual(drive.uploaded, [])

    def test_upload_restriction_keeps_cooldown_and_never_replays(self):
        drive=EditableDrive();editor,client,opened=self.opened(drive)
        def restricted(*args):
            raise RateLimitError('请求过于频繁',retry_after=60)
        with self.assertRaises(RateLimitError) as result:
            editor.save(client,opened['editor'],opened['version'],'new','account',upload=restricted)
        self.assertEqual(result.exception.retry_after,60)
        self.assertIn('上传新内容',str(result.exception))
        with self.assertRaisesRegex(UsageError,'禁止重发'):
            self.save(editor,client,opened,drive)
        self.assertEqual(drive.uploaded,[])

    def test_corrupt_staging_and_new_name_collision_stop_without_overwrite(self):
        for failure in ('bytes','collision','rename'):
            with self.subTest(failure=failure):
                drive = EditableDrive()
                editor, client, opened = self.opened(drive)
                def inject():
                    if failure=='bytes': drive.content['new0']=b'bad content'
                    elif failure=='collision': drive.rows['other']=item('other','note.txt','folder',size=1)
                drive.after_upload=inject
                if failure=='rename':
                    def fail(body):
                        raise httpx.ReadTimeout('secret-url-and-cookie')
                    drive.before_rename=fail
                with self.assertRaisesRegex(RemoteError, '停止自动重试') as error:
                    self.save(editor, client, opened, drive)
                self.assertNotIn('secret', str(error.exception))
                count = len(drive.uploaded), len(drive.writes)
                with self.assertRaisesRegex(UsageError, '禁止重发'):
                    self.save(editor, client, opened, drive)
                self.assertEqual((len(drive.uploaded), len(drive.writes)), count)
                self.assertEqual(drive.content['original'], b'original')
                self.assertFalse(drive.trash)

    def test_competing_name_appearing_after_backup_is_never_overwritten(self):
        drive = EditableDrive()
        editor, client, opened = self.opened(drive)
        def collision(body):
            drive.rows['other']=item('other','note.txt','folder',size=1)
        drive.before_rename=collision
        with self.assertRaises(RemoteError):
            self.save(editor, client, opened, drive)
        self.assertEqual(drive.rows['other']['file_name'], 'note.txt')
        self.assertTrue(drive.rows['original']['file_name'].startswith('.quark-edit-backup-'))
        self.assertEqual(len(drive.writes), 1)
        self.assertFalse(drive.trash)

    def test_lost_install_response_keeps_recoverable_backup_and_blocks_replay(self):
        drive = EditableDrive()
        editor, client, opened = self.opened(drive)
        def install(body):
            if body['fid']=='new0':
                drive.rows['new0']['file_name']=body['file_name']
                raise httpx.ReadTimeout('lost response')
        drive.before_rename=install
        with self.assertRaises(RemoteError): self.save(editor, client, opened, drive)
        self.assertEqual(drive.rows['new0']['file_name'], 'note.txt')
        self.assertTrue(drive.rows['original']['file_name'].startswith('.quark-edit-backup-'))
        self.assertFalse(drive.trash)
        with self.assertRaisesRegex(UsageError, '禁止重发'): self.save(editor, client, opened, drive)

    def test_recycle_failure_reports_saved_content_but_stops_future_autosaves(self):
        drive = EditableDrive();drive.fail_delete=True
        editor, client, opened = self.opened(drive)
        result = self.save(editor, client, opened, drive)
        self.assertTrue(result['blocked'])
        self.assertIn('已保存', result['warning'])
        self.assertEqual(drive.rows['new0']['file_name'], 'note.txt')
        self.assertEqual(drive.content['new0'], b'new content')
        with self.assertRaisesRegex(UsageError, '禁止重发'):
            self.save(editor, client, {**opened,'version':result['version']}, drive)

    def test_account_expiry_empty_and_oversized_edits_do_not_mutate(self):
        drive = EditableDrive();editor, client, opened = self.opened(drive)
        for text in ('','x'*2097153,'中'*1048576,'bad\x00control'):
            with self.assertRaises(UsageError): self.save(editor, client, opened, drive, text)
        with self.assertRaisesRegex(UsageError, '账号'):
            editor.save(client, opened['editor'], opened['version'], 'new', 'another', upload=drive.upload)
        editor.clear()
        with self.assertRaises(UsageError): self.save(editor, client, opened, drive)
        self.assertFalse(drive.writes)
        self.assertFalse(drive.uploaded)

    def test_desktop_dispatch_rejects_share_busy_account_change_and_logged_out(self):
        drive = EditableDrive()
        with tempfile.TemporaryDirectory() as directory:
            service = DesktopService(lambda _:None, backend=DriveBackend(lambda:COOKIE, transport=drive.transport()), directory=directory)
            service.saved_cookie = COOKIE
            params = {'entry':asdict(entry_of(drive.rows['original']))}
            try:
                with self.assertRaisesRegex(UsageError, '分享'):
                    service.dispatch('open','text.open',{**params,'handle':'share'},None)
                opened=service.dispatch('open','text.open',params,None)
                args={key:opened[key] for key in ('editor','version')};args['text']='saved'
                service.busy=True
                with self.assertRaises(UsageError): service.dispatch('save','text.save',args,None)
                service.busy=False
                with patch('quark_cli.desktop.text_editor.Transfer.upload',side_effect=lambda source, parent, remote_name: drive.upload(source,parent,remote_name)):
                    result=service.dispatch('save','text.save',args,None)
                self.assertEqual(result['entry']['fid'], 'new0')
                service.saved_cookie='different account'
                with self.assertRaisesRegex(UsageError, '账号'): service.dispatch('save','text.save',{**args,'version':result['version'],'text':'next'},None)
                service.dispatch('close','text.close',{'editor':opened['editor']},None)
                self.assertFalse(service.editor.sessions)
                service.logged_out=True
                with self.assertRaises(AuthenticationError): service.dispatch('open','text.open',params,None)
            finally:
                service.shutdown()
