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
  --task-type read-only \
  "检查这个项目并说明目录结构。"
```

Windows PowerShell：

```powershell
.\.venv\Scripts\gum-code.exe `
  --workspace "C:\path\to\project" `
  --task-type read-only `
  "检查这个项目并说明目录结构。"
```

`--workspace` 必须指向已存在的目录。非交互终端还必须同时提供任务文本。

用 `--task-type development` 声明开发阶段，或用 `read-only` 声明问答/只读阶段。交互终端会在新阶段尚未声明类型时询问；非交互运行未声明且没有可恢复阶段类型时，只能报告未验证。声明任务类型不会批准命令执行，只读任务实际修改文件后仍需验收。

主 Agent 每次运行默认最多 128 个模型步骤，可用 `--max-steps` 覆盖。实现、自查和失败修复共享该预算；用尽后停止并报告未完成项，不追加模型收尾请求。主步骤数不等于全部模型请求数、token 或费用上限。

请求预算默认为已解析模型上下文窗口的 80%。可用 `--max-context-tokens` 设置更小的正整数预算；超过模型窗口会直接报配置错误。从 provider 自动识别的窗口只用于当前运行，不会写入配置。

### 可选：启用 Docker 命令沙箱

先启动 Docker，并在本地准备一个可信的镜像。Gum Code 不会自动 pull 或 build 镜像。

```bash
.venv/bin/gum-code \
  --workspace /path/to/project \
  --allow-run-command \
  --sandbox-image trusted-local-image:tag
```

Windows 用户将可执行文件替换为 `.\.venv\Scripts\gum-code.exe`。普通命令逐次审批；已登记的固定检查可以另行批准在当前阶段重复执行，仍须通过现有安全检查。

## 交付验收与自动修复

开发阶段先确认范围和检查清单，Agent 再执行项目适用的编译/测试、读取失败结果并修复。交付前还需核对需求与改动；只有当前代码对应的必要检查通过、自查无未解决项，才报告阶段验收完成。修改代码会使旧通过结果失效。没有适用检查、缺少环境或许可、预算耗尽和用户中断均不能算验证成功。

固定检查的重复许可仅当前进程、工作区和阶段有效。普通代码修复后可以重测；改动检查命令、定义/入口、执行配置或输出排除范围需要重新批准。REPL 中可用 `/revoke-verification` 撤销重复许可，保留历史记录；下次执行须重新批准。关闭后重新打开、恢复或 fork 会话不继承许可。只有明确批准的生成目录才能从工作版本检查中排除，Agent 不会把所有新文件或 Git 忽略项自动当成无关产物。

一次性运行的开发任务验证完成返回 `0`，未完成或验证失败返回 `1`；问答/只读任务正常完成仍返回 `0`。REPL 单轮失败不会关闭会话，你可以继续处理未完成项。通过检查只证明已登记的检查在对应输入版本上通过，不保证业务覆盖完整或生产就绪。有界内容扫描可能因工作区规模、链接输入或并发修改受阻，也不是原子文件系统快照。

## Skills 与 Session

在任务中写 `$skill-name` 可在第一次模型请求前加载对应的 Codex 用户 Skill。Gum Code 只扫描 `$CODEX_HOME/skills`（未设置时为 `~/.codex/skills`）的直接子目录；显式 Skill 会在当前 Session 中保持 sticky，单个任务与 Session 最多同时保留 3 个。根 Agent 也可按需使用 `find_skills`、`load_skill` 和 `read_skill_resource`，自动加载仅对当前任务有效。`/skills` 可查看、禁用或清空 sticky 名称，不显示 Skill 内容。

Skills 只作为只读指令与文本资源加载。Gum Code 不执行 Skill 脚本、不安装 Skill，也不会把 Skill 工具或内容交给 Subagent。

新 Session 使用私有、追加写入的 v3 JSONL 事件日志，文件总上限仍为 5 MiB。v1/v2 Session 在首次恢复或导入时复制迁移，源文件保持不变。`/sessions` 可查看不含提示词、参数、结果或 Skill 正文的安全审计时间线，也可从已完成 turn 创建独立 fork。上下文压缩只追加新的活动投影，不删除旧事件。

