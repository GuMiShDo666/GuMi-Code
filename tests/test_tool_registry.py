import json
from pathlib import Path

from vibe_harness.permissions.policy import ToolRisk
from vibe_harness.tools import registry


def test_schema_names_match_registry_keys() -> None:
    for name, schema in registry.ALL_TOOL_SCHEMAS.items():
        assert schema["function"]["name"] == name


def test_every_schema_has_handler() -> None:
    assert set(registry.ALL_TOOL_SCHEMAS) == set(registry.ALL_TOOL_HANDLERS)


def test_risk_sets_do_not_overlap() -> None:
    assert registry.READ_ONLY_TOOL_NAMES.isdisjoint(registry.MUTATING_TOOL_NAMES)
    assert registry.READ_ONLY_TOOL_NAMES.isdisjoint(registry.DESTRUCTIVE_TOOL_NAMES)
    assert registry.READ_ONLY_TOOL_NAMES.isdisjoint(registry.EXECUTION_TOOL_NAMES)
    assert registry.MUTATING_TOOL_NAMES.isdisjoint(registry.DESTRUCTIVE_TOOL_NAMES)
    assert registry.MUTATING_TOOL_NAMES.isdisjoint(registry.EXECUTION_TOOL_NAMES)
    assert registry.DESTRUCTIVE_TOOL_NAMES.isdisjoint(registry.EXECUTION_TOOL_NAMES)


def test_every_tool_has_risk() -> None:
    assert set(registry.ALL_TOOL_RISKS) == set(registry.ALL_TOOL_HANDLERS)


def test_risk_map_matches_schema_and_handler_names() -> None:
    assert set(registry.ALL_TOOL_RISKS) == set(registry.ALL_TOOL_SCHEMAS)
    assert set(registry.ALL_TOOL_RISKS) == set(registry.ALL_TOOL_HANDLERS)


def test_model_visible_names_exclude_execution_tools() -> None:
    assert registry.MODEL_VISIBLE_TOOL_NAMES == (
        frozenset(registry.ALL_TOOL_RISKS) - registry.EXECUTION_TOOL_NAMES
    )


def test_default_model_visible_names_exclude_run_command() -> None:
    assert "run_command" not in registry.MODEL_VISIBLE_TOOL_NAMES


def test_default_tool_schemas_exclude_run_command() -> None:
    names = {schema["function"]["name"] for schema in registry.TOOL_SCHEMAS}
    assert "run_command" not in names


def test_get_tool_schemas_enables_run_command() -> None:
    names = {
        schema["function"]["name"]
        for schema in registry.get_tool_schemas(allow_run_command=True)
    }
    assert "run_command" in names


def test_full_registries_still_contain_run_command() -> None:
    assert "run_command" in registry.ALL_TOOL_RISKS
    assert "run_command" in registry.ALL_TOOL_SCHEMAS
    assert "run_command" in registry.ALL_TOOL_HANDLERS
    assert "run_command" in registry.PREFLIGHTS


def test_tool_schemas_expose_only_model_visible() -> None:
    exposed = {schema["function"]["name"] for schema in registry.TOOL_SCHEMAS}
    assert exposed == set(registry.MODEL_VISIBLE_TOOL_NAMES)


def test_write_file_requires_approval(workspace: Path) -> None:
    out = registry.execute_tool(
        workspace,
        "write_file",
        '{"path": "x.txt", "content": "hi"}',
    )

    assert out == "Error: Approval required: write_file"
    assert not (workspace / "x.txt").exists()


def test_edit_file_requires_approval(workspace: Path) -> None:
    (workspace / "x.txt").write_text("original")

    out = registry.execute_tool(
        workspace,
        "edit_file",
        '{"path": "x.txt", "old_text": "original", "new_text": "changed"}',
    )

    assert out == "Error: Approval required: edit_file"
    assert (workspace / "x.txt").read_text() == "original"


def test_delete_file_requires_approval(workspace: Path) -> None:
    (workspace / "x.txt").write_text("hi")

    out = registry.execute_tool(
        workspace,
        "delete_file",
        '{"path": "x.txt"}',
    )

    assert out == "Error: Approval required: delete_file"
    assert (workspace / "x.txt").exists()


