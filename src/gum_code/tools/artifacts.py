"""Read bounded retained output and publish immutable native-reading snapshots."""

import codecs
import errno
import hashlib
import io
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import BinaryIO

from gum_code.tools.command_logs import (
    COMMAND_LOG_DIR,
    is_command_log_path,
    open_command_log,
)
from gum_code.tools.common import _reject_symlinks

EXPECTED_RESULT_PARENT = Path(".task_outputs/tool-results")
MAX_ARTIFACT_FILE_BYTES = 5 * 1024 * 1024
MAX_CHARS_LIMIT = 20_000
READING_PAGE_CHARS = 18_000
READING_HEADER_BYTES = 2_048
MAX_READING_BODY_BYTES = MAX_ARTIFACT_FILE_BYTES - READING_HEADER_BYTES
READING_TOOL_NAMES = frozenset({
    "list_files", "find_files", "search_text", "read_file", "git_status", "git_diff",
})
READING_SYSTEM_INSTRUCTION = (
    "Reading tools return <reading-output> with bounded content and coverage facts. "
    "Use the exact next call to read more when relevant, without asking the user to page. "
    "For exhaustive review, read the relevant scope or state the unread gaps. "
    "complete means retained-output EOF, not full-query or full-file coverage; "
    "scope_complete means the requested query or line window was captured, not read by you. "
    "For read_file, file_has_more requires next_start_line after finishing this window; "
    "carry source_version as expected_source_version in the next read_file call. "
    "Changed queries/versions are new observations. Missing storage, limits and errors "
    "never prove full coverage. Continue within the current request and remaining budget."
)
_READING_START = "<reading-output>\n"
_READING_END = "\n</reading-output>"
_READING_HEADER = b"GUM_CODE_READING_V1\n"
_READING_NAME = re.compile(r"reading-[0-9a-f]{32}-([0-9a-f]{64})\.txt\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TODO_SUFFIX = "\n\n<reminder>Update your todos.</reminder>"
_SOURCE_KEYS = frozenset({
    "version", "tool", "scope_id", "scope_complete", "gap_reason", "total_items",
    "source_version", "file_has_more", "next_start_line",
})
_PAGE_KEYS = frozenset({
    "snapshot_id", "path", "offset", "next_offset", "complete", "remaining_chars", "next",
})
_GAPS = frozenset({
    None, "scan_limit", "depth_limit", "timeout", "source_limit", "capture_limit",
    "storage_unavailable", "snapshot_unavailable", "source_changed",
})


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _snapshot_id(source: dict[str, object], body: str) -> str:
    return hashlib.sha256(_canonical(source) + b"\n" + body.encode("utf-8")).hexdigest()


class ReadingBuffer:
    """Keep a UTF-8 prefix without allocating an unbounded line or result."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.truncated = False

    def add(self, text: str) -> None:
        if self.truncated:
            return
        data = text.encode("utf-8")
        available = max(0, MAX_READING_BODY_BYTES - len(self.data))
        self.data.extend(data[:available])
        if len(data) > available:
            self.truncated = True

    def text(self) -> str:
        # Only a capacity boundary can split the final valid UTF-8 character.
        return self.data.decode("utf-8", errors="ignore")


def is_reading_path(path: str) -> bool:
    requested = Path(path)
    return requested.parent == EXPECTED_RESULT_PARENT and bool(_READING_NAME.fullmatch(requested.name))


def _result_directory(workspace: Path, *, create: bool = False) -> int:
    """Use directory descriptors so a replaced parent cannot redirect publication."""
    _reject_symlinks(workspace, str(EXPECTED_RESULT_PARENT))
    if os.name != "posix":
        raise OSError("Private reading snapshots are unavailable")
    fd = os.open(workspace.resolve(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in EXPECTED_RESULT_PARENT.parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError("内部结果目录不允许符号链接或非目录路径") from None
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _save_reading(workspace: Path, source: dict[str, object], body: str) -> str:
    header = _READING_HEADER + _canonical(source) + b"\n"
    encoded = header + body.encode("utf-8")
    if len(header) > READING_HEADER_BYTES or len(encoded) > MAX_ARTIFACT_FILE_BYTES:
        raise ValueError("读取快照超过容量上限")
    name = f"reading-{secrets.token_hex(16)}-{_snapshot_id(source, body)}.txt"
    temporary = f".tmp-reading-{secrets.token_hex(16)}"
    directory = _result_directory(workspace, create=True)
    owned = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        owned = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
        # Exclusive atomic publication must not overwrite a collision.
        os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
    finally:
        if owned:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass  # Preserve the original failure/interruption; validation rejects extra links.
        os.close(directory)
    path = (EXPECTED_RESULT_PARENT / name).as_posix()
    _load_reading(workspace, path)
    return path


def _validate_source(source: object) -> bool:
    if not isinstance(source, dict) or source.keys() != _SOURCE_KEYS:
        return False
    if type(source["version"]) is not int or source["version"] != 1:
        return False
    if not isinstance(source["tool"], str) or source["tool"] not in READING_TOOL_NAMES | {"read_tool_output"}:
        return False
    if not isinstance(source["scope_id"], str) or not _DIGEST.fullmatch(source["scope_id"]):
        return False
    gap = source["gap_reason"]
    if type(source["scope_complete"]) is not bool or (gap is not None and not isinstance(gap, str)) or gap not in _GAPS:
        return False
    if gap is None and not source["scope_complete"]:
        return False
    if gap not in {None, "storage_unavailable", "snapshot_unavailable"} and source["scope_complete"]:
        return False
    if not source["scope_complete"] and source["total_items"] is not None:
        return False
    for name in ("total_items", "next_start_line"):
        value = source[name]
        if value is not None and (type(value) is not int or value < (1 if name == "next_start_line" else 0)):
            return False
    if source["file_has_more"] is not None and type(source["file_has_more"]) is not bool:
        return False
    version = source["source_version"]
    if version is not None and (not isinstance(version, str) or not _DIGEST.fullmatch(version)):
        return False
    if source["tool"] != "read_file" and any(source[key] is not None for key in (
        "source_version", "file_has_more", "next_start_line",
    )):
        return False
    if source["next_start_line"] is not None and not (
        source["tool"] == "read_file" and source["file_has_more"] is True
        and source["scope_complete"] and version is not None
    ):
        return False
    return True


def _load_reading(workspace: Path, path: str) -> tuple[dict[str, object], str]:
    if not is_reading_path(path):
        raise ValueError("无效的读取快照路径")
    _reject_symlinks(workspace, path)
    directory = _result_directory(workspace)
    try:
        fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != os.getuid() or before.st_mode & 0o077
            or before.st_size > MAX_ARTIFACT_FILE_BYTES):
            raise ValueError("读取快照不可用")
        data = stream.read(MAX_ARTIFACT_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if len(data) > MAX_ARTIFACT_FILE_BYTES or identity(before) != identity(after) or not data.startswith(_READING_HEADER):
        raise ValueError("读取快照不可用")
    header, separator, raw = data[len(_READING_HEADER):].partition(b"\n")
    if not separator or len(header) + len(_READING_HEADER) + 1 > READING_HEADER_BYTES:
        raise ValueError("读取快照不可用")
    source = json.loads(header)
    if not _validate_source(source):
        raise ValueError("读取快照不可用")
    body = raw.decode("utf-8")
    match = _READING_NAME.fullmatch(Path(path).name)
    if match is None or _snapshot_id(source, body) != match[1]:
        raise ValueError("读取快照已变化")
    return source, body


def _render_reading(metadata: dict[str, object], body: str) -> str:
    header = _canonical(metadata).decode("utf-8")
    output = _READING_START + header + "\nContent:\n" + body + _READING_END
    if len(output) > MAX_CHARS_LIMIT:
        raise ValueError("读取页超过输出上限")
    return output


def _reading_page(source: dict[str, object], body: str, path: str | None,
                  *, offset: int = 0, max_chars: int = READING_PAGE_CHARS,
                  unavailable: str | None = None) -> str:
    chunk = body[offset:offset + min(max_chars, READING_PAGE_CHARS)]
    end = offset + len(chunk)
    remaining = max(0, len(body) - end)
    return _render_reading({
        **source, "snapshot_id": _snapshot_id(source, body), "path": path,
        "offset": offset, "next_offset": end, "complete": remaining == 0 and unavailable is None,
        "remaining_chars": remaining if unavailable is None else None,
        "next": {"tool": "read_tool_output", "arguments": {
            "path": path, "offset": end, "max_chars": READING_PAGE_CHARS,
        }} if path is not None and remaining and unavailable is None else None,
        **({"gap_reason": unavailable} if unavailable is not None else {}),
    }, chunk)


def reading_result(workspace: Path, tool: str, arguments: dict[str, object], body: str,
                   *, max_items: int | None = None, gap_reason: str | None = None,
                   total_items: int | None = None, source_version: str | None = None,
                   file_has_more: bool | None = None, next_start_line: int | None = None) -> str:
    """Retain before cutting the first page; capture, delivery and scope stay distinct."""
    buffer = ReadingBuffer()
    buffer.add(body)
    body = buffer.text()
    if buffer.truncated:
        gap_reason = gap_reason or "capture_limit"
        total_items = next_start_line = None
    source = {
        "version": 1, "tool": tool,
        "scope_id": hashlib.sha256(_canonical([tool, arguments])).hexdigest(),
        "scope_complete": gap_reason is None, "gap_reason": gap_reason,
        "total_items": total_items if gap_reason is None else None,
        "source_version": source_version, "file_has_more": file_has_more,
        "next_start_line": next_start_line if gap_reason is None else None,
    }
    page_chars = READING_PAGE_CHARS
    if max_items is not None:
        lines = body.split("\n", max_items)
        if len(lines) > max_items:
            page_chars = min(page_chars, len("\n".join(lines[:max_items])) + 1)
    path = None
    if len(body) > page_chars:
        try:
            path = _save_reading(workspace, source, body)
        except OSError:
            return _reading_page(source, body, None, max_chars=page_chars,
                                 unavailable="storage_unavailable")
    return _reading_page(source, body, path, max_chars=page_chars)


def parse_reading_output(content: str) -> tuple[dict[str, object], str, str] | None:
    """Parse bounded transport metadata, never instructions from its content."""
    suffix = _TODO_SUFFIX if content.endswith(_TODO_SUFFIX) else ""
    value = content[:-len(suffix)] if suffix else content
    if len(value) > MAX_CHARS_LIMIT or not value.startswith(_READING_START) or not value.endswith(_READING_END):
        return None
    header, separator, body = value[len(_READING_START):-len(_READING_END)].partition("\nContent:\n")
    if not separator or len(header.encode("utf-8")) > READING_HEADER_BYTES:
        return None
    try:
        metadata = json.loads(header)
        if not isinstance(metadata, dict) or metadata.keys() != _SOURCE_KEYS | _PAGE_KEYS:
            return None
        source = {key: metadata[key] for key in _SOURCE_KEYS}
        if not _validate_source(source):
            return None
        for name in ("offset", "next_offset"):
            if type(metadata[name]) is not int or metadata[name] < 0:
                return None
        if metadata["next_offset"] != metadata["offset"] + len(body):
            return None
        if type(metadata["complete"]) is not bool:
            return None
        remaining = metadata["remaining_chars"]
        if remaining is not None and (type(remaining) is not int or remaining < 0):
            return None
        if remaining is None:
            if metadata["complete"] or source["gap_reason"] not in {"storage_unavailable", "snapshot_unavailable"}:
                return None
        elif metadata["complete"] != (remaining == 0):
            return None
        path = metadata["path"]
        if path is not None and (not isinstance(path, str) or not is_reading_path(path)):
            return None
        if remaining and path is None and source["gap_reason"] != "storage_unavailable":
            return None
        if not isinstance(metadata["snapshot_id"], str) or not _DIGEST.fullmatch(metadata["snapshot_id"]):
            return None
        if source["gap_reason"] not in {"storage_unavailable", "snapshot_unavailable"}:
            if path is not None and _READING_NAME.fullmatch(Path(path).name)[1] != metadata["snapshot_id"]:
                return None
            if path is None and metadata["complete"] and (
                metadata["offset"] != 0 or _snapshot_id(source, body) != metadata["snapshot_id"]
            ):
                return None
        expected_next = {"tool": "read_tool_output", "arguments": {
            "path": path, "offset": metadata["next_offset"], "max_chars": READING_PAGE_CHARS,
        }} if path is not None and remaining else None
        if metadata["next"] != expected_next:
            return None
        return metadata, body, suffix
    except (ValueError, TypeError):
        return None


def compact_reading_output(content: str, workspace: Path, preview_chars: int) -> str | None:
    parsed = parse_reading_output(content)
    if parsed is None:
        return None
    metadata, current, suffix = parsed
    source = {key: metadata[key] for key in _SOURCE_KEYS}
    path = metadata["path"]
    if path is None:
        if not metadata["complete"] or metadata["offset"] != 0 or _snapshot_id(source, current) != metadata["snapshot_id"]:
            return content
        if len(current) <= preview_chars:
            return content
        try:
            path = _save_reading(workspace, source, current)
        except OSError:
            return content
        full = current
    else:
        try:
            stored, full = _load_reading(workspace, path)
            if stored != source or metadata["snapshot_id"] != _snapshot_id(stored, full):
                raise ValueError("读取快照不匹配")
            if full[metadata["offset"]:metadata["next_offset"]] != current:
                raise ValueError("读取页不匹配")
        except (OSError, ValueError, TypeError):
            chunk = current[:preview_chars]
            metadata.update(gap_reason="snapshot_unavailable", complete=False,
                            remaining_chars=None, next=None,
                            next_offset=metadata["offset"] + len(chunk))
            return _render_reading(metadata, chunk) + suffix
    return _reading_page(source, full, path, offset=metadata["offset"],
                         max_chars=min(len(current), preview_chars)) + suffix


def _read_page(stream: BinaryIO, offset: int, max_chars: int) -> tuple[str, bool]:
    """Bound bytes read even if a file grows after its size check."""
    decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(), translate=True)
    read_bytes = 0
    parts: list[str] = []
    remaining = max_chars
    while True:
        chunk = stream.read(min(8192, MAX_ARTIFACT_FILE_BYTES - read_bytes + 1))
        read_bytes += len(chunk)
        if read_bytes > MAX_ARTIFACT_FILE_BYTES:
            raise ValueError("文件超过 5 MB 上限")
        text = decoder.decode(chunk, final=not chunk)
        skipped = min(offset, len(text))
        offset -= skipped
        text = text[skipped:]
        kept = min(remaining, len(text))
        if kept:
            parts.append(text[:kept])
            remaining -= kept
        if len(text) > kept:
            return "".join(parts), False
        if not chunk:
            return "".join(parts), True


def _expired_log(path: str, offset: int) -> str:
    return json.dumps({
        "path": path, "offset": offset, "next_offset": offset,
        "complete": True, "content": "", "log_status": "expired_or_unavailable",
        "notice": "命令日志已清理或不可用；历史执行结果不变。",
    }, ensure_ascii=False)


def read_tool_output(
    workspace: Path,
    path: str,
    offset: int = 0,
    max_chars: int = 20_000,
    *,
    artifact_workspace: Path | None = None,
) -> str:
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise ValueError("offset 必须是整数")

    if not isinstance(max_chars, int) or isinstance(max_chars, bool):
        raise ValueError("max_chars 必须是整数")

    if offset < 0:
        raise ValueError("offset 必须 >= 0")

    if not 1 <= max_chars <= MAX_CHARS_LIMIT:
        raise ValueError(f"max_chars 必须在 1 到 {MAX_CHARS_LIMIT} 之间")

    requested = Path(path)

    if requested.is_absolute() or ".." in requested.parts:
        raise ValueError("path 必须是 .task_outputs/tool-results 下的相对路径")

    command_log = is_command_log_path(path)
    if requested.parent != EXPECTED_RESULT_PARENT and not command_log:
        raise ValueError("path 必须在 .task_outputs/tool-results 目录下")

    if requested.suffix != ".txt":
        raise ValueError("只允许读取 .txt 结果文件")

    root = workspace.resolve()

    _reject_symlinks(workspace, path)

    if is_reading_path(path):
        try:
            source, body = _load_reading(workspace, path)
        except (OSError, ValueError, TypeError):
            output = reading_result(workspace, "read_tool_output", {"path": path}, "",
                                    gap_reason="snapshot_unavailable")
            metadata, _, _ = parse_reading_output(output)
            metadata.update(path=path, offset=offset, next_offset=offset,
                            complete=False, remaining_chars=None)
            return _render_reading(metadata, "")
        if artifact_workspace is not None and artifact_workspace.resolve() != workspace.resolve():
            # A child must retain parent pages in its own context-persistence root.
            try:
                path = _save_reading(artifact_workspace, source, body)
            except OSError:
                return _reading_page(source, body, None, offset=offset, max_chars=max_chars,
                                     unavailable="storage_unavailable")
        return _reading_page(source, body, path, offset=offset, max_chars=max_chars)

    expected_dir = (root / (COMMAND_LOG_DIR if command_log else EXPECTED_RESULT_PARENT)).resolve()
    target = (root / requested).resolve()

    if target.parent != expected_dir:
        raise ValueError("path 必须在 .task_outputs/tool-results 目录下")

    if not command_log:
        if not target.exists():
            raise ValueError(f"文件不存在：{path!r}")
        if not target.is_file():
            raise ValueError(f"路径不是普通文件：{path!r}")
        if target.stat().st_size > MAX_ARTIFACT_FILE_BYTES:
            raise ValueError("文件超过 5 MB 上限")

    try:
        stream = (
            os.fdopen(open_command_log(workspace, path), "rb")
            if command_log else target.open("rb")
        )
    except OSError:
        # Eviction may race with the initial existence check.
        if command_log:
            return _expired_log(path, offset)
        raise
    try:
        with stream:
            chunk, complete = _read_page(stream, offset, max_chars)
    except OSError:
        if command_log:
            return _expired_log(path, offset)
        raise

    metadata: dict[str, object] = {}
    if command_log:
        truncated = requested.name.endswith("-truncated.txt")
        metadata = {
            "log_status": "truncated" if truncated else "complete",
            "log_truncated": truncated,
            "notice": "complete 仅表示已读完保留的日志；被截断的中段无法恢复。",
        }

    return json.dumps(
        {
            "path": path,
            "offset": offset,
            "next_offset": offset + len(chunk),
            "complete": complete,
            "content": chunk,
            **metadata,
        },
        ensure_ascii=False,
    )
