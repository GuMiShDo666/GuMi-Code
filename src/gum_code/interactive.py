"""Own the terminal REPL and its durable, locked Session lifecycle."""

import copy
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import choice, prompt
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from gum_code.agent import build_agent_tool_schemas, run_agent
from gum_code.context import compact_history, latest_user_request
from gum_code.config import (
    Settings,
    default_user_config_path,
    has_complete_legacy_settings,
    legacy_public_defaults,
    load_settings,
    resolve_context_budget,
    resolve_context_window,
    save_user_settings,
    validate_provider_settings,
)
from gum_code.events import RichEventRenderer, context_percentage, create_console
from gum_code.permissions.terminal import make_interactive_approval
from gum_code.session import (
    LEGACY_SESSION_VERSION,
    MAX_SESSION_TITLE_CHARS,
    SessionData,
    SessionLease,
    build_interrupted_turn_messages,
    build_tool_batch_recovery_messages,
    load_session,
    save_session,
)
from gum_code.token_counting import load_token_counter
from gum_code.tools.command import load_docker_sandbox
from gum_code.user_state import (
    SessionEntry,
    UserState,
    allocate_session_path,
    create_workspace,
    delete_session,
    import_session,
    list_sessions,
    load_user_state,
    mark_legacy_migration_decided,
    record_workspace,
    remove_recent,
    revoke_trust,
    save_user_state,
    workspace_creation_target,
)

KNOWN_COMMANDS = {
    "/help",
    "/compact",
    "/workspace",
    "/new",
    "/resume",
    "/sessions",
    "/status",
    "/verbose",
    "/exit",
}
_SESSION_CANCELLED = object()


def _safe_terminal_text(value: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "?", str(value))


def display_path(path: Path) -> str:
    resolved = Path(path).resolve()
    try:
        relative = resolved.relative_to(Path.home().resolve())
    except ValueError:
        return _safe_terminal_text(resolved)
    displayed = "~" if not relative.parts else str(Path("~") / relative)
    return _safe_terminal_text(displayed)


def repl_key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("enter")
    def _submit(event: object) -> None:
        event.current_buffer.validate_and_handle()  # type: ignore[attr-defined]

    @bindings.add("escape", "enter")
    def _newline(event: object) -> None:
        event.current_buffer.insert_text("\n")  # type: ignore[attr-defined]

    return bindings


def parse_repl_input(value: str) -> tuple[str, str | None]:
    text = value.strip()
    if not text:
        return "empty", None
    if text.startswith("//"):
        return "task", text[1:]
    if text.startswith("/"):
        command = text.split(maxsplit=1)[0].lower()
        if command in KNOWN_COMMANDS and text == command:
            return "command", command
        return "unknown", command
    return "task", text


def _confirm(message: str, chooser: Callable[..., object] = choice) -> bool:
    try:
        return chooser(
            message,
            options=[(False, "取消"), (True, "确认")],
            default=False,
        ) is True
    except (Exception, KeyboardInterrupt, EOFError):
        return False


def _choose_directory(
    start: Path,
    *,
    chooser: Callable[..., object] = choice,
) -> Path | None:
    current = Path(start).expanduser().resolve()
    show_hidden = False
    while True:
        children = []
        try:
            children = sorted(
                (
                    path
                    for path in current.iterdir()
                    if path.is_dir() and (show_hidden or not path.name.startswith("."))
                ),
                key=lambda path: path.name.casefold(),
            )
        except OSError:
            children = []
        options: list[tuple[object, str]] = [
            (
                ("select", current),
                f"选择当前目录：{_safe_terminal_text(current)}",
            ),
            (("parent", None), "上一级"),
            (
                ("hidden", None),
                "隐藏目录：" + ("已显示" if show_hidden else "已隐藏"),
            ),
            (("cancel", None), "取消"),
        ]
        options.extend(
            (("open", child), _safe_terminal_text(child.name))
            for child in children
        )
        try:
            action, selected = chooser("浏览工作区", options=options)
        except (KeyboardInterrupt, EOFError):
            return None
        if action == "select":
            return current
        if action == "parent":
            current = current.parent
        elif action == "hidden":
            show_hidden = not show_hidden
        elif action == "open":
            current = selected
        else:
            return None


