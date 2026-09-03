from pathlib import Path

import pytest

from vibe_harness.tools.search import find_files, search_text


# --- find_files ---


def test_find_files_matches_nested_files(workspace: Path) -> None:
    (workspace / "src" / "pkg").mkdir(parents=True)
    (workspace / "src" / "pkg" / "a.py").write_text("")
    (workspace / "tests" / "b.py").mkdir(parents=True)
    (workspace / "tests" / "b.py" / "c.py").write_text("")

    out = find_files(workspace, "**/*.py")

    assert "src/pkg/a.py" in out
    assert "tests/b.py/c.py" in out


def test_find_files_respects_start_path(workspace: Path) -> None:
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text("")
    (workspace / "tests").mkdir()
    (workspace / "tests" / "b.py").write_text("")

    out = find_files(workspace, "*.py", path="src")

    assert "src/a.py" in out
    assert "tests/b.py" not in out


def test_find_files_hides_sensitive_paths(workspace: Path) -> None:
    (workspace / ".venv").mkdir()
    (workspace / ".venv" / "x.py").write_text("")
    (workspace / ".env.local").write_text("SECRET")
    (workspace / "ok.py").write_text("")

    out = find_files(workspace, "**/*.py")

    assert "ok.py" in out
    assert ".venv" not in out
    assert ".env.local" not in out


def test_find_files_rejects_escape(workspace: Path) -> None:
    with pytest.raises(ValueError):
        find_files(workspace, "**/*.py", path="../")

    with pytest.raises(ValueError):
        find_files(workspace, "../outside.py")


def test_find_files_caps_results(workspace: Path) -> None:
    for i in range(250):
        (workspace / f"f_{i:03d}.py").write_text("")

    out = find_files(workspace, "*.py")

    assert len(out.splitlines()) == 200


def test_find_files_does_not_follow_symlink_directories(workspace: Path) -> None:
    (workspace / "real").mkdir()
    (workspace / "real" / "a.py").write_text("")
    link = workspace / "link"
    link.symlink_to(workspace / "real")

    out = find_files(workspace, "**/*.py")

    assert "real/a.py" in out
    assert "link/a.py" not in out


# --- search_text ---


def test_search_text_finds_match_with_line_number(workspace: Path) -> None:
    (workspace / "a.py").write_text("def foo():\n    return 1\n")

    out = search_text(workspace, "foo")

    assert "a.py:1:def foo()" in out


def test_search_text_supports_case_sensitive_mode(workspace: Path) -> None:
    (workspace / "a.txt").write_text("Hello\n")

    insensitive = search_text(workspace, "hello")
    sensitive = search_text(workspace, "hello", case_sensitive=True)

    assert "a.txt" in insensitive
    assert sensitive == "(no matches)"


def test_search_text_filters_by_glob(workspace: Path) -> None:
    (workspace / "a.py").write_text("needle\n")
    (workspace / "b.txt").write_text("needle\n")

    out = search_text(workspace, "needle", file_glob="*.py")

    assert "a.py" in out
    assert "b.txt" not in out


def test_search_text_supports_regex(workspace: Path) -> None:
    (workspace / "a.txt").write_text("abc123\n")

    out = search_text(workspace, r"\d{3}", use_regex=True)

    assert "abc123" in out


def test_search_text_hides_sensitive_files(workspace: Path) -> None:
    (workspace / ".env").write_text("SECRET_TOKEN=xyz\n")
    (workspace / ".env.local").write_text("SECRET_TOKEN=local\n")
    (workspace / "ok.txt").write_text("SECRET_TOKEN=xyz\n")

    out = search_text(workspace, "SECRET_TOKEN")

    assert "ok.txt" in out
    assert ".env" not in out
    assert ".env.local" not in out


def test_search_text_returns_relative_paths(workspace: Path) -> None:
    (workspace / "a.py").write_text("unique_probe\n")

    out = search_text(workspace, "unique_probe")

    assert str(workspace) not in out
    assert out.startswith("a.py:")


def test_search_text_rejects_escape(workspace: Path) -> None:
    with pytest.raises(ValueError):
        search_text(workspace, "x", path="../")


def test_search_text_caps_results(workspace: Path) -> None:
    for i in range(150):
        (workspace / f"f{i}.txt").write_text("match\n")

    out = search_text(workspace, "match")

    assert len(out.splitlines()) <= 100


def test_search_text_returns_no_matches(workspace: Path) -> None:
    (workspace / "a.txt").write_text("nothing here\n")

    out = search_text(workspace, "zzz_not_found")

    assert out == "(no matches)"


