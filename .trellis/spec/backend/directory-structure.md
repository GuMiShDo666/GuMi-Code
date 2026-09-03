# Project Structure

> Source-backed ownership boundaries for the Vibe Harness Python CLI.

## Project Shape

This repository is one Python 3.11 package using the `src/` layout. It has no
HTTP API, database, frontend, plugin system, or background worker. Do not add
those layers unless a task explicitly requires them.

```text
src/vibe_harness/
├── __main__.py          # `python -m vibe_harness` entry point
├── cli.py               # CLI parsing, dependency construction, exit codes
├── config.py            # environment-backed model settings
├── agent.py             # model/tool loop and orchestration
├── context.py           # context budgets, persistence, compaction
├── session.py           # durable JSON checkpoints and recovery plans
├── events.py            # terminal rendering of structured agent events
├── todo.py              # in-memory todo validation and rendering
├── permissions/         # risk policy and terminal consent
└── tools/               # schemas, dispatch, validation, handlers
tests/                  # one test module per source capability
```

Evidence: `pyproject.toml`, `src/vibe_harness/__main__.py`,
`src/vibe_harness/cli.py`, and the current module tree.

## Ownership Rules

- Keep argument parsing and process-level wiring in `cli.py`. `main()` builds
  settings/client/callbacks, invokes `run_agent()`, and translates expected
  top-level failures into exit code `1`.
- Keep the iterative model protocol in `agent.py`. It owns messages, step
  limits, control tools, tool-result closure, and context-stage ordering.
- Put context sizing and compaction algorithms in `context.py`; do not embed
  them in CLI rendering or tool handlers.
- Put durable checkpoint validation and interrupted-batch recovery in
  `session.py`. Session files store model-visible state, not client objects or
  permission flags.
- Put user-facing progress formatting in `events.py`; runtime modules emit
  structured event names and payloads through callbacks.
- Put workspace-facing capabilities under `tools/`. `registry.py` owns the
  schema/risk/handler/preflight tables; capability modules own validation and
  side effects.
- Put authorization decisions under `permissions/`. Policy must not depend on
  terminal input; terminal approval must not decide risk.

## Adding or Changing a Capability

Before adding a new module, check whether the behavior belongs to an existing
owner. A new tool normally requires coordinated changes to:

1. A handler and validator in `src/vibe_harness/tools/`.
2. Its JSON schema, handler, and risk registration in `tools/registry.py`.
3. Permission/preflight behavior where the tool can mutate or execute.
4. Agent-loop protocol tests and focused handler/registry tests.
5. CLI/events only if the user-visible contract changes.

Do not create service/repository/controller abstractions for a single call
site. The current code favors module-level functions and small frozen data
classes over dependency-heavy class hierarchies.

## Naming and Imports

- Modules and functions use `snake_case`; constants use `UPPER_SNAKE_CASE`.
- Internal helpers start with `_`; externally used capability functions keep
  descriptive verbs such as `load_session`, `execute_tool`, and `read_file`.
- Use `pathlib.Path` for filesystem boundaries.
- Use package imports such as `from vibe_harness.context import ...`; tests
  import public symbols directly and import a module when monkeypatching its
  internals.

## Non-Applicable Template Areas

There is no database or ORM in the current source. Do not invent migration,
transaction, table-naming, or repository-layer rules. Add a database spec only
after a real persistence dependency and source layer exist.
