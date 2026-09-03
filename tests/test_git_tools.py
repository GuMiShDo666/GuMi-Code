import subprocess
from pathlib import Path

from vibe_harness.tools.git import git_diff, git_status


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=path, check=True
    )


def _commit(path: Path, filename: str, content: str) -> None:
    (path / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "add"], cwd=path, check=True)


# --- git_status ---


def test_git_status_shows_untracked_file(workspace: Path) -> None:
    _init_repo(workspace)
    (workspace / "new.txt").write_text("hi")

    out = git_status(workspace)

    assert "new.txt" in out


def test_git_status_handles_non_repository(workspace: Path) -> None:
    out = git_status(workspace)

    assert out.startswith("Error:")


def test_git_status_filters_sensitive_names(workspace: Path) -> None:
    _init_repo(workspace)
    (workspace / ".env").write_text("SECRET")
    (workspace / "ok.txt").write_text("hi")

    out = git_status(workspace)

    assert "ok.txt" in out
    assert ".env" not in out


# --- git_diff ---


def test_git_diff_shows_unstaged_change(workspace: Path) -> None:
    _init_repo(workspace)
    _commit(workspace, "a.txt", "hello\n")

    (workspace / "a.txt").write_text("changed\n")

    out = git_diff(workspace)

    assert "+changed" in out


def test_git_diff_shows_staged_change(workspace: Path) -> None:
    _init_repo(workspace)
    _commit(workspace, "a.txt", "hello\n")

    (workspace / "a.txt").write_text("staged\n")
    subprocess.run(["git", "add", "a.txt"], cwd=workspace, check=True)

    out = git_diff(workspace, staged=True)

    assert "+staged" in out


def test_git_diff_returns_no_changes(workspace: Path) -> None:
    _init_repo(workspace)
    _commit(workspace, "a.txt", "hello\n")

    out = git_diff(workspace)

    assert out == "(no changes)"


def test_git_diff_handles_non_repository(workspace: Path) -> None:
    out = git_diff(workspace)

    assert out.startswith("Error:")


def test_git_diff_excludes_sensitive_file_content(workspace: Path) -> None:
    _init_repo(workspace)
    _commit(workspace, ".env", "OLD_SECRET\n")
    _commit(workspace, "ok.txt", "hello\n")

    (workspace / ".env").write_text("NEW_SECRET\n")
    (workspace / "ok.txt").write_text("changed\n")

    out = git_diff(workspace)

    assert "NEW_SECRET" not in out
    assert "OLD_SECRET" not in out
    assert "+changed" in out


def test_git_diff_excludes_env_variant_content(workspace: Path) -> None:
    _init_repo(workspace)
    _commit(workspace, ".env.local", "OLD_LOCAL_SECRET\n")
    _commit(workspace, "ok.txt", "hello\n")

    (workspace / ".env.local").write_text("NEW_LOCAL_SECRET\n")
    (workspace / "ok.txt").write_text("changed\n")

    out = git_diff(workspace)

    assert "NEW_LOCAL_SECRET" not in out
    assert "OLD_LOCAL_SECRET" not in out
    assert "+changed" in out


def test_git_diff_truncates_large_output(workspace: Path) -> None:
    _init_repo(workspace)
    _commit(workspace, "a.txt", "start\n")

    (workspace / "a.txt").write_text("x" * 25000 + "\n")

    out = git_diff(workspace)

    assert out.endswith("[output truncated]")