def select_workspace(
    console: Console,
    *,
    state_file: Path | None = None,
    chooser: Callable[..., object] = choice,
    text_prompt: Callable[..., str] = prompt,
) -> Path | None:
    state = load_user_state(state_file)
    while True:
        options: list[tuple[object, str]] = []
        default: object | None = None
        for path in state.recent:
            suffix = " [trusted]" if path in state.trusted else ""
            if path == state.last_used:
                suffix += " [上次使用]"
                default = ("recent", Path(path))
            options.append(
                (("recent", Path(path)), display_path(Path(path)) + suffix)
            )
        options.extend(
            [
                (("browse", None), "浏览目录"),
                (("create", None), "新建空工作区"),
                (("remove", None), "从最近列表移除"),
                (("revoke", None), "撤销信任"),
                (("cancel", None), "退出"),
            ]
        )
        try:
            action, value = chooser(
                "选择工作区", options=options, default=default
            )
        except (KeyboardInterrupt, EOFError):
            return None

        selected: Path | None = None
        trusted_on_create = False
        if action == "recent":
            selected = value
        elif action == "browse":
            selected = _choose_directory(Path.cwd(), chooser=chooser)
        elif action == "create":
            parent = _choose_directory(Path.cwd(), chooser=chooser)
            if parent is not None:
                try:
                    name = text_prompt("新目录名：").strip()
                    target = workspace_creation_target(parent, name)
                    console.print(Text(f"将创建：{_safe_terminal_text(target)}"))
                    if _confirm("创建并信任该工作区？", chooser):
                        selected = create_workspace(parent, name)
                        trusted_on_create = True
                except (OSError, ValueError) as error:
                    console.print(
                        Text(
                            f"创建失败：{_safe_terminal_text(error)}",
                            style="red",
                        )
                    )
                except (KeyboardInterrupt, EOFError):
                    continue
        elif action in {"remove", "revoke"}:
            candidates = state.recent if action == "remove" else state.trusted
            if not candidates:
                console.print(Text("没有可操作的工作区。", style="yellow"))
                continue
            try:
                target = chooser(
                    "选择工作区",
                    options=[
                        (Path(path), display_path(Path(path)))
                        for path in candidates
                    ],
                )
            except (KeyboardInterrupt, EOFError):
                continue
            state = (
                remove_recent(state, target)
                if action == "remove"
                else revoke_trust(state, target)
            )
            save_user_state(state, state_file)
            continue
        else:
            return None

        if selected is None:
            continue
        selected = selected.expanduser().resolve()
        if not selected.is_dir():
            console.print(Text("工作区不存在或不是目录。", style="red"))
            continue
        if str(selected) not in state.trusted and not trusted_on_create:
            console.print(Text(f"完整路径：{_safe_terminal_text(selected)}"))
            if not _confirm("信任该工作区？", chooser):
                continue
        state = record_workspace(state, selected, trusted=True)
        save_user_state(state, state_file)
        return selected


def trust_workspace(
    workspace: Path,
    console: Console,
    *,
    state_file: Path | None = None,
    chooser: Callable[..., object] = choice,
) -> Path:
    resolved = Path(workspace).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"workspace 不存在或不是目录：{resolved}")
    state = load_user_state(state_file)
    if str(resolved) not in state.trusted:
        console.print(Text(f"完整路径：{_safe_terminal_text(resolved)}"))
        if not _confirm("信任该工作区？", chooser):
            raise RuntimeError("工作区未获得信任")
    save_user_state(record_workspace(state, resolved, trusted=True), state_file)
    return resolved


