"""Validate and run argv-only commands inside a fixed Docker sandbox."""

import os
import json
import queue
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import BinaryIO

from gum_code.tools.command_logs import (
    COMMAND_PREVIEW_BYTES,
    MAX_COMMAND_LOG_BYTES,
    CommandCapture,
    command_output_marker,
    prepare_command_logs,
    publish_command_log,
)
from gum_code.tools.common import (
    _is_sensitive_path,
    _reject_symlinks,
    _resolve_workspace_path,
)

MAX_COMMAND_OUTPUT_CHARS = COMMAND_PREVIEW_BYTES + 1024
MAX_SANDBOX_MASKS = 256

SANDBOX_MEMORY = "512m"
SANDBOX_CPUS = "1"
SANDBOX_PIDS_LIMIT = 128
SANDBOX_NOFILE_LIMIT = 256
SANDBOX_TMPFS_SIZE = "64m"

_CONTAINER_WORKSPACE = "/workspace"
_DOCKER_CONTROL_TIMEOUT_SECONDS = 5
_DOCKER_CLEANUP_TIMEOUT_SECONDS = 5
_READ_CHUNK_BYTES = 8192
_DOCKER_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ENVIRONMENT_LABEL = "io.gum-code.project-environment"
ENVIRONMENT_SCOPE_LABEL = "io.gum-code.project-scope"
ENVIRONMENT_VOLUME_RE = re.compile(r"gum-code-env-[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class DependencyMount:
    """Host-owned dependency volume, never accepted from tool arguments."""

    volume: str
    scope: str
    ecosystem: str
    package_root: str = "."


@dataclass(frozen=True)
class DockerSandboxConfig:
    docker_path: Path
    image_id: str
    dependency: DependencyMount | None = None


@dataclass(frozen=True)
class CommandExecutionResult:
    """Host evidence; unknown startup is never evidence of successful execution."""

    started: bool | None
    exit_code: int | None
    reason: str
    output: str = ""


CommandResultCallback = Callable[[CommandExecutionResult], None]


class _CommandInterrupted(KeyboardInterrupt):
    def __init__(self, result: CommandExecutionResult) -> None:
        self.result = result


class _CommandLogFailure(ValueError):
    """A post-execution security failure must not claim execution never started."""


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
    *,
    sandbox: DockerSandboxConfig | None = None,
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

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 300
    ):
        raise ValueError("timeout_seconds 必须在 1 到 300 之间")

    cwd_path = _resolve_workspace_path(workspace, cwd)

    if not cwd_path.exists():
        raise ValueError(f"cwd 不存在：{cwd!r}")

    if not cwd_path.is_dir():
        raise ValueError(f"cwd 不是目录：{cwd!r}")

    _validate_argument_paths(workspace, cwd_path, argv)

    _validate_docker_sandbox(sandbox)
    if sandbox is not None:
        _validate_dependency_mount(sandbox, workspace)
    _validate_sandbox_workspace(workspace, sandbox=sandbox)


def _validate_dependency_mount(sandbox: DockerSandboxConfig, workspace: Path) -> None:
    dependency = sandbox.dependency
    if dependency is None:
        return
    if (
        not isinstance(dependency, DependencyMount)
        or not ENVIRONMENT_VOLUME_RE.fullmatch(dependency.volume)
        or not re.fullmatch(r"[0-9a-f]{64}", dependency.scope)
        or dependency.ecosystem not in {"python", "node"}
    ):
        raise ValueError("项目依赖环境无效")
    package = _resolve_workspace_path(workspace, dependency.package_root)
    if not package.is_dir():
        raise ValueError("项目依赖环境目录无效")
    if dependency.ecosystem == "node":
        target = str(package.relative_to(workspace.resolve()) / "node_modules")
        _reject_symlinks(workspace, target)
        path = package / "node_modules"
        if path.exists() and not path.is_dir():
            raise ValueError("项目依赖挂载位置不是目录")
    result = _run_docker_control(sandbox.docker_path, [
        "volume", "inspect", "--format", "{{json .Labels}}", dependency.volume,
    ])
    try:
        labels = json.loads(result.stdout) if len(result.stdout) <= 8192 else None
    except (ValueError, UnicodeError):
        labels = None
    if (
        result.returncode != 0 or not isinstance(labels, dict)
        or labels.get(ENVIRONMENT_LABEL) != "v1"
        or labels.get(ENVIRONMENT_SCOPE_LABEL) != dependency.scope
    ):
        raise ValueError("项目依赖卷不存在或归属不匹配")


