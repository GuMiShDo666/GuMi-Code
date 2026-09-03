import sys
from pathlib import Path

import pytest

from vibe_harness.tools import registry
from vibe_harness.tools.command import find_shell_syntax_violation, run_command
from vibe_harness.tools.registry import execute_tool


def test_run_command_captures_stdout(workspace: Path) -> None:
    out = run_command(workspace, [sys.executable, "-c", "print('hello')"])

    assert "exit_code: 0" in out
    assert "hello" in out


def test_run_command_captures_stderr_and_exit_code(workspace: Path) -> None:
    out = run_command(
        workspace,
        [sys.executable, "-c", "import sys; print('oops', file=sys.stderr); sys.exit(3)"],
    )

    assert "exit_code: 3" in out
    assert "oops" in out


def test_run_command_uses_requested_cwd(workspace: Path) -> None:
    (workspace / "sub").mkdir()

    out = run_command(
        workspace,
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd="sub",
    )

    assert str(workspace / "sub") in out


def test_run_command_rejects_escape_cwd(workspace: Path) -> None:
    with pytest.raises(ValueError):
        run_command(workspace, ["echo", "x"], cwd="../")


def test_run_command_enforces_timeout(workspace: Path) -> None:
    out = run_command(
        workspace,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout_seconds=1,
    )

    assert "Error: 命令超时" in out


def test_run_command_truncates_output(workspace: Path) -> None:
    out = run_command(
        workspace,
        [sys.executable, "-c", "print('x' * 60000)"],
    )

    assert out.endswith("[output truncated]")


def test_run_command_does_not_expose_api_key(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "super-secret-value")

    out = run_command(
        workspace,
        [sys.executable, "-c", "import os; print(os.environ.get('LLM_API_KEY', ''))"],
    )

    assert "super-secret-value" not in out


def test_run_command_does_not_expose_other_tokens(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "fake-github-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-aws-secret")

    out = run_command(
        workspace,
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "print(os.environ.get('GITHUB_TOKEN', '')); "
                "print(os.environ.get('AWS_SECRET_ACCESS_KEY', ''))"
            ),
        ],
    )

    assert "fake-github-token" not in out
    assert "fake-aws-secret" not in out


def _stdout_of(out: str) -> str:
    return out.split("stdout:\n", 1)[1].split("\nstderr:\n", 1)[0]


def test_run_command_uses_isolated_temporary_home(workspace: Path) -> None:
    out = run_command(
        workspace,
        [sys.executable, "-c", "import os; print(os.environ['HOME'])"],
    )

    home = Path(_stdout_of(out).strip())

    assert home != Path.home()
    assert home != workspace
    assert not home.exists()


def test_run_command_xdg_paths_are_inside_home(workspace: Path) -> None:
    out = run_command(
        workspace,
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "print(os.environ['HOME']); "
                "print(os.environ['XDG_CONFIG_HOME']); "
                "print(os.environ['XDG_CACHE_HOME']); "
                "print(os.environ['XDG_DATA_HOME'])"
            ),
        ],
    )

    home, config, cache, data = [
        Path(line) for line in _stdout_of(out).splitlines()
    ]

    for path in (config, cache, data):
        assert path.is_relative_to(home)


def test_run_command_home_is_writable(workspace: Path) -> None:
    out = run_command(
        workspace,
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "p = os.path.join(os.environ['HOME'], 'marker.txt'); "
                "open(p, 'w').write('hi'); print('written')"
            ),
        ],
    )

    assert "written" in _stdout_of(out)


def test_run_command_uses_fresh_home_each_call(workspace: Path) -> None:
    homes = []
    for _ in range(2):
        out = run_command(
            workspace,
            [sys.executable, "-c", "import os; print(os.environ['HOME'])"],
        )
        homes.append(_stdout_of(out).strip())

    assert homes[0] != homes[1]


def test_run_command_does_not_copy_real_home_config(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = workspace / "fake-home"
    fake_home.mkdir()
    (fake_home / ".gitconfig").write_text("[user]\n  name = Fake User\n")
    monkeypatch.setenv("HOME", str(fake_home))

    out = run_command(
        workspace,
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "print(os.environ['HOME']); "
                "print(os.path.exists("
                "os.path.join(os.environ['HOME'], '.gitconfig')))"
            ),
        ],
    )

    home, has_gitconfig = _stdout_of(out).splitlines()

    assert home != str(fake_home)
    assert has_gitconfig == "False"


def test_run_command_home_is_not_workspace(workspace: Path) -> None:
    out = run_command(
        workspace,
        [sys.executable, "-c", "import os; print(os.environ['HOME'])"],
    )

    home = _stdout_of(out).strip()

    assert home != str(workspace)
    assert str(workspace) not in home


def test_run_command_sensitive_env_still_excluded_after_home_change(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "super-secret-value")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-github-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-aws-secret")

    out = run_command(
        workspace,
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "print(os.environ.get('LLM_API_KEY', '')); "
                "print(os.environ.get('GITHUB_TOKEN', '')); "
                "print(os.environ.get('AWS_SECRET_ACCESS_KEY', ''))"
            ),
        ],
    )

    assert "super-secret-value" not in out
    assert "fake-github-token" not in out
    assert "fake-aws-secret" not in out


