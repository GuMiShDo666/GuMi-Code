import io
import subprocess
from pathlib import Path

import pytest

from vibe_harness.tools import registry
from vibe_harness.tools import command as command_module
from vibe_harness.tools.command import (
    DockerSandboxConfig,
    find_shell_syntax_violation,
    load_docker_sandbox,
    run_command,
)
from vibe_harness.tools.registry import execute_tool


_IMAGE_ID = "sha256:" + "a" * 64


def _sandbox() -> DockerSandboxConfig:
    return DockerSandboxConfig(Path("/usr/local/bin/docker"), _IMAGE_ID)


class _FakeProcess:
    def __init__(
        self,
        args: list[str],
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
    ) -> None:
        self.args = args
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode: int | None = None
        self._final_returncode = returncode
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._final_returncode
        return self._final_returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _install_fake_docker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> tuple[dict[str, object], _FakeProcess]:
    captured: dict[str, object] = {}
    process = _FakeProcess([], stdout, stderr, returncode)

    def fake_popen(args: list[str], **kwargs: object) -> _FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        process.args = args
        return process

    monkeypatch.setattr(command_module, "_validate_docker_sandbox", lambda value: None)
    monkeypatch.setattr(command_module.subprocess, "Popen", fake_popen)
    return captured, process


def _docker_args(captured: dict[str, object]) -> list[str]:
    args = captured["args"]
    assert isinstance(args, list)
    return args


