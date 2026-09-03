import stat
from pathlib import Path

import pytest

from vibe_harness.tools.filesystem import (
    delete_file,
    edit_file,
    list_files,
    read_file,
    write_file,
)


def _make_tree(root: Path) -> None:
    (root / ".env").write_text("SECRET")
    (root / ".DS_Store").write_text("")
    (root / "README.md").write_text("# Hi")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hi')")


# --- list_files ---


def test_list_files_hides_sensitive_names(workspace: Path) -> None:
    _make_tree(workspace)

    out = list_files(workspace)

    assert "[FILE] README.md" in out
    assert "[DIR] src" in out
    assert ".env" not in out
    assert ".DS_Store" not in out


def test_list_files_rejects_parent_traversal(workspace: Path) -> None:
    with pytest.raises(ValueError):
        list_files(workspace, "../")


def test_list_files_rejects_absolute_path(workspace: Path) -> None:
    with pytest.raises(ValueError):
        list_files(workspace, str(workspace))


def test_list_files_caps_output(workspace: Path) -> None:
    for i in range(250):
        (workspace / f"file_{i:03d}.txt").write_text("")

    out = list_files(workspace)

    assert len(out.splitlines()) == 200


def test_list_files_rejects_symlink_to_sensitive_directory(
    workspace: Path,
) -> None:
    secret_dir = workspace / ".git"
    secret_dir.mkdir()
    link = workspace / "link_dir"
    link.symlink_to(secret_dir)

    out = list_files(workspace)

    assert "[DIR] link_dir" not in out
    assert "[FILE] link_dir" not in out


# --- read_file ---


def test_read_file_returns_numbered_lines(workspace: Path) -> None:
    (workspace / "sample.txt").write_text("alpha\nbeta\n")

    out = read_file(workspace, "sample.txt")

    assert "1 | alpha" in out
    assert "2 | beta" in out


def test_read_file_respects_line_range(workspace: Path) -> None:
    (workspace / "sample.txt").write_text("\n".join(f"line{i}" for i in range(1, 11)))

    out = read_file(workspace, "sample.txt", start_line=3, max_lines=2)

    assert "3 | line3" in out
    assert "4 | line4" in out
    assert "5 | line5" not in out


def test_read_file_rejects_sensitive_path(workspace: Path) -> None:
    (workspace / ".env").write_text("SECRET")
    (workspace / ".env.local").write_text("SECRET")
    (workspace / ".git").mkdir()

    with pytest.raises(ValueError):
        read_file(workspace, ".env")

    with pytest.raises(ValueError):
        read_file(workspace, ".env.local")

    with pytest.raises(ValueError):
        read_file(workspace, ".git/config")

    with pytest.raises(ValueError):
        read_file(workspace, "folder/.env")


def test_read_file_rejects_workspace_escape(workspace: Path) -> None:
    with pytest.raises(ValueError):
        read_file(workspace, "../secret.txt")


def test_read_file_rejects_directory(workspace: Path) -> None:
    (workspace / "subdir").mkdir()

    with pytest.raises(ValueError):
        read_file(workspace, "subdir")


def test_read_file_rejects_bad_bounds(workspace: Path) -> None:
    (workspace / "sample.txt").write_text("x\n")

    with pytest.raises(ValueError):
        read_file(workspace, "sample.txt", start_line=0)

    with pytest.raises(ValueError):
        read_file(workspace, "sample.txt", max_lines=401)


def test_read_file_rejects_symlink_to_sensitive_file(workspace: Path) -> None:
    secret = workspace / ".env"
    secret.write_text("SECRET")
    link = workspace / "link.txt"
    link.symlink_to(secret)

    with pytest.raises(ValueError):
        read_file(workspace, "link.txt")


def test_read_file_caps_character_output(workspace: Path) -> None:
    (workspace / "huge.txt").write_text("x" * 25000)

    out = read_file(workspace, "huge.txt")

    assert out.endswith("[output truncated]")


# --- write_file ---


def test_write_file_creates_new_file(workspace: Path) -> None:
    out = write_file(workspace, "new.txt", "hello")

    assert (workspace / "new.txt").read_text() == "hello"
    assert stat.S_IMODE((workspace / "new.txt").stat().st_mode) == 0o644
    assert "写入" in out


def test_write_file_refuses_existing_file_by_default(workspace: Path) -> None:
    (workspace / "existing.txt").write_text("original")

    with pytest.raises(ValueError):
        write_file(workspace, "existing.txt", "changed")

    assert (workspace / "existing.txt").read_text() == "original"


def test_write_file_overwrites_when_enabled(workspace: Path) -> None:
    (workspace / "existing.txt").write_text("original")

    write_file(workspace, "existing.txt", "changed", overwrite=True)

    assert (workspace / "existing.txt").read_text() == "changed"


