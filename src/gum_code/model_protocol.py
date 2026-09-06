"""Preserve OpenAI-compatible assistant messages and context error matching."""


def assistant_message_to_dict(message: object) -> dict[str, object]:
    content = getattr(message, "content", None)
    reasoning_content = getattr(message, "reasoning_content", None)

    result: dict[str, object] = {
        "role": "assistant",
        "content": content,
    }
    if isinstance(reasoning_content, str):
        result["reasoning_content"] = reasoning_content

    tool_calls = getattr(message, "tool_calls", None)
    if not tool_calls:
        return result

    result["tool_calls"] = [
        {
            "id": tool_call.id,
            "type": getattr(tool_call, "type", "function"),
            "function": {
                "name": tool_call.function.name,
                "arguments": tool_call.function.arguments,
            },
        }
        for tool_call in tool_calls
    ]
    return result


def is_context_length_error(error: object) -> bool:
    text = str(error).lower()
    return (
        "prompt_too_long" in text
        or "too many tokens" in text
        or "context_length_exceeded" in text
    )
