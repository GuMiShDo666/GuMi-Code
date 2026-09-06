import codecs
import hashlib
import io
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gum_code.tools.common import (
    MAX_READING_SCAN_ENTRIES,
    MAX_READING_SOURCE_BYTES,
    READING_TIMEOUT_SECONDS,
    SKIP_NAMES,
    _is_sensitive_name,
    _is_sensitive_path,
    _reject_symlinks,
    _resolve_workspace_path,
)
from gum_code.tools.artifacts import MAX_READING_BODY_BYTES, ReadingBuffer, reading_result

MAX_FILE_CONTENT_CHARS = 200_000


@dataclass(frozen=True)
class FileChange:
    """Host-only fact emitted after a real file content/existence change."""

    path: str
    before_head: bytes | None
    after_head: bytes | None
    executable: bool = False


FileChangeCallback = Callable[[FileChange], None]


def _changed(callback: FileChangeCallback | None, change: FileChange) -> None:
    if callback is not None:
        try:
            callback(change)
        except Exception:
            # The file is already committed; do not convert a failed journal to a Tool Result.
            raise RuntimeError("文件变更记录失败；已发生的写入未回滚") from None


def list_files(workspace: Path, path: str = ".", *, artifact_workspace: Path | None = None) -> str:
    root = workspace.resolve()
    target = _resolve_workspace_path(workspace, path)

    if not target.exists():
        raise ValueError(f"路径不存在：{path!r}")

    if not target.is_dir():
        raise ValueError(f"路径不是目录：{path!r}")

    entries: list[str] = []
    size = 0
    gap = None
    deadline = time.monotonic() + READING_TIMEOUT_SECONDS
    with os.scandir(target) as iterator:
        for count, entry in enumerate(iterator, 1):
            if count > MAX_READING_SCAN_ENTRIES:
                gap = "scan_limit"
                break
            if time.monotonic() >= deadline:
                gap = "timeout"
                break
            if entry.name in SKIP_NAMES or _is_sensitive_name(entry.name):
                continue
            item = Path(entry.path)
            try:
                rel = item.resolve().relative_to(root)
            except ValueError:
                continue
            if _is_sensitive_path(rel):
                continue
            text = f"[{'DIR' if entry.is_dir() else 'FILE'}] {entry.name}"
            size += len(text.encode("utf-8")) + 1
            entries.append(text)
            if size > MAX_READING_BODY_BYTES:
                gap = "capture_limit"
                break
    entries.sort()
    return reading_result(artifact_workspace or workspace, "list_files", {"path": target.relative_to(root).as_posix()},
                          "\n".join(entries), max_items=200, gap_reason=gap,
                          total_items=len(entries))


def read_file(
    workspace: Path,
    path: str,
    start_line: int = 1,
    max_lines: int = 200,
    expected_source_version: str | None = None,
    *,
    artifact_workspace: Path | None = None,
) -> str:
    if type(start_line) is not int or start_line < 1:
        raise ValueError("start_line 必须 >= 1")

    if type(max_lines) is not int or not 1 <= max_lines <= 400:
        raise ValueError("max_lines 必须在 1 到 400 之间")
    if expected_source_version is not None and (
        not isinstance(expected_source_version, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_source_version) is None
    ):
        raise ValueError("expected_source_version 必须是 SHA-256")

    target = _resolve_workspace_path(workspace, path)

    if not target.exists():
        raise ValueError(f"路径不存在：{path!r}")

    if not target.is_file():
        raise ValueError(f"路径不是普通文件：{path!r}")

    arguments = {"path": target.relative_to(workspace.resolve()).as_posix(),
                 "start_line": start_line, "max_lines": max_lines}
    destination = artifact_workspace or workspace

    def unavailable(reason: str) -> str:
        return reading_result(destination, "read_file", arguments, "", gap_reason=reason)

    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    deadline = time.monotonic() + READING_TIMEOUT_SECONDS
    digest = hashlib.sha256()
    decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(), translate=True)
    output = ReadingBuffer()
    line_number, line_started, selected_lines, read_bytes = 1, False, 0, 0
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("读取目标不是普通文件")
        if before.st_size > MAX_READING_SOURCE_BYTES:
            return unavailable("source_limit")
        while True:
            if time.monotonic() >= deadline:
                return unavailable("timeout")
            chunk = stream.read(min(8192, MAX_READING_SOURCE_BYTES - read_bytes + 1))
            read_bytes += len(chunk)
            if read_bytes > MAX_READING_SOURCE_BYTES:
                return unavailable("source_limit")
            digest.update(chunk)
            parts = decoder.decode(chunk, final=not chunk).split("\n")
            for index, part in enumerate(parts):
                ended = index < len(parts) - 1
                if not part and not ended:
                    continue
                if start_line <= line_number < start_line + max_lines:
                    if not line_started:
                        output.add(("\n" if selected_lines else "") + f"{line_number} | ")
                        selected_lines += 1
                    output.add(part)
                line_started = True
                if ended:
                    line_number += 1
                    line_started = False
            if not chunk:
                break
        after = os.fstat(stream.fileno())
    try:
        current = target.stat()
    except OSError:
        return unavailable("source_changed")
    if identity(before) != identity(after) or identity(before) != identity(current):
        return unavailable("source_changed")
    source_version = digest.hexdigest()
    if expected_source_version is not None and expected_source_version != source_version:
        return unavailable("source_changed")
    total_lines = line_number - 1 + int(line_started)
    has_more = total_lines >= start_line + max_lines
    return reading_result(destination, "read_file", arguments, output.text(),
                          gap_reason="capture_limit" if output.truncated else None,
                          source_version=source_version, file_has_more=has_more,
                          next_start_line=start_line + max_lines if has_more else None)


