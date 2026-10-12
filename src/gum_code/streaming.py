"""Assemble complete protocol messages while emitting only public text."""
from types import SimpleNamespace as NS


def create_streamed_completion(client, on_text, **kwargs):
    stream = client.chat.completions.create(**kwargs, stream=True, stream_options={"include_usage": True})
    content, reasoning, calls, finish, usage = "", "", {}, None, None
    try:
        for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage
            for choice in chunk.choices:
                if choice.index != 0:
                    raise ValueError("Unexpected stream choice")
                delta = choice.delta
                content += getattr(delta, "content", None) or ""
                reasoning += getattr(delta, "reasoning_content", None) or ""
                if len(content) + len(reasoning) > 2_000_000:
                    raise ValueError("Stream too large")
                for call in getattr(delta, "tool_calls", None) or []:
                    index = call.index
                    if type(index) is not int or not 0 <= index < 128:
                        raise ValueError("Invalid stream tool index")
                    item = calls.setdefault(index, dict(id="", name="", arguments=""))
                    item["id"] += call.id or ""
                    function = call.function
                    if function:
                        item["name"] += function.name or ""
                        item["arguments"] += function.arguments or ""
                    if len(item["arguments"]) > 1_000_000:
                        raise ValueError("Tool arguments too large")
                if getattr(delta, "content", None):
                    on_text(content, False)
                if choice.finish_reason is not None:
                    finish = choice.finish_reason
        if finish is None:
            raise ValueError("Stream ended before completion")
        on_text(content, True)
        tools = [NS(id=c["id"], type="function", function=NS(name=c["name"], arguments=c["arguments"])) for _, c in sorted(calls.items())]
        return NS(choices=[NS(message=NS(content=content or None, reasoning_content=reasoning, tool_calls=tools or None), finish_reason=finish)], usage=usage)
    finally:
        stream.close()
