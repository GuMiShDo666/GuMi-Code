"""Persist private Sessions and safely close interrupted tool-call batches."""

import copy
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import uuid
from dataclasses import dataclass
from collections import deque
from collections.abc import Iterable, Iterator
from itertools import chain, islice
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX is the supported runtime.
    fcntl = None

from gum_code.tools.registry import READ_ONLY_TOOL_NAMES
from gum_code.context import LARGE_RESULT_CHAR_LIMIT, persist_tool_result
from gum_code.verification import validate_task_state
from gum_code.vision import validate_vision_state
from gum_code.self_check import validate_self_check_state

SESSION_VERSION = 3
LEGACY_SESSION_VERSION = 1
SNAPSHOT_SESSION_VERSION = 2
MAX_SESSION_FILE_BYTES = 5 * 1024 * 1024
MAX_SESSION_PROJECTION_BYTES = 5 * 1024 * 1024
SESSION_HISTORY_WARNING_BYTES = 100 * 1024 * 1024
MAX_SESSION_AUDIT_ENTRIES = 10_000
MAX_SESSION_TITLE_CHARS = 30
IN_FLIGHT_MARKER_KEY = "_gum_code_in_flight"
LEGACY_IN_FLIGHT_MARKER_KEY = "_vibe_harness_in_flight"
# Public GET interruption closes the protocol tail without replay or permission grants.
RECOVERABLE_TOOL_NAMES = READ_ONLY_TOOL_NAMES | {"task", "web_search", "read_webpage"}
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
    task_state: dict[str, object] | None = None
    vision_state: dict[str, object] | None = None
    user_request_id: str | None = None
    source_event_seq: int | None = None
    self_check_state: dict[str, object] | None = None
    plan_state: dict[str, object] | None = None
    history_bytes: int = 0
    segment_count: int = 1
    history_modified_at: float = 0


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
    "task.state",
    "vision.state",
    "self_check.state",
    "plan.state",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_request_id(value: object) -> None:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("原始用户请求 id 无效") from None


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
            json.dumps(
                event,
                ensure_ascii=event["type"] in {"task.state", "vision.state", "self_check.state", "plan.state"},
                separators=(",", ":"),
            ) + "\n"
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
    if event_type == "task.state":
        validate_task_state(data)
    elif event_type == "vision.state":
        if validate_vision_state(data)["source_event_seq"] is None:
            raise ValueError("vision.state 缺少原始用户请求来源")
    elif event_type == "self_check.state":
        if validate_self_check_state(data)["source_event_seq"] is None:
            raise ValueError("self_check.state 缺少原始用户请求来源")
    elif event_type == "plan.state":
        from gum_code.todo import validate_plan_state
        validate_plan_state(data)
    elif event_type == "turn.started" and "request_id" in data:
        _validate_request_id(data["request_id"])
    elif event_type == "skill.activated":
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


def _validate_header(header: dict[str, object]) -> None:
    if header["type"] != "session.created":
        raise ValueError("Session JSONL 第一条必须是 session.created")
    data = header["data"]
    assert isinstance(data, dict)
    if data.get("version") != SESSION_VERSION:
        raise ValueError(f"不支持的 Session 版本：{data.get('version')!r}")
    if {"vision_state", "self_check_state", "plan_state", "user_request_id", "source_event_seq"} & data.keys():
        raise ValueError("Session header 不允许包含请求派生状态")
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

