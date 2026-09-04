<p align="center">
  <img src="docs/assets/gum-code-logo.jpg" alt="Gum Code" width="180" />
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
- An OpenAI-compatible model service that supports Chat Completions and `tool_calls`. The tokenizer currently accepts only the exact model name `deepseek-v4-flash`.
- Network access for the first model request so Gum Code can download and cache the pinned DeepSeek V4 tokenizer. You can also pass a downloaded, hash-matched `tokenizer.json` with `--tokenizer-file`.
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
- `LLM_MODEL`, which must currently be `deepseek-v4-flash`

The wizard hides the API key while you type. It saves the settings to your user configuration directory only after a minimal model request succeeds.

For manual configuration, run this command on macOS or Linux:

```bash
cp .env.example .env
```

Run this command in Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and replace every example value. Do not commit a real API key. Gum Code loads each setting in this order: process environment, user configuration, then `.env` in the startup directory.

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

### Optional: enable the Docker command sandbox

Start Docker and prepare a trusted local image first. Gum Code does not pull or build an image for you.

```bash
.venv/bin/gum-code \
  --workspace /path/to/project \
  --allow-run-command \
  --sandbox-image trusted-local-image:tag
```

On Windows, replace the executable with `.\.venv\Scripts\gum-code.exe`. The terminal asks for approval each time the model requests a command.

## Current capabilities and boundaries

| Area | Current capability | Boundary |
| --- | --- | --- |
| Runtime | Provides workspace selection, a persistent REPL, and one-shot CLI tasks. | Gum Code is a local single-user harness. It does not provide a web service or multi-user authorization. |
| Model protocol | Calls OpenAI-compatible Chat Completions with `tool_calls`. | The local tokenizer accepts only the exact model name `deepseek-v4-flash`. Third-party gateways may enforce a different context limit. |
| Files and Git | Lists, reads, searches, writes, and edits files, and reads Git status and diffs. | File tools stay inside the workspace. Deletion requires approval. Path, symlink, sensitive filename, and size rules apply. |
| Command execution | Runs one argv command in a network-disabled Docker container after explicit enablement. | `run_command` is disabled by default and always requires approval. The container can modify the workspace and cannot protect against malicious images, a compromised Docker daemon, or runtime vulnerabilities. |
| Context | Bounds tool results, performs structured compaction, supports manual compaction, and retries once after a context-length error. The terminal reports only the percentage of the configured context limit. | Compaction state belongs to the current Session. Gum Code does not provide cross-Session long-term memory. |
| Sessions | Saves interactive history and supports listing, renaming, resuming, and safely closing interrupted tool batches. | You must inspect current state when an in-flight tool may have produced side effects. The harness does not replay that call automatically. |
| Subagents | A root run can start up to four synchronous, single-level, read-only subagents. Each subagent can make up to eight model requests. | Subagents cannot write, run commands, execute in parallel, delegate recursively, or persist between runs. |
| Hooks | The Python API supports `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop`. | Hooks support synchronous in-process registration only. Gum Code has no hook configuration file, external-command hooks, async callbacks, or automatic discovery. |
| Not implemented | Gum Code currently has no Skills, MCP, Goal Loop, long-term memory, parallel or recursive subagents, or persistent child agents. | Treat these entries as current boundaries, not commitments for a future release. |
