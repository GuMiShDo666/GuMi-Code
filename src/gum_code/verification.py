"""Bounded input scans and read-only parsing of historical task.state records."""

import copy
import hashlib
import json
import os
import re
import stat
import time
import unicodedata
import uuid
from collections.abc import Callable
from pathlib import Path

from gum_code.tools.common import _is_sensitive_path, _reject_symlinks

MAX_CHECKS = 8
MAX_ARGUMENTS = 32
MAX_ARGUMENT_CHARS = 500
MAX_DEFINITION_FILES = 16
MAX_OUTPUT_DIRS = 16
MAX_TEXT_CHARS = 2000
MAX_TASK_STATE_BYTES = 64000
MAX_SCAN_FILES = 20000
MAX_SCAN_BYTES = 128 * 1024 * 1024
MAX_SCAN_SECONDS = 5.0
_STATUSES = {"running", "completed", "incomplete", "failed", "interrupted"}
_CHECK_STATUSES = {"pending", "passed", "failed", "stale", "not_started"}
_DEFINITION_KEYS = {
    "id", "purpose", "argv", "cwd", "timeout_seconds", "definition_files", "output_dirs", "dependency_dirs"
}
_STATE_KEYS = {
    "version", "task_id", "goal", "requires_verification", "status", "stop_reason",
    "steps", "checks", "review",
}
_SECRET = re.compile(
    r"(?i)(authorization\s*[:=]|bearer\s|(?:api[_-]?key|password|secret|access[_-]?token)\s*[=:]|"
    r"^--?(?:api[_-]?key|password|secret|token)$)"
)

_SCAN_MESSAGES = {
    "scan_limit": "扫描超过目录项、字节或时间预算；当前输入范围无法确认。",
    "scan_path": "扫描路径、链接或文件类型不安全；请检查入口和排除根，纳入输入不支持链接。",
    "scan_read": "扫描输入无法读取；请检查普通输入的存在性和读取权限。",
    "scan_changed": "扫描期间输入发生变化；请停止并发写入后重试。",
}


