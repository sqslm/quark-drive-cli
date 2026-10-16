"""Read-only, bounded image previews. Signed addresses stay in Python."""
from __future__ import annotations

import base64
import hashlib
import io
import threading
import time
import warnings
from collections import OrderedDict
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

from ..errors import RemoteError, UsageError, AuthenticationError, RateLimitError, AccessRestrictedError
from ..files import Files
from ..client import API_HOST
from ..paths import valid_fid
from ..shares import ShareSaver
from ..transfer import Transfer
from .core import entry_of


def supported(entry):
    return not entry.directory and PurePosixPath(entry.name).suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'}


class ImageReader:
    def __init__(self):
        self.records = OrderedDict()
        self.lock = threading.Lock()

    @staticmethod
    def scope(cookie, source=None):
        return hashlib.sha256(cookie.encode()).hexdigest(), source

    def remember(self, scope, records, selection=None):
        with self.lock:
            # Keep the first visible page when a very large directory exceeds the cap.
            for record in reversed(records):
                if not supported(entry_of(record)):
                    continue
                key = (scope, str(record['fid']))
                self.records[key] = (dict(record), selection)
                self.records.move_to_end(key)
                while len(self.records) > 256:
                    self.records.popitem(last=False)

    def clear(self):
        with self.lock:
            self.records.clear()

    def read(self, client, scope, entry, *, thumbnail=False, source=None):
        if not supported(entry):
            raise UsageError('图片预览支持 JPG、JPEG、PNG 和 WebP')
        fid = valid_fid(entry.fid)
        with self.lock:
            cached = self.records.get((scope, fid))
        if cached is not None:
            record, selection = cached
        elif source is not None:
            parent = str(PurePosixPath(entry.path).parent)
            selected = ShareSaver(client)._source(source.url, parent, source.passcode)
            self.remember(scope, selected.items, (selected.pwd_id, selected.stoken))
            record = next((item for item in selected.items if str(item.get('fid')) == fid), None)
            selection = (selected.pwd_id, selected.stoken)
        else:
            record, selection = Files(client).info(fid), None
            self.remember(scope, [record])
        if not isinstance(record, dict):
            raise RemoteError('图片已不可用，请刷新目录')
        actual = entry_of(record)
        if ((actual.fid, actual.name, actual.directory) != (entry.fid, entry.name, entry.directory)
                or entry.size is not None and actual.size != entry.size
                or entry.modified_at is not None and actual.modified_at != entry.modified_at):
            raise RemoteError('图片信息已变化，请刷新目录')
        url = record.get('preview_url') or record.get('thumbnail' if thumbnail else 'big_thumbnail')
        # Listing fields can be bare API routes, not signed image addresses.
        # They return 401 even for a valid desktop session; resolve the official
        # preview URL instead of treating that response as an invalid image.
        if isinstance(url, str):
            parsed = urlsplit(url)
            if parsed.hostname == API_HOST and parsed.path in ('/1/clouddrive/file/thumbnail', '/1/clouddrive/file/quick'):
                url = None
        if not isinstance(url, str) or not url:
            if selection is not None:
                token = record.get('share_fid_token')
                if not isinstance(token, str) or not token:
                    raise RemoteError('分享图片缺少读取授权，请重新打开分享')
                payload = self.preview(client, 'share/sharepage/preview', {
                    'pwd_id': selection[0], 'stoken': selection[1], 'fid_list': fid,
                    'fid_token_list': token, 'format': 'png'})
            else:
                payload = self.preview(client, 'file/advanced_preview', {
                    'pdir_fid': valid_fid(record.get('pdir_fid')), 'cat': 'image',
                    'q': '', '_sort': 'updated_at:desc', '_size': 15, 'cur_fid': fid,
                    'cur_value': record.get('updated_at', 0), 'direction': 'after', 'format': ''})
            data = payload.get('data')
            item = next((row for row in data if isinstance(row, dict) and str(row.get('fid')) == fid), None) if isinstance(data, list) else None
            url = item.get('preview_url') if item else None
            if isinstance(url, str) and url:
                with self.lock:
                    stored = self.records.get((scope, fid))
                    if stored and entry_of(stored[0]) == actual:
                        stored[0]['preview_url'] = url
        if not isinstance(url, str) or not url:
            raise RemoteError('服务端尚未提供图片预览，可稍后重试或下载原图')
        try:
            data = self.fetch(client, url, limit=12 * 1024 * 1024)
            return self.normalize(data, thumbnail=thumbnail)
        except Exception:
            # Do not send signed URLs, redirect locations or decoder diagnostics over IPC.
            raise RemoteError('图片加载失败，可能已过期、格式不支持或超过预览大小限制；请重试或下载原图') from None

    @staticmethod
    def preview(client, endpoint, params):
        try:
            return client.get(endpoint, params=params)
        except (RateLimitError, AccessRestrictedError) as error:
            raise type(error)('图片请求暂时受限，请等待冷却结束后重试', retry_after=error.retry_after) from None
        except AuthenticationError:
            raise AuthenticationError('图片读取未获授权，请重新登录或重新打开分享') from None
        except Exception:
            raise RemoteError('图片预览接口暂不可用，请稍后重试或下载原图') from None

    @staticmethod
    def fetch(client, url, *, limit):
        deadline = time.monotonic() + 20
        client.transfer_client().timeout = httpx.Timeout(8, connect=3)
        with Transfer(client)._download_stream(url, {'Accept-Encoding': 'identity'}) as response:
            if response.status_code != 200:
                raise RemoteError('预览地址不可用')
            length = response.headers.get('content-length')
            if length is not None and int(length) > limit:
                raise UsageError('文件超过预览大小限制，请下载后查看')
            data = bytearray()
            for chunk in response.iter_bytes(64 * 1024):
                if len(data) + len(chunk) > limit:
                    raise UsageError('文件超过预览大小限制，请下载后查看')
                if time.monotonic() > deadline:
                    raise RemoteError('预览读取超时')
                data.extend(chunk)
            return bytes(data)

    @staticmethod
    def normalize(data, *, thumbnail):
        from PIL import Image, ImageOps
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ('JPEG', 'PNG', 'WEBP') or image.width * image.height > 32_000_000:
                    raise RemoteError('图片格式或像素超过预览限制')
                image = ImageOps.exif_transpose(image)
                image.thumbnail((80, 80) if thumbnail else (2560, 2560))
                image = image.convert('RGBA' if image.mode in ('RGBA', 'LA', 'P') else 'RGB')
                image.info.clear()
                output = io.BytesIO()
                fmt = 'PNG' if image.mode == 'RGBA' else 'JPEG'
                image.save(output, format=fmt, quality=85)
                encoded = output.getvalue()
                if len(encoded) > 8 * 1024 * 1024:
                    raise RemoteError('图片超过预览大小限制')
                return {'src': f'data:image/{fmt.lower()};base64,' + base64.b64encode(encoded).decode('ascii'),
                        'width': image.width, 'height': image.height}
