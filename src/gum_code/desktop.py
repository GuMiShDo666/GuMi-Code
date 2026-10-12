"""One-operation JSONL desktop host. No terminal UI or provider payload on stdout."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from queue import Queue, Full, Empty
import signal
import time
from pathlib import Path
import re
import sys
from threading import RLock, Lock, Thread
from uuid import UUID, uuid4
from urllib.parse import urlsplit

from openai import APIConnectionError, APITimeoutError, AuthenticationError, PermissionDeniedError, RateLimitError, BadRequestError

from gum_code.skills import discover_skills
from gum_code.project_rules import ProjectRulesError
from gum_code.events import _command_failure
from gum_code.agent import MAX_STEPS
from gum_code.cli import OneShotHost, _run_one_shot
from gum_code.config import Settings, validate_settings, resolve_context_window
from gum_code.session import SESSION_VERSION, SessionLease, load_session, migrate_session_to_v3

MAX_LINE = 1024 * 1024
MAX_TEXT = 100_000


class DesktopProviderError(ValueError):
    """Only a fixed message may cross the desktop boundary."""


def safe_text(value: object, limit: int = MAX_TEXT, secrets=()) -> str:
    if not isinstance(value, str):
        return ""
    for secret in secrets:
        if isinstance(secret, str) and secret:
            value = value.replace(secret, "<REDACTED>")
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
                   "<REDACTED>", value, flags=re.S)
    value = re.sub(r"(?i)(?:Bearer\s+|\bsk-[A-Za-z0-9_-]{8,})\S*", "<REDACTED>", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "<REDACTED>", value)
    value = re.sub(r'''(?i)(["']?(?:api[_-]?key|token|password|secret|cookie|authorization|session[_-]?key)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''', r"\1<REDACTED>", value)
    value = re.sub(r"(\w+://)[^\s/@]+:[^\s/@]+@", r"\1<REDACTED>@", value)
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)
    return value if len(value) <= limit else value[:limit] + "\n[内容已截断]"


def memory_excerpt(value, secrets=()):
    """Bounded redacted head/tail preserves test footers without storing duplicate logs."""
    text=safe_text(value,140000,secrets)
    return text if len(text)<=1800 else text[:600]+"\n[中间内容省略]\n"+text[-1100:]


def read_message(stream):
    line = stream.readline(MAX_LINE + 1)
    if not line or len(line.encode("utf-8")) > MAX_LINE or not line.endswith("\n"):
        raise ValueError("Invalid protocol input")
    message = json.loads(line)
    if not isinstance(message, dict) or type(message.get("v")) is not int or message["v"] != 1:
        raise ValueError("Invalid protocol version")
    return message


class ProtocolInput:
    """Main-process pipe reader; disconnect cancels a running turn, not a probe."""

    def __init__(self, stream):
        self.stream = stream
        self.lines = Queue(maxsize=16)  # Initial request, up to eight additions, and approval replies.
        self.lock = Lock()
        self.started = self.active = self.eof = False

    def _read(self):
        try:
            while True:
                line = self.stream.readline(MAX_LINE + 1)
                if not line:
                    break
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                self.lines.put_nowait(line)
        except (OSError, ValueError, UnicodeError, Full):
            pass
        with self.lock:
            self.eof = True
            cancel = self.active
            self.active = False
        try:
            self.lines.put_nowait("")
        except Full:
            pass
        if cancel:
            os.kill(os.getpid(), signal.SIGINT)

    def readline(self, limit=-1):
        if not self.started:
            self.started = True
            Thread(target=self._read, name="gum-code-desktop-input", daemon=True).start()
        if self.eof and self.lines.empty():
            return ""
        return self.lines.get()

    def arm(self):
        with self.lock:
            if self.eof:
                raise KeyboardInterrupt()
            self.active = True

    def disarm(self):
        with self.lock:
            self.active = False


class DesktopHost(OneShotHost):
    shared_api = True
    require_current_session = True
    interactive_approvals = True
    persist_explicit_skills = False
    live_input = False

    def __init__(self, source, output):
        self.source, self.output = source, output
        self.session_id = self.turn_id = None
        self.seq = 0
        self.lock = RLock()
        self.status = "failed"
        self.cleanup_error = False
        self.secrets = []
        self.provider = None
        self.total_bytes = 0
        self.workspace = None
        self.tools = {}
        self.tool_counter = 0
        self.pending_inputs = []
        self.input_ids = set()
        self.memory = None

    def queue_input(self, message):
        if message.get("type") == "memory_response" and self.memory is not None:
            return True  # Expired RPC response; never interpret as approval or user input.
        if message.get("type") != "input":
            return False
        value, ident = message.get("text"), message.get("id")
        if (message.get("v") != 1 or message.get("sessionId") != self.session_id
                or message.get("turnId") != self.turn_id or not isinstance(ident, str)
                or not re.fullmatch(r"[a-f0-9-]{36}", ident) or not isinstance(value, str)
                or not value.strip() or len(value) > 16000 or len(self.pending_inputs) >= 8):
            raise ValueError("Invalid live input")
        if ident not in self.input_ids:
            self.input_ids.add(ident)
            self.pending_inputs.append({"id": ident, "text": value})
        return True

    def take_inputs(self):
        with self.lock:
            if isinstance(self.source, ProtocolInput):
                while True:
                    try:
                        line = self.source.lines.get_nowait()
                    except Empty:
                        break
                    if not line:
                        raise KeyboardInterrupt()
                    if not self.queue_input(json.loads(line)):
                        raise ValueError("Unexpected input outside approval")
            result, self.pending_inputs = self.pending_inputs, []
            return result

    def complete(self, client, **kwargs):
        from gum_code.streaming import create_streamed_completion
        memory_context_limit=kwargs.pop("memory_context_limit", None)
        ident, previous, last = str(uuid4()), "", 0.0
        def output(raw, done):
            nonlocal previous, last
            if not raw and not previous:
                return
            if not done and time.monotonic() - last < 0.1:
                return
            # Keep the unfinished token and sensitive assignment line private.
            # Known secrets may span provider chunks; never release their prefix.
            end = len(raw)
            if not done:
                end = max(0, end - max([32, *(len(secret) for secret in self.secrets)]))
                boundary = re.search(r"[A-Za-z0-9_/:?=.+@~%\-]*$", raw[:end])
                end = boundary.start() if boundary else end
                line_start = raw.rfind("\n", 0, end) + 1
                if re.search(r"(?i)(api.?key|token|password|secret|cookie|authorization|private key|bearer)", raw[line_start:]):
                    end = line_start
            text = safe_text(raw[:end], secrets=self.secrets)
            if text == previous and not done:
                return
            replace = not text.startswith(previous)
            self.emit("stream", id=ident, text=text if replace else text[len(previous):], replace=replace, done=done)
            previous, last = text, time.monotonic()
        if self.memory is not None:
            self.memory.prepare(kwargs["messages"], self.memory.query or "", self.memory.counter, memory_context_limit or max(1,self.memory.limit*20))
            kwargs["messages"] = self.memory.messages(kwargs["messages"])
        return create_streamed_completion(client, output, **kwargs)

    def emit(self, kind, **fields):
        with self.lock:
            self.seq += 1
            message = dict(v=1, type=kind, sessionId=self.session_id,
                           turnId=self.turn_id, seq=self.seq, **fields)
            line = json.dumps(message, ensure_ascii=False) + "\n"
            self.total_bytes += len(line.encode("utf-8"))
            if len(line.encode("utf-8")) > MAX_LINE or (self.total_bytes > 7 * MAX_LINE and kind not in {"error", "finished"}):
                raise ValueError("Protocol output budget exhausted")
            self.output.write(line)
            self.output.flush()

    def recover_messages(self, messages):
        from gum_code.session import build_tool_batch_recovery_messages
        return build_tool_batch_recovery_messages(messages, allow_interrupted_commands=True)

    def command_backend(self, args):
        from gum_code.local_command import LocalCommandConfig
        return LocalCommandConfig(args.workspace, lambda value: safe_text(value, 140000, self.secrets))

    def model_secret(self, value):
        if isinstance(value, str) and value and value not in self.secrets:
            self.secrets.append(value)

    def settings(self):
        provider = self.provider
        if not isinstance(provider, dict) or set(provider) - {"baseUrl", "model", "apiKey", "contextWindow"}:
            raise DesktopProviderError()
        for key, maximum in (("baseUrl", 2048), ("model", 256), ("apiKey", 4096)):
            value = provider.get(key)
            if (not isinstance(value, str) or not value.strip() or value != value.strip()
                    or len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise DesktopProviderError()
        try:
            parsed = urlsplit(provider["baseUrl"])
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or "?" in provider["baseUrl"]
                    or "#" in provider["baseUrl"] or "\\" in provider["baseUrl"]
                    or any(char.isspace() for char in provider["baseUrl"])):
                raise ValueError()
            parsed.port
            settings = Settings(api_key=provider["apiKey"], base_url=provider["baseUrl"],
                                model=provider["model"], context_window=provider.get("contextWindow"))
            validate_settings(settings)
        except (ValueError, RuntimeError):
            raise DesktopProviderError() from None
        self.model_secret(settings.api_key)
        return settings

    def context_window(self, settings, client):
        try:
            return resolve_context_window(settings, client)
        except RuntimeError as error:
            if str(error) != "无法确定模型上下文窗口，请设置 LLM_CONTEXT_WINDOW":
                raise
            # User-selected fallback: 1M capacity -> existing 80% input budget.
            return 1_000_000

    def vision_settings(self):
        # A text provider is not evidence of visual capability; never load CLI credentials.
        return None

    def session_messages(self, messages):
        # Persist no known provider secret, even if a remote model echoes it.
        def redact(value):
            if isinstance(value, str):
                for secret in self.secrets:
                    value = value.replace(secret, "<REDACTED>")
                return value
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            return value
        return redact(messages)

    def renderer(self):
        return self

    def paused(self, callback):
        return callback

    def close(self):
        pass

    def __call__(self, event_type, payload):
        self.event(event_type, payload)

    def inline(self, value, limit):
        return safe_text(value, limit, self.secrets).replace("\n", " ").replace("\r", " ")[:limit]

    def tool_detail(self, payload):
        arguments = payload.get("arguments")
        if isinstance(arguments, str) and len(arguments) <= MAX_LINE:
            try:
                arguments = json.loads(arguments)
            except ValueError:
                return ""
        if not isinstance(arguments, dict):
            return ""
        # Only a workspace-relative target; never commands, search queries or content.
        target = arguments.get("path")
        if not isinstance(target, str) or len(target) > 4096 or any(ord(c) < 32 for c in target):
            return ""
        path = Path(target)
        if path.is_absolute():
            if self.workspace is None:
                return ""
            try:
                path = path.relative_to(self.workspace)
            except ValueError:
                return ""
        if ".." in path.parts:
            return ""
        return self.inline(str(path), 400)

    def tool_event(self, event_type, payload):
        name = payload.get("tool_name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
            name = "tool"
        key = (str(payload.get("parent_tool_call_id", ""))[:128], str(payload.get("step", ""))[:32], name)
        queue = self.tools.setdefault(key, [])
        if event_type == "tool_call" or not queue:
            self.tool_counter += 1
            call_id = payload.get("tool_call_id")
            identifier = call_id if isinstance(call_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", call_id) and not any(secret in call_id for secret in self.secrets) else f"tool-{self.tool_counter}"
            queue.append(dict(id=identifier, label=self.inline(name, 64), detail=self.tool_detail(payload), started=False))
        item = next((item for item in queue if item["id"] == payload.get("tool_call_id")), None)
        if item is None:
            item = next((item for item in queue if not item["started"]), queue[0]) if event_type == "tool_execution" else queue[0]
        if event_type == "tool_call":
            return
        if event_type == "tool_execution":
            item["started"] = True
            status = "running"
        else:
            queue.remove(item)
            if not queue:
                self.tools.pop(key, None)
            result = payload.get("result")
            failure = _command_failure(payload)
            failed = isinstance(result, str) and result.startswith("Error:")
            status = "failed" if failed or failure else "completed"
            summary = failure or ("操作未完成或被拒绝" if failed else "工具调用结束")
            item["detail"] = self.inline((item["detail"] + " · " if item["detail"] else "") + summary, 500)
        self.emit("progress", **{key: item[key] for key in ("id", "label", "detail")}, status=status)

    def event(self, event_type, payload):
        if self.memory is not None and self.memory.enabled and event_type == "tool_result" and payload.get("tool_name") in {"run_command", "write_file", "edit_file", "self_check"}:
            value=memory_excerpt(payload.get("result"),self.secrets)
            if value:self.memory.observations.append({"id":payload.get("tool_call_id") or f"observed-tool-{self.tool_counter}","tool":payload.get("tool_name"),"result":value})
            self.memory.observations=self.memory.observations[-5:]
        if self.memory is not None and event_type == "task_outcome" and self.memory.observations:
            verifier=self.memory.local_verification
            verified=verifier is not None and verifier.verified is not None and not verifier.needs_check()
            self.emit("memory_evidence",verified=verified,toolIds=[x["id"] for x in self.memory.observations],observation=json.dumps(self.memory.observations,ensure_ascii=False))
        if event_type == "project_rules":
            self.emit("progress", id="project-rules", label="项目规则",
                      detail="AGENTS.md 读取失败" if payload.get("failed") else "已加载 AGENTS.md" if payload.get("loaded") else "未设置 AGENTS.md",
                      status="failed" if payload.get("failed") else "completed")
            return
        if event_type == "skill_activated":
            name = payload.get("name")
            if isinstance(name, str):
                self.emit("progress", id="skill-" + name[:128], label="使用 Skill",
                          detail=self.inline(name, 128), status="completed")
        elif event_type in {"tool_call", "tool_execution", "tool_result"}:
            with self.lock:
                self.tool_event(event_type, payload)
        elif event_type == "plan_state":
            plans = payload.get("plans")
            plan = next((p for p in plans if isinstance(p, dict) and p.get("id") == payload.get("current_plan_id")), {}) if isinstance(plans, list) else {}
            items = plan.get("items", [])
            statuses = {"pending": "pending", "in_progress": "running", "completed": "completed"}
            self.emit("plan", items=[dict(id=f"plan-{index}", text=self.inline(item.get("content"), 200), status=statuses[item["status"]])
                for index, item in enumerate(items[:20]) if isinstance(item, dict) and item.get("status") in statuses and isinstance(item.get("content"), str)] if isinstance(items, list) else [])
        elif event_type == "task_outcome":
            status = payload.get("status")
            if status in {"completed", "incomplete", "interrupted", "failed"}:
                self.status = status
        elif event_type == "final_answer":
            self.emit("answer", text=safe_text(payload.get("answer"), secrets=self.secrets))
        else:
            labels = {
                "request_prepare": "正在准备请求", "model_call": "正在等待模型响应",
                "context_summary": "正在整理上下文", "context_compacted": "上下文整理完成",
                "subagent_prepare": "正在准备子任务", "subagent_step": "子任务正在等待模型响应",
                "subagent_start": "子任务开始", "subagent_end": "子任务结束",
                "pending_user_discarded": "已恢复会话；上次未完成的输入未自动重试",
                "tool_batch_recovery": "已关闭上次中断的工具批次；未重放操作",
            }
            if event_type in labels:
                self.emit("status", text=labels[event_type])

    def notice(self, text):
        self.emit("status", text=safe_text(text, 500, self.secrets))

    def cleanup_failed(self):
        self.cleanup_error = True
        self.emit("error", message="运行资源清理未完成，请检查 Docker；已发生的文件修改未回滚。")

    def ask(self, title, detail):
        # Serialize callback prompts: stdin has exactly one active request.
        with self.lock:
            request_id = str(uuid4())
            self.emit("approval", requestId=request_id, title=title, detail=detail)
            try:
                reply = read_message(self.source)
                while self.queue_input(reply):
                    reply = read_message(self.source)
                approved = (reply.get("type") == "approval_response"
                            and reply.get("requestId") == request_id
                            and reply.get("approved") is True)
            except (ValueError, OSError):
                approved = False
            self.emit("status", text="已批准本次操作" if approved else "已拒绝本次操作")
            return approved

    def approve(self, tool_name, arguments, reason):
        names = {"web_search": "联网搜索", "read_webpage": "读取公开网页", "delete_file": "删除文件", "run_command": "执行命令", "project_network": "服务访问外网",
                 "project_service": "项目服务操作", "project_browser": "项目浏览器操作"}
        title = names.get(tool_name, "工具操作确认")
        # Deliberately omit raw argv, reasons, paths and metadata (may contain secrets).
        detail = "仅批准本次操作；操作可能修改当前项目，停止不会撤销已有修改。"
        if tool_name in {"web_search", "read_webpage"}:
            value = arguments.get("query" if tool_name == "web_search" else "url", "")
            destination = "搜索词发送给 DuckDuckGo。" if tool_name == "web_search" else "向目标网站请求网页。"
            detail = ("仅批准本次公开网络读取。" + destination + "域名由 Google DNS 解析；不使用登录信息。\n"
                      + safe_text(value, 4096, self.secrets))
        if tool_name == "delete_file" and isinstance(arguments.get("path"), str):
            detail += "\n目标：" + safe_text(arguments["path"], 500, self.secrets)
        if tool_name == "run_command":
            argv = list(arguments.get("argv", []))
            for index in range(1, len(argv)):
                if re.fullmatch(r"--?(?:api[-_]?key|token|password|secret|cookie|authorization|auth)", argv[index - 1], re.I):
                    argv[index] = "<REDACTED>"
            detail = ("在本机执行本次命令，可访问本机文件和网络；工作目录不是安全沙箱。仅批准本次，停止不会撤销修改。"
                      "\n目录：" + safe_text(arguments.get("cwd", "."), 600, self.secrets)
                      + "\n参数：" + safe_text(json.dumps(argv, ensure_ascii=False), 10000, self.secrets))
        if tool_name in {"project_service", "project_browser"}:
            action_labels = {"start": "启动", "status": "查看状态", "logs": "读取日志",
                             "restart": "重启", "stop": "停止", "open": "打开页面",
                             "read": "读取页面", "click": "点击控件", "fill": "填写控件", "assert": "检查页面"}
            action = arguments.get("action")
            if isinstance(action, str) and action in action_labels:
                detail += "\n操作：" + action_labels[action]
            for key, label in (("name", "服务名"), ("service", "服务名")):
                value = arguments.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
                    detail += "\n" + label + "：" + safe_text(value, 40, self.secrets)
            for key, allowed, label in (("kind", {"app", "postgres"}, "类型"),
                                        ("ecosystem", {"python", "node", "prepared"}, "环境"),
                                        ("assertion", {"count", "text", "value", "url", "resource_status", "no_errors"}, "检查类型")):
                value = arguments.get(key)
                if isinstance(value, str) and value in allowed:
                    detail += "\n" + label + "：" + value
            port = arguments.get("port")
            if type(port) is int and 1024 <= port <= 65535:
                detail += f"\n端口：{port}"
            if type(arguments.get("online")) is bool:
                detail += "\n请求联网：" + ("是（另行确认）" if arguments["online"] else "否")
            value = arguments.get("path" if tool_name == "project_browser" else "cwd")
            if isinstance(value, str):
                parsed = urlsplit(value)
                target = parsed.path
                if (not parsed.scheme and not parsed.netloc and ".." not in target.split("/")
                        and (tool_name == "project_browser" or not Path(target).is_absolute())):
                    detail += "\n目标相对路径：" + safe_text(target, 500, self.secrets)
            if action == "fill":
                detail += "\n填写内容不在审批卡片中展示。"
        return self.ask(title, detail)

    def environment(self, summary, *, interactive=False):
        name = {"python": "Python", "node": "Node.js", "browser": "Chromium 浏览器", "postgres": "PostgreSQL 测试数据库"}.get(summary.get("ecosystem"))
        if name is None:
            return False
        return self.ask("准备项目环境", f"为当前项目准备 {name} 环境：可能下载公共镜像及依赖、运行第三方安装代码并占用磁盘空间。")

    def network(self, summary, *, interactive=False):
        return self.ask("服务访问外网", "允许当前服务启动配置访问外网；此许可独立于依赖安装联网许可。")

    def vision(self, runtime, *, interactive=False):
        if self.ask("追加图片解析", "仅追加 1 次图片解析调用，可能产生费用；次数不是金额上限。"):
            if runtime.add_credits(1):
                return
        runtime.decline_extra()


def validate_request(request, host):
    action = request.get("action")
    if action not in {"probe", "skills", "history", "turn", "memory_learn", "memory_source", "migrate_history"}:
        raise ValueError("Invalid action")
    if action in {"probe", "skills"}:
        return action, None, None
    for field in ("sessionId", "turnId"):
        value = request.get(field)
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("Invalid identifier")
    host.session_id, host.turn_id = request["sessionId"], request["turnId"]
    workspace, session = request.get("workspace"), request.get("sessionPath")
    if not isinstance(workspace, str) or not isinstance(session, str):
        raise ValueError("Invalid paths")
    root, path = Path(workspace), Path(session)
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise ValueError("Invalid workspace")
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path or not path.parent.is_dir():
        raise ValueError("Invalid session")
    if action == "turn" and (request.get("trusted") is not True or not isinstance(request.get("text"), str)
                             or not request["text"].strip() or len(request["text"]) > MAX_TEXT):
        raise ValueError("Invalid turn")
    return action, root, path


def run(source, output, *, turn_runner=_run_one_shot):
    host = DesktopHost(source, output)
    code = 1
    try:
        request = read_message(source)
        action, workspace, path = validate_request(request, host)
        host.workspace = workspace
        host.live_input = request.get("live") is True
        if action == "probe":
            host.emit("ready")
            host.status, code = "completed", 0
        elif action == "skills":
            entries = discover_skills()
            if len(entries) > 200:
                raise ValueError("Too many skills")
            host.emit("skills", skills=[dict(name=info.name,
                displayName=safe_text(info.display_name, 120)[:120],
                description=safe_text(info.description, 500)[:500]) for info in entries
                if safe_text(info.name) == info.name])
            host.status, code = "completed", 0
        elif action == "migrate_history":
            # Only private import staging can use this host-only operation.
            if (not path.parent.name.startswith(".workbench-import-")
                    or path.name != host.session_id + ".jsonl"
                    or path.parent.stat().st_mode & 0o777 != 0o700
                    or path.parent.stat().st_uid != os.getuid()):
                raise ValueError("Invalid migration staging")
            data = load_session(path, allow_incomplete_tool_batch=True, allow_pending_user_tail=True)
            if data.workspace.resolve() != workspace:
                raise ValueError("Migration workspace mismatch")
            converted = data.version != SESSION_VERSION
            if converted:
                destination = path.with_name(host.session_id + ".converted.jsonl")
                migrate_session_to_v3(path, destination)
                migrated = load_session(destination, allow_incomplete_tool_batch=True, allow_pending_user_tail=True)
                if (migrated.version != SESSION_VERSION or migrated.workspace.resolve() != workspace
                        or migrated.messages != data.messages):
                    raise ValueError("Migration validation failed")
            host.emit("history_migrated", converted=converted)
            host.status, code = "completed", 0
        elif action == "memory_learn":
            from gum_code.desktop_memory import extract
            host.provider=request.get("provider")
            host.emit("memory_learn", proposals=extract(host,request))
            host.status, code = "completed", 0
        elif action == "memory_source":
            import hashlib
            digest=request.get("digest")
            if not isinstance(digest,str) or not re.fullmatch(r"[a-f0-9]{64}",digest):raise ValueError("Invalid source")
            # Read-only projection; concurrent incomplete writes fail visibly, never replay.
            session=load_session(path,allow_incomplete_tool_batch=True,allow_pending_user_tail=True)
            if session.workspace.resolve()!=workspace:raise ValueError("Source workspace mismatch")
            matches=[dict(role="user",content=safe_text(m["content"])) for m in session.messages
                if m.get("role")=="user" and isinstance(m.get("content"),str)
                and hashlib.sha256(m["content"].encode()).hexdigest()==digest]
            requested=request.get('toolIds',[])
            if not isinstance(requested,list) or len(requested)>20 or not all(isinstance(x,str) and len(x)<=200 for x in requested):raise ValueError('Invalid evidence IDs')
            refs=request.get('evidenceRefs',[])
            if not isinstance(refs,list) or len(refs)>20 or not all(isinstance(x,dict) and isinstance(x.get('id'),str) and isinstance(x.get('tool'),str) and isinstance(x.get('digest'),str) and re.fullmatch(r'[a-f0-9]{64}',x['digest']) for x in refs):raise ValueError('Invalid evidence refs')
            calls={};evidence=[]
            for message in session.messages:
                if message.get('role')=='assistant':
                    for call in message.get('tool_calls',[]):
                        calls[call.get('id')]=call.get('function',{}).get('name','tool')
                ident=message.get('tool_call_id')
                if message.get('role')=='tool' and ident in requested:
                    for content in {memory_excerpt(message.get('content')),safe_text(message.get('content'),1800)}:
                        digest=hashlib.sha256(content.encode()).hexdigest()
                        if any(x['id']==ident and x['tool']==calls.get(ident) and x['digest']==digest for x in refs):
                            item={'toolCallId':ident,'label':calls.get(ident,'tool'),'content':content}
                            if item not in evidence:evidence.append(item)
            host.emit("memory_source",messages=matches[:1],evidence=evidence)
            host.status,code="completed",0
        elif action == "history":
            messages = []
            omitted = False
            with SessionLease(path):
                if path.exists():
                    session = load_session(path, allow_incomplete_tool_batch=True, allow_pending_user_tail=True)
                    if session.version != SESSION_VERSION or session.workspace.resolve() != workspace:
                        raise ValueError("Session mismatch")
                    budget = 120_000
                    visible = [message for message in session.messages
                               if message.get("role") in {"user", "assistant"} and isinstance(message.get("content"), str) and message["content"].strip()]
                    for message in reversed(visible):
                        limit = min(budget, MAX_TEXT)
                        if len(message["content"]) > limit:
                            omitted = True
                        content = safe_text(message["content"], limit)
                        item = dict(role=message["role"], content=content)
                        name = message.get("name", "")
                        if message["role"] == "user" and isinstance(name, str) and re.fullmatch(r"input_[a-f0-9_]{36}", name):
                            item["inputId"] = name[6:].replace("_", "-")
                        messages.append(item)
                        budget -= len(content)
                        if budget <= 0 or len(messages) >= 200:
                            break
                    omitted = omitted or len(messages) < len(visible)
            messages.reverse()
            if omitted:
                messages.insert(0, dict(role="assistant", content="【历史显示提示】仅显示最近的部分对话（最多 200 条、约 12 万字符）；完整历史仍保存在本地会话文件中。"))
            host.emit("history", messages=messages)
            host.status, code = "completed", 0
        else:
            if isinstance(source, ProtocolInput):
                source.arm()
            host.provider = request.get("provider")
            if request.get("memory") is True:
                from gum_code.desktop_memory import DesktopMemory
                host.memory=DesktopMemory(host)
            host.settings()  # Validate before any operation; no local-config fallback.
            args = argparse.Namespace(workspace=workspace, task=request["text"],
                session_file=None if path.exists() else path, resume_session=path if path.exists() else None,
                interactive_terminal=False, allow_run_command=True, sandbox_image=None,
                max_context_tokens=None, max_steps=MAX_STEPS, allow_subagent=True, task_type=None)
            host.emit("status", text="正在启动 GuMi-Code")
            # Libraries may print terminal diagnostics. Only the saved output is wire-visible.
            with open('/dev/null', 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
                turn_runner(args, host=host)
            code = 0 if host.status in {"completed", "incomplete"} else 1
    except KeyboardInterrupt:
        host.status, code = "interrupted", 130
    except Exception as error:
        host.status, code = "failed", 1
        message = "GuMi-Code 操作失败，请检查运行环境、模型配置、工作目录或会话占用后重试。"
        if isinstance(error, ValueError) and str(error) in {"Session 显式 Skills 不能超过 3 个", "Session sticky Skills 不能超过 3 个"}:
            message = "每条消息最多使用 3 个技能，请移除一个后重试。"
        elif isinstance(error, ValueError) and str(error) == "Stream ended before completion":
            message = "模型输出中途结束，已有内容保留，请重试。"
        elif isinstance(error, ProjectRulesError):
            message = str(error)
        elif isinstance(error, DesktopProviderError):
            message = "GuMi-Code API 配置不可用。请在设置中配置有效的 API 地址、模型和密钥，再重试。"
        elif isinstance(error, (APITimeoutError, TimeoutError)):
            message = "模型请求超时，请稍后重试；已发生的文件修改仍保留。"
        elif isinstance(error, (AuthenticationError, PermissionDeniedError)):
            message = "模型鉴权或访问权限失败，请检查当前模型的 API 密钥及访问权限。"
        elif isinstance(error, RateLimitError):
            message = "模型服务限流或额度不足，请检查额度后稍后重试。"
        elif isinstance(error, APIConnectionError):
            message = "无法连接模型服务，请检查 API 地址、网络或代理后重试。"
        elif isinstance(error, RuntimeError) and str(error).startswith("上下文胶囊校验失败"):
            reason = next((label for code, label in {
                "rewritten_active_id": "活动条目被改写", "rewritten_historical_id": "历史条目被改写",
                "invalid_supersession": "替代关系无效", "invalid_verification_source": "验证缺少工具依据",
                "missing_active_id": "遗漏必要条目", "invalid_source": "来源引用无效",
                "invalid_json": "返回格式无效", "capsule_too_large": "状态条目过多",
                "invalid_schema": "摘要字段不符合格式", "invalid_item": "状态字段不符合格式",
                "invalid_id": "条目编号无效", "duplicate_id": "条目编号重复",
                "non_monotonic_id": "条目编号被复用", "invalid_version": "摘要版本无效",
                "invalid_status": "条目状态无效", "invalid_reason": "变更依据无效",
                "invalid_text": "条目内容无效", "missing_current_goal": "缺少当前目标",
                "empty_response": "摘要为空", "invalid_response": "模型响应无效",
            }.items() if f"（{code}）" in str(error)), "摘要内容不符合要求")
            message = f"上下文摘要未通过一致性校验：{reason}。已保留原会话，请重试。"
        elif isinstance(error, RuntimeError) and str(error) in {
            "单个完整上下文批次无法在摘要预算内处理", "上下文胶囊请求无法在当前预算内完成",
            "没有可整理的更早上下文",
        }:
            message = "当前内容无法安全压缩，已保留原会话；请使用更大上下文的模型或新建会话。"
        elif isinstance(error, RuntimeError) and str(error) == "受保护的上下文批次超过当前预算":
            message = "上下文预算不足，必须保留的规则、需求或工具批次已超过当前预算，请调整模型上下文配置后重试。"
        elif (isinstance(error, RuntimeError) and str(error) == "上下文超过当前预算") or (isinstance(error, BadRequestError) and getattr(error, "code", None) in {"context_length_exceeded", "context_window_exceeded"}):
            message = "当前会话超过模型上下文预算，请缩小任务范围或新建会话后继续。"
        elif isinstance(error, RuntimeError) and str(error) == "Session 已被另一个进程打开":
            message = "当前会话正被另一个进程使用，请等待其结束后重试。"
        host.emit("error", message=message)
    if host.cleanup_error:
        host.status, code = "failed", 1
    if isinstance(source, ProtocolInput):
        source.disarm()
    host.emit("finished", status=host.status)
    return code


def main():
    # The daemon must not hold the standard buffered stdin lock at interpreter exit.
    source = ProtocolInput(os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0))
    try:
        return run(source, sys.stdout)
    except (BrokenPipeError, OSError):
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        source.disarm()


if __name__ == "__main__":
    raise SystemExit(main())