def test_run_command_captures_stdout_and_stderr(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_docker(
        monkeypatch,
        stdout=b"hello\n",
        stderr=b"warning\n",
        returncode=3,
    )

    out = run_command(workspace, ["python", "-V"], sandbox=_sandbox())

    assert "exit_code: 3" in out
    assert "hello" in out
    assert "warning" in out


def test_run_command_builds_fixed_sandbox_argv(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, _ = _install_fake_docker(monkeypatch)

    run_command(workspace, ["python", "-m", "pytest"], sandbox=_sandbox())

    args = _docker_args(captured)
    image_index = args.index(_IMAGE_ID)

    assert args[:2] == ["/usr/local/bin/docker", "run"]
    for required in (
        "--rm",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        "--ipc=none",
        "--init",
    ):
        assert required in args[:image_index]
    assert "--privileged" not in args
    assert "--entrypoint" in args[:image_index]
    assert args[args.index("--entrypoint") + 1] == "python"
    assert args[image_index + 1 :] == ["-m", "pytest"]
    assert args[args.index("--memory") + 1] == "512m"
    assert args[args.index("--memory-swap") + 1] == "512m"
    assert args[args.index("--cpus") + 1] == "1"
    assert args[args.index("--pids-limit") + 1] == "128"
    assert args[args.index("--ulimit") + 1] == "nofile=256:256"
    assert args[args.index("--user") + 1] != "0:0"
    popen_kwargs = captured["kwargs"]
    assert isinstance(popen_kwargs, dict)
    assert popen_kwargs["stdin"] is subprocess.DEVNULL
    assert "shell" not in popen_kwargs
    assert "env" not in popen_kwargs


def test_run_command_mounts_only_workspace_and_sensitive_masks(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace / ".git").mkdir()
    (workspace / ".env").write_text("placeholder", encoding="utf-8")
    (workspace / "secret.pem").write_text("placeholder", encoding="utf-8")
    (workspace / ".env.example").write_text("SAFE=1", encoding="utf-8")
    captured, _ = _install_fake_docker(monkeypatch)

    run_command(workspace, ["python", "-V"], sandbox=_sandbox())

    args = _docker_args(captured)
    mounts = [args[index + 1] for index, value in enumerate(args) if value == "--mount"]

    assert mounts[0] == f"type=bind,source={workspace.resolve()},target=/workspace"
    assert any("target=/workspace/.git,readonly" in mount for mount in mounts)
    assert any("target=/workspace/.env,readonly" in mount for mount in mounts)
    assert any("target=/workspace/secret.pem,readonly" in mount for mount in mounts)
    assert not any(".env.example" in mount for mount in mounts)
    assert not any("docker.sock" in mount for mount in mounts)


def test_run_command_uses_requested_container_cwd(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace / "sub").mkdir()
    captured, _ = _install_fake_docker(monkeypatch)

    run_command(
        workspace,
        ["python", "-V"],
        cwd="sub",
        sandbox=_sandbox(),
    )

    args = _docker_args(captured)
    assert args[args.index("--workdir") + 1] == "/workspace/sub"


def test_run_command_rejects_escape_cwd(workspace: Path) -> None:
    with pytest.raises(ValueError):
        run_command(workspace, ["echo", "x"], cwd="../")


def test_run_command_enforces_timeout_and_cleans_exact_container(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, process = _install_fake_docker(monkeypatch)
    removed: list[str] = []
    monkeypatch.setattr(
        command_module,
        "_communicate_bounded",
        lambda process, timeout: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(process.args, timeout)
        ),
    )
    monkeypatch.setattr(
        command_module,
        "_remove_container",
        lambda sandbox, name: removed.append(name) or True,
    )

    out = run_command(
        workspace,
        ["python", "-V"],
        timeout_seconds=1,
        sandbox=_sandbox(),
    )

    assert "Error: 命令超时" in out
    args = _docker_args(captured)
    container_name = args[args.index("--name") + 1]
    assert container_name.startswith("vibe-harness-")
    assert removed == [container_name]
    assert process.terminated


def test_run_command_interrupt_cleans_then_propagates(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, process = _install_fake_docker(monkeypatch)
    removed: list[str] = []
    monkeypatch.setattr(
        command_module,
        "_communicate_bounded",
        lambda process, timeout: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    monkeypatch.setattr(
        command_module,
        "_remove_container",
        lambda sandbox, name: removed.append(name) or True,
    )

    with pytest.raises(KeyboardInterrupt):
        run_command(workspace, ["python", "-V"], sandbox=_sandbox())

    args = _docker_args(captured)
    assert removed == [args[args.index("--name") + 1]]
    assert process.terminated


def test_remove_container_targets_only_exact_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(command_module.subprocess, "run", fake_run)

    assert command_module._remove_container(_sandbox(), "vibe-harness-exact")
    assert calls == [
        [
            "/usr/local/bin/docker",
            "rm",
            "--force",
            "vibe-harness-exact",
        ]
    ]


def test_run_command_stops_on_output_quota(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_docker(
        monkeypatch,
        stdout=b"x" * (command_module.MAX_COMMAND_CAPTURE_BYTES + 1),
    )
    monkeypatch.setattr(command_module, "_remove_container", lambda sandbox, name: True)

    out = run_command(
        workspace,
        ["python", "-V"],
        sandbox=_sandbox(),
    )

    assert out == "Error: 命令输出超过限制"


def test_run_command_truncates_model_visible_output(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_docker(monkeypatch, stdout=b"x" * 60_000)

    out = run_command(workspace, ["python", "-V"], sandbox=_sandbox())

    assert out.endswith("[output truncated]")
    assert len(out) <= command_module.MAX_COMMAND_OUTPUT_CHARS + 20


def test_run_command_reports_unconfirmed_cleanup(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_docker(
        monkeypatch,
        stdout=b"x" * (command_module.MAX_COMMAND_CAPTURE_BYTES + 1),
    )
    monkeypatch.setattr(command_module, "_remove_container", lambda sandbox, name: False)

    out = run_command(workspace, ["python", "-V"], sandbox=_sandbox())

    assert "Sandbox 清理未确认" in out


def test_run_command_passes_only_fixed_container_environment(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "super-secret-value")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-github-token")
    captured, _ = _install_fake_docker(monkeypatch)

    run_command(workspace, ["python", "-V"], sandbox=_sandbox())

    args = _docker_args(captured)
    env_values = [args[index + 1] for index, value in enumerate(args) if value == "--env"]
    assert env_values == [
        "HOME=/tmp",
        "XDG_CONFIG_HOME=/tmp/.config",
        "XDG_CACHE_HOME=/tmp/.cache",
        "XDG_DATA_HOME=/tmp/.local/share",
    ]
    assert "super-secret-value" not in "\n".join(args)
    assert "fake-github-token" not in "\n".join(args)


def test_run_command_hides_docker_startup_details(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_docker(
        monkeypatch,
        stderr=b"secret registry and host path",
        returncode=125,
    )
    monkeypatch.setattr(command_module, "_remove_container", lambda sandbox, name: True)

    out = run_command(
        workspace,
        ["python", "-V"],
        sandbox=_sandbox(),
    )

    assert out == "Error: Docker Sandbox 启动失败"
    assert "secret registry" not in out


def test_run_command_has_no_host_fallback_when_docker_launch_fails(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(command_module, "_validate_docker_sandbox", lambda value: None)

    def fail_popen(args: list[str], **kwargs: object) -> object:
        calls.append(args)
        raise OSError("private host detail")

    monkeypatch.setattr(command_module.subprocess, "Popen", fail_popen)

    out = run_command(workspace, ["touch", "marker.txt"], sandbox=_sandbox())

    assert out == "Error: 无法启动 Docker Sandbox"
    assert calls[0][:2] == ["/usr/local/bin/docker", "run"]
    assert not (workspace / "marker.txt").exists()
    assert "private host detail" not in out


def test_run_command_rejects_missing_sandbox(workspace: Path) -> None:
    with pytest.raises(ValueError, match="Docker Sandbox"):
        run_command(workspace, ["python", "-V"])


def test_run_command_rejects_sensitive_symlink(workspace: Path) -> None:
    outside = workspace.parent / "outside-env"
    outside.write_text("placeholder", encoding="utf-8")
    (workspace / ".env").symlink_to(outside)

    try:
        with pytest.raises(ValueError, match="敏感路径不允许是符号链接"):
            command_module._find_sensitive_paths(workspace)
    finally:
        outside.unlink(missing_ok=True)


def test_load_docker_sandbox_resolves_local_image_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(command_module.shutil, "which", lambda name: "/usr/bin/docker")

    def fake_control(path: Path, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        calls.append(arguments)
        if arguments[0] == "version":
            return subprocess.CompletedProcess(arguments, 0, b"29.1.3\n", b"")
        return subprocess.CompletedProcess(arguments, 0, f"{_IMAGE_ID}\n".encode(), b"")

    monkeypatch.setattr(command_module, "_run_docker_control", fake_control)

    sandbox = load_docker_sandbox("trusted:local")

    assert sandbox.image_id == _IMAGE_ID
    assert calls == [
        ["version", "--format", "{{.Server.Version}}"],
        ["image", "inspect", "--format", "{{.Id}}", "trusted:local"],
    ]
    assert all("pull" not in call for call in calls)


def test_load_docker_sandbox_fails_when_daemon_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command_module.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        command_module,
        "_run_docker_control",
        lambda path, arguments: subprocess.CompletedProcess(arguments, 1, b"", b"private"),
    )

    with pytest.raises(ValueError, match="Docker daemon 不可用"):
        load_docker_sandbox("trusted:local")


def test_load_docker_sandbox_fails_without_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command_module.shutil, "which", lambda name: None)

    with pytest.raises(ValueError, match="未找到 Docker CLI"):
        load_docker_sandbox("trusted:local")


def test_load_docker_sandbox_fails_without_local_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command_module.shutil, "which", lambda name: "/usr/bin/docker")

    def fake_control(path: Path, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        returncode = 0 if arguments[0] == "version" else 1
        stdout = b"29.1.3\n" if returncode == 0 else b""
        return subprocess.CompletedProcess(arguments, returncode, stdout, b"private")

    monkeypatch.setattr(command_module, "_run_docker_control", fake_control)

    with pytest.raises(ValueError, match="镜像不存在于本地 Docker"):
        load_docker_sandbox("trusted:missing")


def test_run_command_daemon_failure_skips_hook_and_approval(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        command_module,
        "_run_docker_control",
        lambda path, arguments: subprocess.CompletedProcess(arguments, 1, b"", b"private"),
    )

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["python", "-V"]}',
        approval_callback=lambda name, args, reason: calls.append("approval") or True,
        pre_tool_use_callback=lambda name, args: calls.append("hook"),
        command_sandbox=_sandbox(),
    )

    assert "Docker daemon 不可用" in out
    assert calls == []


def test_run_command_invalid_mount_path_skips_hook_and_approval(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe_workspace = workspace / "comma,workspace"
    unsafe_workspace.mkdir()
    calls: list[str] = []
    monkeypatch.setattr(command_module, "_validate_docker_sandbox", lambda value: None)

    out = execute_tool(
        unsafe_workspace,
        "run_command",
        '{"argv": ["python", "-V"]}',
        approval_callback=lambda name, args, reason: calls.append("approval") or True,
        pre_tool_use_callback=lambda name, args: calls.append("hook"),
        command_sandbox=_sandbox(),
    )

    assert "Docker mount 不支持" in out
    assert calls == []


def test_sensitive_mask_count_is_bounded(workspace: Path) -> None:
    for index in range(command_module.MAX_SANDBOX_MASKS + 1):
        (workspace / f".env.{index}").write_text("placeholder", encoding="utf-8")

    with pytest.raises(ValueError, match="敏感路径过多"):
        command_module._find_sensitive_paths(workspace)


@pytest.mark.parametrize("timeout", [True, False, 0, 301, "5"])
def test_run_command_rejects_invalid_timeout(
    workspace: Path,
    timeout: object,
) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        run_command(
            workspace,
            ["python", "-V"],
            timeout_seconds=timeout,  # type: ignore[arg-type]
        )


def test_run_command_requires_config_before_approval(workspace: Path) -> None:
    marker = workspace / "marker.txt"

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["touch", "marker.txt"]}',
    )

    assert out == "Error: run_command 需要已配置的 Docker Sandbox"
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


def test_run_command_echo_sh_not_rejected(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(command_module, "_validate_docker_sandbox", lambda value: None)

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return False

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["echo", "sh"]}',
        approval_callback=callback,
        command_sandbox=_sandbox(),
    )

    assert calls == ["run_command"]
    assert out == "Error: Tool execution denied by user: run_command"


def test_run_command_python_enters_approval(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(command_module, "_validate_docker_sandbox", lambda value: None)

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return False

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["python", "-m", "pytest"]}',
        approval_callback=callback,
        command_sandbox=_sandbox(),
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


def test_run_command_safe_relative_paths_enter_approval(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(command_module, "_validate_docker_sandbox", lambda value: None)

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return False

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["cat", ".env.example"]}',
        approval_callback=callback,
        command_sandbox=_sandbox(),
    )
    assert calls == ["run_command"]
    assert out == "Error: Tool execution denied by user: run_command"

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["cat", "src/app.py"]}',
        approval_callback=callback,
        command_sandbox=_sandbox(),
    )
    assert calls == ["run_command", "run_command"]


def test_run_command_absolute_program_enters_approval(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(command_module, "_validate_docker_sandbox", lambda value: None)

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return False

    out = execute_tool(
        workspace,
        "run_command",
        '{"argv": ["/usr/bin/python", "--version"]}',
        approval_callback=callback,
        command_sandbox=_sandbox(),
    )

    assert calls == ["run_command"]
    assert out == "Error: Tool execution denied by user: run_command"
