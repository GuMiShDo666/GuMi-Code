"""Register model tools and dispatch them through ordered safety gates."""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from gum_code.permissions import (
    PermissionDecision,
    check_hard_deny,
    decide_permission,
)
from gum_code.permissions.policy import ToolRisk
from gum_code.tools.artifacts import READING_TOOL_NAMES, read_tool_output
from gum_code.tools.command import (
    CommandExecutionResult,
    CommandResultCallback,
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
from gum_code.tools.documents import (
    MAX_DOCX_BATCH, MAX_QUESTION_CHARS,
    analyze_image, read_document, validate_analyze_image, validate_read_document,
)

if TYPE_CHECKING:
    from gum_code.vision import VisionRuntime
    from gum_code.self_check import SelfCheckRuntime

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

BeforeExecuteCallback = Callable[[], None]

DENIED_BY_USER_PREFIX = "Error: Tool execution denied by user: "


class ToolExecutionDenied(str):
    """A string-compatible result that represents an explicit user denial."""

ALL_TOOL_RISKS: dict[str, ToolRisk] = {
    "list_files": ToolRisk.READ,
    "read_file": ToolRisk.READ,
    "read_document": ToolRisk.READ,
    # READ permits the local source; the handler separately enforces visual consent.
    "analyze_image": ToolRisk.READ,
    "find_files": ToolRisk.READ,
    "search_text": ToolRisk.READ,
    "git_status": ToolRisk.READ,
    "git_diff": ToolRisk.READ,
    "read_tool_output": ToolRisk.READ,
    "write_file": ToolRisk.WRITE,
    "edit_file": ToolRisk.WRITE,
    "delete_file": ToolRisk.DESTRUCTIVE,
    "run_command": ToolRisk.EXECUTE,
    "self_check": ToolRisk.EXECUTE,
    "project_service": ToolRisk.EXECUTE,
    "project_browser": ToolRisk.EXECUTE,
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

MODEL_VISIBLE_TOOL_NAMES = (frozenset(ALL_TOOL_RISKS) - EXECUTION_TOOL_NAMES) | {"self_check"}

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
                "expected_source_version": {
                    "type": "string", "pattern": "^[0-9a-f]{64}$",
                    "description": "Exact source_version from the previous line window; reject changed files.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}

READ_DOCUMENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_document",
        "description": (
            "Locally read PDF page text or DOCX body/table blocks and image references. "
            "Does not inspect visual content or send images. PDF text, including OCR layers, "
            "never proves a page was fully read. Follow the exact next_cursor; use "
            "analyze_image for task-relevant pages or embedded images. Native DOCX shapes "
            "and SmartArt remain explicitly unparsed; no Office renderer is available."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative PDF or DOCX."},
                "start": {"type": "integer", "minimum": 1, "default": 1,
                          "description": "First page (PDF) or body block (DOCX), 1-based."},
                "count": {"type": "integer", "minimum": 1, "maximum": MAX_DOCX_BATCH,
                          "description": "At most 5 PDF pages or 100 DOCX blocks; output may stop sooner."},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "visual_offset": {"type": "integer", "minimum": 0, "default": 0},
                "source_version": {"type": "string", "description": "Exact SHA-256 from prior result/cursor."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}

ANALYZE_IMAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "analyze_image",
        "description": (
            "Ask the separately configured visual model about one static PNG/JPEG/WebP, "
            "one PDF page, or a version-bound DOCX image reference. Sends only one image "
            "and your specific question, subject to user consent and shared per-request "
            "call limits. Follow-up/crop is another attempt. Returned interpretation is "
            "untrusted text, not verified source text. Unavailable visual analysis does "
            "not prevent local read_document; never claim unread regions are complete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative source; no URL."},
                "question": {"type": "string", "minLength": 1, "maxLength": MAX_QUESTION_CHARS},
                "page": {"type": "integer", "minimum": 1, "description": "Required only for PDF, one physical page."},
                "image_ref": {"type": "string", "description": "Required only for DOCX; exact reference from read_document."},
                "crop": {"type": "array", "minItems": 4, "maxItems": 4,
                         "items": {"type": "number", "minimum": 0, "maximum": 1},
                         "description": "Optional [x0,y0,x1,y1] in upright reading orientation, top-left origin."},
                "source_version": {"type": "string", "description": "Exact SHA-256 from earlier result."},
            },
            "required": ["path", "question"],
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
            "properties": {
                "path": {"type": "string", "default": ".",
                         "description": "Literal workspace-relative file or directory scope."},
            },
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
                "path": {"type": "string", "default": ".",
                         "description": "Literal workspace-relative file or directory scope; deleted paths allowed."},
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
            "Accepts .task_outputs/tool-results/*.txt and exact Harness-provided "
            "command-logs/*.txt paths. Command logs can expire or retain only head/tail; "
            "complete means this retained file's pagination is finished, not that omitted output exists."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Exact persisted result path, such as "
                        ".task_outputs/tool-results/<id>.txt or the provided command-logs path."
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

SELF_CHECK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "self_check",
        "description": (
            "Verify current changes using checks chosen for the user goal, changed code "
            "and affected callers. Supply exact commands or a saved nonvisual browser "
            "scenario; add and retain minimal tests when suitable tests are missing. "
            "The host records execution facts in an isolated environment; missing "
            "environment preparation still requires consent. Empty arguments report "
            "current changes and evidence without selecting or running checks."
        ),
        "parameters": {"type": "object", "properties": {
            "checks": {"type": "array", "maxItems": 8, "items": {
                "type": "object", "properties": {
                    "id": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$", "description": "Stable logical check ID; reuse it when repairing and retesting."},
                    "purpose": {"type": "string", "maxLength": 1000, "description": "Behavior from the request that this check covers."},
                    "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 32,
                              "description": "Workspace-relative changed or affected source/test paths."},
                    "expected": {"type": "string", "maxLength": 1000, "description": "Expected behavior; this is not a claim that it passed."},
                    "kind": {"type": "string", "enum": ["command", "browser"]},
                    "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                             "description": "Command only: complete program name followed by exact arguments."},
                    "cwd": {"type": "string", "description": "Command only: workspace-relative project directory."},
                    "ecosystem": {"type": "string", "enum": ["python", "node", "prepared"]},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                    "runner": {"type": "string", "enum": ["pytest", "unittest"],
                               "description": "Optional command adapter observing normal test cases at the exact supplied targets."},
                    "service": {"type": "string", "description": "Browser only: current approved project service name."},
                    "test_file": {"type": "string", "description": "Browser only: retained workspace-relative JSON scenario with path and bounded steps; include objective assertions, no visual checks."},
                    "build_check": {"type": "string", "description": "Browser only: build check ID supplying current build evidence."},
                    "artifact_paths": {"type": "array", "items": {"type": "string"}, "maxItems": 16,
                                       "description": "Workspace-relative build outputs whose content must match current build evidence."},
                }, "required": ["id", "purpose", "paths", "expected", "kind"],
                "additionalProperties": False,
            }},
            "exclusions": {"type": "array", "maxItems": 256, "items": {
                "type": "object", "properties": {
                    "path": {"type": "string"},
                    "reason": {"type": "string", "enum": ["visual"]},
                }, "required": ["path", "reason"], "additionalProperties": False,
            }, "description": "Propose purely visual scope to exclude; independent review must confirm it."},
        }, "additionalProperties": False},
    },
}


