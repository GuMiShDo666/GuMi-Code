<p align="center">
  <img src="docs/assets/gum-code-logo.png" alt="Gum Code" width="180" />
</p>

<h1 align="center">Gum Code</h1>

<p align="center">
  A local CLI coding agent harness for personal use and learning how agent runtimes work.
</p>

<p align="center">
  <a href="README.md">中文</a> · <strong>English</strong>
</p>

## Run Gum Code

### Requirements

- Git.
- Python 3.11 or newer.
- An OpenAI-compatible model service that supports Chat Completions and `tool_calls`.
- Docker only if you enable the optional `run_command` tool.

### macOS / Linux

```bash
git clone https://github.com/GuMiShDo666/Gum-Code.git
cd Gum-Code
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/gum-code
```

`python3` must point to Python 3.11 or newer. Replace it with your local command for a supported Python version if needed.

### Windows PowerShell

```powershell
git clone https://github.com/GuMiShDo666/Gum-Code.git
Set-Location Gum-Code
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\gum-code.exe
```

The Python version selected by `py -3` must be 3.11 or newer. These commands have not been verified on a Windows machine.

### First run

`gum-code` first asks you to select and trust an existing workspace. If it cannot find provider settings, the setup wizard requests:

- `LLM_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`

Gum Code first asks the provider for the model context window. If the provider does not supply it and there is no built-in value for that model, interactive mode asks for `LLM_CONTEXT_WINDOW`; non-interactive mode reports a configuration error.

The wizard hides the API key while you type. It saves the settings to your user configuration directory only after a minimal model request succeeds.

For manual configuration, run this command on macOS or Linux:

```bash
cp .env.example .env
```

Run this command in Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and replace every example value. If the provider cannot report its context window, uncomment `LLM_CONTEXT_WINDOW` in `.env.example` and set the model's positive integer limit. Do not commit a real API key. Gum Code loads each setting in this order: process environment, user configuration, then `.env` in the startup directory.

### Run a one-shot task

macOS / Linux:

```bash
.venv/bin/gum-code \
  --workspace /path/to/project \
  --task-type read-only \
  "Inspect this project and explain its directory structure."
```

Windows PowerShell:

```powershell
.\.venv\Scripts\gum-code.exe `
  --workspace "C:\path\to\project" `
  --task-type read-only `
  "Inspect this project and explain its directory structure."
```

`--workspace` must point to an existing directory. Non-interactive terminals also require a task string.

Use `--task-type development` for a development stage or `read-only` for questions and inspection. An interactive terminal asks for the type at the start of an undeclared stage. A non-interactive run without a declared or resumed stage type can only report an unverified outcome. Declaring a task type does not authorize commands; a read-only task that modifies files still requires verification.

The root Agent defaults to 128 model steps per run, configurable with `--max-steps`. Implementation, self-review, and repairs share this budget. At the limit, the harness stops and reports remaining work without another model finalization request. Main-loop steps do not cap all model requests, tokens, or spending.

The request budget defaults to 80% of the resolved model context window. Use `--max-context-tokens` for a smaller positive integer budget; a value above the model window is a configuration error. A window discovered from provider metadata is used only for the current run and is not written to configuration.

### Optional: enable the Docker command sandbox

Start Docker and prepare a trusted local image first. Gum Code does not pull or build an image for you.

```bash
.venv/bin/gum-code \
  --workspace /path/to/project \
  --allow-run-command \
  --sandbox-image trusted-local-image:tag