def _open_log(path: Path, *, writable: bool = False):
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise ValueError("Session 文件必须是普通非链接文件")
    if path.parent.name.endswith(".segments") and (
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        os.close(descriptor)
        raise ValueError("Session 历史段必须是私有文件")
    if info.st_size > MAX_SESSION_FILE_BYTES:
        os.close(descriptor)
        raise ValueError(f"Session 文件超过 {MAX_SESSION_FILE_BYTES} 字节上限")
    return os.fdopen(descriptor, "r+b" if writable else "rb")


def _segments_directory(target: Path) -> Path:
    return target.with_name(target.name + ".segments")


def _segment_path(target: Path, index: int) -> Path:
    return target if index == 0 else _segments_directory(target) / f"{index:016d}.jsonl"


def _segment_manifest(target: Path, *, allow_uncommitted: bool = False) -> dict[str, object] | None:
    directory = _segments_directory(target)
    if not directory.exists() and not directory.is_symlink():
        return None
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("Session 分段目录不安全")
    manifest = directory / "index.json"
    if not manifest.exists() and not manifest.is_symlink():
        if not allow_uncommitted:
            with os.scandir(directory) as entries:
                if any(entry.name.endswith(".jsonl") for entry in entries):
                    raise ValueError("Session 分段索引缺失；历史文件保留，不能猜测提交状态")
        return None  # Unpublished staging files are never history.
    with _open_log(manifest) as source:
        raw = source.read(4097)
    if len(raw) > 4096:
        raise ValueError("Session 分段索引超过限制")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Session 分段索引损坏") from error
    if (not isinstance(data, dict) or set(data) != {"version", "session_id", "segments"}
            or type(data["version"]) is not int or data["version"] != 1
            or type(data["segments"]) is not int or data["segments"] < 2):
        raise ValueError("Session 分段索引无效")
    _validate_request_id(data["session_id"])
    return data


def _read_event_log(target: Path, *, repair_tail: bool = False) -> Iterator[dict[str, object]]:
    """Stream committed segments; the small index is topology, not a snapshot."""
    manifest = _segment_manifest(target)
    count = manifest["segments"] if manifest else 1
    expected = 0
    for index in range(count):
        path = _segment_path(target, index)
        try:
            source = _open_log(path, writable=repair_tail and index == count - 1)
        except OSError as error:
            raise ValueError("Session 文件或已提交历史段缺失/不可安全读取") from error
        with source:
            start = expected
            valid_bytes = 0
            while True:
                line = source.readline(MAX_SESSION_FILE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_SESSION_FILE_BYTES:
                    raise ValueError("Session 单事件超过分段上限")
                complete = line.endswith(b"\n")
                try:
                    parsed = json.loads(line.decode("utf-8"))
                except UnicodeDecodeError as error:
                    raise ValueError("Session 文件不是有效的 UTF-8 文本") from error
                except json.JSONDecodeError as error:
                    if index == count - 1 and not complete and expected:
                        if repair_tail:
                            source.seek(valid_bytes)
                            source.truncate()
                            source.flush()
                            os.fsync(source.fileno())
                        break
                    raise ValueError(f"Session JSONL 第 {expected + 1} 行损坏") from error
                event = _validate_event(parsed, expected)
                if expected == 0:
                    _validate_header(event)
                    if manifest and manifest["session_id"] != event["data"]["id"]:
                        raise ValueError("Session 分段身份不匹配")
                expected += 1
                valid_bytes = source.tell()
                yield event
            if expected == start:
                raise ValueError("Session JSONL 缺少有效事件")


def session_history_usage(path: Path) -> dict[str, object]:
    target = Path(path)
    manifest = _segment_manifest(target)
    count = manifest["segments"] if manifest else 1
    total, modified = 0, 0.0
    for index in range(count):
        with _open_log(_segment_path(target, index)) as source:
            info = os.fstat(source.fileno())
            total += info.st_size
            modified = max(modified, info.st_mtime)
    return {"bytes": total, "segments": count, "warning_level": total // SESSION_HISTORY_WARNING_BYTES,
            "modified_at": modified}


def _event_message(event: dict[str, object]) -> dict[str, object] | None:
    data = event["data"]
    assert isinstance(data, dict)
    message = data.get("message")
    if not isinstance(message, dict):
        return None
    if not isinstance(message.get("role"), str) or not message["role"]:
        raise ValueError("Session 事件中的 message 无效")
    return copy.deepcopy(message)


class _RequestIds:
    """Bound UUID deduplication memory without retaining transcript events."""

    def __init__(self) -> None:
        self.small: set[str] = set()
        self.database = None

    def __contains__(self, value: str) -> bool:
        return value in self.small if self.database is None else self.database.execute(
            "SELECT 1 FROM ids WHERE id = ?", (value,)).fetchone() is not None

    def add(self, value: str) -> None:
        if self.database is None and len(self.small) < 1024:
            self.small.add(value)
            return
        if self.database is None:
            # SQLite owns/removes this private temporary database on close.
            self.database = sqlite3.connect("")
            self.database.execute("PRAGMA cache_size = -256")
            self.database.execute("CREATE TABLE ids (id TEXT PRIMARY KEY)")
            self.database.executemany("INSERT INTO ids VALUES (?)", ((item,) for item in self.small))
            self.small.clear()
        self.database.execute("INSERT INTO ids VALUES (?)", (value,))

    def close(self) -> None:
        if self.database is not None:
            self.database.close()


def _replay_events(events: Iterable[dict[str, object]]) -> tuple[list[dict[str, object]], dict[str, object]]:
    # ponytail: linear replay per operation; add disposable checkpoints only for measured latency.
    request_ids = _RequestIds()
    try:
        return _project_events(iter(events), request_ids)
    finally:
        request_ids.close()


def _projection_bytes(messages: list[dict[str, object]]) -> int:
    total = 0
    for message in messages:
        total += len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if total > MAX_SESSION_PROJECTION_BYTES:
            raise ValueError("Session 内容超过当前消息投影上限；需要先压缩上下文")
    return total


def _project_events(events: Iterator[dict[str, object]], request_ids: _RequestIds) -> tuple[list[dict[str, object]], dict[str, object]]:
    header = next(events)["data"]
    assert isinstance(header, dict)
    state = copy.deepcopy(header)
    messages: list[dict[str, object]] = []
    projection_bytes = 0
    state["_last_seq"] = 0
    for event in events:
        state["_last_seq"] = event["seq"]
        event_type = event["type"]
        data = event["data"]
        assert isinstance(data, dict)
        if event_type == "task.state":
            state["task_state"] = validate_task_state(data)
            continue
        if event_type == "plan.state":
            from gum_code.todo import validate_plan_transition
            state["plan_state"] = validate_plan_transition(state.get("plan_state"), data)
            continue
        if event_type == "vision.state":
            budget = validate_vision_state(data)
            if (
                budget["request_id"] != state.get("user_request_id")
                or budget["source_event_seq"] != state.get("source_event_seq")
            ):
                raise ValueError("vision.state 与原始用户请求不匹配")
            previous = state.get("vision_state")
            if isinstance(previous, dict) and (
                budget["used"] < previous["used"]
                or budget["limit"] < previous["limit"]
                or (previous["declined_extra"] and not budget["declined_extra"])
            ):
                raise ValueError("vision.state 不允许回退本轮预算")
            state["vision_state"] = budget
            continue
        if event_type == "self_check.state":
            check_state = validate_self_check_state(data)
            if (
                check_state["request_id"] != state.get("user_request_id")
                or check_state["source_event_seq"] != state.get("source_event_seq")
            ):
                raise ValueError("self_check.state 与原始用户请求不匹配")
            previous = state.get("self_check_state")
            if isinstance(previous, dict) and (
                check_state["generation"] < previous["generation"]
                or not set(previous["changed_paths"]) <= set(check_state["changed_paths"])
                or not set(previous["ambiguous_paths"]) <= (
                    set(check_state["ambiguous_paths"]) | set(check_state["changed_paths"])
                )
                or (previous["unknown"] is not None and check_state["unknown"] is None)
            ):
                raise ValueError("self_check.state 不允许丢失本轮变化事实")
            state["self_check_state"] = check_state
            continue
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
            projection_bytes = _projection_bytes(messages)
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
        if event_type == "turn.started" and "request_id" in data:
            if data["request_id"] in request_ids:
                raise ValueError("原始用户请求 id 不允许重复开启")
            request_ids.add(data["request_id"])
            state["user_request_id"] = data["request_id"]
            state["source_event_seq"] = event["seq"]
            state["vision_state"] = None
            state["self_check_state"] = None
        if event_type == "tool.result" and messages and _has_in_flight_marker(messages[-1]):
            if messages[-1].get("tool_call_id") != message.get("tool_call_id"):
                raise ValueError("tool.result 与 in-flight 调用不匹配")
            projection_bytes -= _projection_bytes([messages[-1]])
            messages[-1] = message
        else:
            messages.append(message)
        projection_bytes += _projection_bytes([message])
        if projection_bytes > MAX_SESSION_PROJECTION_BYTES:
            raise ValueError("Session 内容超过当前消息投影上限；需要先压缩上下文")
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


def _message_event_data(
    message: dict[str, object],
    history: list[dict[str, object]],
    *,
    request_id: str | None = None,
) -> tuple[str, dict[str, object]]:
    """Build ordered message fields; callers own sequence and history updates."""
    event_type = _message_event_type(message)
    data: dict[str, object] = {"message": message}
    if request_id is not None:
        data["request_id"] = request_id
    if event_type in {"tool.execution_started", "tool.result"}:
        name = _tool_name(history, message.get("tool_call_id"))
        if name is not None:
            data["tool_name"] = name
    return event_type, data


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
    linked = False
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            os.fchmod(file.fileno(), 0o600)
        try:
            os.link(temporary, target)
            linked = True
        except FileExistsError as error:
            raise ValueError(f"Session 文件已存在：{target}") from error
        _sync_directory(target.parent)
    except BaseException:
        if linked and target.exists() and os.path.samestat(os.stat(temporary), target.lstat()):
            target.unlink()
        raise
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


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_segment_index(target: Path, session_id: str, segments: int) -> None:
    directory = _segments_directory(target)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".tmp-index-")
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(json.dumps({"version": 1, "session_id": session_id, "segments": segments},
                                    separators=(",", ":")).encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / "index.json")
        _sync_directory(directory)
        _sync_directory(target.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _append_events(target: Path, events: Iterable[dict[str, object]], session_id: str) -> None:
    # A caller's projection/delta is bounded; reject one oversized event before
    # publishing any part of the append transaction.
    chunks: list[bytes] = []
    buffer = bytearray()
    total = 0
    for event in events:
        encoded = _encode_events([event])
        if len(encoded) > MAX_SESSION_FILE_BYTES:
            raise ValueError(f"Session 单事件超过 {MAX_SESSION_FILE_BYTES} 字节上限")
        total += len(encoded)
        if total > MAX_SESSION_PROJECTION_BYTES * 2:
            raise ValueError("Session 单次追加超过限制")
        if buffer and len(buffer) + len(encoded) > MAX_SESSION_FILE_BYTES:
            chunks.append(bytes(buffer))
            buffer.clear()
        buffer.extend(encoded)
    if buffer:
        chunks.append(bytes(buffer))
    if not chunks:
        return
    manifest = _segment_manifest(target)
    count = manifest["segments"] if manifest else 1
    active = _segment_path(target, count - 1)
    with _open_log(active) as source:
        size = os.fstat(source.fileno()).st_size
        source.seek(max(0, size - 1))
        newline = bool(size) and source.read(1) != b"\n"
    if len(chunks) == 1 and size + newline + len(chunks[0]) <= MAX_SESSION_FILE_BYTES:
        _append_bytes(active, chunks[0])
        return
    directory = _segments_directory(target)
    if not directory.exists():
        directory.mkdir(mode=0o700)
    _segment_manifest(target)  # Validate even an as-yet uncommitted directory.
    published: list[Path] = []
    new_count = count + len(chunks)
    try:
        for index, content in enumerate(chunks, count):
            segment = _segment_path(target, index)
            _publish_new_log(segment, content)
            published.append(segment)
        # All new files are durable before this single topology commit.
        _publish_segment_index(target, session_id, new_count)
    except BaseException:
        current = _segment_manifest(target, allow_uncommitted=True)
        if current is None or current["segments"] != new_count:
            for segment in published:
                try:
                    segment.unlink()
                except OSError:
                    pass
        raise


def session_internal_paths(path: Path) -> tuple[Path, ...]:
    """Return only the exact committed files, never a directory-wide exclusion."""
    target = Path(path)
    if not target.exists():
        return ()
    manifest = _segment_manifest(target)
    count = manifest["segments"] if manifest else 1
    paths = []
    for index in range(count):
        item = _segment_path(target, index)
        with _open_log(item):
            paths.append(item.resolve())
    if manifest:
        paths.append((_segments_directory(target) / "index.json").resolve())
    lock = target.with_name(target.name + ".lock")
    if lock.exists():
        with _open_log(lock):
            paths.append(lock.resolve())
    return tuple(paths)


def save_session(
    path: Path,
    workspace: Path,
    model: str,
    messages: list[dict[str, object]],
    title: str = "",
    sticky_skills: tuple[str, ...] | list[str] = (),
    *,
    user_request_id: str | None = None,
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
    _projection_bytes(fitted)
    if not fitted:
        raise ValueError("Session messages 不能为空")
    request_index = None
    if user_request_id is not None:
        _validate_request_id(user_request_id)
        request_index = next((index for index in range(len(fitted) - 1, -1, -1)
                              if fitted[index].get("role") == "user"), None)
        if request_index is None:
            raise ValueError("原始用户请求快照缺少 user message")
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
        if _segments_directory(target).exists() or _segments_directory(target).is_symlink():
            raise ValueError("Session 分段目录已存在，不能覆盖历史")
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
        for index, message in enumerate(fitted):
            event_type, data = _message_event_data(
                message, history,
                request_id=user_request_id if index == request_index else None,
            )
            events.append(_event(len(events), event_type, data))
            history.append(message)
        content = _encode_events(events)
        if len(content) > MAX_SESSION_FILE_BYTES:
            _publish_event_stream(target, events)
        else:
            _publish_new_log(target, content)
        return

    current, state = _replay_events(_read_event_log(target, repair_tail=True))
    if Path(str(state["workspace"])).resolve() != Path(workspace).resolve():
        raise ValueError("Session workspace 不允许变更")
    additions: list[dict[str, object]] = []
    next_seq = state["_last_seq"] + 1
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

    # A new host request remains an explicit event even after recovery replaced
    # the projection; compaction and internal user-role notices never bind it.
    if request_index is not None:
        if any(event["type"] == "turn.started" and event["data"].get("request_id") == user_request_id
               for event in _read_event_log(target)):
            raise ValueError("原始用户请求 id 不允许重复开启")
        base = fitted[:request_index]
        if current != base:
            additions.append(_event(next_seq, "context.compacted", {
                "messages": base, "source_seq": [0, state["_last_seq"]],
            }))
            next_seq += 1
        history = copy.deepcopy(base)
        for index, message in enumerate(fitted[request_index:], request_index):
            event_type, data = _message_event_data(
                message, history,
                request_id=user_request_id if index == request_index else None,
            )
            additions.append(_event(next_seq, event_type, data))
            next_seq += 1
            history.append(message)
        _append_events(target, additions, state["id"])
        return

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
            event_type, data = _message_event_data(message, history)
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
                {"messages": projected, "source_seq": [0, state["_last_seq"]]},
            )
        )
        next_seq += 1
        history = copy.deepcopy(projected)
        for message in fitted[len(current) :]:
            event_type, data = _message_event_data(message, history)
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
                    "source_seq": [0, state["_last_seq"]],
                },
            )
        )

    _append_events(target, additions, state["id"])


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
        messages, state = _replay_events(_read_event_log(target))
        usage = session_history_usage(target)
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
            last_seq=state["_last_seq"],
            task_state=state.get("task_state"),
            vision_state=state.get("vision_state"),
            user_request_id=state.get("user_request_id"),
            source_event_seq=state.get("source_event_seq"),
            self_check_state=state.get("self_check_state"),
            plan_state=state.get("plan_state"),
            history_bytes=usage["bytes"], segment_count=usage["segments"],
            history_modified_at=usage["modified_at"],
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
    task_state: dict[str, object] | None = None,
    vision_state: dict[str, object] | None = None,
    user_request_id: str | None = None,
    source_event_seq: int | None = None,
    self_check_state: dict[str, object] | None = None,
    plan_state: dict[str, object] | None = None,
    history_bytes: int = 0,
    segment_count: int = 1,
    history_modified_at: float = 0,
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
        task_state=validate_task_state(task_state) if task_state is not None else None,
        vision_state=validate_vision_state(vision_state) if vision_state is not None else None,
        user_request_id=user_request_id,
        source_event_seq=source_event_seq,
        self_check_state=(validate_self_check_state(self_check_state)
                          if self_check_state is not None else None),
        plan_state=copy.deepcopy(plan_state),
        history_bytes=history_bytes, segment_count=segment_count,
        history_modified_at=history_modified_at,
    )


