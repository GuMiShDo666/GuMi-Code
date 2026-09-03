# Tool and Permission Contracts

> Rules for model-visible capabilities, authorization, and workspace safety.

## Registry Is the Integration Boundary

`src/vibe_harness/tools/registry.py` keeps four coordinated tables:

- `ALL_TOOL_SCHEMAS`: model-facing JSON schemas;
- `ALL_TOOL_HANDLERS`: executable Python functions;
- `ALL_TOOL_RISKS`: canonical `READ`, `WRITE`, `DESTRUCTIVE`, or `EXECUTE` risk;
- `PREFLIGHTS`: side-effect-free validation for approval-gated tools.

Schema, handler, and risk keys must stay identical. Add registry consistency
tests with every tool change. Do not treat model visibility as authorization.

## Permission Order

The current execution order is:

```text
dynamic schema visibility
-> JSON parsing
-> structured hard deny
-> risk decision (ALLOW / ASK / DENY)
-> side-effect-free preflight
-> PreToolUse Hook (additional veto only)
-> terminal approval
-> handler validation and execution
-> PostToolUse Hook
-> matching Tool Result
```

References: `agent.py::run_agent`, `tools/registry.py::execute_tool`, and
`permissions/policy.py`.

- Read tools are allowed directly.
- Write, delete, and execute tools require explicit approval.
- Unknown tools and unknown risks fail closed.
- `PreToolUse` can only add a veto after core policy/preflight; it cannot
  authorize a denied call or replace approval.
- Only trimmed case-insensitive `y` or `yes` approves terminal execution;
  empty/other input, EOF, and interruption deny.
- An identical explicitly denied call is not presented repeatedly in one run.

## Dynamic Command Visibility

`run_command` remains registered but is absent from default model schemas.
Only the current invocation's `--allow-run-command` flag exposes it. Agent code
also checks its per-run allow-set, so a forged disabled call is rejected before
approval/execution and still receives a matching Tool Result.

Do not persist command enablement in Session state. A resumed invocation must
opt in again.

## Workspace and Sensitive-File Boundary

Reuse helpers from `tools/common.py`:

- `_resolve_workspace_path` rejects absolute paths, traversal, and resolved
  paths outside the workspace.
- `_reject_symlinks` prevents traversal through symlink components.
- `_is_sensitive_path` hides `.env` variants, credentials, private-key-like
  files, internal transcripts, and persisted tool results.
- `_truncate_output` caps model-visible output.

Safe environment templates such as `.env.example` are intentionally readable;
real `.env` files are not. Search and Git tools must filter sensitive paths in
their output, not rely only on the model to ignore them.

## Mutating Tools

- Run `_validate_*` during preflight and again in the handler.
- Use atomic replacement for file overwrite/edit and preserve an existing
  file's executable mode.
- Do not create missing parent directories unless the schema explicitly opts
  in through `create_parents`.
- Only delete ordinary files; do not delete directories or symlinks.

## Command Execution Boundary

`tools/command.py` implements defense in depth:

- one argv-based program per call, no shell;
- reject shell executables, common shell-launch wrappers, control operators,
  and redirection syntax;
- structured hard deny for directly catastrophic programs/arguments;
- workspace-relative cwd and path arguments;
- 1-300 second timeout and 50,000-character output cap;
- allowlisted environment plus one-shot temporary HOME/XDG directories.

This is not an OS/container sandbox. Interpreters, project scripts, build/test
runners, wrappers, and network-capable programs can still perform arbitrary
effects after opt-in and approval. Never describe the command blacklist or
argument validation as complete isolation. Default execution for untrusted
projects would require an OS/container filesystem and network boundary.

## Anti-Patterns

- Registering a schema without a risk or handler.
- Adding a mutating handler without a preflight.
- Asking for approval before validating impossible/invalid arguments.
- Letting user approval override a hard deny.
- Passing raw command strings to a shell.
- Expanding the blacklist and claiming arbitrary code execution is solved.
- Returning a Tool Call failure without its original `tool_call_id`.