def _validate_self_check(workspace: Path, *, runtime=None, **arguments) -> dict:
    if runtime is None or not callable(getattr(runtime, "execute", None)):
        raise ValueError("自检运行状态不可用")
    if Path(runtime.workspace).resolve() != workspace.resolve():
        raise ValueError("自检工作区不匹配")
    from gum_code.self_check import validate_self_check_arguments
    checked = validate_self_check_arguments(workspace, arguments)
    if any(check.get("kind") == "command" and check_hard_deny("run_command", check)
           for check in checked.get("checks", ())):
        raise ValueError("自检命令被策略拒绝")
    return checked


def _run_self_check(workspace: Path, *, runtime, command_runner, browser_runner,
                    cancel_event=None, **arguments) -> str:
    checked = _validate_self_check(workspace, runtime=runtime, **arguments)
    return runtime.execute(**checked, command_runner=command_runner,
                           browser_runner=browser_runner, cancel_event=cancel_event)


ALL_TOOL_SCHEMAS: dict[str, dict] = {
    "list_files": LIST_FILES_SCHEMA,
    "read_file": READ_FILE_SCHEMA,
    "read_document": READ_DOCUMENT_SCHEMA,
    "analyze_image": ANALYZE_IMAGE_SCHEMA,
    "find_files": FIND_FILES_SCHEMA,
    "search_text": SEARCH_TEXT_SCHEMA,
    "git_status": GIT_STATUS_SCHEMA,
    "git_diff": GIT_DIFF_SCHEMA,
    "read_tool_output": READ_TOOL_OUTPUT_SCHEMA,
    "write_file": WRITE_FILE_SCHEMA,
    "edit_file": EDIT_FILE_SCHEMA,
    "delete_file": DELETE_FILE_SCHEMA,
    "run_command": RUN_COMMAND_SCHEMA,
    "self_check": SELF_CHECK_SCHEMA,
}

