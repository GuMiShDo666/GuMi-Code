"""Track actual request-local changes and run bounded checks, never model verdicts."""

import copy
import hashlib
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from threading import Event

from gum_code.tools.command import CommandExecutionResult, DockerSandboxConfig
from gum_code.tools.common import _is_sensitive_path, _reject_symlinks, _resolve_workspace_path
from gum_code.tools.filesystem import FileChange
from gum_code.verification import MAX_CHECKS, MAX_SCAN_SECONDS, _ScanError

MAX_SELF_CHECK_STATE_BYTES = 64_000
# Temporary product pause; keep the explicit runtime for repair and regression tests.
AUTOMATIC_SELF_CHECK_ENABLED = False
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
_CHECK_STATUSES = {"passed", "failed", "not_run", "stale", "no_tests"}
_BASIC_CHECKS = {"syntax", "build", "typecheck"}
_COVERAGE = {"tests", "basic", "none", "execution", "browser", "excluded"}
_NO_TESTS_EXIT = 5
# Observe normal test cases for explicit targets; these adapters never choose scope.
_CHECK_BOOTSTRAP = "import sys; source='\\n'.join(sys.argv[1:-1]); sys.argv=sys.argv[:1]+sys.argv[-1:]; exec(source)"
_PYTEST_CHECK = """import sys, traceback, json
try:
    import pytest
    sys.argv = ['pytest', *json.loads(sys.argv[-1])]
    class Results:
        passed = False
        interrupted = False
        def pytest_runtest_logreport(self, report):
            if report.when == 'call' and report.passed and not hasattr(report, 'wasxfail'):
                self.passed = True
        def pytest_keyboard_interrupt(self, excinfo):
            self.interrupted = True
    result = Results()
    code = int(pytest.main(sys.argv[1:], plugins=[result]))
    if result.interrupted and code in (0, 5):
        code = 1
    elif code == 0 and not result.passed:
        code = 5
except KeyboardInterrupt:
    code = 130
except (Exception, SystemExit):
    traceback.print_exc()
    code = 1
sys.exit(code)"""
_UNITTEST_CHECK = """import sys, traceback, json, unittest as u
try:
    class Results(u.TextTestResult):
        passed = False
        def addSuccess(self, test):
            self.passed = True
            super().addSuccess(test)
    runner = u.TextTestRunner(resultclass=Results)
    result = u.TestProgram(module=None, argv=['unittest', *json.loads(sys.argv[-1])],
                           testRunner=runner, exit=False).result
    code = 1 if not result.wasSuccessful() else 0 if result.passed else 5
except KeyboardInterrupt:
    code = 130
except (Exception, SystemExit):
    traceback.print_exc()
    code = 1
sys.exit(code)"""