def _configuration_wizard(
    console: Console,
    *,
    config_file: Path | None = None,
    text_prompt: Callable[..., str] = prompt,
    chooser: Callable[..., object] = choice,
    default_base_url: str = "",
    default_model: str = "",
) -> Settings:
    target = default_user_config_path() if config_file is None else config_file
    while True:
        try:
            api_key = text_prompt("LLM API key：", is_password=True).strip()
            base_url = text_prompt(
                "LLM base URL：", default=default_base_url
            ).strip()
            model = text_prompt("LLM model：", default=default_model).strip()
            settings = Settings(api_key=api_key, base_url=base_url, model=model)
        except (KeyboardInterrupt, EOFError):
            raise RuntimeError("已取消 provider 配置") from None

        while True:
            try:
                with console.status("正在验证 provider…"):
                    validate_provider_settings(settings)
                save_user_settings(target, settings)
                return settings
            except (KeyboardInterrupt, EOFError):
                raise RuntimeError("已取消 provider 配置") from None
            except (RuntimeError, OSError, ValueError):
                console.print(
                    Text("Provider 配置验证失败，未保存。", style="red")
                )
                try:
                    action = chooser(
                        "Provider 配置失败",
                        options=[
                            ("retry", "使用相同配置重试"),
                            ("edit", "编辑配置"),
                            ("exit", "退出"),
                        ],
                        default="retry",
                    )
                except (Exception, KeyboardInterrupt, EOFError):
                    action = "exit"
                if action == "retry":
                    continue
                if action == "edit":
                    default_base_url = settings.base_url
                    default_model = settings.model
                    break
                raise RuntimeError("Provider 配置未完成") from None


def ensure_interactive_settings(
    console: Console,
    *,
    startup_directory: Path,
    config_file: Path | None = None,
    state_file: Path | None = None,
) -> Settings:
    target = default_user_config_path() if config_file is None else config_file
    try:
        settings = load_settings(target, startup_directory / ".env")
    except RuntimeError:
        base_url, model = legacy_public_defaults(startup_directory / ".env")
        return _configuration_wizard(
            console,
            config_file=target,
            default_base_url=base_url,
            default_model=model,
        )

    state = load_user_state(state_file)
    legacy = startup_directory / ".env"
    if (
        not target.exists()
        and has_complete_legacy_settings(legacy)
        and not state.legacy_migration_decided
    ):
        migrate = _confirm(
            "检测到项目级旧配置。"
            "是否重新输入并保存到用户配置？"
        )
        state = mark_legacy_migration_decided(state)
        save_user_state(state, state_file)
        if migrate:
            base_url, model = legacy_public_defaults(legacy)
            return _configuration_wizard(
                console,
                config_file=target,
                default_base_url=base_url,
                default_model=model,
            )
    return settings


def _prompt_context_window(
    console: Console,
    *,
    text_prompt: Callable[..., str] = prompt,
) -> int:
    while True:
        try:
            value = int(text_prompt("模型上下文窗口 token 数：").strip())
        except (KeyboardInterrupt, EOFError):
            raise RuntimeError("已取消模型上下文窗口配置") from None
        except ValueError:
            value = 0
        if value > 0:
            return value
        console.print(Text("模型上下文窗口必须是正整数。", style="red"))


def sanitize_title(value: object) -> str:
    if not isinstance(value, str):
        return ""
    title = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    title = " ".join(title.strip().strip("'\"“”").split())
    return title[:MAX_SESSION_TITLE_CHARS]


def fallback_title(task: str) -> str:
    prefix = datetime.now().strftime("%H:%M ")
    return sanitize_title(prefix + task)[:MAX_SESSION_TITLE_CHARS] or prefix.strip()


def generate_title(client: object, model: str, first_task: str) -> str:
    try:
        title_client = client
        with_options = getattr(client, "with_options", None)
        if callable(with_options):
            title_client = with_options(timeout=10, max_retries=0)
        response = title_client.chat.completions.create(  # type: ignore[attr-defined]
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Create a concise session title of at most 30 characters. "
                        "Return only the title."
                    ),
                },
                {"role": "user", "content": first_task},
            ],
            max_tokens=32,
            timeout=10,
        )
        title = sanitize_title(response.choices[0].message.content)
        return title or fallback_title(first_task)
    except (Exception, KeyboardInterrupt):
        return fallback_title(first_task)


