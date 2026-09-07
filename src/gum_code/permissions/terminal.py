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




def request_verification_approval(details: dict[str, object]) -> bool:
    """Deprecated compatibility entry; old verification scopes grant nothing."""
    return False


def make_verification_approval(
    workspace: Path,
    console: Console,
    *,
    chooser: Callable[..., object] = choice,
) -> Callable[[dict[str, object]], bool]:
    """Deprecated compatibility factory; no menu or historical permission."""
    return request_verification_approval


def request_environment_preparation(
    details: dict[str, object], *, interactive: bool = False,
    console: Console | None = None,
) -> bool:
    """Ask for the host-selected environment only; never display raw metadata."""
    if not interactive or not isinstance(details, dict):
        return False
    ecosystem = details.get("ecosystem")
    if not isinstance(ecosystem, str):
        return False
    language = {"python": "Python", "node": "Node.js", "browser": "Chromium 浏览器",
                "postgres": "PostgreSQL 测试数据库"}.get(ecosystem)
    if language is None:
        return False
    source = "PyPI" if ecosystem == "python" else "npm 公共仓库"
    runtime = details.get("runtime")
    version = ""
    if isinstance(runtime, str) and re.fullmatch(r"v?\d{1,3}(?:\.\d{1,3}){0,2}", runtime):
        version = " " + runtime
    missing = details.get("missing")
    missing_text = {
        "docker_unavailable": "Docker 不可用，需手动安装或启动；本程序不会修改系统。",
        "runtime_unavailable": "没有匹配的隔离运行时。",
        "project_dependencies": "项目依赖环境尚未准备。",
    }.get(missing, "缺少可用运行环境或依赖。") if isinstance(missing, str) else "缺少可用运行环境或依赖。"
    text = (
        f"{language}{version} 自检：{missing_text}创建项目专属环境将从公共镜像及 {source} 下载，"
        "占用本地空间，并可能执行第三方依赖安装代码；不会创建数据库或云服务。"
    )
    if ecosystem == "browser":
        text = (
            "浏览器检查环境未准备。将下载固定版本的公开浏览器镜像和 Playwright，"
            "可能占用较多空间并运行安装代码；仅用于当前项目检查，不修改宿主 Python 环境。"
            "拒绝后仍可开发，但浏览器检查未完成。"
        )
    elif ecosystem == "postgres":
        text = (
            "测试数据库镜像未准备。将下载公开 PostgreSQL 镜像，用独立测试数据卷运行；"
            "正常退出停止服务但保留数据，不接入生产数据库。"
        )
    if console is None:
        print(text, flush=True)
    else:
        console.print(Text(text))
    try:
        answer = input("是否创建该项目运行环境？(y/N): ")
    except EOFError:
        return False
    # Ctrl-C remains cancellation rather than consent or an ordinary refusal.
    return answer.strip().lower() in {"y", "yes"}
