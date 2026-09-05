"""Keep bounded verification evidence separate from model claims and permission."""

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
MAX_VERIFICATION_APPROVAL_CHARS = 12000
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

VerificationApprovalCallback = Callable[[dict[str, object]], bool]

_SCAN_MESSAGES = {
    "scan_limit": "扫描超过目录项、字节或时间预算；请明确批准依赖/产物目录排除，纳入源码仍受原预算限制。",
    "scan_path": "扫描路径、链接或文件类型不安全；请检查入口和排除根，纳入输入不支持链接。",
    "scan_read": "扫描输入无法读取；请检查普通输入的存在性和读取权限。",
    "scan_changed": "扫描期间输入发生变化；请停止并发写入后重试。",
    "scope_approval": "扫描排除范围尚未批准或许可已失效；请重新提交计划并批准范围。",
    "version_unavailable": "工作版本无法确认；请检查输入及扫描条件后重试。",
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
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        if _signature(os.fstat(source.fileno())) != _signature(info):
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


def workspace_fingerprint(workspace: Path, output_dirs: tuple[str, ...] = (),
                          internal_paths: tuple[Path, ...] = ()) -> str:
    """Hash bounded ordinary inputs, failing closed on races, links, or limits."""
    root = workspace.resolve()
    started = time.monotonic()
    count = total = 0
    digest = hashlib.sha256()

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
                visit(target)
            elif stat.S_ISREG(info.st_mode):
                file_hash, _ = _file_hash(root, relative.as_posix(), charge)
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
    return digest.hexdigest()


class VerificationRuntime:
    """Own one milestone's historical evidence and non-persistent grants."""

    def __init__(self, workspace: Path, *, requires_verification: bool | None = None,
                 initial_state: dict[str, object] | None = None,
                 approval_callback: VerificationApprovalCallback | None = None,
                 internal_paths: tuple[Path, ...] = ()) -> None:
        self.workspace = workspace.resolve()
        self.approval_callback = approval_callback
        self._grants: set[str] = set()
        self._denied: set[str] = set()
        self._approved_outputs: tuple[str, ...] = ()
        self._approval_sandbox: object = None
        self._before_command: str | None = None
        self._finished = False
        self._internal_paths: tuple[Path, ...] = ()
        self.set_internal_paths(internal_paths)
        self._state = {
            "version": 1, "task_id": str(uuid.uuid4()), "goal": "",
            "requires_verification": requires_verification, "status": "running",
            "stop_reason": "", "steps": 0, "checks": [], "review": None,
        }
        if initial_state is not None:
            self._state = validate_task_state(initial_state)
            if requires_verification is True or self.requires_verification is None:
                self._state["requires_verification"] = requires_verification
            self.invalidate()
            self._state["status"] = "incomplete"
            self._state["stop_reason"] = "restored"
        validate_task_state(self._state)

    @property
    def terminal_state(self) -> str:
        return str(self._state["status"])

    @property
    def requires_verification(self) -> bool | None:
        return self._state["requires_verification"]

    def export_state(self) -> dict[str, object]:
        return validate_task_state(self._state)

    def set_internal_paths(self, paths: tuple[Path, ...]) -> None:
        """Exclude only host-owned Session files, never model-declared directories."""
        if not isinstance(paths, tuple) or len(paths) > 4:
            raise ValueError("内部状态路径无效")
        checked = []
        for path in paths:
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError("内部状态路径必须是绝对路径")
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError("内部状态路径必须是普通文件")
            checked.append(path.absolute())
        result = tuple(checked)
        if self._internal_paths != result and hasattr(self, "_state"):
            self.invalidate()
        self._internal_paths = result

    def begin_run(self, user_query: str, *, requires_verification: bool | None = None) -> None:
        if requires_verification is True:
            self._state["requires_verification"] = True
        elif requires_verification is False and self.requires_verification is None:
            self._state["requires_verification"] = False
        if not self._state["goal"]:
            # User text stays in messages; bounded task metadata must not carry secrets.
            self._state["goal"] = "当前开发阶段" if self.requires_verification else "当前任务"
        self._state.update(status="running", stop_reason="", steps=0)
        self._finished = False

    def invalidate(self) -> None:
        for check in self._state["checks"]:
            if check["status"] == "passed":
                check["status"] = "stale"
        self._state["review"] = None

    def require(self) -> None:
        self._state["requires_verification"] = True

    def _version(self) -> str:
        try:
            checks = self._definitions()
            if self._exclusions(checks):
                if not self._approved_outputs:
                    raise _ScanError("scope_approval")
                if self._identity(checks, self._approval_sandbox)[0] not in self._grants:
                    self.revoke_grants()
                    raise _ScanError("scope_approval")
            version = workspace_fingerprint(self.workspace, self._approved_outputs, self._internal_paths)
        except (ValueError, OSError, RuntimeError) as error:
            code = error.code if isinstance(error, _ScanError) else (
                "scan_read" if isinstance(error, OSError) else "version_unavailable"
            )
            self._state["stop_reason"] = code
            raise _ScanError(code) from None
        if self._state["stop_reason"] in _SCAN_MESSAGES:
            self._state["stop_reason"] = ""
        return version

    def _definitions(self) -> list[dict[str, object]]:
        return [{key: check[key] for key in _DEFINITION_KEYS} for check in self._state["checks"]]

    def _protect_entries(self, check: dict[str, object]) -> None:
        cwd = self.workspace / check["cwd"]
        try:
            _reject_symlinks(self.workspace, check["cwd"])
        except ValueError:
            raise _ScanError("scan_path") from None
        if not cwd.is_dir():
            raise _ScanError("scan_path")
        definitions = set(check["definition_files"])
        program = Path(check["argv"][0]).name
        config_flags = {"-c", "--config", "--config-file", "--configuration", "--manifest-path", "-f", "--file", "--project"}
        for index, token in enumerate(check["argv"]):
            candidate = token.split("=", 1)[-1] if token.startswith("-") and "=" in token else token
            if candidate.startswith("-"):
                continue
            target = cwd / candidate
            explicit_config = (token.startswith("-") and "=" in token) or (
                index > 0 and check["argv"][index - 1] in config_flags
            )
            direct_script = index == 1 and (program.startswith("python") or program in {"node", "nodejs"})
            if target.is_file() and (index == 0 or explicit_config or direct_script or Path(candidate).suffix or "/" in candidate):
                if not target.resolve().is_relative_to(self.workspace):
                    raise _ScanError("scan_path")
                definitions.add(_relative(target.relative_to(self.workspace).as_posix()))
        conventional = {
            "npm": ("package.json", "package-lock.json", "npm-shrinkwrap.json"),
            "pnpm": ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"),
            "yarn": ("package.json", "yarn.lock", ".yarnrc.yml"),
            "bun": ("package.json", "bun.lock", "bun.lockb"),
            "pytest": ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "uv.lock", "poetry.lock", "Pipfile.lock"),
            "cargo": ("Cargo.toml", "Cargo.lock"), "make": ("Makefile", "GNUmakefile"),
            "go": ("go.mod", "go.work", "go.sum", "go.work.sum"),
        }
        names = conventional.get(program, ())
        if program.startswith("python") and "pytest" in check["argv"]:
            names = conventional["pytest"]
        for directory in (cwd, *cwd.parents):
            if not directory.is_relative_to(self.workspace):
                break
            for name in names:
                if (directory / name).is_file():
                    definitions.add((directory / name).relative_to(self.workspace).as_posix())
        check["definition_files"] = _paths(sorted(definitions), MAX_DEFINITION_FILES)
        for name in check["definition_files"]:
            try:
                _reject_symlinks(self.workspace, name)
            except ValueError:
                raise _ScanError("scan_path") from None
            if not (self.workspace / name).is_file():
                raise _ScanError("scan_read")

    def _identity(self, checks: list[dict[str, object]], sandbox: object) -> tuple[str, dict[str, object]]:
        image_id = getattr(sandbox, "image_id", None)
        if not isinstance(image_id, str) or not image_id:
            raise ValueError("验证需要配置 Docker Sandbox")
        definitions = []
        total = 0
        started = time.monotonic()
        def charge(size: int) -> None:
            nonlocal total
            total += size
            if total > MAX_SCAN_BYTES or time.monotonic() - started > MAX_SCAN_SECONDS:
                raise _ScanError("scan_limit")
        for check in checks:
            self._protect_entries(check)
            for name in check["definition_files"]:
                file_hash, mode = _file_hash(self.workspace, name, charge)
                definitions.append((name, file_hash, mode))
        self._validate_outputs(checks)
        bundle = {"task_id": self._state["task_id"], "goal": self._state["goal"],
                  "workspace": str(self.workspace), "image_id": image_id, "checks": checks}
        serialized = json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2)
        if len(serialized) > MAX_VERIFICATION_APPROVAL_CHARS:
            raise ValueError("验证批准内容超过完整展示限制")
        digest = hashlib.sha256((serialized + json.dumps(definitions)).encode()).hexdigest()
        return digest, bundle

    def _approve(self, checks: list[dict[str, object]], sandbox: object) -> bool:
        key, bundle = self._identity(checks, sandbox)
        if key in self._grants:
            return True
        if key in self._denied or self.approval_callback is None:
            return False
        try:
            accepted = self.approval_callback(copy.deepcopy(bundle)) is True
        except (Exception, KeyboardInterrupt, EOFError):
            accepted = False
        if not accepted:
            self._denied.add(key)
            return False
        if self._identity(copy.deepcopy(checks), sandbox)[0] != key:
            raise ValueError("验证定义在批准期间发生变化")
        self._grants = {key}
        self._approved_outputs = self._exclusions(checks)
        self._approval_sandbox = sandbox
        return True

    @staticmethod
    def _repeatable(check: dict[str, object]) -> bool:
        argv = check["argv"]
        program = Path(argv[0]).name
        if program.startswith("python"):
            return argv[1:3] == ["-m", "pytest"] or (
                len(argv) > 1 and not argv[1].startswith("-") and
                (Path(check["cwd"]) / argv[1]).as_posix() in check["definition_files"]
            )
        if program in {"node", "nodejs"}:
            return len(argv) > 1 and not argv[1].startswith("-") and (
                Path(check["cwd"]) / argv[1]).as_posix() in check["definition_files"]
        conventional = {
            "npm": {"package.json"}, "pnpm": {"package.json"},
            "yarn": {"package.json"}, "bun": {"package.json"},
            "cargo": {"Cargo.toml"}, "make": {"Makefile", "GNUmakefile"},
        }
        if program in conventional:
            return any(Path(name).name in conventional[program] for name in check["definition_files"])
        return program in {"pytest", "go"} or (
            "/" in argv[0] and (Path(check["cwd"]) / argv[0]).as_posix() in check["definition_files"]
        )

    @staticmethod
    def _exclusions(checks: list[dict[str, object]]) -> tuple[str, ...]:
        return tuple(sorted({path for check in checks for field in ("output_dirs", "dependency_dirs") for path in check[field]}))

    def _validate_outputs(self, checks: list[dict[str, object]]) -> None:
        outputs = self._exclusions(checks)
        protected = {path for check in checks for path in check["definition_files"]}
        if any(Path(output) == Path(path) or Path(output) in Path(path).parents for output in outputs for path in protected):
            raise ValueError("输出排除不得覆盖验证定义或入口")
        _validate_excluded_dirs(self.workspace, outputs)

    def plan(self, arguments: dict[str, object], sandbox: object = None,
             before_execute: Callable[[], None] | None = None) -> None:
        if set(arguments) != {"action", "goal", "checks"}:
            raise ValueError("验证计划字段无效")
        goal = _text(arguments["goal"])
        raw = arguments["checks"]
        if not isinstance(raw, list) or len(raw) > MAX_CHECKS:
            raise ValueError("验证清单超过限制")
        checks = [_definition(item) for item in raw]
        if len({item["id"] for item in checks}) != len(checks):
            raise ValueError("验证项 ID 重复")
        for check in checks:
            self._protect_entries(check)
        self._validate_outputs(checks)
        unchanged = self._definitions() == checks and self._state["goal"] == goal
        previous_goal = self._state["goal"]
        previous_grants = self._grants.copy()
        previous_outputs = self._approved_outputs
        previous_sandbox = self._approval_sandbox
        self._state["goal"] = goal
        try:
            if unchanged:
                if self._exclusions(checks) and not self._approve(checks, sandbox):
                    raise _ScanError("scope_approval")
                if before_execute is not None:
                    before_execute()
                return
            if self._state["checks"] and not self._approve(checks, sandbox):
                raise ValueError("改变验证清单需要重新批准")
            if not self._state["checks"] and sandbox is not None and self.approval_callback is not None and (
                self._exclusions(checks) or all(self._repeatable(check) for check in checks)
            ):
                self._approve(checks, sandbox)
            candidate = {**self._state, "requires_verification": True, "review": None,
                         "stop_reason": "no_checks" if not checks else (
                             "scope_approval" if self._exclusions(checks) and not self._approved_outputs else ""
                         ),
                         "checks": [{**check, "status": "pending", "evidence": None} for check in checks]}
            candidate = validate_task_state(candidate)
            if before_execute is not None:
                before_execute()
            self._state = candidate
        except BaseException:
            self._state["goal"] = previous_goal
            self._grants = previous_grants
            self._approved_outputs = previous_outputs
            self._approval_sandbox = previous_sandbox
            raise

    def wrap_approval(self, callback: Callable | None, sandbox: object) -> Callable:
        def approve(tool_name: str, arguments: dict[str, object], reason: str) -> bool:
            matched = self._match(arguments) if tool_name == "run_command" else None
            if matched is not None:
                if self.approval_callback is not None:
                    try:
                        repeatable = self._repeatable(matched)
                        if repeatable or self._exclusions(self._definitions()):
                            if not self._approve(self._definitions(), sandbox):
                                return False
                        if repeatable:
                            return True
                    except (ValueError, OSError):
                        return False
            return bool(callback and callback(tool_name, arguments, reason))
        return approve

    def _match(self, arguments: object) -> dict[str, object] | None:
        try:
            command = normalize_command(arguments)
        except ValueError:
            return None
        return next((check for check in self._state["checks"] if all(check[key] == val for key, val in command.items())), None)

    def before_tool(self, tool_name: str) -> None:
        if tool_name in {"write_file", "edit_file", "delete_file"}:
            self.require()
            self.invalidate()
        if tool_name == "run_command":
            try:
                self._before_command = self._version()
            except ValueError:
                self._before_command = None

    def observe_command(self, arguments: object, outcome: object | None) -> None:
        check = self._match(arguments)
        try:
            after = self._version()
        except ValueError:
            after = None
        before = self._before_command
        self._before_command = None
        if before != after or before is None:
            self.invalidate()
            self.require()
        if check is None:
            return
        started = getattr(outcome, "started", False)
        code = getattr(outcome, "exit_code", None)
        reason = getattr(outcome, "reason", "not_started")
        check["status"] = "not_started" if started is not True else "failed"
        check["evidence"] = None
        if before is not None:
            check["evidence"] = {"version": before, "started": started, "exit_code": code, "reason": reason}
        if before is not None and before == after and started is True and code == 0 and reason == "exited":
            check["status"] = "passed"

    def review(self, arguments: dict[str, object],
               before_execute: Callable[[], None] | None = None) -> None:
        if set(arguments) != {"action", "summary", "unresolved"}:
            raise ValueError("自查字段无效")
        summary = _text(arguments["summary"])
        unresolved = arguments["unresolved"]
        if not isinstance(unresolved, list) or len(unresolved) > MAX_CHECKS:
            raise ValueError("自查未解决项超过限制")
        unresolved = [_text(item) for item in unresolved]
        review = {"summary": summary, "unresolved": unresolved, "version": self._version()}
        if before_execute is not None:
            before_execute()
        self._state["review"] = review

    def execute(self, arguments_json: str, sandbox: object = None,
                before_execute: Callable[[], None] | None = None) -> str:
        callback_failed = False
        def notify_execution() -> None:
            nonlocal callback_failed
            if before_execute is not None:
                try:
                    before_execute()
                except BaseException:
                    callback_failed = True
                    raise
        try:
            arguments = json.loads(arguments_json)
            if not isinstance(arguments, dict):
                raise ValueError("验证参数必须是对象")
            action = arguments.get("action")
            if action not in {"plan", "review", "status"}:
                raise ValueError("未知验证动作")
            if action == "status" and set(arguments) != {"action"}:
                raise ValueError("验证状态参数无效")
            if action == "plan":
                self.plan(arguments, sandbox, notify_execution)
            elif action == "review":
                self.review(arguments, notify_execution)
            else:
                notify_execution()
                # Existing context tool-result budgeting can persist this bounded
                # history; the model can recover exact commands after compaction.
                return json.dumps(self.export_state(), ensure_ascii=False)
            return self.summary()
        except _ScanError as error:
            if callback_failed:
                raise
            self._state["stop_reason"] = error.code
            return f"Error: {error}"
        except OSError:
            if callback_failed:
                raise
            self._state["stop_reason"] = "scan_read"
            return f"Error: {_ScanError('scan_read')}"
        except (ValueError, TypeError):
            if callback_failed:
                raise
            return "Error: 验证参数、入口、批准或工作版本无法确认；请检查清单与定义。"

    def completion(self, *, command_available: bool) -> tuple[bool, bool, str]:
        """Return (complete, can_continue, reason), without trusting model text."""
        if self.requires_verification is False:
            return True, False, "not_required"
        if self.requires_verification is None:
            return False, False, "unknown_intent"
        if not command_available:
            return False, False, "command_unavailable"
        if not self._state["checks"]:
            if self._state["stop_reason"] == "no_checks":
                return False, False, "no_checks"
            return False, True, "missing_plan"
        try:
            version = self._version()
        except _ScanError as error:
            self.invalidate()
            return False, False, error.code
        for check in self._state["checks"]:
            if check["evidence"] and check["evidence"]["version"] != version:
                check["status"] = "stale"
            if check["status"] == "not_started":
                return False, False, "approval_or_environment"
        if any(check["status"] != "passed" for check in self._state["checks"]):
            return False, True, "checks_pending"
        review = self._state["review"]
        if review is None or review["version"] != version:
            return False, True, "review_required"
        if review["unresolved"]:
            return False, True, "unresolved_review"
        return True, False, "verified"

    def finish(self, status: str, reason: str) -> bool:
        if self._finished:
            return False
        self._state.update(status=status, stop_reason=reason)
        if status in {"completed", "failed", "interrupted"}:
            if status != "completed" and self._approved_outputs:
                self.invalidate()
            self._grants.clear()
            self._denied.clear()
            self._approved_outputs = ()
            self._approval_sandbox = None
        self._finished = True
        return True

    def persistence_failed(self) -> None:
        self._state.update(status="failed", stop_reason="persistence_error")
        self.revoke_grants()

    def revoke_grants(self) -> None:
        """Host-only revocation; history survives but future execution must ask."""
        self._grants.clear()
        self._denied.clear()
        if self._approved_outputs:
            self.invalidate()
        self._approved_outputs = ()
        self._approval_sandbox = None

    def set_step(self, step: int) -> None:
        self._state["steps"] = step

    def summary(self, *, status: str | None = None, reason: str | None = None) -> str:
        checks = ", ".join(f"{check['id']}={check['status']}" for check in self._state["checks"])
        reason = reason or self._state["stop_reason"] or "尚未结束"
        scope = "仅针对批准输入范围；排除目录未逐文件核验，锁文件不证明其内容未变。" if self._exclusions(self._definitions()) else ""
        return f"验收状态：{status or self._state['status']}；原因：{reason}；检查：{checks or '未登记'}。{_SCAN_MESSAGES.get(reason, '')}{scope}"


