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
  "Inspect this project and explain its directory structure."
```

Windows PowerShell:

```powershell
.\.venv\Scripts\gum-code.exe `
  --workspace "C:\path\to\project" `
  "Inspect this project and explain its directory structure."
```

`--workspace` must point to an existing directory. Non-interactive terminals also require a task string.

Submit your task without choosing a development or read-only mode. The deprecated `--task-type` option prints a compatibility notice but no longer changes execution.

The root Agent defaults to 512 model steps per run, configurable with `--max-steps`, and stops earlier on completion. Implementation, self-checks, and repairs share this budget. At the limit, the harness reports remaining work without extra model, tool, or self-check calls. Main-loop steps do not cap all requests, tokens, or spending; subagent and vision quotas remain separate.

Truncated replies or unfinished Todo items trigger continuation reminders within the remaining budget. After two consecutive reminders without progress, another premature ending produces an incomplete outcome. Normal reads, edits, and text continuation can keep working. This guard cannot prove that the model covered every requirement.

The request budget defaults to 80% of the resolved model context window. Use `--max-context-tokens` for a smaller positive integer budget; a value above the model window is a configuration error. A window discovered from provider metadata is used only for the current run and is not written to configuration.

### Optional: enable the Docker command sandbox

Start Docker and prepare a trusted local image first. Gum Code does not pull or build an image for you.

```bash
.venv/bin/gum-code \
  --workspace /path/to/project \
  --allow-run-command \
  --sandbox-image trusted-local-image:tag
