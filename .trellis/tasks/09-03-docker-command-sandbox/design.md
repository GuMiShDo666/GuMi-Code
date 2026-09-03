# Docker Command Sandbox Design

## Decision Summary

Replace direct host execution for `run_command` with a per-call Docker
container. Keep the existing command schema, hard-deny rules, preflight,
Permission approval, and Hook order as defense in depth. Require an explicit
local image at CLI startup, never pull it, hide existing sensitive workspace
paths, bound resources and output while running, and fail closed on every
sandbox prerequisite or cleanup error that makes execution safety uncertain.

## Configuration Contract

Add a small frozen `DockerSandboxConfig` owned by `tools/command.py`. It carries
only the resolved Docker executable and immutable local image ID. Named module
constants supply the first-version resource/output policy; the model and tool
schema cannot override them.

CLI behavior:

```text
default
  -> run_command hidden; no Docker requirement

--allow-run-command without --sandbox-image
  -> argparse configuration error before settings/tokenizer/model activity

--allow-run-command --sandbox-image <trusted-local-reference>
  -> resolve Docker CLI
  -> verify daemon is reachable
  -> inspect the exact local image with no pull and resolve it to its image ID
  -> create DockerSandboxConfig
  -> pass it through run_agent and execute_tool
```

Supplying `--sandbox-image` without `--allow-run-command` is rejected to make
the capability state unambiguous. The image and enablement remain per-run and
are not written to Session files. Documentation recommends a digest or image
ID; tags are allowed only as an explicit local user choice and still use
`--pull=never`.

The trusted Docker control-plane subprocess may use the host's Docker context
configuration to reach Docker Desktop. None of its environment is forwarded to
the container. Raw control-plane stderr is converted into stable errors.

## Runtime Wiring

```text
CLI parse + startup Docker preflight
-> run_agent(allow_run_command=True, command_sandbox=config)
-> dynamic run_command schema
-> model Tool Call / per-run allow-set
-> execute_tool(command_sandbox=config)
-> structured hard deny
-> command + sandbox preflight
-> PreToolUse veto
-> terminal approval
-> run_command(..., sandbox=config)
-> revalidate command, image, and sensitive mounts
-> Docker container execution + bounded drain
-> cleanup
-> PostToolUse
-> matching Tool Result / Session snapshot
```

`run_agent` rejects the inconsistent state where command execution is enabled
without a sandbox config. `execute_tool` injects the config only for the
`run_command` preflight and handler; model JSON is never merged into it. Other
registry entries retain their current signatures and behavior.

## Container Policy

Harness constructs one argv list and invokes Docker without a shell. The fixed
portion precedes the selected image and model command:

```text
docker run --rm --name <unpredictable-harness-name>
  --pull never
  --network none
  --read-only
  --cap-drop ALL
  --security-opt no-new-privileges=true
  --pids-limit <fixed>
  --memory <fixed>
  --memory-swap <same-fixed-limit>
  --cpus <fixed>
  --ulimit nofile=<fixed>:<fixed>
  --ipc none
  --init
  --user <host-uid>:<host-gid>
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=<fixed>
  --mount type=bind,src=<workspace>,dst=/workspace,rw
  <one mask mount per existing sensitive path>
  --workdir /workspace/<validated-relative-cwd>
  --env HOME=/tmp
  --env XDG_CONFIG_HOME=/tmp/.config
  --env XDG_CACHE_HOME=/tmp/.cache
  --env XDG_DATA_HOME=/tmp/.local/share
  --entrypoint <approved-program>
  <trusted-local-image>
  <approved-arguments...>
```

Exact limits will be named constants and tested. Initial targets are one CPU,
512 MiB memory, 128 processes, 256 file descriptors, 64 MiB temporary storage,
the existing 1-300 second timeout, and the existing 50,000-character
model-visible output ceiling.

No `--privileged`, added capability, host namespace, device, Docker socket,
host HOME/config, SSH agent, credential mount, environment pass-through, or
extra user mount is supported. The image entrypoint is overridden so it cannot
silently wrap the approved argv.

