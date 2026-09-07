"""Session-owned Docker services; durable database data never grants execution."""

import copy
import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Callable

from gum_code import project_environment as environment
from gum_code.permissions import check_hard_deny
from gum_code.permissions.policy import PermissionDecision
from gum_code.tools import command
from gum_code.tools.command_logs import CommandCapture, _OMITTED, _open_private_file
from gum_code.tools.common import _resolve_workspace_path
from gum_code.verification import workspace_snapshot

MAX_SERVICES = 8
MAX_SERVICE_LOG_CHARS = 18000
SERVICE_NAME_PATTERN = r"[a-z][a-z0-9-]{0,31}"
SERVICE_LABEL = "io.gum-code.project-service"
SCOPE_LABEL = "io.gum-code.runtime-scope"
OWNER_LABEL = "io.gum-code.runtime-owner"
POSTGRES_IMAGE = "postgres:15"
_PREVIEW_PROGRAM = """import select,socket,socketserver,sys
target=(sys.argv[1],int(sys.argv[2]))
class Handler(socketserver.BaseRequestHandler):
 def handle(self):
  try:
   with socket.create_connection(target,timeout=3) as upstream:
    self.request.settimeout(30);upstream.settimeout(30)
    peers={self.request:upstream,upstream:self.request}
    while True:
     readable,_,_=select.select(list(peers),[],[],30)
     if not readable:return
     for source in readable:
      data=source.recv(65536)
      if not data:return
      peers[source].sendall(data)
  except OSError:return
class Server(socketserver.ThreadingTCPServer):
 allow_reuse_address=True
 daemon_threads=True
Server(('0.0.0.0',8080),Handler).serve_forever()
"""
_ACTIONS = {"start", "status", "logs", "restart", "stop"}
_START_KEYS = {"action", "name", "kind", "argv", "cwd", "port", "ecosystem", "online", "database"}


def _consent(callback, details: dict) -> bool:
    try:
        return callback is not None and callback(details) is True
    except EOFError:
        return False