def test_run_command_requires_approval(workspace: Path) -> None:
    marker = workspace / "marker.txt"

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["touch", "marker.txt"]}',
    )

    assert out == "Error: Approval required: run_command"
    assert not marker.exists()


# --- 禁止显式启动 Shell ---


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_run_command_rejects_shell(workspace: Path, shell: str) -> None:
    with pytest.raises(ValueError, match="禁止通过 run_command 启动 Shell 解释器"):
        run_command(workspace, [shell, "-c", "echo hi"])


def test_run_command_rejects_shell_with_path(workspace: Path) -> None:
    with pytest.raises(ValueError, match="禁止通过 run_command 启动 Shell 解释器"):
        run_command(workspace, ["/bin/sh", "-c", "echo hi"])


def test_run_command_rejects_env_shell(workspace: Path) -> None:
    with pytest.raises(ValueError, match="禁止通过 run_command 启动 Shell 解释器"):
        run_command(workspace, ["env", "sh", "-c", "echo hi"])


def test_run_command_rejects_xargs_shell(workspace: Path) -> None:
    with pytest.raises(ValueError, match="禁止通过 run_command 启动 Shell 解释器"):
        run_command(workspace, ["xargs", "sh", "-c", "echo hi"])


def test_run_command_rejects_xargs_shell_after_option_value(
    workspace: Path,
) -> None:
    with pytest.raises(ValueError, match="禁止通过 run_command 启动 Shell 解释器"):
        run_command(workspace, ["xargs", "-n", "1", "sh", "-c", "echo hi"])


def test_run_command_rejects_find_exec_shell(workspace: Path) -> None:
    with pytest.raises(ValueError, match="禁止通过 run_command 启动 Shell 解释器"):
        run_command(workspace, ["find", ".", "-exec", "sh", "-c", "echo hi", ";"])


def test_run_command_rejects_find_exec_wrapped_shell(workspace: Path) -> None:
    with pytest.raises(ValueError, match="禁止通过 run_command 启动 Shell 解释器"):
        run_command(
            workspace,
            ["find", ".", "-exec", "env", "sh", "-c", "echo hi", ";"],
        )


def test_run_command_shell_skips_approval(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["sh", "-c", "touch pwned.txt"]}',
        approval_callback=callback,
    )

    assert calls == []
    assert "禁止通过 run_command 启动 Shell 解释器" in out
    assert not (workspace / "pwned.txt").exists()


def test_run_command_echo_sh_not_rejected(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["echo", "sh"]}',
        approval_callback=callback,
    )

    assert calls == ["run_command"]
    assert "exit_code: 0" in out


def test_run_command_python_enters_approval(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return False

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["python", "-m", "pytest"]}',
        approval_callback=callback,
    )

    assert calls == ["run_command"]
    assert out == "Error: Tool execution denied by user: run_command"


@pytest.mark.parametrize(
    "arguments_json",
    [
        '{"argv": ["echo", 123]}',
        '{"argv": ["rm", 123]}',
    ],
)
def test_run_command_non_string_argv_skips_approval(
    workspace: Path,
    arguments_json: str,
) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = execute_tool(
        workspace,
        "run_command",
        arguments_json,
        approval_callback=callback,
    )

    assert calls == []
    assert "argv 必须是字符串列表" in out


# --- 拒绝 Shell 控制符与重定向 ---


@pytest.mark.parametrize("token", ["&&", "||", "|", ";"])
def test_find_shell_syntax_violation_control_operators(token: str) -> None:
    assert find_shell_syntax_violation(["echo", "a", token, "b"]) == token


@pytest.mark.parametrize("token", [">", ">>", "<", "<<"])
def test_find_shell_syntax_violation_redirection(token: str) -> None:
    assert find_shell_syntax_violation(["echo", token, "file"]) == token


@pytest.mark.parametrize(
    "token",
    [
        ">output.log",
        ">>output.log",
        "<input.txt",
        "0<input.txt",
        "2>",
        "2>>",
        "2>error.log",
        "2>>error.log",
        "2>&1",
        "1<&0",
        "&>",
        "&>>",
        "&>output.log",
    ],
)
def test_find_shell_syntax_violation_fd_redirection(token: str) -> None:
    assert find_shell_syntax_violation(["sh", "-c", token]) == token


