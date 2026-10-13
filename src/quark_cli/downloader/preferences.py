"""Only local window preferences; no account or task information is stored."""

from dataclasses import asdict, dataclass
from pathlib import Path

from ..errors import LocalIOError
from ..state import read_json, write_json
from ..transfer import DEFAULT_DOWNLOAD_CONNECTIONS, MAX_DOWNLOAD_CONNECTIONS


@dataclass(frozen=True)
class Preferences:
    output_dir: str = ""
    connections: int = DEFAULT_DOWNLOAD_CONNECTIONS
    width: int = 860
    height: int = 620
    limit_rate: int | None = None


def _validated(data: dict) -> Preferences:
    fields = {"version", "output_dir", "connections", "width", "height"}
    if type(data.get("version")) is int and data["version"] == 1 and set(data) == fields:
        data = {**data, "version": 2, "limit_rate": None}
    if set(data) != fields | {"limit_rate"} or type(data["version"]) is not int or data["version"] != 2:
        raise LocalIOError("下载器设置格式无效或版本不支持")
    folder = data["output_dir"]
    if not isinstance(folder, str) or "\0" in folder or (folder and not Path(folder).is_absolute()):
        raise LocalIOError("下载器保存目录设置无效")
    if type(data["connections"]) is not int or not 1 <= data["connections"] <= MAX_DOWNLOAD_CONNECTIONS:
        raise LocalIOError("下载器线程数设置无效")
    if any(type(data[key]) is not int or data[key] <= 0 for key in ("width", "height")):
        raise LocalIOError("下载器窗口尺寸设置无效")
    rate = data["limit_rate"]
    if rate is not None and (type(rate) is not int or not int(0.01 * 1048576) <= rate <= 10240 * 1048576):
        raise LocalIOError("下载器限速设置无效")
    return Preferences(folder, data["connections"], data["width"], data["height"], rate)


def load_preferences(path: Path) -> Preferences:
    return _validated(read_json(path, default={"version": 2, **asdict(Preferences())}))


def save_preferences(path: Path, preferences: Preferences) -> None:
    data = {"version": 2, **asdict(preferences)}
    _validated(data)
    write_json(path, data)