def test_write_file_creates_parents_when_enabled(workspace: Path) -> None:
    write_file(workspace, "a/b/c.txt", "deep", create_parents=True)

    assert (workspace / "a" / "b" / "c.txt").read_text() == "deep"


def test_write_file_rejects_escape(workspace: Path) -> None:
    with pytest.raises(ValueError):
        write_file(workspace, "../escape.txt", "x")


def test_write_file_rejects_sensitive_path(workspace: Path) -> None:
    with pytest.raises(ValueError):
        write_file(workspace, ".env", "SECRET")


def test_write_file_rejects_symlink(workspace: Path) -> None:
    target = workspace / "real.txt"
    target.write_text("real")
    link = workspace / "link.txt"
    link.symlink_to(target)

    with pytest.raises(ValueError):
        write_file(workspace, "link.txt", "x", overwrite=True)

    assert target.read_text() == "real"


def test_write_file_rejects_oversized_content(workspace: Path) -> None:
    with pytest.raises(ValueError):
        write_file(workspace, "big.txt", "x" * 200_001)


# --- edit_file ---


def test_edit_file_replaces_unique_text(workspace: Path) -> None:
    (workspace / "f.txt").write_text("hello world\n")

    out = edit_file(workspace, "f.txt", "world", "there")

    assert (workspace / "f.txt").read_text() == "hello there\n"
    assert "替换 1 处" in out


def test_edit_file_rejects_missing_text(workspace: Path) -> None:
    (workspace / "f.txt").write_text("hello\n")

    with pytest.raises(ValueError):
        edit_file(workspace, "f.txt", "nope", "x")


def test_edit_file_rejects_ambiguous_text(workspace: Path) -> None:
    (workspace / "f.txt").write_text("a a a\n")

    with pytest.raises(ValueError):
        edit_file(workspace, "f.txt", "a", "b")


def test_edit_file_replaces_all_when_enabled(workspace: Path) -> None:
    (workspace / "f.txt").write_text("a a a\n")

    out = edit_file(workspace, "f.txt", "a", "b", replace_all=True)

    assert (workspace / "f.txt").read_text() == "b b b\n"
    assert "替换 3 处" in out


def test_edit_file_rejects_empty_old_text(workspace: Path) -> None:
    (workspace / "f.txt").write_text("hello\n")

    with pytest.raises(ValueError):
        edit_file(workspace, "f.txt", "", "x")


def test_edit_file_rejects_sensitive_path(workspace: Path) -> None:
    (workspace / ".env").write_text("SECRET")

    with pytest.raises(ValueError):
        edit_file(workspace, ".env", "SECRET", "x")


def test_edit_file_rejects_symlink(workspace: Path) -> None:
    target = workspace / "real.txt"
    target.write_text("real")
    link = workspace / "link.txt"
    link.symlink_to(target)

    with pytest.raises(ValueError):
        edit_file(workspace, "link.txt", "real", "x")


def test_edit_file_preserves_original_on_failure(workspace: Path) -> None:
    (workspace / "f.txt").write_text("hello\n")

    with pytest.raises(ValueError):
        edit_file(workspace, "f.txt", "missing", "x")

    assert (workspace / "f.txt").read_text() == "hello\n"


def test_edit_file_preserves_existing_mode(workspace: Path) -> None:
    target = workspace / "script.sh"
    target.write_text("#!/bin/sh\necho old\n")
    target.chmod(0o755)

    edit_file(workspace, "script.sh", "old", "new")

    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_edit_file_rejects_oversized_file(workspace: Path) -> None:
    target = workspace / "large.txt"
    target.write_text("x" * 200_001)

    with pytest.raises(ValueError):
        edit_file(workspace, "large.txt", "x", "y", replace_all=True)


# --- delete_file ---


def test_delete_file_removes_regular_file(workspace: Path) -> None:
    (workspace / "f.txt").write_text("bye")

    delete_file(workspace, "f.txt")

    assert not (workspace / "f.txt").exists()


def test_delete_file_rejects_directory(workspace: Path) -> None:
    (workspace / "d").mkdir()

    with pytest.raises(ValueError):
        delete_file(workspace, "d")


def test_delete_file_rejects_symlink(workspace: Path) -> None:
    target = workspace / "real.txt"
    target.write_text("real")
    link = workspace / "link.txt"
    link.symlink_to(target)

    with pytest.raises(ValueError):
        delete_file(workspace, "link.txt")

    assert target.exists()