def test_run_command_requires_approval(workspace: Path) -> None:
    marker = workspace / "marker.txt"

    out = registry.execute_tool(
        workspace,
        "run_command",
        '{"argv": ["touch", "marker.txt"]}',
    )

    assert out == "Error: Approval required: run_command"
    assert not marker.exists()


def test_write_file_executes_when_approved(workspace: Path) -> None:
    out = registry.execute_tool(
        workspace,
        "write_file",
        '{"path": "x.txt", "content": "hi"}',
        approval_callback=lambda name, args, reason: True,
    )

    assert "写入 2 字符" in out
    assert (workspace / "x.txt").read_text() == "hi"


def test_write_file_denied_by_user(workspace: Path) -> None:
    out = registry.execute_tool(
        workspace,
        "write_file",
        '{"path": "x.txt", "content": "hi"}',
        approval_callback=lambda name, args, reason: False,
    )

    assert out == "Error: Tool execution denied by user: write_file"
    assert not (workspace / "x.txt").exists()


def test_delete_file_executes_after_approval(workspace: Path) -> None:
    (workspace / "x.txt").write_text("hi")

    registry.execute_tool(
        workspace,
        "delete_file",
        '{"path": "x.txt"}',
        approval_callback=lambda name, args, reason: True,
    )

    assert not (workspace / "x.txt").exists()


def test_run_command_executes_after_approval(workspace: Path) -> None:
    marker = workspace / "marker.txt"

    registry.execute_tool(
        workspace,
        "run_command",
        '{"argv": ["touch", "marker.txt"]}',
        approval_callback=lambda name, args, reason: True,
    )

    assert marker.exists()


def test_approval_callback_receives_arguments_and_reason(
    workspace: Path,
) -> None:
    captured: dict[str, object] = {}

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        captured["name"] = name
        captured["args"] = args
        captured["reason"] = reason
        return True

    registry.execute_tool(
        workspace,
        "write_file",
        '{"path": "x.txt", "content": "hi"}',
        approval_callback=callback,
    )

    assert captured["name"] == "write_file"
    assert captured["args"] == {"path": "x.txt", "content": "hi"}
    assert captured["reason"]


def test_read_tool_does_not_require_approval(workspace: Path) -> None:
    (workspace / "README.md").write_text("# Hi")

    out = registry.execute_tool(
        workspace,
        "list_files",
        '{"path": "."}',
        approval_callback=lambda name, args, reason: False,
    )

    assert "[FILE] README.md" in out


def test_execute_tool_dispatches_list_files(workspace: Path) -> None:
    (workspace / "README.md").write_text("# Hi")
    (workspace / "src").mkdir()

    out = registry.execute_tool(workspace, "list_files", '{"path": "."}')

    assert "[FILE] README.md" in out
    assert "[DIR] src" in out


def test_execute_tool_rejects_unknown_tool(workspace: Path) -> None:
    out = registry.execute_tool(workspace, "delete_everything", "{}")

    assert out.startswith("Error: Unknown tool:")


def test_execute_tool_rejects_invalid_json(workspace: Path) -> None:
    out = registry.execute_tool(workspace, "list_files", "{invalid json")

    assert out.startswith("Error: Invalid tool arguments:")


def test_execute_tool_returns_handler_error(workspace: Path) -> None:
    out = registry.execute_tool(workspace, "list_files", '{"path": "../"}')

    assert out.startswith("Error: 路径越界")


def test_execute_tool_returns_specific_path_error(workspace: Path) -> None:
    out = registry.execute_tool(workspace, "read_file", '{"path": "missing.txt"}')

    assert "路径不存在" in out


def test_execute_tool_returns_sensitive_path_error(workspace: Path) -> None:
    (workspace / ".env").write_text("SECRET")

    out = registry.execute_tool(workspace, "read_file", '{"path": ".env"}')

    assert "禁止访问敏感或内部路径" in out


