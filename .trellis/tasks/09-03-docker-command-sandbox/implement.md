# Docker Command Sandbox Implementation Plan

## 1. Sandbox Configuration and Startup Gate

- [x] Add the frozen command sandbox config and named fixed policy constants.
- [x] Add `--sandbox-image` and enforce its exact relationship with
      `--allow-run-command`.
- [x] Resolve the Docker executable and verify daemon plus exact local image
      with no pull before constructing the model client/request path.
- [x] Pass the config explicitly through CLI, Agent, registry preflight, and the
      `run_command` handler without module-global mutable state.
- [x] Add parse/help/main-wiring/default-disabled/resume regression tests.

Validation:

```bash
PYTHONPATH=src /Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/test_cli.py tests/test_agent.py \
  tests/test_tool_registry.py
```

Rollback point: command mode still stays hidden by default; if config wiring is
uncertain, stop here without replacing host execution.

## 2. Docker Argv and Workspace Mask Construction

- [x] Build fixed Docker argv without a shell and place all model argv only in
      the entrypoint/argument positions after Harness options and image.
- [x] Map validated relative cwd under `/workspace`.
- [x] Apply no-network, read-only root, no-new-privileges, dropped capabilities,
      non-root user, tmpfs, CPU/memory/PID/nofile, init, and lifecycle flags.
- [x] Enumerate sensitive paths without following symlinks, create one-shot
      empty masks, prune sensitive directories, and fail on unsafe mount syntax
      or type races.
- [x] Prove that Docker socket, host HOME/config, secrets, devices, extra mounts,
      and environment values are absent.
- [x] Add focused argv/mount/mask/adversarial path tests.

Validation:

```bash
PYTHONPATH=src /Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/test_command_tool.py
```

## 3. Bounded Execution and Exact Cleanup

- [x] Replace host `subprocess.run` with Docker `Popen` execution.
- [x] Drain stdout/stderr concurrently with a combined capture quota and retain
      only bounded model-visible text.
- [x] Assign a cryptographically random Harness-owned container name.
- [x] On timeout, output overflow, interruption, or uncertain failure, remove
      that exact container, terminate/kill and reap the CLI process, and never
      target broader Docker state.
- [x] Preserve bounded exit-code/stdout/stderr results for ordinary command
      completion and sanitize Docker/runtime failures.
- [x] Add unit tests for timeout, overflow, interrupt, nonzero exit, Docker exit
      125, cleanup failure, and no host fallback.

Validation:

```bash
PYTHONPATH=src /Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/test_command_tool.py \
  tests/test_tool_registry.py
```

## 4. Permission, Hooks, Events, and Session Regression

- [x] Keep structured hard deny and command/sandbox preflight before
      `PreToolUse` and terminal approval.
- [x] Keep per-call `EXECUTE` approval and `PostToolUse` only after handler
      completion.
- [x] Keep forged/default-disabled calls rejected and every Tool Call paired
      with its original Tool Result ID.
- [x] Verify that normal events do not expose Docker argv, image details, host
      paths, command arguments/results, or control-plane errors.
- [x] Verify Session files persist neither enablement nor sandbox image and
      resumed invocations must opt in again.

Validation:

```bash
PYTHONPATH=src /Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/test_permission_policy.py \
  tests/test_tool_registry.py tests/test_agent.py tests/test_events.py \
  tests/test_session.py
```

## 5. Documentation and Specifications

- [x] Update README command examples, trusted/preloaded image setup, failure
      behavior, resource/network/filesystem policy, and limitations.
- [x] Update HANDOFF runtime map, validation evidence, known boundaries, and
      next-step notes.
- [x] Update tool/permission, error-handling, quality/testing, structure, and
      cross-layer specs only where the implemented contract creates a durable
      project convention.
- [x] Ensure no document claims Docker is absolute isolation or that arbitrary
      project runtimes are bundled.

## 6. Verification and Standard Git Flow

- [x] Run all focused offline tests.
- [x] Run the complete offline suite.
- [x] Build and install the package in a fresh temporary environment; verify
      CLI help/default mode without Docker.
- [x] After Docker Desktop is running, use a preloaded trusted image for the
      opt-in live integration test; do not pull automatically.
- [x] Inspect live cleanup with exact container names after timeout and output
      overflow.
- [x] Review scoped diff/status and scan staged filenames/content for likely
      secrets without opening `.env`.
- [ ] Commit with a Conventional Commit, push `feat/docker-command-sandbox`, and
      open a pull request into `main`. Do not merge it automatically.

Full offline validation:

```bash
PYTHONPATH=src \
PYTHONPYCACHEPREFIX=/Users/gumishdo/.local/share/vibe-harness/pycache \
/Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider
```

Docker Desktop Server 29.1.3 is reachable. After explicit user authorization,
the Docker Official Image `python:3.11-slim` was pulled manually for validation;
Harness itself did not pull it. The live integration test passed in 32.75s, and
the full suite including that test passed with 692 tests and one tokenizer skip
in 40.58s. Timeout and output-overflow cases left the exact pre-test Harness
container set unchanged.
