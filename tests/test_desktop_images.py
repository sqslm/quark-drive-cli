"""Read-only image API, bounds and independent desktop worker coverage."""
import base64
import io
import tempfile
import threading
import unittest
from dataclasses import asdict, replace

import httpx
from PIL import Image

from quark_cli.client import QuarkClient
from quark_cli.desktop.service import DesktopService
from quark_cli.downloader.core import entry_of
from quark_cli.downloader.images import ImageReader
from quark_cli.downloader.share import ShareSource
from quark_cli.downloader.worker import DriveBackend
from quark_cli.errors import RemoteError, UsageError, RateLimitError
from tests.test_downloader_worker import COOKIE, item


def picture(fmt='PNG', size=(300, 160)):
    output = io.BytesIO()
    Image.new('RGB', size, '#4080bb').save(output, format=fmt)
    return output.getvalue()


class ImageFixture:
    def __init__(self):
        self.calls = []
        self.record = {**item('photo', '照片.png', size=900), 'updated_at': 123000,
                       'thumbnail': 'https://images.aliyuncs.com/thumb?sign=secret'}
        self.body = picture()
        self.url = 'https://images.aliyuncs.com/preview?sign=secret'
        self.status = 200

    def transport(self):
        def handle(request):
            self.calls.append(request)
            path = request.url.path
            if request.url.host == 'images.aliyuncs.com':
                return httpx.Response(self.status, content=self.body)
            if path.endswith('file/sort'):
                data = {'list': [self.record], '_total': 1}
            elif path.endswith('file/info'):
                data = self.record
            elif path.endswith('advanced_preview') or path.endswith('sharepage/preview'):
                data = [{'fid': 'other', 'preview_url': 'https://evil.example/image'},
                        {'fid': 'photo', 'preview_url': self.url}]
            else:
                raise AssertionError(f'Unexpected request {request.method} {path}')
            return httpx.Response(200, json={'status': 200, 'code': 0, 'data': data})
        return httpx.MockTransport(handle)


