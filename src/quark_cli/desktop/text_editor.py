"""Explicit, version-checked replacement saves for small cloud text files."""
import codecs
import hashlib
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from ..downloader.text_preview import MAX_TEXT_BYTES, decode_text, read_document
from ..downloader.core import Entry, describe_error
from ..errors import UsageError, RemoteError, contextual_error
from ..files import Files
from ..state import private_path
from ..transfer import Transfer


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalized(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


@dataclass
class EditSession:
    account: str
    entry: Entry
    parent: str
    baseline: str
    encoding: str
    bom: bytes
    newline: str
    version: str
    blocked: bool = False


class TextEditor:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()

    def open(self, client, entry, account):
        document = read_document(client, entry)
        raw = document.data
        encoding = document.encoding.lower()
        bom = b''
        if encoding == 'utf-16':
            bom = raw[:2]
            encoding = 'utf-16-be' if bom == codecs.BOM_UTF16_BE else 'utf-16-le'
        elif encoding == 'utf-8-sig':
            bom, encoding = codecs.BOM_UTF8, 'utf-8'
        newline = '\r\n' if '\r\n' in document.text else '\r' if '\r' in document.text else '\n'
        identifier = uuid.uuid4().hex
        session = EditSession(account, document.entry, document.parent, digest(raw), encoding, bom, newline, uuid.uuid4().hex)
        with self.lock:
            self.sessions[identifier] = session
            while len(self.sessions) > 16:
                self.sessions.pop(next(iter(self.sessions)))
        return {**document.public(), 'text': normalized(document.text), 'editor': identifier,
                'version': session.version, 'entry': asdict(document.entry)}

    def close(self, identifier):
        with self.lock:
            self.sessions.pop(identifier, None)

    def clear(self):
        with self.lock:
            self.sessions.clear()

    @staticmethod
    def checked(client, session):
        document = read_document(client, session.entry)
        if document.parent != session.parent or digest(document.data) != session.baseline:
            raise UsageError('云端文件已被修改或移动，已停止保存；请复制草稿并重新读取后合并')
        return document

    def save(self, client, identifier, version, text, account, *, upload=None):
        session = self.sessions.get(identifier)
        if not session or session.account != account:
            raise UsageError('编辑会话或账号已变化，请重新打开文件')
        if session.blocked:
            raise UsageError('上次保存结果未确认，已禁止重发；请核对网盘并重新打开文件，当前草稿可复制')
        if version != session.version:
            raise UsageError('编辑版本已变化，请重新打开文件')
        if not isinstance(text, str) or len(text) > MAX_TEXT_BYTES:
            raise UsageError('编辑内容超过 2 MiB 限制')
        try:
            data = session.bom + normalized(text).replace('\n', session.newline).encode(session.encoding)
        except UnicodeError:
            raise UsageError('当前字符无法按原文件编码保存，草稿已保留') from None
        if len(data) > MAX_TEXT_BYTES:
            raise UsageError('编辑内容按原编码计算超过 2 MiB，草稿已保留')
        decode_text(data)
        self.checked(client, session)
        if digest(data) == session.baseline:
            return {'version':session.version, 'entry':asdict(session.entry), 'warning':'', 'blocked':False}
        if not data:
            raise UsageError('当前上传接口不支持保存空文件，草稿已保留；原文件未修改')
        files = Files(client)
        suffix = PurePosixPath(session.entry.name).suffix
        marker = uuid.uuid4().hex
        stage_name, backup_name = f'.quark-edit-{marker}{suffix}', f'.quark-edit-backup-{marker}{suffix}'
        installed = None
        session.blocked = True  # Any uncertain write must never be automatically replayed.
        phase = '上传新内容'
        try:
            with tempfile.TemporaryDirectory(prefix='quark-editor-') as temporary:
                directory = Path(temporary)
                private_path(directory, directory=True)
                source = directory / ('content'+suffix)
                source.write_bytes(data)
                result = (upload or (lambda path, parent, name: Transfer(client, state_dir=directory/'state').upload(path, parent, remote_name=name)))(source, session.parent, stage_name)
                stage_fid = str(result['fid'])
                phase = '核验暂存内容'
                stage_entry = replace(session.entry, fid=stage_fid, name=stage_name, size=len(data), modified_at=None)
                staged = read_document(client, stage_entry)
                if staged.parent != session.parent or digest(staged.data) != digest(data):
                    raise RemoteError('新版本读回核验失败')
                # Recheck after upload, immediately before changing the original.
                phase = '复核原文件'
                self.checked(client, session)
                rows = files.list_all(session.parent, size=100)['data']['list']
                same = [row for row in rows if row.get('file_name','').casefold() == session.entry.name.casefold()]
                if len(same) != 1 or str(same[0].get('fid')) != session.entry.fid:
                    raise UsageError('目标同名对象已变化')
                phase = '备份原文件'
                files.rename(session.entry.fid, backup_name)
                backup = read_document(client, replace(session.entry, name=backup_name, modified_at=None))
                if backup.parent != session.parent or digest(backup.data) != session.baseline:
                    raise RemoteError('旧版本备份未确认')
                if any(row.get('file_name','').casefold() == session.entry.name.casefold() for row in files.list_all(session.parent,size=100)['data']['list']):
                    raise UsageError('保存期间出现新的同名文件，未覆盖')
                phase = '安装新版本'
                files.rename(stage_fid, session.entry.name)
                target = replace(session.entry, fid=stage_fid, size=len(data), modified_at=None)
                installed = read_document(client, target)
                if installed.parent != session.parent or digest(installed.data) != digest(data):
                    raise RemoteError('新版本安装未确认')
        except Exception as error:
            raise contextual_error(error, f'{describe_error(error, phase)}；保存未完成，已停止自动重试，当前草稿保留。请核对网盘暂存 {stage_name} 和旧版备份 {backup_name} 后重新打开；未永久删除原文件') from None
        old_fid = session.entry.fid
        session.entry = installed.entry
        session.baseline = digest(data)
        session.version = uuid.uuid4().hex
        warning = ''
        try:
            files.delete(old_fid)
            remaining = files.list_all(session.parent,size=100)['data']['list']
            if any(str(row.get('fid')) == old_fid for row in remaining):
                raise RemoteError('旧版回收未确认')
        except Exception:
            warning = f'新内容已保存，但旧版回收未确认；请核对 {backup_name}，自动保存已暂停'
        session.blocked = bool(warning)
        return {'version':session.version, 'entry':asdict(session.entry), 'warning':warning, 'blocked':session.blocked}