def test_delete_file_rejects_sensitive_path(workspace: Path) -> None:
    (workspace / ".env").write_text("SECRET")

    with pytest.raises(ValueError):
        delete_file(workspace, ".env")

    assert (workspace / ".env").exists()


def test_delete_file_rejects_escape(workspace: Path) -> None:
    with pytest.raises(ValueError):
        delete_file(workspace, "../x.txt")


def test_delete_file_rejects_missing_file(workspace: Path) -> None:
    with pytest.raises(ValueError):
        delete_file(workspace, "nope.txt")


# --- .transcripts is treated as sensitive ---


def test_list_files_hides_transcripts(workspace: Path) -> None:
    (workspace / ".transcripts").mkdir()
    (workspace / ".transcripts" / "transcript_test.jsonl").write_text("x\n")
    (workspace / "normal.txt").write_text("ok\n")

    out = list_files(workspace, ".")

    assert "normal.txt" in out
    assert ".transcripts" not in out


def test_read_file_rejects_transcripts(workspace: Path) -> None:
    (workspace / ".transcripts").mkdir()
    transcript = workspace / ".transcripts" / "transcript_test.jsonl"
    transcript.write_text("original\n")

    with pytest.raises(ValueError, match="禁止访问敏感或内部路径"):
        read_file(workspace, ".transcripts/transcript_test.jsonl")

    assert transcript.read_text() == "original\n"


def test_write_file_rejects_transcripts(workspace: Path) -> None:
    (workspace / ".transcripts").mkdir()

    with pytest.raises(ValueError, match="禁止访问敏感或内部路径"):
        write_file(workspace, ".transcripts/transcript_test.jsonl", "x")


def test_edit_file_rejects_transcripts(workspace: Path) -> None:
    (workspace / ".transcripts").mkdir()
    transcript = workspace / ".transcripts" / "transcript_test.jsonl"
    transcript.write_text("original\n")

    with pytest.raises(ValueError, match="禁止访问敏感或内部路径"):
        edit_file(
            workspace,
            ".transcripts/transcript_test.jsonl",
            "original",
            "changed",
        )

    assert transcript.read_text() == "original\n"


def test_delete_file_rejects_transcripts(workspace: Path) -> None:
    (workspace / ".transcripts").mkdir()
    transcript = workspace / ".transcripts" / "transcript_test.jsonl"
    transcript.write_text("original\n")

    with pytest.raises(ValueError, match="禁止访问敏感或内部路径"):
        delete_file(workspace, ".transcripts/transcript_test.jsonl")

    assert transcript.read_text() == "original\n"


# --- .task_outputs is treated as sensitive ---


def test_list_files_hides_task_outputs(workspace: Path) -> None:
    (workspace / ".task_outputs" / "tool-results").mkdir(parents=True)
    (workspace / ".task_outputs" / "tool-results" / "call.txt").write_text("x\n")
    (workspace / "normal.txt").write_text("ok\n")

    out = list_files(workspace, ".")

    assert "normal.txt" in out
    assert ".task_outputs" not in out


def test_read_file_rejects_task_outputs(workspace: Path) -> None:
    (workspace / ".task_outputs" / "tool-results").mkdir(parents=True)
    artifact = workspace / ".task_outputs" / "tool-results" / "call.txt"
    artifact.write_text("original\n")

    with pytest.raises(ValueError, match="禁止访问敏感或内部路径"):
        read_file(workspace, ".task_outputs/tool-results/call.txt")

    assert artifact.read_text() == "original\n"


def test_write_file_rejects_task_outputs(workspace: Path) -> None:
    (workspace / ".task_outputs" / "tool-results").mkdir(parents=True)

    with pytest.raises(ValueError, match="禁止访问敏感或内部路径"):
        write_file(workspace, ".task_outputs/tool-results/call.txt", "x")


def test_edit_file_rejects_task_outputs(workspace: Path) -> None:
    (workspace / ".task_outputs" / "tool-results").mkdir(parents=True)
    artifact = workspace / ".task_outputs" / "tool-results" / "call.txt"
    artifact.write_text("original\n")

    with pytest.raises(ValueError, match="禁止访问敏感或内部路径"):
        edit_file(
            workspace,
            ".task_outputs/tool-results/call.txt",
            "original",
            "changed",
        )

    assert artifact.read_text() == "original\n"


def test_delete_file_rejects_task_outputs(workspace: Path) -> None:
    (workspace / ".task_outputs" / "tool-results").mkdir(parents=True)
    artifact = workspace / ".task_outputs" / "tool-results" / "call.txt"
    artifact.write_text("original\n")

    with pytest.raises(ValueError, match="禁止访问敏感或内部路径"):
        delete_file(workspace, ".task_outputs/tool-results/call.txt")

    assert artifact.read_text() == "original\n"