def _session_choice(
    workspace: Path,
    *,
    chooser: Callable[..., object] = choice,
) -> Path | None | object:
    entries = list_sessions(workspace=workspace, limit=20)
    if not entries:
        return None
    try:
        action = chooser(
            "选择 Session",
            options=[
                ("new", "新建 Session"),
                ("latest", f"继续最近：{_safe_terminal_text(entries[0].title)}"),
                ("history", "查看 Session 历史"),
            ],
            default="new",
        )
    except (KeyboardInterrupt, EOFError):
        return _SESSION_CANCELLED
    if action == "latest":
        return entries[0].path
    if action == "history":
        try:
            return chooser(
                "最近 20 个 Session",
                options=[
                    (entry.path, _safe_terminal_text(entry.title))
                    for entry in entries
                ],
            )
        except (KeyboardInterrupt, EOFError):
            return _SESSION_CANCELLED
    return None


def _choose_session_entry(
    workspace: Path | None,
    *,
    chooser: Callable[..., object] = choice,
) -> SessionEntry | None:
    entries = list_sessions(workspace=workspace)
    if not entries:
        return None
    try:
        return chooser(
            "选择 Session",
            options=[
                (
                    entry,
                    f"{_safe_terminal_text(entry.title)} — "
                    f"{display_path(entry.workspace)}",
                )
                for entry in entries
            ],
        )
    except (KeyboardInterrupt, EOFError):
        return None


def _copy_legacy_session_for_resume(
    source: Path,
    data: SessionData,
    source_lease: SessionLease,
    console: Console,
) -> tuple[Path, SessionData, SessionLease]:
    destination = allocate_session_path()
    console.print(
        Text(
            "v1 Session 需要先复制导入：\n"
            f"源：{_safe_terminal_text(source.resolve())}\n"
            f"目标：{_safe_terminal_text(destination.resolve())}",
            style="yellow",
        )
    )
    if not _confirm("复制导入后恢复？"):
        raise RuntimeError("v1 Session 未导入，源文件保持不变")

    destination_lease = SessionLease(destination).acquire()
    try:
        save_session(
            destination,
            data.workspace,
            data.model,
            data.messages,
            data.title,
        )
        copied = load_session(
            destination,
            allow_incomplete_tool_batch=True,
            allow_pending_user_tail=True,
        )
    except BaseException:
        destination_lease.release()
        raise
    source_lease.release()
    return destination, copied, destination_lease


