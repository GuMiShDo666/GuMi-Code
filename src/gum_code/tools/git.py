import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from gum_code.tools.common import (
    ReadingCapture,
    _is_sensitive_path,
    _resolve_workspace_path,
)
from gum_code.tools.artifacts import reading_result
from gum_code.tools.command import _communicate_bounded

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


def _run_git(workspace: Path, args: list[str]) -> str | None:
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
            captured = ReadingCapture()
            result = subprocess.Popen(
                command,
                cwd=str(workspace_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_isolated_git_env(isolated_home),
            )
            _communicate_bounded(result, _GIT_TIMEOUT, captured)
    except FileNotFoundError:
        return "Error: 未找到 git，请先安装"
    except subprocess.TimeoutExpired:
        return "Error: git 命令超时"
    except OSError:
        return "Error: git 安全环境创建或执行失败"

    if result.returncode != 0:
        return "Error: 不是 Git 仓库或 git 命令失败"

    try:
        return captured.text()
    except UnicodeDecodeError:
        return "Error: git 输出不是有效 UTF-8"


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


def git_status(workspace: Path, path: str = ".", *, artifact_workspace: Path | None = None) -> str:
    relative = _resolve_workspace_path(workspace, path).relative_to(workspace.resolve()).as_posix()
    output = _run_git(workspace, ["--literal-pathspecs", "status", "--short", "--branch", "--", relative])
    destination = artifact_workspace or workspace
    if output is None:
        return reading_result(destination, "git_status", {"path": relative}, "", gap_reason="capture_limit")

    if output.startswith("Error:"):
        return output

    output = _filter_sensitive_status(output)

    return reading_result(destination, "git_status", {"path": relative}, output)


def git_diff(workspace: Path, staged: bool = False, path: str = ".",
             *, artifact_workspace: Path | None = None) -> str:
    if type(staged) is not bool:
        raise ValueError("staged 必须是布尔值")
    relative = _resolve_workspace_path(workspace, path).relative_to(workspace.resolve()).as_posix()
    args = ["diff", "--no-ext-diff", "--no-textconv"]

    if staged:
        args = ["diff", "--cached", "--no-ext-diff", "--no-textconv"]

    output = _run_git(workspace, ["--literal-pathspecs", *args, "--", relative])
    arguments = {"staged": staged, "path": relative}
    destination = artifact_workspace or workspace
    if output is None:
        return reading_result(destination, "git_diff", arguments, "", gap_reason="capture_limit")

    if output.startswith("Error:"):
        return output

    output = _filter_sensitive_diff(output)

    if not output.strip():
        output = "(no changes)"

    return reading_result(destination, "git_diff", arguments, output)
