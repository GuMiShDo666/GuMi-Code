"""Persist private Sessions and safely close interrupted tool-call batches."""

import copy
import hashlib
import json
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX is the supported runtime.
    fcntl = None

from gum_code.tools.registry import READ_ONLY_TOOL_NAMES
from gum_code.context import LARGE_RESULT_CHAR_LIMIT, persist_tool_result

SESSION_VERSION = 3
LEGACY_SESSION_VERSION = 1
SNAPSHOT_SESSION_VERSION = 2
MAX_SESSION_FILE_BYTES = 5 * 1024 * 1024
MAX_SESSION_TITLE_CHARS = 30
IN_FLIGHT_MARKER_KEY = "_gum_code_in_flight"
LEGACY_IN_FLIGHT_MARKER_KEY = "_vibe_harness_in_flight"
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
    session_id: str = ""
    sticky_skills: tuple[str, ...] = ()
    legacy_source: Path | None = None
    parent_session_id: str | None = None
    parent_seq: int | None = None
    last_seq: int = -1


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


def _has_in_flight_marker(message: dict[str, object]) -> bool:
    return (
        message.get(IN_FLIGHT_MARKER_KEY) is True
        or message.get(LEGACY_IN_FLIGHT_MARKER_KEY) is True
    )


_EVENT_TYPES = {
    "session.created",
    "session.metadata_updated",
    "session.recovered",
    "turn.started",
    "turn.completed",
    "message.appended",
    "tool.calls",
    "tool.execution_started",
    "tool.result",
    "skill.activated",
    "context.compacted",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_title(title: str) -> None:
    if not isinstance(title, str) or len(title) > MAX_SESSION_TITLE_CHARS:
        raise ValueError(
            "Session title 必须是不超过 "
            f"{MAX_SESSION_TITLE_CHARS} 字符的字符串"
        )
    if any(ord(character) < 32 for character in title):
        raise ValueError("Session title 不允许控制字符")


def _validate_skill_names(names: object) -> tuple[str, ...]:
    if not isinstance(names, (list, tuple)) or len(names) > 3:
        raise ValueError("Session sticky skills 必须是最多三个名称")
    result: list[str] = []
    for name in names:
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 128
            or Path(name).name != name
            or any(ord(character) < 32 for character in name)
        ):
            raise ValueError("Session sticky skill 名称无效")
        if name not in result:
            result.append(name)
    return tuple(result)


def _event(seq: int, event_type: str, data: dict[str, object]) -> dict[str, object]:
    return {"seq": seq, "time": _utc_now(), "type": event_type, "data": data}


def _encode_events(events: list[dict[str, object]]) -> bytes:
    return b"".join(
        (
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        for event in events
    )


def _validate_event(event: object, expected_seq: int) -> dict[str, object]:
    if not isinstance(event, dict) or set(event) != {"seq", "time", "type", "data"}:
        raise ValueError("Session 事件格式无效")
    if event.get("seq") != expected_seq:
        raise ValueError("Session 事件序号不连续")
    timestamp = event.get("time")
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ValueError("Session 事件时间无效")
    try:
        datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("Session 事件时间无效") from error
    event_type = event.get("type")
    if event_type not in _EVENT_TYPES:
        raise ValueError(f"不支持的 Session 事件类型：{event_type!r}")
    if not isinstance(event.get("data"), dict):
        raise ValueError("Session 事件 data 必须是对象")
    data = event["data"]
    assert isinstance(data, dict)
    if event_type == "skill.activated":
        if (
            not isinstance(data.get("name"), str)
            or data.get("source") not in {"explicit", "automatic"}
            or not isinstance(data.get("content"), str)
            or not isinstance(data.get("sha256"), str)
            or hashlib.sha256(data["content"].encode("utf-8")).hexdigest()
            != data["sha256"]
        ):
            raise ValueError("skill.activated 事件无效")
    elif event_type == "session.recovered" and not isinstance(data.get("kind"), str):
        raise ValueError("session.recovered 事件无效")
    elif event_type == "session.metadata_updated" and not set(data) <= {
        "title",
        "model",
        "sticky_skills",
    }:
        raise ValueError("session.metadata_updated 事件无效")
    elif event_type == "context.compacted":
        source_seq = data.get("source_seq")
        if (
            set(data) != {"messages", "source_seq"}
            or not isinstance(source_seq, list)
            or len(source_seq) != 2
            or not all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in source_seq
            )
        ):
            raise ValueError("context.compacted 事件无效")
    return event


