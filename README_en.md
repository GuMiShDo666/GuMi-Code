# GuMi-Code

GuMi-Code is an AI coding assistant for local projects, available as a desktop app and a CLI. Describe a task to read and explain code, edit files, or run commands while developing features, fixing problems, and writing documentation.

You need an OpenAI-compatible model service supporting Chat Completions and tool calls.

[中文](README.md) · **English**

## Desktop app

Currently supports macOS on Apple Silicon. If you already have `GuMi-Code.app`, open it to get started.

### Start from source

Requires Git and Node.js 24.

```bash
git clone https://github.com/GuMiShDo666/GuMi-Code.git
cd GuMi-Code/desktop
npm ci
npm run prepare:runtime
npm run build
npm start
```

The first runtime preparation downloads Python and dependencies. You do not need to install Python separately.

### Configure and use

1. Open Settings → GuMi-Code settings, enter the API base URL, model name, and API key, then save. Use the connection test to check that the endpoint is reachable.
2. Return to the conversation page, select a project folder, and confirm that you trust it.
3. Send a request, such as “Explain this project's main features” or “Fix this error and run the relevant tests.”
4. Approve or deny command requests after reviewing the displayed operation. You can stop a running task or continue a previous conversation from history.

You can add attachments, manage the selected workspace's memory and `AGENTS.md` in settings, and change the interface language.

Configure the API:

![Desktop API settings](docs/assets/readme/desktop-settings.jpg)

Select a project and enter a task:

![Select a project and enter a task](docs/assets/readme/desktop-task.jpg)

The screenshots use a sample project and configuration. The task has not been sent.

### Package the app

After preparing and building in the `desktop` directory:

```bash
npm run package
open release/GuMi-Code.app
```

The app is created at `desktop/release/GuMi-Code.app`. Move an existing app out of that path before packaging again.

See [desktop/README.md](desktop/README.md) for the desktop usage guide in Chinese.

## CLI

Requires Git, Python 3.11 or newer, and an OpenAI-compatible model service supporting Chat Completions and tool calls.

### Install and start on macOS / Linux

```bash
git clone https://github.com/GuMiShDo666/GuMi-Code.git
cd GuMi-Code
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/gum-code
```

Make sure `python3` selects Python 3.11 or newer.

### Install and start on Windows PowerShell

```powershell
git clone https://github.com/GuMiShDo666/GuMi-Code.git
Set-Location GuMi-Code
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\gum-code.exe
```

The interpreter selected by `py -3` must be Python 3.11 or newer. Windows usage has not been verified on a real machine.

### First-time configuration

Follow the prompts to select and trust an existing project folder, then enter the API key, base URL, and model name. If the model's context window cannot be detected, enter its context limit when prompted.

Alternatively, copy the configuration template from the repository root and edit its example values:

```bash
cp .env.example .env
```

On Windows PowerShell, use `Copy-Item .env.example .env`. The fields are `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL`. Set `LLM_CONTEXT_WINDOW` when a manual context limit is needed. Do not commit a real API key.

### Everyday use

Enter a task after starting the CLI. Keep typing to ask follow-up questions or add requirements.

| Command | Purpose |
| --- | --- |
| `/help` | List available commands |
| `/sessions` | Manage and resume previous sessions |
| `/exit` | Exit |

You can also specify a project directory at startup:

```bash
.venv/bin/gum-code --workspace /path/to/project
```

### Run a one-shot task

macOS / Linux:

```bash
.venv/bin/gum-code --workspace /path/to/project "Inspect this project and explain its directory structure."
```

Windows PowerShell:

```powershell
.\.venv\Scripts\gum-code.exe --workspace "C:\path\to\project" "Inspect this project and explain its directory structure."
```

The workspace must be an existing directory. Non-interactive terminals require both the workspace and task text.

### Optional: run commands

Start Docker and prepare a trusted local image before enabling ordinary CLI command execution:

```bash
.venv/bin/gum-code \
  --workspace /path/to/project \
  --allow-run-command \
  --sandbox-image trusted-local-image:tag
```

Replace the image name with an existing local image; the CLI does not pull it automatically. Each command requires approval. On Windows, replace the executable with `.\.venv\Scripts\gum-code.exe`.
