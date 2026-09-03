import os
import re
from collections.abc import Callable

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

MAX_PREVIEW = 1000

AgentEventCallback = Callable[
    [str, dict[str, object]],
    None,
]


def _safe_inline(value: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "?", str(value))


def _safe_multiline(value: object) -> str:
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "?", str(value))


def create_console() -> Console:
    return Console(
        no_color=os.getenv("NO_COLOR") is not None,
        force_terminal=False,
    )


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
        self._status = self.console.status(message)
        self._status.start()  # type: ignore[attr-defined]

    def close(self) -> None:
        self._stop_status()

    def __call__(self, event_type: str, payload: dict[str, object]) -> None:
        if event_type in {"model_call", "subagent_step", "context_summary"}:
            message = (
                "正在整理上下文…"
                if event_type == "context_summary"
                else "Model 正在思考…"
            )
            self._start_status(message)
            return

        self._stop_status()
        if event_type == "final_answer":
            return
        if event_type == "tool_call":
            name = _safe_inline(payload.get("tool_name", "未知"))
            self.console.print(Text(f"工具：{name}"))
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
            return
        if event_type == "subagent_start":
            self.console.print(Text("Subagent 开始"))
            self._start_status("Subagent 正在处理…")
            return
        if event_type == "subagent_tool":
            self.console.print(
                Text(
                    "Subagent 工具："
                    + _safe_inline(payload.get("tool_name", "未知"))
                )
            )
            self._start_status("Subagent 正在处理…")
            return
        if event_type == "subagent_end":
            status = _safe_inline(payload.get("status", "结束"))
            self.console.print(Text(f"Subagent：{status}"))
            return
        if event_type == "context_compacted":
            self.console.print(Text("已整理上下文。", style="cyan"))
            return
        if event_type == "tool_batch_recovery":
            self.console.print(
                Text("已安全闭合上次中断的工具批次。", style="yellow")
            )
            return
        if self.verbose and event_type == "context_usage":
            self.console.print(
                Text(
                    "Context: "
                    f"{payload.get('used_tokens')} / {payload.get('max_tokens')} tokens"
                )
            )
            return
        if self.verbose and event_type == "provider_usage":
            prompt_tokens = payload.get("prompt_tokens")
            if prompt_tokens is not None:
                self.console.print(Text(f"Provider prompt tokens: {prompt_tokens}"))

    def render_answer(self, answer: str) -> None:
        self._stop_status()
        self.console.print(Markdown(_safe_multiline(answer)))


def _truncate(text: str) -> str:
    if len(text) > MAX_PREVIEW:
        return text[:MAX_PREVIEW] + "\n[truncated]"
    return text


def print_terminal_event(event_type: str, payload: dict[str, object]) -> None:
    if event_type == "subagent_start":
        print(
            "  └─ Subagent 开始"
            f"（最多 {payload.get('max_calls')} 次模型请求）"
        )
        return

    if event_type == "subagent_step":
        print(
            "     第 "
            f"{payload.get('step')} 步"
            f"（{payload.get('calls_used')} / {payload.get('max_calls')} 次请求）"
        )
        return

    if event_type == "subagent_tool":
        print(f"     工具：{_safe_inline(payload.get('tool_name'))}")
        return

    if event_type == "subagent_end":
        print(
            "  └─ Subagent "
            f"{_safe_inline(payload.get('status'))}"
            f"（{payload.get('calls_used')} / {payload.get('max_calls')} 次请求）"
        )
        return

    if event_type == "context_compacted":
        stage = payload.get("stage")
        before_tokens = payload.get("before_tokens")
        after_tokens = payload.get("after_tokens")
        if stage == "snip_compact":
            print(
                "── 上下文整理（snip_compact）："
                f"归档 {payload.get('archived_messages')} 条消息到 "
                f"{payload.get('transcript')}，"
                f"{before_tokens} → {after_tokens} tokens ──"
            )
        elif stage == "micro_compact":
            print(
                "── 上下文整理（micro_compact）："
                f"缩短 {payload.get('shortened_results')} 条结果，"
                f"{before_tokens} → {after_tokens} tokens ──"
            )
        elif stage == "fit_tool_results":
            print(
                "── 上下文整理（fit_tool_results）："
                f"收纳 {payload.get('fitted_results')} 条结果，"
                f"{before_tokens} → {after_tokens} tokens ──"
            )
        elif stage == "compact_history":
            print(
                "── 上下文整理（compact_history）："
                f"摘要写入 {payload.get('transcript')}，"
                f"{before_tokens} → {after_tokens} tokens ──"
            )
        elif stage == "reactive_compact":
            print(
                "── 上下文整理（reactive_compact，第 "
                f"{payload.get('retry')} 次重试）："
                f"摘要写入 {payload.get('transcript')}，"
                f"{before_tokens} → {after_tokens} tokens ──"
            )
        elif stage == "active_compact":
            print(
                "── 上下文整理（active_compact）："
                f"摘要写入 {payload.get('transcript')}，"
                f"{before_tokens} → {after_tokens} tokens ──"
            )
        else:
            print(
                "── 上下文整理（tool_result_budget）："
                f"转存 {payload.get('persisted_results')} 条结果，"
                f"{before_tokens} → {after_tokens} tokens ──"
            )
        return

    if event_type == "context_summary":
        print("── 正在生成历史摘要 ──")
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
        print(
            "── 上下文："
            f"{payload['used_tokens']} / {payload['max_tokens']} tokens ──"
        )
        return

    if event_type == "provider_usage":
        prompt_tokens = payload.get("prompt_tokens")
        local_tokens = payload.get("local_prompt_tokens")
        if prompt_tokens is not None:
            print(
                "── 服务端 Token："
                f"prompt={prompt_tokens}，本地={local_tokens} ──"
            )
        return

    if event_type == "tool_call":
        print(
            f"── 第 {payload['step']} 步："
            f"{_safe_inline(payload['tool_name'])} ──"
        )
        return

    if event_type == "tool_result":
        result = str(payload.get("result", ""))
        if result.startswith("Error:"):
            print(f"── 工具错误：{_safe_inline(payload['tool_name'])} ──")
            print(_truncate(_safe_multiline(result)))
        return

    if event_type == "final_answer":
        print("── 最终回答 ──")
        print(_safe_multiline(payload.get("answer", "")))
        return
