# Product Hooks Implementation Plan

## 1. Hook Registry

- [x] Add `src/vibe_harness/hooks.py` with four validated event names.
- [x] Preserve registration order and first-non-`None` short circuit.
- [x] Add focused tests for all registry invariants.

Validation:

```bash
PYTHONPATH=src /Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/test_hooks.py
```

## 2. Tool Lifecycle Integration

- [x] Add optional pre/post callbacks to `execute_tool()`.
- [x] Place `PreToolUse` after hard deny/risk/preflight and before approval.
- [x] Trigger `PostToolUse` only after a real handler attempt.
- [x] Pass defensive copies and preserve current error boundaries.
- [x] Prove Hooks cannot authorize denied calls or bypass approval.

Validation:

```bash
PYTHONPATH=src /Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/test_tool_registry.py
```

## 3. Agent Lifecycle Integration

- [x] Add optional `hook_registry` injection to `run_agent()`.
- [x] Trigger `UserPromptSubmit` before the current request is appended.
- [x] Adapt tool events to the dispatcher callbacks.
- [x] Trigger `Stop` before natural return and implement bounded continuation.
- [x] Preserve matching Tool Results, snapshots, compaction, and step finalization.

Validation:

```bash
PYTHONPATH=src /Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/test_agent.py
```

## 4. Documentation and Specifications

- [x] Document the programmatic API and non-bypassable Permission boundary.
- [x] Update HANDOFF capability/current-state sections.
- [x] Add Hook lifecycle rules to the Trellis backend spec and index.

## 5. Final Review and Git Flow

- [x] Run the full offline suite (`652 passed in 3.36s`).
- [x] Review scoped diff and repository status.
- [x] Scan staged filenames/content for likely secrets without reading `.env`.
- [ ] Commit with a Conventional Commit message.
- [ ] Push `feat/product-hooks` and open a pull request into `main`.

Full validation:

```bash
PYTHONPATH=src \
PYTHONPYCACHEPREFIX=/Users/gumishdo/.local/share/vibe-harness/pycache \
/Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider
```
