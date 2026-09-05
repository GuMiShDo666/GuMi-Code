"""Wire explicit task intent, durable outcomes, and process-level exit status."""

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
from gum_code.events import (
    create_console,
    deduplicate_tool_execution,
    print_terminal_event,
)
from gum_code.interactive import (
    resolve_task_type,
    run_interactive,
    select_workspace,
    trust_workspace,
)
from gum_code.permissions.terminal import (
    request_terminal_approval,
    request_verification_approval,
)
from gum_code.session import (
    SESSION_VERSION,
    SessionLease,
    append_session_event,
    build_tool_batch_recovery_messages,
    load_session,
    migrate_session_to_v3,
    save_session,
)
from gum_code.skills import SkillRuntime, SkillSnapshot
from gum_code.token_counting import load_token_counter
from gum_code.tools.command import load_docker_sandbox
from gum_code.user_state import allocate_session_path, migrate_legacy_user_data
from gum_code.verification import VerificationRuntime, validate_task_state


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
        "--task-type", choices=("development", "read-only"),
        help="确认当前阶段类型；非交互未声明且无可恢复类型时只能报告未验证",
    )
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
        help="允许模型请求在 Docker Sandbox 中执行命令；固定验收可另行批准阶段内重跑",
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
    session_group.add_argument("--session-file", help="Session 保存路径（JSONL 文件）")
    session_group.add_argument("--resume-session", help="要恢复的 Session JSON/JSONL")
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
            if resumed.version != SESSION_VERSION:
                destination = allocate_session_path()
                destination_lease = SessionLease(destination).acquire()
                try:
                    migrate_session_to_v3(args.resume_session, destination)
                    migrated = load_session(
                        destination,
                        allow_incomplete_tool_batch=True,
                        allow_pending_user_tail=True,
                    )
                except BaseException:
                    destination_lease.release()
                    raise
                assert lease is not None
                lease.release()
                lease = destination_lease
                session_file = destination
                resumed = migrated
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
                "但仍可修改未遮蔽的 workspace 文件；固定验收重跑需单独批准。"
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
        sticky_skills: tuple[str, ...] = ()
        if resumed is not None:
            title = resumed.title
            sticky_skills = resumed.sticky_skills
            if resumed.pending_user_message is not None:
                initial_messages = resumed.messages[:-1]
                print_terminal_event("pending_user_discarded", {})
                assert session_file is not None
                append_session_event(
                    session_file,
                    "session.recovered",
                    {"kind": "pending_user_discarded"},
                )
            elif resumed.tool_batch_state is not None:
                initial_messages = build_tool_batch_recovery_messages(resumed.messages)
                print_terminal_event(
                    "tool_batch_recovery",
                    {
                        "completed": len(resumed.tool_batch_state.completed_call_ids),
                        "pending": len(resumed.tool_batch_state.pending_call_ids),
                    },
                )
                assert session_file is not None
                append_session_event(
                    session_file,
                    "session.recovered",
                    {"kind": "tool_batch_closed"},
                )
            else:
                initial_messages = resumed.messages

        pending_activations: list[SkillSnapshot] = []

        def flush_activations() -> None:
            if session_file is None or not Path(session_file).exists():
                return
            while pending_activations:
                snapshot = pending_activations[0]
                append_session_event(
                    session_file,
                    "skill.activated",
                    {
                        "name": snapshot.name,
                        "display_name": snapshot.display_name,
                        "source": snapshot.source,
                        "sha256": snapshot.sha256,
                        "content": snapshot.content,
                    },
                )
                pending_activations.pop(0)

        def record_activation(snapshot: SkillSnapshot) -> None:
            pending_activations.append(snapshot)
            flush_activations()

        skill_runtime = SkillRuntime(
            sticky_names=sticky_skills,
            activation_callback=record_activation if session_file is not None else None,
        )

        previous_state = resumed.task_state if resumed is not None else None
        if previous_state is not None and previous_state.get("status") == "completed":
            previous_state = None
        verification_runtime = VerificationRuntime(
            args.workspace,
            requires_verification=resolve_task_type(
                args.task_type,
                previous=previous_state,
                interactive=args.interactive_terminal,
            ),
            initial_state=previous_state,
            approval_callback=request_verification_approval,
            internal_paths=(
                (Path(session_file), lease.lock_path)
                if session_file is not None and lease is not None
                else ()
            ),
        )
        last_task_state: dict[str, object] | None = None
        render_event = deduplicate_tool_execution(print_terminal_event)

        def event_callback(event_type: str, payload: dict[str, object]) -> None:
            nonlocal last_task_state
            if event_type == "task_state":
                state = validate_task_state(payload)
                if session_file is not None:
                    append_session_event(session_file, "task.state", state)
                last_task_state = state
            elif event_type == "final_answer" and (
                last_task_state is None
                or last_task_state.get("status") not in {
                    "completed", "incomplete", "failed", "interrupted",
                }
            ):
                raise RuntimeError("缺少可信任务终态，不能展示完成结果")
            render_event(event_type, payload)

        message_snapshot_callback = None
        if session_file is not None:
            workspace = args.workspace
            model = settings.model

            def message_snapshot_callback(messages: list[dict[str, object]]) -> None:
                save_session(
                    session_file,
                    workspace,
                    model,
                    messages,
                    title,
                    skill_runtime.sticky_names,
                )
                flush_activations()

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
            event_callback=event_callback,
            message_snapshot_callback=message_snapshot_callback,
            initial_messages=initial_messages,
            token_counter=token_counter,
            skill_runtime=skill_runtime,
            verification_runtime=verification_runtime,
            **kwargs,
        )
        if last_task_state is not None and last_task_state.get("status") == "completed":
            return 0
        return 1
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
