"""Download one share selection without keeping a copy in the user's drive.

A share link is not a download source this project may request directly, so the
one selected item is transferred into a randomly named temporary folder at the
drive root first, downloaded from that account copy, and then deleted again —
the temporary folder *and* only that folder's recycle record — whether the
download succeeded, failed or was stopped.

The download itself never touches the durable transfer state of an ordinary
account download: it is staged, with its own resume ledger and part file, inside
a temporary directory in the output folder, and that whole staging area is
removed when the request ends. A stopped share download therefore continues
from nothing on the next attempt instead of resuming a local part that is bound
to the account FID cleanup is about to invalidate, while an ordinary account
download keeps its resumable checkpoint exactly as before.

Credential-bearing values — the share link, its passcode, the ``stoken``, the
per-item ``share_fid_token`` — are used in memory only and never reach a log,
an error message or the transfer state.
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..client import QuarkClient
from ..errors import (
    AccessRestrictedError,
    AuthenticationError,
    LocalIOError,
    QuarkError,
    RateLimitError,
    RemoteError,
    UsageError,
    contextual_error,
)
from ..files import Files
from ..paths import is_directory, valid_fid
from ..recursive import RecursiveTransfer
from ..shares import (
    _CREATE_READBACK_ATTEMPTS,
    _CREATE_READBACK_INTERVAL,
    ShareSelection,
    ShareSaver,
    _optional_created_fid,
)
from ..transfer import DEFAULT_DOWNLOAD_CONNECTIONS, MAX_DOWNLOAD_CONNECTIONS, DownloadStopped, Transfer, safe_transfer_name, _publish
from ..transfer_progress import ProgressEvent
from ..transfer_state import safe_local_path
from .core import ROOT_FID

_TEMP_PREFIX = ".quark-download-"
_POLL_INTERVAL = 0.5
_MAX_POLLS = 120
_RECYCLE_PAGE_SIZE = 100
_RECYCLE_MAX_PAGES = 100


@dataclass(frozen=True)
class ShareSource:
    """One share the window browses and downloads from.

    ``url`` is the direct ``https://pan.quark.cn/s/<id>`` link and ``passcode``
    is its extraction code; ``passcode=None`` means the code is taken from the
    link's optional ``pwd`` query. Both are kept out of ``repr`` so a stray
    traceback cannot print a working link.
    """

    url: str = field(repr=False)
    passcode: str | None = field(default=None, repr=False)


def _safe_detail(error: BaseException) -> str:
    """One readable line for a failure, without echoing a raw exception."""
    if isinstance(error, QuarkError):
        return str(error).strip() or "未知错误"
    if isinstance(error, OSError):
        return "本地文件操作失败"
    return "发生未预期的错误"


def _publish_tree(source: Path, target: Path) -> None:
    """Move a completed staged tree to its final name; never replace a target."""
    if target.exists() or target.is_symlink():
        raise UsageError("目标在下载期间被创建，未覆盖")
    try:
        os.rename(source, target)
    except OSError:
        # The staging area is removed with the request, so there is no local
        # checkpoint left to claim: say what actually happened instead.
        raise LocalIOError("无法发布下载目录；暂存内容已清理，请检查目标目录") from None


def _published_total(payload: Any, data: Any) -> int | None:
    """The service's own record count for a paged listing, when it publishes one."""
    total = None
    for source in (data, payload.get("metadata") if isinstance(payload, dict) else None):
        if not isinstance(source, dict):
            continue
        for key in ("_total", "total"):
            if key not in source:
                continue
            value = source[key]
            if type(value) is not int or value < 0 or (total is not None and value != total):
                raise RemoteError("回收站列表总数无效或不一致")
            total = value
    return total


