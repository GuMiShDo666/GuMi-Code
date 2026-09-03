# Docker Command Sandbox Research

## Current Source Boundary

`run_command` currently validates an argv list and runs it directly on the host
with `subprocess.run`. It rejects direct shell launch, shell operators,
redirection, suspicious path arguments, and a small hard-deny set; it also uses
a one-shot HOME/XDG environment and truncates the returned text.

Those controls are useful defense in depth, but interpreters, project scripts,
test runners, and wrappers still execute arbitrary code. Post-capture truncation
also does not bound memory while a child is producing output. The current source
and HANDOFF therefore correctly state that this is not an OS sandbox.

## Threat Model

The sandbox must contain a model-selected process that may be malicious. It
protects the rest of the host filesystem, host credentials/configuration,
network access, and host resource availability while deliberately permitting
approved changes to ordinary files in the chosen workspace.

The following remain trusted control-plane inputs:

- the local Docker CLI and daemon;
- the image explicitly selected by the user at Harness startup;
- Harness-owned Docker flags, mounts, limits, and container names;
- the existing Permission and Hook implementations.

The model controls only the command argv, relative cwd, and bounded timeout. It
does not control Docker options or the image. A malicious image, compromised
Docker daemon, or Docker/VM/kernel escape is outside the guarantee.

## Docker Findings

Docker's `run` interface supports the required controls:

- `--network=none` removes container networking;
- `--read-only` makes the container root filesystem read-only;
- `--mount` creates explicit bind/tmpfs mounts;
- `--cap-drop=ALL` removes Linux capabilities;
- `--security-opt no-new-privileges=true` blocks gaining new privileges;
- `--memory`, `--cpus`, `--pids-limit`, and `--ulimit` bound resources;
- `--user`, `--init`, `--rm`, and a fixed `--name` provide process identity and
  lifecycle controls;
- `--pull=never` fails instead of downloading a missing image.

Docker explicitly warns that `--privileged` is not a securely sandboxed mode,
so it is prohibited. Docker Desktop on macOS runs Linux containers inside its
VM, but bind-mounted host paths remain an explicit host exposure. Enhanced
Container Isolation is not assumed because it is a Docker Business feature.

Sources reviewed 2026-09-03:

- https://docs.docker.com/reference/cli/docker/container/run/
- https://docs.docker.com/engine/security/
- https://docs.docker.com/security/faqs/containers/
- https://docs.docker.com/reference/cli/docker/image/pull/

## Workspace Sensitive-Path Finding

Mounting the complete workspace without additional controls would expose `.env`
files, credential/key files, internal transcripts, and Session-adjacent
artifacts to an interpreter. Disabling network is insufficient because command
output becomes a Tool Result and can be sent to the model provider.

The first version should reuse the canonical sensitive-name policy in
`tools/common.py`, enumerate existing matching files/directories immediately
before launch, and overlay them with empty read-only bind mounts. Directory
walks must not follow symlinks. Unsupported mount-path syntax must fail clearly
instead of silently omitting a mask.

This protects the names covered by current policy, not arbitrary secrets stored
under ordinary source filenames. The README and approval warning must preserve
that limitation.

## Image Strategy Decision

Do not bundle a universal image or automatically pull one. A universal image is
large, quickly becomes stale, and still cannot match arbitrary Python, Node,
Go, or system dependencies. Automatic pulls add network, supply-chain, and
authentication behavior to the security boundary.

Require a user-selected local image through `--sandbox-image`. Harness uses
`--pull=never`, performs local image inspection, and never exposes the image
choice to the model. Documentation recommends an immutable digest or image ID.
The available programs and dependencies are therefore explicit properties of
the user's trusted image.

## Local Environment Probe

The Docker CLI is installed on the current macOS host, but the daemon was not
reachable during planning. Offline design/unit work can proceed, while live
container acceptance must wait until Docker Desktop is running. No image was
pulled as part of research.

## Main Failure Modes to Cover

- Docker executable absent, daemon unavailable, or local image missing;
- model attempts to inject Docker options through command argv;
- image entrypoint wrapping the approved command;
- inherited host environment or Docker socket exposure;
- sensitive workspace files visible inside the bind mount;
- timeout killing only the Docker client while the container keeps running;
- stdout/stderr filling unbounded host memory;
- raw Docker errors leaking host paths or configuration;
- a feature flag or resumed Session silently bypassing per-run opt-in.
