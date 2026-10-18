"""Private saved logins and the owner of each local video library."""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path

from ..errors import AuthenticationError, LocalIOError, UsageError, QuarkError
from ..auth import load_saved_cookie
from ..state import read_json, write_json
from .plugin_state import plugin_directory


def account_key(cookie: str) -> str:
    # A nickname can change or be shared. QR cookies carry the stable __uid;
    # credentials without it get a separate identity, never somebody else's data.
    uid = next((value.strip() for part in cookie.split(';')
                for name, separator, value in [part.strip().partition('=')]
                if name == '__uid' and separator and value.strip()), None)
    return hashlib.sha256(('uid:' + uid if uid else 'cookie:' + cookie).encode()).hexdigest()


class Accounts:
    def __init__(self, directory: Path | None = None, *, allow_unavailable=False):
        self.directory = plugin_directory() if directory is None else directory
        self.path = self.directory / 'accounts.json'
        self.error: QuarkError | None = None
        self.migrated = False
        try:
            self._load()
        except QuarkError as error:
            if not allow_unavailable:
                raise
            # An unavailable account store is read-only. Diagnostics may start,
            # but no account command can replace the file with an empty store.
            self.error = error
            self.document = {'version': 1, 'active': None, 'legacy': None, 'accounts': {}}

    def require_available(self):
        if self.error is not None:
            raise self.error

    def _load(self):
        self.document = read_json(self.path, default={'version': 1, 'active': None, 'legacy': None, 'accounts': {}})
        if 'version' not in self.document and isinstance(self.document.get('accounts'), list):
            self._migrate_legacy()
        doc = self.document
        if (not isinstance(doc, dict) or doc.get('version') != 1 or not isinstance(doc.get('accounts'), dict)
                or not {'active', 'legacy'} <= doc.keys()):
            raise LocalIOError('账号记录格式无效，已停止；不会覆盖原文件')
        for key, row in doc['accounts'].items():
            if (not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key)
                    or not isinstance(row, dict) or 'cookie' not in row or not isinstance(row.get('nickname'), str)
                    or (row.get('cookie') is not None and (not isinstance(row['cookie'], str) or not row['cookie'].strip()
                                                         or '\r' in row['cookie'] or '\n' in row['cookie']))):
                raise LocalIOError('账号记录格式无效，已停止；不会覆盖原文件')
            if row['cookie'] is not None and account_key(row['cookie']) != key:
                raise LocalIOError('账号凭证归属不一致，已停止；不会加载其他账号记录')
        if any(doc.get(field) is not None and (not isinstance(doc[field], str) or doc[field] not in doc['accounts'])
               for field in ('active', 'legacy')):
            raise LocalIOError('账号记录引用无效，已停止；不会覆盖原文件')
        for row in doc['accounts'].values():
            capacity = row.get('capacity')
            if capacity is not None and (not isinstance(capacity, dict) or any(
                    type(capacity.get(field)) is not int or capacity[field] < 0 for field in ('used_bytes', 'total_bytes'))):
                raise LocalIOError('账号容量记录无效，已停止；不会覆盖原文件')
        if not isinstance(doc.get('libraries', {}), dict):
            raise LocalIOError('账号观看记录归属无效，已停止')
        for key, name in doc.get('libraries', {}).items():
            if key not in doc['accounts'] or not isinstance(name, str) or not re.fullmatch(r'library-[A-Za-z0-9_-]+\.json', name):
                raise LocalIOError('账号观看记录归属无效，已停止')

    def _migrate_legacy(self):
        old = self.document
        rows = old['accounts']
        if not isinstance(old.get('current'), (str, type(None))):
            raise LocalIOError('旧账号选择无效，无法迁移')
        doc = {'version': 1, 'active': None, 'legacy': None, 'accounts': {}, 'libraries': {}}
        identities = {}
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                    or not re.fullmatch('[A-Za-z0-9_-]+', row['id'])
                    or not isinstance(row.get('cookie'), str) or not row['cookie'].strip()
                    or '\r' in row['cookie'] or '\n' in row['cookie']
                    or not isinstance(row.get('nickname'), str)):
                raise LocalIOError('旧账号记录无效，无法迁移；原文件保持不变')
            key = account_key(row['cookie'])
            if key in doc['accounts'] or row['id'] in identities:
                raise LocalIOError('旧账号记录重复，无法确定数据归属')
            identities[row['id']] = key
            doc['accounts'][key] = {'cookie': row['cookie'], 'nickname': row['nickname']}
            library = f"library-{row['id']}.json"
            if (self.directory / library).exists():
                doc['libraries'][key] = library
        current = old.get('current')
        if current:
            if current not in identities:
                raise LocalIOError('旧账号选择不存在，无法迁移')
            doc['active'] = identities[current]
        cookie = load_saved_cookie(self.directory / 'cookie')
        if cookie and doc['active'] == account_key(cookie):
            doc['legacy'] = doc['active']
        backup = self.directory / 'accounts.legacy-backup.json'
        if backup.exists() and read_json(backup) != old:
            raise LocalIOError('旧账号迁移备份已存在且内容不同，已停止')
        if not backup.exists():
            write_json(backup, old)
        write_json(self.path, doc)
        self.document = doc
        self.migrated = True

    @property
    def active(self):
        return self.document['active']

    def cookie(self):
        row = self.document['accounts'].get(self.active)
        return row['cookie'] if row else None

    def _save(self, document):
        self.require_available()
        write_json(self.path, document)
        self.document = document

    def add(self, cookie: str, nickname: str | None = None, *, legacy=False):
        self.require_available()
        if not isinstance(cookie, str) or not cookie.strip() or '\r' in cookie or '\n' in cookie:
            raise AuthenticationError('登录未返回有效凭证')
        key = account_key(cookie)
        doc = copy.deepcopy(self.document)
        row = doc['accounts'].setdefault(key, {'nickname': '已保存账号'})
        row['cookie'] = cookie
        if nickname is not None:
            row['nickname'] = nickname
        doc['active'] = key
        if legacy and not self.path.exists():
            doc['legacy'] = key
        self._save(doc)
        return key

    def switch(self, key):
        self.require_available()
        row = self.document['accounts'].get(key) if isinstance(key, str) else None
        if row is None:
            raise UsageError('请选择已保存的账号')
        if not row['cookie']:
            raise UsageError('该账号已退出，请重新扫码登录')
        doc = copy.deepcopy(self.document)
        doc['active'] = key
        self._save(doc)

    def logout(self, key):
        self.require_available()
        if not isinstance(key, str) or key not in self.document['accounts']:
            raise UsageError('请选择已保存的账号')
        doc = copy.deepcopy(self.document)
        doc['accounts'][key]['cookie'] = None
        if key == doc['active']:
            doc['active'] = None
        self._save(doc)

    def profile(self, nickname, capacity):
        self.require_available()
        if self.active is None:
            return
        doc = copy.deepcopy(self.document)
        row = doc['accounts'][self.active]
        row['nickname'] = nickname
        row['capacity'] = capacity
        self._save(doc)

    def data_path(self, name):
        if name == 'library.json' and self.active in self.document.get('libraries', {}):
            return self.directory / self.document['libraries'][self.active]
        if self.active == self.document['legacy'] and self.active is not None:
            return self.directory / name
        return self.directory / 'accounts' / (self.active or 'signed-out') / name

    def public(self):
        self.require_available()
        return {'active': self.active, 'items': [
            {'id': key, 'nickname': row['nickname'], 'logged_in': bool(row['cookie']),
             'current': key == self.active, 'capacity': row.get('capacity')}
            for key, row in self.document['accounts'].items()
        ]}
