"""Own the terminal REPL and its durable, locked Session lifecycle."""

import copy
import re
import sys
import uuid
from dataclasses import dataclass, field, replace
from threading import RLock
from datetime import datetime
from pathlib import Path
from typing import Callable

from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import choice, prompt
from prompt_toolkit.styles import Style
from prompt_toolkit.output import ColorDepth
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from gum_code.agent import build_agent_tool_schemas, run_agent
from gum_code.context import compact_history, latest_user_request
from gum_code.config import (
    DEFAULT_VISION_CALL_LIMIT,
    Settings,
    VisionSettings,
    default_user_config_path,
    default_vision_config_path,
    has_complete_legacy_settings,
    legacy_public_defaults,
    load_settings,
    load_vision_settings,
    resolve_context_budget,
    resolve_context_window,
    save_user_settings,
    save_vision_settings,
    validate_provider_settings,
    validate_vision_settings,
)
from gum_code.events import (
    RichEventRenderer,
    context_percentage,
    create_console,
    environment_cleanup_text,
    task_outcome_text,
)
from gum_code.permissions.terminal import (
    make_interactive_approval,
    request_environment_preparation,
)
from gum_code.session import (
    MAX_SESSION_TITLE_CHARS,
    SESSION_VERSION,
    SessionData,
    SessionLease,
    append_session_event,
    build_interrupted_turn_messages,
    build_tool_batch_recovery_messages,
    completed_session_turns,
    fork_session,
    load_session,
    migrate_session_to_v3,
    save_session,
    session_audit_entries,
    iter_session_audit_entries,
    session_internal_paths,
    session_history_usage,
)
from gum_code.skills import SKILL_SECTION_END, SKILL_SECTION_START, SkillRuntime, SkillSnapshot
from gum_code.token_counting import load_token_counter
from gum_code.tools.command import load_docker_sandbox
from gum_code.tools.registry import READ_ONLY_TOOL_NAMES
from gum_code.self_check import (
    AUTOMATIC_SELF_CHECK_ENABLED,
    SelfCheckRuntime,
    validate_task_outcome,
)
from gum_code.vision import VisionRuntime
from gum_code.todo import PlanRuntime
from gum_code.project_runtime import ProjectRuntime
from gum_code.project_browser import ProjectBrowser
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
    "/skills",
    "/status",
    "/revoke-verification",
    "/verbose",
    "/vision",
    "/exit",
}
_SESSION_CANCELLED = object()


_STARTUP_BIRD = """
                   ⢦⣀
                   ⠈⣿⣷⣄
                    ⠘⣿⣿⣷⣄
    ⠐⠶⣶⣶⣶⣤⣤⣤⣤⣀⣀⣀     ⠘⣿⣿⣿⣷⡄
       ⠉⠛⠿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣦⣄ ⠸⣿⣿⣿⡇
           ⠙⠻⣿⣿⣿⣿⣿⣿⣿⣿⣷⣄⢹⣿⣿⠁⣀⣠⣄⣀
              ⠹⣿⣿⣿⣿⣿⣿⣿⣿⣷⣿⣿⣿⣿⣿⣿⡟⠃
               ⠘⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠏
               ⣀⣘⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠃
          ⢀⣠⣴⣾⣿⣿⠏⠈⣿⣿⣿⣿⣿⣿⣿⣿⠏
       ⣀⣤⠾⠟⠛⠋⠉⣽⠏⣠⣾⣿⣿⣿⠿⠟⠛⠉
             ⣼⣣⣾⡿⠟⠋⠉
            ⣸⠿⠋⠁
""".strip("\n")
_SMALL_STARTUP_BIRD = """
          ⠰⣄⡀
           ⠹⣿⣦⡀
  ⠈⠉⠛⢿⣿⣷⣶⣶⣤⣀⢹⣿⡇
      ⠈⠙⣿⣿⣿⣿⣷⣿⣧⣶⣶⠄
        ⠈⣿⣿⣿⣿⣿⣿⡿⠃
     ⣠⣴⠾⡿⢋⣿⣿⣿⡿⠟
    ⠉⠁ ⣼⡵⠟⠋⠉
       ⠉
""".strip("\n")


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