def _validate_image_reference(image: str) -> None:
    if not isinstance(image, str) or not image or not image.strip():
        raise ValueError("sandbox-image 不能为空")

    if image != image.strip() or image.startswith("-"):
        raise ValueError("sandbox-image 格式无效")

    if any(character.isspace() or character == "\x00" for character in image):
        raise ValueError("sandbox-image 格式无效")


def _run_docker_control(
    docker_path: Path,
    arguments: list[str],
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            [str(docker_path), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_DOCKER_CONTROL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Docker daemon 响应超时")
    except OSError:
        raise ValueError("无法连接 Docker，请确认 Docker Desktop 已启动")


def _inspect_local_image(docker_path: Path, image: str) -> str:
    result = _run_docker_control(
        docker_path,
        ["image", "inspect", "--format", "{{.Id}}", image],
    )

    if result.returncode != 0:
        raise ValueError("Sandbox 镜像不存在于本地 Docker，且 Harness 不会自动拉取")

    image_id = result.stdout.decode("utf-8", errors="replace").strip()
    if not _DOCKER_IMAGE_ID_RE.fullmatch(image_id):
        raise ValueError("Docker 未返回有效的本地镜像 ID")

    return image_id


def load_docker_sandbox(image: str) -> DockerSandboxConfig:
    _validate_image_reference(image)

    docker = shutil.which("docker")
    if docker is None:
        raise ValueError("未找到 Docker CLI，请先安装 Docker Desktop")

    docker_path = Path(docker).resolve()
    version = _run_docker_control(
        docker_path,
        ["version", "--format", "{{.Server.Version}}"],
    )
    if version.returncode != 0 or not version.stdout.strip():
        raise ValueError("Docker daemon 不可用，请启动 Docker Desktop")

    image_id = _inspect_local_image(docker_path, image)
    return DockerSandboxConfig(docker_path=docker_path, image_id=image_id)


def _validate_docker_sandbox(sandbox: DockerSandboxConfig | None) -> None:
    if sandbox is None:
        raise ValueError("run_command 需要已配置的 Docker Sandbox")

    if not isinstance(sandbox, DockerSandboxConfig):
        raise ValueError("Docker Sandbox 配置无效")

    if not sandbox.docker_path.is_absolute():
        raise ValueError("Docker CLI 路径必须是绝对路径")

    if not _DOCKER_IMAGE_ID_RE.fullmatch(sandbox.image_id):
        raise ValueError("Sandbox 本地镜像 ID 无效")

    version = _run_docker_control(
        sandbox.docker_path,
        ["version", "--format", "{{.Server.Version}}"],
    )
    if version.returncode != 0 or not version.stdout.strip():
        raise ValueError("Docker daemon 不可用，请启动 Docker Desktop")

    inspected_id = _inspect_local_image(sandbox.docker_path, sandbox.image_id)
    if inspected_id != sandbox.image_id:
        raise ValueError("Sandbox 本地镜像已变更，请重新启动 Harness")


def _validate_mount_component(value: str) -> None:
    if any(character in value for character in (",", "\n", "\r", "\x00")):
        raise ValueError("Workspace 路径包含 Docker mount 不支持的字符")


def _find_sensitive_paths(
    workspace: Path, *, dependency_root: Path | None = None,
) -> list[tuple[Path, bool]]:
    root = workspace.resolve()
    sensitive: list[tuple[Path, bool]] = []

    for directory, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        directory_path = Path(directory)
        relative_directory = directory_path.relative_to(root)
        kept_directories: list[str] = []

        for name in directory_names:
            relative = relative_directory / name
            candidate = directory_path / name
            if relative == dependency_root:
                # The verified readonly managed volume covers this exact subtree.
                continue
            if _is_sensitive_path(relative):
                if candidate.is_symlink():
                    raise ValueError("敏感路径不允许是符号链接")
                sensitive.append((relative, True))
                if len(sensitive) > MAX_SANDBOX_MASKS:
                    raise ValueError("Workspace 敏感路径过多，无法安全创建 Sandbox")
            else:
                kept_directories.append(name)

        directory_names[:] = kept_directories

        for name in file_names:
            relative = relative_directory / name
            if not _is_sensitive_path(relative):
                continue

            candidate = directory_path / name
            if candidate.is_symlink():
                raise ValueError("敏感路径不允许是符号链接")
            sensitive.append((relative, False))
            if len(sensitive) > MAX_SANDBOX_MASKS:
                raise ValueError("Workspace 敏感路径过多，无法安全创建 Sandbox")

    sensitive.sort(key=lambda item: item[0].as_posix())
    return sensitive


def _managed_dependency_root(sandbox: DockerSandboxConfig | None) -> Path | None:
    dependency = sandbox.dependency if sandbox is not None else None
    if dependency is not None and dependency.ecosystem == "node":
        return Path(dependency.package_root) / "node_modules"
    return None


def _validate_sandbox_workspace(
    workspace: Path, *, sandbox: DockerSandboxConfig | None = None,
) -> None:
    root = workspace.resolve()
    _validate_mount_component(str(root))
    for relative, _ in _find_sensitive_paths(root, dependency_root=_managed_dependency_root(sandbox)):
        _validate_mount_component(
            f"{_CONTAINER_WORKSPACE}/{relative.as_posix()}"
        )


def _prepare_sensitive_masks(
    workspace: Path,
    mask_root: Path,
    *,
    sandbox: DockerSandboxConfig | None = None,
) -> list[tuple[Path, str]]:
    masks: list[tuple[Path, str]] = []

    for index, (relative, is_directory) in enumerate(
        _find_sensitive_paths(workspace, dependency_root=_managed_dependency_root(sandbox))
    ):
        destination = f"{_CONTAINER_WORKSPACE}/{relative.as_posix()}"
        _validate_mount_component(destination)

        mask = mask_root / f"mask-{index}"
        if is_directory:
            mask.mkdir()
        else:
            mask.write_bytes(b"")

        _validate_mount_component(str(mask))
        masks.append((mask, destination))

    return masks


def _container_workdir(workspace: Path, cwd: str) -> str:
    cwd_path = _resolve_workspace_path(workspace, cwd)
    relative = cwd_path.relative_to(workspace.resolve())
    if relative == Path("."):
        return _CONTAINER_WORKSPACE
    return f"{_CONTAINER_WORKSPACE}/{relative.as_posix()}"


def _numeric_user() -> str:
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return "65534:65534"
    uid = getuid()
    gid = getgid()
    if uid == 0 or gid == 0:
        return "65534:65534"
    return f"{uid}:{gid}"


def _build_docker_argv(
    sandbox: DockerSandboxConfig,
    workspace: Path,
    argv: list[str],
    cwd: str,
    container_name: str,
    masks: list[tuple[Path, str]],
) -> list[str]:
    root = workspace.resolve()
    _validate_mount_component(str(root))
    _validate_dependency_mount(sandbox, workspace)

    command = [
        str(sandbox.docker_path),
        "run",
        "--rm",
        "--name",
        container_name,
        "--pull=never",
        "--log-driver=none",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        "--pids-limit",
        str(SANDBOX_PIDS_LIMIT),
        "--memory",
        SANDBOX_MEMORY,
        "--memory-swap",
        SANDBOX_MEMORY,
        "--cpus",
        SANDBOX_CPUS,
        "--ulimit",
        f"nofile={SANDBOX_NOFILE_LIMIT}:{SANDBOX_NOFILE_LIMIT}",
        "--ipc=none",
        "--init",
        "--user",
        _numeric_user(),
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={SANDBOX_TMPFS_SIZE}",
        "--mount",
        f"type=bind,source={root},target={_CONTAINER_WORKSPACE}",
    ]

    for source, destination in masks:
        command.extend(
            [
                "--mount",
                f"type=bind,source={source},target={destination},readonly",
            ]
        )

    program = argv[0]
    if sandbox.dependency is not None:
        dependency = sandbox.dependency
        package = _container_workdir(workspace, dependency.package_root)
        target = "/opt/gum-env" if dependency.ecosystem == "python" else f"{package}/node_modules"
        command.extend(["--mount", f"type=volume,source={dependency.volume},target={target},readonly,volume-nocopy"])
        if dependency.ecosystem == "python":
            if program in {"python", "python3"}:
                program = "/opt/gum-env/bin/python"
            command.extend([
                "--env", "VIRTUAL_ENV=/opt/gum-env",
                "--env", "PATH=/opt/gum-env/bin:/usr/local/bin:/usr/bin:/bin",
                "--env", f"PYTHONPATH={package}:{package}/src",
                "--env", "PYTHONDONTWRITEBYTECODE=1",
            ])
        else:
            command.extend(["--env", f"PATH={target}/.bin:/usr/local/bin:/usr/bin:/bin"])

    command.extend(
        [
            "--workdir",
            _container_workdir(workspace, cwd),
            "--env",
            "HOME=/tmp",
            "--env",
            "XDG_CONFIG_HOME=/tmp/.config",
            "--env",
            "XDG_CACHE_HOME=/tmp/.cache",
            "--env",
            "XDG_DATA_HOME=/tmp/.local/share",
            "--entrypoint",
            program,
            sandbox.image_id,
            *argv[1:],
        ]
    )
    return command


def _read_pipe(
    name: str,
    stream: BinaryIO,
    events: queue.Queue[tuple[str, bytes | OSError | None]],
    stop: threading.Event,
) -> None:
    read = getattr(stream, "read1", stream.read)
    try:
        while not stop.is_set():
            try:
                chunk = read(_READ_CHUNK_BYTES)
            except (OSError, ValueError):
                chunk = OSError("Docker output pipe read failed")
            if not chunk:
                break
            while not stop.is_set():
                try:
                    events.put((name, chunk), timeout=0.1)
                    break
                except queue.Full:
                    continue
            if isinstance(chunk, OSError):
                break
    finally:
        while not stop.is_set():
            try:
                events.put((name, None), timeout=0.1)
                break
            except queue.Full:
                continue
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    timeout_seconds: int,
    captured: CommandCapture,
    *,
    cancel_event: threading.Event | None = None,
) -> None:
    if process.stdout is None or process.stderr is None:
        raise OSError("Docker output pipes are unavailable")

    events: queue.Queue[tuple[str, bytes | OSError | None]] = queue.Queue(maxsize=8)
    stop = threading.Event()
    # Drain both pipes concurrently to avoid child-process capture deadlocks.
    threads = [
        threading.Thread(
            target=_read_pipe,
            args=(name, stream, events, stop),
            daemon=True,
        )
        for name, stream in (
            ("stdout", process.stdout),
            ("stderr", process.stderr),
        )
    ]
    for thread in threads:
        thread.start()

    finished: set[str] = set()
    deadline = time.monotonic() + timeout_seconds

    try:
        while len(finished) < 2:
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout_seconds)

            try:
                name, chunk = events.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue

            if chunk is None:
                getattr(captured, name).add(b"", final=True)
                finished.add(name)
                continue
            if isinstance(chunk, OSError):
                raise chunk
            getattr(captured, name).add(chunk)

        if cancel_event is None:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        else:
            while process.poll() is None:
                if cancel_event.is_set():
                    raise KeyboardInterrupt
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(process.args, timeout_seconds)
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue
    except BaseException:
        # Stop the pipe writer before closing buffered readers (which may block).
        _stop_process(process)
        stop.set()
        for thread in threads:
            thread.join(timeout=0.2)
        while True:
            try:
                name, chunk = events.get_nowait()
            except queue.Empty:
                break
            if isinstance(chunk, bytes) and name not in finished:
                getattr(captured, name).add(chunk)
        for name in ("stdout", "stderr"):
            if name not in finished:
                getattr(captured, name).add(b"", final=True)
        raise
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=0.2)
        for thread, stream in zip(threads, (process.stdout, process.stderr)):
            # If termination failed, a blocked reader owns its buffered lock.
            # It will close the stream itself when the writer finally releases it.
            if not thread.is_alive():
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