class _ScanError(ValueError):
    """Expose only fixed diagnostics, never filesystem exception details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"{code}: {_SCAN_MESSAGES[code]}")


def _text(value: object, *, maximum: int = MAX_TEXT_CHARS, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError("验证文本为空或超过限制")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in value):
        raise ValueError("验证文本不允许控制字符")
    if _SECRET.search(value):
        raise ValueError("验证清单不得包含凭据或凭据参数")
    return value


def _relative(value: object, *, allow_root: bool = False) -> str:
    text = _text(value, maximum=MAX_ARGUMENT_CHARS)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or "\\" in text:
        raise ValueError("验证路径必须位于工作区内")
    normalized = path.as_posix()
    if normalized == "." and not allow_root:
        raise ValueError("验证路径不能是工作区根目录")
    if _is_sensitive_path(path) or _is_sensitive_path(Path(normalized.lower())):
        raise ValueError("验证不能读取敏感或内部路径")
    return normalized


def _paths(value: object, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("验证路径列表超过限制")
    return sorted(set(_relative(item) for item in value))


def normalize_command(arguments: object) -> dict[str, object]:
    if not isinstance(arguments, dict) or not set(arguments) <= {"argv", "cwd", "timeout_seconds"}:
        raise ValueError("验证命令参数无效")
    argv = arguments.get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGUMENTS:
        raise ValueError("验证命令参数列表无效")
    argv = [_text(item, maximum=MAX_ARGUMENT_CHARS) for item in argv]
    timeout = arguments.get("timeout_seconds", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise ValueError("验证超时必须为 1 至 300 秒")
    return {"argv": argv, "cwd": _relative(arguments.get("cwd", "."), allow_root=True),
            "timeout_seconds": timeout}


def _definition(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not {"id", "purpose", "argv"} <= set(value) <= _DEFINITION_KEYS:
        raise ValueError("验证项字段无效")
    check_id = _text(value["id"], maximum=64)
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", check_id):
        raise ValueError("验证项 ID 无效")
    result = {"id": check_id, "purpose": _text(value["purpose"])}
    result.update(normalize_command({key: value[key] for key in ("argv", "cwd", "timeout_seconds") if key in value}))
    result["definition_files"] = _paths(value.get("definition_files", []), MAX_DEFINITION_FILES)
    result["output_dirs"] = _paths(value.get("output_dirs", []), MAX_OUTPUT_DIRS)
    result["dependency_dirs"] = _paths(value.get("dependency_dirs", []), MAX_OUTPUT_DIRS)
    return result


def validate_task_state(value: object) -> dict[str, object]:
    """Validate private historical state; no field can encode an execution grant."""
    if not isinstance(value, dict) or set(value) != _STATE_KEYS or value.get("version") != 1:
        raise ValueError("任务验证状态格式无效")
    if isinstance(value["version"], bool):
        raise ValueError("任务验证状态版本无效")
    try:
        uuid.UUID(str(value["task_id"]))
    except (ValueError, AttributeError):
        raise ValueError("任务验证标识无效") from None
    _text(value["goal"], empty=True)
    if value["requires_verification"] is not None and not isinstance(value["requires_verification"], bool):
        raise ValueError("任务类型无效")
    if not isinstance(value["status"], str) or value["status"] not in _STATUSES:
        raise ValueError("任务验证状态无效")
    _text(value["stop_reason"], maximum=64, empty=True)
    if isinstance(value["steps"], bool) or not isinstance(value["steps"], int) or value["steps"] < 0:
        raise ValueError("任务步骤无效")
    checks = value["checks"]
    if not isinstance(checks, list) or len(checks) > MAX_CHECKS:
        raise ValueError("验证清单超过限制")
    value = copy.deepcopy(value)
    checks = value["checks"]
    ids = set()
    for check in checks:
        required = (_DEFINITION_KEYS - {"dependency_dirs"}) | {"status", "evidence"}
        if not isinstance(check, dict) or not required <= set(check) <= _DEFINITION_KEYS | {"status", "evidence"}:
            raise ValueError("验证项状态无效")
        check.setdefault("dependency_dirs", [])
        definition = _definition({key: check[key] for key in _DEFINITION_KEYS})
        if definition["id"] in ids or not isinstance(check["status"], str) or check["status"] not in _CHECK_STATUSES:
            raise ValueError("验证项标识或状态无效")
        ids.add(definition["id"])
        evidence = check["evidence"]
        if evidence is not None:
            if not isinstance(evidence, dict) or set(evidence) != {"version", "started", "exit_code", "reason"}:
                raise ValueError("验证证据格式无效")
            if not isinstance(evidence["version"], str) or not re.fullmatch(r"[0-9a-f]{64}", evidence["version"]):
                raise ValueError("验证证据版本无效")
            if evidence["started"] is not None and not isinstance(evidence["started"], bool):
                raise ValueError("验证执行状态无效")
            code = evidence["exit_code"]
            if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
                raise ValueError("验证退出码无效")
            _text(evidence["reason"], maximum=64)
    review = value["review"]
    if review is not None:
        if not isinstance(review, dict) or set(review) != {"summary", "unresolved", "version"}:
            raise ValueError("自查记录无效")
        _text(review["summary"])
        if not isinstance(review["unresolved"], list) or len(review["unresolved"]) > MAX_CHECKS:
            raise ValueError("自查未解决项无效")
        for item in review["unresolved"]:
            _text(item)
        if not isinstance(review["version"], str) or not re.fullmatch(r"[0-9a-f]{64}", review["version"]):
            raise ValueError("自查版本无效")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_TASK_STATE_BYTES:
        raise ValueError("任务验证状态超过限制")
    return value


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _file_hash(root: Path, name: str, charge: Callable[[int], None]) -> tuple[str, int]:
    name = _relative(name)
    target = root / name
    try:
        _reject_symlinks(root, name)
    except ValueError:
        raise _ScanError("scan_path") from None
    if not target.resolve().is_relative_to(root):
        raise _ScanError("scan_path")
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise _ScanError("scan_path")
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or _signature(opened) != _signature(info):
            raise _ScanError("scan_changed")
        digest = hashlib.sha256()
        while chunk := source.read(65536):
            charge(len(chunk))
            digest.update(chunk)
        if _signature(os.fstat(source.fileno())) != _signature(info):
            raise _ScanError("scan_changed")
    try:
        _reject_symlinks(root, name)
    except ValueError:
        raise _ScanError("scan_path") from None
    if _signature(target.lstat()) != _signature(info):
        raise _ScanError("scan_changed")
    return digest.hexdigest(), info.st_mode & 0o111


def _validate_excluded_dirs(root: Path, paths: tuple[str, ...]) -> None:
    for path in paths:
        try:
            path = _relative(path)
            _reject_symlinks(root, path)
            target = root / path
            if target.exists() and not target.is_dir():
                raise ValueError("not a directory")
        except (ValueError, RuntimeError):
            raise _ScanError("scan_path") from None


def _workspace_scan(workspace: Path, output_dirs: tuple[str, ...] = (),
                    internal_paths: tuple[Path, ...] = ()) -> tuple[str, dict[str, tuple[str, int]]]:
    """Hash bounded ordinary inputs, failing closed on races, links, or limits."""
    root = workspace.resolve()
    started = time.monotonic()
    count = total = 0
    digest = hashlib.sha256()
    snapshot: dict[str, tuple[str, int]] = {}

    def check_limits() -> None:
        if count > MAX_SCAN_FILES or total > MAX_SCAN_BYTES or time.monotonic() - started > MAX_SCAN_SECONDS:
            raise _ScanError("scan_limit")

    def charge(size: int) -> None:
        nonlocal total
        total += size
        check_limits()

    def visit(directory: Path) -> None:
        nonlocal count, total
        before = directory.stat()
        entries = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                count += 1
                check_limits()
                entries.append(entry)
        for entry in sorted(entries, key=lambda item: item.name):
            check_limits()
            target = Path(entry.path)
            relative = target.relative_to(root)
            if _is_sensitive_path(relative) or _is_sensitive_path(Path(relative.as_posix().lower())):
                continue
            if any(relative == Path(output) or Path(output) in relative.parents for output in output_dirs):
                continue
            info = target.lstat()
            if target in internal_paths:
                if not stat.S_ISREG(info.st_mode):
                    raise _ScanError("scan_path")
                continue
            if stat.S_ISLNK(info.st_mode):
                raise _ScanError("scan_path")
            _reject_symlinks(root, relative.as_posix())
            if not target.resolve().is_relative_to(root):
                raise _ScanError("scan_path")
            digest.update(json.dumps([relative.as_posix(), stat.S_IFMT(info.st_mode), info.st_mode & 0o111]).encode())
            if stat.S_ISDIR(info.st_mode):
                snapshot[relative.as_posix()] = ("__directory__", info.st_mode & 0o111)
                visit(target)
            elif stat.S_ISREG(info.st_mode):
                file_hash, _ = _file_hash(root, relative.as_posix(), charge)
                snapshot[relative.as_posix()] = (file_hash, info.st_mode & 0o111)
                digest.update(file_hash.encode())
            else:
                raise _ScanError("scan_path")
        if _signature(directory.stat()) != _signature(before):
            raise _ScanError("scan_changed")

    try:
        _validate_excluded_dirs(root, output_dirs)
        visit(root)
        _validate_excluded_dirs(root, output_dirs)
    except OSError:
        raise _ScanError("scan_read") from None
    except _ScanError:
        raise
    except (ValueError, RuntimeError):
        raise _ScanError("scan_path") from None
    return digest.hexdigest(), snapshot


def workspace_fingerprint(workspace: Path, output_dirs: tuple[str, ...] = (),
                          internal_paths: tuple[Path, ...] = ()) -> str:
    """Retain the historical fingerprint while sharing the safe scan boundary."""
    return _workspace_scan(workspace, output_dirs, internal_paths)[0]


def workspace_snapshot(workspace: Path, output_dirs: tuple[str, ...] = (),
                       internal_paths: tuple[Path, ...] = ()) -> dict[str, tuple[str, int]]:
    """Return bounded content identities for observing actual command changes."""
    return _workspace_scan(workspace, output_dirs, internal_paths)[1]


# Historical prompt marker only: remove it when continuing old Sessions.
VERIFICATION_INSTRUCTION = (
    "开发阶段必须先用 verification plan 登记必要构建/测试与覆盖目的，再用 run_command 实测。"
    "大型依赖/生成目录可分别用 dependency_dirs/output_dirs 提议明确排除，用户批准前不生效；源码、配置和锁文件不得排除。"
    "失败后在已授权范围修复并重测；不得删除检查、降低断言或屏蔽错误骗取通过。"
    "完成前用 verification review 提交需求覆盖、改动自查及 unresolved 列表。"
    "普通工具输出、Todo 和你的完成声明不能替代宿主验收。无法验证时说明阻塞，不声称完成。"
)