for _reading_name in READING_TOOL_NAMES:
    ALL_TOOL_SCHEMAS[_reading_name]["function"]["description"] += (
        " Returns <reading-output>: follow exact next via read_tool_output when needed. "
        "complete is retained-result EOF; scope_complete describes capture, not full-file review. "
        "Limits or unavailable storage are explicit gaps."
    )
READ_FILE_SCHEMA["function"]["description"] += (
    " file_has_more/next_start_line describe unread later lines; carry source_version "
    "as expected_source_version for the next window after finishing this page sequence."
)
for _reading_schema in (FIND_FILES_SCHEMA, SEARCH_TEXT_SCHEMA):
    _reading_schema["function"]["parameters"]["properties"]["max_results"]["description"] = (
        "Maximum first-page items; later retained output is available through exact next."
    )
READ_TOOL_OUTPUT_SCHEMA["function"]["description"] += (
    " Native reading snapshots return <reading-output> with original scope/version and gaps. "
    "A new query is not a continuation of an old snapshot."
)

ALL_TOOL_HANDLERS: dict[str, Callable[..., str]] = {
    "list_files": list_files,
    "read_file": read_file,
    "read_document": read_document,
    "analyze_image": analyze_image,
    "find_files": find_files,
    "search_text": search_text,
    "git_status": git_status,
    "git_diff": git_diff,
    "read_tool_output": read_tool_output,
    "write_file": write_file,
    "edit_file": edit_file,
    "delete_file": delete_file,
    "run_command": run_command,
    "self_check": _run_self_check,
}

PREFLIGHTS: dict[str, Callable[..., None]] = {
    "read_document": validate_read_document,
    "analyze_image": validate_analyze_image,
    "write_file": _validate_write_file,
    "edit_file": _validate_edit_file,
    "delete_file": _validate_delete_file,
    "run_command": _validate_run_command,
    "self_check": _validate_self_check,
}

def _validate_project_tool(workspace: Path, *, runtime=None, browser=False, **arguments) -> None:
    if runtime is None or Path(runtime.workspace).resolve() != Path(workspace).resolve():
        raise ValueError("Project runtime unavailable")
    if browser:
        runtime.browser.validate(arguments)
    else:
        runtime.validate(arguments)


def _project_service(workspace: Path, *, runtime, cancel_event=None, **arguments) -> str:
    return runtime.execute(arguments, approved=True, cancel_event=cancel_event)


def _project_browser(workspace: Path, *, runtime, cancel_event=None, **arguments) -> str:
    return runtime._redact(runtime.browser.execute(arguments, cancel_event=cancel_event))


