import codecs
import json
import tempfile
import unittest
from dataclasses import asdict

import httpx

from quark_cli.desktop.service import DesktopService
from quark_cli.downloader.core import entry_of
from quark_cli.downloader.text_preview import MAX_TEXT_BYTES, decode_text
from quark_cli.downloader.worker import DriveBackend
from quark_cli.errors import AuthenticationError, RemoteError, UsageError, RateLimitError
from tests.test_downloader_worker import COOKIE, item


class TextFixture:
    def __init__(self, body=b'{"message":"hello"}', name='test.json'):
        self.body = body
        self.record = item('text', name, size=len(body), updated_at=123)
        self.requests = []
        self.address_size = None
        self.address_name = None
        self.address_url = 'https://files.aliyuncs.com/content?sig=private-signature'
        self.failure = None
        self.stream = None

    def transport(self):
        def handle(request):
            self.requests.append(request)
            if self.failure is not None:
                return httpx.Response(200, json={'status':self.failure,'code':self.failure,'message':'private-signature private-credential'})
            if request.url.path.endswith('/file/info'):
                data = self.record
            elif request.url.path.endswith('/file/download'):
                assert json.loads(request.content) == {'fids':['text']}
                data = [{'fid':'text','file_name':self.address_name or self.record['file_name'],
                         'size':self.record['size'] if self.address_size is None else self.address_size,
                         'download_url':self.address_url}]
            elif request.url.host == 'files.aliyuncs.com':
                return httpx.Response(200, stream=self.stream) if self.stream is not None else httpx.Response(200, content=self.body)
            else:
                raise AssertionError('Unexpected request')
            return httpx.Response(200, json={'status':200,'code':0,'data':data})
        return httpx.MockTransport(handle)

    def backend(self):
        return DriveBackend(lambda:COOKIE, transport=self.transport())


class TextTests(unittest.TestCase):
    def test_common_files_decode_without_executing_or_writing_content(self):
        samples = [('settings.json', '{"中文":true}', 'utf-8'),
                   ('note.txt', '中文\n第二行', 'gb18030'),
                   ('README.md', '# 标题\n<script>window.bad=true</script>', 'utf-8-sig'),
                   ('config.ini', '中文=值', 'utf-16'),
                   ('manifest.xml', '<root/>', 'utf-8')]
        for name, text, encoding in samples:
            with self.subTest(name=name):
                fixture = TextFixture(text.encode(encoding), name)
                result = fixture.backend().text_preview(entry_of(fixture.record))
                self.assertEqual(result['text'], text)
                self.assertEqual(result['size'], len(fixture.body))
                self.assertNotIn('private', str(result))
                self.assertEqual([request.method for request in fixture.requests], ['GET','POST','GET'])
                self.assertNotIn('cookie', fixture.requests[-1].headers)

    def test_refresh_reads_updated_size_and_contents_instead_of_cached_entry(self):
        fixture = TextFixture(b'first', 'live.txt')
        entry = entry_of(fixture.record)
        backend = fixture.backend()
        self.assertEqual(backend.text_preview(entry)['text'], 'first')
        fixture.body = b'updated content'
        fixture.record.update(size=len(fixture.body), updated_at=999)
        result = backend.text_preview(entry)
        self.assertEqual(result['text'], 'updated content')
        self.assertEqual(result['modified_at'], 999)
        self.assertEqual(len(fixture.requests), 6)

    def test_empty_file_and_bom_encodings(self):
        fixture = TextFixture(b'', 'empty.txt')
        self.assertEqual(fixture.backend().text_preview(entry_of(fixture.record))['text'], '')
        self.assertEqual(len(fixture.requests), 1)
        for body in (codecs.BOM_UTF16_BE+'中文'.encode('utf-16-be'), codecs.BOM_UTF16_LE+'中文'.encode('utf-16-le')):
            self.assertEqual(decode_text(body), ('中文','UTF-16'))

    def test_large_file_is_rejected_before_requesting_download_address(self):
        fixture = TextFixture()
        fixture.record['size'] = MAX_TEXT_BYTES+1
        with self.assertRaisesRegex(UsageError, '2 MiB'):
            fixture.backend().text_preview(entry_of(fixture.record))
        self.assertEqual(len(fixture.requests), 1)

    def test_oversized_unknown_length_stream_is_stopped(self):
        class Oversized(httpx.SyncByteStream):
            def __iter__(self):
                yield b'a' * MAX_TEXT_BYTES
                yield b'b'
        fixture = TextFixture()
        fixture.record['size'] = None
        fixture.stream = Oversized()
        with self.assertRaisesRegex(UsageError, '预览大小限制'):
            fixture.backend().text_preview(entry_of(fixture.record))

    def test_binary_invalid_encoding_and_unsupported_extensions_are_rejected(self):
        for body in (b'\x00binary', b'bad\x1bcontrol', b'\xff\xff'):
            with self.subTest(body=body), self.assertRaises(UsageError):
                decode_text(body)
        fixture = TextFixture(name='program.exe')
        with self.assertRaises(UsageError):
            fixture.backend().text_preview(entry_of(fixture.record))
        self.assertFalse(fixture.requests)

    def test_renamed_files_do_not_open_as_old_selection(self):
        fixture = TextFixture()
        entry = entry_of(fixture.record)
        fixture.record['file_name'] = 'renamed.json'
        with self.assertRaisesRegex(UsageError, '选中文件已变化'):
            fixture.backend().text_preview(entry)
        self.assertEqual(len(fixture.requests), 1)

    def test_address_identity_and_truncated_response_are_checked(self):
        for failure in ('size','name','truncated','untrusted'):
            with self.subTest(failure=failure):
                fixture = TextFixture()
                if failure=='size':fixture.address_size=MAX_TEXT_BYTES+1
                elif failure=='name':fixture.address_name='other.json'
                elif failure=='truncated':fixture.body=b'{'  # metadata still has full length
                else:fixture.address_url='https://evil.example/private-signature'
                with self.assertRaises((RemoteError,UsageError)) as raised:
                    fixture.backend().text_preview(entry_of(fixture.record))
                self.assertNotIn('private', str(raised.exception))
                self.assertFalse(any(request.url.host=='evil.example' for request in fixture.requests))

    def test_api_failures_are_sanitized_and_cooldown_type_is_preserved(self):
        for status, error in ((500,RemoteError),(401,AuthenticationError),(429,RateLimitError)):
            with self.subTest(status=status):
                fixture = TextFixture();fixture.failure=status
                with self.assertRaises(error) as raised:
                    fixture.backend().text_preview(entry_of(fixture.record))
                self.assertNotIn('private', str(raised.exception))

    def test_ipc_rejects_share_and_logged_out_reads(self):
        fixture = TextFixture()
        with tempfile.TemporaryDirectory() as directory:
            service = DesktopService(lambda _:None, backend=fixture.backend(), directory=directory)
            params = {'entry':asdict(entry_of(fixture.record))}
            try:
                self.assertIn('hello', service.dispatch('text','text.read',params,None)['text'])
                with self.assertRaisesRegex(UsageError, '分享'):
                    service.dispatch('text','text.read',{**params,'handle':'share'},None)
                service.logged_out=True
                with self.assertRaises(AuthenticationError):
                    service.dispatch('text','text.read',params,None)
            finally:
                service.shutdown()
