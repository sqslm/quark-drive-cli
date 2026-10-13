"""Private task records; transfer checkpoints remain owned by the transfer engine."""

from dataclasses import asdict, dataclass
from pathlib import Path

from ..errors import LocalIOError
from ..state import read_json, write_json
from ..shares import parse_share_url
from ..downloader.worker import Download


@dataclass(frozen=True)
class Upload:
    source: str
    parent: str
    recursive: bool
    name: str
    limit_rate: int | None = None


class TaskStore:
    def __init__(self, path):
        self.path = path
        self.writable = True
        self.warning = ""

    def load(self):
        try:
            document = read_json(self.path, default={"version": 1, "tasks": []})
            if (
                set(document) != {"version", "tasks"}
                or type(document["version"]) is not int
                or document["version"] != 1
                or not isinstance(document["tasks"], list)
            ):
                raise LocalIOError("任务记录格式或版本无效")
            rows, seen = [], set()
            fields = {
                "id",
                "name",
                "state",
                "detail",
                "target",
                "revision",
                "context",
                "direction",
                "request",
                "share_url",
            }
            for record in document["tasks"]:
                if (
                    not isinstance(record, dict)
                    or set(record) != fields
                    or record["id"] in seen
                ):
                    raise LocalIOError("任务记录包含无效或重复项目")
                seen.add(record["id"])
                if any(
                    not isinstance(record[key], str)
                    for key in (
                        "id",
                        "name",
                        "state",
                        "detail",
                        "target",
                        "context",
                        "direction",
                        "share_url",
                    )
                ):
                    raise LocalIOError("任务记录字段无效")
                if (
                    record["state"]
                    not in (
                        "waiting",
                        "running",
                        "stopped",
                        "done",
                        "failed",
                        "cancelled",
                    )
                    or type(record["revision"]) is not int
                ):
                    raise LocalIOError("任务状态无效")
                if record["direction"] == "download":
                    request = Download(**record["request"])
                    if (
                        not Path(request.target).is_absolute()
                        or request.overwrite
                        or request.share_source is not None
                    ):
                        raise LocalIOError("下载任务目标无效")
                elif record["direction"] == "upload":
                    request = Upload(**record["request"])
                    if not Path(request.source).is_absolute():
                        raise LocalIOError("上传任务来源无效")
                else:
                    raise LocalIOError("任务方向无效")
                if request.name != record["name"]:
                    raise LocalIOError("任务名称与请求不一致")
                if record["share_url"]:
                    parse_share_url(record["share_url"])
                rows.append(
                    {
                        **record,
                        "request": request,
                        "state": "stopped"
                        if record["state"] == "running"
                        else record["state"],
                        "detail": "运行中断，继续前将核验检查点"
                        if record["state"] == "running"
                        else record["detail"],
                    }
                )
            return rows
        except Exception:
            self.writable = False
            self.warning = "任务记录无法读取，本次队列仅在窗口内有效，未覆盖原记录"
            return []

    def save(self, tasks):
        if not self.writable:
            return False
        records = []
        for task in tasks:
            record = {
                key: task[key]
                for key in (
                    "id",
                    "name",
                    "state",
                    "detail",
                    "target",
                    "revision",
                    "context",
                    "direction",
                    "share_url",
                )
            }
            request = asdict(task["request"])
            # No passcode, share token or URL query is serialized from ShareSource.
            request.pop("share_source", None)
            record["request"] = request
            records.append(record)
        try:
            write_json(self.path, {"version": 1, "tasks": records})
            return True
        except LocalIOError:
            self.writable = False
            self.warning = (
                "任务记录无法保存，本次队列仅在窗口内有效，检查点仍由传输引擎保存"
            )
            return False
