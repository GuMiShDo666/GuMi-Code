import copy
import json
from collections.abc import Callable
from pathlib import Path

from gum_code.permissions import (
    PermissionDecision,
    check_hard_deny,
    decide_permission,
)
from gum_code.permissions.policy import ToolRisk
from gum_code.tools.artifacts import read_tool_output
from gum_code.tools.command import (
    DockerSandboxConfig,
    _validate_run_command,
    run_command,
)
from gum_code.tools.filesystem import (
    _validate_delete_file,
    _validate_edit_file,
    _validate_write_file,
    delete_file,
    edit_file,
    list_files,
    read_file,
    write_file,
)
from gum_code.tools.git import git_diff, git_status
from gum_code.tools.search import find_files, search_text

ApprovalCallback = Callable[
    [str, dict[str, object], str],
    bool,
]

PreToolUseCallback = Callable[
    [str, dict[str, object]],
    str | None,
]

PostToolUseCallback = Callable[
    [str, dict[str, object], str],
    object,
]

DENIED_BY_USER_PREFIX = "Error: Tool execution denied by user: "


class ToolExecutionDenied(str):
    """A string-compatible result that represents an explicit user denial."""

ALL_TOOL_RISKS: dict[str, ToolRisk] = {
    "list_files": ToolRisk.READ,
    "read_file": ToolRisk.READ,
    "find_files": ToolRisk.READ,
    "search_text": ToolRisk.READ,
    "git_status": ToolRisk.READ,
    "git_diff": ToolRisk.READ,
    "read_tool_output": ToolRisk.READ,
    "write_file": ToolRisk.WRITE,
    "edit_file": ToolRisk.WRITE,
    "delete_file": ToolRisk.DESTRUCTIVE,
    "run_command": ToolRisk.EXECUTE,
}

READ_ONLY_TOOL_NAMES = frozenset(
    name
    for name, risk in ALL_TOOL_RISKS.items()
    if risk is ToolRisk.READ
)

MUTATING_TOOL_NAMES = frozenset(
    name
    for name, risk in ALL_TOOL_RISKS.items()
    if risk is ToolRisk.WRITE
)

DESTRUCTIVE_TOOL_NAMES = frozenset(
    name
    for name, risk in ALL_TOOL_RISKS.items()
    if risk is ToolRisk.DESTRUCTIVE
)

EXECUTION_TOOL_NAMES = frozenset(
    name
    for name, risk in ALL_TOOL_RISKS.items()
    if risk is ToolRisk.EXECUTE
)

MODEL_VISIBLE_TOOL_NAMES = frozenset(ALL_TOOL_RISKS) - EXECUTION_TOOL_NAMES

LIST_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": (
            "List files and directories inside the current workspace. "
            "The path must be relative to the workspace root."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative directory path, such as '.' or 'src'.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}

READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a UTF-8 text file inside the current workspace. "
            "Returns numbered lines."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to read, starting from 1.",
                    "minimum": 1,
                    "default": 1,
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum number of lines to return.",
                    "minimum": 1,
                    "maximum": 400,
                    "default": 200,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}

FIND_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_files",
        "description": (
            "Find files inside the workspace matching a glob pattern. "
            "Returns relative file paths."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, such as '**/*.py'.",
                },
                "path": {
                    "type": "string",
                    "description": "Relative directory to start searching from.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 200,
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
}

SEARCH_TEXT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_text",
        "description": (
            "Search for text inside workspace files using ripgrep. "
            "Returns matches with file path and line number."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Relative directory to search in.",
                },
                "file_glob": {
                    "type": ["string", "null"],
                    "description": "Only search files matching this glob, such as '*.py'.",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Whether matching is case-sensitive.",
                },
                "use_regex": {
                    "type": "boolean",
                    "description": "Treat the query as a regular expression.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 100,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

GIT_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git_status",
        "description": "Show the git working tree status.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

GIT_DIFF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git_diff",
        "description": "Show unstaged or staged changes in the git working tree.",
        "parameters": {
            "type": "object",
            "properties": {
                "staged": {
                    "type": "boolean",
                    "description": "Show staged (cached) changes instead of unstaged.",
                },
            },
            "additionalProperties": False,
        },
    },
}

WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Create or fully overwrite a file inside the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path.",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write.",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Allow overwriting an existing file.",
                },
                "create_parents": {
                    "type": "boolean",
                    "description": "Create missing parent directories.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
}

EDIT_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": "Replace an exact piece of text inside a workspace file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path.",
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to replace.",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences when true.",
                },
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
    },
}

DELETE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delete_file",
        "description": "Delete a single file inside the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}

READ_TOOL_OUTPUT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_tool_output",
        "description": (
            "Read a persisted tool result whose exact path was provided by the Harness. "
            "Only .task_outputs/tool-results/*.txt is accepted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Exact persisted result path, such as "
                        ".task_outputs/tool-results/<id>.txt."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": "Character offset to start reading from.",
                    "minimum": 0,
                    "default": 0,
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum number of characters to return.",
                    "minimum": 1,
                    "maximum": 20000,
                    "default": 20000,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}

