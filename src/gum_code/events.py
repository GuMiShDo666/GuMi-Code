"""Render structured Agent events with bounded, sanitized terminal text."""

import json
import os
import re
from collections.abc import Callable

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme

MAX_PREVIEW = 1000
_ANSWER_THEME = Theme({"markdown.code": "bold"}, inherit=False)

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


class RichEventRenderer:
    """Render bounded progress without exposing raw arguments or results."""

    def __init__(self, console: Console, *, verbose: bool = False) -> None:
        self.console = console
        self.verbose = verbose
        self._status: object | None = None

    def set_verbose(self, enabled: bool) -> None:
        self.verbose = enabled

    def _stop_status(self) -> None:
        status = self._status
        if status is not None:
            self._status = None
            status.stop()  # type: ignore[attr-defined]

    def _start_status(self, message: str) -> None:
        self._stop_status()
        self._status = self.console.status(message, spinner="dots")
        self._status.start()  # type: ignore[attr-defined]

    def close(self) -> None:
        self._stop_status()

    def __call__(self, event_type: str, payload: dict[str, object]) -> None:
        if event_type in {"request_prepare", "subagent_prepare"}:
            self._start_status("准备中...")
            return
        if event_type in {"model_call", "subagent_step"}:
            self._start_status("思考中...")
            return
        if event_type == "context_summary":
            self._start_status("整理上下文中...")
            return

        if event_type == "tool_execution":
            self._stop_status()
            name = _bounded_inline(payload.get("tool_name", "未知"))
            self.console.print(Text(f"执行中：正在调用{name}"))
            return

        if event_type in {"tool_call", "subagent_tool"}:
            self._stop_status()
            return

        self._stop_status()
        if event_type == "final_answer":
            return
        if event_type == "task_state":
            text = task_state_text(payload)
            if text is not None:
                self.console.print(Text(text))
            return
        if event_type == "tool_result":
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
        if event_type == "subagent_start":
            self.console.print(Text("Subagent 开始"))
            return
        if event_type == "subagent_end":
            status = _safe_inline(payload.get("status", "结束"))
            self.console.print(Text(f"Subagent：{status}"))
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
        if self.verbose and event_type == "context_usage":
            text = _usage_text(payload)
            if text is not None:
                self.console.print(Text(text))
            return
        if self.verbose and event_type == "provider_usage":
            return

    def render_answer(self, answer: str) -> None:
        self._stop_status()
        with self.console.use_theme(_ANSWER_THEME):
            self.console.print(Markdown(_safe_multiline(answer)))


def _truncate(text: str) -> str:
    if len(text) > MAX_PREVIEW:
        return text[:MAX_PREVIEW] + "\n[truncated]"
    return text


def print_terminal_event(event_type: str, payload: dict[str, object]) -> None:
    """Render one event while sanitizing model-derived terminal text."""
    if event_type == "task_state":
        text = task_state_text(payload)
        if text is not None:
            print(text, flush=True)
        return
    progress_text = {
        "request_prepare": "准备中...",
        "subagent_prepare": "准备中...",
        "model_call": "思考中...",
        "subagent_step": "思考中...",
        "context_summary": "整理上下文中...",
    }.get(event_type)
    if progress_text is not None:
        print(
            f"── 第 {_safe_inline(payload.get('step', '?'))} 步："
            f"{progress_text} ──",
            flush=True,
        )
        return

    if event_type == "tool_execution":
        name = _bounded_inline(payload.get("tool_name", "未知"))
        print(
            f"── 第 {_safe_inline(payload.get('step', '?'))} 步："
            f"执行中：正在调用{name} ──",
            flush=True,
        )
        return

    if event_type == "subagent_start":
        print(
            "  └─ Subagent 开始"
            f"（最多 {payload.get('max_calls')} 次模型请求）",
            flush=True,
        )
        return

    if event_type == "subagent_tool":
        return

    if event_type == "subagent_end":
        print(
            "  └─ Subagent "
            f"{_safe_inline(payload.get('status'))}"
            f"（{payload.get('calls_used')} / {payload.get('max_calls')} 次请求）"
        )
        return

    if event_type == "context_compacted":
        text = _compacted_text(payload)
        print(f"── {text or '已整理上下文。'} ──")
        return

    if event_type == "tool_batch_recovery":
        print(
            "── 恢复中断工具批次：保留 "
            f"{payload.get('completed')} 个已完成结果，"
            f"{payload.get('pending')} 个未执行调用"
            "交给模型重新规划 ──"
        )
        return

    if event_type == "pending_user_discarded":
        print(
            "── 已丢弃 Session 中未完成的旧用户输入，"
            "仅执行本次任务 ──"
        )
        return

    if event_type == "context_usage":
        text = _usage_text(payload)
        if text is not None:
            print(f"── {text} ──")
        return

    if event_type == "provider_usage":
        return

    if event_type == "tool_call":
        return

    if event_type == "tool_result":
        if payload.get("tool_name") in {"read_document", "analyze_image"}:
            notice = _document_notice(payload)
            if notice is not None:
                print(notice, flush=True)
            return
        result = str(payload.get("result", ""))
        command_failure = _command_failure(payload)
        if command_failure is not None:
            print(f"── 工具错误：{_safe_inline(payload['tool_name'])} ──")
            print(command_failure)
        elif result.startswith("Error:"):
            print(f"── 工具错误：{_safe_inline(payload['tool_name'])} ──")
            print(_truncate(_safe_multiline(result)))
        elif (
            payload.get("tool_name") == "delete_file"
            and isinstance(payload.get("deleted_path"), str)
        ):
            print("已删除：" + _bounded_inline(payload["deleted_path"]))
        return

    if event_type == "final_answer":
        print("── 最终回答 ──")
        print(_safe_multiline(payload.get("answer", "")))
        return