class ProjectRuntime:
    def __init__(self, workspace: Path, *, session_key: str,
                 command_sandbox: command.DockerSandboxConfig | None = None,
                 environment_approval_callback: Callable[[dict], bool] | None = None,
                 network_approval_callback: Callable[[dict], bool] | None = None):
        self.workspace = Path(workspace).resolve()
        if not isinstance(session_key, str) or not 1 <= len(session_key) <= 4096:
            raise ValueError("项目运行会话标识无效")
        self.scope = environment._digest([str(self.workspace), session_key])
        self.owner = secrets.token_hex(16)
        self.command_sandbox = command_sandbox
        self.environment_approval_callback = environment_approval_callback
        self.network_approval_callback = network_approval_callback
        self.docker_path = command_sandbox.docker_path if command_sandbox else Path(shutil.which("docker") or "/nonexistent")
        self.network = f"gum-code-net-{self.owner}"
        self.egress_network = f"gum-code-out-{self.owner}"
        self.preview_network = f"gum-code-preview-{self.owner}"
        self.services: dict[str, dict] = {}
        self._networks: set[str] = set()
        self._preparations: set[str] = set()
        self._environment_cleanup: set[str] = set()
        self._lease = None
        self._fd = None
        self._state = {"version": 1, "databases": {}}
        self._secrets: set[str] = set()
        self._closed = False
        self._lock = threading.RLock()
        self.internal_paths: tuple[Path, ...] = ()
        self._start_failures: dict[str, dict] = {}

    def validate(self, arguments: dict) -> dict:
        """No Docker or storage side effects; handler repeats validation after gates."""
        if not isinstance(arguments, dict) or not isinstance(arguments.get("action"), str) or arguments["action"] not in _ACTIONS:
            raise ValueError("项目服务操作无效")
        action = arguments["action"]
        if set(arguments) - (_START_KEYS if action == "start" else {"action", "name"}):
            raise ValueError("项目服务参数无效")
        name = arguments.get("name")
        if name is None and action == "status":
            return {"action": action}
        if not isinstance(name, str) or not re.fullmatch(SERVICE_NAME_PATTERN, name):
            raise ValueError("服务名必须是 1–32 个小写字母、数字或连字符，以字母开头")
        if action != "start":
            return {"action": action, "name": name}
        if self._closed:
            raise ValueError("项目运行环境已关闭")
        kind = arguments.get("kind", "app")
        if not isinstance(kind, str) or kind not in {"app", "postgres"} or type(arguments.get("online", False)) is not bool:
            raise ValueError("项目服务类型或网络选项无效")
        if kind == "postgres":
            if set(arguments) - {"action", "name", "kind"}:
                raise ValueError("测试数据库不接受命令、端口、网络或凭据参数")
            return {"action": action, "name": name, "kind": kind}
        argv = arguments.get("argv")
        cwd = arguments.get("cwd", ".")
        ecosystem = arguments.get("ecosystem", "prepared")
        port = arguments.get("port")
        database = arguments.get("database")
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
                or any(not isinstance(arg, str) or not arg or len(arg) > 4096 or "\x00" in arg for arg in argv)
                or not isinstance(cwd, str) or not isinstance(ecosystem, str) or ecosystem not in {"python", "node", "prepared"}):
            raise ValueError("项目服务命令或环境无效")
        command._validate_program(argv)
        if port is not None and (type(port) is not int or not 1024 <= port <= 65535):
            raise ValueError("应用端口必须在 1024–65535 之间")
        if database is not None and (not isinstance(database, str) or not re.fullmatch(SERVICE_NAME_PATTERN, database)):
            raise ValueError("测试数据库服务名无效")
        path = _resolve_workspace_path(self.workspace, cwd)
        if not path.is_dir():
            raise ValueError("服务工作目录不存在")
        if command._launches_shell(argv) or command.find_shell_syntax_violation(argv) is not None:
            raise ValueError("项目服务必须使用单个 argv 程序，不支持 Shell")
        command._validate_argument_paths(self.workspace, path, argv)
        denied = check_hard_deny("run_command", {"argv": argv, "cwd": cwd})
        if denied is not None and denied.decision is PermissionDecision.DENY:
            raise ValueError("项目服务命令被安全策略拒绝")
        return {"action": action, "name": name, "kind": kind, "argv": list(argv),
                "cwd": path.relative_to(self.workspace).as_posix(), "ecosystem": ecosystem,
                "port": port, "online": arguments.get("online", False), "database": database}

    def approved_restart(self, arguments: dict) -> bool:
        """Only the same runtime's original immutable launch can reuse consent."""
        if self._closed:
            return False
        try:
            args = self.validate(arguments)
            item = self.services.get(args.get("name"))
            if args["action"] != "restart" or item is None or not item.get("approved"):
                return False
            return self._launch_identity(item["config"], item["sandbox"]) == item["identity"]
        except (ValueError, OSError):
            return False

    def execute(self, arguments: dict, *, approved: bool = False, cancel_event=None) -> str:
        """Registry passes host-only approval after Hook and in-flight persistence."""
        with self._lock:
            args = self.validate(arguments)
            environment._cancel(cancel_event)
            action, name = args["action"], args.get("name")
            if self._closed and action in {"start", "restart"}:
                raise ValueError("项目运行环境已关闭")
            if action in {"start", "stop", "restart"} and approved is not True and not self.approved_restart(args):
                raise ValueError("项目服务操作需要批准")
            if action == "start":
                result = self._attempt_start(args, cancel_event)
            elif action == "status":
                result = self._status(name) if name else {"services": [self._status(key) for key in self.services]}
                for status in [result] if name else result["services"]:
                    if status.get("status") in {"exited", "dead"}:
                        self._remember_failure(self.services[status["name"]]["config"])
            elif action == "logs":
                item = self._item(name)
                if item.get("container") and self._status(name, probe=False).get("status") in {"exited", "dead"}:
                    self._remember_failure(item["config"])
                text, truncated = item["capture"].render("service output", MAX_SERVICE_LOG_CHARS)
                text = self._redact(text, truncated=truncated, service_logs=True)
                truncated = truncated or len(text) > MAX_SERVICE_LOG_CHARS
                result = {"name": name, "output": text[:MAX_SERVICE_LOG_CHARS], "truncated": truncated,
                          "capture_complete": item["capture_done"].is_set(), "capture_error": item.get("capture_error", False)}
                if item.get("config"):
                    failure = self._start_failures.get(self._failure_key(item["config"]))
                    if failure is not None and text and not result["capture_error"]:
                        digest = environment._digest([text])
                        failure["diagnosed"] = digest != failure.get("diagnosis")
                        failure["diagnosis"] = digest
                        failure["port_conflict"] = bool(re.search(r"address already in use|eaddrinuse", text, re.I))
            elif action == "stop":
                result = self._stop(name)
            else:
                item = self._item(name)
                config = copy.deepcopy(item["config"])
                reuse = self.approved_restart(args)
                if not reuse and approved is not True:
                    raise ValueError("服务配置已变化，需要重新批准")
                item["reuse_environment"] = reuse
                self._stop(name)
                result = self._attempt_start(config, cancel_event)
            if self._environment_cleanup or self._preparations:
                result["cleanup_required"] = sorted(self._environment_cleanup | self._preparations)
            return json.dumps(result, ensure_ascii=False)

    def _snapshot(self) -> dict | None:
        try:
            return workspace_snapshot(self.workspace, internal_paths=self.internal_paths)
        except (OSError, ValueError, RuntimeError):
            return None

    @staticmethod
    def _failure_key(config: dict) -> str:
        # Names and equivalent port substitutions are not a new executable.
        value = {key: data for key, data in config.items() if key not in {"name", "port", "action"}}
        if config.get("port"):
            port = str(config["port"])
            value["argv"] = ["<port>" if arg == port else arg.rsplit("=", 1)[0] + "=<port>"
                             if "=" in arg and arg.rsplit("=", 1)[1] == port else arg
                             for arg in config.get("argv", [])]
        return environment._digest(value)

    def _remember_failure(self, config: dict) -> None:
        key = self._failure_key(config)
        item = self.services.get(config["name"], {})
        snapshot = item.get("source_snapshot") if item.get("container") else self._snapshot()
        signature = environment._digest(snapshot) if snapshot is not None else None
        previous = self._start_failures.get(key, {})
        self._start_failures[key] = {**previous, "source": signature, "diagnosed": False,
                                     "port": config.get("port")}
        # ponytail: bounded recent launch failures; use durable history only if a
        # single invocation needs more than the existing service configuration cap.
        if len(self._start_failures) > MAX_SERVICES:
            del self._start_failures[next(iter(self._start_failures))]

    def _attempt_start(self, config: dict, cancel_event) -> dict:
        if self.services.get(config["name"], {}).get("container"):
            raise ValueError("该服务已登记；请使用 restart 或先 stop")
        key = self._failure_key(config)
        previous = self._start_failures.get(key)
        snapshot = self._snapshot()
        source = environment._digest(snapshot) if snapshot is not None else None
        if previous and previous["source"] == source:
            port_changed = previous["port"] != config.get("port")
            if not previous.get("diagnosed") or (port_changed and not previous.get("port_conflict")):
                raise ValueError("同一服务启动失败后必须先读取状态/日志并修正原因；换名称或无关端口不能重试")
            previous["diagnosed"] = False
        try:
            result = self._start(config, cancel_event)
        except (ValueError, OSError, subprocess.SubprocessError):
            self._remember_failure(config)
            raise
        if result.get("status") in {"exited", "dead"} or result.get("running") is False:
            self._remember_failure(config)
        else:
            self._start_failures.pop(key, None)
        return result

    def _item(self, name: str) -> dict:
        if name not in self.services:
            raise ValueError("当前会话尚未登记该服务")
        return self.services[name]

    def _control(self, argv: list[str], *, limit: int = 32768, timeout: int = 15,
                 cancel_event=None, allow_failure: bool = False) -> tuple[int, str]:
        capture = CommandCapture()
        process = subprocess.Popen([str(self.docker_path), *argv], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            command._communicate_bounded(process, timeout, capture, cancel_event=cancel_event)
        except (OSError, subprocess.SubprocessError):
            command._stop_process(process)
            raise ValueError("Docker 项目操作中断或超时") from None
        except BaseException:
            command._stop_process(process)
            raise
        if capture.stdout.total_bytes > limit:
            raise ValueError("Docker 项目状态超过读取上限")
        if process.returncode and not allow_failure:
            raise ValueError("Docker 项目操作失败，请检查运行环境")
        return process.returncode, capture.stdout.render(limit)

    def _labels(self, *, owner: bool = True) -> list[str]:
        values = [f"{SERVICE_LABEL}=v1", f"{SCOPE_LABEL}={self.scope}"]
        if owner:
            values.append(f"{OWNER_LABEL}={self.owner}")
        return [part for value in values for part in ("--label", value)]

    def _initialize(self) -> None:
        if self._fd is not None:
            return
        if not self.docker_path.is_file():
            raise ValueError("Docker 不可用，请先启动 Docker")
        self._control(["version", "--format", "{{.Server.Version}}"])
        lease = environment._lease(self.workspace, self.scope, namespace="project-runtimes")
        fd = lease.__enter__()
        try:
            _, leftovers = self._control(["container", "ls", "--all", "--filter", f"label={SCOPE_LABEL}={self.scope}", "--format", "{{.Names}}"])
            if leftovers.strip():
                names = [name for name in leftovers.splitlines() if re.fullmatch(r"gum-code-svc-[0-9a-f]{32}", name)]
                raise ValueError("发现本会话遗留服务；需要检查并清理后重新运行：" + ", ".join(names[:MAX_SERVICES]))
            try:
                state_fd = _open_private_file(fd, self.scope + ".json")
            except FileNotFoundError:
                state = {"version": 1, "databases": {}}
            else:
                try:
                    raw = os.read(state_fd, 16385)
                finally:
                    os.close(state_fd)
                if len(raw) > 16384:
                    raise ValueError("项目运行状态无效")
                state = json.loads(raw)
            if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("databases"), dict) or len(state["databases"]) > MAX_SERVICES:
                raise ValueError("项目运行状态无效")
            for name, value in state["databases"].items():
                if (not re.fullmatch(SERVICE_NAME_PATTERN, name) or not isinstance(value, dict)
                        or not re.fullmatch(r"gum-code-db-[0-9a-f]{32}", value.get("volume", ""))
                        or not re.fullmatch(r"[0-9a-f]{64}", value.get("password", ""))
                        or not command._DOCKER_IMAGE_ID_RE.fullmatch(value.get("image", ""))):
                    raise ValueError("项目运行状态无效")
                self._secrets.add(value["password"])
            self._state = state
            self._lease, self._fd = lease, fd
        except BaseException:
            lease.__exit__(None, None, None)
            raise

    def _network(self, online: bool = False, *, preview: bool = False) -> str:
        name = self.preview_network if preview else self.egress_network if online else self.network
        if name not in self._networks:
            self._networks.add(name)
            self._control(["network", "create", *([] if online or preview else ["--internal"]), *self._labels(), name])
        return name

    def _owned(self, kind: str, name: str, *, owner: bool = True) -> bool:
        template = "{{json .Config.Labels}}" if kind == "container" else "{{json .Labels}}"
        code, raw = self._control([kind, "inspect", "--format", template, name], allow_failure=True)
        if code:
            return False
        labels = json.loads(raw)
        return (isinstance(labels, dict) and labels.get(SERVICE_LABEL) == "v1"
                and labels.get(SCOPE_LABEL) == self.scope and (not owner or labels.get(OWNER_LABEL) == self.owner))

    def _launch_identity(self, config: dict, sandbox: command.DockerSandboxConfig) -> str:
        value = [config, str(self.workspace), str(sandbox.docker_path), sandbox.image_id,
                 sandbox.dependency.__dict__ if sandbox.dependency else None]
        if config["kind"] == "app" and config["ecosystem"] != "prepared":
            project = environment.discover_project(self.workspace, config["cwd"], ecosystem=config["ecosystem"])
            if project.reason:
                raise ValueError("项目依赖配置不受支持")
            value.append(project.identity)
        value.append([(str(path), directory) for path, directory in command._find_sensitive_paths(self.workspace, dependency_root=command._managed_dependency_root(sandbox))] if config["kind"] == "app" else [])
        return environment._digest(value)

    def _image(self, image: str, cancel_event) -> command.DockerSandboxConfig:
        try:
            image_id = command._inspect_local_image(self.docker_path, image)
        except ValueError:
            callback = self.environment_approval_callback
            detail = {"missing": "project_runtime_image", "ecosystem": "postgres" if image == POSTGRES_IMAGE else "python", "runtime": image,
                      "public_source": "Docker Hub", "downloads": True, "disk_usage": True, "dependency_scripts": False}
            if not _consent(callback, detail):
                raise ValueError("项目运行镜像未准备，未获下载许可") from None
            sandbox = command.DockerSandboxConfig(self.docker_path, "sha256:" + "0" * 64)
            environment._run_step(sandbox, ["pull", image], cancel_event=cancel_event)
            image_id = command._inspect_local_image(self.docker_path, image)
        return command.DockerSandboxConfig(self.docker_path, image_id)

    def _database(self, name: str, cancel_event) -> tuple[command.DockerSandboxConfig, dict]:
        info = self._state["databases"].get(name)
        if info is not None:
            if not self._owned("volume", info["volume"], owner=False):
                raise ValueError("测试数据库数据卷丢失或归属不符，不会自动重建")
            return command.DockerSandboxConfig(self.docker_path, command._inspect_local_image(self.docker_path, info["image"])), info
        if len(self._state["databases"]) >= MAX_SERVICES:
            raise ValueError("当前会话测试数据库数量达到上限")
        sandbox = self._image(POSTGRES_IMAGE, cancel_event)
        info = {"volume": "gum-code-db-" + secrets.token_hex(16), "password": secrets.token_hex(32), "image": sandbox.image_id}
        self._secrets.add(info["password"])
        # Save the identity first. A failed create must never cause silent data replacement.
        self._state["databases"][name] = info
        environment._save_record(self._fd, self.scope, self._state)
        self._control(["volume", "create", *self._labels(owner=False), info["volume"]], cancel_event=cancel_event)
        if not self._owned("volume", info["volume"], owner=False):
            raise ValueError("测试数据库数据卷创建未确认")
        preparation = "gum-code-svc-" + secrets.token_hex(16)
        self._preparations.add(preparation)
        try:
            try:
                self._control(["run", "--rm", "--name", preparation, *self._labels(), "--pull=never", "--network=none",
                               "--read-only", "--user=0:0", "--cap-drop=ALL", "--cap-add=CHOWN", "--security-opt=no-new-privileges=true",
                               "--mount", f"type=volume,source={info['volume']},target=/data", "--entrypoint=chown", sandbox.image_id,
                               "999:999", "/data"], cancel_event=cancel_event)
            finally:
                self._cleanup_preparation(preparation)
        except BaseException as exc:
            if preparation in self._preparations:
                cleanup = tuple(dict.fromkeys((*getattr(exc, "cleanup_required", ()), preparation)))
                if isinstance(exc, KeyboardInterrupt):
                    raise environment.EnvironmentInterrupted(cleanup) from None
                raise environment._Unavailable("测试数据库准备失败；需检查资源：" + preparation,
                                               cleanup_required=cleanup) from None
            raise
        if preparation in self._preparations:
            raise environment._Unavailable("测试数据库准备清理未确认：" + preparation,
                                           cleanup_required=(preparation,))
        return sandbox, info

    def _cleanup_preparation(self, name: str) -> bool:
        """Retry only a registered preparation; unknown ownership never permits deletion."""
        try:
            if self._owned("container", name):
                self._control(["rm", "--force", name])
            elif not environment._absent_resource(command.DockerSandboxConfig(self.docker_path, "sha256:" + "0" * 64), "container", name):
                return False
        except (ValueError, OSError, subprocess.SubprocessError):
            return False
        self._preparations.discard(name)
        return True

    def _start(self, config: dict, cancel_event) -> dict:
        name = config["name"]
        if name in self.services and self.services[name].get("container"):
            raise ValueError("该服务已登记；请使用 restart 或先 stop")
        if name not in self.services and len(self.services) >= MAX_SERVICES:
            raise ValueError("当前会话最多运行 8 个服务")
        self._initialize()
        online = config.get("online", False)
        old = self.services.get(name)
        stack = ExitStack()
        container = "gum-code-svc-" + secrets.token_hex(16)
        item = None
        try:
            if config["kind"] == "postgres":
                sandbox, database = self._database(name, cancel_event)
            elif config["ecosystem"] == "prepared":
                if self.command_sandbox is None:
                    raise ValueError("预配服务需要本次指定可信 Docker 镜像")
                sandbox = self.command_sandbox
            else:
                project = environment.discover_project(self.workspace, config["cwd"], ecosystem=config["ecosystem"])
                result = stack.enter_context(environment.ensure_environment(self.workspace, project,
                    command_sandbox=old["sandbox"] if old and old.get("reuse_environment") else None,
                    approval_callback=self.environment_approval_callback, cancel_event=cancel_event))
                self._environment_cleanup.update(result.cleanup_required)
                if result.status != "ready" or result.sandbox is None:
                    detail = "；需检查资源：" + ", ".join(result.cleanup_required) if result.cleanup_required else ""
                    raise environment._Unavailable("项目服务环境不可用：" + result.reason + detail,
                                                   cleanup_required=result.cleanup_required)
                sandbox = result.sandbox
            command._validate_docker_sandbox(sandbox)
            identity = self._launch_identity(config, sandbox)
            if online and not (old and old.get("network_approved") and old["identity"] == identity):
                callback = self.network_approval_callback
                if not _consent(callback, {"service": name, "network": "external", "image_id": sandbox.image_id,
                                           "scope": "current_session_launch_configuration"}):
                    raise ValueError("项目运行期联网未获批准")
            network = self._network()
            temp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="gum-code-service-", dir=Path("/tmp").resolve())))
            if temp.is_relative_to(self.workspace):
                raise ValueError("项目服务私有目录不能位于工作区")
            if config["kind"] == "app":
                command._validate_run_command(self.workspace, config["argv"], config["cwd"], sandbox=sandbox)
                masks = command._prepare_sensitive_masks(self.workspace, temp, sandbox=sandbox)
                argv = command._build_docker_argv(sandbox, self.workspace, config["argv"], config["cwd"], container, masks)[1:]
                argv[0] = "create"
                argv.remove("--rm")
                argv[argv.index("--network=none")] = "--network=" + network
                insert = argv.index("--entrypoint")
                options = [*self._labels(), "--network-alias", name]
                if config["port"] is not None:
                    _, version = self._control(["version", "--format", "{{.Server.Version}}"])
                    if not re.match(r"\d+\.", version) or int(version.split(".")[0]) < 28:
                        raise ValueError("localhost 预览需要 Docker 28 或更新版本")
                if config["database"]:
                    db_item = self._item(config["database"])
                    if db_item["config"]["kind"] != "postgres" or not self._status(config["database"])["running"]:
                        raise ValueError("关联测试数据库尚未运行")
                    db = self._state["databases"][config["database"]]
                    env_file = temp / "application.env"
                    with env_file.open("x", encoding="utf-8") as stream:
                        os.chmod(env_file, 0o600)
                        stream.write(f"DATABASE_URL=postgresql://gum_code:{db['password']}@{config['database']}:5432/gum_code\n")
                    options.extend(["--env-file", str(env_file)])
                argv[insert:insert] = options
            else:
                env_file = temp / "database.env"
                with env_file.open("x", encoding="utf-8") as stream:
                    os.chmod(env_file, 0o600)
                    stream.write(f"POSTGRES_USER=gum_code\nPOSTGRES_DB=gum_code\nPOSTGRES_PASSWORD={database['password']}\nPGDATA=/var/lib/postgresql/data/pgdata\n")
                argv = ["create", "--name", container, *self._labels(), "--pull=never", "--network", network, "--network-alias", name,
                        "--log-driver=none", "--read-only", "--user=999:999", "--cap-drop=ALL", "--security-opt=no-new-privileges=true",
                        "--pids-limit=128", "--memory=512m", "--memory-swap=512m", "--cpus=1", "--init", "--shm-size=64m",
                        "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777", "--tmpfs", "/var/run/postgresql:rw,nosuid,nodev,size=16m,mode=1777",
                        "--mount", f"type=volume,source={database['volume']},target=/var/lib/postgresql/data",
                        "--env-file", str(env_file), sandbox.image_id, "postgres"]
            item = {"config": copy.deepcopy(config), "sandbox": sandbox, "container": container, "stack": stack,
                    "identity": identity, "approved": True, "network_approved": online,
                    "source_snapshot": self._snapshot(),
                    "capture": CommandCapture(), "capture_done": threading.Event(), "threads": [], "process": None, "preview": None}
            self.services[name] = item
            self._control(argv, cancel_event=cancel_event)
            if not self._owned("container", container):
                raise ValueError("项目服务容器归属无法确认")
            if online:
                self._control(["network", "connect", self._network(True), container], cancel_event=cancel_event)
            process = subprocess.Popen([str(self.docker_path), "start", "--attach", container], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            item["process"] = process
            self._capture(item)
            if config.get("port"):
                self._start_preview(item, cancel_event)
            # Starting is distinct from protocol readiness. status performs fresh probes.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                environment._cancel(cancel_event)
                result = self._status(name)
                if result["running"] or process.poll() is not None:
                    return result
                time.sleep(0.05)
            return self._status(name)
        except BaseException as exc:
            self._environment_cleanup.update(resource for resource in getattr(exc, "cleanup_required", ())
                if re.fullmatch(r"gum-code-(?:prepare|env)-[0-9a-f]{32}", resource))
            if item is not None:
                try:
                    self._stop(name)
                except (ValueError, OSError, subprocess.SubprocessError):
                    cleanup = tuple(dict.fromkeys((*getattr(exc, "cleanup_required", ()),
                                                   *([item["preview"]] if item.get("preview") else []), container)))
                    if isinstance(exc, KeyboardInterrupt):
                        raise environment.EnvironmentInterrupted(cleanup) from None
                    raise environment._Unavailable("项目服务启动失败且清理未确认：" + ", ".join(cleanup),
                                                   cleanup_required=cleanup) from None
            else:
                stack.close()
            raise

    def _start_preview(self, item: dict, cancel_event) -> None:
        # Internal Docker networks do not publish ports. Only this fixed-target
        # relay joins a publishable bridge; project code gets no additional route.
        sandbox = self._image("python:3.11-slim", cancel_event)
        name = "gum-code-svc-" + secrets.token_hex(16)
        item["preview"] = name
        self._control(["create", "--name", name, *self._labels(), "--pull=never", "--network", self.network,
                       "--publish", "127.0.0.1::8080/tcp", "--log-driver=none", "--read-only", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges=true", "--user", command._numeric_user(), "--pids-limit=64",
                       "--memory=64m", "--memory-swap=64m", "--cpus=0.5", "--init", "--entrypoint=python",
                       sandbox.image_id, "-I", "-c", _PREVIEW_PROGRAM, item["config"]["name"], str(item["config"]["port"])], cancel_event=cancel_event)
        self._control(["network", "connect", self._network(preview=True), name], cancel_event=cancel_event)
        self._control(["start", name], cancel_event=cancel_event)

    def _capture(self, item: dict) -> None:
        finished = set()
        lock = threading.Lock()

        def read(name, stream):
            try:
                while chunk := stream.read1(8192):
                    getattr(item["capture"], name).add(chunk)
            except (OSError, ValueError):
                item["capture_error"] = True
            finally:
                stream.close()
                getattr(item["capture"], name).add(b"", final=True)
                with lock:
                    finished.add(name)
                    if len(finished) == 2:
                        item["capture_done"].set()
        for name in ("stdout", "stderr"):
            thread = threading.Thread(target=read, args=(name, getattr(item["process"], name)), daemon=True)
            item["threads"].append(thread)
            thread.start()

    def _status(self, name: str, *, probe: bool = True) -> dict:
        item = self._item(name)
        result = {"name": name, "kind": item["config"]["kind"], "running": False, "ready": False,
                  "status": "stopped", "exit_code": item.get("exit_code"), "url": None, "origin": None}
        container = item.get("container")
        if not container:
            return result
        if not self._owned("container", container):
            return {**result, "status": "unknown", "reason": "container_missing_or_changed"}
        _, raw = self._control(["inspect", "--format", '{"state":{{json .State}},"ports":{{json .NetworkSettings.Ports}}}', container])
        info = json.loads(raw)
        state = info["state"]
        result.update(running=state["Running"], status=state["Status"], exit_code=None if state["Running"] else state["ExitCode"])
        if not result["running"]:
            return result
        config = item["config"]
        if config["kind"] == "postgres":
            if probe:
                code, _ = self._control(["exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "gum_code", "-d", "gum_code"], allow_failure=True)
                result["ready"] = code == 0
        elif config["port"]:
            preview = item.get("preview")
            if not preview or not self._owned("container", preview):
                return {**result, "status": "unknown", "reason": "preview_unavailable"}
            _, raw = self._control(["inspect", "--format", "{{json .NetworkSettings.Ports}}", preview])
            mappings = (json.loads(raw) or {}).get("8080/tcp") or []
            if len(mappings) != 1 or mappings[0].get("HostIp") != "127.0.0.1":
                return {**result, "status": "unknown", "reason": "unsafe_port_binding"}
            port = int(mappings[0]["HostPort"])
            result["url"] = f"http://127.0.0.1:{port}"
            result["origin"] = f"http://{name}:{config['port']}"
            if not probe:
                return result
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
            try:
                connection.request("HEAD", "/")
                response = connection.getresponse()
                result["ready"] = True
                result["http_status"] = response.status
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            result["readiness"] = "http_response_not_business_validation"
        return result

    def _stop(self, name: str) -> dict:
        item = self._item(name)
        preview = item.get("preview")
        if preview:
            if self._owned("container", preview):
                self._control(["rm", "--force", preview])
            elif not environment._absent_resource(item["sandbox"], "container", preview):
                raise ValueError("本机预览归属无法确认：" + preview)
            item["preview"] = None
        container = item.get("container")
        if container:
            if self._owned("container", container):
                self._control(["stop", "--time", "5", container], timeout=10)
                status = self._status(name, probe=False)
                item["exit_code"] = status["exit_code"]
                self._control(["rm", container])
            elif not environment._absent_resource(item["sandbox"], "container", container):
                raise ValueError("项目服务归属无法确认：" + container)
            item["container"] = None
        if item.get("process") is not None:
            command._stop_process(item["process"])
        for thread in item["threads"]:
            thread.join(timeout=0.5)
        item["capture_done"].set()
        item["stack"].close()
        return self._status(name)

    def _redact(self, text: str, *, truncated: bool = False, service_logs: bool = False) -> str:
        ends = (_OMITTED,) if truncated else ()
        starts = (_OMITTED,) if truncated else ()
        if service_logs:
            # CommandCapture's fixed framing bounds each stream. Even without
            # omission, its current tail can end midway through a credential.
            ends += ("\nstderr:\n",)
            starts += ("service output\nstdout:\n", "\nstderr:\n")
        for secret in self._secrets:
            text = text.replace(secret, "<REDACTED>")
            if truncated or service_logs:
                # Longest first also handles secrets with repeated prefixes.
                for size in range(len(secret) - 1, 0, -1):
                    for boundary in ends:
                        text = text.replace(secret[:size] + boundary, "<REDACTED>" + boundary)
                    for boundary in starts:
                        text = text.replace(boundary + secret[-size:], boundary + "<REDACTED>")
                    if service_logs and text.endswith(secret[:size]):
                        text = text[:-size] + "<REDACTED>"
        return re.sub(r"(postgres(?:ql)?://)[^\s/@]*:[^\s/@]*@", r"\1<REDACTED>@", text)

    def browser_scope(self) -> dict:
        with self._lock:
            origins = {}
            for name in self.services:
                status = self._status(name, probe=False)
                if status["running"] and status["origin"]:
                    origins[name] = status["origin"]
            return {"docker_path": self.docker_path, "network": self.network, "origins": origins}

    def browser_evidence(self, service: str, input_version: str, paths: list[str]) -> dict:
        """Bind checks to a real launch; a fresh digest never updates an old process."""
        with self._lock:
            item = self.services.get(service)
            if not item or not item.get("approved"):
                return {"current": False, "reason": "service_unapproved"}
            config = item["config"]
            try:
                status = self._status(service, probe=False)
                identity_current = self._launch_identity(config, item["sandbox"]) == item["identity"]
            except (ValueError, OSError, subprocess.SubprocessError):
                return {"current": False, "reason": "service_unavailable"}
            if not status.get("running") or not status.get("origin") or not identity_current:
                return {"current": False, "reason": "service_unavailable_or_changed"}
            current, started = self._snapshot(), item.get("source_snapshot")
            document_root = self._static_root(config)
            facts = {"current": False, "reason": "service_source_changed", "cwd": config["cwd"],
                     "document_root": document_root, "static": document_root is not None,
                     "service_identity": environment._digest([item["container"], item["identity"]]),
                     "input_version": input_version,
                     "source_version": environment._digest(current) if current is not None else None,
                     "start_version": environment._digest(started) if started is not None else None}
            if current is None or started is None:
                facts["reason"] = "service_source_unknown"
                return facts
            if document_root is not None:
                try:
                    root = _resolve_workspace_path(self.workspace, document_root)
                    # Static proof only applies to files actually served from this root.
                    if any(not _resolve_workspace_path(self.workspace, path).is_relative_to(root) for path in paths):
                        facts["reason"] = "static_scope_mismatch"
                        return facts
                except ValueError:
                    facts["reason"] = "static_scope_mismatch"
                    return facts
            elif current != started:
                return facts
            related = {}
            # ponytail: conservatively check all live apps, including a frontend's
            # proxy backends; add dependency scoping only if restart cost warrants it.
            for name, other in self.services.items():
                if name == service or other["config"]["kind"] != "app" or not other.get("container"):
                    continue
                try:
                    running = self._status(name, probe=False).get("running") is True
                    unchanged = self._launch_identity(other["config"], other["sandbox"]) == other["identity"]
                except (ValueError, OSError, subprocess.SubprocessError):
                    running = unchanged = False
                snapshot = other.get("source_snapshot")
                if (not running or not unchanged or snapshot is None
                        or (self._static_root(other["config"]) is None and snapshot != current)):
                    facts["reason"] = "related_service_source_changed"
                    return facts
                related[name] = environment._digest([other["container"], other["identity"]])
            facts["related_services"] = related
            facts["service_identity"] = environment._digest([item["container"], item["identity"], related])
            facts.update(current=True, reason="current")
            return facts

    def _static_root(self, config: dict) -> str | None:
        """Recognize only stdlib static serving; other processes require restart."""
        argv = config.get("argv", [])
        if (len(argv) < 3 or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", argv[0])
                or argv[1:3] != ["-m", "http.server"]):
            return None
        # A local module can replace stdlib http.server under python -m.
        cwd = self.workspace / config["cwd"]
        if any((cwd / path).exists() for path in ("http", "http.py", "sitecustomize.py", "usercustomize.py")):
            return None
        directory, index = config["cwd"], 3
        while index < len(argv):
            arg = argv[index]
            if arg in {"-d", "--directory", "--bind", "-b", "--protocol", "-p"} and index + 1 < len(argv):
                if arg in {"-d", "--directory"}:
                    directory = (Path(config["cwd"]) / argv[index + 1]).as_posix()
                index += 2
            elif arg.startswith("--directory="):
                directory = (Path(config["cwd"]) / arg.split("=", 1)[1]).as_posix()
                index += 1
            elif arg.isdecimal() or arg.startswith(("--bind=", "--protocol=")):
                index += 1
            else:
                return None
        try:
            return _resolve_workspace_path(self.workspace, directory).relative_to(self.workspace).as_posix()
        except ValueError:
            return None

    def has_active_services(self) -> bool:
        """Conservative local hint: Docker disconnect is not evidence of shutdown."""
        return any(item.get("container") for item in self.services.values())

    def close(self) -> tuple[str, ...]:
        """Stop only this invocation's resources. Database/dependency volumes survive."""
        with self._lock:
            failures = []
            for name in tuple(self._preparations):
                if not self._cleanup_preparation(name):
                    failures.append(name)
            for name in tuple(self._environment_cleanup):
                kind = "container" if re.fullmatch(r"gum-code-prepare-[0-9a-f]{32}", name) else "volume"
                sandbox = command.DockerSandboxConfig(self.docker_path, "sha256:" + "0" * 64)
                if environment._absent_resource(sandbox, kind, name):
                    self._environment_cleanup.remove(name)
                else:
                    failures.append(name)
            for name in reversed(self.services):
                try:
                    self._stop(name)
                except (ValueError, OSError, subprocess.SubprocessError):
                    failures.append(self.services[name].get("container") or name)
            for network in tuple(self._networks):
                try:
                    if not self._owned("network", network):
                        control = command.DockerSandboxConfig(self.docker_path, "sha256:" + "0" * 64)
                        if not environment._absent_resource(control, "network", network):
                            raise ValueError("项目网络归属无法确认")
                    else:
                        self._control(["network", "rm", network])
                    self._networks.remove(network)
                except (ValueError, OSError, subprocess.SubprocessError):
                    failures.append(network)
            if not failures and self._lease is not None:
                self._lease.__exit__(None, None, None)
                self._lease, self._fd = None, None
            self._closed = True
            return tuple(failures)
