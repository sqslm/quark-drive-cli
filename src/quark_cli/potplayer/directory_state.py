"""Account-local verified directory names and parents, shared by navigation and titles."""
from __future__ import annotations

import re
import threading
from pathlib import Path

from ..errors import LocalIOError, RemoteError
from ..paths import is_directory, valid_fid
from ..state import read_json, write_json

_CATEGORIES = {'电视剧', '电影', '动漫', '综艺', '课程'}
_CONTAINERS = _CATEGORIES | {'影视', '视频', '资源', '我的网盘'}
_VERSION = re.compile(r'^(?:[248]k|\d{3,4}p|蓝光|高清|超清|原画|杜比|hdr|web[- .]?dl|blu[- .]?ray|s\d{1,2}|第[一二三四五六七八九十\d]+季)(?:$|[ ._\-（(])', re.I)


class Directories:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _read(self):
        document = read_json(self.path, default={'version': 1, 'items': {}})
        if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('items'), dict):
            raise LocalIOError('目录信息格式无效，不会覆盖原文件')
        for fid, row in document['items'].items():
            if not isinstance(row, dict) or set(row) != {'name', 'parent'} or not isinstance(row['name'], str):
                raise LocalIOError('目录信息格式无效，不会覆盖原文件')
            try:
                valid_fid(fid)
                valid_fid(row['parent'])
            except RemoteError:
                raise LocalIOError('目录信息格式无效，不会覆盖原文件') from None
        return document['items']

    def note(self, parent, items):
        with self._lock:
            nodes = self._read()
            before = dict(nodes)
            for item in items:
                if item.get('is_dir'):
                    nodes[item['fid']] = {'name': item['name'], 'parent': parent}
            if nodes != before:
                write_json(self.path, {'version': 1, 'items': nodes})

    def chain(self, fid: str, fetch=None):
        """Root-to-leaf chain; fetch only missing ancestors, never scan siblings."""
        with self._lock:
            nodes = self._read()
            changed, seen, chain = False, set(), []
            current = fid
            while current != '0':
                if current in seen or len(seen) >= 64:
                    raise RemoteError('父目录链无效，无法定位上级目录')
                seen.add(current)
                row = nodes.get(current)
                if row is None:
                    if fetch is None:
                        break
                    item = fetch(current)
                    if not is_directory(item) or valid_fid(item.get('fid')) != current:
                        raise RemoteError('父目录信息无效')
                    parent = valid_fid(item.get('pdir_fid'))
                    name = item.get('file_name')
                    if not isinstance(name, str) or not name:
                        raise RemoteError('目录名称无效')
                    row = nodes[current] = {'parent': parent, 'name': name}
                    changed = True
                chain.append({'fid': current, **row})
                current = row['parent']
            if changed:
                write_json(self.path, {'version': 1, 'items': nodes})
            return list(reversed(chain))

    def context(self, parent: str):
        chain = self.chain(parent)
        category = next((row['name'] for row in reversed(chain) if row['name'] in _CATEGORIES), '未分类')
        chosen = next((index for index in reversed(range(len(chain)))
                       if chain[index]['name'] not in _CONTAINERS and not _VERSION.match(chain[index]['name'])), None)
        if chosen is None:
            title = '我的网盘' if parent == '0' else '未命名剧集'
            series_fid, version = parent, ''
        else:
            title = chain[chosen]['name']
            series_fid = chain[chosen]['fid']
            version = '/'.join(row['name'] for row in chain[chosen + 1:])
        return {'parent': parent, 'parent_name': chain[-1]['name'] if chain else '',
                'series_fid': series_fid, 'series_name': title, 'category': category,
                'version': version, 'video_prefix': '/'.join(part for part in (title, version) if part)}
