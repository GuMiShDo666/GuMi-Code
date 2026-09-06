"""Track actual request-local changes and run bounded checks, never model verdicts."""

import copy
import ast
import hashlib
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from threading import Event

from gum_code.tools.command import CommandExecutionResult, DockerSandboxConfig
from gum_code.tools.common import _is_sensitive_path, _reject_symlinks, _resolve_workspace_path
from gum_code.tools.filesystem import FileChange
from gum_code.verification import MAX_CHECKS, MAX_SCAN_FILES, MAX_SCAN_SECONDS, _ScanError

MAX_SELF_CHECK_STATE_BYTES = 64_000
MAX_CHANGED_PATHS = 256
_SOURCE_SUFFIXES = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts",
    ".vue", ".svelte", ".html", ".css", ".scss", ".sass", ".less", ".sql",
    ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb",
    ".php", ".swift", ".sh", ".bash", ".zsh", ".ex", ".exs", ".lua", ".r",
    ".json", ".toml", ".ini", ".cfg", ".yaml", ".yml",
}
_CONFIG_NAMES = {
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "pyproject.toml",
    "requirements.txt", "setup.cfg", "setup.py", "pytest.ini", "tox.ini", "Makefile",
    "GNUmakefile", "Dockerfile", "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
    "Pipfile", "Pipfile.lock", "poetry.lock", "uv.lock", "pnpm-lock.yaml", "yarn.lock",
}
_DOCUMENT_NAMES = {"readme", "license", "licence", "notice", "authors", "changelog"}
_RESULT_STATUSES = {"not_needed", "passed", "failed", "not_run", "source_changed", "unknown"}
_CHECK_STATUSES = {"passed", "failed", "not_run", "stale"}

SELF_CHECK_INSTRUCTION = (
    "仅当前请求实际修改源码、测试或运行/构建配置后，在完成一组修改时调用 self_check。"
    "读取、搜索、问答和纯文档修改无需自检。self_check 无参数，不登记计划或提交审阅。"
    "根据真实失败在任务范围内修复后复测；不得弱化已有正确测试来制造通过。"
    "相同输入的失败不要重复空跑；不能把静态阅读、安装成功或语法检查说成测试通过。"
    "缺环境时由宿主询问准备，拒绝或缺输入后保留工作并说明未验证。"
    "开发项目时维护原生依赖及版本清单和简短运行说明，优先更新已有清单与锁文件；"
    "只记录实际依赖，不从宿主全局包生成清单，不为无依赖或无依赖变化的任务生成重复空文件。"
)


def _path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("自检路径无效")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("自检路径无效")
    if _is_sensitive_path(path) or _is_sensitive_path(Path(value.lower())):
        raise ValueError("自检路径无效")
    return path.as_posix()