验收计划和历史结果使用独立的 `task.state` 事件保存；`turn.completed` 仍只表示对话消息边界。新版可以读取没有验收状态的旧日志，按未验收处理；旧版无法读取含 `task.state` 的新日志。回退程序时保留新日志，使用旧版可读的旧会话或新建会话，不要删改历史事件。

同一模型步骤中连续调用同名工具时，终端只显示一次该工具的执行提示；每次调用、结果与 Session 事件仍分别保留。

同一模型步骤中相邻的 `task` 会组成并行组；非 `task` 工具是串行屏障，父 Agent 会等待整组结束并按原 Tool Call 顺序提交结果。每次根运行最多启动 4 个 Subagent。Subagent 共用当前 OpenAI 兼容 client，因此外部自定义 client 必须支持并发 `chat.completions.create()`。`Ctrl+C` 会协作取消后续步骤，但不能强杀已经进入的同步 HTTP 请求。

## 当前能力与边界

| 范围 | 当前能力 | 边界 |
| --- | --- | --- |
| 运行方式 | 支持工作区选择、持久 REPL 和一次性 CLI 任务。 | 它是本地单用户 Harness，没有 Web 服务或多用户权限系统。 |
| 模型协议 | 调用 OpenAI 兼容的 Chat Completions 和 `tool_calls`，通过通用本地估算和有效的 provider `usage` 校准管理 token 预算。 | 仅统计文本消息和工具定义；第三方网关不提供模型窗口时需要手动配置。 |
| 文件与 Git | 支持列出、读取、搜索、写入和编辑文件，以及查看 Git 状态和差异。 | 文件工具只能访问工作区；删除需要审批；路径、符号链接、敏感文件名和大小受到限制。 |
| 命令执行 | 显式启用后，可以在断网 Docker 容器中运行单个 argv 命令。 | `run_command` 默认关闭；普通命令逐次审批，固定检查可明确批准阶段内重跑。容器仍可修改工作区，也不能防御恶意镜像、Docker daemon 入侵或运行时漏洞。 |
| 交付验收 | 结合真实命令结果、工作版本和改动自查判断阶段完成，失败后在剩余步骤内修复重测。 | 依赖用户确认的范围、检查清单与可用执行环境；不保证模型能修复所有错误或覆盖所有业务要求。 |
| 上下文 | 支持工具结果裁剪、结构化压缩、主动压缩和超限后的单次重试；终端只显示上下文占上限的百分比。 | 压缩摘要属于当前 Session，不是跨 Session 长期 Memory。 |
| Session | 使用 v3 JSONL 事件日志自动保存交互记录，支持安全审计、已完成 turn fork、重命名、恢复和安全闭合中断工具批次。 | 单文件上限 5 MiB；达到上限后需新建或 fork Session。已开始工具的副作用未知时，使用者必须检查当前状态，Harness 不会自动重放。 |
| Skills | 共用 Codex 顶层用户 Skill 库，支持显式 sticky 加载和根 Agent 的渐进式只读加载。 | 不扫描 `.system`、插件或项目 Skill；不执行脚本；Subagent 不接收 Skills。 |
| Subagent | 相邻 `task` 默认并行；每次根 Agent 运行最多启动 4 个单层、只读子 Agent，每个最多使用 8 次模型请求。 | 子 Agent 使用独立上下文且不接收 Skills，不能写入、执行命令或递归委派，也不会持久化。普通工具仍串行。 |
| Hooks | Python API 支持 `UserPromptSubmit`、`PreToolUse`、`PostToolUse` 和 `Stop`。 | Hooks 只支持同步、进程内注册；没有配置文件、外部命令、异步回调或自动发现。 |
| 尚未实现 | 项目当前不包含 MCP、Goal Loop、长期 Memory、递归 Subagent 或子 Agent 持久化。 | 不要把 README 中的边界当作未来版本承诺。 |
