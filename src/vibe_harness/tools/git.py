import subprocess
from pathlib import Path

from vibe_harness.tools.common import (
    MAX_OUTPUT_CHARS,
    _is_sensitive_path,
    _truncate_output,
)

_GIT_TIMEOUT = 10


def _run_git(workspace: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(workspace.resolve()),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except FileNotFoundError:
        return "Error: 未找到 git，请先安装"
    except subprocess.TimeoutExpired:
        return "Error: git 命令超时"

    if result.returncode != 0:
        return "Error: 不是 Git 仓库或 git 命令失败"

    return result.stdout


def _path_has_sensitive_part(path_text: str) -> bool:
    cleaned = path_text.strip().strip('"')
    return _is_sensitive_path(Path(cleaned))


def _filter_sensitive_status(output: str) -> str:
    lines = []
    for line in output.splitlines():
        if line.startswith("##"):
            lines.append(line)
            continue
        path_text = line[3:]
        if _path_has_sensitive_part(path_text):
            continue
        lines.append(line)
    return "\n".join(lines)


def _filter_sensitive_diff(output: str) -> str:
    lines = output.splitlines()
    result: list[str] = []
    skip = False

    for line in lines:
        if line.startswith("diff --git "):
            b_path = line.split(" b/", 1)[1] if " b/" in line else line
            skip = _path_has_sensitive_part(b_path)

        if not skip:
            result.append(line)

    return "\n".join(result)


def git_status(workspace: Path) -> str:
    output = _run_git(workspace, ["status", "--short", "--branch"])

    if output.startswith("Error:"):
        return output

    output = _filter_sensitive_status(output)

    return _truncate_output(output, MAX_OUTPUT_CHARS)


def git_diff(workspace: Path, staged: bool = False) -> str:
    args = ["diff", "--no-ext-diff", "--no-textconv"]

    if staged:
        args = ["diff", "--cached", "--no-ext-diff", "--no-textconv"]

    output = _run_git(workspace, args)

    if output.startswith("Error:"):
        return output

    output = _filter_sensitive_diff(output)

    if not output.strip():
        return "(no changes)"

    return _truncate_output(output, MAX_OUTPUT_CHARS)