RUN_COMMAND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "Run a single program in the approved workspace through the "
            "configured no-network Docker sandbox. Available programs and "
            "dependencies come from its fixed image. Runs one program per "
            "call; shell operators, redirection, and pipes are not supported."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command and arguments as a list.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Relative working directory.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Timeout in seconds, from 1 to 300.",
                    "minimum": 1,
                    "maximum": 300,
                    "default": 120,
                },
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
    },
}

ALL_TOOL_SCHEMAS: dict[str, dict] = {
    "list_files": LIST_FILES_SCHEMA,
    "read_file": READ_FILE_SCHEMA,
    "find_files": FIND_FILES_SCHEMA,
    "search_text": SEARCH_TEXT_SCHEMA,
    "git_status": GIT_STATUS_SCHEMA,
    "git_diff": GIT_DIFF_SCHEMA,
    "read_tool_output": READ_TOOL_OUTPUT_SCHEMA,
    "write_file": WRITE_FILE_SCHEMA,
    "edit_file": EDIT_FILE_SCHEMA,
    "delete_file": DELETE_FILE_SCHEMA,
    "run_command": RUN_COMMAND_SCHEMA,
}

ALL_TOOL_HANDLERS: dict[str, Callable[..., str]] = {
    "list_files": list_files,
    "read_file": read_file,
    "find_files": find_files,
    "search_text": search_text,
    "git_status": git_status,
    "git_diff": git_diff,
    "read_tool_output": read_tool_output,
    "write_file": write_file,
    "edit_file": edit_file,
    "delete_file": delete_file,
    "run_command": run_command,
}

PREFLIGHTS: dict[str, Callable[..., None]] = {
    "write_file": _validate_write_file,
    "edit_file": _validate_edit_file,
    "delete_file": _validate_delete_file,
    "run_command": _validate_run_command,
}

def get_tool_schemas(
    *,
    allow_run_command: bool = False,
) -> list[dict]:
    visible_names = set(MODEL_VISIBLE_TOOL_NAMES)

    if allow_run_command:
        visible_names.add("run_command")

    return [
        schema
        for name, schema in ALL_TOOL_SCHEMAS.items()
        if name in visible_names
    ]


def get_read_only_tool_schemas() -> list[dict]:
    return [
        schema
        for name, schema in ALL_TOOL_SCHEMAS.items()
        if name in READ_ONLY_TOOL_NAMES
    ]


TOOL_SCHEMAS = get_tool_schemas()


def _format_tool_error(exc: Exception) -> str:
    if isinstance(exc, UnicodeDecodeError):
        return "Error: 文件不是有效 UTF-8 文本"
    if isinstance(exc, TypeError):
        return "Error: Invalid tool arguments"
    if isinstance(exc, ValueError):
        return f"Error: {exc}"
    return "Error: Tool execution failed"


def execute_tool(
    workspace: Path,
    tool_name: str,
    arguments_json: str,
    approval_callback: ApprovalCallback | None = None,
    pre_tool_use_callback: PreToolUseCallback | None = None,
    post_tool_use_callback: PostToolUseCallback | None = None,
    command_sandbox: DockerSandboxConfig | None = None,
) -> str:
    handler = ALL_TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Error: Unknown tool: {tool_name}"

    try:
        arguments = json.loads(arguments_json)
    except json.JSONDecodeError:
        return "Error: Invalid tool arguments: not valid JSON"

    if not isinstance(arguments, dict):
        return "Error: Invalid tool arguments: expected a JSON object"

    if check_hard_deny(tool_name, arguments) is not None:
        return f"Error: Tool denied by policy: {tool_name}"

    risk = ALL_TOOL_RISKS.get(tool_name)

    permission = decide_permission(tool_name, risk)

    if permission.decision is PermissionDecision.DENY:
        return f"Error: Tool denied by policy: {tool_name}"

    preflight = PREFLIGHTS.get(tool_name)
    if preflight is not None:
        try:
            if tool_name == "run_command":
                preflight(
                    workspace=workspace,
                    **arguments,
                    sandbox=command_sandbox,
                )
            else:
                preflight(workspace=workspace, **arguments)
        except (UnicodeDecodeError, TypeError, ValueError, OSError) as exc:
            return _format_tool_error(exc)

    if pre_tool_use_callback is not None:
        blocked = pre_tool_use_callback(tool_name, copy.deepcopy(arguments))
        if blocked is not None:
            detail = f": {blocked}" if blocked else ""
            return f"Error: Tool blocked by PreToolUse hook{detail}"

    if permission.decision is PermissionDecision.ASK:
        if approval_callback is None:
            return f"Error: Approval required: {tool_name}"

        approved = approval_callback(
            tool_name,
            arguments,
            permission.reason,
        )

        if not approved:
            return ToolExecutionDenied(f"{DENIED_BY_USER_PREFIX}{tool_name}")

    try:
        if tool_name == "run_command":
            result = handler(
                workspace=workspace,
                **arguments,
                sandbox=command_sandbox,
            )
        else:
            result = handler(workspace=workspace, **arguments)
    except (UnicodeDecodeError, TypeError, ValueError, OSError) as exc:
        result = _format_tool_error(exc)

    if post_tool_use_callback is not None:
        post_tool_use_callback(
            tool_name,
            copy.deepcopy(arguments),
            result,
        )

    return result
