"""Small cloud text files, read in memory without executing their contents."""
import codecs
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from ..errors import RemoteError, UsageError, AuthenticationError, RateLimitError, AccessRestrictedError
from ..files import Files
from ..paths import valid_fid
from ..transfer import download_address
from .core import Entry, entry_of
from .images import ImageReader

MAX_TEXT_BYTES = 2 * 1024 * 1024
TEXT_EXTENSIONS = {'.json', '.txt', '.md', '.markdown', '.log', '.csv', '.yaml', '.yml', '.toml', '.ini', '.xml', '.conf'}


def supported_text(entry):
    return not entry.directory and PurePosixPath(entry.name).suffix.lower() in TEXT_EXTENSIONS


def decode_text(data):
    if data.startswith(codecs.BOM_UTF8):
        encodings = ('utf-8-sig',)
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encodings = ('utf-16',)
    else:
        encodings = ('utf-8', 'gb18030')
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except UnicodeError:
            continue
        if any(ord(char) < 32 and char not in '\t\r\n' or ord(char) == 127 for char in text):
            raise UsageError('文件包含二进制控制字符，无法作为文本预览')
        return text, encoding.upper()
    raise UsageError('无法识别文本编码，请下载后查看')


@dataclass
class TextDocument:
    entry: Entry
    parent: str
    data: bytes
    text: str
    encoding: str

    def public(self):
        return {'text': self.text, 'encoding': self.encoding, 'size': len(self.data), 'modified_at': self.entry.modified_at}


def read_document(client, entry):
    if not supported_text(entry):
        raise UsageError('该文件类型暂不支持文本预览')
    try:
        record = Files(client).info(valid_fid(entry.fid))
        actual = entry_of(record)
        if (actual.fid, actual.name, actual.directory) != (entry.fid, entry.name, False):
            raise UsageError('选中文件已变化，请刷新目录')
        if actual.size is not None and actual.size > MAX_TEXT_BYTES:
            raise UsageError('文本预览仅支持不超过 2 MiB 的文件，请下载后查看')
        if actual.size == 0:
            data = b''
        else:
            address = download_address(client, actual.fid)
            if address.name is not None and address.name != actual.name:
                raise RemoteError('预览文件身份不一致')
            if address.size is not None and (address.size > MAX_TEXT_BYTES or actual.size is not None and address.size != actual.size):
                raise UsageError('文件大小已变化或超过 2 MiB，请刷新目录后重试')
            data = ImageReader.fetch(client, address.url, limit=MAX_TEXT_BYTES)
            expected = actual.size if actual.size is not None else address.size
            if expected is not None and len(data) != expected:
                raise RemoteError('预览内容不完整')
    except (RateLimitError, AccessRestrictedError) as error:
        raise type(error)('文本预览请求受限，请等待冷却结束后重试', retry_after=error.retry_after) from None
    except AuthenticationError:
        raise AuthenticationError('文本读取未获授权，请重新登录') from None
    except UsageError:
        raise
    except Exception:
        raise RemoteError('文本读取失败，请刷新重试或下载后查看') from None
    text, encoding = decode_text(data)
    return TextDocument(replace(actual, path=entry.path, location=entry.location), valid_fid(record.get('pdir_fid')), data, text, encoding)


def read_text(client, entry):
    return read_document(client, entry).public()
