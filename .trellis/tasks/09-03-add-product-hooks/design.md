# Product Hooks Design

## Decision Summary

Add a small, synchronous, per-instance `HookRegistry` and inject it into
`run_agent()`. Keep the four s04 lifecycle names and callback ordering, while
keeping Permission and tool dispatch as mandatory Harness controls.

The design deliberately does not copy the reference's process-global registry,
`shell=True` command execution, substring-only permission rules, or argument
logging.

## Public Contract

`src/vibe_harness/hooks.py` owns:

- `HookEvent`, a string enum with the four supported event names;
- `HookCallback`, the synchronous callback type;
- `HookRegistry.register_hook(event, callback)`;
- `HookRegistry.trigger_hooks(event, *args)`.

Each registry instance owns its callback lists. `run_agent()` accepts an
optional instance; `None` means no Hooks and retains existing behavior.

Callback arguments are intentionally small:

| Event | Arguments | Control result |
|---|---|---|
| `UserPromptSubmit` | `query` | ignored by Agent; callbacks should return `None` |
| `PreToolUse` | `tool_name`, copied parsed arguments | non-`None` vetoes this call |
| `PostToolUse` | `tool_name`, copied parsed arguments, result | ignored by Agent |
| `Stop` | copied messages | non-empty string is appended as a user continuation |

`trigger_hooks()` stops at the first non-`None` callback result, matching the
reference. Event-specific Agent code decides whether that result affects flow.

## Runtime Placement

```text
current user query
-> UserPromptSubmit
-> append current user message + snapshot
-> context pipeline + model
-> Gate 0 enabled-tool check
-> in-flight snapshot
-> parse arguments
-> hard deny
-> risk decision
-> side-effect-free preflight when required
-> PreToolUse (additional veto only)
-> terminal approval when required
-> handler attempt
-> PostToolUse
-> matching Tool Result + snapshot
-> natural no-tool response
-> Stop
-> final answer OR bounded continuation
```

The tool Hooks run inside `execute_tool()` because that function already owns
the parsed arguments and exact security ordering. Agent control tools
(`todo_write`, `compact`) remain outside the tool registry and do not trigger
tool-use Hooks in this version.

## Security Invariants

- `PreToolUse` runs only after hard deny and preflight. It cannot turn a deny
  into allow and cannot skip approval.
- Mutable arguments and message history are deep-copied before callback entry.
- A post-Hook exception propagates. The existing in-flight snapshot therefore
  remains conservative after a side effect instead of reporting a false Tool
  Result and inviting an unsafe retry.
- Hook result text is treated as untrusted model-visible Tool Result data.
- No Hook output is printed directly; terminal rendering remains event-based.
- `max_steps` wins over `Stop`, including when a Hook repeatedly requests
  continuation.

## Compatibility and Rollback

- `hook_registry=None` is a no-op path.
- New callback parameters on `execute_tool()` are optional.
- No Session schema, CLI argument, tool schema, dependency, or persisted state
  changes are required.
- Rollback is limited to removing the optional registry wiring, Hook module,
  tests, and documentation; persisted Sessions remain compatible.
