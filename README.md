<p align="center">
  <img src="docs/assets/gum-code-logo.png" alt="Gum Code" width="180" />
</p>

<h1 align="center">Gum Code</h1>

<p align="center">
  面向个人使用与 Agent 运行机制学习的本地 CLI 编程 Agent Harness。
</p>

<p align="center">
  <strong>中文</strong> · <a href="README_en.md">English</a>
</p>

## 启动

### 环境要求

- Git。
- Python 3.11 或更高版本。
- 一个支持 Chat Completions 和 `tool_calls` 的 OpenAI 兼容模型服务。
- Docker 只在启用可选的 `run_command` 工具时需要。

### macOS / Linux

```bash
git clone https://github.com/GuMiShDo666/Gum-Code.git
cd Gum-Code
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/gum-code
```

`python3` 必须指向 Python 3.11 或更高版本。如果系统使用其他命令名称，请在创建虚拟环境时替换它。

### Windows PowerShell

```powershell
git clone https://github.com/GuMiShDo666/Gum-Code.git
Set-Location Gum-Code
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\gum-code.exe
```

`py -3` 选中的 Python 必须是 3.11 或更高版本。Windows 命令尚未经过实机验证。

### 首次运行

`gum-code` 会先让你选择并信任一个已存在的工作区。如果未找到 Provider 配置，启动向导会请求：

- `LLM_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`

Gum Code 会优先从 provider 的模型元数据获取上下文窗口。如果 provider 不提供且本地没有该模型的内置值，交互模式会请你输入 `LLM_CONTEXT_WINDOW`；非交互模式会直接报配置错误。

向导会隐藏 API Key 输入，并在一次最小模型请求验证成功后才将配置保存到用户配置目录。

如果需要手动配置，macOS/Linux 执行：

```bash
cp .env.example .env
```

Windows PowerShell 执行：

```powershell
Copy-Item .env.example .env
```

编辑 `.env` 并替换所有示例值。当 provider 无法提供上下文窗口时，取消 `.env.example` 中 `LLM_CONTEXT_WINDOW` 的注释并填入模型的正整数上限。不要提交真实的 API Key。配置优先级为：进程环境变量 > 用户配置 > 启动目录中的 `.env`。

### 执行一次性任务

macOS / Linux：

```bash
.venv/bin/gum-code \
  --workspace /path/to/project \
  "检查这个项目并说明目录结构。"
```

Windows PowerShell：

```powershell
.\.venv\Scripts\gum-code.exe `
  --workspace "C:\path\to\project" `
  "检查这个项目并说明目录结构。"
```

`--workspace` 必须指向已存在的目录。非交互终端还必须同时提供任务文本。

请求预算默认为已解析模型上下文窗口的 80%。可用 `--max-context-tokens` 设置更小的正整数预算；超过模型窗口会直接报配置错误。从 provider 自动识别的窗口只用于当前运行，不会写入配置。

### 可选：启用 Docker 命令沙箱

先启动 Docker，并在本地准备一个可信的镜像。Gum Code 不会自动 pull 或 build 镜像。

```bash
.venv/bin/gum-code \
  --workspace /path/to/project \
  --allow-run-command \
  --sandbox-image trusted-local-image:tag
```

Windows 用户将可执行文件替换为 `.\.venv\Scripts\gum-code.exe`。模型每次请求命令都需要终端审批。

## 当前能力与边界

| 范围 | 当前能力 | 边界 |
| --- | --- | --- |
| 运行方式 | 支持工作区选择、持久 REPL 和一次性 CLI 任务。 | 它是本地单用户 Harness，没有 Web 服务或多用户权限系统。 |
| 模型协议 | 调用 OpenAI 兼容的 Chat Completions 和 `tool_calls`，通过通用本地估算和有效的 provider `usage` 校准管理 token 预算。 | 仅统计文本消息和工具定义；第三方网关不提供模型窗口时需要手动配置。 |
| 文件与 Git | 支持列出、读取、搜索、写入和编辑文件，以及查看 Git 状态和差异。 | 文件工具只能访问工作区；删除需要审批；路径、符号链接、敏感文件名和大小受到限制。 |
| 命令执行 | 显式启用后，可以在断网 Docker 容器中运行单个 argv 命令。 | `run_command` 默认关闭，且每次都需要审批。容器仍可修改工作区，也不能防御恶意镜像、Docker daemon 入侵或运行时漏洞。 |
| 上下文 | 支持工具结果裁剪、结构化压缩、主动压缩和超限后的单次重试；终端只显示上下文占上限的百分比。 | 压缩摘要属于当前 Session，不是跨 Session 长期 Memory。 |
| Session | 自动保存交互记录，支持查看、重命名、恢复和安全闭合中断的工具批次。 | 已开始工具的副作用未知时，使用者必须检查当前状态；Harness 不会自动重放该调用。 |
| Subagent | 每次根 Agent 运行最多可启动 4 个同步、单层、只读子 Agent，每个最多使用 8 次模型请求。 | 子 Agent 不能写入、执行命令、并行或递归委派，也不会持久化。 |
| Hooks | Python API 支持 `UserPromptSubmit`、`PreToolUse`、`PostToolUse` 和 `Stop`。 | Hooks 只支持同步、进程内注册；没有配置文件、外部命令、异步回调或自动发现。 |
| 尚未实现 | 项目当前不包含 Skills、MCP、Goal Loop、长期 Memory、并行/递归 Subagent 或子 Agent 持久化。 | 不要把 README 中的边界当作未来版本承诺。 |