def _remove_container(
    sandbox: DockerSandboxConfig,
    container_name: str,
) -> bool:
    try:
        result = subprocess.run(
            [str(sandbox.docker_path), "rm", "--force", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_DOCKER_CLEANUP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return

    try:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        return


def _abort_container(
    process: subprocess.Popen[bytes],
    sandbox: DockerSandboxConfig,
    container_name: str,
) -> bool:
    removed = _remove_container(sandbox, container_name)
    _stop_process(process)
    return removed


def _run_command(
    workspace: Path,
    argv: list[str],
    cwd: str = ".",
    timeout_seconds: int = 120,
    *,
    sandbox: DockerSandboxConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> CommandExecutionResult:
    """Run validated argv in Docker and return bounded host execution evidence.

    Error cleanup targets only this invocation's randomized container.
    """
    _validate_run_command(
        workspace,
        argv,
        cwd,
        timeout_seconds,
        sandbox=sandbox,
    )
    assert sandbox is not None
    if cancel_event is not None and cancel_event.is_set():
        raise KeyboardInterrupt

    prepared_logs = prepare_command_logs(workspace)
    container_name = f"gum-code-{secrets.token_hex(8)}"
    captured = CommandCapture()

    def finish(
        started: bool | None, exit_code: int | None, reason: str, status: str,
    ) -> CommandExecutionResult:
        incomplete = reason != "exited"
        if incomplete:
            status += "\n[output capture incomplete]"
        saved, truncated = captured.render(status, MAX_COMMAND_LOG_BYTES - 128)
        truncated = truncated or incomplete
        try:
            path = publish_command_log(workspace, prepared_logs, saved, truncated)
        except ValueError:
            raise _CommandLogFailure("命令日志安全检查失败；执行结果未被采纳") from None
        preview, preview_truncated = captured.render(status, COMMAND_PREVIEW_BYTES)
        metadata = {
            "path": path,
            "log_status": ("truncated" if truncated else "complete") if path else "unavailable",
            "exit_code": exit_code,
            "reason": reason,
            "preview_truncated": preview_truncated or incomplete,
        }
        return CommandExecutionResult(
            started, exit_code, reason, command_output_marker(metadata, preview),
        )

    with tempfile.TemporaryDirectory(prefix="gum-code-masks-") as tmpdir:
        masks = _prepare_sensitive_masks(workspace, Path(tmpdir), **({"sandbox": sandbox} if sandbox.dependency is not None else {}))
        docker_argv = _build_docker_argv(
            sandbox,
            workspace,
            argv,
            cwd,
            container_name,
            masks,
        )

        try:
            process = subprocess.Popen(
                docker_argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError:
            return CommandExecutionResult(
                False, None, "not_started", "Error: 无法启动 Docker Sandbox"
            )

        # Abnormal paths remove only this invocation's randomized container.
        try:
            _communicate_bounded(
                process,
                timeout_seconds,
                captured,
                **({"cancel_event": cancel_event} if cancel_event is not None else {}),
            )
        except subprocess.TimeoutExpired:
            cleaned = _abort_container(process, sandbox, container_name)
            suffix = "" if cleaned else "；Sandbox 清理未确认，请手动检查 Docker"
            return finish(None, None, "timeout", f"Error: 命令超时{suffix}")
        except KeyboardInterrupt:
            cleaned = _abort_container(process, sandbox, container_name)
            suffix = "" if cleaned else "；Sandbox 清理未确认，请手动检查 Docker"
            try:
                result = finish(None, None, "interrupted", f"Error: 命令已中断{suffix}")
            except Exception:
                # Diagnostic failures must never convert cancellation into continuation.
                result = CommandExecutionResult(
                    None, None, "interrupted", f"Error: 命令已中断；命令日志未保存{suffix}",
                )
            raise _CommandInterrupted(result) from None
        except OSError:
            cleaned = _abort_container(process, sandbox, container_name)
            suffix = "" if cleaned else "；Sandbox 清理未确认，请手动检查 Docker"
            return finish(
                None, None, "unknown", f"Error: Docker Sandbox 执行失败{suffix}"
            )

        if process.returncode == 125:
            cleaned = _remove_container(sandbox, container_name)
            suffix = "" if cleaned else "；Sandbox 清理未确认，请手动检查 Docker"
            return CommandExecutionResult(
                False, 125, "not_started", f"Error: Docker Sandbox 启动失败{suffix}"
            )

        if process.returncode is None or process.returncode < 0:
            cleaned = _abort_container(process, sandbox, container_name)
            suffix = "" if cleaned else "；Sandbox 清理未确认，请手动检查 Docker"
            return finish(
                None, process.returncode, "unknown",
                f"Error: Docker Sandbox 执行失败{suffix}",
            )

    if process.returncode in (126, 127):
        return CommandExecutionResult(
            False, process.returncode, "not_started",
            "Error: Sandbox 内命令不存在或无法执行",
        )

    return finish(True, process.returncode, "exited", f"exit_code: {process.returncode}")


def run_command(
    workspace: Path,
    argv: list[str],
    cwd: str = ".",
    timeout_seconds: int = 120,
    *,
    sandbox: DockerSandboxConfig | None = None,
    result_callback: CommandResultCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Keep model text separate from optional host-owned execution evidence.

    Observer failures propagate even after a side effect; no successful-looking
    Tool Result is returned when recording execution evidence fails.
    """
    try:
        result = _run_command(
            workspace, argv, cwd, timeout_seconds, sandbox=sandbox,
            **({"cancel_event": cancel_event} if cancel_event is not None else {}),
        )
    except KeyboardInterrupt as exc:
        if result_callback is not None:
            result_callback(
                exc.result if isinstance(exc, _CommandInterrupted)
                else CommandExecutionResult(None, None, "interrupted")
            )
        raise
    except (TypeError, ValueError, OSError) as exc:
        if result_callback is not None:
            result_callback(
                CommandExecutionResult(None, None, "unknown")
                if isinstance(exc, _CommandLogFailure)
                else CommandExecutionResult(False, None, "not_started")
            )
        raise
    except Exception:
        if result_callback is not None:
            result_callback(CommandExecutionResult(None, None, "unknown"))
        raise

    # Outside the runner's error handlers: callback ValueError is not a tool error.
    if result_callback is not None:
        result_callback(result)
    return result.output
