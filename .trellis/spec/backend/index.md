# Vibe Harness Development Guidelines

> Source-backed rules for the project's Python CLI Agent Harness.

## Scope

This is a single-package Python 3.11 CLI. The important runtime boundaries are
CLI wiring, the Agent Loop, tool/permission execution, context management,
Session recovery, and terminal events. There is no database or web layer.

## Guidelines Index

| Guide | Use it when |
|---|---|
| [Project Structure](./directory-structure.md) | Deciding module ownership or adding a capability |
| [Tool and Permission Contracts](./tool-permission-guidelines.md) | Changing schemas, handlers, risks, paths, or command execution |
| [Hook Lifecycle Contracts](./hooks-guidelines.md) | Registering callbacks or changing prompt/tool/stop lifecycle points |
| [Context and Session Contracts](./context-session-guidelines.md) | Changing messages, compaction, artifacts, snapshots, or resume behavior |
| [Error Handling](./error-handling.md) | Adding validation, subprocess behavior, tool errors, or CLI failures |
| [Terminal Events and Logging](./logging-guidelines.md) | Changing progress/error/approval output or event payloads |
| [Quality and Testing](./quality-guidelines.md) | Implementing, reviewing, and validating any source change |

## Pre-Development Checklist

- Read the task PRD/design/implementation plan when present.
- Read the guides covering every affected runtime boundary.
- Search current source and tests before introducing a helper or constant.
- Identify schema, runtime validation, permission, message-protocol, event, and
  persistence consumers of the changed contract.
- Keep `.env`, credentials, real user HOME configuration, and mutable E2E work
  outside inspection and test output.

## Quality Check

- Run focused tests for changed behavior and then the full pytest suite.
- Verify failure/denial paths and absence of unintended side effects.
- Confirm every assistant Tool Call is closed by a matching Tool Result.
- Confirm Session/context changes preserve protocol ordering and recovery.
- Confirm docs describe current source and do not overstate security.
- Confirm no generic template text or non-applicable database/web guidance was
  introduced.