def resolve_task_type(
    requested: str | None,
    *,
    previous: dict[str, object] | None = None,
    interactive: bool = False,
    chooser: Callable[..., object] | None = None,
) -> bool | None:
    """Accept the retired public argument without selecting any runtime mode."""
    if requested not in {None, "development", "read-only"}:
        raise ValueError("task-type 必须是 development 或 read-only")
    return None


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
                renderer = RichEventRenderer(console)
                try:
                    renderer("model_call", {})
                    validate_provider_settings(settings)
                finally:
                    renderer.close()
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
        settings = _configuration_wizard(
            console,
            config_file=target,
            default_base_url=base_url,
            default_model=model,
        )
        _offer_vision_configuration(console)
        return settings

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
            settings = _configuration_wizard(
                console,
                config_file=target,
                default_base_url=base_url,
                default_model=model,
            )
            _offer_vision_configuration(console)
            return settings
    return settings


def _vision_consent(settings: VisionSettings, chooser: Callable[..., object]) -> bool:
    recipient = _safe_terminal_text(settings.base_url).replace(settings.api_key, "***")
    model = _safe_terminal_text(settings.model).replace(settings.api_key, "***")
    warning = " HTTP 未加密，图片可能被截获。" if settings.base_url.lower().startswith("http:") else ""
    return _confirm(
        f"接收方：{recipient}；模型：{model}。将发送当前任务相关图片/PDF 页面，"
        f"调用可能收费；次数上限不是费用上限。{warning} 确认启用？", chooser,
    )


def _vision_configuration_wizard(
    console: Console, *, config_file: Path | None = None,
    text_prompt: Callable[..., str] | None = None,
    chooser: Callable[..., object] | None = None,
) -> bool:
    """Save local consent only; never probe an endpoint or echo a credential."""
    target = default_vision_config_path() if config_file is None else config_file
    ask, choose = text_prompt or prompt, chooser or choice
    previous = load_vision_settings(target)
    try:
        key = ask("视觉 API key（不显示）：", is_password=True).strip()
        base = ask("视觉 base URL：", default=(
            previous.base_url.replace(previous.api_key, "***") if previous else ""
        )).strip()
        model = ask("视觉 model：", default=(
            previous.model.replace(previous.api_key, "***") if previous else ""
        )).strip()
        candidate = VisionSettings(key, base, model, default_call_limit=(
            previous.default_call_limit if previous else DEFAULT_VISION_CALL_LIMIT
        ))
        validate_vision_settings(candidate)
        if not _vision_consent(candidate, choose):
            return False
        save_vision_settings(target, replace(candidate, enabled=True))
    except (Exception, KeyboardInterrupt, EOFError):
        console.print(Text("图片解析配置未保存。", style="yellow"))
        return False
    console.print(Text("图片解析配置已保存；尚未验证服务的图片理解能力。"))
    return True


def _offer_vision_configuration(console: Console) -> None:
    if _confirm("主模型配置已保存。现在配置可选的图片解析功能？", choice):
        _vision_configuration_wizard(console)


def manage_vision_settings(console: Console) -> None:
    """A host-only no-argument menu; updates apply to subsequent requests."""
    target = default_vision_config_path()
    while True:
        settings = load_vision_settings(target)
        state = "未配置" if settings is None else "已启用" if settings.enabled else "已关闭"
        console.print(Text(f"图片解析：{state}"))
        if settings is not None:
            for label, value in (("接收方", settings.base_url), ("模型", settings.model)):
                console.print(Text(f"{label}：{_safe_terminal_text(value).replace(settings.api_key, '***')}"))
            console.print(Text(f"后续新请求默认次数：{settings.default_call_limit}"))
        try:
            action = choice("图片解析设置", options=[
                ("back", "返回"), ("configure", "配置/修改"),
                ("toggle", "启用/关闭"), ("limit", "修改后续新请求默认次数"),
            ], default="back")
            if action == "back" or action not in {"configure", "toggle", "limit"}:
                return
            if action == "configure":
                _vision_configuration_wizard(console, config_file=target)
            elif settings is None:
                console.print(Text("请先配置图片解析服务。", style="yellow"))
            elif action == "toggle":
                if settings.enabled:
                    save_vision_settings(target, replace(settings, enabled=False))
                    console.print(Text("已关闭后续图片发送；已经发送或在途的数据无法撤回。"))
                elif _vision_consent(settings, choice):
                    save_vision_settings(target, replace(settings, enabled=True))
            else:
                count = int(prompt("后续新请求默认次数（正整数；取消不更改）：").strip())
                if count <= 0:
                    raise ValueError
                save_vision_settings(target, replace(settings, default_call_limit=count))
        except (KeyboardInterrupt, EOFError):
            return
        except Exception:
            console.print(Text("图片解析设置未更改。", style="yellow"))