SELF_CHECK_INSTRUCTION = (
    "实际修改代码后，按用户需求、当前差异、相关调用关系与项目规范选择最小适用检查。"
    "self_check(checks=[...]) 只执行你选定的命令或已保存的非视觉浏览器场景；空参数只查询状态。"
    "每项说明 purpose、paths 和 expected，指定完整 argv/cwd/ecosystem 或 service/test_file。"
    "不要因上层有依赖清单而测试无关项目；缺适用测试就补最小测试并保留到项目。"
    "浏览器场景保存为 JSON {path,steps}，步骤使用 open/read/click/fill/assert 的受限操作。"
    "全部视觉检查排除；不得调用视觉模型或截图判断，页面结果说明视觉效果未验证。"
    "纯视觉文件可提议 exclusions=[{path,reason:'visual'}]，由独立复核确认。"
    "命令成功不等于功能覆盖；读取真实失败，修复后用同一 id 复测，不削弱正确断言。"
    "需要构建产物时，命令声明 artifact_paths，浏览器引用 build_check 和相同 artifact_paths。"
    "宿主在完成前独立复核原始需求、代码、测试和当前执行事实；不要自行提交通过结论。"
    "缺环境由宿主请求准备；拒绝、预算不足或验证缺失时保留工作并说明未验证。"
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


def _legacy_coverage(checks: list[dict[str, object]]) -> str:
    passed = {check["name"] for check in checks if check["status"] == "passed"}
    return "tests" if "test" in passed else "basic" if passed & _BASIC_CHECKS else "none"


def _validate_legacy_self_check_state(value: object) -> dict[str, object]:
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
        if not isinstance(result, dict) or set(result) - {"cleanup_required", "coverage"} != {"status", "reason", "generation", "input_version", "checks", "uncovered"}:
            raise ValueError("自检结果无效")
        classified = "coverage" in result
        if classified and (not isinstance(result["coverage"], str) or result["coverage"] not in _COVERAGE):
            raise ValueError("自检覆盖类别无效")
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
            if not isinstance(check["name"], str) or not isinstance(check["status"], str) or check["name"] not in _BASIC_CHECKS | {"test", "smoke"} or check["status"] not in _CHECK_STATUSES:
                raise ValueError("自检检查项无效")
            if not classified and (check["name"] == "smoke" or check["status"] == "no_tests"):
                raise ValueError("自检检查项缺少覆盖类别")
            if check["started"] is not None and type(check["started"]) is not bool:
                raise ValueError("自检启动事实无效")
            if check["exit_code"] is not None and (type(check["exit_code"]) is not int or not -255 <= check["exit_code"] <= 65535):
                raise ValueError("自检退出事实无效")
            _reason(check["reason"])
            if not isinstance(check["input_version"], str) or re.fullmatch(r"[0-9a-f]{64}", check["input_version"]) is None:
                raise ValueError("自检输入版本无效")
            if check["status"] == "passed" and not (check["started"] is True and check["reason"] == "exited" and check["exit_code"] == 0):
                raise ValueError("自检通过缺少执行事实")
            if check["status"] == "no_tests" and not (
                    check["name"] == "test" and check["started"] is True and check["reason"] == "exited"
                    and check["exit_code"] == _NO_TESTS_EXIT and "no_tests" in result["uncovered"]):
                raise ValueError("零测试缺少执行事实")
        if classified and result["coverage"] != _legacy_coverage(result["checks"]):
            raise ValueError("自检覆盖类别与检查不符")
        accepted = {"passed", "no_tests"} if classified else {"passed"}
        if result["status"] == "passed" and (not result["checks"] or any(c["status"] not in accepted or c["input_version"] != result["input_version"] for c in result["checks"]) or result["generation"] != value["generation"] or value["unknown"] is not None or value["ambiguous_paths"]):
            raise ValueError("自检通过缺少检查")
        if classified and result["status"] == "passed" and (
                result["coverage"] == "none" or any(item != "no_tests" for item in result["uncovered"])):
            raise ValueError("自检通过缺少覆盖证据")
    if len(json.dumps(value, ensure_ascii=True).encode("utf-8")) > MAX_SELF_CHECK_STATE_BYTES:
        raise ValueError("自检状态超过限制")
    return copy.deepcopy(value)


def validate_task_outcome(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) - {"coverage"} != {"status", "reason", "step", "self_check_status", "changed", "summary"}:
        raise ValueError("任务结束事实无效")
    if not isinstance(value["status"], str) or value["status"] not in {"completed", "incomplete", "failed", "interrupted"}:
        raise ValueError("任务结束状态无效")
    _reason(value["reason"])
    if type(value["step"]) is not int or value["step"] < 0 or type(value["changed"]) is not bool:
        raise ValueError("任务结束事实无效")
    if not isinstance(value["self_check_status"], str) or value["self_check_status"] not in _RESULT_STATUSES | {"pending", "disabled"}:
        raise ValueError("任务自检状态无效")
    if not isinstance(value["summary"], str) or len(value["summary"]) > 2000:
        raise ValueError("任务摘要无效")
    if value["self_check_status"] == "disabled" and ("coverage" in value or value["summary"] or value["changed"]):
        raise ValueError("停用的自检不能声明覆盖或变化事实")
    if "coverage" in value:
        if not isinstance(value["coverage"], str) or value["coverage"] not in _COVERAGE:
            raise ValueError("任务自检覆盖类别无效")
        if value["self_check_status"] == "passed" and value["coverage"] == "none":
            raise ValueError("任务自检通过缺少覆盖证据")
        if value["self_check_status"] == "not_needed" and value["coverage"] != "none":
            raise ValueError("任务自检覆盖类别不符")
        if value["status"] == "completed" and (
                value["self_check_status"] not in {"passed", "not_needed"}
                or value["changed"] and value["self_check_status"] != "passed"):
            raise ValueError("任务完成缺少自检证据")
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


def _code_kind(name: str, head: bytes | None, executable: bool, entries: set[str]) -> str:
    path = Path(name)
    if name in entries or path.name in _CONFIG_NAMES or path.suffix.lower() in _SOURCE_SUFFIXES:
        return "code"
    if (path.name == ".gitkeep" and head == b"" and not executable):
        return "other"
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


def _text(value: object, limit: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError("验证说明无效或超过限制")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is None:
        raise ValueError("验证目标 ID 无效")
    return value


def _paths(value: object, limit: int = 32) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= limit:
        raise ValueError("验证路径列表无效")
    result = [_path(item) for item in value]
    if len(result) != len(set(result)):
        raise ValueError("验证路径重复")
    return result


def _check_definition(value: object) -> dict:
    common = {"id", "purpose", "paths", "expected", "kind"}
    if not isinstance(value, dict) or not common <= value.keys():
        raise ValueError("验证目标缺少 id/purpose/paths/expected/kind")
    _identifier(value["id"])
    _text(value["purpose"])
    _text(value["expected"])
    _paths(value["paths"])
    kind = value["kind"]
    if kind == "command":
        required = common | {"argv", "cwd", "ecosystem"}
        if not required <= value.keys() or value.keys() - required - {"timeout_seconds", "runner", "artifact_paths"}:
            raise ValueError("命令验证参数无效")
        argv = value["argv"]
        if not isinstance(argv, list) or not 1 <= len(argv) <= 128 or any(
                not isinstance(item, str) or len(item) > 8000 or "\x00" in item for item in argv):
            raise ValueError("验证 argv 无效")
        if not argv[0].strip() or argv[0].lstrip().startswith("-"):
            raise ValueError("argv 首项必须是程序名，例如 python3 -m http.server")
        _path(value["cwd"])
        if value["ecosystem"] not in ("python", "node", "prepared"):
            raise ValueError("验证环境无效")
        timeout = value.get("timeout_seconds", 120)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ValueError("验证超时无效")
        if "runner" in value and value["runner"] not in ("pytest", "unittest"):
            raise ValueError("测试观测器无效")
        if "runner" in value and not (len(argv) >= 3 and Path(argv[0]).name in ("python", "python3")
                                      and argv[1:3] == ["-m", value["runner"]]):
            raise ValueError("测试观测器需要显式 python -m pytest/unittest 及目标参数")
    elif kind == "browser":
        required = common | {"service", "test_file"}
        if not required <= value.keys() or value.keys() - required - {"build_check", "artifact_paths"}:
            raise ValueError("浏览器验证参数无效")
        if not isinstance(value["service"], str) or re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value["service"]) is None:
            raise ValueError("浏览器服务名称无效")
        _path(value["test_file"])
        if "build_check" in value:
            _identifier(value["build_check"])
    else:
        raise ValueError("验证类型只能是 command 或 browser")
    if "artifact_paths" in value:
        _paths(value["artifact_paths"], 16)
    return copy.deepcopy(value)


def validate_self_check_arguments(workspace: Path, arguments: object) -> dict:
    """Validate model choices without granting execution or accepting verdicts."""
    if not isinstance(arguments, dict) or arguments.keys() - {"checks", "exclusions"}:
        raise ValueError("验证参数仅允许 checks 和 exclusions")
    if len(json.dumps(arguments, ensure_ascii=True).encode()) > 24000:
        raise ValueError("验证计划超过限制，请缩小本次检查范围")
    result = {}
    if "checks" in arguments:
        checks = arguments["checks"]
        if not isinstance(checks, list) or len(checks) > MAX_CHECKS:
            raise ValueError("验证目标数量超过限制")
        result["checks"] = [_check_definition(item) for item in checks]
        if len({item["id"] for item in checks}) != len(checks):
            raise ValueError("验证目标 ID 重复")
        for item in checks:
            for name in item["paths"] + item.get("artifact_paths", []) + [item.get("cwd", "."), item.get("test_file", ".")]:
                _resolve_workspace_path(workspace, name)
                _reject_symlinks(workspace, name)
    if "exclusions" in arguments:
        exclusions = arguments["exclusions"]
        if not isinstance(exclusions, list) or len(exclusions) > MAX_CHANGED_PATHS:
            raise ValueError("视觉排除列表无效")
        for item in exclusions:
            if not isinstance(item, dict) or set(item) != {"path", "reason"} or item["reason"] != "visual":
                raise ValueError("只能提议排除视觉检查，不能取消功能验证")
            _path(item["path"])
        if len({item["path"] for item in exclusions}) != len(exclusions):
            raise ValueError("视觉排除路径重复")
        result["exclusions"] = copy.deepcopy(exclusions)
    return result


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _bounded_output(value: str, limit: int = 2000) -> str:
    # Bound the serialized form too: non-ASCII diagnostics expand in Session JSON.
    while len(json.dumps(value, ensure_ascii=True).encode()) > limit:
        value = value[:len(value) // 2]
    return value


def validate_self_check_state(value: object) -> dict:
    """Read v1 facts only; current v2 receipts never restore approval or a pass."""
    if isinstance(value, dict) and value.get("version") == 1:
        return _validate_legacy_self_check_state(value)
    base_keys = {"version", "request_id", "source_event_seq", "changed_paths", "ambiguous_paths",
                 "generation", "unknown", "last_result"}
    if not isinstance(value, dict) or set(value) != base_keys | {"checks", "exclusions", "review", "unresolved"} or type(value["version"]) is not int or value["version"] != 2:
        raise ValueError("验证状态无效")
    _validate_legacy_self_check_state({**{k: value[k] for k in base_keys}, "version": 1, "last_result": None})
    for key in ("checks", "unresolved"):
        records = value[key]
        if not isinstance(records, list) or len(records) > MAX_CHECKS:
            raise ValueError("验证记录超过限制")
        for record in records:
            if not isinstance(record, dict) or set(record) != {
                    "definition", "evidence_id", "generation", "input_version", "status", "started",
                    "exit_code", "reason", "evidence_kind", "environment", "output", "output_truncated", "artifacts", "browser"}:
                raise ValueError("验证事实记录无效")
            _check_definition(record["definition"])
            if not isinstance(record["evidence_id"], str) or re.fullmatch(r"[0-9a-f]{32}", record["evidence_id"]) is None:
                raise ValueError("验证事实 ID 无效")
            if type(record["generation"]) is not int or not 0 <= record["generation"] <= value["generation"]:
                raise ValueError("验证事实版本无效")
            if not isinstance(record["input_version"], str) or re.fullmatch(r"[0-9a-f]{64}", record["input_version"]) is None:
                raise ValueError("验证输入版本无效")
            if (not isinstance(record["status"], str) or record["status"] not in _CHECK_STATUSES
                    or not isinstance(record["evidence_kind"], str) or record["evidence_kind"] not in {"execution", "tests", "browser"}):
                raise ValueError("验证结果类别无效")
            if record["started"] is not None and type(record["started"]) is not bool:
                raise ValueError("验证启动事实无效")
            if record["exit_code"] is not None and (type(record["exit_code"]) is not int or not -255 <= record["exit_code"] <= 65535):
                raise ValueError("验证退出事实无效")
            _reason(record["reason"])
            if record["status"] == "passed" and not (record["started"] is True and record["reason"] == "exited" and record["exit_code"] == 0):
                raise ValueError("验证通过缺少执行事实")
            if record["status"] == "no_tests" and not (record["evidence_kind"] == "tests" and record["started"] is True and record["exit_code"] == 5):
                raise ValueError("零用例事实无效")
            if type(record["output_truncated"]) is not bool:
                raise ValueError("验证输出完整性无效")
            if not isinstance(record["output"], str) or len(json.dumps(record["output"]).encode()) > 2200:
                raise ValueError("验证输出摘要超过限制")
            if not isinstance(record["environment"], str) or len(record["environment"]) > 200:
                raise ValueError("验证环境事实无效")
            if not isinstance(record["artifacts"], dict) or len(record["artifacts"]) > 16:
                raise ValueError("构建产物事实无效")
            for path, digest in record["artifacts"].items():
                _path(path)
                if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    raise ValueError("构建产物摘要无效")
            if record["browser"] is not None and not isinstance(record["browser"], dict):
                raise ValueError("浏览器事实无效")
    if len({r["definition"]["id"] for r in value["checks"]}) != len(value["checks"]):
        raise ValueError("验证目标重复")
    validate_self_check_arguments(Path.cwd(), {"exclusions": value["exclusions"]})
    review = value["review"]
    if review is not None:
        if not isinstance(review, dict) or set(review) != {"status", "input_version", "generation", "report"}:
            raise ValueError("复核状态无效")
        if not isinstance(review["status"], str) or review["status"] not in {"passed", "changes_requested", "unavailable"}:
            raise ValueError("复核状态无效")
        if type(review["generation"]) is not int or not 0 <= review["generation"] <= value["generation"]:
            raise ValueError("复核版本无效")
        if not isinstance(review["input_version"], str) or re.fullmatch(r"[0-9a-f]{64}", review["input_version"]) is None:
            raise ValueError("复核输入无效")
        if not isinstance(review["report"], dict):
            raise ValueError("复核报告无效")
    result = value["last_result"]
    if result is not None:
        if not isinstance(result, dict) or set(result) - {"cleanup_required"} != {
                "status", "reason", "generation", "input_version", "checks", "uncovered", "coverage"}:
            raise ValueError("验证汇总无效")
        if (not isinstance(result["status"], str) or result["status"] not in _RESULT_STATUSES
                or not isinstance(result["coverage"], str) or result["coverage"] not in _COVERAGE):
            raise ValueError("验证汇总状态无效")
        _reason(result["reason"])
        if type(result["generation"]) is not int or result["generation"] != value["generation"] or not isinstance(result["checks"], list) or len(result["checks"]) > MAX_CHECKS:
            raise ValueError("验证汇总版本无效")
        if result["status"] == "passed" and (not review or review["status"] != "passed"
                or review["generation"] != value["generation"] or review["input_version"] != result["input_version"]
                or value["unknown"] or value["ambiguous_paths"]):
            raise ValueError("完成缺少当前复核事实")
        if result["status"] == "passed":
            resolved = review["report"].get("resolved_checks", [])
            if not isinstance(resolved, list) or any(not isinstance(i, str) for i in resolved):
                raise ValueError("已解决检查引用无效")
            if value["unresolved"] or result["uncovered"] or review["report"].get("verdict") != "passed":
                raise ValueError("验证完成仍有未解决范围")
            for record in value["checks"]:
                if record["evidence_id"] in resolved:
                    continue
                if (record["status"] != "passed" or record["input_version"] != result["input_version"]
                        or record["generation"] != value["generation"]):
                    raise ValueError("验证完成缺少当前输入版本事实")
            facts = [{key: record[key] for key in (
                "evidence_id", "status", "started", "exit_code", "reason", "input_version")} for record in value["checks"]]
            if result["checks"] != facts:
                raise ValueError("验证汇总与宿主记录不符")
            covered = [path for record in value["checks"] if record["status"] == "passed"
                       for path in record["definition"]["paths"]]
            covered += [record["definition"]["test_file"] for record in value["checks"]
                        if record["status"] == "passed" and record["definition"]["kind"] == "browser"]
            proposed = {item["path"] for item in value["exclusions"]}
            approved = {item.get("path") for item in review["report"].get("excluded_paths", []) if isinstance(item, dict)}
            if proposed != approved or any(path not in approved and not any(Path(path).is_relative_to(p) for p in covered)
                                           for path in value["changed_paths"]):
                raise ValueError("验证完成遗漏本轮变化路径")
    if len(json.dumps(value, ensure_ascii=True).encode()) > MAX_SELF_CHECK_STATE_BYTES:
        raise ValueError("验证状态超过限制")
    return copy.deepcopy(value)


class SelfCheckRuntime:
    """One original request's changes and latest bounded execution facts."""

    def __init__(self, workspace: Path, *, request_id: str | None = None,
                 initial_state: dict[str, object] | None = None,
                 command_sandbox: DockerSandboxConfig | None = None,
                 approval_callback: Callable[[dict[str, object]], bool] | None = None,
                 internal_paths: tuple[Path, ...] = ()) -> None:
        self.workspace = workspace.resolve()
        self.command_sandbox = command_sandbox
        self.browser_evidence_callback = None
        self.approval_callback = approval_callback
        self._internal_paths = tuple(Path(p).resolve() for p in internal_paths)
        self._cancel = Event()
        self._denied_scopes: set[str] = set()
        self._entries: set[str] = set()
        self._excluded_dirs: set[str] = set()
        self._output_dirs: set[str] = set()
        self._generated_outputs: dict[str, str] = {}
        self._cleanup_required: set[str] = set()
        self._before_command = None
        self._checked_snapshot = None
        self._state = {"version": 2, "request_id": request_id or str(uuid.uuid4()),
                       "source_event_seq": None, "changed_paths": [], "ambiguous_paths": [],
                       "generation": 0, "unknown": None, "last_result": None,
                       "checks": [], "exclusions": [], "review": None, "unresolved": []}
        if initial_state is not None:
            restored = validate_self_check_state(initial_state)
            if request_id is not None and restored["request_id"] != request_id:
                raise ValueError("自检状态与原始请求不匹配")
            self._state.update({key: restored[key] for key in (
                "request_id", "source_event_seq", "changed_paths", "ambiguous_paths", "generation", "unknown")})
            # Historical receipts remain in the Session; none is current execution authority.
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
        self._state["review"] = None
        self._state["last_result"] = None
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

    def _current(self) -> list[dict]:
        version = self._version(self._checked_snapshot)
        return [r for r in self._state["checks"] if r["generation"] == self._state["generation"]
                and r["input_version"] == version]

    def _uncovered_paths(self) -> list[str]:
        covered = [p for r in self._current() if r["status"] == "passed" for p in r["definition"]["paths"]]
        covered += [r["definition"]["test_file"] for r in self._current()
                    if r["status"] == "passed" and r["definition"]["kind"] == "browser"]
        proposed = {item["path"] for item in self._state["exclusions"]}
        return [p for p in self._state["changed_paths"] if p not in proposed
                and not any(Path(p).is_relative_to(parent) for parent in covered)]

    def _checks_ready(self) -> bool:
        review = self._state["review"]
        resolved = set(review["report"].get("resolved_checks", [])) if review and review["status"] == "passed" else set()
        current = self._current()
        return (self._review_ready() and all(
            r["status"] == "passed" and r in current or r["evidence_id"] in resolved
            for r in self._state["checks"]))

    def _review_ready(self) -> bool:
        return (not self._state["unknown"] and not self._state["ambiguous_paths"]
                and not self._uncovered_paths() and all(
                    r in self._current() or r["status"] in {"failed", "no_tests", "not_run"}
                    for r in self._state["checks"]))

    def needs_check(self) -> bool:
        self.recheck_final()
        if not self._state["changed_paths"]:
            return False
        review = self._state["review"]
        return not self._checks_ready() or (review is not None and review["status"] == "changes_requested")

    def needs_review(self) -> bool:
        self.recheck_final()
        return bool(self._state["changed_paths"]) and self._review_ready() and self._state["review"] is None

    def _coverage(self) -> str:
        kinds = {r["evidence_kind"] for r in self._current() if r["status"] == "passed"}
        if not kinds:
            return "excluded" if self._state["exclusions"] else "none"
        return "tests" if "tests" in kinds else "browser" if "browser" in kinds else "execution"

    def _refresh_result(self) -> dict:
        changed = bool(self._state["changed_paths"])
        review = self._state["review"]
        if self._state["unknown"] or self._state["ambiguous_paths"]:
            status, reason = "unknown", "change_unknown"
        elif not changed:
            status, reason = "not_needed", "no_code_changes"
        elif any(r["status"] in {"failed", "no_tests", "not_run"} and not (review and review["status"] == "passed" and r["evidence_id"] in review["report"].get("resolved_checks", [])) for r in self._current()):
            status, reason = "failed", "checks_failed"
        elif not self._checks_ready():
            status, reason = "not_run", "missing_current_evidence"
        elif not review:
            status, reason = "not_run", "review_pending"
        elif review["status"] != "passed":
            status, reason = "not_run", "review_" + review["status"]
        elif self._state["unresolved"]:
            status, reason = "failed", "unresolved_checks"
        else:
            status, reason = "passed", "verified_scope"
        result = {"status": status, "reason": reason, "generation": self._state["generation"],
                  "input_version": self._version(self._checked_snapshot), "checks": [
                      {k: r[k] for k in ("evidence_id", "status", "started", "exit_code", "reason", "input_version")}
                      for r in self._state["checks"]],
                  "uncovered": self._uncovered_paths()[:MAX_CHANGED_PATHS], "coverage": self._coverage()}
        if self._cleanup_required:
            result["cleanup_required"] = sorted(self._cleanup_required)[:16]
        self._state["last_result"] = result
        return result

    def summary(self) -> str:
        result = self._refresh_result()
        if result["status"] == "not_needed":
            return ""
        if result["status"] == "passed":
            count = sum(r["status"] == "passed" for r in self._current())
            text = f"验证：{count} 项当前检查及独立复核完成，仅代表本次覆盖范围。"
            if result["coverage"] == "execution":
                text += "命令已执行成功；未取得正常测试用例计数，不代表功能测试全部通过。"
        else:
            text = {"review_pending": "验证：已有执行结果，独立复核尚未完成。",
                    "review_changes_requested": "验证：独立复核发现问题，尚未修复复测。",
                    "review_unavailable": "验证：独立复核不可用，当前交付未完成验收。",
                    "checks_failed": "验证：相关检查失败或未执行，需诊断、修复及复测。",
                    "change_unknown": "验证：当前改动或输入无法确认，不能报告通过。"}.get(
                        result["reason"], "验证：当前代码仍缺少适用证据，已发生的改动保留。")
        if self._state["exclusions"] or any(r["definition"]["kind"] == "browser" for r in self._state["checks"]):
            text += "视觉效果未验证。"
        if self._cleanup_required:
            text += "环境资源清理未确认：" + "、".join(sorted(self._cleanup_required)[:16])
        return text

    def outcome(self, *, step: int, status: str | None = None, reason: str | None = None) -> dict:
        result = self._refresh_result()
        complete = result["status"] in {"passed", "not_needed"}
        return validate_task_outcome({"status": status or ("completed" if complete else "incomplete"),
            "reason": reason or result["reason"], "step": step, "self_check_status": result["status"],
            "changed": bool(self._state["changed_paths"]), "summary": self.summary(),
            "coverage": result["coverage"]})

    def recheck_final(self) -> None:
        if self.browser_evidence_callback is not None:
            for record in self._state["checks"]:
                if record["definition"]["kind"] != "browser" or record["status"] != "passed":
                    continue
                try:
                    current_service = self.browser_evidence_callback(record["definition"], record["input_version"])
                    previous_service = (record["browser"] or {}).get("service", {})
                    current = (current_service.get("current") is True
                               and current_service.get("service_identity") == (record["browser"] or {}).get("service_identity")
                               and current_service.get("source_version") == previous_service.get("source_version"))
                except (ValueError, OSError, RuntimeError):
                    current = False
                if not current:
                    record["status"] = "stale"
                    self._state["review"] = None
                    self._state["last_result"] = None
        if self._checked_snapshot is not None:
            current = self._scan()
            if current != self._checked_snapshot:
                self._observe(self._checked_snapshot, current)
                self._checked_snapshot = current
                self._state["review"] = None
                self._state["last_result"] = None

    def review_context(self, user_query: str) -> dict:
        self.recheck_final()
        if not self._review_ready():
            raise ValueError("当前验证证据尚不足以复核")
        context = {"user_query": user_query, "input_version": self._version(self._checked_snapshot),
                   "generation": self._state["generation"], "changed_paths": self._state["changed_paths"],
                   "deleted_paths": [p for p in self._state["changed_paths"] if not (self.workspace / p).exists()],
                   "checks": copy.deepcopy(self._state["checks"]),
                   "exclusions": copy.deepcopy(self._state["exclusions"]),
                   "unresolved": copy.deepcopy(self._state["unresolved"]),
                   "visual_checks": "excluded", "uncovered_paths": self._uncovered_paths()}
        if len(json.dumps(context, ensure_ascii=True).encode()) > MAX_SELF_CHECK_STATE_BYTES:
            raise ValueError("复核上下文超过限制，不能静默截断")
        return context

    def mark_review_unavailable(self, reason: str) -> None:
        version = self._version(self._checked_snapshot or self._scan())
        if version is None:
            self.mark_unknown("scan_unknown")
            return
        self._state["review"] = {"status": "unavailable", "input_version": version,
                                 "generation": self._state["generation"], "report": {"reason": _reason(reason)}}
        self._refresh_result()

    def record_review(self, report: dict, *, input_version: str) -> None:
        self.recheck_final()
        if input_version != self._version(self._checked_snapshot) or not self._review_ready():
            raise ValueError("复核期间输入变化，需重新验证")
        if not isinstance(report, dict) or report.get("verdict") not in {"passed", "changes_requested"}:
            raise ValueError("复核缺少明确结论")
        evidence = report.get("evidence", [])
        if not isinstance(evidence, list) or (not evidence and report["verdict"] == "passed"):
            raise ValueError("复核通过必须引用实际读取的代码、测试或相关调用方")
        for item in evidence:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError("复核文件引用无效")
            path = _path(item["path"])
            if not _resolve_workspace_path(self.workspace, path).is_file():
                raise ValueError("复核文件引用不存在")
            _reject_symlinks(self.workspace, path)
        if len(json.dumps(report, ensure_ascii=True).encode()) > 12000:
            self.mark_review_unavailable("report_limit")
            return
        proposed = {item["path"] for item in self._state["exclusions"]}
        accepted = report.get("excluded_paths", [])
        if not isinstance(accepted, list) or any(not isinstance(item, dict) or set(item) != {"path", "reason"}
                or item["path"] not in proposed or not isinstance(item["reason"], str) or not item["reason"].strip() for item in accepted):
            raise ValueError("复核不能扩展视觉排除范围")
        resolved = report.get("resolved_checks", [])
        unresolved = {r["evidence_id"]: r for r in self._state["unresolved"]}
        if not isinstance(resolved, list) or len(set(resolved)) != len(resolved) or set(resolved) - unresolved.keys():
            raise ValueError("复核引用的失败事实无效")
        verdict = report["verdict"]
        if verdict == "passed" and (proposed != {item["path"] for item in accepted} or set(resolved) != unresolved.keys()):
            raise ValueError("复核未处理排除范围或旧失败")
        if verdict == "passed":
            self._state["unresolved"] = []
        self._state["review"] = {"status": verdict, "input_version": input_version,
                                 "generation": self._state["generation"], "report": copy.deepcopy(report)}
        self._refresh_result()
        validate_self_check_state(self._state)

    def _observed_command(self, check: dict) -> tuple[dict, str]:
        argv = check["argv"]
        runner = check.get("runner")
        if len(argv) >= 3 and Path(argv[0]).name in ("python", "python3") and argv[1] == "-m" and argv[2] in ("pytest", "unittest"):
            runner = argv[2]
        if runner:
            source = _PYTEST_CHECK if runner == "pytest" else _UNITTEST_CHECK
            argv = [argv[0], "-c", _CHECK_BOOTSTRAP, *source.splitlines(), json.dumps(argv[3:])]
        return {"argv": argv, "cwd": check["cwd"], "timeout_seconds": check.get("timeout_seconds", 120)}, "tests" if runner else "execution"

    def _artifact_receipts(self, paths: list[str]) -> dict:
        if not paths:
            return {}
        from gum_code.verification import workspace_snapshot
        full = workspace_snapshot(self.workspace, tuple(sorted(p for p in self._excluded_dirs
                     if not any(Path(target).is_relative_to(p) or Path(p).is_relative_to(target) for target in paths))), self._internal_paths)
        result = {}
        for path in paths:
            selected = {p: identity for p, identity in full.items() if Path(p).is_relative_to(path)}
            if not selected:
                raise ValueError("构建产物未生成")
            result[path] = _digest(selected)
        return result

    def _prepare_outputs(self, check: dict) -> None:
        # Only native, verified compiler output configuration can omit generated files.
        # Arbitrary scripts/artifact declarations never hide a newly created source tree.
        root = Path(check["cwd"])
        argv = check["argv"]
        program = " ".join(argv)
        package = self.workspace / root / "package.json"
        if argv[:2] == ["npm", "run"] and len(argv) == 3 and package.is_file():
            data = json.loads(_read_text(self.workspace, (root / "package.json").as_posix()))
            program = data.get("scripts", {}).get(argv[2], "")
        declared, identity = None, None
        if program in {"tsc", "tsc -b", "node node_modules/typescript/bin/tsc"}:
            config_path = root / "tsconfig.json"
            if (self.workspace / config_path).is_file():
                config = json.loads(_read_text(self.workspace, config_path.as_posix()))
                if isinstance(config, dict) and "extends" not in config:
                    options = config.get("compilerOptions", {})
                    if isinstance(options, dict) and isinstance(options.get("outDir"), str):
                        declared = _path((root / options["outDir"]).as_posix())
                        identity = _digest(config)
        elif program == "vite build" and not any((self.workspace / root).glob("vite.config.*")):
            declared, identity = (root / "dist").as_posix(), _digest(program)
        if declared not in check.get("artifact_paths", []):
            return
        target = self.workspace / declared
        protected = self._state["changed_paths"] + list(self._entries) + check["paths"]
        if declared == "." or any(Path(p).is_relative_to(declared) for p in protected):
            raise ValueError("不能把受检源码声明为构建输出")
        if target.is_file() or (target.is_dir() and any(target.iterdir())
                               and self._generated_outputs.get(declared) != identity):
            return
        self._excluded_dirs.add(declared)
        self._output_dirs.add(declared)
        self._generated_outputs[declared] = identity

    def _store_record(self, record: dict) -> None:
        original = copy.deepcopy(self._state)
        checks = self._state["checks"]
        previous = next((r for r in checks if r["definition"]["id"] == record["definition"]["id"]), None)
        if previous is not None:
            checks.remove(previous)
        checks.append(record)
        unresolved = self._state["unresolved"]
        unresolved[:] = [r for r in unresolved if r["definition"] != record["definition"]
                         or (record["definition"]["kind"] == "browser"
                             and (r.get("browser") or {}).get("scenario_version")
                             != (record.get("browser") or {}).get("scenario_version"))]
        if record["status"] != "passed":
            unresolved.append(copy.deepcopy(record))
        self._state["review"] = None
        self._state["last_result"] = None
        try:
            validate_self_check_state(self._state)
        except ValueError:
            # Never discard a failure to manufacture a successful bounded state.
            self._state = original
            self.mark_unknown("evidence_limit")

    def execute(self, *, checks: list | None = None, exclusions: list | None = None,
                command_runner: Callable | None = None, browser_runner: Callable | None = None,
                cancel_event: Event | None = None, allow_prepare: bool = True) -> str:
        from gum_code.project_environment import discover_project, ensure_environment, EnvironmentResult
        arguments = {}
        if checks is not None:
            arguments["checks"] = checks
        if exclusions is not None:
            arguments["exclusions"] = exclusions
        selected = validate_self_check_arguments(self.workspace, arguments)
        self._check_cancel(cancel_event)
        self.recheck_final()
        if not self._state["changed_paths"] or not selected:
            return json.dumps({**self._refresh_result(), "changed_paths": self._state["changed_paths"],
                               "review": self._state["review"]}, ensure_ascii=False)
        if exclusions is not None and exclusions != self._state["exclusions"]:
            if any(item["path"] not in self._state["changed_paths"] for item in exclusions):
                raise ValueError("只能提议排除本轮实际变化路径")
            self._state["exclusions"] = copy.deepcopy(exclusions)
            self._state["review"] = None
        for check in selected.get("checks", []):
            self._check_cancel(cancel_event)
            if len({r["definition"]["id"] for r in self._state["checks"]} | {check["id"]}) > MAX_CHECKS:
                raise ValueError("本轮验证目标超过限制")
            if check["kind"] == "command":
                if check["ecosystem"] == "prepared":
                    env = EnvironmentResult("ready", sandbox=self.command_sandbox, identity=_digest(str(self.command_sandbox))) if self.command_sandbox else EnvironmentResult("unavailable", "prepared_environment_required")
                    lease = nullcontext(env)
                else:
                    project = discover_project(self.workspace, check["cwd"], ecosystem=check["ecosystem"])
                    lease = ensure_environment(self.workspace, project, command_sandbox=self.command_sandbox,
                        approval_callback=self.approval_callback if allow_prepare else None,
                        denied_scopes=self._denied_scopes, cancel_event=cancel_event or self._cancel,
                        change_callback=self.record_path)
            else:
                lease = nullcontext(EnvironmentResult("ready", identity="current_project_browser"))
            with lease as environment:
                self._cleanup_required.update(getattr(environment, "cleanup_required", ()))
                self._excluded_dirs.update(getattr(environment, "excluded_dirs", ()))
                if check["kind"] == "command":
                    try:
                        self._prepare_outputs(check)
                    except ValueError:
                        return json.dumps({**self._refresh_result(), "error": "invalid_artifact_scope"}, ensure_ascii=False)
                before = self._scan()
                version = self._version(before)
                if before is None:
                    break
                old = next((r for r in self._state["checks"] if r["definition"] == check
                            and r["input_version"] == version and r["environment"] == environment.identity), None)
                if old is not None and old["status"] == "passed" and check["kind"] == "command":
                    continue
                if old is not None and old["status"] in {"failed", "no_tests"} and check["kind"] == "command":
                    continue  # Same failed inputs and command cannot acquire new evidence.
                # A fresh attempt supersedes its old receipt even if a hook or runner raises.
                self._state["checks"] = [r for r in self._state["checks"] if r["definition"]["id"] != check["id"]]
                self._state["review"] = None
                self._state["last_result"] = None
                output, browser, artifacts = "", None, {}
                fact = CommandExecutionResult(False, None, "command_unavailable")
                evidence_kind = "execution"
                try:
                    if environment.status != "ready":
                        reason = environment.reason or "environment_unavailable"
                        if reason == "cancelled":
                            raise KeyboardInterrupt
                        fact = CommandExecutionResult(False, None, _reason(reason))
                    elif check["kind"] == "command" and command_runner is not None:
                        args, evidence_kind = self._observed_command(check)
                        output, fact = command_runner(args, environment.sandbox)
                        if not isinstance(fact, CommandExecutionResult):
                            fact = CommandExecutionResult(None, None, "unknown")
                        if fact.started is True and fact.exit_code == 0 and fact.reason == "exited":
                            try:
                                artifacts = self._artifact_receipts(check.get("artifact_paths", []))
                            except ValueError:
                                fact = CommandExecutionResult(True, fact.exit_code, "artifact_unavailable")
                    elif check["kind"] == "browser" and browser_runner is not None:
                        evidence_kind = "browser"
                        build = check.get("build_check")
                        if build:
                            receipt = next((r for r in self._current() if r["definition"]["id"] == build and r["status"] == "passed"), None)
                            try:
                                artifacts_match = bool(check.get("artifact_paths")) and receipt is not None and receipt["artifacts"] == self._artifact_receipts(check["artifact_paths"])
                            except ValueError:
                                artifacts_match = False
                            if not artifacts_match:
                                return json.dumps({**self._refresh_result(), "error": "current_build_evidence_required"}, ensure_ascii=False)
                        output, browser = browser_runner(check, version)
                        service = browser.get("service", {}) if isinstance(browser, dict) else {}
                        if not build and any(part in {"dist", "build"} for part in Path(service.get("document_root") or ".").parts):
                            browser["reason"] = "build_evidence_required"
                        assertions = browser.get("assertions", []) if isinstance(browser, dict) else []
                        passed = (browser.get("started") is True and browser.get("reason") == "exited"
                                  and bool(assertions) and all(a.get("passed") is True for a in assertions)
                                  and bool(browser.get("service_identity"))) if isinstance(browser, dict) else False
                        fact = CommandExecutionResult(browser.get("started") if isinstance(browser, dict) else None,
                                                      0 if passed else None, "exited" if passed else "browser_unverified")
                finally:
                    after = self._scan()
                    self._observe(before, after)
                stable = before == after and after is not None
                status = ("stale" if not stable else "passed" if fact.started is True and fact.reason == "exited" and fact.exit_code == 0
                          else "no_tests" if evidence_kind == "tests" and fact.started is True and fact.reason == "exited" and fact.exit_code == 5
                          else "failed" if fact.started else "not_run")
                record = {"definition": check, "evidence_id": uuid.uuid4().hex,
                          "generation": self._state["generation"], "input_version": version,
                          "status": status, "started": fact.started, "exit_code": fact.exit_code,
                          "reason": fact.reason, "evidence_kind": evidence_kind,
                          "environment": environment.identity, "output": _bounded_output(output),
                          "output_truncated": _bounded_output(output) != output,
                          "artifacts": artifacts, "browser": browser}
                self._checked_snapshot = after
                self._store_record(record)
                if not stable:
                    break
        if self._checked_snapshot is None:
            self._checked_snapshot = self._scan()
        result = self._refresh_result()
        validate_self_check_state(self._state)
        return json.dumps({**result, "evidence": self._state["checks"], "review": self._state["review"]}, ensure_ascii=False)
