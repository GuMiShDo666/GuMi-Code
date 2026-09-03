import json
import re
from collections.abc import Callable
from pathlib import Path

from prompt_toolkit.shortcuts import choice
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

MAX_ARGUMENTS_PREVIEW = 1000


def _safe_terminal_text(value: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "?", str(value))


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


def _safe_argument_summary(
    workspace: Path,
    tool_name: str,
    arguments: dict[str, object],
) -> str:
    parts: list[str] = []
    path_value = arguments.get("path")
    if isinstance(path_value, str):
        try:
            target = (workspace / path_value).resolve()
            parts.append(f"目标：{_safe_terminal_text(target)}")
        except OSError:
            parts.append("目标：无法解析")
    if tool_name == "run_command":
        argv = arguments.get("argv")
        if isinstance(argv, list) and argv and isinstance(argv[0], str):
            parts.append(f"程序：{Path(argv[0]).name}")
            parts.append(f"参数个数：{max(0, len(argv) - 1)}")
        parts.append(
            "命令在断网 Docker Sandbox 中执行，仍可修改工作区。"
        )
    if not parts:
        parts.append(f"参数字段：{', '.join(sorted(arguments)) or '无'}")
    return "\n".join(parts)[:MAX_ARGUMENTS_PREVIEW]


def make_interactive_approval(
    workspace: Path,
    console: Console,
    *,
    chooser: Callable[..., object] = choice,
) -> Callable[[str, dict[str, object], str], bool]:
    """Create a one-call approval UI. Any UI failure is a denial."""

    resolved_workspace = Path(workspace).resolve()

    def approve(
        tool_name: str,
        arguments: dict[str, object],
        reason: str,
    ) -> bool:
        try:
            body = Text()
            body.append(f"工具：{_safe_terminal_text(tool_name)}\n")
            body.append(f"风险：{_safe_terminal_text(reason[:500])}\n")
            body.append(
                _safe_argument_summary(resolved_workspace, tool_name, arguments)
            )
            console.print(Panel(body, title="Permission", border_style="yellow"))
            decision = chooser(
                "是否允许本次执行？",
                options=[(False, "拒绝"), (True, "仅批准本次")],
                default=False,
            )
            return decision is True
        except (Exception, KeyboardInterrupt, EOFError):
            return False

    return approve