def test_search_text_file_glob_cannot_reinclude_env(workspace: Path) -> None:
    (workspace / ".env").write_text(
        "SYNTHETIC_SECRET=hidden\n",
        encoding="utf-8",
    )

    out = search_text(
        workspace,
        "SYNTHETIC_SECRET",
        file_glob=".env",
    )

    assert out == "(no matches)"
    assert "hidden" not in out


def test_search_text_wildcard_still_hides_sensitive_files(workspace: Path) -> None:
    (workspace / ".env").write_text(
        "shared_marker=hidden\n",
        encoding="utf-8",
    )
    (workspace / "safe.txt").write_text(
        "shared_marker=visible\n",
        encoding="utf-8",
    )

    out = search_text(
        workspace,
        "shared_marker",
        file_glob="**",
    )

    assert "safe.txt" in out
    assert "visible" in out
    assert ".env" not in out
    assert "hidden" not in out


def test_search_text_hides_private_key_pem_and_nested_env(workspace: Path) -> None:
    (workspace / "id_rsa").write_text("PRIVATE_KEY_MARKER\n", encoding="utf-8")
    (workspace / "certificate.pem").write_text("CERTIFICATE_MARKER\n", encoding="utf-8")
    (workspace / "nested").mkdir()
    (workspace / "nested" / ".env.local").write_text(
        "NESTED_ENV_MARKER\n",
        encoding="utf-8",
    )

    assert search_text(workspace, "PRIVATE_KEY_MARKER", file_glob="id_rsa") == (
        "(no matches)"
    )
    assert search_text(workspace, "CERTIFICATE_MARKER", file_glob="certificate.pem") == (
        "(no matches)"
    )
    assert search_text(workspace, "NESTED_ENV_MARKER", file_glob="**") == "(no matches)"


def test_search_text_rejects_escaping_file_glob(workspace: Path) -> None:
    with pytest.raises(
        ValueError,
        match="file_glob 不能越出工作目录",
    ):
        search_text(
            workspace,
            "needle",
            file_glob="../*.py",
        )


def test_search_text_rejects_absolute_file_glob(workspace: Path) -> None:
    with pytest.raises(
        ValueError,
        match="file_glob 不能越出工作目录",
    ):
        search_text(
            workspace,
            "needle",
            file_glob="/tmp/*.py",
        )


def test_find_files_hides_transcripts(workspace: Path) -> None:
    (workspace / ".transcripts").mkdir()
    (workspace / ".transcripts" / "transcript_test.jsonl").write_text("x\n")
    (workspace / "normal.txt").write_text("ok\n")

    out = find_files(workspace, "**/*")

    assert "normal.txt" in out
    assert ".transcripts" not in out


def test_search_text_hides_transcripts(workspace: Path) -> None:
    (workspace / ".transcripts").mkdir()
    (workspace / ".transcripts" / "transcript_test.jsonl").write_text(
        "SYNTHETIC_TRANSCRIPT_MARKER\n",
        encoding="utf-8",
    )
    (workspace / "normal.txt").write_text(
        "SYNTHETIC_TRANSCRIPT_MARKER\n",
        encoding="utf-8",
    )

    hidden = search_text(
        workspace,
        "SYNTHETIC_TRANSCRIPT_MARKER",
        file_glob=".transcripts/**",
    )
    assert hidden == "(no matches)"

    visible = search_text(workspace, "SYNTHETIC_TRANSCRIPT_MARKER")
    assert "normal.txt" in visible
    assert ".transcripts" not in visible


def test_find_files_hides_task_outputs(workspace: Path) -> None:
    (workspace / ".task_outputs" / "tool-results").mkdir(parents=True)
    (workspace / ".task_outputs" / "tool-results" / "call.txt").write_text("x\n")
    (workspace / "normal.txt").write_text("ok\n")

    out = find_files(workspace, "**/*")

    assert "normal.txt" in out
    assert ".task_outputs" not in out


def test_search_text_hides_task_outputs(workspace: Path) -> None:
    (workspace / ".task_outputs" / "tool-results").mkdir(parents=True)
    (workspace / ".task_outputs" / "tool-results" / "call.txt").write_text(
        "SYNTHETIC_OUTPUT_MARKER\n",
        encoding="utf-8",
    )
    (workspace / "normal.txt").write_text(
        "SYNTHETIC_OUTPUT_MARKER\n",
        encoding="utf-8",
    )

    hidden = search_text(
        workspace,
        "SYNTHETIC_OUTPUT_MARKER",
        file_glob=".task_outputs/**",
    )
    assert hidden == "(no matches)"

    visible = search_text(workspace, "SYNTHETIC_OUTPUT_MARKER")
    assert "normal.txt" in visible
    assert ".task_outputs" not in visible