@dataclass
class InteractiveRuntime:
    workspace: Path
    settings: Settings
    client: object
    console: Console
    args: object
    token_counter: object
    command_sandbox: object | None
    messages: list[dict[str, object]] | None = None
    session_path: Path | None = None
    lease: SessionLease | None = None
    title: str = ""
    first_task: str | None = None
    verbose: bool = False

    def close(self) -> None:
        if self.lease is not None:
            self.lease.release()
            self.lease = None

    def _ensure_session(self) -> None:
        if self.session_path is None:
            self.session_path = allocate_session_path()
        if self.lease is None:
            self.lease = SessionLease(self.session_path).acquire()

    def _save(self, messages: list[dict[str, object]]) -> None:
        self._ensure_session()
        candidate = copy.deepcopy(messages)
        save_session(
            self.session_path,
            self.workspace,
            self.settings.model,
            candidate,
            self.title,
        )
        self.messages = candidate

    def load_locked(self, path: Path) -> None:
        """Validate and lock a destination before releasing the current Session."""
        destination = Path(path).resolve()
        if self.session_path is not None and destination == self.session_path.resolve():
            return
        lease = SessionLease(destination).acquire()
        try:
            data = load_session(
                destination,
                allow_incomplete_tool_batch=True,
                allow_pending_user_tail=True,
            )
            if data.model != self.settings.model:
                raise ValueError("Session model 与当前 provider 不匹配")
            destination_workspace = data.workspace.resolve()
            if destination_workspace != self.workspace:
                trust_workspace(destination_workspace, self.console)
            if data.version == LEGACY_SESSION_VERSION:
                destination, data, lease = _copy_legacy_session_for_resume(
                    destination,
                    data,
                    lease,
                    self.console,
                )
        except BaseException:
            lease.release()
            raise
        self.close()
        self.workspace = data.workspace.resolve()
        self.session_path = destination.resolve()
        self.lease = lease
        self.title = data.title
        self.first_task = next(
            (
                str(message.get("content"))
                for message in data.messages
                if message.get("role") == "user"
                and isinstance(message.get("content"), str)
            ),
            None,
        )
        self.messages = self._recover_loaded_data(data)

    def _recover_failed_turn(
        self,
        latest: list[dict[str, object]] | None,
        completed: list[dict[str, object]] | None,
    ) -> None:
        if latest is None:
            return
        try:
            recovered = build_interrupted_turn_messages(latest)
            if recovered == latest and latest[-1].get("role") == "user":
                recovered = (
                    copy.deepcopy(completed)
                    if completed is not None
                    else latest[:-1]
                )
            self._save(recovered)
            if not any(message.get("role") == "user" for message in recovered):
                self.first_task = None
        except (IndexError, ValueError):
            if completed is not None:
                self._save(completed)

    def _recover_loaded_data(self, data: SessionData) -> list[dict[str, object]]:
        if data.pending_user_message is not None:
            preview = _safe_terminal_text(
                data.pending_user_message.replace("\n", " ")[:160]
            )
            self.console.print(
                Text(f"上次未发送的用户消息：{preview}", style="yellow")
            )
            resend = _confirm("重新发送这条消息？")
            base = copy.deepcopy(data.messages[:-1])
            if resend:
                self.messages = base
                self.run_turn(data.pending_user_message)
                return self.messages or base
            save_session(
                self.session_path,
                data.workspace,
                data.model,
                base,
                data.title,
            )
            return base
        if data.tool_batch_state is not None:
            recovered = build_tool_batch_recovery_messages(data.messages)
            save_session(
                self.session_path,
                data.workspace,
                data.model,
                recovered,
                data.title,
            )
            self.console.print(
                Text("已安全闭合上次中断的工具批次。", style="yellow")
            )
            return recovered
        return copy.deepcopy(data.messages)

    def run_turn(self, task: str) -> None:
        """Run one turn with durable snapshots and safe interruption recovery."""
        self._ensure_session()
        if self.first_task is None:
            self.first_task = task
        completed = copy.deepcopy(self.messages)
        latest = copy.deepcopy(self.messages)

        def snapshot(messages: list[dict[str, object]]) -> None:
            nonlocal latest
            latest = copy.deepcopy(messages)
            # Persist boundaries so recovery can close unfinished calls without replay.
            self._save(messages)

        renderer = RichEventRenderer(self.console, verbose=self.verbose)
        kwargs: dict[str, object] = {}
        if self.command_sandbox is not None:
            kwargs["command_sandbox"] = self.command_sandbox
        if not self.args.allow_subagent:
            kwargs["allow_subagent"] = False
        try:
            answer = run_agent(
                client=self.client,
                model=self.settings.model,
                workspace=self.workspace,
                user_query=task,
                max_steps=self.args.max_steps,
                max_context_tokens=self.args.max_context_tokens,
                allow_run_command=self.args.allow_run_command,
                approval_callback=make_interactive_approval(
                    self.workspace, self.console
                ),
                event_callback=renderer,
                message_snapshot_callback=snapshot,
                initial_messages=self.messages,
                token_counter=self.token_counter,
                **kwargs,
            )
        except KeyboardInterrupt:
            renderer.close()
            self._recover_failed_turn(latest, completed)
            self.console.print(
                Text(
                    "本轮已中断；已完成的副作用未回滚。",
                    style="yellow",
                )
            )
            return
        except Exception:
            renderer.close()
            self._recover_failed_turn(latest, completed)
            raise
        finally:
            renderer.close()

        renderer.render_answer(answer)
        if not self.title and self.first_task is not None:
            with self.console.status("正在生成 Session 标题…"):
                self.title = generate_title(
                    self.client, self.settings.model, self.first_task
                )
            if self.messages is not None:
                self._save(self.messages)

    def new_session(self) -> None:
        self.close()
        self.messages = None
        self.session_path = None
        self.title = ""
        self.first_task = None

    def compact_context(self) -> bool:
        if not self.messages:
            self.console.print(Text("当前 Session 没有可整理的上下文。", style="yellow"))
            return False
        try:
            active_request = latest_user_request(self.messages)
        except RuntimeError:
            self.console.print(Text("上下文整理失败，已保留原 Session。", style="red"))
            return False
        if not active_request:
            self.console.print(Text("当前 Session 没有可整理的用户请求。", style="yellow"))
            return False
        tools = build_agent_tool_schemas(
            allow_run_command=bool(getattr(self.args, "allow_run_command", False)),
            allow_subagent=bool(getattr(self.args, "allow_subagent", True)),
        )
        before_tokens = self.token_counter.count_request(self.messages, tools)
        renderer = RichEventRenderer(self.console, verbose=self.verbose)
        renderer("context_summary", {"step": 1})
        try:
            compacted, _ = compact_history(
                self.client,
                self.settings.model,
                self.workspace,
                self.messages,
                active_request,
                tools=tools,
                token_counter=self.token_counter,
                max_context_tokens=self.args.max_context_tokens,
            )
            after_tokens = self.token_counter.count_request(compacted, tools)
            self._save(compacted)
        except Exception:
            renderer.close()
            self.console.print(Text("上下文整理失败，已保留原 Session。", style="red"))
            return False
        else:
            renderer(
                "context_compacted",
                {
                    "before_tokens": before_tokens,
                    "after_tokens": after_tokens,
                    "max_tokens": self.args.max_context_tokens,
                },
            )
            return True
        finally:
            renderer.close()

    def show_status(self, *, show_title: bool = False) -> None:
        table = Table(show_header=False, box=None)
        table.add_row("Workspace", Text(_safe_terminal_text(self.workspace)))
        table.add_row("Model", Text(_safe_terminal_text(self.settings.model)))
        session = self.title or (
            str(self.session_path) if self.session_path else "new"
        )
        table.add_row("Session", Text(_safe_terminal_text(session)))
        table.add_row("Sandbox", "on" if self.args.allow_run_command else "off")
        table.add_row("Subagent", "on" if self.args.allow_subagent else "off")
        if self.messages:
            tools = build_agent_tool_schemas(
                allow_run_command=self.args.allow_run_command,
                allow_subagent=self.args.allow_subagent,
            )
            used_tokens = self.token_counter.count_request(self.messages, tools)
            percent = context_percentage(used_tokens, self.args.max_context_tokens)
        else:
            percent = 0
        table.add_row("上下文占用", f"{percent}%")
        if show_title:
            self.console.print(Text("Gum Code", style="bold cyan"))
        self.console.print(Panel(table, border_style="cyan"))


