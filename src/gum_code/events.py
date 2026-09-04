"""Render structured Agent events with bounded, sanitized terminal text."""

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


def create_console() -> Console:
    return Console(no_color=os.getenv("NO_COLOR") is not None)


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
        if event_type == "tool_result":
            result = str(payload.get("result", ""))
            if result.startswith("Error:"):
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
        result = str(payload.get("result", ""))
        if result.startswith("Error:"):
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
