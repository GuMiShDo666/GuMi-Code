import argparse
import sys
from pathlib import Path

from openai import OpenAI, OpenAIError

from gum_code.agent import MAX_STEPS, run_agent
from gum_code.config import (
    load_settings,
    resolve_context_budget,
    resolve_context_window,
)
from gum_code.events import create_console, print_terminal_event
from gum_code.interactive import (
    run_interactive,
    select_workspace,
    trust_workspace,
)
from gum_code.permissions.terminal import request_terminal_approval
from gum_code.session import (
    LEGACY_SESSION_VERSION,
    SessionLease,
    build_tool_batch_recovery_messages,
    load_session,
    save_session,
)
from gum_code.token_counting import load_token_counter
from gum_code.tools.command import load_docker_sandbox
from gum_code.user_state import migrate_legacy_user_data


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是整数")
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _is_interactive_terminal() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def parse_args(
    argv: list[str] | None = None,
    *,
    interactive: bool | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gum-code")
    parser.add_argument("--workspace", help="工作区目录路径")
    parser.add_argument(
        "--max-steps", type=_positive_int, default=MAX_STEPS,
        help=f"最多执行的正模型步骤数（默认 {MAX_STEPS}）",
    )
    parser.add_argument(
        "--max-context-tokens", type=_positive_int,
        default=None,
        help="模型请求预算；默认使用模型上下文窗口的 80%%",
    )
    parser.add_argument(
        "--allow-run-command", action="store_true",
        help="允许模型请求在 Docker Sandbox 中执行命令，每次仍需审批",
    )
    parser.add_argument(
        "--no-subagent", dest="allow_subagent", action="store_false",
        help="关闭默认启用的同步、单层、只读 task Subagent",
    )
    parser.add_argument(
        "--sandbox-image", default=None,
        help="run_command 使用的本地可信 Docker 镜像；Harness 不会自动拉取",
    )
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument("--session-file", help="Session 保存路径（JSON 文件）")
    session_group.add_argument("--resume-session", help="要恢复的 Session JSON")
    parser.add_argument("task", nargs="?", help="要执行的任务描述")

    args = parser.parse_args(argv)
    args.interactive_terminal = (
        _is_interactive_terminal() if interactive is None else interactive
    )
    if not args.interactive_terminal:
        if args.workspace is None:
            parser.error("非交互终端必须指定 --workspace")
        if args.task is None:
            parser.error("非交互终端必须指定 task")

    if args.workspace is not None:
        workspace = Path(args.workspace).expanduser().resolve()
        if not workspace.exists() or not workspace.is_dir():
            parser.error(f"workspace 不存在或不是目录：{workspace}")
        args.workspace = workspace

    if args.allow_run_command and args.sandbox_image is None:
        parser.error("--allow-run-command 必须同时指定 --sandbox-image")
    if not args.allow_run_command and args.sandbox_image is not None:
        parser.error("--sandbox-image 只能与 --allow-run-command 一起使用")

    if args.session_file is not None:
        requested = Path(args.session_file).expanduser()
        if requested.is_symlink():
            parser.error("session-file 不允许是符号链接")
        target = requested.resolve()
        if not target.parent.exists() or not target.parent.is_dir():
            parser.error(f"session-file 父目录不存在或不是目录：{target.parent}")
        args.session_file = target

    if args.resume_session is not None:
        requested = Path(args.resume_session).expanduser()
        if requested.is_symlink():
            parser.error("resume-session 不允许是符号链接")
        target = requested.resolve()
        if not target.exists() or not target.is_file():
            parser.error(f"resume-session 不存在或不是普通文件：{target}")
        args.resume_session = target
    return args


def _run_one_shot(args: argparse.Namespace) -> int:
    lease: SessionLease | None = None
    try:
        session_file = args.session_file or args.resume_session
        if session_file is not None:
            lease = SessionLease(session_file).acquire()

        resumed = None
        if args.resume_session is not None:
            resumed = load_session(
                args.resume_session,
                allow_incomplete_tool_batch=True,
                allow_pending_user_tail=True,
            )
            if resumed.version == LEGACY_SESSION_VERSION:
                raise ValueError(
                    "one-shot 不能直接恢复 v1 Session；"
                    "请先在交互模式的 /sessions 中显式导入"
                )
            if args.workspace is None:
                args.workspace = resumed.workspace.resolve()
            elif resumed.workspace.resolve() != args.workspace:
                raise ValueError("Session workspace 与 --workspace 不匹配")
        elif args.workspace is None:
            selected = select_workspace(create_console())
            if selected is None:
                return 0
            args.workspace = selected

        if args.interactive_terminal:
            args.workspace = trust_workspace(args.workspace, create_console())

        command_sandbox = None
        if args.allow_run_command:
            command_sandbox = load_docker_sandbox(args.sandbox_image)
            print(
                "警告：run_command 已启用；命令将在断网 Docker Sandbox 中运行，"
                "但仍可修改未遮蔽的 workspace 文件且每次需审批。"
            )

        settings = load_settings()
        if resumed is not None and resumed.model != settings.model:
            raise ValueError("Session model 与当前 provider 不匹配")
        client = OpenAI(api_key=settings.api_key, base_url=settings.base_url)
        context_window = resolve_context_window(settings, client)
        max_context_tokens = resolve_context_budget(
            context_window, args.max_context_tokens
        )
        token_counter = load_token_counter(settings.model)
        initial_messages = None
        title = ""
        if resumed is not None:
            title = resumed.title
            if resumed.pending_user_message is not None:
                initial_messages = resumed.messages[:-1]
                print_terminal_event("pending_user_discarded", {})
            elif resumed.tool_batch_state is not None:
                initial_messages = build_tool_batch_recovery_messages(resumed.messages)
                print_terminal_event(
                    "tool_batch_recovery",
                    {
                        "completed": len(resumed.tool_batch_state.completed_call_ids),
                        "pending": len(resumed.tool_batch_state.pending_call_ids),
                    },
                )
            else:
                initial_messages = resumed.messages

        message_snapshot_callback = None
        if session_file is not None:
            workspace = args.workspace
            model = settings.model

            def message_snapshot_callback(messages: list[dict[str, object]]) -> None:
                save_session(session_file, workspace, model, messages, title)

        kwargs: dict[str, object] = {}
        if command_sandbox is not None:
            kwargs["command_sandbox"] = command_sandbox
        if not args.allow_subagent:
            kwargs["allow_subagent"] = False
        run_agent(
            client=client,
            model=settings.model,
            workspace=args.workspace,
            user_query=args.task,
            max_steps=args.max_steps,
            max_context_tokens=max_context_tokens,
            allow_run_command=args.allow_run_command,
            approval_callback=request_terminal_approval,
            event_callback=print_terminal_event,
            message_snapshot_callback=message_snapshot_callback,
            initial_messages=initial_messages,
            token_counter=token_counter,
            **kwargs,
        )
        return 0
    finally:
        if lease is not None:
            lease.release()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        migrate_legacy_user_data()
        if args.task is None:
            return run_interactive(args)
        return _run_one_shot(args)
    except (RuntimeError, OpenAIError, OSError, ValueError) as exc:
        sys.stdout.flush()
        print(f"[错误] {exc}", file=sys.stderr, flush=True)
        return 1