def _read_event_log(
    target: Path,
    *,
    repair_tail: bool = False,
) -> list[dict[str, object]]:
    if target.is_symlink():
        raise ValueError(f"拒绝读取符号链接目标：{target}")
    if not target.exists() or not target.is_file():
        raise ValueError(f"Session 文件不存在或不是普通文件：{target}")
    if target.stat().st_size > MAX_SESSION_FILE_BYTES:
        raise ValueError(f"Session 文件超过 {MAX_SESSION_FILE_BYTES} 字节上限：{target}")
    raw = target.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"Session 文件不是有效的 UTF-8 文本：{target}") from error
    if not text:
        raise ValueError("Session JSONL 为空")

    lines = text.splitlines(keepends=True)
    events: list[dict[str, object]] = []
    valid_bytes = 0
    for index, line in enumerate(lines):
        complete = line.endswith("\n")
        payload = line[:-1] if complete else line
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as error:
            if index == len(lines) - 1 and not complete:
                if repair_tail:
                    with target.open("r+b") as file:
                        file.truncate(valid_bytes)
                        file.flush()
                        os.fsync(file.fileno())
                break
            raise ValueError(f"Session JSONL 第 {index + 1} 行损坏") from error
        events.append(_validate_event(parsed, index))
        valid_bytes += len(line.encode("utf-8"))

    if not events:
        raise ValueError("Session JSONL 缺少有效事件")
    header = events[0]
    if header["type"] != "session.created":
        raise ValueError("Session JSONL 第一条必须是 session.created")
    data = header["data"]
    assert isinstance(data, dict)
    if data.get("version") != SESSION_VERSION:
        raise ValueError(f"不支持的 Session 版本：{data.get('version')!r}")
    try:
        uuid.UUID(str(data.get("id")))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("Session 缺少有效 id") from error
    if not isinstance(data.get("workspace"), str) or not data["workspace"]:
        raise ValueError("Session 缺少有效的 workspace")
    if not isinstance(data.get("model"), str) or not data["model"]:
        raise ValueError("Session 缺少有效的 model")
    _validate_title(data.get("title", ""))
    _validate_skill_names(data.get("sticky_skills", []))
    legacy_source = data.get("legacy_source")
    if legacy_source is not None and (
        not isinstance(legacy_source, str) or not Path(legacy_source).is_absolute()
    ):
        raise ValueError("Session legacy_source 无效")
    parent_id = data.get("parent_session_id")
    parent_seq = data.get("parent_seq")
    if (parent_id is None) != (parent_seq is None):
        raise ValueError("Session fork lineage 不完整")
    if parent_id is not None:
        try:
            uuid.UUID(parent_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Session parent_session_id 无效") from error
        if (
            not isinstance(parent_seq, int)
            or isinstance(parent_seq, bool)
            or parent_seq < 0
        ):
            raise ValueError("Session parent_seq 无效")
    return events


def _event_message(event: dict[str, object]) -> dict[str, object] | None:
    data = event["data"]
    assert isinstance(data, dict)
    message = data.get("message")
    if not isinstance(message, dict):
        return None
    if not isinstance(message.get("role"), str) or not message["role"]:
        raise ValueError("Session 事件中的 message 无效")
    return copy.deepcopy(message)


def _replay_events(
    events: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    header = events[0]["data"]
    assert isinstance(header, dict)
    state = copy.deepcopy(header)
    messages: list[dict[str, object]] = []
    for event in events[1:]:
        event_type = event["type"]
        data = event["data"]
        assert isinstance(data, dict)
        if event_type == "session.metadata_updated":
            if "title" in data:
                _validate_title(data["title"])
                state["title"] = data["title"]
            if "model" in data:
                if not isinstance(data["model"], str) or not data["model"]:
                    raise ValueError("Session metadata model 无效")
                state["model"] = data["model"]
            if "sticky_skills" in data:
                state["sticky_skills"] = list(
                    _validate_skill_names(data["sticky_skills"])
                )
            continue
        if event_type == "context.compacted":
            replacement = data.get("messages")
            if not isinstance(replacement, list) or not all(
                isinstance(message, dict) for message in replacement
            ):
                raise ValueError("context.compacted messages 无效")
            messages = copy.deepcopy(replacement)
            continue
        message = _event_message(event)
        if message is None:
            if event_type in {
                "turn.started",
                "turn.completed",
                "message.appended",
                "tool.calls",
                "tool.execution_started",
                "tool.result",
            }:
                raise ValueError(f"{event_type} 事件缺少 message")
            continue
        role = message.get("role")
        if (
            (event_type == "turn.started" and role != "user")
            or (
                event_type == "turn.completed"
                and (role != "assistant" or bool(message.get("tool_calls")))
            )
            or (
                event_type == "tool.calls"
                and (role != "assistant" or not isinstance(message.get("tool_calls"), list))
            )
            or (
                event_type == "tool.execution_started"
                and (role != "tool" or not _has_in_flight_marker(message))
            )
            or (
                event_type == "tool.result"
                and (role != "tool" or _has_in_flight_marker(message))
            )
        ):
            raise ValueError(f"{event_type} 事件 message 类型不匹配")
        if event_type == "tool.result" and messages and _has_in_flight_marker(messages[-1]):
            if messages[-1].get("tool_call_id") != message.get("tool_call_id"):
                raise ValueError("tool.result 与 in-flight 调用不匹配")
            messages[-1] = message
        else:
            messages.append(message)
    return messages, state


def _message_event_type(message: dict[str, object]) -> str:
    role = message.get("role")
    if role == "user":
        return "turn.started"
    if role == "assistant" and message.get("tool_calls"):
        return "tool.calls"
    if role == "assistant":
        return "turn.completed"
    if role == "tool" and _has_in_flight_marker(message):
        return "tool.execution_started"
    if role == "tool":
        return "tool.result"
    return "message.appended"


def _tool_name(messages: list[dict[str, object]], call_id: object) -> str | None:
    for message in reversed(messages):
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict) or call.get("id") != call_id:
                continue
            function = call.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                return function["name"]
    return None


def _fit_messages_for_session(
    workspace: Path,
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    fitted = copy.deepcopy(messages)
    for message in fitted:
        content = message.get("content")
        if (
            message.get("role") == "tool"
            and not _has_in_flight_marker(message)
            and isinstance(content, str)
            and len(content) > LARGE_RESULT_CHAR_LIMIT
            and not content.startswith("<persisted-output>")
        ):
            try:
                message["content"] = persist_tool_result(
                    workspace,
                    message.get("tool_call_id", "result"),
                    content,
                )
            except OSError:
                pass
    return fitted


def _publish_new_log(target: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(
        dir=str(target.parent), prefix=".tmp-session-"
    )
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            os.fchmod(file.fileno(), 0o600)
        try:
            os.link(temporary, target)
        except FileExistsError as error:
            raise ValueError(f"Session 文件已存在：{target}") from error
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _append_bytes(target: Path, content: bytes) -> None:
    if not content:
        return
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("Session 文件必须是普通文件")
    with os.fdopen(descriptor, "r+b") as file:
        file.seek(0, os.SEEK_END)
        offset = file.tell()
        try:
            if offset:
                file.seek(offset - 1)
                if file.read(1) != b"\n":
                    file.seek(0, os.SEEK_END)
                    file.write(b"\n")
            file.seek(0, os.SEEK_END)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            file.seek(offset)
            file.truncate()
            file.flush()
            os.fsync(file.fileno())
            raise


def save_session(
    path: Path,
    workspace: Path,
    model: str,
    messages: list[dict[str, object]],
    title: str = "",
    sticky_skills: tuple[str, ...] | list[str] = (),
) -> None:
    """Append the semantic difference from a message projection to Session v3."""
    target = Path(path)

    if target.is_symlink():
        raise ValueError(f"拒绝写入符号链接目标：{target}")

    parent = target.parent
    if not parent.exists():
        raise ValueError(f"父目录不存在：{parent}")
    if not parent.is_dir():
        raise ValueError(f"父目录不是目录：{parent}")

    _validate_title(title)
    skills = _validate_skill_names(sticky_skills)
    if not isinstance(model, str) or not model:
        raise ValueError("Session model 必须是非空字符串")
    fitted = _fit_messages_for_session(Path(workspace), messages)
    if not fitted:
        raise ValueError("Session messages 不能为空")
    _session_data_from_messages(
        workspace=Path(workspace),
        model=model,
        messages=fitted,
        version=SESSION_VERSION,
        title=title,
        allow_incomplete_tool_batch=True,
        allow_pending_user_tail=True,
    )

    if not target.exists():
        header = _event(
            0,
            "session.created",
            {
                "version": SESSION_VERSION,
                "id": str(uuid.uuid4()),
                "workspace": str(Path(workspace).resolve()),
                "model": model,
                "title": title,
                "sticky_skills": list(skills),
            },
        )
        events = [header]
        history: list[dict[str, object]] = []
        for message in fitted:
            event_type = _message_event_type(message)
            data: dict[str, object] = {"message": message}
            if event_type in {"tool.execution_started", "tool.result"}:
                name = _tool_name(history, message.get("tool_call_id"))
                if name is not None:
                    data["tool_name"] = name
            events.append(_event(len(events), event_type, data))
            history.append(message)
        content = _encode_events(events)
        if len(content) > MAX_SESSION_FILE_BYTES:
            raise ValueError(f"Session 内容超过 {MAX_SESSION_FILE_BYTES} 字节上限")
        _publish_new_log(target, content)
        return

    events = _read_event_log(target, repair_tail=True)
    current, state = _replay_events(events)
    if Path(str(state["workspace"])).resolve() != Path(workspace).resolve():
        raise ValueError("Session workspace 不允许变更")
    additions: list[dict[str, object]] = []
    next_seq = len(events)
    metadata: dict[str, object] = {}
    if state.get("model") != model:
        metadata["model"] = model
    if state.get("title", "") != title:
        metadata["title"] = title
    if tuple(state.get("sticky_skills", [])) != skills:
        metadata["sticky_skills"] = list(skills)
    if metadata:
        additions.append(_event(next_seq, "session.metadata_updated", metadata))
        next_seq += 1

    prefix = len(fitted) >= len(current) and fitted[: len(current)] == current
    replaces_in_flight = (
        len(current) > 0
        and len(fitted) == len(current)
        and fitted[:-1] == current[:-1]
        and _has_in_flight_marker(current[-1])
        and fitted[-1].get("role") == "tool"
        and not _has_in_flight_marker(fitted[-1])
        and current[-1].get("tool_call_id") == fitted[-1].get("tool_call_id")
    )
    if replaces_in_flight:
        data = {"message": fitted[-1]}
        name = _tool_name(current, fitted[-1].get("tool_call_id"))
        if name is not None:
            data["tool_name"] = name
        additions.append(_event(next_seq, "tool.result", data))
    elif prefix:
        history = copy.deepcopy(current)
        for message in fitted[len(current) :]:
            event_type = _message_event_type(message)
            data = {"message": message}
            if event_type in {"tool.execution_started", "tool.result"}:
                name = _tool_name(history, message.get("tool_call_id"))
                if name is not None:
                    data["tool_name"] = name
            additions.append(_event(next_seq, event_type, data))
            next_seq += 1
            history.append(message)
    elif (
        len(fitted) > len(current)
        and current
        and fitted[1 : len(current)] == current[1:]
    ):
        projected = copy.deepcopy(fitted[: len(current)])
        additions.append(
            _event(
                next_seq,
                "context.compacted",
                {"messages": projected, "source_seq": [0, len(events) - 1]},
            )
        )
        next_seq += 1
        history = copy.deepcopy(projected)
        for message in fitted[len(current) :]:
            event_type = _message_event_type(message)
            data = {"message": message}
            if event_type in {"tool.execution_started", "tool.result"}:
                name = _tool_name(history, message.get("tool_call_id"))
                if name is not None:
                    data["tool_name"] = name
            additions.append(_event(next_seq, event_type, data))
            next_seq += 1
            history.append(message)
    elif fitted != current:
        additions.append(
            _event(
                next_seq,
                "context.compacted",
                {
                    "messages": fitted,
                    "source_seq": [0, len(events) - 1],
                },
            )
        )

    content = _encode_events(additions)
    needs_newline = 0 if target.read_bytes().endswith(b"\n") else 1
    if target.stat().st_size + needs_newline + len(content) > MAX_SESSION_FILE_BYTES:
        raise ValueError(f"Session 内容超过 {MAX_SESSION_FILE_BYTES} 字节上限")
    _append_bytes(target, content)


def load_session(
    path: Path,
    *,
    allow_incomplete_tool_batch: bool = False,
    allow_pending_user_tail: bool = False,
) -> SessionData:
    """Load legacy JSON or validate and replay a Session v3 JSONL log."""
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
    except json.JSONDecodeError:
        events = _read_event_log(target)
        messages, state = _replay_events(events)
        if not messages:
            raise ValueError("Session 缺少有效的 messages（非空列表）")
        return _session_data_from_messages(
            workspace=Path(str(state["workspace"])),
            model=str(state["model"]),
            messages=messages,
            version=SESSION_VERSION,
            title=str(state.get("title", "")),
            allow_incomplete_tool_batch=allow_incomplete_tool_batch,
            allow_pending_user_tail=allow_pending_user_tail,
            session_id=str(state["id"]),
            sticky_skills=_validate_skill_names(state.get("sticky_skills", [])),
            legacy_source=(
                Path(str(state["legacy_source"]))
                if isinstance(state.get("legacy_source"), str)
                else None
            ),
            parent_session_id=(
                str(state["parent_session_id"])
                if isinstance(state.get("parent_session_id"), str)
                else None
            ),
            parent_seq=(
                state["parent_seq"]
                if isinstance(state.get("parent_seq"), int)
                and not isinstance(state.get("parent_seq"), bool)
                else None
            ),
            last_seq=len(events) - 1,
        )

    if not isinstance(data, dict):
        raise ValueError("Session 顶层必须是 JSON 对象")

    version = data.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in {LEGACY_SESSION_VERSION, SNAPSHOT_SESSION_VERSION}
    ):
        raise ValueError(f"不支持的 Session 版本：{data.get('version')!r}")

    title = ""
    if version == SNAPSHOT_SESSION_VERSION:
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

    return _session_data_from_messages(
        workspace=Path(workspace_raw),
        model=model,
        messages=messages,
        version=version,
        title=title,
        allow_incomplete_tool_batch=allow_incomplete_tool_batch,
        allow_pending_user_tail=allow_pending_user_tail,
    )


def _session_data_from_messages(
    *,
    workspace: Path,
    model: str,
    messages: list[dict[str, object]],
    version: int,
    title: str,
    allow_incomplete_tool_batch: bool,
    allow_pending_user_tail: bool,
    session_id: str = "",
    sticky_skills: tuple[str, ...] = (),
    legacy_source: Path | None = None,
    parent_session_id: str | None = None,
    parent_seq: int | None = None,
    last_seq: int = -1,
) -> SessionData:
    if messages[0].get("role") != "system":
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
        workspace=workspace,
        model=model,
        messages=copy.deepcopy(messages),
        version=version,
        title=title,
        tool_batch_state=tool_batch_state,
        pending_user_message=pending_user_message,
        session_id=session_id,
        sticky_skills=sticky_skills,
        legacy_source=legacy_source,
        parent_session_id=parent_session_id,
        parent_seq=parent_seq,
        last_seq=last_seq,
    )


def append_session_event(path: Path, event_type: str, data: dict[str, object]) -> int:
    """Append one validated non-projection Session event."""
    if event_type not in {"skill.activated", "session.recovered"}:
        raise ValueError(f"不允许直接追加 Session 事件：{event_type}")
    target = Path(path)
    events = _read_event_log(target)
    addition = _event(len(events), event_type, copy.deepcopy(data))
    _validate_event(addition, len(events))
    content = _encode_events([addition])
    if target.stat().st_size + len(content) > MAX_SESSION_FILE_BYTES:
        raise ValueError(f"Session 内容超过 {MAX_SESSION_FILE_BYTES} 字节上限")
    _append_bytes(target, content)
    return len(events)


def migrate_session_to_v3(source: Path, destination: Path) -> Path:
    """Copy a v1/v2 snapshot into a new v3 log without changing the source."""
    source_path = Path(source).resolve()
    data = load_session(
        source_path,
        allow_incomplete_tool_batch=True,
        allow_pending_user_tail=True,
    )
    if data.version == SESSION_VERSION:
        raise ValueError("Session 已是 v3")
    target = Path(destination)
    if target.exists():
        raise ValueError(f"Session 文件已存在：{target}")
    header = _event(
        0,
        "session.created",
        {
            "version": SESSION_VERSION,
            "id": str(uuid.uuid4()),
            "workspace": str(data.workspace.resolve()),
            "model": data.model,
            "title": data.title,
            "sticky_skills": [],
            "legacy_source": str(source_path),
        },
    )
    events = [header]
    for message in data.messages:
        events.append(
            _event(
                len(events),
                _message_event_type(message),
                {
                    "message": copy.deepcopy(message),
                    "provenance": "legacy_import",
                    "synthetic_time": True,
                    "synthetic_boundary": True,
                },
            )
        )
    content = _encode_events(events)
    if len(content) > MAX_SESSION_FILE_BYTES:
        raise ValueError(f"Session 内容超过 {MAX_SESSION_FILE_BYTES} 字节上限")
    _publish_new_log(target, content)
    return target


def session_audit_entries(path: Path) -> list[dict[str, object]]:
    """Return only structural event metadata safe for terminal display."""
    entries: list[dict[str, object]] = []
    for event in _read_event_log(Path(path), repair_tail=False):
        data = event["data"]
        assert isinstance(data, dict)
        event_type = str(event["type"])
        tool_name = data.get("tool_name")
        if event_type == "tool.calls":
            message = data.get("message")
            calls = message.get("tool_calls") if isinstance(message, dict) else None
            if isinstance(calls, list):
                names = []
                for call in calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    if isinstance(function, dict) and isinstance(function.get("name"), str):
                        names.append(function["name"])
                tool_name = ", ".join(names) or None
        entries.append(
            {
                "seq": event["seq"],
                "time": event["time"],
                "type": event_type,
                "status": (
                    "synthetic"
                    if data.get("synthetic_boundary") is True
                    else "completed"
                    if event_type == "turn.completed"
                    else "recorded"
                ),
                "tool_name": tool_name if isinstance(tool_name, str) else None,
                "summary": (
                    "synthetic imported boundary"
                    if data.get("synthetic_boundary") is True
                    else "turn completed"
                    if event_type == "turn.completed"
                    else event_type
                ),
            }
        )
    return entries


def completed_session_turns(path: Path) -> list[tuple[int, str]]:
    return [
        (int(entry["seq"]), str(entry["time"]))
        for entry in session_audit_entries(path)
        if entry["type"] == "turn.completed"
    ]


def fork_session(source: Path, boundary_seq: int, destination: Path) -> Path:
    """Create an independent v3 child from one completed turn boundary."""
    events = _read_event_log(Path(source), repair_tail=False)
    if boundary_seq <= 0 or boundary_seq >= len(events):
        raise ValueError("fork 边界不存在")
    if events[boundary_seq]["type"] != "turn.completed":
        raise ValueError("只能从已完成 turn 创建 fork")
    messages, state = _replay_events(events[: boundary_seq + 1])
    target = Path(destination)
    header = _event(
        0,
        "session.created",
        {
            "version": SESSION_VERSION,
            "id": str(uuid.uuid4()),
            "workspace": state["workspace"],
            "model": state["model"],
            "title": state.get("title", ""),
            "sticky_skills": state.get("sticky_skills", []),
            "parent_session_id": state["id"],
            "parent_seq": boundary_seq,
        },
    )
    child_events = [header]
    for message in messages:
        child_events.append(
            _event(
                len(child_events),
                _message_event_type(message),
                {"message": message, "provenance": "fork_import"},
            )
        )
    content = _encode_events(child_events)
    if len(content) > MAX_SESSION_FILE_BYTES:
        raise ValueError(f"Session 内容超过 {MAX_SESSION_FILE_BYTES} 字节上限")
    _publish_new_log(target, content)
    return target


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

        if _has_in_flight_marker(message):
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
    """Plan closure of an interrupted tail without executing any pending call."""
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
    """Close pending calls with synthetic Tool Results instead of replaying them."""
    plan = plan_tool_batch_recovery(messages)

    result = [
        message
        for message in copy.deepcopy(messages)
        if not _has_in_flight_marker(message)
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
    """Close a live batch without claiming in-flight side effects failed."""
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

    # Keep completed results and synthesize the rest; never replay tools.
    result = [
        message
        for message in copy.deepcopy(messages)
        if not _has_in_flight_marker(message)
    ]
    completed = set(state.completed_call_ids)
    for call in tool_calls:
        call_id = call.get("id") if isinstance(call, dict) else None
        if call_id in completed:
            continue

        function = call.get("function") if isinstance(call, dict) else None
        tool_name = function.get("name") if isinstance(function, dict) else None
        if tool_name == "task":
            content = TASK_INTERRUPTED_RESULT
        else:
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
            content = json.dumps(
                {"status": "error", "code": code, "message": message},
                separators=(",", ":"),
            )
        result.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": content,
            }
        )
    return result
