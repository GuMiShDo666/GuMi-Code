# Code Reuse Thinking Guide

> Search for the existing contract owner before adding code.

## Why This Matters Here

Vibe Harness is small, but one behavior often appears at several boundaries:
JSON schema, Python validation, permission policy, Agent messages, Session
recovery, terminal events, and tests. Duplicating a value or rule makes those
boundaries drift.

## Search Before Writing

Use `rg` to find symbols, error text, event names, schema fields, and limits:

```bash
rg -n "MAX_TODO_CONTENT_CHARS|tool_call_id|context_compacted" src tests
```

Before creating a helper, inspect these current owners:

| Concern | Existing owner |
|---|---|
| Workspace paths and sensitive names | `tools/common.py` |
| Tool schemas, handlers, risks, preflights | `tools/registry.py` |
| Permission decisions | `permissions/policy.py` |
| Context limits and persistence | `context.py` |
| Session format and recovery | `session.py` |
| Terminal rendering | `events.py` and `permissions/terminal.py` |
| Todo limits and normalization | `todo.py` |

## Intentional Shared Contracts

- Use `MAX_TODO_CONTENT_CHARS` in both the `todo_write` schema and
  `TodoManager.update()`; do not duplicate the literal.
- Derive visible/risk tool sets from `ALL_TOOL_RISKS` instead of maintaining
  unrelated name lists.
- Reuse `_resolve_workspace_path`, `_reject_symlinks`, `_is_sensitive_path`,
  and `_truncate_output` in workspace-facing tools.
- Reuse `_validate_*` in both preflight and the real handler.
- Use `IN_FLIGHT_MARKER_KEY` from `session.py` in Agent snapshots and recovery.

## When to Extract

Extract or reuse when:

- the same safety rule must hold in two or more tools;
- schema and runtime enforce the same numeric limit;
- multiple consumers decode the same message/artifact format;
- a fix must apply consistently before and after approval.

Do not extract when a helper has one simple caller and no shared contract. A
new abstraction should make ownership clearer, not merely shorten one function.

## Anti-Patterns

- Copying sensitive-path lists into a new tool.
- Adding a second risk table or a separate list of all tools.
- Hard-coding a schema limit that already has a runtime constant.
- Reimplementing atomic write without preserving mode and cleanup behavior.
- Parsing OpenAI Tool Calls differently in Agent and Session code without a
  documented reason and round-trip tests.

## Review Prompt

After a cross-file change, search for the old symbol/value and ask:

1. Did every consumer move to the canonical owner?
2. Are schema and runtime behavior still identical?
3. Did reuse accidentally broaden a security boundary?
4. Do tests prove both the shared behavior and safe near-neighbor behavior?