def request_extra_vision_calls(
    runtime: VisionRuntime, *, console: Console | None = None,
    interactive: bool | None = None, text_prompt: Callable[..., str] | None = None,
) -> None:
    """Ask only from the host's completed root batch; denial never grants calls."""
    if interactive is None:
        interactive = bool(sys.stdin.isatty() and sys.stdout.isatty())
    if not interactive:
        runtime.decline_extra()
        return
    try:
        state = runtime.export_state()
        (console or create_console()).print(Text(
            f"本轮图片解析已使用 {state['used']}/{state['limit']} 次。"
            "追加调用可能产生费用，次数不是金额上限。"
        ))
        count = int((text_prompt or prompt)("追加次数（正整数；留空拒绝）：").strip())
        if count <= 0:
            raise ValueError
    except (Exception, KeyboardInterrupt, EOFError):
        runtime.decline_extra()
        return
    if not runtime.add_credits(count):
        runtime.decline_extra()


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
            "旧版 Session 需要先复制导入为 v3 JSONL：\n"
            f"源：{_safe_terminal_text(source.resolve())}\n"
            f"目标：{_safe_terminal_text(destination.resolve())}",
            style="yellow",
        )
    )
    if not _confirm("复制导入后恢复？"):
        raise RuntimeError("旧版 Session 未导入，源文件保持不变")

    destination_lease = SessionLease(destination).acquire()
    try:
        migrate_session_to_v3(source, destination)
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
    sticky_skills: tuple[str, ...] = ()
    skill_runtime: SkillRuntime | None = None
    pending_skill_activations: list[SkillSnapshot] | None = None
    verification_runtime: object | None = None  # Retired constructor argument.
    task_type_consumed: bool = False
    last_task_state: dict[str, object] | None = None
    vision_runtime: VisionRuntime | None = None
    session_write_lock: object = field(default_factory=RLock, repr=False)
    self_check_runtime: SelfCheckRuntime | None = None
    last_task_outcome: dict[str, object] | None = None
    plan_runtime: PlanRuntime = field(default_factory=PlanRuntime)
    project_runtime: ProjectRuntime | None = None
    history_warning_level: int = 0

    def _close_project(self) -> bool:
        if self.project_runtime is not None:
            browser_failures = self.project_runtime.browser.close()
            service_failures = self.project_runtime.close()
            if browser_failures or service_failures:
                self.console.print(Text("项目运行资源清理未完成，请检查 Docker；数据库数据未删除。", style="yellow"))
                return False
            self.project_runtime = None
        return True

    def close(self) -> bool:
        if not self._close_project():
            return False
        if self.vision_runtime is not None:
            self.vision_runtime.cancel()
        self.vision_runtime = None
        self.verification_runtime = None
        if self.self_check_runtime is not None:
            self.self_check_runtime.cancel()
        self.self_check_runtime = None
        if self.lease is not None:
            self.lease.release()
            self.lease = None
        return True

    def _ensure_session(self) -> None:
        if self.session_path is None:
            self.session_path = allocate_session_path()
        if self.lease is None:
            self.lease = SessionLease(self.session_path).acquire()

    def _warn_history(self) -> None:
        if self.session_path is None or not self.session_path.exists():
            return
        try:
            usage = session_history_usage(self.session_path)
        except (OSError, ValueError):
            return  # Save/load owns persistence failures; this is an advisory only.
        if usage["warning_level"] > self.history_warning_level:
            self.console.print(Text("会话历史占用较大；旧记录已保留，可由你决定是否清理。", style="yellow"))
            self.history_warning_level = usage["warning_level"]

    def _save(
        self, messages: list[dict[str, object]], *, user_request_id: str | None = None,
    ) -> None:
        with self.session_write_lock:
            self._ensure_session()
            candidate = copy.deepcopy(messages)
            kwargs = {"user_request_id": user_request_id} if user_request_id is not None else {}
            save_session(
                self.session_path, self.workspace, self.settings.model, candidate, self.title,
                self.skill_runtime.sticky_names if self.skill_runtime is not None else self.sticky_skills,
                **kwargs,
            )
            internal_paths = session_internal_paths(self.session_path)
            if self.self_check_runtime is not None:
                self.self_check_runtime.set_internal_paths(internal_paths)
            if self.project_runtime is not None:
                self.project_runtime.internal_paths = internal_paths
            self.messages = candidate
            self._flush_skill_activations()

    def _append_event(self, event_type: str, data: dict[str, object]) -> None:
        with self.session_write_lock:
            if self.session_path is None:
                raise RuntimeError("Session 尚未建立")
            append_session_event(self.session_path, event_type, data)
            internal_paths = session_internal_paths(self.session_path)
            if self.self_check_runtime is not None:
                self.self_check_runtime.set_internal_paths(internal_paths)
            if self.project_runtime is not None:
                self.project_runtime.internal_paths = internal_paths

    def _persist_vision(self, state: dict[str, object]) -> None:
        # Called under the budget lock: never read runtime state or render here.
        self._append_event("vision.state", state)

    def _restore_vision(self, data: SessionData) -> None:
        if self.vision_runtime is not None:
            self.vision_runtime.cancel()
        self.vision_runtime = (
            VisionRuntime(
                load_vision_settings(), request_id=data.user_request_id,
                initial_state=data.vision_state, persist_callback=self._persist_vision,
                settings_loader=lambda: load_vision_settings(),
            )
            if data.user_request_id is not None and data.vision_state is not None else None
        )

    def _restore_self_check(self, data: SessionData) -> None:
        if self.self_check_runtime is not None:
            self.self_check_runtime.cancel()
        self.self_check_runtime = (
            SelfCheckRuntime(
                self.workspace, request_id=data.user_request_id,
                initial_state=data.self_check_state, command_sandbox=self.command_sandbox,
            ) if AUTOMATIC_SELF_CHECK_ENABLED and data.user_request_id is not None else None
        )
        if self.self_check_runtime is not None and data.source_event_seq is not None:
            self.self_check_runtime.bind_source(data.source_event_seq)
            if self.session_path is not None:
                # A crash between a side effect and its fact event is not clean.
                with self.session_write_lock:
                    unknown = False
                    for entry in iter_session_audit_entries(self.session_path):
                        if entry["seq"] <= data.source_event_seq:
                            continue
                        if entry["type"] == "self_check.state":
                            unknown = False
                        elif (entry["type"] == "tool.execution_started"
                              and entry["tool_name"] not in READ_ONLY_TOOL_NAMES | {"task"}):
                            unknown = True
                if unknown:
                    self.self_check_runtime.mark_unknown()
        self.last_task_outcome = None

    def _record_skill_activation(self, snapshot: SkillSnapshot) -> None:
        with self.session_write_lock:
            if self.pending_skill_activations is None:
                self.pending_skill_activations = []
            self.pending_skill_activations.append(snapshot)
            if self.session_path is not None and self.session_path.exists():
                self._flush_skill_activations()

    def _flush_skill_activations(self) -> None:
        if self.session_path is None or not self.session_path.exists():
            return
        pending = self.pending_skill_activations or []
        while pending:
            snapshot = pending[0]
            self._append_event(
                "skill.activated",
                {
                    "name": snapshot.name,
                    "display_name": snapshot.display_name,
                    "source": snapshot.source,
                    "sha256": snapshot.sha256,
                    "content": snapshot.content,
                },
            )
            pending.pop(0)

    def load_locked(self, path: Path) -> None:
        """Validate and lock a destination before releasing the current Session."""
        destination = Path(path).resolve()
        if self.session_path is not None and destination == self.session_path.resolve():
            if not self._close_project():
                raise RuntimeError("项目服务尚未停止，无法恢复会话")
            if destination.exists():
                data = load_session(
                    destination, allow_incomplete_tool_batch=True, allow_pending_user_tail=True,
                )
                self._restore_vision(data)
                self._restore_self_check(data)
                self.plan_runtime = PlanRuntime(data.plan_state)
            elif self.vision_runtime is not None:
                self.vision_runtime.cancel()
                self.vision_runtime = None
            if not destination.exists() and self.self_check_runtime is not None:
                self.self_check_runtime.cancel()
                self.self_check_runtime = None
            self.verification_runtime = None
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
            if data.version != SESSION_VERSION:
                destination, data, lease = _copy_legacy_session_for_resume(
                    destination,
                    data,
                    lease,
                    self.console,
                )
        except BaseException:
            lease.release()
            raise
        if not self.close():
            lease.release()
            raise RuntimeError("项目服务尚未停止，无法切换会话")
        self.workspace = data.workspace.resolve()
        self.session_path = destination.resolve()
        self.lease = lease
        self.title = data.title
        self.sticky_skills = data.sticky_skills
        self.last_task_state = data.task_state
        self.plan_runtime = PlanRuntime(data.plan_state)
        self.history_warning_level = 0
        self._restore_vision(data)
        self._restore_self_check(data)
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
        self._warn_history()

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
            assert self.session_path is not None
            self._append_event(
                "session.recovered",
                {"kind": "interrupted_turn"},
            )
            if not any(message.get("role") == "user" for message in recovered):
                self.first_task = None
        except (IndexError, ValueError):
            if completed is not None:
                self._save(completed)
                assert self.session_path is not None
                self._append_event(
                    "session.recovered",
                    {"kind": "last_completed_projection"},
                )

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
                assert self.session_path is not None
                self._append_event(
                    "session.recovered",
                    {"kind": "pending_user_resend"},
                )
                self.messages = base
                self.run_turn(data.pending_user_message, resume_data=data)
                return self.messages or base
            self._save(base)
            assert self.session_path is not None
            self._append_event(
                "session.recovered",
                {"kind": "pending_user_discarded"},
            )
            return base
        if data.tool_batch_state is not None:
            recovered = build_tool_batch_recovery_messages(data.messages)
            self._save(recovered)
            assert self.session_path is not None
            self._append_event(
                "session.recovered",
                {"kind": "tool_batch_closed"},
            )
            self.console.print(
                Text("已安全闭合上次中断的工具批次。", style="yellow")
            )
            return recovered
        return copy.deepcopy(data.messages)

    def run_turn(self, task: str, *, resume_data: SessionData | None = None) -> None:
        """Run one turn with durable snapshots and safe interruption recovery."""
        self._ensure_session()
        if resume_data is not None:
            self._restore_vision(resume_data)
            self._restore_self_check(resume_data)
            self.plan_runtime = PlanRuntime(resume_data.plan_state)
            request_id = None if resume_data.user_request_id is not None else str(uuid.uuid4())
        else:
            if self.vision_runtime is not None:
                self.vision_runtime.cancel()
            request_id = str(uuid.uuid4())
            self.vision_runtime = VisionRuntime(
                load_vision_settings(), request_id=request_id,
                persist_callback=self._persist_vision,
                settings_loader=lambda: load_vision_settings(),
            )
        if not AUTOMATIC_SELF_CHECK_ENABLED:
            if self.self_check_runtime is not None:
                self.self_check_runtime.cancel()
            self.self_check_runtime = None
        elif resume_data is None or self.self_check_runtime is None:
            if self.self_check_runtime is not None:
                self.self_check_runtime.cancel()
            self.self_check_runtime = SelfCheckRuntime(
                self.workspace, request_id=request_id, command_sandbox=self.command_sandbox,
            )
            if resume_data is not None and resume_data.user_request_id is None:
                # Legacy logs cannot distinguish an original request from a Stop
                # continuation; past execution is uncertain, not a clean reset.
                with self.session_write_lock:
                    if any(entry["type"] == "tool.execution_started"
                           and entry["tool_name"] not in READ_ONLY_TOOL_NAMES | {"task"}
                           for entry in iter_session_audit_entries(self.session_path)):
                        self.self_check_runtime.mark_unknown()
        first_snapshot = True
        if self.first_task is None:
            self.first_task = task
        completed = copy.deepcopy(self.messages)
        latest = copy.deepcopy(self.messages)
        self.skill_runtime = SkillRuntime(
            sticky_names=self.sticky_skills,
            activation_callback=self._record_skill_activation,
        )
        if not self.task_type_consumed and getattr(self.args, "task_type", None) is not None:
            self.console.print(Text("提示：--task-type 已弃用。"))
        self.task_type_consumed = True
        self.verification_runtime = None
        assert self.session_path is not None and self.lease is not None
        internal_paths = (self.session_path, self.lease.lock_path,
                          *session_internal_paths(self.session_path))
        renderer = RichEventRenderer(self.console, verbose=self.verbose)
        environment_approval_callback = (
            renderer.paused(lambda summary: request_environment_preparation(
                summary, console=self.console, interactive=True,
            ))
        ) if bool(getattr(self.args, "interactive_terminal", False)) else None
        if self.self_check_runtime is not None:
            self.self_check_runtime.set_internal_paths(internal_paths)
            self.self_check_runtime.approval_callback = environment_approval_callback
        self.last_task_outcome = None

        if self.project_runtime is None:
            self.project_runtime = ProjectRuntime(
                self.workspace, session_key=str(self.session_path), command_sandbox=self.command_sandbox,
            )
            self.project_runtime.browser = ProjectBrowser(
                self.project_runtime.browser_scope, redactor=self.project_runtime._redact,
            )
        self.project_runtime.internal_paths = internal_paths
        self.project_runtime.environment_approval_callback = environment_approval_callback
        self.project_runtime.browser.approval_callback = environment_approval_callback
        self.project_runtime.browser.begin_request()
        self.project_runtime.network_approval_callback = renderer.paused(lambda summary: (
            make_interactive_approval(self.workspace, self.console)(
                "project_network", {}, "允许当前服务启动配置访问外网；不同于依赖安装联网许可。"
            ) if bool(getattr(self.args, "interactive_terminal", False)) else False
        ))

        def snapshot(messages: list[dict[str, object]]) -> None:
            nonlocal latest, first_snapshot
            latest = copy.deepcopy(messages)
            # Persist boundaries so recovery can close unfinished calls without replay.
            self._save(messages, user_request_id=request_id if first_snapshot else None)
            if first_snapshot:
                first_snapshot = False
                if request_id is not None:
                    with self.session_write_lock:
                        saved = load_session(self.session_path, allow_incomplete_tool_batch=True,
                                             allow_pending_user_tail=True)
                    if saved.user_request_id != request_id or saved.source_event_seq is None:
                        raise RuntimeError("缺少原始用户请求来源")
                    # Budget -> Session lock ordering; do not bind while saving.
                    if self.vision_runtime is not None:
                        self.vision_runtime.bind_source(saved.source_event_seq)
                    if self.self_check_runtime is not None:
                        self.self_check_runtime.bind_source(saved.source_event_seq)

        def event_callback(event_type: str, payload: dict[str, object]) -> None:
            if event_type == "self_check_state":
                assert self.session_path is not None
                self._append_event("self_check.state", payload)
            elif event_type == "plan_state":
                self._append_event("plan.state", payload)
            elif event_type == "task_outcome":
                self.last_task_outcome = validate_task_outcome(payload)
            renderer(event_type, payload)
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
                approval_callback=renderer.paused(make_interactive_approval(
                    self.workspace, self.console
                )),
                event_callback=event_callback,
                message_snapshot_callback=snapshot,
                initial_messages=self.messages,
                token_counter=self.token_counter,
                skill_runtime=self.skill_runtime,
                plan_runtime=self.plan_runtime,
                project_runtime=self.project_runtime,
                self_check_runtime=self.self_check_runtime,
                vision_runtime=self.vision_runtime,
                on_vision_budget_exhausted=renderer.paused(lambda runtime: request_extra_vision_calls(
                    runtime, console=self.console,
                    interactive=bool(getattr(self.args, "interactive_terminal", False)),
                )),
                **kwargs,
            )
        except KeyboardInterrupt as error:
            if self.self_check_runtime is not None:
                self.self_check_runtime.cancel()
            if self.vision_runtime is not None:
                self.vision_runtime.cancel()
            renderer.close()
            cleanup = environment_cleanup_text(getattr(error, "cleanup_required", ()))
            if cleanup is not None:
                self.console.print(Text(cleanup, style="yellow"))
            self._recover_failed_turn(latest, completed)
            self.console.print(
                Text(
                    "本轮已中断；已完成的副作用未回滚。",
                    style="yellow",
                )
            )
            return
        except Exception:
            if self.self_check_runtime is not None:
                self.self_check_runtime.cancel()
            if self.vision_runtime is not None:
                self.vision_runtime.cancel()
            renderer.close()
            self._recover_failed_turn(latest, completed)
            raise
        finally:
            if self.skill_runtime is not None:
                self.sticky_skills = self.skill_runtime.sticky_names
            renderer.close()

            self._warn_history()

        if self.last_task_outcome is None or self.last_task_outcome.get("status") not in {
            "completed", "incomplete", "failed", "interrupted",
        }:
            raise RuntimeError("缺少可信任务终态，不能展示完成结果")
        renderer.render_answer(answer)
        if (
            not self.title
            and self.first_task is not None
            and self.last_task_outcome is not None
            and self.last_task_outcome.get("status") == "completed"
        ):
            title_renderer = RichEventRenderer(self.console)
            try:
                title_renderer("model_call", {})
                self.title = generate_title(
                    self.client, self.settings.model, self.first_task
                )
            finally:
                title_renderer.close()
            if self.messages is not None:
                self._save(self.messages)

    def new_session(self) -> None:
        if not self.close():
            raise RuntimeError("项目服务尚未停止，无法新建会话")
        self.messages = None
        self.session_path = None
        self.title = ""
        self.first_task = None
        self.sticky_skills = ()
        self.skill_runtime = None
        self.pending_skill_activations = None
        self.last_task_state = None
        self.last_task_outcome = None
        self.plan_runtime = PlanRuntime()
        self.history_warning_level = 0

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
            allow_self_check=self.self_check_runtime is not None,
            allow_plans=True,
            allow_project_runtime=True,
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
            renderer.render_compaction(
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
        if self.self_check_runtime is not None and self.last_task_outcome is not None:
            table.add_row("自检", task_outcome_text(self.last_task_outcome) or "状态未确认")
        if self.messages:
            tools = build_agent_tool_schemas(
                allow_run_command=self.args.allow_run_command,
                allow_subagent=self.args.allow_subagent,
                allow_self_check=self.self_check_runtime is not None,
                allow_plans=True,
                allow_project_runtime=True,
            )
            used_tokens = self.token_counter.count_request(self.messages, tools)
            percent = context_percentage(used_tokens, self.args.max_context_tokens)
        else:
            percent = 0
        table.add_row("上下文占用", f"{percent}%")
        if show_title:
            self.console.print()
            bird = _SMALL_STARTUP_BIRD if self.console.width < 60 else _STARTUP_BIRD
            self.console.print(Text(bird, style="bold bright_blue", no_wrap=True))
            self.console.print()
            self.console.print(Text.assemble(
                ("GuMi-Code", "bold blue"), ("  /  终端编程助手", "dim"),
            ))
            self.console.print(Text(
                display_path(self.workspace), style="dim", no_wrap=True, overflow="ellipsis",
            ))
            self.console.print(Text.assemble(
                ("模型  ", "dim"), (_safe_terminal_text(self.settings.model), "bold"),
                (f"    上下文 {percent}%", "dim"),
            ))
            self.console.print(Text.assemble(
                ("命令逐次审批" if self.args.allow_run_command else "命令关闭", "blue"),
                ("  ·  ", "dim"),
                ("子代理开启" if self.args.allow_subagent else "子代理关闭", "dim"),
            ))
            self.console.print(Text("/help 帮助  ·  /status 状态  ·  /sessions 会话", style="dim"))
            return
        self.console.print(Panel(table, title="状态", title_align="left", border_style="blue"))


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
                    ("audit", "查看安全审计时间线"),
                    ("fork", "从已完成 turn 创建 fork"),
                    ("resume", "恢复"),
                    ("rename", "重命名"),
                    ("delete", "删除"),
                    ("back", "返回"),
                ],
            )
        except (KeyboardInterrupt, EOFError):
            continue
        if operation == "view":
            data = load_session(
                entry.path,
                allow_incomplete_tool_batch=True,
                allow_pending_user_tail=True,
            )
            lineage = (
                f"\nparent={data.parent_session_id}@{data.parent_seq}"
                if data.parent_session_id is not None
                else ""
            )
            runtime.console.print(
                Text(
                    f"{display_path(entry.path)}\n"
                    f"{display_path(entry.workspace)}\n{entry.title}{lineage}"
                )
            )
        elif operation == "audit":
            table = Table("seq", "time", "type", "status", "tool")
            for item in session_audit_entries(entry.path):
                table.add_row(
                    str(item["seq"]),
                    _safe_terminal_text(item["time"]),
                    _safe_terminal_text(item["type"]),
                    _safe_terminal_text(item["status"]),
                    _safe_terminal_text(item["tool_name"] or ""),
                )
            runtime.console.print(table)
        elif operation == "fork":
            turns = completed_session_turns(entry.path)
            if not turns:
                runtime.console.print(Text("没有可 fork 的已完成 turn。", style="yellow"))
                continue
            try:
                boundary = choice(
                    "选择已完成 turn",
                    options=[
                        (seq, f"seq {seq} — {_safe_terminal_text(time)}")
                        for seq, time in turns
                    ],
                )
            except (KeyboardInterrupt, EOFError):
                continue
            destination = allocate_session_path()
            lease = SessionLease(destination).acquire()
            try:
                fork_session(entry.path, boundary, destination)
            finally:
                lease.release()
            runtime.console.print(Text(f"已创建 fork：{display_path(destination)}"))
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
            elif _confirm("仅删除该 Session 及其分段历史？"):
                # The shared deletion boundary owns the one non-reentrant lease.
                delete_session(entry.path)


