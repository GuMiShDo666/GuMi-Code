import fnmatch
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from gum_code.tools.common import (
    MAX_READING_DEPTH,
    MAX_READING_SCAN_ENTRIES,
    READING_TIMEOUT_SECONDS,
    ReadingCapture,
    _is_sensitive_path,
    _resolve_workspace_path,
    _sensitive_glob_patterns,
)
from gum_code.tools.artifacts import MAX_READING_BODY_BYTES, reading_result
from gum_code.tools.command import _communicate_bounded


class _ScanLimit(Exception):
    pass


def _find_bounded(target: Path, root: Path, pattern: str) -> tuple[list[str], str | None]:
    raw_parts = Path(pattern).parts
    parts = tuple(part for index, part in enumerate(raw_parts)
                  if part != "**" or index == 0 or raw_parts[index - 1] != "**")
    if not parts or any("**" in part and part != "**" for part in parts):
        raise ValueError("** 必须是完整的路径段")
    results: set[str] = set()
    visited = size = 0
    deadline = time.monotonic() + READING_TIMEOUT_SECONDS

    def walk(directory: Path, position: int, depth: int) -> None:
        nonlocal visited, size
        if position == len(parts):
            return  # Path.glob ending in ** yields directories, not files.
        if depth > MAX_READING_DEPTH:
            raise _ScanLimit("depth_limit")
        recursive = parts[position] == "**"
        if recursive:
            walk(directory, position + 1, depth)
        with os.scandir(directory) as iterator:
            for entry in iterator:
                visited += 1
                if visited > MAX_READING_SCAN_ENTRIES:
                    raise _ScanLimit("scan_limit")
                if time.monotonic() >= deadline:
                    raise _ScanLimit("timeout")
                item = Path(entry.path)
                try:
                    relative = item.resolve().relative_to(root)
                except (ValueError, RuntimeError):
                    continue
                if _is_sensitive_path(relative):
                    continue
                if recursive:
                    if entry.is_dir(follow_symlinks=False):
                        walk(item, position, depth + 1)
                elif fnmatch.fnmatch(entry.name, parts[position]):
                    if position + 1 == len(parts):
                        if entry.is_symlink() or not entry.is_file() or pattern.endswith(os.sep):
                            continue
                        value = str(relative)
                        if value not in results:
                            results.add(value)
                            size += len(value.encode("utf-8")) + 1
                            if size > MAX_READING_BODY_BYTES:
                                raise _ScanLimit("capture_limit")
                    elif entry.is_dir():
                        walk(item, position + 1, depth + 1)
    gap = None
    try:
        walk(target, 0, 0)
    except _ScanLimit as error:
        gap = str(error)
    return sorted(results), gap


def find_files(
    workspace: Path,
    pattern: str,
    path: str = ".",
    max_results: int = 200,
    *,
    artifact_workspace: Path | None = None,
) -> str:
    if not pattern:
        raise ValueError("匹配模式不能为空")

    pattern_path = Path(pattern)
    if pattern_path.is_absolute() or ".." in pattern_path.parts:
        raise ValueError("匹配模式不能越出工作目录")

    if type(max_results) is not int or not 1 <= max_results <= 200:
        raise ValueError("max_results 必须在 1 到 200 之间")

    root = workspace.resolve()
    target = _resolve_workspace_path(workspace, path)

    if not target.exists():
        raise ValueError(f"路径不存在：{path!r}")

    if not target.is_dir():
        raise ValueError(f"路径不是目录：{path!r}")

    results, gap = _find_bounded(target, root, pattern)
    return reading_result(artifact_workspace or workspace, "find_files",
                          {"pattern": str(Path(pattern)) + (os.sep if pattern.endswith(os.sep) else ""),
                           "path": target.relative_to(root).as_posix()}, "\n".join(results),
                          max_items=max_results, gap_reason=gap, total_items=len(results))


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
    max_results: int | None = None,
) -> list[str]:
    results = []

    for raw_line in stdout.splitlines():
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            raise ValueError("Error: rg 输出解析失败")

        if not isinstance(payload, dict):
            raise ValueError("Error: rg 输出解析失败")

        if payload.get("type") != "match":
            continue

        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("Error: rg 输出解析失败")
        path_data = data.get("path", {})
        lines_data = data.get("lines", {})
        if not isinstance(path_data, dict) or not isinstance(lines_data, dict):
            raise ValueError("Error: rg 输出解析失败")

        path_text = path_data.get("text")
        matched_text = lines_data.get("text")
        line_number = data.get("line_number")

        if not isinstance(path_text, str) or not isinstance(matched_text, str):
            raise ValueError("Error: rg 输出解析失败")
        if type(line_number) is not int or line_number < 1:
            raise ValueError("Error: rg 输出解析失败")

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

        if max_results is not None and len(results) >= max_results:
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
    *,
    artifact_workspace: Path | None = None,
) -> str:
    if not query:
        raise ValueError("查询不能为空")

    _validate_file_glob(file_glob)

    if type(max_results) is not int or not 1 <= max_results <= 100:
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

    captured = ReadingCapture()
    try:
        result = subprocess.Popen(
            cmd,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _communicate_bounded(result, READING_TIMEOUT_SECONDS, captured)
    except subprocess.TimeoutExpired:
        return "Error: 搜索超时"
    except OSError:
        return "Error: 无法执行 rg"

    arguments = {"query": query, "path": relative_target.as_posix(), "file_glob": file_glob,
                 "case_sensitive": case_sensitive, "use_regex": use_regex}
    destination = artifact_workspace or workspace
    if result.returncode not in (0, 1):
        return "Error: rg 执行失败"

    try:
        stdout = captured.text()
    except UnicodeDecodeError:
        return "Error: rg 输出解析失败"
    if stdout is None:
        return reading_result(destination, "search_text", arguments, "", gap_reason="capture_limit")
    if result.returncode == 1:
        return reading_result(destination, "search_text", arguments, "(no matches)", total_items=0)
    try:
        lines = _parse_safe_rg_matches(stdout, root)
    except ValueError:
        return "Error: rg 输出解析失败"

    return reading_result(destination, "search_text", arguments,
                          "\n".join(lines) if lines else "(no matches)",
                          max_items=max_results, total_items=len(lines))