ALL_TOOL_SCHEMAS.update({
    "project_service": {
        "type": "function", "function": {
            "name": "project_service",
            "description": "Manage this session's Docker project services. Start apps or an isolated PostgreSQL test database; observe readiness/logs, restart unchanged approved services or stop them. No host execution. Services stop at exit; database data remains. External network needs separate consent.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["start", "status", "logs", "restart", "stop"]},
                "name": {"type": "string", "maxLength": 32},
                "kind": {"type": "string", "enum": ["app", "postgres"]},
                "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "cwd": {"type": "string"},
                "port": {"type": "integer", "minimum": 1024, "maximum": 65535},
                "ecosystem": {"type": "string", "enum": ["python", "node", "prepared"]},
                "online": {"type": "boolean"},
                "database": {"type": "string"},
            }, "required": ["action"], "additionalProperties": False},
        },
    },
    "project_browser": {
        "type": "function", "function": {
            "name": "project_browser",
            "description": "Observe and interact with a currently running project_service webpage in an isolated browser. Open by service name and relative path; read controls before click/fill. No arbitrary URLs or scripts. Results are observations, not a test-pass certificate. Missing environment requires preparation consent.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["open", "read", "click", "fill", "assert"]},
                "service": {"type": "string", "maxLength": 32},
                "path": {"type": "string", "maxLength": 2000},
                "selector": {"type": "string", "maxLength": 500},
                "value": {"type": "string", "maxLength": 4000},
                "assertion": {"type": "string", "enum": ["count", "text", "value", "url", "resource_status", "no_errors"]},
                "expected": {"type": ["string", "integer", "boolean"]},
            }, "required": ["action"], "additionalProperties": False},
        },
    },
})
ALL_TOOL_HANDLERS.update(project_service=_project_service, project_browser=_project_browser)
PREFLIGHTS.update(project_service=_validate_project_tool, project_browser=_validate_project_tool)