def append_session_event(path: Path, event_type: str, data: dict[str, object]) -> int:
    """Append one validated non-projection Session event."""
    if event_type not in {"skill.activated", "session.recovered", "task.state", "vision.state", "self_check.state", "plan.state"}:
        raise ValueError(f"不允许直接追加 Session 事件：{event_type}")
    target = Path(path)
    _, state = _replay_events(_read_event_log(target, repair_tail=True))
    seq = state["_last_seq"] + 1
    addition = _event(seq, event_type, copy.deepcopy(data))
    _validate_event(addition, seq)
    if event_type in {"vision.state", "self_check.state", "plan.state"}:
        _replay_events(chain(_read_event_log(target), [addition]))
    _append_events(target, [addition], state["id"])
    return seq


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
    if target.exists() or _segments_directory(target).exists() or _segments_directory(target).is_symlink():
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
        _publish_event_stream(target, events)
    else:
        _publish_new_log(target, content)
    return target


def iter_session_audit_entries(path: Path) -> Iterator[dict[str, object]]:
    """Return only structural event metadata safe for terminal display."""
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
        yield (
            {
                "seq": event["seq"],
                "time": event["time"],
                "type": event_type,
                "status": (
                    str(data["status"])
                    if event_type == "task.state"
                    else "synthetic"
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


def session_audit_entries(path: Path) -> list[dict[str, object]]:
    """Return a bounded recent audit window; raw history is available by iterator."""
    entries = deque(iter_session_audit_entries(path), maxlen=MAX_SESSION_AUDIT_ENTRIES)
    if entries and entries[0]["seq"] != 0:
        return [{"seq": -1, "time": entries[0]["time"], "type": "history.window",
                 "status": "truncated", "tool_name": None,
                 "summary": "Earlier audit entries omitted; use streaming history."}, *entries]
    return list(entries)


def iter_completed_session_turns(path: Path) -> Iterator[tuple[int, str]]:
    for entry in iter_session_audit_entries(path):
        if entry["type"] == "turn.completed":
            yield int(entry["seq"]), str(entry["time"])


def completed_session_turns(path: Path) -> list[tuple[int, str]]:
    return list(deque(iter_completed_session_turns(path), maxlen=MAX_SESSION_AUDIT_ENTRIES))


def fork_session(source: Path, boundary_seq: int, destination: Path) -> Path:
    """Create an independent v3 child from one completed turn boundary."""
    if type(boundary_seq) is not int or boundary_seq <= 0:
        raise ValueError("fork 边界不存在")
    boundary = None
    for event in _read_event_log(Path(source)):
        if event["seq"] == boundary_seq:
            boundary = event["type"]
    if boundary is None:
        raise ValueError("fork 边界不存在")
    if boundary != "turn.completed":
        raise ValueError("只能从已完成 turn 创建 fork")
    _, state = _replay_events(islice(_read_event_log(Path(source)), boundary_seq + 1))
    return _copy_log(Path(source), Path(destination), state, boundary_seq, fork=True)


def _publish_event_stream(target: Path, events: Iterable[dict[str, object]]) -> None:
    """Stage a complete new logical log; publish its entry path last."""
    if target.exists() or target.is_symlink() or _segments_directory(target).exists():
        raise ValueError(f"Session 文件或分段目录已存在：{target}")
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".tmp-session-build-") as directory:
        staged = Path(directory) / "session.jsonl"
        buffer = bytearray()
        count = 0
        identity = None
        for seq, event in enumerate(events):
            _validate_event(event, seq)
            if seq == 0:
                _validate_header(event)
                identity = event["data"]["id"]
            encoded = _encode_events([event])
            if len(encoded) > MAX_SESSION_FILE_BYTES:
                raise ValueError("Session 单事件超过分段上限")
            if buffer and len(buffer) + len(encoded) > MAX_SESSION_FILE_BYTES:
                _publish_new_log(_segment_path(staged, count), bytes(buffer))
                count += 1
                _segments_directory(staged).mkdir(mode=0o700, exist_ok=True)
                buffer.clear()
            buffer.extend(encoded)
        if not buffer:
            raise ValueError("Session JSONL 为空")
        _publish_new_log(_segment_path(staged, count), bytes(buffer))
        count += 1
        if count > 1:
            _publish_segment_index(staged, identity, count)
        _replay_events(_read_event_log(staged))
        moved = linked = False
        try:
            if count > 1:
                if _segments_directory(target).exists() or _segments_directory(target).is_symlink():
                    raise ValueError("Session 分段目录已存在")
                os.rename(_segments_directory(staged), _segments_directory(target))
                moved = True
            os.link(staged, target)
            linked = True
            _sync_directory(target.parent)
        except BaseException:
            if moved and not linked:
                # Return only this builder's private directory to its staging area.
                os.rename(_segments_directory(target), _segments_directory(staged))
            raise


def _copy_log(source: Path, target: Path, state: dict[str, object], boundary_seq: int, *, fork: bool) -> Path:
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
    def copied_events():
        yield header
        for original in islice(_read_event_log(source), 1, boundary_seq + 1):
            copied = copy.deepcopy(original)
            if fork and copied["type"] == "task.state":
                # Preserve source sequence identities without restoring legacy obligations.
                copied["type"] = "session.recovered"
                copied["data"] = {"kind": "legacy_verification_not_inherited"}
            yield copied
    _publish_event_stream(target, copied_events())
    return target


def copy_session_log(source: Path, destination: Path) -> Path:
    """Import a v3 event history with a new identity and no restored authority."""
    _, state = _replay_events(_read_event_log(Path(source)))
    return _copy_log(Path(source), Path(destination), state, state["_last_seq"], fork=False)


def delete_session_log(path: Path) -> None:
    """Delete only the explicitly selected, validated logical Session files."""
    target = Path(path)
    with SessionLease(target):
        data = load_session(target, allow_incomplete_tool_batch=True, allow_pending_user_tail=True)
        if data.version == SESSION_VERSION:
            for _ in _read_event_log(target):
                pass
            manifest = _segment_manifest(target)
            if manifest:
                for index in range(1, manifest["segments"]):
                    _segment_path(target, index).unlink()
                (_segments_directory(target) / "index.json").unlink()
                try:
                    _segments_directory(target).rmdir()
                except OSError:
                    pass  # Unknown files are not owned history and are never deleted.
        target.unlink()


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
            # A fully closed batch can precede a new user request or harness
            # notice. It is no longer the active tail; never reopen/replay it.
            if len(completed) == len(tool_call_ids) and in_flight_call_id is None:
                if any(item.get("role") == "tool" for item in tail[tail_index + 1 :]):
                    raise ValueError("tool call 批次后存在孤立 tool 消息")
                return None
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
    *, allow_interrupted_commands: bool = False,
) -> ToolBatchRecoveryPlan | None:
    """Plan closure of an interrupted tail without executing any pending call."""
    if type(allow_interrupted_commands) is not bool:
        raise ValueError("Interrupted command recovery flag must be boolean")
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
        if name not in RECOVERABLE_TOOL_NAMES and not (allow_interrupted_commands and name == "run_command"):
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
    *, allow_interrupted_commands: bool = False,
) -> list[dict[str, object]]:
    """Close pending calls with synthetic Tool Results instead of replaying them."""
    plan = plan_tool_batch_recovery(messages, allow_interrupted_commands=allow_interrupted_commands)

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
        elif tool_call.interrupted and tool_call.name == "run_command":
            content = (
                "上次命令执行被中断，退出状态与结果未确认；命令可能已修改文件。"
                "宿主未重放该命令，不得将其作为成功验证。请先检查当前文件状态；"
                "如需重新执行，必须发起新的命令调用并重新审批。"
            )
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
