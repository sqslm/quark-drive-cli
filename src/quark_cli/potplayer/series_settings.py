"""Account-local intro/outro rules, keyed by verified series directory FID."""
from pathlib import Path
import threading

from ..errors import LocalIOError, RemoteError, UsageError
from ..paths import valid_fid
from ..state import read_json, write_json


def seconds(value):
    if type(value) is not int or not 0 <= value <= 3600:
        raise UsageError('片头和片尾须为 0–3600 的整数秒；0 表示关闭')
    return value


class SeriesSettings:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _read(self):
        document = read_json(self.path, default={'version': 1, 'series': {}})
        try:
            if not isinstance(document, dict) or set(document) != {'version', 'series'} or document['version'] != 1:
                raise ValueError()
            if not isinstance(document['series'], dict):
                raise ValueError()
            for fid, rule in document['series'].items():
                if valid_fid(fid) == '0' or not isinstance(rule, dict) or set(rule) != {'intro_seconds', 'outro_seconds'}:
                    raise ValueError()
                seconds(rule['intro_seconds'])
                seconds(rule['outro_seconds'])
        except (ValueError, RemoteError, UsageError):
            raise LocalIOError('片头片尾设置格式无效，不会覆盖原文件') from None
        return document

    def get(self, series):
        with self._lock:
            return dict(self._read()['series'].get(series, {'intro_seconds': 0, 'outro_seconds': 0}))

    def save(self, series, intro, outro):
        if valid_fid(series) == '0':
            raise UsageError('请为具体剧集设置片头片尾')
        rule = {'intro_seconds': seconds(intro), 'outro_seconds': seconds(outro)}
        with self._lock:
            document = self._read()
            if intro == outro == 0:
                document['series'].pop(series, None)
            else:
                document['series'][series] = rule
            write_json(self.path, document)
        return rule
