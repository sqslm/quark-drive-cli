"""Explicit business methods over JSON lines; credentials never leave Python."""
from __future__ import annotations

import base64
import json
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable

from ..auth import QRLogin, get_cookie, load_saved_cookie
from ..errors import QuarkError, UsageError
from ..state import config_directory
from ..transfer import DownloadStopped
from ..downloader.core import Entry, EXTENSION_GROUPS, describe_error, download_plan, progress_text, summary_text
from ..downloader.preferences import Preferences, _validated, load_preferences, save_preferences
from ..downloader.share import ShareSource
from ..downloader.share_input import parse_share_input
from ..downloader.search import SearchCancelled
from ..downloader.worker import Download, DriveBackend, Search


class DesktopService:
    def __init__(self, send: Callable[[dict], None], *, backend=None, directory=None):
        self.send = send
        self.directory = Path(directory) if directory is not None else config_directory() / "downloader"
        self.preferences_path = self.directory / "preferences.json"
        self.saved_cookie = None
        self.backend = backend or DriveBackend(self.cookie)
        self.reads = ThreadPoolExecutor(2, thread_name_prefix="quark-read")
        self.transfers = ThreadPoolExecutor(1, thread_name_prefix="quark-transfer")
        self.lock = threading.RLock()
        self.search_cancel = threading.Event()
        self.login_cancel = threading.Event()
        self.stop = threading.Event()
        self.tasks: list[dict] = []
        self.sources: dict[str, ShareSource] = {}
        self.busy = False
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
            if self.busy:
                self.send({"id": identifier, "error": "请先停止传输再登录"})
                return
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
        except Exception as error:
            self.send({"id": identifier, "error": describe_error(error, "操作")})

    def dispatch(self, identifier, method, p, cancel):
        if method == "hello":
            return {"protocol": 1, "preferences": asdict(self.current_preferences()), "extension_groups": EXTENSION_GROUPS,
                    "warning": self.preferences_warning}
        if method == "account":
            return {"nickname": self.backend.account_nickname()}
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
            self.saved_cookie = load_saved_cookie()
            self.sources.clear()
            for task in self.tasks:
                task["valid"] = False
            try:
                nickname = self.backend.account_nickname()
            except Exception:
                nickname = "昵称暂不可用"
            return {"nickname": nickname}
        if method == "tasks":
            return [self.public_task(task) for task in self.tasks]
        if method == "download":
            with self.lock:
                if self.busy:
                    raise UsageError("传输正在进行，请先停止")
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
                        "target": str(plan.target), "request": request, "valid": True, "revision": 0})
                self.tasks = tasks
                self.start_tasks(tasks)
                return [self.public_task(task) for task in tasks]
        if method == "tasks.retry":
            with self.lock:
                if self.busy:
                    raise UsageError("传输正在进行")
                tasks = [task for task in self.tasks if task["valid"] and task["state"] in ("failed", "stopped", "waiting")
                         and (not p.get("ids") or task["id"] in p["ids"])]
                preferences = self.current_preferences()
                for task in tasks:
                    task["request"] = replace(task["request"], connections=preferences.connections, limit_rate=preferences.limit_rate)
                self.start_tasks(tasks)
                return [self.public_task(task) for task in self.tasks]
        raise UsageError("不支持的请求")

    @staticmethod
    def entry(row):
        return Entry(str(row["fid"]), str(row["name"]), bool(row["directory"]), row.get("size"),
                     str(row.get("path", "")), tuple(tuple(item) for item in row.get("location", ())))

    @staticmethod
    def public_task(task):
        return {key: task[key] for key in ("id", "name", "state", "detail", "target", "valid", "revision")}

    def start_tasks(self, tasks):
        if not tasks:
            raise UsageError("没有可继续的任务")
        self.busy = True
        self.stop.clear()
        for task in tasks:
            task["state"] = "waiting"
            task["revision"] += 1
        self.transfers.submit(self.run_tasks, tasks)

    def run_tasks(self, tasks):
        try:
            for task in tasks:
                if self.stop.is_set():
                    break
                task["state"] = "running"
                task["revision"] += 1
                self.event(None, "task", self.public_task(task))
                def progress(event):
                    task["detail"] = progress_text(event)
                    task["revision"] += 1
                    self.event(None, "task", self.public_task(task))
                try:
                    result = self.backend.download(task["request"], progress, self.stop.is_set)
                    task["state"], task["detail"] = "done", summary_text(result, task["name"])
                except DownloadStopped as error:
                    task["state"], task["detail"] = "stopped", str(error)
                except Exception as error:
                    task["state"], task["detail"] = "failed", describe_error(error, "下载")
                task["revision"] += 1
                self.event(None, "task", self.public_task(task))
        finally:
            with self.lock:
                self.busy = False
            self.event(None, "batch.finished", {"stopped": self.stop.is_set()})

    def shutdown(self):
        self.closed = True
        self.stop.set()
        self.search_cancel.set()
        self.login_cancel.set()
        self.reads.shutdown(wait=True, cancel_futures=True)
        self.transfers.shutdown(wait=True, cancel_futures=True)


METHODS = frozenset({"hello", "account", "browse", "search", "search.cancel", "locate", "share.parse",
    "share.open", "share.browse", "preferences.save", "login", "login.cancel", "download", "download.stop", "tasks", "tasks.retry"})


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
