"""Render structured Agent events with bounded, sanitized terminal text."""

import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from threading import RLock
from typing import ParamSpec, TypeVar

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme

MAX_PREVIEW = 1000
_ANSWER_THEME = Theme({"markdown.code": "bold"}, inherit=False)
_READ_PROGRESS_TOOLS = frozenset({
    "list_files", "read_file", "read_document", "analyze_image", "find_files",
    "search_text", "git_status", "git_diff", "read_tool_output", "find_skills",
    "load_skill", "read_skill_resource",
})
_WRITE_PROGRESS_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})
_THINKING_EVENTS = frozenset({
    "request_prepare", "subagent_prepare", "model_call", "subagent_step",
    "context_summary",
})
_P = ParamSpec("_P")
_R = TypeVar("_R")

AgentEventCallback = Callable[
    [str, dict[str, object]],
    None,
]


def deduplicate_tool_execution(
    callback: AgentEventCallback,
) -> AgentEventCallback:
    """Hide adjacent same-tool progress within one model step only."""
    last_key: tuple[object, object] | None = None

    def emit(event_type: str, payload: dict[str, object]) -> None:
        nonlocal last_key
        if event_type == "tool_execution":
            key = (payload.get("step"), payload.get("tool_name"))
            if key == last_key:
                return
            last_key = key
        callback(event_type, payload)

    return emit


def context_percentage(used_tokens: object, max_tokens: object) -> int:
    if (
        isinstance(used_tokens, bool)
        or not isinstance(used_tokens, int)
        or used_tokens < 0
        or isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens <= 0
    ):
        raise ValueError("上下文用量必须是有效的非负整数")
    return (used_tokens * 100 + max_tokens // 2) // max_tokens


def _usage_text(payload: dict[str, object]) -> str | None:
    try:
        percent = context_percentage(payload.get("used_tokens"), payload.get("max_tokens"))
    except ValueError:
        return None
    return f"上下文占用：{percent}%"


def _compacted_text(payload: dict[str, object]) -> str | None:
    try:
        before = context_percentage(payload.get("before_tokens"), payload.get("max_tokens"))
        after = context_percentage(payload.get("after_tokens"), payload.get("max_tokens"))
    except ValueError:
        return None
    return f"上下文占用：{before}% → {after}%"


def _safe_inline(value: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(value))


def _safe_multiline(value: object) -> str:
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(value))


def _bounded_inline(value: object) -> str:
    text = _safe_inline(value)
    return text if len(text) <= MAX_PREVIEW else text[:MAX_PREVIEW] + "…"


def _command_failure(payload: dict[str, object]) -> str | None:
    """Show fixed failure notices without printing private command previews."""
    content = payload.get("result")
    if payload.get("tool_name") != "run_command" or not isinstance(content, str):
        return None
    if not content.startswith("<command-output>\n") or "\n</command-output>" not in content:
        return None
    header = content.partition("\n")[2].partition("\n")[0]
    if len(header) > 1_024:
        return None
    try:
        metadata = json.loads(header)
    except ValueError:
        return None
    if not isinstance(metadata, dict) or not isinstance(metadata.get("reason"), str):
        return None
    reason = metadata["reason"]
    if reason == "exited":
        code = metadata.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool) and code != 0:
            return "命令非零退出；尚未通过检查。"
        return None
    if metadata.get("backend") == "local":
        return {"timeout": "本机命令超时；进程已停止，尚未通过检查。",
                "output_limit": "本机命令输出超过上限；进程已停止，尚未通过检查。",
                "interrupted": "本机命令已中断。", "unknown": "本机命令状态未确认。"}.get(reason)
    return {
        "timeout": "命令超时；请检查 Sandbox 是否已清理。",
        "interrupted": "命令已中断；请检查 Sandbox 是否已清理。",
        "unknown": "命令执行状态未确认；请检查 Sandbox。",
    }.get(reason)


def create_console() -> Console:
    return Console(no_color=os.getenv("NO_COLOR") is not None)


