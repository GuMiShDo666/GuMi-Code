import json
import shutil
import subprocess
from pathlib import Path

from vibe_harness.tools.common import (
    MAX_OUTPUT_CHARS,
    _is_sensitive_path,
    _resolve_workspace_path,
    _sensitive_glob_patterns,
    _truncate_output,
)


def find_files(
    workspace: Path,
    pattern: str,
    path: str = ".",
    max_results: int = 200,
) -> str:
    if not pattern:
        raise ValueError("匹配模式不能为空")

    pattern_path = Path(pattern)
    if pattern_path.is_absolute() or ".." in pattern_path.parts:
        raise ValueError("匹配模式不能越出工作目录")

    if not 1 <= max_results <= 200:
        raise ValueError("max_results 必须在 1 到 200 之间")

    root = workspace.resolve()
    target = _resolve_workspace_path(workspace, path)

    if not target.exists():
        raise ValueError(f"路径不存在：{path!r}")

    if not target.is_dir():
        raise ValueError(f"路径不是目录：{path!r}")

    results = []
    for item in target.glob(pattern):
        if item.is_symlink():
            continue
        if not item.is_file():
            continue
        try:
            rel = item.resolve().relative_to(root)
        except ValueError:
            continue
        if _is_sensitive_path(rel):
            continue
        results.append(str(rel))
        if len(results) >= max_results:
            break

    results.sort()

    return "\n".join(results)


def _validate_file_glob(file_glob: str | None) -> None:
    if file_glob is None:
        return

    pattern_path = Path(file_glob)
    if pattern_path.is_absolute() or ".." in pattern_path.parts:
        raise ValueError("file_glob 不能越出工作目录")


def _build_rg_command(query: str, target: str, file_glob: str | None) -> list[str]:
    cmd = ["rg", "--line-number", "--no-heading", "--color", "never", "--json"]

    if file_glob:
        cmd += ["--glob", file_glob]

    for pattern in _sensitive_glob_patterns():
        cmd += ["--glob", pattern]

    cmd += ["--", query, str(target)]

    return cmd


def _parse_safe_rg_matches(
    stdout: str,
    root: Path,
    max_results: int,
) -> list[str]:
    results = []

    for raw_line in stdout.splitlines():
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            raise ValueError("Error: rg 输出解析失败")

        if not isinstance(payload, dict):
            continue

        if payload.get("type") != "match":
            continue

        data = payload.get("data", {})
        path_data = data.get("path", {})
        lines_data = data.get("lines", {})

        path_text = path_data.get("text")
        matched_text = lines_data.get("text")
        line_number = data.get("line_number")

        if not isinstance(path_text, str):
            continue
        if not isinstance(matched_text, str):
            continue
        if not isinstance(line_number, int):
            continue

        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = root / candidate

        try:
            relative = candidate.resolve().relative_to(root)
        except ValueError:
            continue

        if _is_sensitive_path(relative):
            continue

        text = matched_text.rstrip("\r\n")
        results.append(f"{relative.as_posix()}:{line_number}:{text}")

        if len(results) >= max_results:
            break

    return results


def search_text(
    workspace: Path,
    query: str,
    path: str = ".",
    file_glob: str | None = None,
    case_sensitive: bool = False,
    use_regex: bool = False,
    max_results: int = 100,
) -> str:
    if not query:
        raise ValueError("查询不能为空")

    _validate_file_glob(file_glob)

    if not 1 <= max_results <= 100:
        raise ValueError("max_results 必须在 1 到 100 之间")

    root = workspace.resolve()
    target = _resolve_workspace_path(workspace, path)

    if not target.exists():
        raise ValueError(f"路径不存在：{path!r}")

    if not target.is_dir():
        raise ValueError(f"路径不是目录：{path!r}")

    if shutil.which("rg") is None:
        return "Error: 未找到 rg（ripgrep），请先安装"

    relative_target = target.relative_to(root)
    target_arg = "." if relative_target == Path(".") else str(relative_target)
    cmd = _build_rg_command(query, target_arg, file_glob)

    if not case_sensitive:
        cmd.insert(1, "--ignore-case")

    if not use_regex:
        cmd.insert(1, "--fixed-strings")

    try:
        result = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "Error: 搜索超时"
    except OSError:
        return "Error: 无法执行 rg"

    if result.returncode == 1:
        return "(no matches)"

    if result.returncode != 0:
        return "Error: rg 执行失败"

    try:
        lines = _parse_safe_rg_matches(result.stdout, root, max_results)
    except ValueError:
        return "Error: rg 输出解析失败"

    if not lines:
        return "(no matches)"

    output = "\n".join(lines)

    return _truncate_output(output, MAX_OUTPUT_CHARS)