class ImageTests(unittest.TestCase):
    def test_listing_thumbnail_reuses_metadata_and_does_not_expose_addresses(self):
        fixture = ImageFixture()
        backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
        entry = backend.listing('0')[0]
        result = backend.image(entry, thumbnail=True)
        self.assertEqual(len(fixture.calls), 2)
        self.assertNotIn('cookie', fixture.calls[-1].headers)
        self.assertNotIn('secret', str(result))
        self.assertNotIn('thumbnail', asdict(entry))
        image = Image.open(io.BytesIO(base64.b64decode(result['src'].split(',')[1])))
        self.assertEqual(image.size, (80, 43))
        self.assertFalse(image.info.get('exif'))

    def test_preview_exact_fid_and_fixed_official_parameters(self):
        fixture = ImageFixture()
        backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
        entry = backend.listing('0')[0]
        result = backend.image(entry)
        request = fixture.calls[1]
        self.assertTrue(request.url.path.endswith('/file/advanced_preview'))
        self.assertEqual(dict(request.url.params)['cur_fid'], 'photo')
        self.assertEqual(dict(request.url.params)['cat'], 'image')
        self.assertEqual(result['width'], 300)
        self.assertTrue(all(request.method == 'GET' for request in fixture.calls))

    def test_refresh_refetches_metadata_and_url(self):
        fixture = ImageFixture()
        backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
        entry = backend.listing('0')[0]
        fixture.record['thumbnail'] = 'https://images.aliyuncs.com/fresh'
        backend.image(entry, thumbnail=True, refresh=True)
        self.assertTrue(fixture.calls[-2].url.path.endswith('file/info'))
        self.assertEqual(fixture.calls[-1].url.path, '/fresh')

    def test_search_result_with_unknown_metadata_can_preview(self):
        fixture = ImageFixture()
        backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
        entry = replace(entry_of(fixture.record), size=None, modified_at=None)
        self.assertEqual(backend.image(entry)['width'], 300)

    def test_share_browse_and_refresh_use_exact_fid_even_with_pattern_characters(self):
        from share_fixture import ShareFixture, shared, SHARE_URL
        fixture = ShareFixture()
        fixture.share_directories['0'] = [shared('photo', '照片[1].png', 'owner', size=900)]
        requests = []
        def handle(request):
            requests.append(request)
            if request.url.path.endswith('sharepage/preview'):
                self.assertEqual(request.url.params['fid_token_list'], 'private-token-photo')
                return httpx.Response(200, json={'status':200,'code':0,'data':[{'fid':'photo','preview_url':'https://images.aliyuncs.com/image'}]})
            if request.url.host == 'images.aliyuncs.com':
                return httpx.Response(200, content=picture())
            return fixture.handler(request)
        backend = DriveBackend(lambda: COOKIE, transport=httpx.MockTransport(handle))
        source = ShareSource(SHARE_URL, '1111')
        entry = backend.share_listing(source, '/')[0]
        self.assertNotIn('private', str(asdict(entry)))
        initial = len(requests)
        backend.image(entry, source=source)
        self.assertEqual(len(requests)-initial, 2)
        backend.image(entry, source=source, refresh=True)
        self.assertFalse(fixture.saves)
        self.assertEqual(len(fixture.token_bodies), 2)

    def test_account_scope_prevents_reusing_previous_metadata(self):
        fixture = ImageFixture()
        cookie = [COOKIE]
        backend = DriveBackend(lambda: cookie[0], transport=fixture.transport())
        entry = backend.listing('0')[0]
        cookie[0] = 'sid=another'
        backend.image(entry, thumbnail=True)
        self.assertTrue(fixture.calls[-2].url.path.endswith('file/info'))

    def test_rejects_changed_identity_and_unsupported_files(self):
        fixture = ImageFixture()
        backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
        entry = backend.listing('0')[0]
        fixture.record['file_name'] = 'changed.png'
        with self.assertRaisesRegex(RemoteError, '变化'):
            backend.image(entry, refresh=True)
        with self.assertRaises(UsageError):
            backend.image(entry_of(item('txt', 'test.svg', size=9)))

    def test_share_preview_only_reads_without_save_or_new_authorization(self):
        fixture = ImageFixture()
        backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
        source = ShareSource('https://pan.quark.cn/s/test', '1111')
        record = {**fixture.record, 'share_fid_token': 'private-token'}
        backend.images.remember(backend.images.scope(COOKIE, source), [record], ('test', 'private-stoken'))
        result = backend.image(entry_of(record), source=source)
        self.assertEqual(result['height'], 160)
        self.assertEqual(len(fixture.calls), 2)
        params = fixture.calls[0].url.params
        self.assertEqual(params['fid_token_list'], 'private-token')
        self.assertNotIn('private', str(result))
        self.assertTrue(all(request.method == 'GET' for request in fixture.calls))

    def test_invalid_or_untrusted_image_response_is_sanitized(self):
        for url, body, status in [('https://evil.example/secret', picture(), 200),
                                   ('https://images.aliyuncs.com/preview', b'<svg>secret</svg>', 200),
                                   ('https://images.aliyuncs.com/preview', picture(), 403)]:
            with self.subTest(url=url, status=status):
                fixture = ImageFixture()
                fixture.url, fixture.body, fixture.status = url, body, status
                backend = DriveBackend(lambda: COOKIE, transport=fixture.transport())
                with self.assertRaisesRegex(RemoteError, '^图片加载失败') as raised:
                    backend.image(entry_of(fixture.record))
                self.assertNotIn('secret', str(raised.exception))
                self.assertFalse(any(request.url.host == 'evil.example' for request in fixture.calls))

    def test_fetch_rejects_redirect_and_size_overflow(self):
        for response in (httpx.Response(302, headers={'location':'https://evil.example/secret'}),
                         httpx.Response(200, content=b'a' * 11),
                         httpx.Response(200, headers={'content-length':'100'}, content=b'a')):
            with self.subTest(response=response):
                transport = httpx.MockTransport(lambda request:response)
                with QuarkClient(COOKIE, transport=transport) as client:
                    with self.assertRaises(Exception):
                        ImageReader.fetch(client, 'https://images.aliyuncs.com/image', limit=10)

    def test_normalizes_supported_rasters_and_caps_dimensions(self):
        for fmt in ('JPEG', 'PNG', 'WEBP'):
            with self.subTest(fmt=fmt):
                result = ImageReader.normalize(picture(fmt, (3000, 100)), thumbnail=False)
                self.assertEqual(result['width'], 2560)
                self.assertLessEqual(result['height'], 100)
        with self.assertRaises(RemoteError):
            ImageReader.normalize(picture('GIF'), thumbnail=False)
        oversized = io.BytesIO()
        Image.new('1', (8001, 4000)).save(oversized, format='PNG')
        with self.assertRaisesRegex(RemoteError, '像素'):
            ImageReader.normalize(oversized.getvalue(), thumbnail=False)

    def test_preview_api_does_not_echo_share_tokens_and_preserves_cooldown(self):
        for code, error in [(400, RemoteError), (429, RateLimitError)]:
            with self.subTest(code=code):
                response = lambda request:httpx.Response(200, json={'status':code,'code':code,'message':'private-stoken private-token-photo'})
                with QuarkClient(COOKIE, transport=httpx.MockTransport(response)) as client:
                    with self.assertRaises(error) as raised:
                        ImageReader.preview(client, 'share/sharepage/preview', {'stoken':'private-stoken'})
                    self.assertNotIn('private', str(raised.exception))
                    if code == 429:
                        self.assertGreater(raised.exception.retry_after, 0)

    def test_metadata_cap_prioritizes_first_page_and_clear_releases_tokens(self):
        reader = ImageReader()
        records = [item(str(n), f'{n}.jpg', size=1) for n in range(1000)]
        reader.remember('scope', records, ('share', 'token'))
        self.assertEqual(len(reader.records), 256)
        self.assertIn(('scope', '0'), reader.records)
        reader.clear()
        self.assertFalse(reader.records)

    def test_image_workers_do_not_block_browse_and_rpc_is_whitelisted(self):
        started = [threading.Event(), threading.Event()]
        release = threading.Event()
        class Backend:
            def image(self, entry, **kwargs):
                started[int(entry.fid)].set()
                release.wait(3)
                return {'src': 'data:image/png;base64,', 'width': 1, 'height': 1}
            def listing(self, fid):
                return []
        with tempfile.TemporaryDirectory() as directory:
            responses = []
            service = DesktopService(responses.append, backend=Backend(), directory=directory)
            try:
                for n in range(2):
                    service.accept({'id': str(n), 'method':'image.read', 'params':{'entry':asdict(entry_of(item(str(n), 'a.jpg', size=1)))}})
                self.assertTrue(all(event.wait(1) for event in started))
                service.accept({'id':'browse', 'method':'browse', 'params':{}})
                event = threading.Event()
                for _ in range(100):
                    if any(response['id'] == 'browse' for response in responses):
                        break
                    event.wait(.01)
                self.assertIn({'id':'browse', 'result':[]}, responses)
            finally:
                release.set()
                service.shutdown()


if __name__ == '__main__':
    unittest.main()
