# Journal - GuMiShDo666 (Part 1)

> AI development session journal
> Started: 2026-09-03

---


## Session 1: DeepSeek V4 tokenizer context budget
<!-- trellis-session: v=2 fp=c84492537bf76f59 -->

**Date**: 2026-09-03
**Task**: DeepSeek V4 tokenizer context budget
**Branch**: `feat/tokenizer-context-budget`

### Summary

Replaced character estimates with pinned official DeepSeek V4 token counting; added provider usage comparison, thinking-mode reasoning preservation, tests, docs, packaging validation, and PR #2.

### Git Commits

| Hash | Message |
|------|---------|
| `42ec94d` | feat: count context with DeepSeek V4 tokenizer |

### Status

[OK] **Completed**


## Session 2: Docker command sandbox
<!-- trellis-session: v=2 fp=e9b920d55707be6e -->

**Date**: 2026-09-03
**Task**: Docker command sandbox
**Branch**: `feat/docker-command-sandbox`

### Summary

Moved opt-in run_command execution into a fail-closed Docker sandbox, completed offline and live containment validation, and opened PR #5.

### Main Changes

- Added explicit trusted-image configuration and immutable image-ID locking across CLI, Agent, registry, and command execution.
- Enforced no network, read-only root, non-root execution, dropped capabilities, bounded resources, sensitive-path masks, bounded output, and exact cleanup.
- Updated tests, README, HANDOFF, TEST_REPORT, and Trellis specifications.

### Git Commits

| Hash | Message |
|------|---------|
| `f6a890f` | feat: sandbox command execution with Docker |

### Testing

- [OK] Full offline suite: 691 passed, 2 skipped in 6.15s.
- [OK] Live Docker test with python:3.11-slim: passed in 32.75s.
- [OK] Full suite with live Docker test: 692 passed, 1 skipped in 40.58s.

### Status

[OK] **Completed**

### Next Steps

- Review PR #5; do not merge automatically.
