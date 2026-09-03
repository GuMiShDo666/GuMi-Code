# Add Docker command sandbox

## Goal

Run model-requested commands inside a fail-closed Docker container with workspace-only mounting, no network by default, and bounded resources while preserving Permission approval.

## Requirements

- Sandbox scope is limited to the model-visible `run_command` capability.
  Existing filesystem, search, Git, Hook, context, and Session behavior must not
  be moved into Docker by this task.
- `run_command` remains hidden by default and keeps its per-invocation
  `--allow-run-command` opt-in. Enabling it must also require an explicit
  trusted sandbox image selected by the user through the CLI.
- The selected image must already exist in the local Docker image store.
  Harness must use it with `--pull=never`; it must never pull, build, or let the
  model choose an image.
- Every approved command runs in a fresh Docker container. There is no host
  subprocess fallback when Docker, the daemon, the image, or container startup
  is unavailable.
- The container receives one read-write bind mount for the selected workspace
  and no host home directory, credential directory, Docker socket, device, or
  unrelated host path.
- Existing workspace paths classified as sensitive or internal by the Harness
  safety policy must be hidden from the container with empty read-only masks.
  Safe environment templates such as `.env.example` remain visible.
- Container networking is disabled. The root filesystem is read-only, writable
  temporary storage is bounded, Linux capabilities are dropped, privilege
  escalation is disabled, and CPU, memory, process, and file-descriptor limits
  are applied.
- The container process runs as the invoking user's numeric UID/GID where the
  platform supports it. The image's configured entrypoint must not replace or
  wrap the approved argv.
- Model input remains a single argv-based program with a workspace-relative
  cwd and the current shell/path/hard-deny validation as defense in depth.
  Model-controlled text must appear only after fixed Docker options and the
  fixed user-selected image.
- Permission order remains fail closed: structured hard deny, side-effect-free
  validation and Docker preflight, `PreToolUse`, explicit terminal approval,
  sandbox execution, then `PostToolUse`.
- The Docker CLI and daemon/image availability are checked before the first
  model request when command execution is enabled, and checked again before
  approval/execution. Expected failures produce bounded actionable messages
  without exposing Docker configuration, host environment values, or raw
  internal errors.
- Each container has an unpredictable Harness-owned name. Timeout, output-quota
  termination, interruption, and Docker client failure trigger best-effort
  removal of that exact container so work cannot continue in the background.
- stdout and stderr are drained without unbounded in-memory buffering. Crossing
  the capture quota terminates the container and returns a bounded result.
- Documentation must state the remaining boundary honestly: Docker reduces
  host exposure, but the approved command can still modify non-masked workspace
  files, a trusted image is required, and Docker/VM/kernel vulnerabilities are
  outside the Harness guarantee.

## Constraints and Non-Goals

- No network-enabled mode, privileged mode, host PID/IPC namespace, Docker
  socket mount, arbitrary extra mounts, or model-selected resource settings.
- No bundled universal development image, automatic image build/pull/update,
  registry authentication, or package installation workflow in this task.
- No attempt to make arbitrary language runtimes available. Available programs
  and dependencies are determined by the user-selected local image.
- No claim of absolute isolation. This task does not protect files inside the
  writable workspace that are not classified as sensitive, and it does not
  defend against a compromised Docker daemon or container-runtime escape.
- No Session schema change. Command enablement and sandbox image selection are
  per-run capabilities and must be supplied again when resuming.

## Acceptance Criteria

- [x] Without `--allow-run-command`, `run_command` is absent from model-visible
      schemas and no Docker preflight occurs.
- [x] `--allow-run-command` without an explicit sandbox image fails before any
      model request; a missing CLI, unavailable daemon, or missing local image
      also fails closed without pulling or asking for command approval.
- [x] The model cannot control the image, Docker flags, mounts, container name,
      environment, network, privileges, or resource limits.
- [x] An approved command is executed only through Docker with no network,
      read-only root, dropped capabilities, no-new-privileges, bounded
      resources, a non-root numeric user, and no inherited host secrets.
- [x] The selected workspace and requested relative cwd map correctly into the
      container, and ordinary workspace changes are visible on the host.
- [x] Existing `.env` variants, credential/key files, `.git`, `.ssh`, `.aws`,
      `.transcripts`, `.task_outputs`, and other current sensitive-path matches
      are not readable through the command container.
- [x] No host path outside the workspace and no Docker socket is mounted into
      the command container.
- [x] Invalid commands and unavailable sandbox prerequisites are rejected
      before `PreToolUse` and terminal approval; valid commands still require
      both the existing policy and explicit approval.
- [x] Timeout, excessive output, and interruption stop and remove the exact
      Harness-owned container, and model-visible output remains within the
      documented limit.
- [x] Offline unit tests cover argv construction, masks, configuration wiring,
      permission ordering, bounded capture, cleanup, failures, and compatibility
      of default-disabled command mode without requiring Docker.
- [x] An opt-in live integration test with a preloaded user-selected image proves
      workspace write-through, sensitive-path masking, network denial, non-root
      execution, output/timeout cleanup, and absence of host fallback.
- [x] README, HANDOFF, CLI help, and Trellis specs describe setup, usage,
      limitations, and the new sandbox boundary consistently.
- [x] Focused tests, the full offline suite, a fresh package install probe, and
      the live Docker test (once the daemon is available) pass before the branch
      is committed, pushed, and opened as a pull request to `main`.

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