def _manage_skills(runtime: InteractiveRuntime) -> None:
    while True:
        names = runtime.sticky_skills
        options: list[tuple[object, str]] = [("back", "返回")]
        options.extend((name, f"禁用 {name}") for name in names)
        if names:
            options.append(("clear", "清除全部 sticky Skills"))
        runtime.console.print(
            Text("Sticky Skills: " + (", ".join(names) if names else "无"))
        )
        try:
            action = choice("Skills 管理", options=options)
        except (KeyboardInterrupt, EOFError):
            return
        if action == "back":
            return
        updated = () if action == "clear" else tuple(
            name for name in names if name != action
        )
        runtime.sticky_skills = updated
        if runtime.skill_runtime is not None:
            runtime.skill_runtime.sticky_names = updated
        if runtime.messages:
            cleaned = copy.deepcopy(runtime.messages)
            for message in cleaned:
                content = message.get("content")
                if message.get("role") != "system" or not isinstance(content, str):
                    continue
                start = content.find(SKILL_SECTION_START)
                end = content.find(SKILL_SECTION_END)
                if start >= 0 and end >= start:
                    message["content"] = (
                        content[:start].rstrip()
                        + content[end + len(SKILL_SECTION_END) :]
                    ).strip()
                break
            runtime._save(cleaned)


