"""Bounded current-project browser actions in an optional Docker environment."""

import json
import queue
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from gum_code.tools.command import DockerSandboxConfig, _inspect_local_image, _stop_process, _run_docker_control
from gum_code.project_environment import _absent_resource

BROWSER_VERSION = "1.58.0"
BROWSER_BASE = f"mcr.microsoft.com/playwright/python:v{BROWSER_VERSION}-noble"
BROWSER_IMAGE = f"gum-code-browser:{BROWSER_VERSION}"
MAX_BROWSER_RESULT = 32_000
MAX_BROWSER_WIRE = 256_000
MAX_BROWSER_VALUE = 4_000
BROWSER_TIMEOUT = 30

# The worker has no workspace mount, arbitrary script API or user browser profile.
_WORKER = r'''
import json, sys
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

allowed = set()
errors = []
def origin(url):
    p = urlsplit(url)
    return p.scheme + "://" + p.netloc
def route(request):
    if origin(request.request.url) in allowed:
        request.continue_()
    else:
        if len(errors) < 12: errors.append("blocked_external_request")
        request.abort()
def emit(value):
    wire = json.dumps(value, ensure_ascii=True) + "\n"
    if len(wire) > 256000:
        wire = json.dumps({"status":"failed","reason":"browser_output_limit"}) + "\n"
    sys.stdout.write(wire)
    sys.stdout.flush()
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
    context = browser.new_context(accept_downloads=False, service_workers="block")
    context.route("**/*", route)
    context.route_web_socket("**/*", lambda socket: socket.close())
    page = context.new_page()
    page.set_default_timeout(10000)
    page.on("popup", lambda popup: popup.close())
    page.on("dialog", lambda dialog: dialog.dismiss())
    page.on("pageerror", lambda error: errors.append("page_script_error") if len(errors) < 12 else None)
    page.on("console", lambda message: errors.append("page_console_error") if message.type == "error" and len(errors) < 12 else None)
    page.on("response", lambda response: errors.append("http_error") if response.status >= 400 and len(errors) < 12 else None)
    page.on("requestfailed", lambda request: errors.append("request_failed") if len(errors) < 12 else None)
    emit({"status":"ready"})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            allowed = set(request["origins"])
            errors.clear()
            action = request["action"]
            if action == "open":
                if origin(request["url"]) not in allowed: raise ValueError()
                page.goto(request["url"], wait_until="domcontentloaded")
            else:
                if origin(page.url) not in allowed: raise ValueError()
                if action == "click": page.locator(request["selector"]).click()
                elif action == "fill": page.locator(request["selector"]).fill(request["value"])
                elif action != "read": raise ValueError()
            if origin(page.url) not in allowed: raise ValueError()
            text = page.locator("body").inner_text(timeout=5000)
            controls = page.locator("input,button,textarea,select,a").evaluate_all(
                "els => els.slice(0,60).map(e => ({tag:e.tagName.toLowerCase(),id:e.id,name:e.getAttribute('name')||'',type:e.getAttribute('type')||'',text:e.innerText||''}))")
            emit({"status":"observed","url":page.url,"text":text,
                  "truncated":False,"controls":controls,"errors":errors[:12],
                  "coverage":"interaction_observation_not_test_pass"})
        except Exception:
            emit({"status":"failed","reason":"browser_action_failed","errors":errors[:12]})
    context.close()
    browser.close()
'''