def _manage_sessions(runtime: InteractiveRuntime) -> None:
    while True:
        entries = list_sessions()
        options: list[tuple[object, str]] = [(("back", None), "返回")]
        options.extend(
            (
                ("entry", entry),
                f"{_safe_terminal_text(entry.title)} — "
                f"{display_path(entry.workspace)}",
            )
            for entry in entries
        )
        options.append((("import", None), "导入旧 Session"))
        try:
            action, value = choice("Session 管理", options=options)
        except (KeyboardInterrupt, EOFError):
            return
        if action == "back":
            return
        if action == "import":
            try:
                source = (
                    Path(prompt("旧 Session JSON 路径："))
                    .expanduser()
                    .resolve()
                )
                destination = allocate_session_path()
                runtime.console.print(
                    Text(
                        f"源：{_safe_terminal_text(source)}\n"
                        f"目标：{_safe_terminal_text(destination)}"
                    )
                )
                if _confirm("复制导入？"):
                    imported = import_session(
                        source,
                        destination.parent,
                        destination=destination,
                    )
                    runtime.console.print(
                        Text(f"已导入：{_safe_terminal_text(imported)}")
                    )
            except (OSError, ValueError) as error:
                runtime.console.print(
                    Text(
                        f"导入失败：{_safe_terminal_text(error)}",
                        style="red",
                    )
                )
            continue

        entry: SessionEntry = value
        try:
            operation = choice(
                _safe_terminal_text(entry.title),
                options=[
                    ("view", "查看元数据"),
                    ("resume", "恢复"),
                    ("rename", "重命名"),
                    ("delete", "删除"),
                    ("back", "返回"),
                ],
            )
        except (KeyboardInterrupt, EOFError):
            continue
        if operation == "view":
            runtime.console.print(
                Text(
                    f"{display_path(entry.path)}\n"
                    f"{display_path(entry.workspace)}\n{entry.title}"
                )
            )
        elif operation == "resume":
            if entry.workspace != runtime.workspace:
                runtime.console.print(
                    Text(
                        f"目标工作区："
                        f"{_safe_terminal_text(entry.workspace)}"
                    )
                )
                if not _confirm("跨工作区恢复？"):
                    continue
            runtime.load_locked(entry.path)
            return
        elif operation == "rename":
            title = sanitize_title(prompt("新标题："))
            if not title:
                runtime.console.print(Text("标题不能为空。", style="red"))
                continue
            if runtime.session_path == entry.path:
                runtime.title = title
                if runtime.messages is not None:
                    runtime._save(runtime.messages)
            else:
                lease = SessionLease(entry.path).acquire()
                try:
                    data = load_session(
                        entry.path,
                        allow_incomplete_tool_batch=True,
                        allow_pending_user_tail=True,
                    )
                    save_session(
                        entry.path,
                        data.workspace,
                        data.model,
                        data.messages,
                        title,
                    )
                finally:
                    lease.release()
        elif operation == "delete":
            if runtime.session_path == entry.path:
                runtime.console.print(
                    Text("不能删除当前正在使用的 Session。", style="red")
                )
            elif _confirm("仅删除该 Session JSON？"):
                lease = SessionLease(entry.path).acquire()
                try:
                    delete_session(entry.path)
                finally:
                    lease.release()


