from collections.abc import Callable

MAX_PREVIEW = 1000

AgentEventCallback = Callable[
    [str, dict[str, object]],
    None,
]


def _truncate(text: str) -> str:
    if len(text) > MAX_PREVIEW:
        return text[:MAX_PREVIEW] + "\n[truncated]"
    return text


def print_terminal_event(event_type: str, payload: dict[str, object]) -> None:
    if event_type == "context_compacted":
        stage = payload.get("stage")
        before_chars = payload.get("before_chars")
        after_chars = payload.get("after_chars")
        if stage == "snip_compact":
            print(
                "── 上下文整理（snip_compact）："
                f"归档 {payload.get('archived_messages')} 条消息到 "
                f"{payload.get('transcript')}，"
                f"{before_chars} → {after_chars} 字符 ──"
            )
        elif stage == "micro_compact":
            print(
                "── 上下文整理（micro_compact）："
                f"缩短 {payload.get('shortened_results')} 条结果，"
                f"{before_chars} → {after_chars} 字符 ──"
            )
        elif stage == "fit_tool_results":
            print(
                "── 上下文整理（fit_tool_results）："
                f"收纳 {payload.get('fitted_results')} 条结果，"
                f"{before_chars} → {after_chars} 字符 ──"
            )
        elif stage == "compact_history":
            print(
                "── 上下文整理（compact_history）："
                f"摘要写入 {payload.get('transcript')}，"
                f"{before_chars} → {after_chars} 字符 ──"
            )
        elif stage == "reactive_compact":
            print(
                "── 上下文整理（reactive_compact，第 "
                f"{payload.get('retry')} 次重试）："
                f"摘要写入 {payload.get('transcript')}，"
                f"{before_chars} → {after_chars} 字符 ──"
            )
        elif stage == "active_compact":
            print(
                "── 上下文整理（active_compact）："
                f"摘要写入 {payload.get('transcript')}，"
                f"{before_chars} → {after_chars} 字符 ──"
            )
        else:
            print(
                "── 上下文整理（tool_result_budget）："
                f"转存 {payload.get('persisted_results')} 条结果，"
                f"{before_chars} → {after_chars} 字符 ──"
            )
        return

    if event_type == "context_summary":
        print("── 正在生成历史摘要 ──")
        return

    if event_type == "tool_batch_recovery":
        print(
            "── 恢复中断工具批次：保留 "
            f"{payload.get('completed')} 个已完成结果，"
            f"{payload.get('pending')} 个未执行调用交给模型重新规划 ──"
        )
        return

    if event_type == "context_usage":
        print(
            f"── 上下文：{payload['used_chars']} / {payload['max_chars']} 字符 ──"
        )
        return

    if event_type == "tool_call":
        print(f"── 第 {payload['step']} 步：{payload['tool_name']} ──")
        return

    if event_type == "tool_result":
        result = str(payload.get("result", ""))
        if result.startswith("Error:"):
            print(f"── 工具错误：{payload['tool_name']} ──")
            print(_truncate(result))
        return

    if event_type == "final_answer":
        print("── 最终回答 ──")
        print(str(payload.get("answer", "")))
        return
