import os
import stat
import tempfile
from pathlib import Path

from vibe_harness.tools.common import (
    MAX_OUTPUT_CHARS,
    SKIP_NAMES,
    _is_sensitive_name,
    _is_sensitive_path,
    _reject_symlinks,
    _resolve_workspace_path,
    _truncate_output,
)

MAX_FILE_CONTENT_CHARS = 200_000


def list_files(workspace: Path, path: str = ".") -> str:
    root = workspace.resolve()
    target = _resolve_workspace_path(workspace, path)

    if not target.exists():
        raise ValueError(f"路径不存在：{path!r}")

    if not target.is_dir():
        raise ValueError(f"路径不是目录：{path!r}")

    entries = []
    for item in target.iterdir():
        if _is_sensitive_name(item.name):
            continue
        resolved = item.resolve()
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if _is_sensitive_path(rel):
            continue
        if item.is_dir():
            entries.append(f"[DIR] {item.name}")
        else:
            entries.append(f"[FILE] {item.name}")

    entries.sort()

    if len(entries) > 200:
        entries = entries[:200]

    return "\n".join(entries)


def read_file(
    workspace: Path,
    path: str,
    start_line: int = 1,
    max_lines: int = 200,
) -> str:
    if start_line < 1:
        raise ValueError("start_line 必须 >= 1")

    if not 1 <= max_lines <= 400:
        raise ValueError("max_lines 必须在 1 到 400 之间")

    target = _resolve_workspace_path(workspace, path)

    if not target.exists():
        raise ValueError(f"路径不存在：{path!r}")

    if not target.is_file():
        raise ValueError(f"路径不是普通文件：{path!r}")

    lines = []
    with target.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            if line_number < start_line:
                continue
            stripped = line.rstrip("\n")
            lines.append(f"{line_number} | {stripped}")
            if len(lines) >= max_lines:
                break

    output = "\n".join(lines)

    return _truncate_output(output, MAX_OUTPUT_CHARS)


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
) -> str:
    _validate_write_file(workspace, path, content, overwrite, create_parents)

    target = _resolve_workspace_path(workspace, path)

    parent = target.parent
    if not parent.exists():
        parent.mkdir(parents=True)

    _atomic_write(target, content)

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
) -> str:
    _validate_edit_file(workspace, path, old_text, new_text, replace_all)

    target = _resolve_workspace_path(workspace, path)
    content = target.read_text(encoding="utf-8")
    count = content.count(old_text)
    new_content = content.replace(old_text, new_text)
    _atomic_write(target, new_content)

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
) -> str:
    _validate_delete_file(workspace, path)

    target = _resolve_workspace_path(workspace, path)
    target.unlink()

    return f"已删除 {path}"
