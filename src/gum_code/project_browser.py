"""Bounded current-project browser actions in an optional Docker environment."""

import hashlib
import json
import os
import queue
import re
import secrets
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from gum_code.tools.command import DockerSandboxConfig, _inspect_local_image, _stop_process, _run_docker_control
from gum_code.project_environment import _absent_resource
from gum_code.tools.common import _reject_symlinks, _resolve_workspace_path

BROWSER_VERSION = "1.58.0"
BROWSER_BASE = f"mcr.microsoft.com/playwright/python:v{BROWSER_VERSION}-noble"
BROWSER_IMAGE = f"gum-code-browser:{BROWSER_VERSION}"
MAX_BROWSER_RESULT = 32_000
MAX_BROWSER_WIRE = 256_000
MAX_BROWSER_VALUE = 4_000
BROWSER_TIMEOUT = 30
MAX_SCENARIO_BYTES = 32_000
MAX_SCENARIO_STEPS = 32

# The worker has no workspace mount, arbitrary script API or user browser profile.
_WORKER = r'''
import json, sys
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

allowed = set()
errors = []
resources = {}
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
def response_result(response):
    if response.status >= 400 and len(errors) < 12: errors.append("http_error")
    if len(resources) < 128: resources[response.url] = response.status
def assert_result(request, page):
    kind, expected = request["assertion"], request["expected"]
    if kind in {"count", "text", "value"}:
        target = page.locator(request["selector"])
        if kind == "count": expect(target).to_have_count(expected)
        elif kind == "text": expect(target).to_have_text(expected)
        else: expect(target).to_have_value(expected)
    elif kind == "url":
        expect(page).to_have_url(origin(page.url) + expected)
    elif kind == "resource_status":
        if resources.get(origin(page.url) + request["path"]) != expected: raise AssertionError()
    elif kind == "no_errors":
        if errors: raise AssertionError()
    else: raise ValueError()
    return {"assertion":kind,"passed":True}
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
    context = browser.new_context(accept_downloads=False, service_workers="block",
        extra_http_headers={"Cache-Control":"no-cache"})
    context.route("**/*", route)
    context.route_web_socket("**/*", lambda socket: socket.close())
    page = context.new_page()
    page.set_default_timeout(10000)
    page.on("popup", lambda popup: popup.close())
    page.on("dialog", lambda dialog: dialog.dismiss())
    page.on("pageerror", lambda error: errors.append("page_script_error") if len(errors) < 12 else None)
    page.on("console", lambda message: errors.append("page_console_error") if message.type == "error" and len(errors) < 12 else None)
    page.on("response", response_result)
    page.on("requestfailed", lambda request: errors.append("request_failed") if len(errors) < 12 else None)
    emit({"status":"ready"})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            allowed = set(request["origins"])
            action = request["action"]
            assertion = None
            if action == "open":
                errors.clear()
                resources.clear()
                if origin(request["url"]) not in allowed: raise ValueError()
                page.goto(request["url"], wait_until="load")
            else:
                if origin(page.url) not in allowed: raise ValueError()
                if action == "click": page.locator(request["selector"]).click()
                elif action == "fill": page.locator(request["selector"]).fill(request["value"])
                elif action == "assert": assertion = assert_result(request, page)
                elif action != "read": raise ValueError()
            if origin(page.url) not in allowed: raise ValueError()
            text = page.locator("body").inner_text(timeout=5000)
            controls = page.locator("input,button,textarea,select,a").evaluate_all(
                "els => els.slice(0,60).map(e => ({tag:e.tagName.toLowerCase(),id:e.id,name:e.getAttribute('name')||'',type:e.getAttribute('type')||'',text:e.innerText||''}))")
            emit({"status":"asserted" if assertion else "observed","assertion":assertion,"url":page.url,"text":text,
                  "truncated":False,"controls":controls,"errors":errors[:12],
                  "coverage":"nonvisual_assertion" if assertion else "interaction_observation_not_test_pass"})
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

    def validate(self, arguments: dict) -> None:
        if not isinstance(arguments, dict) or arguments.keys() - {
            "action", "service", "path", "selector", "value", "assertion", "expected",
        }:
            raise ValueError("Invalid browser arguments")
        action = arguments.get("action")
        if action not in {"open", "read", "click", "fill", "assert"}:
            raise ValueError("Invalid browser action")
        keys = {"open": {"service", "path"}, "read": set(), "click": {"selector"},
                "fill": {"selector", "value"}, "assert": {"assertion", "expected", "selector", "path"}}
        if arguments.keys() - {"action"} - keys[action]:
            raise ValueError("Unexpected browser action fields")
        if action == "open":
            service = arguments.get("service")
            if not isinstance(service, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", service):
                raise ValueError("Invalid project service")
        if action == "open" or (action == "assert" and arguments.get("assertion") in {"url", "resource_status"}):
            path = arguments.get("expected") if arguments.get("assertion") == "url" else arguments.get("path", "/")
            if (not isinstance(path, str) or len(path) > 2000 or not path.startswith("/")
                    or path.startswith("//") or "\\" in path
                    or any(ord(c) < 32 for c in path) or urlsplit(path).netloc):
                raise ValueError("Browser path must stay in the project")
        if action in {"click", "fill"} or (action == "assert" and arguments.get("assertion") in {"count", "text", "value"}):
            selector = arguments.get("selector")
            if not isinstance(selector, str) or not selector.strip() or len(selector) > 500:
                raise ValueError("Invalid browser selector")
            if action == "assert" and re.search(r":(?:visible|hidden)\b", selector):
                raise ValueError("Visual assertions are excluded")
        if action == "fill":
            value = arguments.get("value")
            if not isinstance(value, str) or len(value) > MAX_BROWSER_VALUE:
                raise ValueError("Invalid browser value")
        if action == "assert":
            kind, expected = arguments.get("assertion"), arguments.get("expected")
            if kind not in {"count", "text", "value", "url", "resource_status", "no_errors"}:
                raise ValueError("Invalid nonvisual assertion")
            extra = {"selector"} if kind in {"count", "text", "value"} else {"path"} if kind == "resource_status" else set()
            if arguments.keys() - {"action", "assertion", "expected"} - extra:
                raise ValueError("Unexpected assertion fields")
            if kind in {"count", "resource_status"}:
                low, high = (0, 10000) if kind == "count" else (100, 599)
                if type(expected) is not int or not low <= expected <= high:
                    raise ValueError("Invalid assertion expected count/status")
            elif kind == "no_errors":
                if expected is not True:
                    raise ValueError("no_errors expects true")
            elif not isinstance(expected, str) or len(expected) > MAX_BROWSER_VALUE:
                raise ValueError("Invalid assertion expected text")

    def load_scenario(self, workspace: Path, test_file: str) -> tuple[dict, str]:
        """Read the retained test, never executable code or an arbitrary file URL."""
        path = _resolve_workspace_path(workspace, test_file)
        _reject_symlinks(workspace, test_file)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SCENARIO_BYTES:
                raise ValueError("Browser scenario must be a bounded regular JSON file")
            content = stream.read(MAX_SCENARIO_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("Browser scenario changed while reading")
        if len(content) > MAX_SCENARIO_BYTES:
            raise ValueError("Browser scenario exceeds limit")
        scenario = json.loads(content)
        if not isinstance(scenario, dict) or set(scenario) != {"path", "steps"}:
            raise ValueError("Browser scenario requires path and steps")
        self.validate({"action": "open", "service": "scenario", "path": scenario["path"]})
        steps = scenario["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_SCENARIO_STEPS:
            raise ValueError("Browser scenario requires 1–32 steps")
        for step in steps:
            self.validate(step)
            if step["action"] == "open":
                raise ValueError("Browser scenario has one service-bound entry path")
        if not any(step["action"] == "assert" for step in steps):
            raise ValueError("Browser scenario requires a nonvisual assertion")
        return scenario, hashlib.sha256(content).hexdigest()

    def execute_check(self, workspace: Path, check: dict, input_version: str, *, action_runner,
                      service_evidence: dict, cancel_event=None) -> tuple[str, dict]:
        """Replay bounded retained steps through the registry's ordinary tool gates."""
        facts = {"started": False, "reason": "browser_unverified", "assertions": [],
                 "input_version": input_version, "service_identity": service_evidence.get("service_identity"),
                 "service": service_evidence, "visual": "excluded"}
        if service_evidence.get("current") is not True:
            facts["reason"] = service_evidence.get("reason", "service_unverified")
            return json.dumps(facts, ensure_ascii=False), facts
        try:
            scenario, digest = self.load_scenario(workspace, check["test_file"])
        except (OSError, ValueError, TypeError, KeyError):
            facts["reason"] = "invalid_browser_scenario"
            return json.dumps(facts, ensure_ascii=False), facts
        facts["scenario_version"] = digest
        steps = [{"action": "open", "service": check["service"], "path": scenario["path"]}, *scenario["steps"]]
        for index, step in enumerate(steps):
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt
            output = action_runner(step)
            try:
                result = json.loads(output.removeprefix("Error: 浏览器检查失败 "))
            except (OSError, ValueError, TypeError, AttributeError):
                result = {}
            if not isinstance(result, dict):
                result = {}
            facts["started"] = facts["started"] or result.get("status") in {"observed", "asserted", "failed"}
            assertion = result.get("assertion")
            if step["action"] == "assert":
                passed = (result.get("status") == "asserted" and isinstance(assertion, dict)
                          and assertion.get("assertion") == step["assertion"] and assertion.get("passed") is True)
                facts["assertions"].append({"step": index, "assertion": step["assertion"], "passed": passed})
            else:
                passed = result.get("status") == "observed"
            if not passed or result.get("errors"):
                facts["reason"] = "browser_check_failed"
                facts["failed_step"] = index
                return json.dumps(facts, ensure_ascii=False), facts
        try:
            _, current = self.load_scenario(workspace, check["test_file"])
        except (OSError, ValueError, TypeError):
            current = None
        facts["reason"] = "exited" if current == digest else "scenario_changed"
        return json.dumps(facts, ensure_ascii=False), facts

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
