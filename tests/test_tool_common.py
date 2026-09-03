from pathlib import Path

import pytest

from vibe_harness.tools.common import (
    _is_sensitive_name,
    _resolve_workspace_path,
    _truncate_output,
)


def test_resolve_workspace_path_returns_target(workspace: Path) -> None:
    (workspace / "src").mkdir()

    target = _resolve_workspace_path(workspace, "src")

    assert target == (workspace / "src").resolve()


def test_resolve_workspace_path_rejects_absolute(workspace: Path) -> None:
    with pytest.raises(ValueError):
        _resolve_workspace_path(workspace, str(workspace))


def test_resolve_workspace_path_rejects_escape(workspace: Path) -> None:
    with pytest.raises(ValueError):
        _resolve_workspace_path(workspace, "../")


def test_resolve_workspace_path_rejects_sensitive(workspace: Path) -> None:
    with pytest.raises(ValueError):
        _resolve_workspace_path(workspace, ".env")

    with pytest.raises(ValueError):
        _resolve_workspace_path(workspace, ".env.local")

    with pytest.raises(ValueError):
        _resolve_workspace_path(workspace, ".npmrc")


def test_sensitive_name_allows_env_templates() -> None:
    assert not _is_sensitive_name(".env.example")
    assert not _is_sensitive_name(".env.sample")
    assert not _is_sensitive_name(".env.template")


def test_truncate_output_appends_marker() -> None:
    out = _truncate_output("x" * 100, max_chars=50)

    assert out.endswith("[output truncated]")


def test_truncate_output_leaves_short_text_untouched() -> None:
    out = _truncate_output("short", max_chars=50)

    assert out == "short"
