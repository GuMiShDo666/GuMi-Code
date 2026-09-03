import argparse
import sys
from pathlib import Path

from openai import OpenAI, OpenAIError

from vibe_harness.agent import MAX_STEPS, run_agent
from vibe_harness.context import DEFAULT_MAX_CONTEXT_CHARS
from vibe_harness.config import load_settings
from vibe_harness.events import print_terminal_event
from vibe_harness.permissions.terminal import request_terminal_approval
from vibe_harness.session import (
    build_tool_batch_recovery_messages,
    load_session,
    save_session,
)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是整数")

    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")

    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="vibe-harness")

    parser.add_argument(
        "--workspace",
        required=True,
        help="工作区目录路径",
    )
    parser.add_argument(
        "--max-steps",
        type=_positive_int,
        default=MAX_STEPS,
        help=f"最多执行的正模型步骤数（默认 {MAX_STEPS}）",
    )
    parser.add_argument(
        "--max-context-chars",
        type=_positive_int,
        default=DEFAULT_MAX_CONTEXT_CHARS,
        help="模型请求允许的最大上下文字符数",
    )
    parser.add_argument(
        "--allow-run-command",
        action="store_true",
        help=(
            "允许模型请求执行本机命令；"
            "每次命令仍需终端审批，且不提供 OS 沙箱"
        ),
    )
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--session-file",
        default=None,
        help="Session 保存路径（JSON 文件）",
    )
    session_group.add_argument(
        "--resume-session",
        default=None,
        help="要恢复的 Session 文件路径（JSON 文件）",
    )
    parser.add_argument(
        "task",
        help="要执行的任务描述",
    )

    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()

    if not workspace.exists() or not workspace.is_dir():
        parser.error(f"workspace 不存在或不是目录：{workspace}")

    args.workspace = workspace

    if args.session_file is not None:
        requested_session_file = Path(args.session_file).expanduser()
        if requested_session_file.is_symlink():
            parser.error("session-file 不允许是符号链接")

        session_file = requested_session_file.resolve()

        parent = session_file.parent
        if not parent.exists() or not parent.is_dir():
            parser.error(f"session-file 父目录不存在或不是目录：{parent}")

        args.session_file = session_file

    if args.resume_session is not None:
        requested_resume = Path(args.resume_session).expanduser()
        if requested_resume.is_symlink():
            parser.error("resume-session 不允许是符号链接")

        resume_session = requested_resume.resolve()

        if not resume_session.exists() or not resume_session.is_file():
            parser.error(f"resume-session 不存在或不是普通文件：{resume_session}")

        args.resume_session = resume_session

    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.allow_run_command:
        print(
            "警告：run_command 已启用；"
            "命令仍需审批，但当前不是 OS 沙箱。"
        )

    try:
        settings = load_settings()

        client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
        )

        message_snapshot_callback = None
        initial_messages = None
        session_file = None

        if args.session_file is not None:
            session_file = args.session_file
        elif args.resume_session is not None:
            session_file = args.resume_session
            data = load_session(session_file, allow_incomplete_tool_batch=True)

            if data.workspace.resolve() != args.workspace:
                raise ValueError(
                    f"Session workspace 不匹配："
                    f"{data.workspace.resolve()} != {args.workspace}"
                )

            if data.model != settings.model:
                raise ValueError(
                    f"Session model 不匹配："
                    f"{data.model!r} != {settings.model!r}"
                )

            if data.tool_batch_state is not None:
                initial_messages = build_tool_batch_recovery_messages(
                    data.messages
                )
                print_terminal_event(
                    "tool_batch_recovery",
                    {
                        "completed": len(
                            data.tool_batch_state.completed_call_ids
                        ),
                        "pending": len(
                            data.tool_batch_state.pending_call_ids
                        ),
                    },
                )
            else:
                initial_messages = data.messages

        if session_file is not None:
            workspace = args.workspace
            model = settings.model

            def message_snapshot_callback(
                messages: list[dict[str, object]],
            ) -> None:
                save_session(session_file, workspace, model, messages)

        run_agent(
            client=client,
            model=settings.model,
            workspace=args.workspace,
            user_query=args.task,
            max_steps=args.max_steps,
            max_context_chars=args.max_context_chars,
            allow_run_command=args.allow_run_command,
            approval_callback=request_terminal_approval,
            event_callback=print_terminal_event,
            message_snapshot_callback=message_snapshot_callback,
            initial_messages=initial_messages,
        )
    except (RuntimeError, OpenAIError, OSError, ValueError) as exc:
        sys.stdout.flush()
        print(f"[错误] {exc}", file=sys.stderr, flush=True)
        return 1

    return 0