def _atomic_write(target: Path, content: str) -> None:
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            os.fchmod(f.fileno(), mode)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _validate_write_file(
    workspace: Path,
    path: str,
    content: str,
    overwrite: bool = False,
    create_parents: bool = False,
) -> None:
    if len(content) > MAX_FILE_CONTENT_CHARS:
        raise ValueError("内容超过 200,000 字符上限")

    target = _resolve_workspace_path(workspace, path)
    _reject_symlinks(workspace, path)

    if target.exists():
        if not target.is_file():
            raise ValueError(f"路径不是普通文件：{path!r}")
        if not overwrite:
            raise ValueError("文件已存在，使用 overwrite=True 覆盖")

    parent = target.parent
    if parent.exists() and not parent.is_dir():
        raise ValueError("父路径不是目录")
    if not parent.exists() and not create_parents:
        raise ValueError("父目录不存在，使用 create_parents=True 创建")


def write_file(
    workspace: Path,
    path: str,
    content: str,
    overwrite: bool = False,
    create_parents: bool = False,
    *,
    file_change_callback: FileChangeCallback | None = None,
) -> str:
    _validate_write_file(workspace, path, content, overwrite, create_parents)

    target = _resolve_workspace_path(workspace, path)
    encoded = content.encode("utf-8")
    previous = None
    executable = False
    if file_change_callback is not None and target.exists():
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("写入目标不是普通文件")
            previous = source.read(len(encoded) + 1)
            after = os.fstat(source.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise ValueError("文件在写入前发生变化")
        executable = bool(before.st_mode & 0o111)

    parent = target.parent
    if not parent.exists():
        parent.mkdir(parents=True)

    _atomic_write(target, content)
    if file_change_callback is not None and previous != encoded:
        _changed(file_change_callback, FileChange(
            target.relative_to(workspace.resolve()).as_posix(),
            previous[:512] if previous is not None else None, encoded[:512], executable,
        ))

    return f"写入 {len(content)} 字符"


def _validate_edit_file(
    workspace: Path,
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> None:
    if not old_text:
        raise ValueError("old_text 不能为空")

    target = _resolve_workspace_path(workspace, path)
    _reject_symlinks(workspace, path)

    if not target.exists():
        raise ValueError(f"路径不存在：{path!r}")

    if not target.is_file():
        raise ValueError(f"路径不是普通文件：{path!r}")

    if target.stat().st_size > MAX_FILE_CONTENT_CHARS:
        raise ValueError("文件超过 200,000 字节上限")

    content = target.read_text(encoding="utf-8")

    count = content.count(old_text)
    if count == 0:
        raise ValueError("未找到 old_text")

    if count > 1 and not replace_all:
        raise ValueError("old_text 出现多次，使用 replace_all=True 替换")


def edit_file(
    workspace: Path,
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    *,
    file_change_callback: FileChangeCallback | None = None,
) -> str:
    _validate_edit_file(workspace, path, old_text, new_text, replace_all)

    target = _resolve_workspace_path(workspace, path)
    content = target.read_text(encoding="utf-8")
    count = content.count(old_text)
    new_content = content.replace(old_text, new_text)
    executable = bool(target.stat().st_mode & 0o111)
    _atomic_write(target, new_content)
    if content != new_content:
        _changed(file_change_callback, FileChange(
            target.relative_to(workspace.resolve()).as_posix(),
            content.encode("utf-8")[:512], new_content.encode("utf-8")[:512], executable,
        ))

    return f"替换 {count} 处"


def _validate_delete_file(workspace: Path, path: str) -> None:
    target = _resolve_workspace_path(workspace, path)
    _reject_symlinks(workspace, path)

    if not target.exists():
        raise ValueError(f"文件不存在：{path!r}")

    if target.is_symlink():
        raise ValueError("不能删除符号链接")

    if not target.is_file():
        raise ValueError("只能删除普通文件")


def delete_file(
    workspace: Path,
    path: str,
    *,
    file_change_callback: FileChangeCallback | None = None,
) -> str:
    _validate_delete_file(workspace, path)

    target = _resolve_workspace_path(workspace, path)
    head = b""
    executable = bool(target.stat().st_mode & 0o111)
    if file_change_callback is not None:
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("删除目标不是普通文件")
            head = source.read(512)
    target.unlink()
    _changed(file_change_callback, FileChange(
        target.relative_to(workspace.resolve()).as_posix(), head, None, executable,
    ))

    return f"已删除：{path}"
