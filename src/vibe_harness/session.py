import copy
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX is the supported runtime.
    fcntl = None

from vibe_harness.tools.registry import READ_ONLY_TOOL_NAMES

SESSION_VERSION = 2
LEGACY_SESSION_VERSION = 1
MAX_SESSION_FILE_BYTES = 5 * 1024 * 1024
MAX_SESSION_TITLE_CHARS = 30
IN_FLIGHT_MARKER_KEY = "_vibe_harness_in_flight"
RECOVERABLE_TOOL_NAMES = READ_ONLY_TOOL_NAMES | {"task"}
TASK_INTERRUPTED_RESULT = json.dumps(
    {
        "status": "error",
        "code": "task_interrupted",
        "message": (
            "The previous read-only task was interrupted or not run; "
            "re-plan it from the current workspace state."
        ),
    },
    separators=(",", ":"),
)


@dataclass(frozen=True)
class ToolBatchState:
    tool_call_ids: tuple[str, ...]
    completed_call_ids: tuple[str, ...]
    pending_call_ids: tuple[str, ...]
    in_flight_call_id: str | None


@dataclass(frozen=True)
class SessionData:
    workspace: Path
    model: str
    messages: list[dict[str, object]]
    version: int = SESSION_VERSION
    title: str = ""
    tool_batch_state: ToolBatchState | None = None
    pending_user_message: str | None = None


@dataclass(frozen=True)
class PendingToolCall:
    id: str
    name: str
    arguments: str
    interrupted: bool = False


@dataclass(frozen=True)
class ToolBatchRecoveryPlan:
    completed_call_ids: tuple[str, ...]
    pending_tool_calls: tuple[PendingToolCall, ...]


def save_session(
    path: Path,
    workspace: Path,
    model: str,
    messages: list[dict[str, object]],
    title: str = "",
) -> None:
    target = Path(path)

    if target.is_symlink():
        raise ValueError(f"拒绝写入符号链接目标：{target}")

    parent = target.parent
    if not parent.exists():
        raise ValueError(f"父目录不存在：{parent}")
    if not parent.is_dir():
        raise ValueError(f"父目录不是目录：{parent}")

    if not isinstance(title, str) or len(title) > MAX_SESSION_TITLE_CHARS:
        raise ValueError(
            "Session title 必须是不超过 "
            f"{MAX_SESSION_TITLE_CHARS} 字符的字符串"
        )
    if any(ord(character) < 32 for character in title):
        raise ValueError("Session title 不允许控制字符")

    data = {
        "version": SESSION_VERSION,
        "workspace": str(Path(workspace).resolve()),
        "model": model,
        "title": title,
        "messages": messages,
    }

    content = json.dumps(data, ensure_ascii=False, indent=2)
    content_size = len(content.encode("utf-8"))
    if content_size > MAX_SESSION_FILE_BYTES:
        raise ValueError(
            f"Session 内容超过 {MAX_SESSION_FILE_BYTES} 字节上限"
        )

    fd, tmp_path = tempfile.mkstemp(dir=str(parent), prefix=".tmp-session-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            os.fchmod(f.fileno(), 0o600)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_session(
    path: Path,
    *,
    allow_incomplete_tool_batch: bool = False,
    allow_pending_user_tail: bool = False,
) -> SessionData:
    target = Path(path)

    if target.is_symlink():
        raise ValueError(f"拒绝读取符号链接目标：{target}")

    if not target.exists():
        raise ValueError(f"Session 文件不存在：{target}")

    if not target.is_file():
        raise ValueError(f"Session 文件不是普通文件：{target}")

    if target.stat().st_size > MAX_SESSION_FILE_BYTES:
        raise ValueError(
            f"Session 文件超过 {MAX_SESSION_FILE_BYTES} 字节上限：{target}"
        )

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Session 文件不是有效的 UTF-8 文本：{target}"
        ) from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Session 文件不是有效的 JSON") from exc

    if not isinstance(data, dict):
        raise ValueError("Session 顶层必须是 JSON 对象")

    version = data.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in {
            LEGACY_SESSION_VERSION,
            SESSION_VERSION,
        }
    ):
        raise ValueError(f"不支持的 Session 版本：{data.get('version')!r}")

    title = ""
    if version == SESSION_VERSION:
        title_raw = data.get("title")
        if not isinstance(title_raw, str) or len(title_raw) > MAX_SESSION_TITLE_CHARS:
            raise ValueError("Session 缺少有效的 title")
        if any(ord(character) < 32 for character in title_raw):
            raise ValueError("Session title 不允许控制字符")
        title = title_raw

    workspace_raw = data.get("workspace")
    if not isinstance(workspace_raw, str) or not workspace_raw:
        raise ValueError("Session 缺少有效的 workspace")

    model = data.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("Session 缺少有效的 model")

    messages_raw = data.get("messages")
    if not isinstance(messages_raw, list) or not messages_raw:
        raise ValueError("Session 缺少有效的 messages（非空列表）")

    messages: list[dict[str, object]] = []
    for index, message in enumerate(messages_raw):
        if not isinstance(message, dict):
            raise ValueError(f"messages[{index}] 必须是 JSON 对象")
        if not isinstance(message.get("role"), str) or not message.get("role"):
            raise ValueError(f"messages[{index}] 缺少有效的 role")
        messages.append(message)

    if messages[0]["role"] != "system":
        raise ValueError("Session 第一条消息必须是 system message")

    tool_batch_state = analyze_tool_batch_tail(messages)

    pending_user_message = None
    if tool_batch_state is None:
        tail_role = messages[-1]["role"]
        if tail_role == "user" and allow_pending_user_tail:
            content = messages[-1].get("content")
            if not isinstance(content, str):
                raise ValueError("Session 末尾 user message 缺少文本内容")
            pending_user_message = content
        elif tail_role not in {"assistant", "system"}:
            raise ValueError("Session 最后一条消息必须是 assistant message")
    elif not allow_incomplete_tool_batch:
        raise ValueError(
            "Session 未正常结束：最后一条 assistant message "
            "不能包含 tool_calls"
        )

    return SessionData(
        workspace=Path(workspace_raw),
        model=model,
        messages=copy.deepcopy(messages),
        version=version,
        title=title,
        tool_batch_state=tool_batch_state,
        pending_user_message=pending_user_message,
    )


