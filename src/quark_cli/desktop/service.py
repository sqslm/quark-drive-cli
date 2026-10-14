"""Explicit business methods over JSON lines; credentials never leave Python."""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import threading
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable

from ..auth import QRLogin, LoginCancelled, get_cookie, load_saved_cookie, save_cookie, logout
from ..errors import QuarkError, UsageError, AuthenticationError
from ..state import config_directory, read_json, write_json
from ..transfer import DownloadStopped, Transfer
from ..recursive import RecursiveTransfer
from ..paths import RemotePaths, valid_fid
from ..files import Files
from ..shares import parse_share_url
from ..downloader.core import Entry, EXTENSION_GROUPS, describe_error, download_plan, progress_text, summary_text
from ..downloader.preferences import Preferences, _validated, load_preferences, save_preferences
from ..downloader.share import ShareSource
from ..downloader.share_input import parse_share_input
from ..downloader.search import SearchCancelled
from ..downloader.worker import Download, DriveBackend, Search
from .file_operations import FileOperations
from .tasks import TaskStore, Upload
from .sharing import Sharing
from .recycle import Recycle
from .favorites import Favorites
from ..share_management import safe_operation_result


class DesktopService:
    def __init__(self, send: Callable[[dict], None], *, backend=None, directory=None):
        self.send = send
        self.directory = Path(directory) if directory is not None else config_directory() / "downloader"
        self.preferences_path = self.directory / "preferences.json"
        self.saved_cookie = None
        self.session_path = self.directory / "session.json"
        self.session_warning = ""
        try:
            session = read_json(self.session_path, default={"version":1,"logged_out":False,"use_saved":False})
            if session.get('version') != 1 or type(session.get('logged_out')) is not bool or type(session.get('use_saved')) is not bool:
                raise UsageError('账号会话记录格式无效，请重新登录')
            self.logged_out = session['logged_out']
            self.use_saved = session['use_saved']
        except QuarkError as error:
            self.logged_out, self.use_saved = True, False
            self.session_warning = describe_error(error,'读取账号状态')
        self.web_login = None
        self.backend = backend or DriveBackend(self.cookie)
        self.reads = ThreadPoolExecutor(2, thread_name_prefix="quark-read")
        self.transfers = ThreadPoolExecutor(1, thread_name_prefix="quark-transfer")
        self.lock = threading.RLock()
        self.writes = threading.Lock()
        self.search_cancel = threading.Event()
        self.login_cancel = threading.Event()
        self.stop = threading.Event()
        self.task_store = TaskStore(self.directory / "tasks.json")
        self.tasks: list[dict] = self.task_store.load()
        for task in self.tasks:
            task["valid"] = False
            task["_cancel"] = threading.Event()
        self.pending_count = 0
        self.batch_id = None
        self.batch_tasks = []
        self.active_id = None
        self.sources: dict[str, ShareSource] = {}
        self.busy = False
        self.logging_in = False
        self.managing = False
        self.cut = None
        self.favorites = Favorites(self.directory / "favorites.json")
        self.closed = False
        self.preferences_warning = ""
        self.preferences_writable = True
        self.preferences = None

    def current_preferences(self):
        if self.preferences is None:
            try:
                self.preferences = load_preferences(self.preferences_path)
            except QuarkError as error:
                self.preferences = Preferences()
                self.preferences_writable = False
                self.preferences_warning = describe_error(error, "读取设置")
        return self.preferences

    def cookie(self):
        if self.logged_out:
            raise AuthenticationError('已退出本客户端，请重新登录')
        if self.use_saved and not self.saved_cookie:
            saved = load_saved_cookie()
            if not saved:
                raise AuthenticationError('本客户端的登录凭证已失效，请重新登录')
            return saved
        return self.saved_cookie or get_cookie()

    def event(self, identifier, event, data):
        self.send({"id": identifier, "event": event, "data": data})

    def accept(self, message):
        identifier = message.get("id") if isinstance(message, dict) else None
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            self.send({"id": None, "error": "请求 ID 无效"})
            return
        method, params = message.get("method"), message.get("params", {})
        if method not in METHODS or not isinstance(params, dict):
            self.send({"id": identifier, "error": "不支持的请求"})
            return
        if self.closed:
            self.send({"id": identifier, "error": "客户端正在退出，不再接收新任务"})
            return
        if method == "search.cancel":
            self.search_cancel.set()
        elif method == "login.cancel":
            self.login_cancel.set()
        elif method == "download.stop":
            self.stop.set()
        if method in ("search.cancel", "login.cancel", "download.stop"):
            self.send({"id": identifier, "result": True})
            return
        if method == "search":
            self.search_cancel.set()
            self.search_cancel = threading.Event()
            cancel = self.search_cancel
        elif method == "login":
            with self.lock:
                if self.busy or self.logging_in or self.managing:
                    self.send({"id": identifier, "error": "请先结束传输或当前登录"})
                    return
                self.logging_in = True
                self.cut = None
            self.login_cancel.set()
            self.login_cancel = threading.Event()
            cancel = self.login_cancel
        else:
            cancel = None
        self.reads.submit(self.execute, identifier, method, params, cancel)

    def execute(self, identifier, method, params, cancel=None):
        try:
            result = self.dispatch(identifier, method, params, cancel)
            self.send({"id": identifier, "result": result})
        except SearchCancelled:
            self.send({"id": identifier, "result": {"cancelled": True}})
        except LoginCancelled:
            self.send({"id": identifier, "result": {"cancelled": True}})
        except Exception as error:
            self.send({"id": identifier, "error": describe_error(error, "操作")})
        finally:
            if method == "login":
                with self.lock:
                    self.logging_in = False

    def dispatch(self, identifier, method, p, cancel):
        if self.closed:
            raise UsageError('客户端正在退出，不再接收新操作')
        if method == 'logout':
            with self.lock:
                if self.busy or self.logging_in or self.managing:
                    raise UsageError('请先结束传输、登录或文件操作，再退出账号')
            with self.writes, self.lock:
                if self.busy or self.logging_in or self.managing:
                    raise UsageError('请先结束传输、登录或文件操作，再退出账号')
                write_json(self.session_path, {'version':1,'logged_out':True,'use_saved':False})
                self.logged_out = True
                self.saved_cookie = None
                self.use_saved = False
                self.clear_account_context()
                warning = ''
                try:
                    logout()
                except QuarkError as error:
                    warning = '已退出本客户端，但本地凭证清理失败：' + describe_error(error,'清理凭证')
                return {'logged_in':False,'nickname':'未登录','warning':warning}
        if method == 'login.web.begin':
            with self.lock:
                if self.busy or self.logging_in or self.managing:
                    raise UsageError('请先结束传输或当前账号操作')
                self.logging_in = True
                self.cut = None
                self.web_login = uuid.uuid4().hex
                self.search_cancel.set()
                return {'token':self.web_login}
        if method == 'login.web.cancel':
            with self.lock:
                if self.web_login is not None and self.web_login == p.get('token'):
                    self.web_login = None
                    self.logging_in = False
                return True
        if method == 'login.web.finish':
            with self.writes:
                with self.lock:
                    if not self.web_login or self.web_login != p.get('token'):
                        raise LoginCancelled('网页登录已取消，未保存凭证')
                candidate = p.get('cookie')
                if not isinstance(candidate,str) or not candidate.strip() or '\r' in candidate or '\n' in candidate:
                    raise AuthenticationError('官方网页登录未返回有效凭证')
                nickname = self.backend.verify_login(candidate)
                with self.lock:
                    if self.web_login != p.get('token'):
                        raise LoginCancelled('网页登录已取消，未保存凭证')
                    save_cookie(candidate)
                    result = self.activate_login(candidate,nickname)
                    self.web_login = None
                    self.logging_in = False
                    return result
        if self.logged_out and method not in ('hello','account','preferences.save','tasks','tasks.clear','tasks.cancel','share.parse','login','favorites.list'):
            raise AuthenticationError('已退出本客户端，请重新登录')
        if method.startswith('favorites.'):
            if method == 'favorites.list' and self.logged_out:
                return []
            if p.get('handle'):
                raise UsageError('分享目录不能设为账号常用文件夹')
            with self.lock:
                if self.logging_in:
                    raise UsageError('请等待登录结束')
                context = self.context()
                if method == 'favorites.list':
                    return self.favorites.list(context)
                if method == 'favorites.add':
                    return self.favorites.add(context, p['trail'])
                if method == 'favorites.remove':
                    return self.favorites.remove(context, valid_fid(p['fid']))
            if method == 'favorites.open':
                fid = valid_fid(p['fid'])
                if not any(row['fid'] == fid for row in self.favorites.list(context)):
                    raise UsageError('常用文件夹已移除或登录已变化')
                with self.backend._client(interactive=True) as client:
                    paths = FileOperations(client).paths
                    chain = paths.ancestry(fid)
                    from ..paths import is_directory
                    if not is_directory(chain[0].item):
                        raise UsageError('常用位置已不是目录，请移除该记录')
                    trail = [['0','我的网盘']] + [[obj.fid,obj.item['file_name']] for obj in reversed(chain[:-1])]
                with self.lock:
                    if context != self.context() or self.logging_in:
                        raise UsageError('登录已变化，请重新打开常用文件夹')
                    if not any(row['fid'] == fid for row in self.favorites.list(context)):
                        raise UsageError('常用文件夹已移除')
                    self.favorites.add(context, trail)
                return trail
        if method == 'file.cut':
            if p.get('handle'):
                raise UsageError('分享文件只读，不能剪切')
            with self.lock:
                if self.busy or self.logging_in or self.managing:
                    raise UsageError('请先结束传输、登录或网盘操作')
                entries = [asdict(self.entry(row)) for row in p.get('entries',[])]
                ids = [valid_fid(row['fid']) for row in entries]
                if not ids or '0' in ids or len(set(ids)) != len(ids):
                    raise UsageError('请选择有效且不重复的文件或目录')
                self.cut = {'id':uuid.uuid4().hex,'entries':entries,'context':self.context(),'blocked':False}
                return self.cut_view()
        if method == 'file.clearCut':
            with self.lock:
                if self.managing:
                    raise UsageError('网盘操作正在执行，请等待结果后取消剪切')
                if self.cut and self.cut['id'] == p.get('id'):
                    self.cut = None
                return self.cut_view()
        if method == 'file.paste':
            if p.get('handle'):
                raise UsageError('分享文件只读，不能粘贴')
            with self.mutation():
                clipboard = self.cut
                if clipboard is None or clipboard['id'] != p.get('id') or clipboard['context'] != self.context():
                    raise UsageError('剪切记录已失效或登录已变化，请重新剪切')
                if clipboard['blocked']:
                    raise UsageError('上次粘贴结果未确认，请先刷新核实，未重复提交')
                # Reserve this intent before opening any metadata or write request.
                clipboard['blocked'] = True
                with self.backend._client(interactive=True) as client:
                    try:
                        results = FileOperations(client).paste(clipboard['entries'],p['target'])
                    except QuarkError:
                        # paste reports write outcomes; an exception here is
                        # target preflight, before any move was submitted.
                        clipboard['blocked'] = False
                        raise
                done = {row['fid'] for row in results if row['state'] == 'done'}
                clipboard['entries'] = [row for row in clipboard['entries'] if row['fid'] not in done]
                clipboard['blocked'] = any(row['state'] == 'unknown' for row in results)
                if not clipboard['entries']:
                    self.cut = None
                return {'results':results,'clipboard':self.cut_view()}
        if method in ('share.list','share.link','recycle.list'):
            with self.backend._client() as client:
                if method == 'recycle.list':
                    return Recycle(client).listing()
                sharing = Sharing(client)
                if method == 'share.list':
                    return sharing.manager.list_own(all_pages=True)
                return sharing.link(p['share_id'])
        if method in ('share.create','share.revoke','share.save','recycle.restore','recycle.remove'):
            with self.mutation(), self.backend._client() as client:
                if method.startswith('recycle.'):
                    return Recycle(client).apply(method.split('.')[1], p['entries'])
                sharing = Sharing(client)
                if method == 'share.save':
                    source = self.sources[p['handle']]
                    try:
                        return sharing.save(source, str(p.get('path','/')), p['entries'], p['target'])
                    except UsageError:
                        raise
                    except QuarkError as error:
                        return {'saved':False,'result':'unknown','detail':describe_error(error,'永久转存') + '；结果未确认，未自动重发，请检查目标目录'}
                if p.get('handle'):
                    raise UsageError('分享浏览不能创建或取消账户分享')
                if method == 'share.create':
                    try:
                        return sharing.create(p['entry'], passcode=p.get('passcode') or None, expire_days=p.get('expire_days',7),
                            progress=lambda text: self.event(identifier,'share.progress',text))
                    except QuarkError as error:
                        evidence = safe_operation_result(getattr(error,'share_evidence',{}))
                        if not evidence:
                            raise
                        return {**evidence, 'detail':describe_error(error,'创建分享')}
                results = []
                seen = set()
                for entry in p['entries']:
                    share_id = entry['share_id']
                    try:
                        if share_id in seen:
                            raise UsageError('重复选择分享')
                        seen.add(share_id)
                        current = sharing.manager._own_record(share_id)
                        if any(current.get(key) != entry.get(key) for key in ('pwd_id','title','share_url')):
                            raise UsageError('本人分享记录已变化，请刷新后选择')
                        result = sharing.manager.revoke(share_id)
                        results.append({**result,'name':entry.get('title',share_id),'state':'done','detail':'已取消分享'})
                    except QuarkError as error:
                        evidence = safe_operation_result(getattr(error,'share_evidence',{}))
                        results.append({**evidence,'share_id':share_id,'name':entry.get('title',share_id),
                                        'state':'unknown' if evidence.get('result') == 'unknown' else 'failed',
                                        'detail':describe_error(error,'取消分享')})
                return results
        if method.startswith("file."):
            if p.get("handle"):
                raise UsageError("分享文件只读，不能修改")
            with self.mutation() if method != 'file.info' else self.writes:
                with self.lock:
                    if (self.busy or self.logging_in) and method != "file.info":
                        raise UsageError("请先停止传输再修改网盘文件")
                with self.backend._client(interactive=True) as client:
                    operations = FileOperations(client)
                    if method == "file.info":
                        return operations.info(p["entry"])
                    if method == "file.mkdir":
                        return operations.mkdir(str(p.get("parent", "0")), str(p.get("name", "")))
                    return operations.apply(method[5:], p["entries"], name=p.get("name"), target=p.get("target"))
        if method == "hello":
            try:
                self.cookie()
                signed_in = True
            except QuarkError:
                signed_in = False
            return {"protocol": 1, "preferences": asdict(self.current_preferences()), "extension_groups": EXTENSION_GROUPS,
                    "signed_in":signed_in,
                    "warning": "; ".join(value for value in (self.session_warning,self.preferences_warning, self.task_store.warning) if value)}
        if method == "account":
            try:
                self.cookie()
            except AuthenticationError:
                return {'nickname':'未登录','logged_in':False}
            return {"nickname": self.backend.account_nickname(),'logged_in':True}
        if method == "browse":
            return [asdict(row) for row in self.backend.listing(str(p.get("fid", "0")))]
        if method == "locate":
            return asdict(self.backend.locate(self.entry(p["entry"])))
        if method == "search":
            request = Search(str(p.get("fid", "0")), str(p.get("query", "")), recursive=bool(p.get("recursive", False)))
            self.backend.search_stream(request, lambda rows, scanned: self.event(identifier, "search.page",
                {"entries": [asdict(row) for row in rows], "scanned": scanned}) if not cancel.is_set() else None, cancel.is_set)
            return {"cancelled": cancel.is_set()}
        if method == "share.parse":
            source = parse_share_input(str(p.get("text", "")))
            return {"url": source.url, "passcode": source.passcode or ""}
        if method == "share.open":
            source = parse_share_input(str(p.get("text", "")), passcode=str(p.get("passcode", "")) or None)
            handle = uuid.uuid4().hex
            rows = self.backend.share_listing(source, "/")
            self.sources[handle] = source
            return {"handle": handle, "entries": [asdict(row) for row in rows]}
        if method == "share.browse":
            source = self.sources[p["handle"]]
            return [asdict(row) for row in self.backend.share_listing(source, str(p.get("path", "/")))]
        if method == "preferences.save":
            self.current_preferences()
            if self.busy:
                raise UsageError("传输期间不能修改下载设置")
            preferences = Preferences(**p)
            _validated({"version": 2, **asdict(preferences)})
            self.preferences = preferences
            persisted = False
            warning = ""
            if self.preferences_writable:
                try:
                    save_preferences(self.preferences_path, preferences)
                    persisted = True
                except QuarkError as error:
                    warning = describe_error(error, "保存设置")
            else:
                warning = "设置文件损坏或不可访问：本次设置仅在窗口内有效，未覆盖原文件"
            return {"preferences": asdict(preferences), "persisted": persisted, "warning": warning}
        if method == "login":
            if cancel.is_set():
                return {"cancelled": True}
            self.search_cancel.set()
            def qr(path):
                self.event(identifier, "login.qr", "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii"))
            QRLogin(cancelled=cancel.is_set).run(on_qr_ready=qr)
            candidate = load_saved_cookie()
            if not candidate:
                raise AuthenticationError('扫码登录凭证缺失，请重新登录')
            self.saved_cookie = candidate
            self.logged_out = False
            try:
                nickname = self.backend.account_nickname()
            except Exception:
                nickname = "昵称暂不可用"
            return self.activate_login(candidate,nickname)
        if method == "upload":
            with self.lock:
                if self.logging_in or self.managing or (self.busy and self.stop.is_set()):
                    raise UsageError("请等待登录或停止操作结束")
            context = self.context()
            preferences = self.current_preferences()
            sources = [Path(value).absolute() for value in p["sources"]]
            parent = str(p.get("parent", "0"))
            tasks = []
            with self.backend._client() as client:
                paths = RemotePaths(Files(client))
                parent = paths.resolve(parent, directory=True).fid
                target = paths.absolute_path(parent).rstrip("/")
                operations = FileOperations(client)
                for source in sources:
                    if not source.exists():
                        raise UsageError("上传来源不存在")
                    request = Upload(str(source), valid_fid(parent), source.is_dir(), source.name, preferences.limit_rate)
                    # New directory uploads never merge into an unrelated existing folder.
                    if request.recursive:
                        transfer = RecursiveTransfer(client)
                        _, state_path, _ = transfer._state("upload", str(source), parent, source.name)
                        if not state_path.exists():
                            operations.available(parent, source.name)
                    tasks.append({"id":uuid.uuid4().hex,"name":source.name,"state":"waiting","detail":"",
                        "target":target + "/" + source.name,"request":request,"valid":True,"revision":0,
                        "context":context,"direction":"upload","_cancel":threading.Event(),"share_url":""})
            with self.lock:
                if self.logging_in or self.managing or (self.busy and self.stop.is_set()) or context != self.context():
                    raise UsageError("登录或队列状态已经变化，请重新提交")
                self.tasks.extend(tasks)
                self.start_tasks(tasks)
                return [self.public_task(task) for task in self.tasks]
        if method == "tasks":
            self.refresh_task_context()
            return [self.public_task(task) for task in self.tasks]
        if method == "tasks.cancel":
            with self.lock:
                for task in self.tasks:
                    if task["id"] in p.get("ids", []) and task["state"] not in ("done", "cancelled"):
                        task["_cancel"].set()
                        if task["id"] != self.active_id:
                            task["state"] = "cancelled"
                            task["revision"] += 1
                            self.event(None, "task", self.public_task(task))
                self.persist_tasks()
                return [self.public_task(task) for task in self.tasks]
        if method == "tasks.clear":
            with self.lock:
                if self.busy:
                    raise UsageError("请在本批次结束后清理记录")
                self.tasks = [task for task in self.tasks if task["state"] not in ("done", "cancelled")]
                self.persist_tasks()
                return [self.public_task(task) for task in self.tasks]
        if method == "tasks.authorize":
            with self.lock:
                if self.busy or self.logging_in:
                    raise UsageError("请在传输和登录结束后重新授权")
                source = self.sources[p["handle"]]
                self.verify_context()
                for task in self.tasks:
                    if task["id"] in p["ids"]:
                        if task["context"] != self.context() or not task["share_url"]:
                            raise UsageError("登录上下文不同，请在当前账号重新选择下载")
                        if parse_share_url(source.url).pwd_id != parse_share_url(task["share_url"]).pwd_id:
                            raise UsageError("重新授权的分享链接与原任务不一致")
                        task["request"] = replace(task["request"], share_source=source)
                        task["valid"] = True
                        task["revision"] += 1
                return [self.public_task(task) for task in self.tasks]
        if method == "download":
            with self.lock:
                if self.logging_in or self.managing or (self.busy and self.stop.is_set()):
                    raise UsageError("请等待登录或停止操作结束")
                entries = [self.entry(row) for row in p["entries"]]
                if not entries:
                    raise UsageError("请先勾选文件或目录")
                preferences = self.current_preferences()
                source = self.sources[p["handle"]] if p.get("handle") else None
                tasks = []
                for entry in entries:
                    plan = download_plan(preferences.output_dir, entry)
                    request = Download(entry.fid, str(plan.target), plan.recursive, False, entry.name,
                        source, str(p.get("path", "/")), preferences.connections, preferences.limit_rate)
                    tasks.append({"id": uuid.uuid4().hex, "name": entry.name, "state": "waiting", "detail": "",
                        "target": str(plan.target), "request": request, "valid": True, "revision": 0,
                        "context": self.context(), "direction": "download", "_cancel": threading.Event(),
                        "share_url": f"https://pan.quark.cn/s/{parse_share_url(source.url).pwd_id}" if source else ""})
                self.tasks.extend(tasks)
                self.start_tasks(tasks)
                return [self.public_task(task) for task in self.tasks]
        if method == "tasks.retry":
            with self.lock:
                if self.busy or self.logging_in or self.managing:
                    raise UsageError("传输正在进行")
                self.verify_context()
                self.refresh_task_context()
                tasks = [task for task in self.tasks if task["valid"] and task["state"] in ("failed", "stopped", "waiting")
                         and (not p.get("ids") or task["id"] in p["ids"])]
                preferences = self.current_preferences()
                for task in tasks:
                    task["request"] = replace(task["request"], **({"connections": preferences.connections} if task["direction"] == "download" else {}), limit_rate=preferences.limit_rate)
                self.start_tasks(tasks)
                return [self.public_task(task) for task in self.tasks]
        raise UsageError("不支持的请求")

    @contextmanager
    def mutation(self):
        with self.writes:
            with self.lock:
                if self.busy or self.logging_in:
                    raise UsageError('请先结束传输或登录，再修改网盘内容')
                self.managing = True
            try:
                yield
            finally:
                with self.lock:
                    self.managing = False

    def clear_account_context(self):
        self.search_cancel.set()
        self.cut = None
        self.sources.clear()
        if hasattr(self.backend,'_path_cache'):
            self.backend._path_cache.clear()
        for task in self.tasks:
            task['valid'] = False

    def activate_login(self, cookie, nickname):
        warning = ''
        try:
            write_json(self.session_path, {'version':1,'logged_out':False,'use_saved':True})
            self.session_warning = ''
        except QuarkError as error:
            warning = '本次登录已完成，但账号选择未持久化；下次启动请核对账号：' + describe_error(error,'保存账号状态')
        self.saved_cookie = cookie
        self.logged_out = False
        self.use_saved = True
        self.clear_account_context()
        return {'nickname':nickname,'logged_in':True,'warning':warning}

    def cut_view(self):
        clipboard = self.cut
        if clipboard is None:
            return None
        return {key:clipboard[key] for key in ('id','entries','blocked')}

    @staticmethod
    def entry(row):
        return Entry(str(row["fid"]), str(row["name"]), bool(row["directory"]), row.get("size"),
                     str(row.get("path", "")), tuple(tuple(item) for item in row.get("location", ())))

    def context(self):
        return hashlib.sha256(self.cookie().encode("utf-8")).hexdigest()

    def verify_context(self):
        with self.backend._client() as client:
            client.auth_status()

    def refresh_task_context(self):
        try:
            context = self.context()
        except QuarkError:
            context = None
        for task in self.tasks:
            task["valid"] = task["context"] == context and not (
                task["share_url"] and task["request"].share_source is None)

    @staticmethod
    def public_task(task):
        output = {key: task[key] for key in ("id", "name", "state", "detail", "target", "valid", "revision", "direction")}
        output["needs_authorization"] = bool(task["state"] not in ("done", "cancelled") and task["share_url"] and task["request"].share_source is None)
        output["share_url"] = task["share_url"]
        output["local_dir"] = str(Path(task["request"].source).parent if task["direction"] == "upload" else
            Path(task["target"]) if task["request"].recursive and task["state"] == "done" else Path(task["target"]).parent)
        return output

    def persist_tasks(self):
        if not self.task_store.save(self.tasks):
            self.event(None, "warning", self.task_store.warning)

    def start_tasks(self, tasks):
        if self.closed:
            raise UsageError('客户端正在退出，不再启动传输')
        if not tasks:
            raise UsageError("没有可继续的任务；分享任务需重新授权，登录不同请重新选择")
        new_batch = not self.busy
        if new_batch:
            self.stop.clear()
            self.batch_id = uuid.uuid4().hex
            self.batch_tasks = []
        self.busy = True
        self.batch_tasks.extend(tasks)
        for task in tasks:
            task["state"] = "waiting"
            task["_cancel"].clear()
            task["revision"] += 1
            self.pending_count += 1
        self.persist_tasks()
        if new_batch:
            self.event(None, "batch.started", {"batch_id": self.batch_id})
        for task in tasks:
            self.transfers.submit(self.run_task, task)

    def run_task(self, task):
        try:
            with self.lock:
                if task["_cancel"].is_set() or self.stop.is_set():
                    task["state"] = "cancelled" if task["_cancel"].is_set() else "stopped"
                    task["revision"] += 1
                    self.persist_tasks()
                    self.event(None, "task", self.public_task(task))
                    return
                self.active_id = task["id"]
                task["state"] = "running"
                task["revision"] += 1
                self.persist_tasks()
                self.event(None, "task", self.public_task(task))
            def stopped():
                return self.stop.is_set() or task["_cancel"].is_set()
            def progress(event):
                with self.lock:
                    task["detail"] = progress_text(event)
                    task["revision"] += 1
                    self.event(None, "task", self.public_task(task))
            try:
                if task["context"] != self.context():
                    raise UsageError("登录上下文已变化，请重新选择任务")
                if task["direction"] == "upload":
                    request = task["request"]
                    with self.backend._client() as client:
                        if request.recursive:
                            result = RecursiveTransfer(client).upload_tree(request.source, request.parent, resume=True,
                                limit_rate=request.limit_rate, progress=progress, should_stop=stopped)
                        else:
                            result = Transfer(client).upload(request.source, request.parent, resume=True,
                                limit_rate=request.limit_rate, progress=progress, should_stop=stopped)
                else:
                    result = self.backend.download(task["request"], progress, stopped)
                task["state"], task["detail"] = "done", summary_text({**result, "path": task["name"], "size": result.get("size", result.get("file_size"))}, task["name"]).replace("已下载", "已上传", 1) if task["direction"] == "upload" else summary_text(result, task["name"])
            except DownloadStopped as error:
                task["state"], task["detail"] = ("cancelled" if task["_cancel"].is_set() else "stopped"), str(error)
            except Exception as error:
                task["state"], task["detail"] = "failed", describe_error(error, "传输")
            with self.lock:
                task["revision"] += 1
                self.persist_tasks()
                self.event(None, "task", self.public_task(task))
        finally:
            with self.lock:
                self.active_id = None
                self.pending_count -= 1
                self.busy = self.pending_count > 0
                if not self.busy:
                    counts = {state: sum(task['state'] == state for task in self.batch_tasks)
                              for state in ('done', 'failed', 'stopped', 'cancelled')}
                    self.event(None, "batch.finished", {"batch_id": self.batch_id,
                               "stopped": self.stop.is_set(), "counts": counts})

    def shutdown(self):
        with self.lock:
            self.closed = True
            self.stop.set()
            self.search_cancel.set()
            self.login_cancel.set()
        self.reads.shutdown(wait=True, cancel_futures=True)
        # Let queued jobs record their stopped state without opening a transfer.
        self.transfers.shutdown(wait=True)


METHODS = frozenset({"hello", "account", "browse", "search", "search.cancel", "locate", "share.parse",
    "share.open", "share.browse", "preferences.save", "login", "login.cancel", "download", "download.stop", "tasks", "tasks.retry",
    "file.info", "file.mkdir", "file.rename", "file.move", "file.delete",
    "upload", "tasks.cancel", "tasks.clear", "tasks.authorize",
    'share.list','share.link','share.create','share.revoke','share.save','recycle.list','recycle.restore','recycle.remove',
    'file.cut','file.paste','file.clearCut',
    'favorites.list','favorites.add','favorites.remove','favorites.open',
    'logout','login.web.begin','login.web.finish','login.web.cancel'})


def main():
    # stdout is only the protocol. Normal library exceptions are sanitized above.
    lock = threading.Lock()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stdin.reconfigure(encoding="utf-8")
    def send(message):
        with lock:
            print(json.dumps(message, ensure_ascii=False), flush=True)
    service = DesktopService(send)
    try:
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except (ValueError, TypeError):
                send({"id": None, "error": "无效 JSON 请求"})
                continue
            service.accept(message)
    finally:
        service.shutdown()