def _handle_repl_command(runtime: InteractiveRuntime, command: str) -> bool:
    if command == "/exit":
        runtime.close()
        return True
    if command == "/help":
        runtime.console.print(Text(" ".join(sorted(KNOWN_COMMANDS - {"/revoke-verification"}))))
    elif command == "/compact":
        runtime.compact_context()
    elif command == "/status":
        runtime.show_status()
    elif command == "/revoke-verification":
        runtime.console.print(Text("验收登记与阶段许可已移除；无需使用此命令。"))
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
    elif command == "/skills":
        _manage_skills(runtime)
    elif command == "/vision":
        manage_vision_settings(runtime.console)
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
        prompt_continuation="    · ",
        bottom_toolbar=[("class:hint", " Enter 发送 · Alt+Enter 换行 · /help 帮助 · /exit 退出")],
        style=Style.from_dict({"prompt": "bold ansiblue", "hint": "ansibrightblack", "bottom-toolbar": "noreverse"}),
        color_depth=ColorDepth.DEPTH_1_BIT if runtime.console.no_color else None,
    )
    while True:
        runtime.console.print()
        runtime.console.rule(style="dim")
        try:
            value = prompt_session.prompt([("class:prompt", "GuMi> ")])
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
    runtime: InteractiveRuntime | None = None

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
            if data.version != SESSION_VERSION:
                if session_path is None or initial_lease is None:
                    raise RuntimeError("旧版 Session 恢复状态无效")
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
            sticky_skills=data.sticky_skills if data else (),
            last_task_state=data.task_state if data else None,
            plan_runtime=PlanRuntime(data.plan_state if data else None),
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
            runtime._restore_vision(data)
            runtime._restore_self_check(data)
            runtime.messages = runtime._recover_loaded_data(data)
            runtime._warn_history()
        try:
            return run_repl(runtime)
        except (KeyboardInterrupt, EOFError):
            runtime.close()
            return 0
    finally:
        if runtime is not None:
            runtime.close()
        if initial_lease is not None:
            initial_lease.release()