class ProjectBrowser:
    """One disposable browser per live project network; grants stay in memory."""

    def __init__(self, scope: Callable[[], dict], *, approval_callback=None, redactor=None):
        self.scope = scope
        self.approval_callback = approval_callback
        self.redactor = redactor or (lambda text: text)
        self.process = None
        self.container_name = None
        self.docker_path = None
        self.network = None
        self.image_id = "sha256:" + "0" * 64
        self.denied = False
        self.replies = queue.Queue(maxsize=2)
        self.lock = threading.Lock()
        self.begin_request()

    def begin_request(self) -> None:
        self.denied = False
        self.request_attempted = False
        self.request_failed = False

    def note_result(self, result: str) -> None:
        self.request_attempted = True
        try:
            value = json.loads(result)
            failed = not isinstance(value, dict) or value.get("status") != "observed" or bool(value.get("errors"))
        except (TypeError, ValueError):
            failed = True
        self.request_failed = self.request_failed or failed

    def completion_notice(self) -> str | None:
        if not self.request_attempted:
            return None
        if self.request_failed:
            return "浏览器检查存在失败或未完成项；后续成功操作不能证明这些缺口已全部解决。"
        return "浏览器已执行本轮操作并返回观测结果；这不代表所有业务流程均已验证。"

    def validate(self, arguments: dict) -> None:
        if not isinstance(arguments, dict) or arguments.keys() - {
            "action", "service", "path", "selector", "value",
        }:
            raise ValueError("Invalid browser arguments")
        action = arguments.get("action")
        if action not in {"open", "read", "click", "fill"}:
            raise ValueError("Invalid browser action")
        if action == "open":
            service = arguments.get("service")
            if not isinstance(service, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", service):
                raise ValueError("Invalid project service")
            path = arguments.get("path", "/")
            if (not isinstance(path, str) or len(path) > 2000 or not path.startswith("/")
                    or path.startswith("//") or "\\" in path
                    or any(ord(c) < 32 for c in path) or urlsplit(path).netloc):
                raise ValueError("Browser path must stay in the project")
        if action in {"click", "fill"}:
            selector = arguments.get("selector")
            if not isinstance(selector, str) or not selector.strip() or len(selector) > 500:
                raise ValueError("Invalid browser selector")
        if action == "fill":
            value = arguments.get("value")
            if not isinstance(value, str) or len(value) > MAX_BROWSER_VALUE:
                raise ValueError("Invalid browser value")

    def _prepare(self, docker: Path, cancel_event=None) -> str | None:
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt
        try:
            return _inspect_local_image(docker, BROWSER_IMAGE)
        except ValueError:
            pass
        if self.denied:
            return None
        if self.approval_callback is None or self.approval_callback({
            "ecosystem": "browser", "runtime": BROWSER_VERSION,
            "missing": "runtime_unavailable",
        }) is not True:
            self.denied = True
            return None
        dockerfile = (
            f"FROM {BROWSER_BASE}\n"
            f"RUN python -m pip install --no-cache-dir playwright=={BROWSER_VERSION}\n"
        )
        process = None
        try:
            process = subprocess.Popen(
                [str(docker), "build", "--pull=false", "-t", BROWSER_IMAGE, "-"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 300
            payload = dockerfile.encode()
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise KeyboardInterrupt
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(process.args, 300)
                try:
                    process.communicate(input=payload, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    payload = None
        except (OSError, subprocess.TimeoutExpired):
            self.denied = True
            return None
        finally:
            if process is not None and process.poll() is None:
                _stop_process(process)
        if process.returncode != 0:
            self.denied = True
            return None
        try:
            return _inspect_local_image(docker, BROWSER_IMAGE)
        except ValueError:
            return None

    def _receive(self, cancel_event=None) -> dict:
        deadline = time.monotonic() + BROWSER_TIMEOUT
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt
            try:
                line = self.replies.get(timeout=0.1)
            except queue.Empty:
                if self.process is None or self.process.poll() is not None:
                    break
                continue
            if not line:
                break
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                break
            if isinstance(value, dict):
                return value
            break
        raise ValueError("Browser unavailable or action timed out")

    def _start(self, scope: dict, cancel_event=None) -> bool:
        docker = Path(scope["docker_path"])
        image = self._prepare(docker, cancel_event)
        if not image:
            return False
        self.docker_path = docker
        self.image_id = image
        self.network = scope["network"]
        self.container_name = "gum-code-browser-" + secrets.token_hex(16)
        args = [str(docker), "run", "--rm", "-i", "--name", self.container_name,
                "--pull=never", "--log-driver=none", "--read-only", "--init",
                "--label", "io.gum-code.project-browser=" + self.container_name,
                "--user", "1000:1000", "--cap-drop=ALL", "--security-opt=no-new-privileges=true",
                "--memory", "1g", "--memory-swap", "1g", "--cpus", "1", "--pids-limit", "256",
                "--network", self.network, "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
                "--env", "HOME=/tmp", "--entrypoint", "python", image, "-u", "-c", _WORKER]
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
        output = self.process.stdout
        replies = self.replies

        def receive_lines():
            while True:
                line = output.readline(MAX_BROWSER_WIRE + 1)
                if len(line) > MAX_BROWSER_WIRE:
                    line = b""
                try:
                    replies.put_nowait(line)
                except queue.Full:
                    return
                if not line:
                    return

        threading.Thread(target=receive_lines, daemon=True).start()
        return self._receive(cancel_event).get("status") == "ready"

    def execute(self, arguments: dict, *, cancel_event=None) -> str:
        self.validate(arguments)
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt
        with self.lock:
            scope = self.scope()
            origins = scope.get("origins", {})
            if not origins:
                return "Error: 浏览器检查未完成：没有运行中的当前项目网页服务"
            if self.network is not None and self.network != scope.get("network"):
                if self.close():
                    return "Error: 浏览器清理未完成"
            if self.process is None and self.container_name is not None:
                if self.close():
                    return "Error: 浏览器资源仍待清理，不能启动另一个浏览器"
            try:
                if self.process is None and not self._start(scope, cancel_event):
                    cleanup = self.close()
                    return "Error: 浏览器检查未完成：环境不可用或未获准备批准" + ("；资源清理未完成" if cleanup else "")
                request = dict(arguments, origins=list(origins.values()))
                if arguments["action"] == "open":
                    target = origins.get(arguments["service"])
                    if not target:
                        return "Error: Browser service is not running"
                    request["url"] = target + arguments.get("path", "/")
                payload = json.dumps(request, ensure_ascii=True).encode() + b"\n"
                if cancel_event is not None and cancel_event.is_set():
                    raise KeyboardInterrupt
                self.process.stdin.write(payload)
                self.process.stdin.flush()
                result = self._receive(cancel_event)
                # Redact complete fields before clipping; clipping first can expose
                # an unrecognizable prefix of a generated database credential.
                result = json.loads(self.redactor(json.dumps(result, ensure_ascii=False)))
                for key, limit in (("text", 12000), ("url", 2000)):
                    if key in result:
                        result["truncated"] = bool(result.get("truncated")) or len(result[key]) > limit
                        result[key] = result[key][:limit]
                for control in result.get("controls", []):
                    for key, limit in (("id", 100), ("name", 100), ("type", 30), ("text", 150)):
                        if key in control:
                            result["truncated"] = bool(result.get("truncated")) or len(control[key]) > limit
                            control[key] = control[key][:limit]
                rendered = json.dumps(result, ensure_ascii=False)
                if len(rendered) > MAX_BROWSER_RESULT:
                    raise ValueError("Browser result limit")
                return ("Error: 浏览器检查失败 " if result.get("status") == "failed" else "") + rendered
            except (OSError, ValueError):
                cleanup = self.close()
                return "Error: 浏览器检查未完成" + ("；浏览器资源清理未完成" if cleanup else "")
            except BaseException:
                self.close()
                raise

    def close(self) -> tuple[str, ...]:
        """Stop only this instance's random container; never remove an image/volume."""
        failed = ()
        if self.container_name is not None and self.docker_path is not None:
            try:
                sandbox = DockerSandboxConfig(self.docker_path, self.image_id)
                if not _absent_resource(sandbox, "container", self.container_name):
                    labels = _run_docker_control(self.docker_path, [
                        "inspect", "--format", "{{json .Config.Labels}}", self.container_name,
                    ])
                    info = json.loads(labels.stdout) if labels.returncode == 0 and len(labels.stdout) < 8192 else {}
                    if not isinstance(info, dict) or info.get("io.gum-code.project-browser") != self.container_name:
                        raise ValueError("Browser ownership unavailable")
                    result = subprocess.run(
                        [str(self.docker_path), "rm", "-f", self.container_name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                    )
                    if result.returncode != 0 and not _absent_resource(sandbox, "container", self.container_name):
                        failed = (self.container_name,)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                failed = (self.container_name,)
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                failed = (self.container_name or "browser",)
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    stream.close()
        self.process = None
        if not failed:
            self.container_name = None
            self.network = None
        self.replies = queue.Queue(maxsize=2)
        return failed