def _document_notice(payload: dict[str, object]) -> str | None:
    """Only fixed status text reaches progress, even for malformed/private results."""
    result = payload.get("result")
    if not isinstance(result, str) or len(result) > 21_000:
        return "文档处理状态未确认。"
    if result.startswith("Error:"):
        return "文档处理失败，请检查输入或可用权限。"
    try:
        # Agent can append its Todo reminder after the bounded JSON object.
        state, _ = json.JSONDecoder().raw_decode(result)
    except ValueError:
        return "文档处理状态未确认。"
    if not isinstance(state, dict):
        return "文档处理状态未确认。"
    status = state.get("status")
    if status == "ok":
        return None
    reason = state.get("reason")
    if isinstance(reason, str):
        message = {
            "vision_budget_exhausted": "本轮视觉调用额度已用完，保留已读取部分。",
            "vision_unavailable": "视觉功能未配置或未启用，可继续读取文字。",
            "vision_disabled": "视觉功能已关闭，可继续读取文字。",
            "vision_configuration_changed": "视觉配置已变更，请在下一轮重新使用。",
            "source_changed": "源文件已变化，请重新读取。",
            "timeout": "文档或视觉处理超时，保留已读取部分。",
            "vision_timeout": "视觉处理超时，保留已读取部分。",
            "cancelled": "文档或视觉处理已取消。",
            "vision_state_unavailable": "视觉额度状态不可用，本轮停止外发。",
        }.get(reason)
        if message is not None:
            return message
    return ("已读取可用部分，仍有内容未解析。" if status == "partial"
            else "文档或视觉处理不可用，请查看缺失说明。")


def task_state_text(payload: dict[str, object]) -> str | None:
    """Render bounded check identities and status, never arguments or logs."""
    status = payload.get("status")
    if not isinstance(status, str):
        return None
    if status == "completed" and payload.get("requires_verification") is False:
        return "任务已完成（问答/只读，无构建验收）。"
    if status == "completed":
        checks = payload.get("checks")
        labels = {
            "passed": "通过", "failed": "失败", "pending": "待执行",
            "stale": "需重测", "not_started": "未启动",
        }
        summary = []
        for check in checks[:8] if isinstance(checks, list) else []:
            if not isinstance(check, dict):
                continue
            check_id, check_status = check.get("id"), check.get("status")
            if (
                isinstance(check_id, str)
                and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", check_id)
                and isinstance(check_status, str) and check_status in labels
            ):
                summary.append(f"{check_id}：{labels[check_status]}")
        return "阶段验收通过。" + (" 检查：" + "；".join(summary) if summary else "")
    return {
        "incomplete": "阶段未完成，尚未满足验收条件。",
        "failed": "阶段失败，尚未满足验收条件。",
        "interrupted": "阶段已中断，尚未完成验收。",
    }.get(status)


def task_outcome_text(payload: dict[str, object]) -> str | None:
    """Render only fixed outcome labels, never persisted paths or model text."""
    status = payload.get("status")
    check_status = payload.get("self_check_status")
    if not isinstance(status, str) or not isinstance(check_status, str):
        return None
    if status not in {"completed", "incomplete", "failed", "interrupted"}:
        return None
    if payload.get("status") == "interrupted":
        return "本轮已中断，已发生的改动未回滚。"
    coverage = payload.get("coverage")
    if check_status == "passed" and "coverage" in payload:
        labels = {
            "tests": "实际执行的测试已通过；仅代表此次检查范围。",
            "basic": "基础检查已通过；功能测试未验证。",
            "none": "自检未取得足够验证证据，已保留本轮结果。",
            "execution": "所选命令与独立复核已完成；仅代表此次检查范围。",
            "browser": "本次非视觉浏览器检查与独立复核已完成；视觉效果未验证。",
            "excluded": "本轮适用检查仅涉及已排除的视觉范围；视觉效果未验证。",
        }
        return labels.get(coverage, "当前自检覆盖范围未确认，不能报告通过。") if isinstance(coverage, str) else "当前自检覆盖范围未确认，不能报告通过。"
    if check_status == "not_run" and "coverage" in payload:
        return "自检未取得足够验证证据，已保留本轮结果。"
    return {
        "not_needed": "本轮无代码变化，无需自检。",
        "passed": "所运行的自检已通过；具体覆盖范围见结果。",
        "failed": "自检失败，当前改动尚未通过检查。",
        "not_run": "自检尚未执行，已保留本轮结果。",
        "source_changed": "检查后代码已变化，当前版本仍需复测。",
        "unknown": "当前变更或检查状态未确认，不能报告通过。",
        "pending": "本轮改动尚未完成自检。",
    }.get(check_status)


def environment_cleanup_text(resources: object) -> str | None:
    """Expose only bounded host-generated Docker resource identities."""
    if not isinstance(resources, (list, tuple)):
        return None
    names = list(dict.fromkeys(
        name for name in resources[:16]
        if isinstance(name, str) and re.fullmatch(r"gum-code-(?:prepare|env)-[0-9a-f]{32}", name)
    ))
    if not names:
        return None
    return "准备资源清理未确认，请手动检查 Docker：\n" + "\n".join(names)


def _self_check_cleanup_notice(payload: dict[str, object]) -> str | None:
    result = payload.get("result")
    if payload.get("tool_name") != "self_check" or not isinstance(result, str) or len(result) > 150_000:
        return None
    try:
        state, _ = json.JSONDecoder().raw_decode(result)
    except ValueError:
        return None
    return environment_cleanup_text(state.get("cleanup_required")) if isinstance(state, dict) else None