class SessionLease:
    """Hold a non-blocking POSIX lock for one Session ownership lifecycle."""

    def __init__(self, session_path: Path) -> None:
        self.session_path = Path(session_path)
        self.lock_path = self.session_path.with_name(
            self.session_path.name + ".lock"
        )
        self._file: object | None = None

    def acquire(self) -> "SessionLease":
        if self._file is not None:
            raise RuntimeError("Session lock 已在当前进程持有")
        if fcntl is None:
            raise RuntimeError("当前平台不支持 Session 独占锁")
        if self.lock_path.is_symlink():
            raise ValueError("Session lock 不允许是符号链接")
        if not self.lock_path.parent.is_dir():
            raise ValueError("Session lock 父目录不存在")

        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(
                self.lock_path,
                flags,
                0o600,
            )
        except OSError as error:
            raise ValueError("Session lock 无法安全打开") from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Session lock 必须是普通文件")
            os.fchmod(descriptor, 0o600)
            file = os.fdopen(descriptor, "a+")
        except BaseException:
            os.close(descriptor)
            raise
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            file.close()
            raise RuntimeError("Session 已被另一个进程打开") from error
        self._file = file
        return self

    def release(self) -> None:
        file = self._file
        if file is None:
            return
        self._file = None
        if fcntl is not None:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()

    def __enter__(self) -> "SessionLease":
        return self.acquire()

    def __exit__(self, *args: object) -> None:
        self.release()


def analyze_tool_batch_tail(
    messages: list[dict[str, object]],
) -> ToolBatchState | None:
    batch_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "assistant":
            batch_index = index
            break

    if batch_index is None:
        return None

    tool_calls = messages[batch_index].get("tool_calls")
    if not tool_calls:
        return None
    if not isinstance(tool_calls, list):
        raise ValueError("assistant tool_calls 必须是列表")

    tool_call_ids: list[str] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            raise ValueError("tool call 必须是 JSON 对象")
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("tool call 缺少合法 ID")
        tool_call_ids.append(call_id)

    if len(set(tool_call_ids)) != len(tool_call_ids):
        raise ValueError("tool call ID 重复")

    tool_call_id_set = set(tool_call_ids)
    completed: list[str] = []
    in_flight_call_id: str | None = None

    tail = messages[batch_index + 1 :]
    for tail_index, message in enumerate(tail):
        if message.get("role") != "tool":
            raise ValueError("tool call 批次后存在非 tool 消息")

        result_id = message.get("tool_call_id")
        if result_id not in tool_call_id_set:
            raise ValueError(f"未知的 tool result ID：{result_id!r}")

        if message.get(IN_FLIGHT_MARKER_KEY) is True:
            if in_flight_call_id is not None:
                raise ValueError("in-flight 标记重复")
            if result_id in completed:
                raise ValueError(f"tool result ID 重复：{result_id!r}")
            if (
                len(completed) >= len(tool_call_ids)
                or result_id != tool_call_ids[len(completed)]
            ):
                raise ValueError(
                    "in-flight 必须是 completed 后的下一个 tool call"
                )
            if tail_index != len(tail) - 1:
                raise ValueError("in-flight 必须是 Session 最后一条消息")
            in_flight_call_id = result_id
        else:
            if result_id in completed:
                raise ValueError(f"tool result ID 重复：{result_id!r}")
            if result_id != tool_call_ids[len(completed)]:
                raise ValueError("tool result 顺序错误，必须是有序前缀")
            completed.append(result_id)

    if in_flight_call_id is not None:
        pending_start = len(completed) + 1
    else:
        pending_start = len(completed)

    pending = tool_call_ids[pending_start:]

    return ToolBatchState(
        tool_call_ids=tuple(tool_call_ids),
        completed_call_ids=tuple(completed),
        pending_call_ids=tuple(pending),
        in_flight_call_id=in_flight_call_id,
    )