def _handle_repl_command(runtime: InteractiveRuntime, command: str) -> bool:
    if command == "/exit":
        runtime.close()
        return True
    if command == "/help":
        runtime.console.print(Text(" ".join(sorted(KNOWN_COMMANDS))))
    elif command == "/compact":
        runtime.compact_context()
    elif command == "/status":
        runtime.show_status()
    elif command == "/verbose":
        runtime.verbose = not runtime.verbose
        runtime.console.print(Text(f"verbose: {'on' if runtime.verbose else 'off'}"))
    elif command == "/new":
        runtime.new_session()
        runtime.console.print(Text("已开始新 Session。"))
    elif command == "/resume":
        all_workspaces = _confirm("显示所有工作区的 Session？")
        entry = _choose_session_entry(None if all_workspaces else runtime.workspace)
        if entry is not None:
            if entry.workspace != runtime.workspace:
                runtime.console.print(
                    Text(
                        f"目标工作区："
                        f"{_safe_terminal_text(entry.workspace)}"
                    )
                )
                if not _confirm("跨工作区恢复？"):
                    return False
            runtime.load_locked(entry.path)
            runtime.show_status()
    elif command == "/sessions":
        _manage_sessions(runtime)
    elif command == "/workspace":
        selected = select_workspace(runtime.console)
        if selected is not None:
            chosen = _session_choice(selected)
            if chosen is _SESSION_CANCELLED:
                return False
            if chosen is None:
                runtime.new_session()
                runtime.workspace = selected
            else:
                runtime.load_locked(chosen)  # type: ignore[arg-type]
            runtime.show_status()
    return False


