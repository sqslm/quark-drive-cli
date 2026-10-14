"""Desktop selections over the existing, verified share protocols."""

from dataclasses import replace
import time

from ..errors import UsageError, QuarkError, AuthenticationError, RateLimitError, AccessRestrictedError
from ..downloader.core import describe_error
from ..paths import valid_fid
from ..shares import ShareSaver
from ..share_management import ShareManager, safe_operation_result
from .file_operations import FileOperations


class Sharing:
    def __init__(self, client):
        self.client = client
        self.manager = ShareManager(client)
        self.files = FileOperations(client)

    def link(self, share_id):
        record = self.manager._own_record(share_id)
        return safe_operation_result(self.manager._password(share_id, record))

    def revoke(self, entries, *, progress=None):
        if not entries:raise UsageError('请先选择本人分享')
        if progress:progress(f'正在核验 {len(entries)} 个本人分享…')
        records = {row['share_id']:row for row in self.manager._catalog()[0]}
        results, candidates, seen = [], [], set()
        for entry in entries:
            results.append({'share_id':entry.get('share_id'),'name':entry.get('title',''),'state':'failed','detail':''})
            try:
                share_id = valid_fid(entry.get('share_id'))
                if share_id in seen:raise UsageError('重复选择分享')
                seen.add(share_id)
                actual = records.get(share_id)
                if actual is None or any(actual.get(key) != entry.get(key) for key in ('pwd_id','title','share_url')):
                    raise UsageError('本人分享记录已变化，请刷新后选择')
                candidates.append(len(results)-1)
            except QuarkError as error:results[-1]['detail'] = describe_error(error,'取消分享预检')
        if not candidates:return results
        if progress:progress(f'正在批量取消 {len(candidates)} 个分享…')
        try:
            self.manager._call('post','share/delete',body={'share_ids':[results[index]['share_id'] for index in candidates]})
        except QuarkError as error:
            if getattr(error,'api_rejected',False) or isinstance(error,(AuthenticationError,RateLimitError,AccessRestrictedError)):
                for index in candidates:results[index].update(state='failed' if getattr(error,'api_rejected',False) else 'unknown',
                    detail=describe_error(error,'取消分享')+'；未重发，请刷新核实')
                return results
            # Resolve an ambiguous response only by complete read-only catalog
            # confirmation. Never repeat the delete request.
        pending = set(candidates)
        detail = '取消分享结果尚未确认；未重发，请刷新核实'
        for attempt in range(6):
            if progress:progress(f'正在核实取消结果，已确认 {len(candidates)-len(pending)}/{len(candidates)} 个…')
            try:remaining = {row['share_id'] for row in self.manager._catalog()[0]}
            except QuarkError as error:
                detail = describe_error(error,'取消分享核验')+'；未重发，请刷新核实';break
            for index in tuple(pending):
                if results[index]['share_id'] not in remaining:
                    results[index].update(state='done',detail='已取消分享',revoked=True);pending.remove(index)
            if not pending:break
            if attempt < 5:time.sleep(0.5)
        for index in pending:results[index].update(state='unknown',detail=detail)
        return results

    def create(self, entry, *, passcode=None, expire_days=7, progress=None):
        actual, _ = self.files.checked(entry)
        return self.manager.create(
            valid_fid(actual["fid"]),
            passcode=passcode,
            expire_days=expire_days,
            verify_tree=False,
            progress=progress,
        )

    def save(self, source, path, entries, target, *, progress=None):
        if not entries:
            raise UsageError("请先勾选分享文件")
        if progress:progress('正在核验分享成员和目标目录…')
        saver = ShareSaver(self.client)
        selection = saver.select(source.url, source=path, passcode=source.passcode)
        members = {str(row["fid"]): row for row in selection.items}
        ids = [valid_fid(row["fid"]) for row in entries]
        if len(set(ids)) != len(ids):
            raise UsageError("重复选择分享成员")
        chosen = []
        for entry, fid in zip(entries, ids):
            row = members.get(fid)
            if (
                row is None
                or row["file_name"] != entry["name"]
                or (row["file_type"] == 0) != entry["directory"]
                or (not entry["directory"] and row.get("size") != entry.get("size"))
            ):
                raise UsageError("分享成员已变化，请重新打开后选择")
            chosen.append(row)
        target = self.files.paths.resolve(str(target), directory=True).fid
        names = [row["file_name"].casefold() for row in chosen]
        if len(set(names)) != len(names):
            raise UsageError("选中分享成员存在同名内容，未转存")
        for row in chosen:
            self.files.available(target, row["file_name"])
        selection = replace(selection, items=chosen)
        if progress:progress('正在提交转存请求…')
        task_id = saver._submit_selection(selection, target, created=False)
        if progress:progress('已受理，正在等待服务端完成转存…')
        task = saver._poll_task(task_id, poll_interval=0.5, max_polls=120, immediate=True)
        if progress:progress('正在核验转存结果…')
        saver._confirm_saved_items(selection, target, task_id, task)
        return {
            "saved": True,
            "item_count": len(chosen),
            "destination_fid": target,
            "destination_path": self.files.paths.absolute_path(target),
        }
