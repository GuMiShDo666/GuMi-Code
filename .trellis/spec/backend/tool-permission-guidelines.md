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

Command enablement requires both `--allow-run-command` and a user-selected
local `--sandbox-image`. Do not persist either value in Session state. A
resumed invocation must opt in and select the trusted image again.

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

- every approved command runs in a fresh Docker container; no host fallback;
- user-selected local image resolved to an image ID and `--pull=never`;
- no network, read-only root, dropped capabilities, no-new-privileges, numeric
  non-root user, tmpfs, and fixed CPU/memory/PID/nofile limits;
- one read-write workspace mount, with current sensitive paths overlaid by
  empty read-only masks and no host HOME, Docker socket, devices, or extra
  user-controlled mounts; the mask count is capped and overflow fails closed;
- one argv-based program per call, no shell;
- reject shell executables, common shell-launch wrappers, control operators,
  and redirection syntax;
- structured hard deny for directly catastrophic programs/arguments;
- workspace-relative cwd and path arguments;
- 1-300 second timeout, bounded streaming capture, and exact-name cleanup on
  timeout, output overflow, interruption, or uncertain Docker failure;
- fixed container HOME/XDG paths under its bounded `/tmp`; host environment is
  not forwarded into the container.

Docker is a containment boundary, not an absolute security guarantee. An
approved command can modify every ordinary unmasked workspace file. Sensitive
masking recognizes the canonical name/suffix policy, not secrets stored under
arbitrary source filenames. The trusted image, Docker daemon, VM/runtime, and
kernel remain outside the Harness guarantee. Never describe argv filtering or
Docker itself as complete isolation.

## Anti-Patterns

- Registering a schema without a risk or handler.
- Adding a mutating handler without a preflight.
- Asking for approval before validating impossible/invalid arguments.
- Letting user approval override a hard deny.
- Passing raw command strings to a shell.
- Expanding the blacklist and claiming arbitrary code execution is solved.
- Letting the model choose the image, Docker flags, mounts, network, user,
  environment, or resource limits.
- Mounting the Docker socket or host HOME into the command container.
- Pulling/building an image implicitly or falling back to host execution.
- Returning a Tool Call failure without its original `tool_call_id`.
