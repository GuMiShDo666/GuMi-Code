import json
from pathlib import Path

import pytest

from vibe_harness.tools.artifacts import (
    MAX_ARTIFACT_FILE_BYTES,
    read_tool_output,
)

RESULT_PATH = ".task_outputs/tool-results/call_test.txt"


def _make_result(workspace: Path, content: str) -> None:
    results_dir = workspace / ".task_outputs" / "tool-results"
    results_dir.mkdir(parents=True)
    (results_dir / "call_test.txt").write_text(content, encoding="utf-8")


def test_read_tool_output_returns_json_with_content(workspace: Path) -> None:
    content = "SYNTHETIC_RESULT_LINE_1\nSYNTHETIC_RESULT_LINE_2\n"
    _make_result(workspace, content)

    data = json.loads(read_tool_output(workspace, RESULT_PATH))

    assert data["path"] == RESULT_PATH
    assert data["offset"] == 0
    assert data["content"] == content
    assert data["next_offset"] == len(content)
    assert data["complete"] is True
    assert "1 |" not in data["content"]


def test_read_tool_output_paginates_and_rejoins(workspace: Path) -> None:
    content = "0123456789"
    _make_result(workspace, content)

    chunks: list[str] = []
    offset = 0
    while True:
        data = json.loads(
            read_tool_output(workspace, RESULT_PATH, offset=offset, max_chars=3)
        )
        chunks.append(data["content"])
        offset = data["next_offset"]
        if data["complete"]:
            break

    assert "".join(chunks) == content
    assert offset == len(content)


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "/tmp/outside.txt",
        ".transcripts/history.jsonl",
        ".task_outputs/other/file.txt",
        ".task_outputs/tool-results/sub/file.txt",
        ".task_outputs/tool-results/no_extension",
    ],
)
def test_read_tool_output_rejects_invalid_path(workspace: Path, path: str) -> None:
    with pytest.raises(ValueError):
        read_tool_output(workspace, path)


def test_read_tool_output_rejects_missing_file(workspace: Path) -> None:
    with pytest.raises(ValueError):
        read_tool_output(workspace, RESULT_PATH)


def test_read_tool_output_rejects_symlink(workspace: Path) -> None:
    results_dir = workspace / ".task_outputs" / "tool-results"
    results_dir.mkdir(parents=True)
    real = workspace / "secret.txt"
    real.write_text("SYNTHETIC_SECRET\n", encoding="utf-8")
    link = results_dir / "link.txt"
    link.symlink_to(real)

    with pytest.raises(ValueError):
        read_tool_output(workspace, ".task_outputs/tool-results/link.txt")


def test_read_tool_output_rejects_non_utf8(workspace: Path) -> None:
    results_dir = workspace / ".task_outputs" / "tool-results"
    results_dir.mkdir(parents=True)
    (results_dir / "bad.txt").write_bytes(b"\xff\xfe\x00\x00")

    with pytest.raises(ValueError):
        read_tool_output(workspace, ".task_outputs/tool-results/bad.txt")


def test_read_tool_output_rejects_file_over_size_limit(workspace: Path) -> None:
    results_dir = workspace / ".task_outputs" / "tool-results"
    results_dir.mkdir(parents=True)
    oversized = results_dir / "oversized.txt"
    oversized.write_bytes(b"x" * (MAX_ARTIFACT_FILE_BYTES + 1))

    with pytest.raises(ValueError, match="5 MB"):
        read_tool_output(
            workspace,
            ".task_outputs/tool-results/oversized.txt",
        )


def test_read_tool_output_rejects_bad_offset(workspace: Path) -> None:
    _make_result(workspace, "abc")

    with pytest.raises(ValueError):
        read_tool_output(workspace, RESULT_PATH, offset=-1)

    with pytest.raises(ValueError):
        read_tool_output(workspace, RESULT_PATH, offset=True)


def test_read_tool_output_rejects_bad_max_chars(workspace: Path) -> None:
    _make_result(workspace, "abc")

    with pytest.raises(ValueError):
        read_tool_output(workspace, RESULT_PATH, max_chars=0)

    with pytest.raises(ValueError):
        read_tool_output(workspace, RESULT_PATH, max_chars=20001)

    with pytest.raises(ValueError):
        read_tool_output(workspace, RESULT_PATH, max_chars=True)
