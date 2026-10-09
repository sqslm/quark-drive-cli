"""Validated TXT/CSV inputs, serial share saves, and atomic safe reports."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from .errors import LocalIOError, QuarkError, RemoteError, UsageError
from .paths import DirectoryTarget, Scope, path_components
from .shares import ShareSaver, parse_share_url, validate_patterns


@dataclass(frozen=True)
class BatchRow:
    line: int
    share_id: str
    destination: str
    source: str
    url: str = field(repr=False)
    passcode: str | None = field(repr=False)


@dataclass(frozen=True)
class BatchResult:
    output: dict[str, Any]
    exit_code: int


def _row(line: int, url: str, destination: str, source: str, passcode: str | None) -> BatchRow:
    try:
        link = parse_share_url(url, passcode)
        path_components(destination)
        path_components(source)
    except UsageError:
        # Raw input, especially URL query parameters, must not enter reports.
        raise UsageError(f"批量输入第 {line} 行的链接、目标或源路径格式无效") from None
    return BatchRow(line, link.pwd_id, destination, source, url, passcode)


def load_batch(path: str | Path, *, destination: str | None = None, source: str = "/") -> list[BatchRow]:
    source_path = Path(path)
    if source_path.suffix.lower() not in (".txt", ".csv"):
        raise UsageError("批量输入文件必须使用 .txt 或 .csv 扩展名")
    if destination is not None:
        path_components(destination)
    path_components(source)
    try:
        text = source_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise UsageError("批量输入必须使用 UTF-8 编码") from None
    except OSError:
        raise LocalIOError("无法读取批量输入文件") from None

    rows: list[BatchRow] = []
    if source_path.suffix.lower() == ".txt":
        if destination is None:
            raise UsageError("TXT 批量输入必须指定 --to 统一目标")
        for line, value in enumerate(text.splitlines(), 1):
            url = value.strip()
            if url and not url.startswith("#"):
                rows.append(_row(line, url, destination, source, None))
    else:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        try:
            header = next(reader, [])
            if not header or len(set(header)) != len(header) or "url" not in header or set(header) - {"url", "to", "passcode", "source"}:
                raise UsageError("CSV 表头须包含 url，其他列仅允许 to、passcode、source，且不能重复")
            while True:
                line = reader.line_num + 1
                values = next(reader, None)
                if values is None:
                    break
                if not values or all(not value for value in values):
                    continue
                if len(values) != len(header):
                    raise UsageError(f"CSV 第 {line} 行字段数与表头不符")
                record = dict(zip(header, values))
                target = record.get("to") or destination
                if target is None:
                    raise UsageError(f"CSV 第 {line} 行缺少目标目录，也没有 --to 默认值")
                rows.append(_row(line, record["url"], target, record.get("source") or source, record.get("passcode") or None))
        except csv.Error:
            raise UsageError(f"CSV 第 {reader.line_num} 行格式无效") from None

    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        identity = (row.share_id, row.source, row.destination)
        if identity in seen:
            raise UsageError(f"批量输入第 {row.line} 行重复指定同一分享、源和目标")
        seen.add(identity)
    return rows


class _AtomicReport:
    def __init__(self, path: str | Path | None, input_path: str | Path) -> None:
        self.target: Path | None = None
        self.temporary: Path | None = None
        self.stream: TextIO | None = None
        if path is None:
            return
        descriptor: int | None = None
        try:
            target = Path(path).absolute()
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise UsageError("报告路径必须是普通文件，不能是目录或符号链接")
            source = Path(input_path).resolve()
            if target.resolve() == source or (target.exists() and os.path.samefile(source, target)):
                raise UsageError("报告不能覆盖批量输入文件")
            descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            self.temporary = Path(temporary)
            self.stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            descriptor = None  # Ownership has moved to the stream.
            self.target = target
        except OSError:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self.close()
            raise LocalIOError("无法准备批量报告文件；未提交任何转存") from None

    def publish(self, output: dict[str, Any]) -> None:
        if self.stream is None or self.target is None or self.temporary is None:
            return
        try:
            json.dump(output, self.stream, ensure_ascii=False, separators=(",", ":"))
            self.stream.write("\n")
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()
            self.stream = None
            os.replace(self.temporary, self.target)
            self.temporary = None
        except OSError:
            raise LocalIOError("批量操作已结束，但报告原子写入失败；未重新提交任何转存") from None

    def close(self) -> bool:
        complete = True
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                complete = False
            self.stream = None
        if self.temporary is not None:
            try:
                self.temporary.unlink(missing_ok=True)
            except OSError:
                complete = False
            self.temporary = None
        return complete


def _destination_key(target: DirectoryTarget) -> tuple[str, ...]:
    return ("new", target.parent_fid, target.name) if target.missing else ("existing", target.fid or "0")


def _safe_result(row: BatchRow, status: str, *, result: dict[str, Any] | None = None, error: QuarkError | None = None, phase: str | None = None) -> dict[str, Any]:
    output: dict[str, Any] = {"line": row.line, "share_id": row.share_id, "destination_path": row.destination,
                              "source_path": row.source, "result": status}
    if phase is not None:
        output["phase"] = phase
    if result is not None:
        fields = ("task_id", "destination_fid", "item_count", "created_directory", "create_directory", "reason")
        output.update({key: result[key] for key in fields if key in result and isinstance(result[key], (str, int, bool))})
    if error is not None:
        output.update({"error_type": error.label, "exit_code": error.exit_code})
        task_id = getattr(error, "task_id", None)
        if isinstance(task_id, str) and task_id:
            output["task_id"] = task_id
    return output


class BatchSaver:
    def __init__(self, saver: ShareSaver) -> None:
        self.saver = saver

    def run(self, path: str | Path, *, destination: str | None = None, report: str | Path | None = None,
            scope: Scope | None = None, dry_run: bool = False, source: str = "/",
            include: list[str] | tuple[str, ...] = (), exclude: list[str] | tuple[str, ...] = ()) -> BatchResult:
        includes, excludes = validate_patterns(include), validate_patterns(exclude)
        rows = load_batch(path, destination=destination, source=source)
        report_file = _AtomicReport(report, path)
        finished: BatchResult | None = None
        try:
            # This barrier covers every destination, even rows whose source is
            # empty. No remote mutation can precede full range verification.
            targets: list[DirectoryTarget] = []
            for row in rows:
                try:
                    target = self.saver.paths.directory_target(row.destination, allow_missing_leaf=True)
                    if scope:
                        scope.check_target(target)
                except QuarkError as error:
                    raise type(error)(f"批量输入第 {row.line} 行的目标或范围无法确认；整批未执行") from None
                targets.append(target)

            prepared: list[dict[str, Any] | QuarkError] = []
            reserved: dict[tuple[str, ...], set[str]] = {}
            for row, target in zip(rows, targets):
                try:
                    preview = self.saver.save(row.url, row.destination, passcode=row.passcode, source=row.source,
                                              scope=scope, dry_run=True, include=includes, exclude=excludes)
                    names = {record["file_name"] for record in preview.get("items", [])}
                    prior = reserved.setdefault(_destination_key(target), set())
                    if names & prior:
                        raise UsageError("批量行在同一目标选中了同名项目")
                    prior.update(names)
                    prepared.append(preview)
                except QuarkError as error:
                    prepared.append(error)

            results: list[dict[str, Any]] = []
            blocked: set[tuple[str, ...]] = set()
            exit_code = 0
            for row, target, preview in zip(rows, targets, prepared):
                key = _destination_key(target)
                if isinstance(preview, QuarkError):
                    results.append(_safe_result(row, "failed", error=preview, phase="preflight"))
                    exit_code = max(exit_code, preview.exit_code)
                    continue
                if preview.get("skipped"):
                    results.append(_safe_result(row, "skipped", result=preview))
                    continue
                if dry_run:
                    results.append(_safe_result(row, "preview", result=preview))
                    continue
                if key in blocked:
                    error = RemoteError("同目标此前操作未确认成功，已阻止后续行")
                    record = _safe_result(row, "blocked", error=error, phase="execute")
                    record["reason"] = "previous_destination_failure"
                    results.append(record)
                    exit_code = max(exit_code, error.exit_code)
                    continue
                try:
                    result = self.saver.save(row.url, row.destination, passcode=row.passcode, source=row.source, scope=scope,
                                             include=includes, exclude=excludes)
                    results.append(_safe_result(row, "skipped" if result.get("skipped") else "saved", result=result))
                except QuarkError as error:
                    # Conservatively isolate the entire destination after a
                    # failure: writes may have been accepted without a reply.
                    blocked.add(key)
                    results.append(_safe_result(row, "failed", error=error, phase="execute"))
                    exit_code = max(exit_code, error.exit_code)

            output = {"dry_run": dry_run, "total": len(rows),
                      "succeeded": sum(record["result"] in ("saved", "preview") for record in results),
                      "skipped": sum(record["result"] == "skipped" for record in results),
                      "failed": sum(record["result"] in ("failed", "blocked") for record in results), "rows": results}
            if report_file.target is not None:
                output["report"] = {"path": str(report_file.target), "written": True}
                try:
                    report_file.publish(output)
                except LocalIOError:
                    output["report"].update({"written": False, "error_type": "local_io_error"})
                    exit_code = 5
            finished = BatchResult(output, exit_code)
        finally:
            if not report_file.close() and finished is not None:
                finished.output["report"].update({"cleanup_failed": True, "error_type": "local_io_error"})
                finished = BatchResult(finished.output, 5)
        return finished
