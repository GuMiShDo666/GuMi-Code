import os
import re
import subprocess
import tempfile
from pathlib import Path, PureWindowsPath

from vibe_harness.tools.common import (
    _is_sensitive_path,
    _reject_symlinks,
    _resolve_workspace_path,
    _truncate_output,
)

MAX_COMMAND_OUTPUT_CHARS = 50_000

_CONTROL_OPERATORS = frozenset({";", "&&", "||", "|"})

_REDIRECTION_OPERATORS = frozenset({">", ">>", "<", "<<"})

_REDIRECTION_PREFIX_RE = re.compile(
    r"^(?:(?:[0-9]+)?(?:>>?|<<?)|&>>?)(?!=)"
)


def find_shell_syntax_violation(argv: list[str]) -> str | None:
    program = Path(argv[0]).name.lower() if argv else ""
    find_exec_open = False

    for token in argv:
        if program == "find" and token in ("-exec", "-execdir"):
            find_exec_open = True
            continue
        if find_exec_open and token in (";", "+"):
            find_exec_open = False
            continue
        if token in _CONTROL_OPERATORS:
            return token
        if token in _REDIRECTION_OPERATORS:
            return token
        if _REDIRECTION_PREFIX_RE.match(token):
            return token
    return None


SHELL_EXECUTABLE_NAMES = {
    "sh",
    "bash",
    "zsh",
    "dash",
    "ksh",
    "fish",
    "csh",
    "tcsh",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
}

_SAFE_ENV_KEYS = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USER",
    "VIRTUAL_ENV",
}


def _sanitized_env(home: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_ENV_KEYS
    }
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    return env


def _is_shell_name(token: str) -> bool:
    return Path(token).name.lower() in SHELL_EXECUTABLE_NAMES


def _launches_shell(argv: list[str]) -> bool:
    if _is_shell_name(argv[0]):
        return True

    program = Path(argv[0]).name.lower()

    if program in {"env", "xargs"}:
        return any(_is_shell_name(arg) for arg in argv[1:])

    if program == "find":
        for i, arg in enumerate(argv):
            if arg in ("-exec", "-execdir") and i + 1 < len(argv):
                command_end = next(
                    (
                        j
                        for j in range(i + 1, len(argv))
                        if argv[j] in (";", "+")
                    ),
                    len(argv),
                )
                if any(
                    _is_shell_name(token)
                    for token in argv[i + 1 : command_end]
                ):
                    return True
        return False

    return False


def _is_absolute_path_arg(arg: str) -> bool:
    return Path(arg).is_absolute() or PureWindowsPath(arg).is_absolute()


def _reject_bad_path_argument(
    workspace: Path,
    cwd_path: Path,
    candidate: str,
) -> None:
    if not candidate:
        return

    if _is_absolute_path_arg(candidate):
        raise ValueError("参数中包含绝对路径，已拒绝")

    path = Path(candidate)
    windows_path = PureWindowsPath(candidate)

    if ".." in path.parts or ".." in windows_path.parts:
        raise ValueError("参数不能包含 .. 路径段")

    if _is_sensitive_path(path) or _is_sensitive_path(windows_path):
        raise ValueError("禁止访问敏感或内部路径")

    relative_cwd = cwd_path.relative_to(workspace.resolve())
    workspace_relative = relative_cwd / path
    _reject_symlinks(workspace, str(workspace_relative))


def _validate_argument_paths(
    workspace: Path,
    cwd_path: Path,
    argv: list[str],
) -> None:
    for arg in argv[1:]:
        if arg.startswith("-") and "=" in arg:
            _reject_bad_path_argument(
                workspace,
                cwd_path,
                arg.split("=", 1)[1],
            )
        else:
            _reject_bad_path_argument(workspace, cwd_path, arg)


def _validate_run_command(
    workspace: Path,
    argv: list[str],
    cwd: str = ".",
    timeout_seconds: int = 120,
) -> None:
    if not argv:
        raise ValueError("argv 不能为空")

    if not all(isinstance(arg, str) for arg in argv):
        raise ValueError("argv 必须是字符串列表")

    if not argv[0].strip():
        raise ValueError("命令不能为空")

    if _launches_shell(argv):
        raise ValueError("禁止通过 run_command 启动 Shell 解释器")

    violation = find_shell_syntax_violation(argv)
    if violation is not None:
        raise ValueError(
            f"不支持 Shell 运算符或重定向：{violation!r}。"
            "run_command 一次只运行一个程序，请拆分成多次工具调用，"
            "或改用其他工具（如 write_file、search_text、list_files 等）完成。"
        )

    if not 1 <= timeout_seconds <= 300:
        raise ValueError("timeout_seconds 必须在 1 到 300 之间")

    cwd_path = _resolve_workspace_path(workspace, cwd)

    if not cwd_path.exists():
        raise ValueError(f"cwd 不存在：{cwd!r}")

    if not cwd_path.is_dir():
        raise ValueError(f"cwd 不是目录：{cwd!r}")

    _validate_argument_paths(workspace, cwd_path, argv)


def run_command(
    workspace: Path,
    argv: list[str],
    cwd: str = ".",
    timeout_seconds: int = 120,
) -> str:
    _validate_run_command(workspace, argv, cwd, timeout_seconds)

    cwd_path = _resolve_workspace_path(workspace, cwd)

    with tempfile.TemporaryDirectory(prefix="vibe-harness-home-") as tmpdir:
        env = _sanitized_env(Path(tmpdir))
        try:
            result = subprocess.run(
                argv,
                cwd=str(cwd_path),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return "Error: 命令超时"
        except FileNotFoundError:
            return "Error: 命令不存在"
        except OSError:
            return "Error: 无法执行命令"

    output = (
        f"exit_code: {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    return _truncate_output(output, MAX_COMMAND_OUTPUT_CHARS)
