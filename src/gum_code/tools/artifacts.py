import json
from pathlib import Path

from gum_code.tools.common import _reject_symlinks

EXPECTED_RESULT_PARENT = Path(".task_outputs/tool-results")
MAX_ARTIFACT_FILE_BYTES = 5 * 1024 * 1024
MAX_CHARS_LIMIT = 20_000


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

    if requested.parent != EXPECTED_RESULT_PARENT:
        raise ValueError("path 必须在 .task_outputs/tool-results 目录下")

    if requested.suffix != ".txt":
        raise ValueError("只允许读取 .txt 结果文件")

    root = workspace.resolve()

    _reject_symlinks(workspace, path)

    expected_dir = (root / EXPECTED_RESULT_PARENT).resolve()
    target = (root / requested).resolve()

    if target.parent != expected_dir:
        raise ValueError("path 必须在 .task_outputs/tool-results 目录下")

    if not target.exists():
        raise ValueError(f"文件不存在：{path!r}")

    if not target.is_file():
        raise ValueError(f"路径不是普通文件：{path!r}")

    if target.stat().st_size > MAX_ARTIFACT_FILE_BYTES:
        raise ValueError("文件超过 5 MB 上限")

    content = target.read_text(encoding="utf-8")

    chunk = content[offset : offset + max_chars]

    return json.dumps(
        {
            "path": path,
            "offset": offset,
            "next_offset": offset + len(chunk),
            "complete": offset + len(chunk) >= len(content),
            "content": chunk,
        },
        ensure_ascii=False,
    )