def test_execute_tool_returns_utf8_error(workspace: Path) -> None:
    (workspace / "bad.txt").write_bytes(b"\xff\xfe\x00")

    out = registry.execute_tool(workspace, "read_file", '{"path": "bad.txt"}')

    assert out == "Error: 文件不是有效 UTF-8 文本"


def test_execute_tool_returns_invalid_arguments_for_missing_param(
    workspace: Path,
) -> None:
    out = registry.execute_tool(workspace, "read_file", "{}")

    assert out == "Error: Invalid tool arguments"


def test_execute_tool_returns_invalid_arguments_for_extra_param(
    workspace: Path,
) -> None:
    out = registry.execute_tool(
        workspace,
        "list_files",
        '{"path": ".", "extra": "x"}',
    )

    assert out == "Error: Invalid tool arguments"


def test_execute_tool_hides_oserror_details(
    workspace: Path,
    monkeypatch,
) -> None:
    def boom(**kwargs: object) -> str:
        raise OSError("sensitive /etc/passwd detail")

    monkeypatch.setitem(registry.ALL_TOOL_HANDLERS, "list_files", boom)

    out = registry.execute_tool(workspace, "list_files", '{"path": "."}')

    assert out == "Error: Tool execution failed"
    assert "/etc/passwd" not in out


def test_read_tool_output_is_read_risk() -> None:
    assert registry.ALL_TOOL_RISKS["read_tool_output"] is ToolRisk.READ
    assert "read_tool_output" in registry.READ_ONLY_TOOL_NAMES


def test_read_tool_output_does_not_require_approval(workspace: Path) -> None:
    results_dir = workspace / ".task_outputs" / "tool-results"
    results_dir.mkdir(parents=True)
    (results_dir / "call_test.txt").write_text("SYNTHETIC\n", encoding="utf-8")

    out = registry.execute_tool(
        workspace,
        "read_tool_output",
        '{"path": ".task_outputs/tool-results/call_test.txt"}',
        approval_callback=lambda name, args, reason: False,
    )

    data = json.loads(out)
    assert data["content"] == "SYNTHETIC\n"


def test_read_tool_output_rejects_other_paths_via_registry(workspace: Path) -> None:
    out = registry.execute_tool(
        workspace,
        "read_tool_output",
        '{"path": ".transcripts/history.jsonl"}',
    )

    assert out == "Error: path 必须在 .task_outputs/tool-results 目录下"


# --- 审批前 preflight ---


def test_preflight_covers_all_approval_tools() -> None:
    approval_tools = {
        name
        for name, risk in registry.ALL_TOOL_RISKS.items()
        if risk is not ToolRisk.READ
    }

    assert set(registry.PREFLIGHTS) == approval_tools