def plan_tool_batch_recovery(
    messages: list[dict[str, object]],
) -> ToolBatchRecoveryPlan | None:
    last_assistant_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "assistant":
            last_assistant_index = index
            break

    if last_assistant_index is None:
        return None

    tool_calls = messages[last_assistant_index].get("tool_calls")
    if not tool_calls:
        return None
    if not isinstance(tool_calls, list):
        raise ValueError("assistant tool_calls 必须是列表")

    tool_call_by_id: dict[str, tuple[str, str]] = {}
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            raise ValueError("tool call 必须是 JSON 对象")
        call_id = tool_call.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("tool call 缺少合法 ID")

        function = tool_call.get("function")
        if not isinstance(function, dict):
            raise ValueError("tool call 缺少合法 function")

        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tool call 缺少合法 name")

        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            raise ValueError("tool call 缺少合法 arguments")

        tool_call_by_id[call_id] = (name, arguments)

    state = analyze_tool_batch_tail(messages)
    if state is None:
        return None

    in_flight_call_id = state.in_flight_call_id
    if in_flight_call_id is not None:
        name, _ = tool_call_by_id[in_flight_call_id]
        if name not in RECOVERABLE_TOOL_NAMES:
            raise ValueError(
                f"存在 in-flight 工具调用，无法自动恢复："
                f"{in_flight_call_id}"
            )

    pending_tool_calls = []
    if in_flight_call_id is not None:
        name, arguments = tool_call_by_id[in_flight_call_id]
        pending_tool_calls.append(
            PendingToolCall(
                id=in_flight_call_id,
                name=name,
                arguments=arguments,
                interrupted=True,
            )
        )
    for call_id in state.pending_call_ids:
        name, arguments = tool_call_by_id[call_id]
        pending_tool_calls.append(
            PendingToolCall(id=call_id, name=name, arguments=arguments)
        )

    return ToolBatchRecoveryPlan(
        completed_call_ids=state.completed_call_ids,
        pending_tool_calls=tuple(pending_tool_calls),
    )


def build_tool_batch_recovery_messages(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    plan = plan_tool_batch_recovery(messages)

    result = [
        message
        for message in copy.deepcopy(messages)
        if message.get(IN_FLIGHT_MARKER_KEY) is not True
    ]

    if plan is None:
        return result

    for tool_call in plan.pending_tool_calls:
        if tool_call.name == "task":
            content = TASK_INTERRUPTED_RESULT
        elif tool_call.interrupted:
            content = (
                "该只读工具在上次进程中被中断，未保留可用结果，"
                "请重新调用或重新规划。"
            )
        else:
            content = (
                "该工具在上次进程中尚未执行，"
                "请根据当前状态重新规划。"
            )
        result.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": content,
            }
        )

    return result


def build_interrupted_turn_messages(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Close a live interrupted tool batch without replaying any call."""
    state = analyze_tool_batch_tail(messages)
    if state is None:
        return copy.deepcopy(messages)

    assistant_index = max(
        index
        for index, message in enumerate(messages)
        if message.get("role") == "assistant"
    )
    tool_calls = messages[assistant_index].get("tool_calls")
    if not isinstance(tool_calls, list):
        raise ValueError("assistant tool_calls 必须是列表")

    result = [
        message
        for message in copy.deepcopy(messages)
        if message.get(IN_FLIGHT_MARKER_KEY) is not True
    ]
    completed = set(state.completed_call_ids)
    for call in tool_calls:
        call_id = call.get("id") if isinstance(call, dict) else None
        if call_id in completed:
            continue
        if call_id == state.in_flight_call_id:
            code = "interrupted_unknown_state"
            message = (
                "The tool turn was interrupted and may have changed state; "
                "inspect current state and do not replay it automatically."
            )
        else:
            code = "interrupted_not_run"
            message = (
                "The tool call was not run because the turn was interrupted; "
                "re-plan it from current state."
            )
        result.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(
                    {"status": "error", "code": code, "message": message},
                    separators=(",", ":"),
                ),
            }
        )
    return result
