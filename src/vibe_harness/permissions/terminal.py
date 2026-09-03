import json

MAX_ARGUMENTS_PREVIEW = 1000


def request_terminal_approval(
    tool_name: str,
    arguments: dict[str, object],
    reason: str,
) -> bool:
    preview = json.dumps(arguments, ensure_ascii=False, indent=2)

    if len(preview) > MAX_ARGUMENTS_PREVIEW:
        preview = preview[:MAX_ARGUMENTS_PREVIEW] + "\n[arguments truncated]"

    print(f"工具：{tool_name}")
    print(f"原因：{reason}")
    print("参数预览：")
    print(preview)

    try:
        answer = input("是否批准执行？(y/N): ")
    except (EOFError, KeyboardInterrupt):
        return False

    return answer.strip().lower() in {"y", "yes"}