class RichEventRenderer:
    """Render bounded progress without exposing raw arguments or results."""

    def __init__(
        self, console: Console, *, verbose: bool = False,
        persistent_callback: AgentEventCallback | None = None,
    ) -> None:
        self.console = console
        self.verbose = verbose
        self._persistent_callback = persistent_callback
        self._status: object | None = None
        self._message: str | None = None
        self._running = False
        self._pause_depth = 0
        self._closed = False
        # Input/close can overlap child callbacks; never hold this lock over input.
        self._lock = RLock()
        try:
            self._terminal = bool(console.file.isatty())
        except (AttributeError, OSError):
            self._terminal = False

    def set_verbose(self, enabled: bool) -> None:
        self.verbose = enabled

    def _stop_status(self) -> None:
        status = self._status
        if status is not None and self._running:
            self._running = False
            status.stop()  # type: ignore[attr-defined]

    def _start_status(self, message: str) -> None:
        with self._lock:
            if self._closed or message == self._message:
                return
            self._message = message
            if not self._terminal:
                return
            # Group keeps Spinner from assembling Text into a wrapping text line.
            text = Group(Text(message, style="blue", no_wrap=True, overflow="crop"))
            if self._status is None:
                self._status = self.console.status(text, spinner="dots", spinner_style="blue")
            else:
                self._status.update(text)  # type: ignore[attr-defined]
            if not self._pause_depth and not self._running:
                self._status.start()  # type: ignore[attr-defined]
                self._running = True

    @contextmanager
    def pause(self) -> Iterator[None]:
        """Suspend refresh across input/output, including concurrent child events."""
        with self._lock:
            self._pause_depth += 1
            self._stop_status()
        try:
            yield
        finally:
            with self._lock:
                self._pause_depth -= 1
                if not self._pause_depth and not self._closed and self._status is not None:
                    self._status.start()  # type: ignore[attr-defined]
                    self._running = True

    def paused(self, callback: Callable[_P, _R]) -> Callable[_P, _R]:
        """Preserve the original approval callback's values and exceptions."""
        @wraps(callback)
        def invoke(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with self.pause():
                return callback(*args, **kwargs)
        return invoke

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop_status()

    def __call__(self, event_type: str, payload: dict[str, object]) -> None:
        if event_type in _THINKING_EVENTS:
            self._start_status("思考中...")
            return
        if event_type == "tool_execution":
            name = payload.get("tool_name")
            if isinstance(name, str) and name in _READ_PROGRESS_TOOLS:
                message = "正在读取..."
            elif isinstance(name, str) and name in _WRITE_PROGRESS_TOOLS:
                message = "正在修改文件..."
            else:
                message = "思考中..."
            self._start_status(message)
            return
        if event_type in {"tool_call", "subagent_tool", "subagent_start"}:
            return
        if event_type in {"context_usage", "context_compacted"} and not self.verbose:
            return
        if event_type == "subagent_end" and payload.get("status") not in {"error", "cancelled"}:
            return
        if event_type == "tool_result":
            if payload.get("tool_name") in {"read_document", "analyze_image"}:
                visible = _document_notice(payload) is not None
            else:
                visible = (
                    str(payload.get("result", "")).startswith("Error:")
                    or _command_failure(payload) is not None
                    or (payload.get("tool_name") == "delete_file"
                        and isinstance(payload.get("deleted_path"), str))
                )
            if not visible and _self_check_cleanup_notice(payload) is None:
                return
        elif event_type not in {
            "final_answer", "task_state", "task_outcome", "subagent_end",
            "context_usage", "context_compacted", "tool_batch_recovery",
            "pending_user_discarded",
        }:
            return
        if event_type in {"final_answer", "task_outcome"}:
            self.close()
        with self.pause():
            if self._persistent_callback is not None:
                self._persistent_callback(event_type, payload)
            else:
                self._render_persistent(event_type, payload)

    def _render_persistent(self, event_type: str, payload: dict[str, object]) -> None:
        if event_type == "task_state":
            text = task_state_text(payload)
            if text is not None:
                self.console.print(Text(text))
            return
        if event_type == "task_outcome":
            text = task_outcome_text(payload)
            if text is not None:
                self.console.print(Text(text))
            return
        if event_type == "tool_result":
            cleanup = _self_check_cleanup_notice(payload)
            if cleanup is not None:
                self.console.print(Text(cleanup, style="yellow"))
            if payload.get("tool_name") in {"read_document", "analyze_image"}:
                notice = _document_notice(payload)
                if notice is not None:
                    self.console.print(Text(notice, style="yellow"))
                return
            result = str(payload.get("result", ""))
            if result.startswith("Error:") or _command_failure(payload) is not None:
                self.console.print(
                    Text(
                        "工具失败："
                        + _safe_inline(payload.get("tool_name", "未知")),
                        style="red",
                    )
                )
            elif (
                payload.get("tool_name") == "delete_file"
                and isinstance(payload.get("deleted_path"), str)
            ):
                self.console.print(
                    Text(
                        "已删除：" + _bounded_inline(payload["deleted_path"]),
                        style="green",
                    )
                )
            return
        if event_type == "subagent_end":
            text = "Subagent 已取消。" if payload.get("status") == "cancelled" else "Subagent 执行失败。"
            self.console.print(Text(text, style="yellow"))
            return
        if event_type == "context_compacted":
            text = _compacted_text(payload)
            self.console.print(Text(text or "已整理上下文。", style="cyan"))
            return
        if event_type == "tool_batch_recovery":
            self.console.print(
                Text("已安全闭合上次中断的工具批次。", style="yellow")
            )
            return
        if event_type == "pending_user_discarded":
            self.console.print(Text("已丢弃 Session 中未完成的旧用户输入，仅执行本次任务。", style="yellow"))
            return
        if self.verbose and event_type == "context_usage":
            text = _usage_text(payload)
            if text is not None:
                self.console.print(Text(text))
            return
        if self.verbose and event_type == "provider_usage":
            return

    def render_answer(self, answer: str) -> None:
        self.close()
        self.console.print()
        self.console.print(Text("GuMi", style="bold blue"))
        with self.console.use_theme(_ANSWER_THEME):
            self.console.print(Markdown(_safe_multiline(answer)))

    def render_compaction(self, payload: dict[str, object]) -> None:
        """Keep the explicit /compact result separate from automatic progress."""
        with self.pause():
            text = _compacted_text(payload)
            self.console.print(Text(text or "已整理上下文。", style="cyan"))


def _truncate(text: str) -> str:
    if len(text) > MAX_PREVIEW:
        return text[:MAX_PREVIEW] + "\n[truncated]"
    return text


def print_terminal_event(
    event_type: str, payload: dict[str, object], *, verbose: bool = False,
) -> None:
    """Render permanent plain output only, sanitizing model-derived text."""
    if event_type == "task_outcome":
        text = task_outcome_text(payload)
        if text is not None:
            print(text, flush=True)
        return
    if event_type == "task_state":
        text = task_state_text(payload)
        if text is not None:
            print(text, flush=True)
        return
    if event_type in _THINKING_EVENTS | {"tool_execution", "subagent_start"}:
        return

    if event_type == "subagent_tool":
        return

    if event_type == "subagent_end":
        if payload.get("status") == "error":
            print("Subagent 执行失败。", flush=True)
        elif payload.get("status") == "cancelled":
            print("Subagent 已取消。", flush=True)
        return

    if event_type == "context_compacted":
        if verbose:
            text = _compacted_text(payload)
            print(f"── {text or '已整理上下文。'} ──", flush=True)
        return

    if event_type == "tool_batch_recovery":
        print(
            "── 恢复中断工具批次：保留 "
            f"{payload.get('completed')} 个已完成结果，"
            f"{payload.get('pending')} 个未执行调用"
            "交给模型重新规划 ──",
            flush=True,
        )
        return

    if event_type == "pending_user_discarded":
        print(
            "── 已丢弃 Session 中未完成的旧用户输入，"
            "仅执行本次任务 ──",
            flush=True,
        )
        return

    if event_type == "context_usage":
        text = _usage_text(payload) if verbose else None
        if text is not None:
            print(f"── {text} ──", flush=True)
        return

    if event_type == "provider_usage":
        return

    if event_type == "tool_call":
        return

    if event_type == "tool_result":
        cleanup = _self_check_cleanup_notice(payload)
        if cleanup is not None:
            print(cleanup, flush=True)
        if payload.get("tool_name") in {"read_document", "analyze_image"}:
            notice = _document_notice(payload)
            if notice is not None:
                print(notice, flush=True)
            return
        result = str(payload.get("result", ""))
        command_failure = _command_failure(payload)
        if command_failure is not None:
            print(f"── 工具错误：{_safe_inline(payload['tool_name'])} ──", flush=True)
            print(command_failure, flush=True)
        elif result.startswith("Error:"):
            print(f"── 工具错误：{_safe_inline(payload['tool_name'])} ──", flush=True)
            print(_truncate(_safe_multiline(result)), flush=True)
        elif (
            payload.get("tool_name") == "delete_file"
            and isinstance(payload.get("deleted_path"), str)
        ):
            print("已删除：" + _bounded_inline(payload["deleted_path"]), flush=True)
        return

    if event_type == "final_answer":
        print("── 最终回答 ──", flush=True)
        print(_safe_multiline(payload.get("answer", "")), flush=True)
        return
