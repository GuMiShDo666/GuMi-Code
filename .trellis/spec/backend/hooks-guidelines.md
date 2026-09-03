# Hook Lifecycle Contracts

> Rules for extending the Agent lifecycle without weakening Harness controls.

## Supported Events

`hooks.py::HookRegistry` owns four synchronous, per-instance event lists:

| Event | Callback arguments | Control behavior |
|---|---|---|
| `UserPromptSubmit` | current query | observer; Agent ignores the result |
| `PreToolUse` | tool name, copied parsed arguments | non-`None` vetoes the call |
| `PostToolUse` | tool name, copied arguments, final result | observer; Agent ignores the result |
| `Stop` | copied messages | non-empty string requests continuation |

Callbacks run in registration order. The first non-`None` result ends that
event's callback chain. Registry instances must remain isolated; do not add
process-global callbacks that leak across runs or tests.

## Permission Boundary

Hooks extend policy but do not own authorization. Registry tools keep this
mandatory order:

```text
Gate 0 enabled-tool check
-> argument parsing
-> hard deny
-> risk decision
-> side-effect-free preflight when required
-> PreToolUse additional veto
-> terminal approval when required
-> handler attempt
-> PostToolUse
-> matching Tool Result
```

Never let a Hook convert deny to allow, skip terminal approval, change parsed
arguments used by the handler, or override workspace/sensitive-path checks.
Agent control tools such as `todo_write` and `compact` stay outside registry
tool Hooks unless a future task explicitly redesigns that boundary.

## Message and Session Safety

- A `PreToolUse` veto still produces the original matching `tool_call_id`.
- `Stop` continuation is appended as a user message and snapshotted before the
  next model request.
- `max_steps` remains authoritative over repeated Stop continuations.
- Pass defensive copies of messages and parsed arguments into callbacks.
- Let a Hook exception propagate. If a post-Hook fails after a side effect,
  the existing in-flight Session marker must remain conservative rather than
  recording a false completed Tool Result.

## Logging and Trust

Hooks are trusted in-process Python code, not a sandbox or plugin isolation
boundary. Do not load arbitrary external commands in the base registry. Hook
implementations must not print or return credentials, authorization values,
unbounded outputs, full histories, or sensitive file contents.

Keep normal user-facing output in `events.py`; do not add direct printing to
the Hook framework.

## Required Tests

- registration order, short circuit, invalid events, and instance isolation;
- pre-Hook veto without approval or handler side effects;
- hard deny and preflight before pre-Hook;
- approval still required after a permissive pre-Hook;
- post-Hook only after a handler attempt;
- exact OpenAI Tool Result IDs for vetoes;
- Stop continuation plus max-step enforcement;
- unchanged behavior with no Hook registry.
