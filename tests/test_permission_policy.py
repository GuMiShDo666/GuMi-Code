from vibe_harness.permissions.policy import (
    PermissionDecision,
    ToolRisk,
    check_hard_deny,
    decide_permission,
)


def test_read_tool_is_allowed() -> None:
    result = decide_permission("list_files", ToolRisk.READ)

    assert result.decision is PermissionDecision.ALLOW


def test_write_tool_requires_approval() -> None:
    result = decide_permission("write_file", ToolRisk.WRITE)

    assert result.decision is PermissionDecision.ASK


def test_destructive_tool_requires_approval() -> None:
    result = decide_permission("delete_file", ToolRisk.DESTRUCTIVE)

    assert result.decision is PermissionDecision.ASK


def test_execute_tool_requires_approval() -> None:
    result = decide_permission("run_command", ToolRisk.EXECUTE)

    assert result.decision is PermissionDecision.ASK


def test_unknown_tool_is_denied() -> None:
    result = decide_permission("delete_everything", None)

    assert result.decision is PermissionDecision.DENY


def test_every_decision_has_reason() -> None:
    cases = [
        ("list_files", ToolRisk.READ),
        ("write_file", ToolRisk.WRITE),
        ("delete_file", ToolRisk.DESTRUCTIVE),
        ("run_command", ToolRisk.EXECUTE),
        ("unknown_tool", None),
    ]

    for name, risk in cases:
        result = decide_permission(name, risk)
        assert result.reason
        assert isinstance(result.reason, str)


def test_hard_deny_ignores_non_command_tools() -> None:
    assert check_hard_deny("write_file", {"path": "x.txt"}) is None
    assert check_hard_deny("list_files", {"path": "."}) is None


def test_hard_deny_missing_or_invalid_argv() -> None:
    assert check_hard_deny("run_command", {}) is None
    assert check_hard_deny("run_command", {"argv": []}) is None
    assert check_hard_deny("run_command", {"argv": "not a list"}) is None
    assert check_hard_deny("run_command", {"argv": [1, 2, 3]}) is None
    assert check_hard_deny("run_command", {"argv": ["rm", 1]}) is None


def test_hard_deny_direct_programs() -> None:
    for program in ["sudo", "shutdown", "reboot", "halt", "poweroff", "dd"]:
        result = check_hard_deny("run_command", {"argv": [program, "arg"]})
        assert result is not None
        assert result.decision is PermissionDecision.DENY


def test_hard_deny_matches_program_basename() -> None:
    result = check_hard_deny(
        "run_command",
        {"argv": ["/usr/bin/sudo", "whoami"]},
    )
    assert result is not None
    assert result.decision is PermissionDecision.DENY


def test_hard_deny_mkfs_variants() -> None:
    for program in ["mkfs", "mkfs.ext4", "mkfs.xfs", "mkfs.btrfs"]:
        result = check_hard_deny("run_command", {"argv": [program, "/dev/sda1"]})
        assert result is not None
        assert result.decision is PermissionDecision.DENY


def test_hard_deny_rm_rf_root() -> None:
    cases = [
        ["rm", "-rf", "/"],
        ["rm", "-r", "-f", "/"],
        ["rm", "-fr", "/"],
        ["rm", "--recursive", "--force", "/"],
    ]
    for argv in cases:
        result = check_hard_deny("run_command", {"argv": argv})
        assert result is not None
        assert result.decision is PermissionDecision.DENY


def test_hard_deny_does_not_over_block() -> None:
    cases = [
        ["ls", "-la"],
        ["echo", "hello"],
        ["touch", "file.txt"],
        ["rm", "-rf", "somefile"],
        ["rm", "-rf", "/tmp"],
        ["rm", "-r", "/"],
        ["rm", "-f", "/"],
        ["sudoedit", "file.txt"],
        ["rebootx"],
    ]
    for argv in cases:
        assert check_hard_deny("run_command", {"argv": argv}) is None