class ShareDownloader:
    """Save one share selection into a temporary drive folder, download, clean up.

    The cloud copy exists only for the duration of one request: it is created
    under a random name, its identity is confirmed by reading the drive back,
    and cleanup removes only the FID this run verified it created.
    """

    def __init__(self, client: QuarkClient) -> None:
        self.client = client
        self.files = Files(client)
        self.saver = ShareSaver(client)

    def download(
        self,
        source: ShareSource,
        *,
        fid: str,
        name: str,
        target: str | Path,
        recursive: bool,
        share_path: str = "/",
        progress: Callable[[ProgressEvent], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        connections: int = DEFAULT_DOWNLOAD_CONNECTIONS,
        limit_rate: int | None = None,
    ) -> dict[str, Any]:
        """Download the one selected share item to ``target``, then clean up.

        Every cloud write is preceded by the local preflight: the target name,
        its parent folder and the absence of an existing target are checked
        before the temporary drive folder is created, and a stop that is already
        requested ends the request before any cloud write is made.
        """
        if type(connections) is not int or not 1 <= connections <= MAX_DOWNLOAD_CONNECTIONS:
            raise UsageError("下载并发连接数必须是 1 到 16 之间的整数")
        if limit_rate is not None and (type(limit_rate) is not int or limit_rate <= 0):
            raise UsageError("下载限速必须是正整数字节/秒")
        original = safe_transfer_name(name)
        destination = safe_local_path(target)
        if destination.name != original:
            raise RemoteError("下载目标与分享项目名称不符，已停止")
        if not destination.parent.is_dir():
            raise LocalIOError("下载目标目录不存在")
        if destination.exists() or destination.is_symlink():
            raise UsageError("目标已存在；分享下载不覆盖已有文件或目录")
        if should_stop is not None and should_stop():
            raise DownloadStopped("下载已停止：尚未转存任何数据")

        selected = self._select_one(source, share_path, fid, original, recursive)
        # A stop asked for while the share was being read must land before the
        # temporary folder is created, so a cancelled request writes nothing.
        self._stop_requested(should_stop, "下载已停止：尚未创建临时目录")
        temp_name = f"{_TEMP_PREFIX}{uuid.uuid4().hex}"
        self._emit(progress, "saving", original)

        temp_fid: str | None = None
        account_fid: str | None = None
        primary: BaseException | None = None
        result: dict[str, Any] | None = None
        try:
            temp_fid = self._create_temp_directory(temp_name)
            # The folder exists but nothing has been saved into it yet: a stop
            # here still skips the save and the cleanup below removes the folder.
            self._stop_requested(should_stop, "下载已停止：未提交转存")
            account_fid = self._save_selection(selected, temp_fid, original, recursive)
            result = self._transfer(account_fid, destination, original, recursive, progress, should_stop, connections, limit_rate)
        except BaseException as error:
            primary = error

        cleanup: BaseException | None = None
        if temp_fid is not None:
            self._emit(progress, "cleaning", original)
            try:
                self._clean_up(temp_fid, temp_name, original, account_fid, recursive)
            except BaseException as error:
                cleanup = error
        if cleanup is not None:
            raise self._cleanup_error(temp_fid, temp_name, cleanup, result is not None, primary) from cleanup
        if primary is not None:
            raise primary
        assert result is not None
        return result

    @staticmethod
    def _stop_requested(should_stop: Callable[[], bool] | None, message: str) -> None:
        """End the request here when the window has asked it to stop."""
        if should_stop is not None and should_stop():
            raise DownloadStopped(message)

    # -- selection and the temporary cloud copy ---------------------------------

    def _select_one(self, source: ShareSource, share_path: str, fid: str, name: str, recursive: bool) -> ShareSelection:
        """Re-read the share and keep exactly the requested child.

        The selection is matched by the exact FID from the listing the window
        showed, never by a name pattern: share item names may contain ``fnmatch``
        metacharacters, so a broad ``include`` could select a different item.
        """
        selection = self.saver.select(source.url, source=share_path, passcode=source.passcode)
        matches = [item for item in selection.items if item["fid"] == fid]
        if len(matches) != 1:
            raise RemoteError("分享中已找不到精确选中的项目，已停止转存")
        item = matches[0]
        if item["file_name"] != name:
            raise RemoteError("分享项目名称已变化，已停止转存")
        if is_directory(item) != recursive:
            raise RemoteError("分享项目类型与请求不符，已停止转存")
        return ShareSelection(selection.pwd_id, selection.source_fid, selection.source_path, [item], selection.stoken)

    def _create_temp_directory(self, name: str) -> str:
        """Create only this request's own temporary folder and prove it first.

        The name must be free at the drive root, the mkdir response must name
        exactly one FID, and the folder read back at the root must carry that
        same FID, the same name, the directory type and the root as its parent.
        A same-name folder this run cannot prove it created is never adopted and
        never deleted: the failure names the temporary folder instead.
        """
        self._require_absent_root_child(name)
        try:
            result = self.files.mkdir(name, ROOT_FID)
        except AuthenticationError:
            raise AuthenticationError(f"创建临时目录未获授权（{name}）；未提交转存") from None
        except Exception as original:
            raise contextual_error(original, f"创建临时目录失败（{name}）；未提交转存") from None
        created_fid = _optional_created_fid(result)
        if created_fid is None:
            raise RemoteError(
                f"创建临时目录的响应未给出唯一 FID（{name}）；未提交转存，且不会删除任何目录，请检查网盘根目录后手动处理"
            )
        try:
            created_fid = valid_fid(created_fid)
            return self._confirm_temp_directory(name, created_fid)
        except AuthenticationError:
            raise AuthenticationError(f"临时目录创建未获授权；未提交转存，请检查网盘根目录下的 {name}") from None
        except (RateLimitError, AccessRestrictedError) as original:
            raise contextual_error(original, f"无法确认新建临时目录（{name}）；未提交转存，请手动处理") from None
        except Exception as original:
            # The readback never proved that the FID the response named is the
            # folder at the root: report the concrete temporary name instead of
            # adopting a same-name folder this run cannot prove it created.
            raise contextual_error(original, f"无法确认临时目录创建结果（{name}）；未提交转存，请检查网盘根目录后手动处理") from None

    def _require_absent_root_child(self, name: str) -> None:
        """Refuse to create the temporary folder when the random name is taken."""
        try:
            children = self.saver._list_children(ROOT_FID)
        except AuthenticationError:
            raise AuthenticationError(f"读取网盘根目录未获授权（{name}）；未提交转存") from None
        except (RateLimitError, AccessRestrictedError):
            raise
        except Exception as original:
            raise contextual_error(original, f"读取网盘根目录失败（{name}）；未提交转存") from None
        if any(str(child.get("file_name")) == name for child in children):
            raise RemoteError(f"网盘根目录已存在同名目录（{name}）；未创建临时目录且不会删除它，请手动处理")

    def _confirm_temp_directory(self, name: str, created_fid: str) -> str:
        """Read the root back until the folder with ``created_fid`` is proven ours."""
        for attempt in range(_CREATE_READBACK_ATTEMPTS):
            if attempt:
                time.sleep(_CREATE_READBACK_INTERVAL)
            try:
                children = self.saver._list_children(ROOT_FID)
            except (RateLimitError, AccessRestrictedError):
                # A throttle/restriction must stop the readback, not retry it.
                raise
            except AuthenticationError:
                raise AuthenticationError(f"临时目录创建读回未获授权；未提交转存，请检查网盘根目录下的 {name}") from None
            except Exception:
                # Listing can lag or fail transiently immediately after mkdir.
                continue
            matches = [child for child in children if str(child.get("fid")) == created_fid]
            if not matches:
                continue
            if len(matches) != 1 or not self._is_own_temp(matches[0], created_fid, name):
                raise RemoteError(f"新建临时目录与创建结果不符（{name}，FID={created_fid}）；未提交转存，请手动处理")
            return created_fid
        raise RemoteError(f"新建临时目录读回超时，无法唯一确认（{name}）；未提交转存，请手动处理")

    @staticmethod
    def _is_own_temp(item: Any, temp_fid: str, name: str) -> bool:
        """Whether one root listing row is exactly the folder this run created."""
        return (
            str(item.get("fid")) == temp_fid
            and item.get("file_name") == name
            and is_directory(item)
            and str(item.get("pdir_fid")) == ROOT_FID
        )

    def _save_selection(self, selection: ShareSelection, temp_fid: str, name: str, recursive: bool) -> str:
        """Save the single selected item into the temporary folder, once."""
        task_id = self.saver._submit_selection(selection, temp_fid, created=True)
        try:
            task = self.saver._poll_task(task_id, poll_interval=_POLL_INTERVAL, max_polls=_MAX_POLLS)
            confirmed = self.saver._confirm_saved_items(selection, temp_fid, task_id, task)
        except QuarkError as error:
            error.task_id = task_id
            raise
        account_fid = confirmed[0]["fid"]
        # The share's own FID is a share-namespace identifier and is never the
        # account copy: the FID used for the download is the one read back from
        # the account folder, and its identity is confirmed in the account
        # namespace rather than inherited from the share.
        if account_fid in {item["fid"] for item in selection.items}:
            raise RemoteError(f"转存后的账号副本仍是分享 FID（task_id={task_id}）；未下载，请人工核对")
        info = self.files.info(account_fid)
        if (
            info.get("file_name") != name
            or is_directory(info) != recursive
            or str(info.get("pdir_fid")) != temp_fid
        ):
            raise RemoteError(f"转存后的账号副本与分享项目不符（task_id={task_id}）；请人工核对")
        return account_fid

    # -- the staged local download ----------------------------------------------

    def _transfer(
        self,
        account_fid: str,
        destination: Path,
        name: str,
        recursive: bool,
        progress: Callable[[ProgressEvent], None] | None,
        should_stop: Callable[[], bool] | None,
        connections: int,
        limit_rate: int | None,
    ) -> dict[str, Any]:
        """Download the account copy inside a temporary staging area, then publish.

        The staging directory sits in the output folder, so publishing is a
        rename on the same filesystem. Both the resume ledger and the part file
        live inside it and are discarded with the rest of the staging area: no
        local checkpoint survives that would point at the temporary cloud FID
        that cleanup removes.
        """
        staging = tempfile.TemporaryDirectory(prefix=_TEMP_PREFIX, dir=destination.parent)
        try:
            root = Path(staging.name)
            state_dir = root / "state"
            payload_dir = root / "payload"
            payload_dir.mkdir()
            staged = payload_dir / name
            options: dict[str, Any] = {"resume": True, "overwrite": False, "connections": connections}
            if limit_rate is not None:
                options["limit_rate"] = limit_rate
            if progress is not None:
                options["progress"] = progress
            if should_stop is not None:
                options["should_stop"] = should_stop
            transfer = Transfer(self.client, state_dir=state_dir)
            if recursive:
                result = RecursiveTransfer(self.client, transfer=transfer, state_dir=state_dir).download_tree(
                    account_fid, staged, **options
                )
                _publish_tree(staged, destination)
            else:
                result = transfer.download(account_fid, staged, **options)
                _publish(staged, destination, False)
            result["path"] = str(destination)
            return result
        finally:
            staging.cleanup()

    # -- cloud cleanup ----------------------------------------------------------

    def _clean_up(self, temp_fid: str, temp_name: str, name: str, account_fid: str | None, recursive: bool) -> None:
        """Delete this run's temporary folder and only that folder's recycle record.

        The folder is proven to still be the one this run created (exact FID,
        name, type and root parent) and to hold nothing but this run's own saved
        copy before anything is deleted; a renamed, moved or foreign folder is
        refused and named instead of being removed.
        """
        self._require_temp_identity(temp_fid, temp_name)
        self._confirm_temp_contents(temp_fid, temp_name, name, account_fid, recursive)
        try:
            response = self.files.delete(temp_fid)
        except AuthenticationError:
            raise AuthenticationError(f"临时目录删除未获授权（{temp_name}）；请手动删除") from None
        except Exception as original:
            raise contextual_error(original, f"临时目录删除失败（{temp_name}）；请手动删除") from None
        task_id = response.get("task_id") if isinstance(response, dict) else None
        if isinstance(task_id, (str, int)) and not isinstance(task_id, bool) and str(task_id).strip():
            try:
                self.saver._poll_task(str(task_id), poll_interval=_POLL_INTERVAL, max_polls=_MAX_POLLS)
            except QuarkError:
                raise RemoteError(f"临时目录删除任务未确认完成（{temp_name}，task_id={task_id}）；请手动处理") from None
        self._confirm_gone(temp_fid, temp_name)

        # Ordinary deletion enters the recycle bin, whose listing can lag.
        # Wait for this directory's record before treating an empty read as gone.
        record_ids: list[str] = []
        for attempt in range(_CREATE_READBACK_ATTEMPTS):
            if attempt:
                time.sleep(_CREATE_READBACK_INTERVAL)
            record_ids = self._recycle_record_ids(temp_fid)
            if record_ids:
                break
        if not record_ids:
            # Not in the drive and not in the recycle bin: nothing to remove.
            self._confirm_gone(temp_fid, temp_name)
            return
        if len(record_ids) > 1:
            raise RemoteError(f"回收站存在多条临时目录记录（{temp_name}）；未自动清理，请手动处理")
        try:
            self.client.post("file/recycle/remove", body={"select_mode": 2, "record_list": record_ids})
        except AuthenticationError:
            raise AuthenticationError(f"清理回收站记录未获授权（{temp_name}）；请手动清理") from None
        except Exception as original:
            raise contextual_error(original, f"清理回收站记录失败（{temp_name}）；请手动清理") from None
        # The recycle listing can lag behind the removal; poll read-only until it
        # catches up. The removal itself is issued exactly once.
        for attempt in range(_CREATE_READBACK_ATTEMPTS):
            if attempt:
                time.sleep(_CREATE_READBACK_INTERVAL)
            if not self._recycle_record_ids(temp_fid):
                break
        else:
            raise RemoteError(f"临时目录的回收站记录仍然存在（{temp_name}）；请手动清理")
        self._confirm_gone(temp_fid, temp_name)

    def _require_temp_identity(self, temp_fid: str, temp_name: str) -> None:
        """Prove the root still holds exactly the folder this run created."""
        try:
            children = self.saver._list_children(ROOT_FID)
        except AuthenticationError:
            raise AuthenticationError(f"确认临时目录身份未获授权（{temp_name}）；未删除，请手动处理") from None
        except (RateLimitError, AccessRestrictedError):
            raise
        except Exception as original:
            raise contextual_error(original, f"读取网盘根目录失败，无法确认临时目录身份（{temp_name}）；未删除，请手动处理") from None
        matches = [child for child in children if str(child.get("fid")) == temp_fid]
        if len(matches) != 1 or not self._is_own_temp(matches[0], temp_fid, temp_name):
            raise RemoteError(f"临时目录身份不符，拒绝删除（{temp_name}，FID={temp_fid}）；请手动处理")

    def _confirm_temp_contents(
        self, temp_fid: str, temp_name: str, name: str, account_fid: str | None, recursive: bool
    ) -> None:
        """Refuse to delete a temporary folder that holds anything but our own copy."""
        try:
            children = self.saver._list_children(temp_fid)
        except AuthenticationError:
            raise AuthenticationError(f"确认临时目录内容未获授权（{temp_name}）；未删除，请手动处理") from None
        except (RateLimitError, AccessRestrictedError):
            raise
        except Exception as original:
            raise contextual_error(original, f"无法确认临时目录内容（{temp_name}）；未删除，请手动处理") from None
        if account_fid is None and not children:
            return
        # A save may have copied the item before its final task/info read failed.
        # Its sole matching child still belongs to the new temporary directory.
        wrong = len(children) != 1
        if not wrong:
            child = children[0]
            wrong = (
                (account_fid is not None and str(child.get("fid")) != account_fid)
                or child.get("file_name") != name
                or is_directory(child) != recursive
                or str(child.get("pdir_fid")) != temp_fid
            )
        if wrong:
            raise RemoteError(f"临时目录内容与本次转存不符，拒绝删除（{temp_name}）；请手动处理")

    def _confirm_gone(self, temp_fid: str, temp_name: str) -> None:
        """Read the drive root back until the temporary folder is gone."""
        for attempt in range(_CREATE_READBACK_ATTEMPTS):
            if attempt:
                time.sleep(_CREATE_READBACK_INTERVAL)
            try:
                children = self.saver._list_children(ROOT_FID)
            except AuthenticationError:
                raise AuthenticationError(f"确认临时目录删除未获授权（{temp_name}）；请手动处理") from None
            except (RateLimitError, AccessRestrictedError):
                raise
            except Exception:
                # A listing can lag right after a delete task finished.
                continue
            if not any(str(child.get("fid")) == temp_fid for child in children):
                return
        raise RemoteError(f"临时目录仍在我的网盘（{temp_name}）；删除未确认，请手动处理")

    def _recycle_record_ids(self, temp_fid: str) -> list[str]:
        """The recycle records of exactly this FID; every page is read and validated."""
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        expected_total: int | None = None
        for page in range(1, _RECYCLE_MAX_PAGES + 1):
            payload = self.client.get(
                "file/recycle/list", params={"_page": page, "_size": _RECYCLE_PAGE_SIZE}
            )
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise RemoteError("回收站列表响应缺少 data.list")
            batch = data["list"]
            if any(not isinstance(record, dict) for record in batch):
                raise RemoteError("回收站列表包含无效记录")
            for record in batch:
                record_id = record.get("record_id")
                if (
                    not isinstance(record_id, (str, int))
                    or isinstance(record_id, bool)
                    or not str(record_id).strip()
                ):
                    raise RemoteError("回收站记录缺少 record_id")
                if str(record_id) in seen:
                    raise RemoteError("回收站列表包含重复记录")
                valid_fid(record.get("fid"))
                seen.add(str(record_id))
            records.extend(batch)
            total = _published_total(payload, data)
            if total is not None:
                if expected_total is not None and total != expected_total:
                    raise RemoteError("回收站分页总数不一致")
                expected_total = total
            total = expected_total
            if total is not None and total < len(records):
                raise RemoteError("回收站列表总数与记录数不一致")
            if len(batch) < _RECYCLE_PAGE_SIZE:
                # A short page is only the end when the service either published
                # no total or a total this read already accounted for.
                if total is not None and total > len(records):
                    raise RemoteError("回收站列表提前结束，未取到全部记录")
                break
        else:
            raise RemoteError("回收站列表分页超过安全上限")
        return [str(record["record_id"]) for record in records if str(record.get("fid", "")) == temp_fid]

    # -- reporting --------------------------------------------------------------

    @staticmethod
    def _emit(progress: Callable[[ProgressEvent], None] | None, phase: str, name: str) -> None:
        """Announce a transfer/cleanup phase without ever breaking the cleanup."""
        if progress is None:
            return
        try:
            progress(ProgressEvent("download", phase, name, 0, 0, None, 0.0))
        except Exception:
            # A window callback must never stop the cleanup it was reporting on.
            pass

    @staticmethod
    def _cleanup_error(
        temp_fid: str,
        temp_name: str,
        cleanup: BaseException,
        completed: bool,
        primary: BaseException | None,
    ) -> RemoteError:
        """One line naming the leftover temporary folder and saying what else happened."""
        parts = [f"临时目录清理失败（{temp_name}，FID={temp_fid}）：{_safe_detail(cleanup)}"]
        if completed:
            parts.append("本地文件已下载完成")
        if primary is not None:
            detail = "下载已停止，需重新下载" if isinstance(primary, DownloadStopped) else _safe_detail(primary)
            parts.append(f"本次结果：{detail}")
        parts.append("请手动删除网盘根目录下的临时目录")
        return RemoteError("；".join(parts))