@pytest.mark.parametrize(
    "argv",
    [
        ["echo", "hello"],
        ["echo", "hello, world"],
        ["echo", "a>b"],
        ["echo", "a;b"],
        ["echo", "a|b"],
        ["echo", ">="],
        ["echo", "x>=y"],
        ["rg", "--glob", "*.py"],
        ["find", ".", "-name", "*.txt"],
        ["find", ".", "-exec", "echo", "{}", ";"],
        ["find", ".", "-execdir", "echo", "{}", "+"],
        ["grep", "-n", "pat[0-9]?"],
    ],
)
def test_find_shell_syntax_violation_ignores_normal_arguments(
    argv: list[str],
) -> None:
    assert find_shell_syntax_violation(argv) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["echo", "a", "&&", "b"],
        ["echo", "a", "||", "b"],
        ["echo", "a", "|", "b"],
        ["echo", "a", ";", "b"],
        ["echo", "x", ">", "file"],
        ["echo", "x", ">>", "file"],
        ["cat", "<", "file"],
        ["echo", ">out.txt"],
        ["cat", "0<input.txt"],
        ["echo", "2>error.log"],
        ["echo", "&>output.log"],
    ],
)
def test_run_command_rejects_shell_syntax(workspace: Path, argv: list[str]) -> None:
    with pytest.raises(ValueError, match="不支持 Shell 运算符或重定向"):
        run_command(workspace, argv)


@pytest.mark.parametrize(
    "arguments_json",
    [
        '{"argv": ["echo", "a", "&&", "echo", "b"]}',
        '{"argv": ["echo", "x", ">", "out.txt"]}',
        '{"argv": ["echo", ">out.txt"]}',
        '{"argv": ["cat", "0<input.txt"]}',
        '{"argv": ["echo", "2>error.log"]}',
    ],
)
def test_run_command_shell_syntax_skips_approval(
    workspace: Path,
    arguments_json: str,
) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = execute_tool(
        workspace,
        "run_command",
        arguments_json,
        approval_callback=callback,
    )

    assert calls == []
    assert "不支持 Shell 运算符或重定向" in out


def test_run_command_shell_syntax_skips_handler(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**kwargs: object) -> str:
        raise AssertionError("handler must not run for shell syntax")

    monkeypatch.setitem(registry.ALL_TOOL_HANDLERS, "run_command", boom)

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["echo", "a", "|", "wc", "-l"]}',
        approval_callback=lambda name, args, reason: True,
    )

    assert "不支持 Shell 运算符或重定向" in out


# --- 拒绝显式的敏感与越界路径参数 ---


@pytest.mark.parametrize(
    "argv",
    [
        ["cat", ".env"],
        ["cat", ".env.local"],
        ["cat", ".ssh/config"],
        ["cat", "id_rsa"],
        ["cat", "cert.pem"],
        ["cat", "/etc/passwd"],
        ["cat", "../outside.txt"],
        ["cat", "a/../../outside.txt"],
        ["cat", "..\\outside.txt"],
        ["cat", "C:\\Users\\foo\\file"],
        ["cat", ".ssh\\config"],
        ["tool", "--config=.env"],
        ["tool", "--file=/etc/passwd"],
    ],
)
def test_run_command_rejects_bad_path_arguments(
    workspace: Path,
    argv: list[str],
) -> None:
    with pytest.raises(ValueError):
        run_command(workspace, argv)


def test_run_command_rejects_symlink_argument(workspace: Path) -> None:
    outside = workspace.parent / "outside_secret.txt"
    outside.write_text("secret")
    link = workspace / "link.txt"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="禁止通过符号链接访问"):
        run_command(workspace, ["cat", "link.txt"])


def test_run_command_rejects_symlink_relative_to_cwd(workspace: Path) -> None:
    subdirectory = workspace / "sub"
    subdirectory.mkdir()
    outside = workspace.parent / "outside_from_subdirectory.txt"
    outside.write_text("synthetic", encoding="utf-8")
    link = subdirectory / "link.txt"
    link.symlink_to(outside)

    try:
        with pytest.raises(ValueError, match="禁止通过符号链接访问"):
            run_command(workspace, ["cat", "link.txt"], cwd="sub")
    finally:
        outside.unlink(missing_ok=True)


def test_run_command_sensitive_path_skips_approval(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["cat", ".env"]}',
        approval_callback=callback,
    )

    assert calls == []
    assert "禁止访问敏感或内部路径" in out


def test_run_command_absolute_path_skips_approval(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["cat", "/etc/passwd"]}',
        approval_callback=callback,
    )

    assert calls == []
    assert "绝对路径" in out


def test_run_command_flag_path_skips_approval(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["tool", "--config=.env"]}',
        approval_callback=callback,
    )
    assert calls == []
    assert "禁止访问敏感或内部路径" in out

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["tool", "--file=/etc/passwd"]}',
        approval_callback=callback,
    )
    assert calls == []
    assert "绝对路径" in out


def test_run_command_safe_relative_paths_enter_approval(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return False

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["cat", ".env.example"]}',
        approval_callback=callback,
    )
    assert calls == ["run_command"]
    assert out == "Error: Tool execution denied by user: run_command"

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["cat", "src/app.py"]}',
        approval_callback=callback,
    )
    assert calls == ["run_command", "run_command"]


def test_run_command_absolute_program_enters_approval(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return False

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["/usr/bin/python", "--version"]}',
        approval_callback=callback,
    )

    assert calls == ["run_command"]
    assert out == "Error: Tool execution denied by user: run_command"