VERIFICATION_TOOL_SCHEMA = {
    "type": "function", "function": {
        "name": "verification",
        "description": "Plan required checks, record a source-version-bound self-review, or inspect verification. Never grants completion by model claim.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["plan", "review", "status"]},
            "goal": {"type": "string", "maxLength": MAX_TEXT_CHARS},
            "checks": {"type": "array", "maxItems": MAX_CHECKS, "items": {
                "type": "object", "properties": {
                    "id": {"type": "string", "maxLength": 64},
                    "purpose": {"type": "string", "maxLength": MAX_TEXT_CHARS},
                    "argv": {"type": "array", "minItems": 1, "maxItems": MAX_ARGUMENTS,
                             "items": {"type": "string", "maxLength": MAX_ARGUMENT_CHARS}},
                    "cwd": {"type": "string", "maxLength": MAX_ARGUMENT_CHARS},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                    "definition_files": {"type": "array", "maxItems": MAX_DEFINITION_FILES,
                                         "items": {"type": "string", "maxLength": MAX_ARGUMENT_CHARS}},
                    "output_dirs": {"type": "array", "maxItems": MAX_OUTPUT_DIRS,
                                    "items": {"type": "string", "maxLength": MAX_ARGUMENT_CHARS}},
                    "dependency_dirs": {"type": "array", "maxItems": MAX_OUTPUT_DIRS,
                                        "description": "Concrete installed-dependency directories to exclude only after explicit scope approval; source, config and lock files must remain checked.",
                                        "items": {"type": "string", "maxLength": MAX_ARGUMENT_CHARS}},
                }, "required": ["id", "purpose", "argv"], "additionalProperties": False,
            }},
            "summary": {"type": "string", "maxLength": MAX_TEXT_CHARS},
            "unresolved": {"type": "array", "maxItems": MAX_CHECKS,
                           "items": {"type": "string", "maxLength": MAX_TEXT_CHARS}},
        }, "required": ["action"], "additionalProperties": False},
    },
}

VERIFICATION_INSTRUCTION = (
    "开发阶段必须先用 verification plan 登记必要构建/测试与覆盖目的，再用 run_command 实测。"
    "大型依赖/生成目录可分别用 dependency_dirs/output_dirs 提议明确排除，用户批准前不生效；源码、配置和锁文件不得排除。"
    "失败后在已授权范围修复并重测；不得删除检查、降低断言或屏蔽错误骗取通过。"
    "完成前用 verification review 提交需求覆盖、改动自查及 unresolved 列表。"
    "普通工具输出、Todo 和你的完成声明不能替代宿主验收。无法验证时说明阻塞，不声称完成。"
)