def get_tool_schemas(
    *,
    allow_run_command: bool = False,
    allow_project_runtime: bool = False,
) -> list[dict]:
    visible_names = set(MODEL_VISIBLE_TOOL_NAMES)

    if allow_run_command:
        visible_names.add("run_command")
    if allow_project_runtime:
        visible_names.update({"project_service", "project_browser"})

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
    before_execute_callback: BeforeExecuteCallback | None = None,
    command_result_callback: CommandResultCallback | None = None,
    vision_runtime: "VisionRuntime | None" = None,
    cancel_event: Event | None = None,
    self_check_runtime: "SelfCheckRuntime | None" = None,
    file_change_callback: Callable | None = None,
    artifact_workspace: Path | None = None,
    project_runtime=None,
) -> str:
    """Dispatch one tool through policy, preflight, hook, and approval gates.

    The execution callback fires after all gates and before handler dispatch.
    """
    handler = ALL_TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Error: Unknown tool: {tool_name}"

    try:
        arguments = json.loads(arguments_json)
    except json.JSONDecodeError:
        return "Error: Invalid tool arguments: not valid JSON"

    if not isinstance(arguments, dict):
        return "Error: Invalid tool arguments: expected a JSON object"

    if tool_name in {"project_service", "project_browser"} and arguments.keys() - (
        ALL_TOOL_SCHEMAS[tool_name]["function"]["parameters"]["properties"].keys()
    ):
        return "Error: Invalid tool arguments"

    if tool_name in READING_TOOL_NAMES | {"read_tool_output"} and arguments.keys() - (
        ALL_TOOL_SCHEMAS[tool_name]["function"]["parameters"]["properties"].keys()
    ):
        return "Error: Invalid tool arguments"

    if tool_name in {"write_file", "edit_file", "delete_file"} and arguments.keys() - (
        ALL_TOOL_SCHEMAS[tool_name]["function"]["parameters"]["properties"].keys()
    ):
        return "Error: Invalid tool arguments"

    if tool_name in {"read_document", "analyze_image"} and arguments.keys() - (
        ALL_TOOL_SCHEMAS[tool_name]["function"]["parameters"]["properties"].keys()
    ):
        return "Error: Invalid tool arguments"

    if tool_name == "run_command" and arguments.keys() - {
        "argv", "cwd", "timeout_seconds"
    }:
        return "Error: Invalid tool arguments"

    # Hard denials run before approval and cannot be overridden by it.
    if check_hard_deny(tool_name, arguments) is not None:
        return f"Error: Tool denied by policy: {tool_name}"

    risk = ALL_TOOL_RISKS.get(tool_name)

    permission = decide_permission(tool_name, risk)

    if permission.decision is PermissionDecision.DENY:
        return f"Error: Tool denied by policy: {tool_name}"

    preflight = PREFLIGHTS.get(tool_name)
    if preflight is not None:
        try:
            if tool_name in {"project_service", "project_browser"}:
                preflight(workspace=workspace, runtime=project_runtime,
                          browser=tool_name == "project_browser", **arguments)
            elif tool_name == "self_check":
                arguments = preflight(workspace=workspace, runtime=self_check_runtime,
                                      **arguments)
            elif tool_name == "run_command":
                preflight(
                    workspace=workspace,
                    **arguments,
                    sandbox=command_sandbox,
                )
            else:
                preflight(workspace=workspace, **arguments)
        except (UnicodeDecodeError, TypeError, ValueError, OSError) as exc:
            if tool_name in {"read_document", "analyze_image"}:
                return "Error: Invalid document arguments or unavailable source"
            return _format_tool_error(exc)

    if pre_tool_use_callback is not None:
        blocked = pre_tool_use_callback(tool_name, copy.deepcopy(arguments))
        if blocked is not None:
            detail = f": {blocked}" if blocked else ""
            return f"Error: Tool blocked by PreToolUse hook{detail}"

    project_observation = tool_name == "project_service" and arguments.get("action") in {"status", "logs"}
    project_restart = (tool_name == "project_service" and project_runtime is not None
                       and project_runtime.approved_restart(arguments))
    if permission.decision is PermissionDecision.ASK and not (project_observation or project_restart):
        if approval_callback is None:
            return f"Error: Approval required: {tool_name}"

        approval_arguments = copy.deepcopy(arguments)
        if tool_name == "project_browser" and "value" in approval_arguments:
            approval_arguments["value"] = "<REDACTED>"
        approved = approval_callback(
            tool_name,
            approval_arguments,
            permission.reason,
        )

        if not approved:
            return ToolExecutionDenied(f"{DENIED_BY_USER_PREFIX}{tool_name}")

    # Announce execution only after every gate has passed.
    if before_execute_callback is not None:
        before_execute_callback()

    if tool_name == "self_check":
        def run_check(check_arguments, sandbox):
            outcomes: list[CommandExecutionResult] = []
            # The runtime supplies the current change scope and leased offline sandbox.
            # Exact-call approval cannot grant ordinary run_command broader permission.
            checked = copy.deepcopy(check_arguments)
            output = execute_tool(
                workspace, "run_command", json.dumps(checked),
                approval_callback=lambda name, values, reason: name == "run_command" and values == checked,
                pre_tool_use_callback=pre_tool_use_callback,
                post_tool_use_callback=post_tool_use_callback,
                command_sandbox=sandbox,
                command_result_callback=outcomes.append,
                cancel_event=cancel_event,
            )
            return output, outcomes[-1] if outcomes else None

        def run_browser_check(check, input_version):
            if project_runtime is None:
                return "Error: Project runtime unavailable", {
                    "started": False, "reason": "runtime_unavailable",
                }
            def service_snapshot(target, version):
                project_runtime.internal_paths = tuple(self_check_runtime._internal_paths)
                paths = target.get("artifact_paths", []) if target.get("build_check") else target["paths"]
                return project_runtime.browser_evidence(target["service"], version, paths)

            self_check_runtime.browser_evidence_callback = service_snapshot
            service_evidence = service_snapshot(check, input_version)

            def run_step(step):
                current = service_snapshot(check, input_version)
                if (not current.get("current") or current.get("service_identity")
                        != service_evidence.get("service_identity")
                        or current.get("source_version") != service_evidence.get("source_version")):
                    return "Error: Browser service changed during verification"
                checked = copy.deepcopy(step)
                approval_values = copy.deepcopy(checked)
                if "value" in approval_values:
                    approval_values["value"] = "<REDACTED>"
                return execute_tool(
                    workspace, "project_browser", json.dumps(checked),
                    approval_callback=lambda name, values, reason: (
                        name == "project_browser" and values == approval_values
                    ),
                    pre_tool_use_callback=pre_tool_use_callback,
                    post_tool_use_callback=post_tool_use_callback,
                    cancel_event=cancel_event, project_runtime=project_runtime,
                )

            output, facts = project_runtime.browser.execute_check(
                workspace, check, input_version, action_runner=run_step,
                service_evidence=service_evidence, cancel_event=cancel_event,
            )
            current = service_snapshot(check, input_version)
            if (service_evidence.get("current") is True
                    and (not current.get("current") or current.get("service_identity")
                         != service_evidence.get("service_identity")
                         or current.get("source_version") != service_evidence.get("source_version"))):
                facts = {**facts, "current": False, "reason": "service_changed"}
                output = json.dumps({"status": "failed", "reason": "service_changed"})
            return output, facts

        # Hook/callback exceptions inside a real check must escape, not become success text.
        result = handler(workspace=workspace, runtime=self_check_runtime,
                         command_runner=run_check, browser_runner=run_browser_check,
                         cancel_event=cancel_event, **arguments)
        if post_tool_use_callback is not None:
            post_tool_use_callback(tool_name, copy.deepcopy(arguments), result)
        return result

    command_results: list[CommandExecutionResult] = []
    try:
        if tool_name in {"project_service", "project_browser"}:
            if (self_check_runtime is not None
                    and Path(self_check_runtime.workspace).resolve() == workspace.resolve()):
                project_runtime.internal_paths = tuple(self_check_runtime._internal_paths)
            result = handler(workspace=workspace, runtime=project_runtime,
                             cancel_event=cancel_event, **arguments)
        elif tool_name == "run_command":
            command_kwargs: dict[str, object] = {}
            if command_result_callback is not None:
                command_kwargs["result_callback"] = command_results.append
            if cancel_event is not None:
                command_kwargs["cancel_event"] = cancel_event
            result = handler(
                workspace=workspace,
                **arguments,
                sandbox=command_sandbox,
                **command_kwargs,
            )
        elif tool_name in {"read_document", "analyze_image"}:
            document_kwargs: dict[str, object] = {}
            if tool_name == "analyze_image":
                document_kwargs["vision_runtime"] = vision_runtime
            if cancel_event is not None:
                document_kwargs["cancel_event"] = cancel_event
            result = handler(workspace=workspace, **arguments, **document_kwargs)
        elif tool_name in {"write_file", "edit_file", "delete_file"} and file_change_callback is not None:
            result = handler(workspace=workspace, **arguments, file_change_callback=file_change_callback)
        elif tool_name in READING_TOOL_NAMES | {"read_tool_output"} and artifact_workspace is not None:
            result = handler(workspace=workspace, **arguments, artifact_workspace=artifact_workspace)
        else:
            result = handler(workspace=workspace, **arguments)
    except (UnicodeDecodeError, TypeError, ValueError, OSError) as exc:
        result = ("Error: Document processing failed"
                  if tool_name in {"read_document", "analyze_image"}
                  else _format_tool_error(exc))

    if post_tool_use_callback is not None:
        post_tool_use_callback(
            tool_name,
            copy.deepcopy(arguments),
            result,
        )

    # A completed process is not accepted evidence until the post-Hook succeeds.
    # Keep the external callback outside handler error conversion as well.
    if tool_name == "run_command" and command_result_callback is not None:
        command_result_callback(
            command_results[-1] if command_results
            else CommandExecutionResult(None, None, "unknown")
        )

    return result