```

On Windows, replace the executable with `.\.venv\Scripts\gum-code.exe`. Ordinary commands require approval per call. Automatic self-checks run only host-selected project checks and do not authorize arbitrary model commands.

## Automatic self-checks and repair

After changes to source, tests, or runtime/build configuration, the Agent calls the parameterless `self_check` tool and repairs failures within the remaining steps. If the model ends before checking, the host schedules a check within that same budget. Reading, questions, documentation-only edits, and unchanged writes do not trigger checks. You do not register a check plan or submit acceptance results.

Checks bind to current code and environment inputs. Later edits, checks that rewrite source, or uncertain inputs invalidate previous passes. Installation success, model claims, and PASS strings are not execution evidence.

Self-checks distinguish executed test cases, syntax/type/build checks, and ordinary scripts. The fixed pytest/unittest adapters require a successful non-skipped, non-expected-failure case for test-pass evidence. Zero or all-skipped cases do not count. Unknown scripts, empty scripts, and npm test returning zero prove only successful script execution. Passing basic checks can support qualified completion with function behavior explicitly unverified; successful scripts alone cannot pass self-check. Test execution does not establish assertion quality, requirement coverage, or production readiness.

A completed one-shot task exits with `0`; incomplete work, failed/unavailable checks after code edits, and exhausted budgets return `1`. A failed REPL turn leaves the conversation open. Exhaustion does not trigger another model request, and unchanged failing checks are not rerun in an empty loop.

### Missing environments

An available isolated environment needs no per-check prompt. Downloading a runtime or installing project dependencies requires consent after a short risk explanation; refusal, empty input, or non-interactive execution does not approve preparation. Preparation supports standard public Python/pip and Node.js/npm manifests and compatible locks, using project-specific Docker dependency volumes. Network-enabled preparation containers receive selected dependency metadata, not workspace source or user configuration. Dependency installation scripts can still execute third-party code and use the network. Checks run offline with read-only dependencies and a writable workspace. Gum Code does not install/start Docker, provision databases or cloud services, or install global host packages. Other package managers and unsupported dependencies report an unavailable environment. Preparation requires POSIX locking and safe file operations; Windows is not supported yet.

Existing dirty files are not attributed to the current request. Input scans remain bounded to 20,000 entries, 128 MiB, and 5 seconds, and fail closed on unsafe or uncertain inputs. Only verified managed dependencies and known outputs are excluded; Git-ignore status alone does not exclude source.

The `self_check.state` Session event stores request-local change facts. Resume/fork clears current passing evidence and preparation permission; old `task.state` entries remain historical. Older binaries may reject new fields or events. Preserve original logs and use a compatible or new Session when downgrading.

### Build logs

`run_command` keeps draining stdout/stderr; exceeding the preview or saved-log budget alone does not stop a command. Bounded head/tail buffers are kept in memory, then private logs are saved on completion when possible: at most 5 MiB per file and 100 MiB per workspace for new command logs. Only the oldest completed logs owned by this mechanism are reclaimed; other tool artifacts are untouched. Temporary containers use `--log-driver=none` to avoid a second Docker container log.

The model can page through retained content with `read_tool_output`. Omitted middle output cannot be recovered, and old references may become unavailable after cleanup. The `complete` pagination field means the saved file has been read, not that the original output is complete. Ordinary write, quota, or lock failures allow execution to continue with an explicit unsaved-log status; path and sandbox safety failures still reject execution. A genuine zero exit with unchanged inputs is not failed solely for truncated or unsaved logs, while timeouts and pipe read errors cannot pass. The terminal still does not stream log bodies.

### File, search, and Git pagination

Long listings, searches, UTF-8 file reads, and Git status/diffs retain bounded snapshots. The model uses `read_tool_output` on demand, including for long single lines; you do not page results by hand. A full audit requires reading the relevant scope or stating the coverage gap.

Results distinguish reaching the saved snapshot's end (`complete`), capturing the requested scope (`scope_complete`), and remaining file lines. `read_file` defaults to 200 lines, up to 400; subsequent line windows carry the source version to prevent mixing changed files. Git status/diff accept a workspace-relative path to narrow the scope.

Responses stay within 20,000 characters and snapshots within 5 MiB. Directory scans allow 10,000 entries, 64 levels, and 10 seconds. Text source-version scans allow 32 MiB and 10 seconds; search/Git capture also has byte and time limits. Limits, save failures, and source changes produce explicit gaps. Reading snapshots have no workspace-wide automatic eviction quota; child snapshots expire with the child's temporary directory.

## PDFs, DOCX files, and images

`read_file` still reads UTF-8 text only. The new `read_document` extracts PDF text and DOCX body/table blocks locally in batches, including image references. On demand, `analyze_image` sends one static image, selected PDF page, or DOCX embedded image to a separate visual model and returns text to the main Agent. The main model does not need image support.

After the first main-provider configuration is saved, visual setup is optional; skipping it leaves other features available. Existing users can enter `/vision` in the REPL to configure the recipient, enable/disable sending, or change the default call limit for future requests. Visual settings are stored separately in the private user configuration file `.env.vision`; workspace `.env` values cannot grant consent. Setup explains the recipient and possible charges before saving. It validates settings locally without sending a test image, so successful saving does not verify provider image capability. The service must support OpenAI-compatible Chat Completions with single-image input and text output.

Each explicit user request defaults to 32 visual attempts shared by the parent and all children. Failures and timeouts count; this is not a monetary spending cap. At most two visual calls run concurrently, with a 60-second timeout and no automatic retry. When exhausted, only the host main thread asks for extra calls after collecting the complete batch. Denial or non-interactive execution grants nothing, and visual credit never extends the main model step budget. Resuming the same request preserves consumed calls; missing budget records do not silently restore a full allowance.

Local batches contain at most five PDF pages or 100 DOCX body blocks, subject first to a 20,000-character result limit. The Agent continues using the exact cursor and source version. Text or an OCR layer does not prove that a PDF's visual content was read; scanned pages, vector graphics, and selected regions can be analyzed on demand. Static PNG/JPEG/WebP are supported; animations and unsupported formats are rejected. Ordinary DOCX embedded images are readable, while native complex shapes, SmartArt, unapplied image transforms, and unread non-body content remain explicitly incomplete. Users can supply a PDF or screenshot for complex layouts. No Word, LibreOffice, WPS, OCR engine, or cloud office converter is required.

Source documents are limited to 25 MiB and images to 10 MiB, with additional page, pixel, ZIP/XML, and parsing-time limits. Short-lived parser processes use private temporary snapshots and clean up on completion without persistent image caching. This is not an OS sandbox or secure erasure after abnormal termination. Platforms without safe file-opening primitives reject document reads; Windows paths are not verified. Images and Base64 never enter the main model or Session; analysis text follows existing Session/tool-result retention. A changed source requires a fresh read, and historical analysis is not evidence for its new version.

Visual budgets use a new `vision.state` event without credentials or sending permission. The new reader accepts older v3 logs; older releases reject logs containing the new event. Preserve originals when downgrading and use a readable older Session or a new one, rather than deleting events. Validation uses synthetic documents and simulated providers, not evidence of real-model recognition accuracy or production suitability.

## Skills and Sessions

Use `$skill-name` in a task to load a Codex user Skill before the first model request. Gum Code scans only direct children of `$CODEX_HOME/skills` (or `~/.codex/skills` when unset). Explicit Skills stay sticky in the current Session, with a maximum of three active Skills per task and three sticky names per Session. The root Agent can also use `find_skills`, `load_skill`, and `read_skill_resource`; automatic loads last only for the current task. `/skills` lists, disables, or clears sticky names without printing Skill content.

Skills are read-only instructions and text resources. Gum Code does not execute or install Skill scripts, and Subagents receive neither Skill tools nor Skill content.

New Sessions use private append-only v3 JSONL event logs with the existing 5 MiB total-file limit. The first resume/import of v1 or v2 creates a migrated copy while preserving the source. `/sessions` can show a structural audit timeline without prompts, arguments, results, or Skill bodies, and can fork an independently replayable child from a completed turn. Context compaction appends a replacement projection without deleting earlier events.

Verification plans and historical outcomes use a separate `task.state` event; `turn.completed` remains a conversation boundary. The new reader accepts older logs without verification state and treats them as unverified. Older releases cannot read logs containing `task.state`. Preserve those logs when downgrading and use an older readable Session or a new one; do not delete historical events to force compatibility.

The terminal switches one line between `思考中...`, `正在读取...`, and `正在修改文件...`, without tool names or accumulated status lines. Calls, results, and Session events remain distinct.

Adjacent `task` calls in one model step form a parallel group. Non-`task` tools are serial barriers, and the parent waits for the whole group before committing results in the original Tool Call order. A root run starts at most four Subagents. They share the current OpenAI-compatible client, so an injected client must support concurrent `chat.completions.create()` calls. `Ctrl+C` cooperatively cancels later work but cannot forcibly stop a synchronous HTTP request already in progress.

## Current capabilities and boundaries

| Area | Current capability | Boundary |
| --- | --- | --- |
| Runtime | Provides workspace selection, a persistent REPL, and one-shot CLI tasks. | Gum Code is a local single-user harness. It does not provide a web service or multi-user authorization. |
| Model protocol | Calls OpenAI-compatible Chat Completions with `tool_calls`, using a generic local estimate calibrated by valid provider `usage` values for token budgeting. | Only text messages and tool definitions are counted. A manual model window is required when a third-party gateway does not expose one. |
| Files and Git | Lists, reads, searches, writes, and edits files, and reads Git status and diffs. | File tools stay inside the workspace. Deletion requires approval. Path, symlink, sensitive filename, and size rules apply. |
| Command execution | Runs one argv command in a network-disabled Docker container after explicit enablement. | `run_command` is disabled by default. Ordinary commands need per-call approval; fixed checks can receive explicit stage-scoped repeat permission. The container can modify the workspace and cannot protect against malicious images, a compromised Docker daemon, or runtime vulnerabilities. |
| Self-checks | Runs project checks after actual code changes, binds evidence to current inputs, and repairs/reruns within the remaining steps. | Requires an available isolated environment and distinguishes tests from basic checks. It cannot guarantee repair of every error or full business coverage. |
| Context | Bounds tool results, performs structured compaction, supports manual compaction, and retries once after a context-length error. The terminal reports only the percentage of the configured context limit. | Compaction state belongs to the current Session. Gum Code does not provide cross-Session long-term memory. |
| Sessions | Stores interactive history in v3 JSONL event logs and supports structural audit, completed-turn forks, listing, renaming, resuming, and safe interrupted-batch closure. | Each file is capped at 5 MiB. Start or fork a Session at the limit. Inspect current state when an in-flight tool may have produced side effects; the harness does not replay it. |
| Skills | Reuses the top-level Codex user Skill library through explicit sticky activation and progressive root-Agent reads. | `.system`, plugin, and project Skills are excluded. Scripts never execute, and Subagents receive no Skills. |
| Subagents | Adjacent `task` calls run in parallel. A root run starts at most four single-level, read-only subagents, each with at most eight model requests. | Each Subagent has isolated context and no Skills. It cannot write, run commands, delegate recursively, or persist; ordinary tools remain serial. |
| Hooks | The Python API supports `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop`. | Hooks support synchronous in-process registration only. Gum Code has no hook configuration file, external-command hooks, async callbacks, or automatic discovery. |
| Not implemented | Gum Code currently has no MCP, Goal Loop, long-term memory, recursive subagents, or persistent child agents. | Treat these entries as current boundaries, not commitments for a future release. |