def test_write_env_skips_approval(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = registry.execute_tool(
        workspace,
        "write_file",
        '{"path": ".env", "content": "SECRET"}',
        approval_callback=callback,
    )

    assert calls == []
    assert "禁止访问敏感或内部路径" in out


def test_write_file_existing_without_overwrite_skips_approval(
    workspace: Path,
) -> None:
    (workspace / "x.txt").write_text("existing")
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = registry.execute_tool(
        workspace,
        "write_file",
        '{"path": "x.txt", "content": "hi"}',
        approval_callback=callback,
    )

    assert calls == []
    assert "文件已存在" in out
    assert (workspace / "x.txt").read_text() == "existing"


def test_write_file_directory_target_skips_approval(workspace: Path) -> None:
    (workspace / "target").mkdir()
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = registry.execute_tool(
        workspace,
        "write_file",
        '{"path": "target", "content": "hi", "overwrite": true}',
        approval_callback=callback,
    )

    assert calls == []
    assert "路径不是普通文件" in out
    assert (workspace / "target").is_dir()


def test_write_file_non_directory_parent_skips_approval(workspace: Path) -> None:
    (workspace / "parent").write_text("existing")
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = registry.execute_tool(
        workspace,
        "write_file",
        (
            '{"path": "parent/child.txt", "content": "hi", '
            '"create_parents": true}'
        ),
        approval_callback=callback,
    )

    assert calls == []
    assert "父路径不是目录" in out
    assert (workspace / "parent").read_text() == "existing"


def test_edit_file_missing_old_text_skips_approval(workspace: Path) -> None:
    (workspace / "x.txt").write_text("hello")
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = registry.execute_tool(
        workspace,
        "edit_file",
        '{"path": "x.txt", "old_text": "nope", "new_text": "x"}',
        approval_callback=callback,
    )

    assert calls == []
    assert "未找到 old_text" in out
    assert (workspace / "x.txt").read_text() == "hello"


def test_delete_missing_file_skips_approval(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = registry.execute_tool(
        workspace,
        "delete_file",
        '{"path": "nope.txt"}',
        approval_callback=callback,
    )

    assert calls == []
    assert "文件不存在" in out


def test_run_command_empty_argv_skips_approval(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = registry.execute_tool(
        workspace,
        "run_command",
        '{"argv": []}',
        approval_callback=callback,
    )

    assert calls == []
    assert "argv 不能为空" in out


def test_run_command_missing_cwd_skips_approval(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = registry.execute_tool(
        workspace,
        "run_command",
        '{"argv": ["echo", "hi"], "cwd": "nope"}',
        approval_callback=callback,
    )

    assert calls == []
    assert "cwd 不存在" in out


def test_write_file_denied_does_not_create_parents(workspace: Path) -> None:
    out = registry.execute_tool(
        workspace,
        "write_file",
        '{"path": "a/b/c.txt", "content": "hi", "create_parents": true}',
        approval_callback=lambda name, args, reason: False,
    )

    assert out == "Error: Tool execution denied by user: write_file"
    assert not (workspace / "a").exists()


def test_valid_write_approves_exactly_once(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = registry.execute_tool(
        workspace,
        "write_file",
        '{"path": "x.txt", "content": "hi"}',
        approval_callback=callback,
    )

    assert calls == ["write_file"]
    assert (workspace / "x.txt").read_text() == "hi"


def test_run_command_hard_deny_skips_approval(workspace: Path) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = registry.execute_tool(
        workspace,
        "run_command",
        '{"argv": ["sudo", "whoami"]}',
        approval_callback=callback,
    )

    assert calls == []
    assert out == "Error: Tool denied by policy: run_command"


def test_run_command_hard_deny_rm_rf_root_skips_approval(
    workspace: Path,
) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = registry.execute_tool(
        workspace,
        "run_command",
        '{"argv": ["rm", "-rf", "/"]}',
        approval_callback=callback,
    )

    assert calls == []
    assert out == "Error: Tool denied by policy: run_command"


def test_run_command_hard_deny_skips_handler(
    workspace: Path,
    monkeypatch,
) -> None:
    def boom(**kwargs: object) -> str:
        raise AssertionError("handler must not run for hard-deny command")

    monkeypatch.setitem(registry.ALL_TOOL_HANDLERS, "run_command", boom)

    out = registry.execute_tool(
        workspace,
        "run_command",
        '{"argv": ["shutdown", "-h", "now"]}',
        approval_callback=lambda name, args, reason: True,
    )

    assert out == "Error: Tool denied by policy: run_command"


def test_run_command_safe_command_still_requires_approval(
    workspace: Path,
) -> None:
    calls: list[str] = []

    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        calls.append(name)
        return True

    out = registry.execute_tool(
        workspace,
        "run_command",
        '{"argv": ["echo", "hello"]}',
        approval_callback=callback,
    )

    assert calls == ["run_command"]


def test_write_file_revalidates_after_approval(workspace: Path) -> None:
    def callback(name: str, args: dict[str, object], reason: str) -> bool:
        (workspace / "x.txt").write_text("sneaky")
        return True

    out = registry.execute_tool(
        workspace,
        "write_file",
        '{"path": "x.txt", "content": "hi"}',
        approval_callback=callback,
    )

    assert "文件已存在" in out
    assert (workspace / "x.txt").read_text() == "sneaky"
