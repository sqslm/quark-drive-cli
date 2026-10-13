"""Local folder bookmarks, isolated by the current credential context."""

from ..errors import LocalIOError, UsageError
from ..paths import valid_fid, validate_name
from ..state import read_json, write_json


class Favorites:
    def __init__(self, path):
        self.path = path

    def document(self):
        data = read_json(self.path, default={"version": 1, "accounts": {}})
        if data.get("version") != 1 or not isinstance(data.get("accounts"), dict):
            raise LocalIOError("常用文件夹记录格式无效，未覆盖原文件")
        for context, rows in data["accounts"].items():
            if not isinstance(context, str) or not isinstance(rows, list):
                raise LocalIOError("常用文件夹记录格式无效，未覆盖原文件")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    raise LocalIOError("常用文件夹记录格式无效，未覆盖原文件")
                valid_fid(row.get("fid"))
        return data

    def list(self, context):
        return self.document()["accounts"].get(context, [])

    def add(self, context, trail):
        if not isinstance(trail, list) or len(trail) < 2 or trail[0] != ["0", "我的网盘"]:
            raise UsageError("请选择有效的网盘目录")
        seen = set()
        for segment in trail:
            if not isinstance(segment, list) or len(segment) != 2:
                raise UsageError("目录路径无效")
            fid = valid_fid(segment[0])
            validate_name(segment[1])
            if fid in seen:
                raise UsageError("目录路径包含重复 FID")
            seen.add(fid)
        data = self.document()
        rows = data["accounts"].setdefault(context, [])
        row = {"fid": valid_fid(trail[-1][0]), "name": trail[-1][1]}
        rows[:] = [old for old in rows if old["fid"] != row["fid"]] + [row]
        write_json(self.path, data)
        return rows

    def remove(self, context, fid):
        data = self.document()
        rows = [row for row in data["accounts"].get(context, []) if row["fid"] != fid]
        data["accounts"][context] = rows
        write_json(self.path, data)
        return rows