## Workspace and Sensitive Masks

The workspace is mounted read-write because approved build/test commands may
create normal artifacts. Before launch, Harness walks the workspace without
following symlinks and reuses `_is_sensitive_path` to identify current
sensitive files and directories. Each match receives an empty read-only bind
mount of the same file/directory kind, created under a one-shot Harness-owned
temporary mask directory.

If a path cannot be represented safely in Docker mount syntax, changes type
during preparation, or cannot be masked, launch fails. Sensitive directories
are pruned during enumeration so children do not need redundant mounts. The
mask list is capped at 256 entries; overflow fails closed before approval. Safe
templates such as `.env.example` remain visible under the existing policy.

This design intentionally hides `.git` and `.venv` from arbitrary command
execution. Built-in Git tools remain available, and runtime dependencies must
come from the selected image. The container can modify or delete all other
workspace files after the user approves the command; Permission remains the
authorization boundary for that risk.

## Process, Output, and Cleanup

Use `subprocess.Popen` for the trusted Docker CLI and drain stdout/stderr
concurrently in fixed-size chunks. Retain only a bounded combined prefix. When
the raw capture quota is crossed, stop the named container immediately and
return a bounded output-limit error; do not continue draining an unlimited
producer until the normal timeout.

Generate the container name with cryptographic randomness and a fixed Harness
prefix. On timeout, output overflow, interruption, or uncertain Docker client
failure:

1. issue `docker rm --force <exact-name>` with a short internal timeout;
2. terminate/kill and reap the Docker CLI process if still alive;
3. never target a prefix, label query, glob, or user-supplied name;
4. return a stable bounded Tool Result, or re-raise interruption after cleanup.

`--rm` handles normal completion, but is not treated as sufficient for timeout
cleanup. Docker exit code 125 is reported as sandbox startup failure; ordinary
container program exit codes and bounded stdout/stderr retain the current result
shape where safe.

## Preflight and Error Contract

Startup preflight gives immediate feedback before any model request. Tool
preflight repeats the cheap safety checks before Hook/approval because daemon,
image, workspace, and mask state may have changed. The handler repeats them
immediately before launch.

Expected missing CLI/daemon/image, invalid mount path, container-start,
timeout, and cleanup failures use concise Chinese messages. Raw Docker stderr,
exception reprs, workspace paths, environment values, and image-registry
credentials are never returned to the model or normal terminal events.

If exact-container cleanup cannot be confirmed after a failed launch, the
result states that manual Docker inspection is required. Harness still never
falls back to the host.

## Compatibility

- Default runs have no Docker dependency and preserve current tool schemas.
- `run_command` remains `EXECUTE` risk and requires approval for every call.
- Hook order and Tool Result pairing do not change.
- The tool JSON schema does not gain image, mount, network, environment, user,
  or resource fields.
- Existing host-oriented `run_command` unit tests are migrated to Docker argv
  and fake-process tests. The default test suite remains offline and does not
  require a daemon.
- Session format/version remains unchanged; resumed runs must opt in and supply
  the image again.

## Test Strategy

Offline tests use monkeypatched Docker discovery/inspection/process boundaries
and temporary workspaces. They assert exact invariant flags and ordering,
absence of host fallback and secret env, path/cwd mapping, sensitive masks,
preflight-before-approval, output quota behavior, exact-name cleanup, sanitized
errors, schema visibility, CLI wiring, and Session resume behavior.

An opt-in integration test runs only when a caller supplies a preloaded image
reference. It verifies write-through to a temporary workspace, hidden `.env`
and `.git`, no network, non-root identity, read-only root, timeout/output
cleanup, and no access to a marker outside the workspace. It never pulls an
image or reads repository credentials.

## Rollback

Rollback removes the sandbox config/CLI wiring and Docker execution adapter,
then restores the prior host command implementation and documentation warning.
No Session migration or data rollback is required. Because direct host
execution is the known unsafe boundary, a production rollback should disable
`run_command` entirely rather than silently re-enable host execution.