def _reason(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None:
        raise ValueError("自检原因无效")
    return value


def validate_self_check_state(value: object) -> dict[str, object]:
    """Validate historical facts, not authority or a restored passing verdict."""
    keys = {"version", "request_id", "source_event_seq", "changed_paths", "ambiguous_paths",
            "generation", "unknown", "last_result"}
    if not isinstance(value, dict) or set(value) != keys or type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("自检状态无效")
    if not isinstance(value["request_id"], str):
        raise ValueError("自检请求无效")
    try:
        uuid.UUID(value["request_id"])
    except (ValueError, AttributeError):
        raise ValueError("自检请求无效") from None
    seq = value["source_event_seq"]
    if seq is not None and (type(seq) is not int or seq < 1):
        raise ValueError("自检来源无效")
    if type(value["generation"]) is not int or not 0 <= value["generation"] <= 2**31 - 1:
        raise ValueError("自检代次无效")
    for key in ("changed_paths", "ambiguous_paths"):
        paths = value[key]
        if not isinstance(paths, list) or len(paths) > MAX_CHANGED_PATHS:
            raise ValueError("自检路径列表无效")
        normalized = [_path(item) for item in paths]
        if normalized != sorted(set(normalized)):
            raise ValueError("自检路径列表无效")
    if value["unknown"] is not None:
        _reason(value["unknown"])
    result = value["last_result"]
    if result is not None:
        if not isinstance(result, dict) or set(result) - {"cleanup_required"} != {"status", "reason", "generation", "input_version", "checks", "uncovered"}:
            raise ValueError("自检结果无效")
        cleanup = result.get("cleanup_required", [])
        if not isinstance(cleanup, list) or len(cleanup) > 16 or any(
                not isinstance(item, str) or re.fullmatch(r"gum-code-(?:prepare|env)-[0-9a-f]{32}", item) is None for item in cleanup):
            raise ValueError("自检清理资源无效")
        if not isinstance(result["status"], str) or result["status"] not in _RESULT_STATUSES or type(result["generation"]) is not int or not 0 <= result["generation"] <= value["generation"]:
            raise ValueError("自检结果无效")
        _reason(result["reason"])
        version = result["input_version"]
        if version is not None and (not isinstance(version, str) or re.fullmatch(r"[0-9a-f]{64}", version) is None):
            raise ValueError("自检输入版本无效")
        if not isinstance(result["uncovered"], list) or len(result["uncovered"]) > MAX_CHECKS:
            raise ValueError("自检未覆盖项无效")
        for item in result["uncovered"]:
            _reason(item)
        if not isinstance(result["checks"], list) or len(result["checks"]) > MAX_CHECKS:
            raise ValueError("自检检查项无效")
        for check in result["checks"]:
            if not isinstance(check, dict) or set(check) != {"name", "status", "started", "exit_code", "reason", "input_version"}:
                raise ValueError("自检检查项无效")
            if not isinstance(check["name"], str) or not isinstance(check["status"], str) or check["name"] not in {"syntax", "test", "build", "typecheck"} or check["status"] not in _CHECK_STATUSES:
                raise ValueError("自检检查项无效")
            if check["started"] is not None and type(check["started"]) is not bool:
                raise ValueError("自检启动事实无效")
            if check["exit_code"] is not None and (type(check["exit_code"]) is not int or not -255 <= check["exit_code"] <= 65535):
                raise ValueError("自检退出事实无效")
            _reason(check["reason"])
            if not isinstance(check["input_version"], str) or re.fullmatch(r"[0-9a-f]{64}", check["input_version"]) is None:
                raise ValueError("自检输入版本无效")
            if check["status"] == "passed" and not (check["started"] is True and check["reason"] == "exited" and check["exit_code"] == 0):
                raise ValueError("自检通过缺少执行事实")
        if result["status"] == "passed" and (not result["checks"] or any(c["status"] != "passed" or c["input_version"] != result["input_version"] for c in result["checks"]) or result["generation"] != value["generation"] or value["unknown"] is not None or value["ambiguous_paths"]):
            raise ValueError("自检通过缺少检查")
    if len(json.dumps(value, ensure_ascii=True).encode("utf-8")) > MAX_SELF_CHECK_STATE_BYTES:
        raise ValueError("自检状态超过限制")
    return copy.deepcopy(value)


def validate_task_outcome(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"status", "reason", "step", "self_check_status", "changed", "summary"}:
        raise ValueError("任务结束事实无效")
    if not isinstance(value["status"], str) or value["status"] not in {"completed", "incomplete", "failed", "interrupted"}:
        raise ValueError("任务结束状态无效")
    _reason(value["reason"])
    if type(value["step"]) is not int or value["step"] < 0 or type(value["changed"]) is not bool:
        raise ValueError("任务结束事实无效")
    if not isinstance(value["self_check_status"], str) or value["self_check_status"] not in _RESULT_STATUSES | {"pending"}:
        raise ValueError("任务自检状态无效")
    if not isinstance(value["summary"], str) or len(value["summary"]) > 2000:
        raise ValueError("任务摘要无效")
    return copy.deepcopy(value)


def _head(workspace: Path, name: str) -> tuple[bytes, bool]:
    target = _resolve_workspace_path(workspace, name)
    _reject_symlinks(workspace, name)
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("自检输入不是普通文件")
        return source.read(512), bool(info.st_mode & 0o111)


def _read_text(workspace: Path, name: str) -> str:
    target = _resolve_workspace_path(workspace, name)
    _reject_symlinks(workspace, name)
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 200000:
            raise ValueError("自检输入超过限制或不是普通文件")
        content = source.read(200001)
        after = os.fstat(source.fileno())
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or before.st_ctime_ns != after.st_ctime_ns or len(content) > 200000:
        raise ValueError("自检输入在读取时发生变化")
    return content.decode("utf-8")


def _project_files(workspace: Path, package: Path) -> list[Path]:
    """Discover entries with a streaming bound; dependency trees are not test discovery."""
    pending, found = [package], []
    count, started = 0, time.monotonic()
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_SCAN_FILES or time.monotonic() - started > MAX_SCAN_SECONDS:
                    raise _ScanError("scan_limit")
                path = Path(entry.path)
                if entry.is_symlink() or _is_sensitive_path(path.relative_to(workspace)):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name != "node_modules":
                        pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    found.append(path)
    return sorted(found)


def _code_kind(name: str, head: bytes | None, executable: bool, entries: set[str]) -> str:
    path = Path(name)
    if name in entries or path.name in _CONFIG_NAMES or path.suffix.lower() in _SOURCE_SUFFIXES:
        return "code"
    if (path.name == "CACHEDIR.TAG" or
            (path.name in {"nodeids", "lastfailed", "stepwise"} and
             path.parent.parts[-3:] == (".pytest_cache", "v", "cache"))):
        return "other"
    if path.stem.lower() in _DOCUMENT_NAMES or path.suffix.lower() in {".md", ".rst", ".txt", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".gif", ".svg"}:
        return "code" if path.name.startswith("requirements") and path.suffix == ".txt" else "other"
    if path.name.startswith(("tsconfig", "jsconfig", "vite.config", "webpack.config")):
        return "code"
    if head is not None and b"\x00" not in head and (executable or head.startswith(b"#!")):
        return "code"
    return "ambiguous" if not path.suffix and head is not None and b"\x00" not in head else "other"


class SelfCheckRuntime:
    """One original request's changes and latest bounded execution facts."""

    def __init__(self, workspace: Path, *, request_id: str | None = None,
                 initial_state: dict[str, object] | None = None,
                 command_sandbox: DockerSandboxConfig | None = None,
                 approval_callback: Callable[[dict[str, object]], bool] | None = None,
                 internal_paths: tuple[Path, ...] = ()) -> None:
        self.workspace = workspace.resolve()
        self.command_sandbox = command_sandbox
        self.approval_callback = approval_callback
        self._internal_paths = tuple(Path(p).resolve() for p in internal_paths)
        self._cancel = Event()
        self._denied_scopes: set[str] = set()
        self._entries: set[str] = set()
        self._entry_ecosystems: dict[str, str] = {}
        self._excluded_dirs: set[str] = set()
        self._output_dirs: set[str] = set()
        self._generated_outputs: dict[str, str] = {}
        self._cleanup_required: set[str] = set()
        self._before_command = None
        self._checked_snapshot = None
        self._last_key = None
        self._group_cache: dict[tuple, list[dict]] = {}
        self._state = {"version": 1, "request_id": request_id or str(uuid.uuid4()),
                       "source_event_seq": None, "changed_paths": [], "ambiguous_paths": [],
                       "generation": 0, "unknown": None, "last_result": None}
        if initial_state is not None:
            restored = validate_self_check_state(initial_state)
            if request_id is not None and restored["request_id"] != request_id:
                raise ValueError("自检状态与原始请求不匹配")
            self._state = restored
            self._state["last_result"] = None
        validate_self_check_state(self._state)

    def bind_source(self, source_event_seq: int) -> None:
        candidate = {**self._state, "source_event_seq": source_event_seq}
        validate_self_check_state(candidate)
        if self._state["source_event_seq"] not in (None, source_event_seq):
            raise ValueError("自检不能改绑原始请求")
        self._state = candidate

    def set_internal_paths(self, paths: tuple[Path, ...]) -> None:
        self._internal_paths = tuple(Path(path).resolve() for path in paths)

    def export_state(self) -> dict[str, object]:
        return validate_self_check_state(self._state)

    def cancel(self) -> None:
        self._cancel.set()

    def mark_unknown(self, reason: str = "restored_unknown") -> None:
        self._state["unknown"] = _reason(reason)
        if self._state["last_result"] is not None and self._state["last_result"]["status"] == "passed":
            self._state["last_result"]["status"] = "unknown"
            self._state["last_result"]["reason"] = "change_unknown"

    def _check_cancel(self, cancel_event: Event | None = None) -> None:
        if self._cancel.is_set() or (cancel_event is not None and cancel_event.is_set()):
            raise KeyboardInterrupt

    def _add_path(self, name: str, kind: str) -> None:
        if kind == "other":
            return
        # An explicitly modified entry is never hidden by an earlier output declaration.
        for directory in tuple(self._excluded_dirs):
            if Path(name).is_relative_to(directory):
                self._excluded_dirs.discard(directory)
                self._output_dirs.discard(directory)
                self._generated_outputs.pop(directory, None)
        key = "changed_paths" if kind == "code" else "ambiguous_paths"
        previous = copy.deepcopy(self._state)
        if name not in self._state[key]:
            if len(self._state[key]) >= MAX_CHANGED_PATHS:
                self.mark_unknown("change_limit")
                return
            self._state[key] = sorted([*self._state[key], name])
        if kind == "code" and name in self._state["ambiguous_paths"]:
            self._state["ambiguous_paths"].remove(name)
        self._state["generation"] += 1
        self._group_cache.clear()
        if self._state["last_result"] is not None and self._state["last_result"]["status"] == "passed":
            self._state["last_result"]["status"] = "source_changed"
            self._state["last_result"]["reason"] = "source_changed"
        try:
            validate_self_check_state(self._state)
        except ValueError:
            self._state = previous
            self.mark_unknown("change_limit")

    def on_file_change(self, change: FileChange) -> None:
        if not isinstance(change, FileChange):
            raise ValueError("自检需要宿主文件变化事实")
        name = _path(change.path)
        kinds = {_code_kind(name, h, change.executable, self._entries) for h in (change.before_head, change.after_head) if h is not None}
        self._add_path(name, "code" if "code" in kinds else "ambiguous" if "ambiguous" in kinds else "other")

    def record_path(self, path: str) -> None:
        name = _path(path)
        head, executable = _head(self.workspace, name)
        self.on_file_change(FileChange(name, None, head, executable))

    def _scan(self) -> dict[str, tuple[object, ...]] | None:
        from gum_code.verification import workspace_snapshot
        started = time.monotonic()
        try:
            snapshot = workspace_snapshot(self.workspace, tuple(sorted(self._excluded_dirs)),
                                          self._internal_paths)
            result = {}
            for name, identity in snapshot.items():
                if time.monotonic() - started > MAX_SCAN_SECONDS:
                    raise _ScanError("scan_limit")
                if identity[0] == "__directory__":
                    continue
                head, executable = _head(self.workspace, name)
                kind = _code_kind(name, head, executable, self._entries)
                if kind != "other":
                    result[name] = (*identity, kind)
            return result
        except (ValueError, OSError, RuntimeError) as error:
            self.mark_unknown(getattr(error, "code", "scan_read"))
            return None

    @staticmethod
    def _version(snapshot: dict[str, tuple[object, ...]] | None) -> str | None:
        if snapshot is None:
            return None
        return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def before_command(self, arguments: dict[str, object] | None = None) -> None:
        if isinstance(arguments, dict):
            argv = arguments.get("argv", [])
            cwd = arguments.get("cwd", ".")
            if isinstance(argv, list) and len(argv) > 1 and isinstance(argv[0], str) and Path(argv[0]).name in {"python", "python3", "node", "nodejs"}:
                if isinstance(argv[1], str) and not argv[1].startswith("-") and isinstance(cwd, str):
                    try:
                        entry = _path((Path(cwd) / argv[1]).as_posix())
                        self._entries.add(entry)
                        self._entry_ecosystems[entry] = "python" if Path(argv[0]).name.startswith("python") else "node"
                        for directory in tuple(self._excluded_dirs):
                            if Path(entry).is_relative_to(directory):
                                self._excluded_dirs.discard(directory)
                                self._output_dirs.discard(directory)
                                self._generated_outputs.pop(directory, None)
                        if entry in self._state["ambiguous_paths"]:
                            self._add_path(entry, "code")
                    except ValueError:
                        pass
        self._before_command = self._scan()

    def after_command(self) -> None:
        before, self._before_command = self._before_command, None
        after = self._scan()
        self._observe(before, after)

    def _observe(self, before: dict | None, after: dict | None) -> None:
        if before is None or after is None:
            self.mark_unknown(self._state["unknown"] or "scan_unknown")
            return
        for name in sorted(before.keys() | after.keys()):
            if before.get(name) != after.get(name):
                identity = after.get(name, before.get(name))
                self._add_path(name, identity[-1])

    def needs_check(self) -> bool:
        result = self._state["last_result"]
        return bool(self._state["changed_paths"]) and (result is None or result["generation"] != self._state["generation"])

    def _result(self, status: str, reason: str, checks: list | None = None,
                version: str | None = None, uncovered: list[str] | None = None) -> dict[str, object]:
        if status == "passed" and (self._state["unknown"] or self._state["ambiguous_paths"]):
            status, reason = "unknown", "change_unknown"
        result = {"status": status, "reason": reason, "generation": self._state["generation"],
                  "input_version": version, "checks": checks or [], "uncovered": uncovered or []}
        if self._cleanup_required:
            result["cleanup_required"] = sorted(self._cleanup_required)[:16]
        self._state["last_result"] = result
        validate_self_check_state(self._state)
        return result

    def summary(self) -> str:
        text = self._check_summary()
        result = self._state["last_result"]
        if result and result.get("cleanup_required"):
            text += "环境资源清理未确认，需人工检查：" + "、".join(result["cleanup_required"])
        return text

    def _check_summary(self) -> str:
        result = self._state["last_result"]
        if not self._state["changed_paths"] and not self._state["ambiguous_paths"] and not self._state["unknown"]:
            return ""
        if self._state["unknown"] or self._state["ambiguous_paths"]:
            return "自检：改动范围或当前输入无法确认，未验证；已发生的改动保留。"
        if result is not None and result["status"] == "source_changed":
            return "自检：检查之后或期间源码发生变化，需对当前代码复测。"
        if result is None or result["generation"] != self._state["generation"]:
            return "自检：当前代码改动尚未检查。"
        passed = sum(check["status"] == "passed" for check in result["checks"])
        if result["status"] == "passed":
            suffix = "；没有发现可运行测试，仅覆盖语法/编译" if "no_tests" in result["uncovered"] else "；仅代表实际运行的检查范围"
            return f"自检：实际运行并通过 {passed} 项{suffix}。"
        return {"failed": "自检：实际检查失败，尚未修复通过。", "source_changed": "自检：检查期间源码发生变化，需对当前代码复测。"}.get(result["status"], "自检：检查未运行或未覆盖完整范围，已完成的改动保留。")

    def outcome(self, *, step: int, status: str | None = None, reason: str | None = None) -> dict[str, object]:
        result = self._state["last_result"]
        changed = bool(self._state["changed_paths"])
        check_status = "unknown" if self._state["unknown"] or self._state["ambiguous_paths"] else result["status"] if result else "pending" if changed else "not_needed"
        complete = not self._state["unknown"] and not self._state["ambiguous_paths"] and (
            not changed or (result is not None and result["status"] == "passed" and result["generation"] == self._state["generation"]))
        return validate_task_outcome({"status": status or ("completed" if complete else "incomplete"),
            "reason": reason or ("answered" if not changed and complete else result["reason"] if result else "not_checked"),
            "step": step, "self_check_status": check_status, "changed": changed, "summary": self.summary()})

    def recheck_final(self) -> None:
        result = self._state["last_result"]
        if result and result["status"] == "passed":
            current = self._scan()
            if self._version(current) != result["input_version"]:
                self._observe(self._checked_snapshot, current)
                result["status"], result["reason"] = "source_changed", "source_changed"

    def execute(self, *, command_runner: Callable | None = None, cancel_event: Event | None = None,
                allow_prepare: bool = True) -> str:
        self._check_cancel(cancel_event)
        if not self._state["changed_paths"]:
            return json.dumps(self._result("not_needed", "no_code_changes"), ensure_ascii=False)
        if command_runner is None:
            return json.dumps(self._result("not_run", "command_unavailable"), ensure_ascii=False)
        return self._execute_checks(command_runner, cancel_event, allow_prepare)

    def _execute_checks(self, command_runner: Callable, cancel_event: Event | None, allow_prepare: bool) -> str:
        from gum_code.project_environment import discover_project, ensure_environment
        try:
            groups, uncovered = self._discover()
        except (ValueError, OSError, RuntimeError) as error:
            self.mark_unknown(getattr(error, "code", "discovery_unavailable"))
            return json.dumps(self._result("unknown", "discovery_unavailable"), ensure_ascii=False)
        if not groups:
            return json.dumps(self._result("not_run", "no_check_entry", uncovered=uncovered), ensure_ascii=False)
        checks, outputs = [], []
        version = None
        identity_parts = []
        for root, ecosystem, commands in groups:
            self._check_cancel(cancel_event)
            project = discover_project(self.workspace, root, ecosystem=ecosystem)
            with ensure_environment(self.workspace, project, command_sandbox=self.command_sandbox,
                    approval_callback=self.approval_callback if allow_prepare else None,
                    denied_scopes=self._denied_scopes, cancel_event=cancel_event or self._cancel,
                    change_callback=self.record_path) as environment:
                self._cleanup_required.update(getattr(environment, "cleanup_required", ()))
                self._excluded_dirs.update(getattr(environment, "excluded_dirs", ()))
                if environment.status != "ready":
                    reason = getattr(environment, "reason", "environment_unavailable")
                    if reason == "cancelled" or environment.status == "cancelled":
                        raise KeyboardInterrupt
                    self._result("not_run", reason if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason or "") else "environment_unavailable", checks, version, uncovered)
                    return json.dumps(self._state["last_result"], ensure_ascii=False)
                identity_parts.append(environment.identity)
                current = self._scan()
                version = self._version(current)
                if current is None:
                    return json.dumps(self._result("unknown", "scan_unknown", checks), ensure_ascii=False)
                key = (version, tuple(identity_parts), json.dumps(groups, sort_keys=True))
                if len(groups) == 1 and key == self._last_key and self._state["last_result"] is not None:
                    if self._cleanup_required:
                        self._state["last_result"]["cleanup_required"] = sorted(self._cleanup_required)[:16]
                    return json.dumps({**self._state["last_result"], "reused": True}, ensure_ascii=False)
                group_key = (version, environment.identity, json.dumps(commands, sort_keys=True))
                if group_key in self._group_cache:
                    checks.extend(copy.deepcopy(self._group_cache[group_key]))
                    continue
                group_start = current
                group_offset = len(checks)
                for name, arguments in commands:
                    self._check_cancel(cancel_event)
                    before = self._scan()
                    before_version = self._version(before)
                    if before is None:
                        return json.dumps(self._result("unknown", "scan_unknown", checks), ensure_ascii=False)
                    try:
                        output, fact = command_runner(arguments, environment.sandbox)
                    finally:
                        after = self._scan()
                        self._observe(before, after)
                    stable = before == after and after is not None
                    fact = fact if isinstance(fact, CommandExecutionResult) else CommandExecutionResult(None, None, "unknown")
                    passed = stable and fact.started is True and fact.reason == "exited" and fact.exit_code == 0
                    checks.append({"name": name, "status": "passed" if passed else "stale" if not stable else "failed" if fact.started else "not_run",
                        "started": fact.started, "exit_code": fact.exit_code, "reason": fact.reason,
                        "input_version": before_version})
                    outputs.append({"name": name, "output": output[:12000]})
                    if not stable or group_start != after:
                        for check in checks:
                            if check["status"] == "passed":
                                check["status"] = "stale"
                        result = self._result("source_changed", "source_changed", checks, self._version(after), uncovered)
                        self._last_key = None
                        return json.dumps({**result, "diagnostics": outputs}, ensure_ascii=False)
                self._last_key = key
                self._group_cache[group_key] = copy.deepcopy(checks[group_offset:])
        end = self._scan()
        if end is None:
            result = self._result("unknown", "scan_unknown", checks)
        elif any(check["input_version"] != self._version(end) for check in checks):
            for check in checks:
                check["status"] = "stale"
            result = self._result("source_changed", "source_changed", checks, self._version(end), uncovered)
        elif any(check["status"] != "passed" for check in checks):
            result = self._result("failed", "checks_failed", checks, self._version(end), uncovered)
        elif any(item != "no_tests" for item in uncovered):
            result = self._result("not_run", "partial_coverage", checks, self._version(end), uncovered)
        else:
            self._checked_snapshot = end
            result = self._result("passed", "checked", checks, self._version(end), uncovered)
        return json.dumps({**result, "diagnostics": outputs}, ensure_ascii=False)

    def _discover(self) -> tuple[list, list[str]]:
        self._excluded_dirs.difference_update(self._output_dirs)
        self._output_dirs.clear()
        groups: dict[tuple[str, str], list[str]] = {}
        uncovered: list[str] = []
        for name in self._state["changed_paths"]:
            path = self.workspace / name
            ecosystem = "python" if path.suffix in {".py", ".pyi"} or path.name in {"pyproject.toml", "requirements.txt", "setup.cfg", "pytest.ini"} else "node" if path.suffix in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"} or path.name in {"package.json", "package-lock.json", "tsconfig.json"} else None
            ecosystem = ecosystem or self._entry_ecosystems.get(name)
            if ecosystem is None and path.exists():
                head, _ = _head(self.workspace, name)
                if head.startswith(b"#!"):
                    ecosystem = "python" if b"python" in head.splitlines()[0] else "node" if b"node" in head.splitlines()[0] else None
            root = self.workspace
            for parent in (path.parent, *path.parent.parents):
                if not parent.is_relative_to(self.workspace):
                    break
                if (parent / "package.json").is_file():
                    ecosystem, root = "node", parent
                    break
                if (parent / "pyproject.toml").is_file() or (parent / "requirements.txt").is_file():
                    ecosystem, root = "python", parent
                    break
            if ecosystem is None:
                uncovered.append("unsupported_code")
                continue
            groups.setdefault((root.relative_to(self.workspace).as_posix(), ecosystem), []).append(name)
        result, total = [], 0
        for (root, ecosystem), paths in sorted(groups.items()):
            commands = []
            package = self.workspace / root
            project_files = _project_files(self.workspace, package)
            if ecosystem == "python":
                commands = self._python_checks(root, package, paths, project_files, uncovered)
            else:
                target = package / "package.json"
                scripts = {}
                if target.is_file():
                    data = json.loads(_read_text(self.workspace, target.relative_to(self.workspace).as_posix()))
                    if not isinstance(data, dict):
                        raise ValueError("项目清单格式无效")
                    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
                    if not isinstance(scripts, dict):
                        raise ValueError("项目脚本格式无效")
                    self._typescript_outputs(package, project_files)
                for kind in ("typecheck", "build", "test"):
                    script = scripts.get(kind)
                    if isinstance(script, str) and script.strip() and not re.search(r"(?:--watch\b|\bwatch\b|\bdev\b|\bserve\b)", script):
                        commands.append((kind, {"argv": ["npm", "run", kind], "cwd": root, "timeout_seconds": 300}))
                if not commands:
                    dependencies = {}
                    if target.is_file():
                        for field in ("dependencies", "devDependencies"):
                            declared = data.get(field, {})
                            if not isinstance(declared, dict):
                                raise ValueError("项目依赖格式无效")
                            dependencies.update(declared)
                    if "typescript" in dependencies and (package / "tsconfig.json").is_file():
                        commands.append(("typecheck", {"argv": ["node", "node_modules/typescript/bin/tsc", "--noEmit"], "cwd": root, "timeout_seconds": 300}))
                    for name in paths:
                        if Path(name).suffix in {".js", ".mjs", ".cjs"} and (self.workspace / name).is_file():
                            relative = (self.workspace / name).relative_to(package).as_posix()
                            commands.append(("syntax", {"argv": ["node", "--check", relative], "cwd": root, "timeout_seconds": 60}))
                    if any(".test." in path.name or path.name.startswith("test-") for path in project_files if path.suffix in {".js", ".cjs", ".mjs"}):
                        commands.append(("test", {"argv": ["node", "--test"], "cwd": root, "timeout_seconds": 300}))
                if not any(kind == "test" for kind, _ in commands):
                    uncovered.append("no_tests")
            if len(commands) + total > MAX_CHECKS:
                uncovered.append("check_limit")
            commands = commands[:MAX_CHECKS - total]
            total += len(commands)
            if commands:
                result.append((root, ecosystem, commands))
        return result, list(dict.fromkeys(uncovered))[:MAX_CHECKS]

    def _python_checks(self, root: str, package: Path, paths: list[str],
                       project_files: list[Path], uncovered: list[str]) -> list:
        """Select Python commands and append coverage gaps in discovery order."""
        commands = []
        files = [str(Path(name).relative_to(root)) if root != "." else name for name in paths if (self.workspace / name).is_file() and (Path(name).suffix in {".py", ".pyi"} or not Path(name).suffix)]
        if files:
            script = "import pathlib,sys; [compile(pathlib.Path(p).read_bytes(), p, 'exec') for p in sys.argv[1:]]"
            commands.append(("syntax", {"argv": ["python", "-c", script, *files], "cwd": root, "timeout_seconds": 60}))
        candidates = [p for p in project_files if (p.name.startswith("test_") or p.name.startswith("check_")) and p.suffix == ".py"]
        pytest_declared = (package / "pytest.ini").is_file()
        for filename in ("requirements.txt", "pyproject.toml"):
            target = package / filename
            if target.is_file():
                pytest_declared = pytest_declared or bool(re.search(r"\bpytest\b", _read_text(self.workspace, target.relative_to(self.workspace).as_posix())))
        unittest_files, direct_files = [], []
        for candidate in candidates[:MAX_CHANGED_PATHS]:
            try:
                tree = ast.parse(_read_text(self.workspace, candidate.relative_to(self.workspace).as_posix()))
            except SyntaxError:
                pytest_declared = True
                continue
            has_unittest = any(isinstance(node, ast.ClassDef) and any(
                (isinstance(base, ast.Attribute) and base.attr == "TestCase") or (isinstance(base, ast.Name) and base.id == "TestCase") for base in node.bases) for node in ast.walk(tree))
            has_functions = any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_") for node in ast.iter_child_nodes(tree))
            if has_functions:
                pytest_declared = True
            elif has_unittest:
                unittest_files.append(candidate)
            else:
                direct_files.append(candidate)
        if len(candidates) > MAX_CHANGED_PATHS:
            uncovered.append("check_limit")
        if pytest_declared:
            commands.append(("test", {"argv": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"], "cwd": root, "timeout_seconds": 300}))
        elif unittest_files:
            # Explicit modules prevent a zero-test directory discovery from looking like coverage.
            for candidate in unittest_files[:MAX_CHECKS]:
                relative = candidate.relative_to(package).as_posix()
                script = "import runpy,unittest as u,sys,types;n=u.defaultTestLoader.loadTestsFromModule(types.SimpleNamespace(**runpy.run_path(sys.argv[1])));r=u.TextTestRunner().run(n);sys.exit(not(r.testsRun and r.wasSuccessful()))"
                commands.append(("test", {"argv": ["python", "-c", script, relative], "cwd": root, "timeout_seconds": 300}))
        elif direct_files:
            for candidate in direct_files[:MAX_CHECKS]:
                commands.append(("test", {"argv": ["python", candidate.relative_to(package).as_posix()], "cwd": root, "timeout_seconds": 300}))
        else:
            uncovered.append("no_tests")
        return commands

    def _typescript_outputs(self, package: Path, files: list[Path]) -> None:
        target = package / "tsconfig.json"
        if not target.is_file():
            return
        try:
            config_text = _read_text(self.workspace, target.relative_to(self.workspace).as_posix())
            config = json.loads(config_text)
        except json.JSONDecodeError:
            return  # JSONC/extends need compiler-specific discovery; do not guess an exclusion.
        if not isinstance(config, dict) or "extends" in config:
            return
        options = config.get("compilerOptions", {})
        if not isinstance(options, dict) or not isinstance(options.get("outDir"), str):
            return
        name = _path((package.relative_to(self.workspace) / options["outDir"]).as_posix())
        output = _resolve_workspace_path(self.workspace, name)
        _reject_symlinks(self.workspace, name)
        if output == package or package.is_relative_to(output):
            return
        protected = [self.workspace / path for path in self._state["changed_paths"]] + [self.workspace / path for path in self._entries]
        root_dir = options.get("rootDir")
        if isinstance(root_dir, str):
            protected.append(_resolve_workspace_path(self.workspace, (package.relative_to(self.workspace) / root_dir).as_posix()))
        if any(path == output or path.is_relative_to(output) for path in protected):
            return
        # Existing JavaScript may be source. Only an empty output root first observed
        # under this exact config, then generated by our checks, is safely reusable.
        identity = hashlib.sha256(config_text.encode()).hexdigest()
        if any(path.is_relative_to(output) for path in files) and self._generated_outputs.get(name) != identity:
            return
        self._generated_outputs[name] = identity
        self._excluded_dirs.add(name)
        self._output_dirs.add(name)
