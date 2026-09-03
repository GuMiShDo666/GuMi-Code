# Error Handling

> Error contracts across validators, tool dispatch, model orchestration, and CLI.

## Boundary Model

The project uses different failure representations at different boundaries:

| Boundary | Local convention |
|---|---|
| Pure validation | Raise `ValueError` with a concise actionable message |
| Tool dispatcher | Return a model-visible string beginning with `Error:` |
| Expected subprocess failure | Return a bounded generic `Error:` result |
| Agent/context invariant | Raise `RuntimeError` |
| CLI boundary | Catch expected exception classes, print `[\u9519\u8bef]` to stderr, return `1` |

Evidence: `tools/filesystem.py`, `tools/command.py`,
`tools/registry.py::_format_tool_error`, `agent.py`, and `cli.py::main`.

## Validation Before Side Effects

- Keep validation in a `_validate_*` helper when the operation can mutate or
  execute. `write_file`, `edit_file`, `delete_file`, and `run_command` follow
  this pattern.
- Preflight validation must be side-effect free so `execute_tool()` can run it
  before requesting approval.
- The handler must call the same validator again immediately before the side
  effect. Do not assume state stayed unchanged during user approval.
- Validate the full replacement value before assigning state. `TodoManager`
  builds a normalized list first so a failed update preserves the old list.

## Tool Errors

`execute_tool()` is the model-facing error boundary:

- Invalid JSON and non-object arguments produce explicit stable errors.
- Unknown tools and policy denials are returned without invoking a handler.
- `UnicodeDecodeError`, `TypeError`, `ValueError`, and `OSError` from known
  handlers are converted through `_format_tool_error()`.
- Unexpected exception details are not returned to the model; use the generic
  `Error: Tool execution failed` rather than leaking paths, environment data,
  or implementation internals.
- Explicit user denial uses the string-compatible `ToolExecutionDenied` type.
  Agent deduplication must check the type, not imitate its text prefix.

Every assistant Tool Call must receive a matching `role=tool` result with the
same `tool_call_id`, including validation, permission, and disabled-tool
failures. Never abort a tool batch in a way that leaves the protocol open.

## Subprocess Errors

- Use argument lists and `shell=False` behavior; never interpolate user/model
  input into a shell command string.
- Apply a timeout and catch `TimeoutExpired`.
- Return stable messages for missing executables and OS launch errors.
- Do not include inherited environment values or raw exception reprs in errors.
- Docker startup/preflight errors must not return raw daemon stderr, image
  registry details, Docker configuration, or host paths to the model.
- Timeout, output overflow, interruption, and uncertain Docker-client failure
  require best-effort removal of the exact random Harness container name. If
  cleanup cannot be confirmed, return a bounded manual-inspection warning;
  never broaden cleanup by prefix/filter and never fall back to the host.

Reference implementations: `tools/search.py::search_text`,
`tools/git.py::_run_git`, and `tools/command.py::run_command`.

## CLI Errors

`cli.py::main` owns process-level reporting. Expected `RuntimeError`, OpenAI
SDK errors, `OSError`, and `ValueError` are printed once to stderr and return
exit code `1`. Keep configuration and client construction inside that `try`
block so startup failures follow the same contract.

Argument-shape and path errors discovered by `argparse` belong in
`parse_args()` and should use `parser.error()`.

## Avoid

- Broad `except Exception` around normal business logic.
- Returning successful-looking text after validation or execution failed.
- Logging or formatting API keys, tokens, cookies, authorization headers, or
  sensitive file contents.
- Catching an error only to continue with partially mutated state.
- Exposing raw model/tool arguments in ordinary error logs.