```

On Windows, replace the executable with `.\.venv\Scripts\gum-code.exe`. Ordinary commands require approval per call. Registered checks can receive explicit permission to repeat within the current stage, while retaining the existing safety checks.

## Verification and repair

Confirm the stage scope and check list before development. The Agent runs the project's applicable build/tests, reads failures, and repairs within the remaining steps. Delivery also requires a requirements and change review. The harness reports stage completion only when required checks pass for the current code and the review has no unresolved items. Code changes invalidate previous passes. Missing checks, unavailable environments or permission, exhausted steps, and interruptions are not verification success.

Repeat permission applies only to the current process, workspace, and stage. Ordinary code repairs can be retested; changes to commands, definitions/entry points, execution configuration, or output exclusions require approval again. In the REPL, `/revoke-verification` revokes repeat permission without deleting history; another execution requires fresh approval. Restarting, resuming, or forking does not restore permission. Only declared and approved generated directories can be excluded from input-version checks; new files and Git-ignored files are not assumed to be irrelevant outputs.

A one-shot development task exits with `0` after verified completion and `1` if incomplete or verification fails. A completed read-only answer still exits with `0`. A failed REPL turn leaves the conversation open for follow-up. Passing checks does not establish complete business coverage or production readiness. Bounded content scans may be blocked by workspace size, symbolic-link inputs, or concurrent changes; they are not atomic filesystem snapshots.

## Skills and Sessions

Use `$skill-name` in a task to load a Codex user Skill before the first model request. Gum Code scans only direct children of `$CODEX_HOME/skills` (or `~/.codex/skills` when unset). Explicit Skills stay sticky in the current Session, with a maximum of three active Skills per task and three sticky names per Session. The root Agent can also use `find_skills`, `load_skill`, and `read_skill_resource`; automatic loads last only for the current task. `/skills` lists, disables, or clears sticky names without printing Skill content.

Skills are read-only instructions and text resources. Gum Code does not execute or install Skill scripts, and Subagents receive neither Skill tools nor Skill content.

New Sessions use private append-only v3 JSONL event logs with the existing 5 MiB total-file limit. The first resume/import of v1 or v2 creates a migrated copy while preserving the source. `/sessions` can show a structural audit timeline without prompts, arguments, results, or Skill bodies, and can fork an independently replayable child from a completed turn. Context compaction appends a replacement projection without deleting earlier events.

Verification plans and historical outcomes use a separate `task.state` event; `turn.completed` remains a conversation boundary. The new reader accepts older logs without verification state and treats them as unverified. Older releases cannot read logs containing `task.state`. Preserve those logs when downgrading and use an older readable Session or a new one; do not delete historical events to force compatibility.

Within one model step, adjacent executions of the same tool produce one terminal progress line while every call, result, and Session event remains distinct.

Adjacent `task` calls in one model step form a parallel group. Non-`task` tools are serial barriers, and the parent waits for the whole group before committing results in the original Tool Call order. A root run starts at most four Subagents. They share the current OpenAI-compatible client, so an injected client must support concurrent `chat.completions.create()` calls. `Ctrl+C` cooperatively cancels later work but cannot forcibly stop a synchronous HTTP request already in progress.

## Current capabilities and boundaries

| Area | Current capability | Boundary |
| --- | --- | --- |
| Runtime | Provides workspace selection, a persistent REPL, and one-shot CLI tasks. | Gum Code is a local single-user harness. It does not provide a web service or multi-user authorization. |
| Model protocol | Calls OpenAI-compatible Chat Completions with `tool_calls`, using a generic local estimate calibrated by valid provider `usage` values for token budgeting. | Only text messages and tool definitions are counted. A manual model window is required when a third-party gateway does not expose one. |
| Files and Git | Lists, reads, searches, writes, and edits files, and reads Git status and diffs. | File tools stay inside the workspace. Deletion requires approval. Path, symlink, sensitive filename, and size rules apply. |
| Command execution | Runs one argv command in a network-disabled Docker container after explicit enablement. | `run_command` is disabled by default. Ordinary commands need per-call approval; fixed checks can receive explicit stage-scoped repeat permission. The container can modify the workspace and cannot protect against malicious images, a compromised Docker daemon, or runtime vulnerabilities. |
| Verification | Combines real command outcomes, input versions, and self-review before stage completion, with repairs and reruns within the remaining steps. | Depends on the confirmed scope, check list, and execution environment. It does not guarantee that the model can repair every error or cover every business requirement. |
| Context | Bounds tool results, performs structured compaction, supports manual compaction, and retries once after a context-length error. The terminal reports only the percentage of the configured context limit. | Compaction state belongs to the current Session. Gum Code does not provide cross-Session long-term memory. |
| Sessions | Stores interactive history in v3 JSONL event logs and supports structural audit, completed-turn forks, listing, renaming, resuming, and safe interrupted-batch closure. | Each file is capped at 5 MiB. Start or fork a Session at the limit. Inspect current state when an in-flight tool may have produced side effects; the harness does not replay it. |
| Skills | Reuses the top-level Codex user Skill library through explicit sticky activation and progressive root-Agent reads. | `.system`, plugin, and project Skills are excluded. Scripts never execute, and Subagents receive no Skills. |
| Subagents | Adjacent `task` calls run in parallel. A root run starts at most four single-level, read-only subagents, each with at most eight model requests. | Each Subagent has isolated context and no Skills. It cannot write, run commands, delegate recursively, or persist; ordinary tools remain serial. |
| Hooks | The Python API supports `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop`. | Hooks support synchronous in-process registration only. Gum Code has no hook configuration file, external-command hooks, async callbacks, or automatic discovery. |
| Not implemented | Gum Code currently has no MCP, Goal Loop, long-term memory, recursive subagents, or persistent child agents. | Treat these entries as current boundaries, not commitments for a future release. |
