"""Private saved logins and the owner of each local video library."""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path

from ..errors import AuthenticationError, LocalIOError, UsageError
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
    def __init__(self, directory: Path | None = None):
        self.directory = plugin_directory() if directory is None else directory
        self.path = self.directory / 'accounts.json'
        self.document = read_json(self.path, default={'version': 1, 'active': None, 'legacy': None, 'accounts': {}})
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

    @property
    def active(self):
        return self.document['active']

    def cookie(self):
        row = self.document['accounts'].get(self.active)
        return row['cookie'] if row else None

    def _save(self, document):
        write_json(self.path, document)
        self.document = document

    def add(self, cookie: str, nickname: str | None = None, *, legacy=False):
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
        row = self.document['accounts'].get(key) if isinstance(key, str) else None
        if row is None:
            raise UsageError('请选择已保存的账号')
        if not row['cookie']:
            raise UsageError('该账号已退出，请重新扫码登录')
        doc = copy.deepcopy(self.document)
        doc['active'] = key
        self._save(doc)

    def logout(self, key):
        if not isinstance(key, str) or key not in self.document['accounts']:
            raise UsageError('请选择已保存的账号')
        doc = copy.deepcopy(self.document)
        doc['accounts'][key]['cookie'] = None
        if key == doc['active']:
            doc['active'] = None
        self._save(doc)

    def profile(self, nickname, capacity):
        if self.active is None:
            return
        doc = copy.deepcopy(self.document)
        row = doc['accounts'][self.active]
        row['nickname'] = nickname
        row['capacity'] = capacity
        self._save(doc)

    def data_path(self, name):
        if self.active == self.document['legacy'] and self.active is not None:
            return self.directory / name
        return self.directory / 'accounts' / (self.active or 'signed-out') / name

    def public(self):
        return {'active': self.active, 'items': [
            {'id': key, 'nickname': row['nickname'], 'logged_in': bool(row['cookie']),
             'current': key == self.active, 'capacity': row.get('capacity')}
            for key, row in self.document['accounts'].items()
        ]}
