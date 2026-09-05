import codecs
import io
import json
import os
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
