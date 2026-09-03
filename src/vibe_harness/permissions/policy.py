from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ToolRisk(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    EXECUTE = "execute"


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionResult:
    decision: PermissionDecision
    reason: str


@dataclass(frozen=True)
class ToolPolicy:
    risk: ToolRisk
    enabled: bool


def decide_permission(
    tool_name: str,
    risk: ToolRisk | None,
) -> PermissionResult:
    if risk is None:
        return PermissionResult(
            decision=PermissionDecision.DENY,
            reason=f"未知工具不允许执行：{tool_name}",
        )

    if risk is ToolRisk.READ:
        return PermissionResult(
            decision=PermissionDecision.ALLOW,
            reason=f"只读工具可以直接执行：{tool_name}",
        )

    if risk is ToolRisk.WRITE:
        return PermissionResult(
            decision=PermissionDecision.ASK,
            reason=f"修改文件前需要确认：{tool_name}",
        )

    if risk is ToolRisk.DESTRUCTIVE:
        return PermissionResult(
            decision=PermissionDecision.ASK,
            reason=f"删除文件前需要确认：{tool_name}",
        )

    if risk is ToolRisk.EXECUTE:
        return PermissionResult(
            decision=PermissionDecision.ASK,
            reason=f"命令执行前需要确认：{tool_name}",
        )

    return PermissionResult(
        decision=PermissionDecision.DENY,
        reason=f"无法识别风险类型：{tool_name}",
    )


HARD_DENIED_PROGRAMS = frozenset(
    {
        "sudo",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "dd",
    }
)


def _basename(program: str) -> str:
    return Path(program).name.lower()


def _rm_recursively_removes_root(args: list[str]) -> bool:
    recursive = False
    force = False
    targets: list[str] = []

    for arg in args:
        if arg == "--recursive":
            recursive = True
            continue
        if arg == "--force":
            force = True
            continue
        if arg.startswith("-") and not arg.startswith("--"):
            flags = arg[1:]
            if "r" in flags or "R" in flags:
                recursive = True
            if "f" in flags:
                force = True
            continue
        targets.append(arg)

    return recursive and force and "/" in targets


def check_hard_deny(
    tool_name: str,
    arguments: dict[str, object],
) -> PermissionResult | None:
    if tool_name != "run_command":
        return None

    argv = arguments.get("argv")
    if not isinstance(argv, list) or not argv:
        return None

    if not all(isinstance(arg, str) for arg in argv):
        return None

    program = _basename(argv[0])

    if program in HARD_DENIED_PROGRAMS:
        return PermissionResult(
            decision=PermissionDecision.DENY,
            reason=f"命令被硬拒绝：{program}",
        )

    if program == "mkfs" or program.startswith("mkfs."):
        return PermissionResult(
            decision=PermissionDecision.DENY,
            reason=f"命令被硬拒绝：{program}",
        )

    if program == "rm":
        rest = argv[1:]
        if _rm_recursively_removes_root(rest):
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason="命令被硬拒绝：rm -rf /",
            )

    return None