def run_repl(runtime: InteractiveRuntime) -> int:
    runtime.show_status(show_title=True)
    prompt_session: PromptSession[str] = PromptSession(
        history=InMemoryHistory(),
        multiline=True,
        key_bindings=repl_key_bindings(),
    )
    while True:
        try:
            value = prompt_session.prompt("gu> ")
        except KeyboardInterrupt:
            runtime.console.print(Text("已取消当前输入。"))
            continue
        except EOFError:
            runtime.close()
            return 0

        kind, payload = parse_repl_input(value)
        if kind == "empty":
            continue
        if kind == "unknown":
            runtime.console.print(
                Text(f"未知命令：{_safe_terminal_text(payload)}", style="red")
            )
            continue
        if kind == "task":
            try:
                runtime.run_turn(payload or "")
            except Exception:
                runtime.console.print(
                    Text("本轮失败，已保留可恢复的 Session。", style="red")
                )
            continue

        try:
            if _handle_repl_command(runtime, payload or ""):
                return 0
        except (KeyboardInterrupt, EOFError):
            runtime.console.print(Text("已取消当前命令。"))
        except (OSError, RuntimeError, ValueError):
            runtime.console.print(
                Text("命令失败，当前 Session 仍保持打开。", style="red")
            )


def run_interactive(args: object, *, startup_directory: Path | None = None) -> int:
    console = create_console()
    startup = Path.cwd() if startup_directory is None else Path(startup_directory)
    initial_lease: SessionLease | None = None
    session_path: Path | None = args.resume_session
    explicit_new_path: Path | None = args.session_file
    workspace: Path | None = args.workspace
    data: SessionData | None = None

    try:
        if session_path is not None:
            initial_lease = SessionLease(session_path).acquire()
            data = load_session(
                session_path,
                allow_incomplete_tool_batch=True,
                allow_pending_user_tail=True,
            )
            if (
                workspace is not None
                and workspace.resolve() != data.workspace.resolve()
            ):
                raise ValueError("Session workspace 与 --workspace 不匹配")
            workspace = data.workspace.resolve()
            workspace = trust_workspace(workspace, console)
        elif workspace is None:
            workspace = select_workspace(console)
            if workspace is None:
                return 0
        else:
            workspace = trust_workspace(workspace, console)

        if session_path is None and explicit_new_path is None:
            session_path = _session_choice(workspace)
            if session_path is _SESSION_CANCELLED:
                return 0

        settings = ensure_interactive_settings(
            console,
            startup_directory=startup,
        )
        if session_path is not None and data is None:
            initial_lease = SessionLease(session_path).acquire()
            data = load_session(
                session_path,
                allow_incomplete_tool_batch=True,
                allow_pending_user_tail=True,
            )
        if data is not None:
            if data.workspace.resolve() != workspace.resolve():
                raise ValueError("Session workspace 不匹配")
            if data.model != settings.model:
                raise ValueError("Session model 与当前 provider 不匹配")
            if data.version == LEGACY_SESSION_VERSION:
                if session_path is None or initial_lease is None:
                    raise RuntimeError("v1 Session 恢复状态无效")
                session_path, data, initial_lease = (
                    _copy_legacy_session_for_resume(
                        session_path,
                        data,
                        initial_lease,
                        console,
                    )
                )
        client = OpenAI(api_key=settings.api_key, base_url=settings.base_url)
        try:
            context_window = resolve_context_window(settings, client)
        except RuntimeError:
            context_window = _prompt_context_window(console)
            settings = replace(settings, context_window=context_window)
            save_user_settings(default_user_config_path(), settings)
        else:
            settings = replace(settings, context_window=context_window)
        args.max_context_tokens = resolve_context_budget(
            context_window, args.max_context_tokens
        )

        command_sandbox = None
        if args.allow_run_command:
            command_sandbox = load_docker_sandbox(args.sandbox_image)
        runtime = InteractiveRuntime(
            workspace=workspace,
            settings=settings,
            client=client,
            console=console,
            args=args,
            token_counter=load_token_counter(settings.model),
            command_sandbox=command_sandbox,
            session_path=session_path or explicit_new_path,
            lease=initial_lease,
            title=data.title if data else "",
            first_task=next(
                (
                    str(message.get("content"))
                    for message in (data.messages if data else [])
                    if message.get("role") == "user"
                    and isinstance(message.get("content"), str)
                ),
                None,
            ),
        )
        initial_lease = None
        if data is not None:
            runtime.messages = runtime._recover_loaded_data(data)
        try:
            return run_repl(runtime)
        except (KeyboardInterrupt, EOFError):
            runtime.close()
            return 0
    finally:
        if initial_lease is not None:
            initial_lease.release()
