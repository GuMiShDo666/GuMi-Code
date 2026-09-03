import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from gum_code.tools.common import (
    MAX_OUTPUT_CHARS,
    _is_sensitive_path,
    _truncate_output,
)

_GIT_TIMEOUT = 10
_GIT_CONFIG_OVERRIDES = (
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    f"core.hooksPath={os.devnull}",
)


def _isolated_git_env(home: str) -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": home,
        "LC_ALL": "C",
        "PATH": os.defpath,
        "XDG_CONFIG_HOME": home,
    }


def _run_git(workspace: Path, args: list[str]) -> str:
    git_path = shutil.which("git")
    if git_path is None:
        return "Error: 未找到 git，请先安装"

    workspace_root = workspace.resolve()
    executable = Path(git_path).resolve()
    if executable.is_relative_to(workspace_root):
        return "Error: 拒绝执行工作区内的 git"

    temp_parent = Path(tempfile.gettempdir()).resolve()
    if temp_parent.is_relative_to(workspace_root):
        return "Error: 无法为 git 创建安全的临时环境"

    command = [str(executable), "--no-pager"]
    for override in _GIT_CONFIG_OVERRIDES:
        command.extend(("-c", override))
    command.extend(args)

    try:
        with tempfile.TemporaryDirectory(
            prefix="gum-code-git-",
            dir=temp_parent,
        ) as isolated_home:
            result = subprocess.run(
                command,
                cwd=str(workspace_root),
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
                env=_isolated_git_env(isolated_home),
            )
    except FileNotFoundError:
        return "Error: 未找到 git，请先安装"
    except subprocess.TimeoutExpired:
        return "Error: git 命令超时"
    except OSError:
        return "Error: git 安全环境创建或执行失败"

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
