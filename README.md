# Gum Code

一个面向个人日常使用和 Agent 运行机制学习的本地 CLI 编程 Agent Harness。

它使用 OpenAI 兼容的 `tool_calls` 消息协议，由 Harness 负责工具执行、终端审批、Todo、上下文压缩、只读 Subagent、Agent step 预算和 Session 恢复。

## 功能

- 支持文件读写、搜索、Git 状态检查和可选的 Docker 隔离命令执行。
- 读取、`write_file` 和 `edit_file` 直接允许；删除仍需审批；命令执行默认不向模型开放。
- 支持 Todo 跟踪和可配置的 Agent step 上限；Todo 数量与单项长度有硬上限。
- 支持工具结果裁剪、历史摘要、模型主动 `compact` 和上下文超限后的单次压缩重试。
- 使用固定版本的 DeepSeek V4 官方 tokenizer 和消息编码器计算请求 token，不再用字符数估算上下文。
- 支持工作区选择、持久 REPL、自动 Session 保存和中断工具批次的安全恢复。
- 支持 `UserPromptSubmit`、`PreToolUse`、`PostToolUse` 和 `Stop` 四类进程内 Hooks。
- 默认开放同步、单层、只读 `task` Subagent，用独立上下文返回有界 JSON 调查报告。

## 快速开始

### 环境要求

- Python 3.11 或更高版本。
- 可调用 OpenAI 兼容 Chat Completions 和 Tool Calling 的模型服务。
- 只读/文件工具不需要 Docker；使用 `run_command` 时需要已启动的
  Docker Desktop/Engine 和一个已存在于本地的可信镜像。

### 首次安装

源码保留在当前项目，虚拟环境放在非 iCloud 目录：

```bash
cd "/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/Gum-Code"
python3.11 -m venv /Users/gumishdo/.local/share/gum-code/venv
/Users/gumishdo/.local/share/gum-code/venv/bin/python -m pip install .
```

首次运行会从 `deepseek-ai/DeepSeek-V4-Flash` 的固定 revision 下载
`tokenizer.json` 并交由 Hugging Face `tokenizers` 缓存。运行时不会执行远程
Python 代码。无网络环境可以预先获取官方 `tokenizer.json`，然后通过
`--tokenizer-file` 显式指定；文件哈希不匹配时 Harness 会在首次模型请求前拒绝启动。

### 配置

```bash
cd "/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/Gum-Code"
cp -n .env.example .env
```

编辑 `.env`，配置下列公开配置名。不要将真实密钥写入源码、测试、文档或提交记录。

| 变量 | 用途 | 必需 |
| --- | --- | --- |
| `LLM_API_KEY` | 模型服务 API Key | 是 |
| `LLM_BASE_URL` | 当前模型服务的 OpenAI 兼容 API 地址 | 是 |
| `LLM_MODEL` | 当前服务支持的模型名称 | 是 |

Gum Code 按键独立合并配置：进程环境变量 >
`~/.config/gum-code/.env` > 启动目录的旧 `.env`。TTY 中缺少配置时，
首次向导会隐藏输入 API Key，用一次最小模型请求验证成功后才会
原子写入 mode `0600` 的用户配置。

首次真正启动时，Gum Code 会将旧的
`~/.config/gu-harness/` 和 `~/.local/share/gu-harness/` 中受支持的
配置、状态和 Session 复制到新目录。迁移不覆盖新文件，不删除旧数据，
且不跟随符号链接；完成标记防止用户之后删除的 Session 被再次复制。
仅当旧工作区路径不存在且同级 `Gum-Code` 新路径存在时，才会改写
从 `vibe-harness` 到 `Gum-Code` 的那一个路径分量。

### 第一个只读任务

```bash
cd "/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/Gum-Code"
mkdir -p /tmp/gum-code-first-run
/Users/gumishdo/.local/share/gum-code/venv/bin/gum-code \
  --workspace /tmp/gum-code-first-run \
  "只读检查工作区的文件结构，不要修改任何内容。"
```

`--workspace` 必须指向已存在的目录。Harness 中的文件工具不允许越出该目录。

## 日常使用

### 交互式工作区与 REPL

TTY 中直接执行 `gum-code` 会打开工作区选择器；也可直接指定已存在
的目录。首次访问已存在目录时必须确认信任。选择器只列出最近路径和
当前浏览目录的直接子目录，不会扫描 Desktop、Git 仓库或其他项目根。

```bash
/Users/gumishdo/.local/share/gum-code/venv/bin/gum-code
/Users/gumishdo/.local/share/gum-code/venv/bin/gum-code \
  --workspace "/absolute/path/to/workspace"
```

REPL 支持 `/help`、`/workspace`、`/new`、`/resume`、`/sessions`、
`/status`、`/verbose` 和 `/exit`。`//text` 会将 `/text` 作为普通任务发送；
未知斜杠命令不会进入模型。Enter 提交，Escape 后按 Enter（部分终端为
Alt/Meta+Enter）插入换行，Up/Down 仅浏览当前进程内的输入历史。
Command+Enter 没有可移植的终端键码；iTerm2 或 VS Code Terminal 用户可将它
映射为 Escape+Enter：

- iTerm2：打开 Settings > Profiles > Keys > Key Mappings，新增
  `Command+Enter`，Action 选 `Send Hex Codes`，值填 `0x1b 0x0d`。
- VS Code：打开 Keyboard Shortcuts (JSON)，添加
  `{"key":"cmd+enter","command":"workbench.action.terminal.sendSequence",`
  `"when":"terminalFocus","args":{"text":"\u001b\r"}}`。

Ctrl+C 取消编辑或中断当前轮，Ctrl+D 正常退出。

自动 Session 位于 `~/.local/share/gum-code/sessions/`，在第一条任务提交
前不创建空文件。同一 Session 由进程生命期独占锁保护；不同 Session 可并行打开。
`/sessions` 可查看、恢复、重命名、确认后删除，也可显式复制导入旧 JSON；
撤销工作区信任或从最近列表移除都不会删除真实目录或 Session。
交互恢复 v1 Session 时也会显示源和目标，确认复制为 v2 后才继续；
源文件保持不变。one-shot 不能完成这次交互确认，因此会拒绝直接恢复 v1。

### 执行任务

```bash
/Users/gumishdo/.local/share/gum-code/venv/bin/gum-code \
  --workspace "/absolute/path/to/workspace" \
  "检查当前项目并完成任务。"
```

`write_file` 和 `edit_file` 会在路径、符号链接、敏感文件、大小和 Hook
检查通过后直接执行。`delete_file` 和已显式开启的 `run_command`
仍会显示工具名、原因和限长参数预览；只有输入 `y` 或 `yes`
（忽略大小写和首尾空白）才允许。删除成功后立即显示一条仅包含相对路径的回执。

`run_command` 默认不会出现在模型可见的工具中。先自行准备一个本地
可信镜像（Harness 不会自动 pull 或 build），再在当前运行显式开启：

```bash
/Users/gumishdo/.local/share/gum-code/venv/bin/gum-code \
  --workspace "/absolute/path/to/workspace" \
  --allow-run-command \
  --sandbox-image "your-trusted-local-image:tag" \
  "运行项目测试。"
```

启动时 Harness 会确认 Docker daemon 和镜像已就绪，并将 tag 解析为当前本地
的不可变 image ID。每次获批命令都在新容器中运行，固定策略为：

- `--network=none`，根文件系统只读，`/tmp` 为 64 MiB 临时可写空间。
- 仅将 workspace 读写挂载到 `/workspace`；不挂载宿主 HOME、Docker socket、
  凭据目录、设备或其他用户路径。
- 已存在的 `.env` 变体、`.git`、`.venv`、`.ssh`、`.aws`、密钥文件和
  Harness 内部产物会被空的只读挂载遮蔽；`.env.example` 等安全模板仍可见。
- 删除 Linux capabilities、禁止提权，以当前数字 UID/GID 运行，限制为
  1 CPU、512 MiB 内存、128 进程和 256 文件描述符。
- 超时、输出超过采集配额或中断时，Harness 会按随机精确容器名强制清理。

镜像必须自带命令所需的运行时和依赖。由于断网且 `.venv` 被遮蔽，不能依赖
宿主虚拟环境或在命令中临时下载包。恢复 Session 时也必须重新传入这两个开关。

开启后每次命令仍需终端审批。容器可以修改或删除 workspace 内未遮蔽的普通文件，
也无法防御恶意镜像、被入侵的 Docker daemon 或容器运行时漏洞，因此这不是绝对隔离。
明显的直接高危命令（例如 `sudo`、关机/重启、磁盘格式化和 `rm -rf /`）会在审批前硬拒绝，不能通过输入 `y` 强制放行。该规则只是纵深防御，不能识别所有包装器或解释器绕过。
`run_command` 一次只运行一个程序。`&&`、`||`、管道、分号和重定向会在审批前返回明确错误；Harness 不会通过开启 Shell 来执行它们。

### 只读 Subagent

根 Agent 可在需要多步调查时自主调用 `task`。子 Agent 只获得当前任务描述和只读工具，不继承父对话，不能写入、删除、执行命令、再委派或并行运行。简单查询应由根 Agent 直接使用只读工具。

每次根运行最多启动 4 个子 Agent；每个子 Agent 最多发起 8 次模型请求，包括压缩摘要请求。子 Agent 只能用 `submit_report` 单独提交报告，成功结果为最多 20,000 字符的 `status/summary/findings/evidence/risks` JSON。证据校验只确认工作区内文件和行号存在，不证明自然语言断言真实。

如需在本次运行禁用委派：

```bash
/Users/gumishdo/.local/share/gum-code/venv/bin/gum-code \
  --workspace "/absolute/path/to/workspace" \
  --no-subagent \
  "直接检查这个文件。"
```

子 Agent 的上下文转存使用工作区外的临时目录并在结束时清理。共享 Git
读工具使用隔离环境并禁用仓库 fsmonitor/Hook 与可选索引刷新，避免 READ
调用继承模型凭据、执行仓库配置代码或改写索引。这个只读承诺不包括用户
自行注册的可产生副作用的 Python Hook。

真实网关验收用例位于 `tests/test_subagent_gateway_integration.py`，仅在调用方
显式设置 `GUM_CODE_SUBAGENT_E2E=1` 时运行，并通过项目现有的
`load_settings()` 加载 `.env` 中的三个 `LLM_*` 配置，不会输出凭据。

```bash
GUM_CODE_SUBAGENT_E2E=1 PYTHONPATH=src python -m pytest -q \
  tests/test_subagent_gateway_integration.py
```

### 保存与恢复 Session

先准备一个非工作区的 Session 目录：

```bash
mkdir -p /Users/gumishdo/.local/share/gum-code/sessions
```

启动任务并保存 Session：

```bash
/Users/gumishdo/.local/share/gum-code/venv/bin/gum-code \
  --workspace "/absolute/path/to/workspace" \
  --session-file /Users/gumishdo/.local/share/gum-code/sessions/daily.json \
  "只读分析这个项目。"
```

使用相同的 workspace 和模型继续任务：

```bash
/Users/gumishdo/.local/share/gum-code/venv/bin/gum-code \
  --workspace "/absolute/path/to/workspace" \
  --resume-session /Users/gumishdo/.local/share/gum-code/sessions/daily.json \
  "继续上一次的分析。"
```

`--session-file` 和 `--resume-session` 不能同时使用。恢复时，Session 中的 workspace 和模型必须与当前运行一致。
Thinking mode 下的 assistant `reasoning_content` 会随消息保存和恢复，以满足 DeepSeek 在携带 tools 的后续请求中完整回传该字段的协议要求；它不会在普通终端日志中输出。

工具批次在进程中断后按下列规则处理：

- 已完成的工具保留真实结果。
- 尚未开始的 pending 工具不会被直接重放；Harness 会让模型根据当前状态重新规划，新调用会重新经过当前权限策略和安全检查。
- 已开始但副作用未知的 in-flight 工具会拒绝自动恢复，不会覆盖原 Session。
- in-flight/pending `task` 会被补成 `task_interrupted` 结果并交给根 Agent 重新规划；旧子 Agent 不会自动重放。

REPL 内的实时 Ctrl+C 中断会将每个原始 tool call 闭合一次：已完成结果保留，
in-flight 记为副作用未知，pending 记为未执行，两者都不自动重放。如果进程崩溃后
Session 只余一条 user 尾部，交互恢复会先显示有界预览并要求明确选择重发或丢弃；
one-shot 的新任务一律优先，不会静默重发旧输入。

### 调整运行预算

```bash
/Users/gumishdo/.local/share/gum-code/venv/bin/gum-code \
  --workspace "/absolute/path/to/workspace" \
  --max-steps 16 \
  --max-context-tokens 800000 \
  "分析这个复杂任务，先维护 Todo，再逐步执行。"
```

- `--max-steps` 限制一次运行允许的正常工具型模型步数，默认为 `16`。步数耗尽后，Harness 会额外发起一次「禁止调用工具」的阶段总结请求；上下文压缩和超限重试的模型请求不计入这个上限。
- `--max-context-tokens` 默认为 `800000`，且不能超过
  DeepSeek-V4-Flash 的 `1000000` token 模型上限。历史摘要输入最多使用
  `640000` tokens；计数仍包含实际启用的工具 Schema。
- 旧参数 `--max-context-chars` 已移除，不会把旧值静默解释成 token。迁移时请显式改用 `--max-context-tokens` 并重新确认预算值；同样的数字并不代表同样的容量。
- 无网络或需要完全可复现时，加上 `--tokenizer-file /absolute/path/tokenizer.json`。
- Subagent 的 4 次委派与 8 次子请求为固定安全/成本上限，当前不提供 CLI 调节。

终端会在网关返回 `usage` 时显示服务端 prompt token 与本地预检值。
`1M` 是官方模型能力，不证明第三方网关接受同样的上限；网关拒绝会直接显示为错误，
Gum Code 不会改用字符估算或静默降低预算。

### 程序化 Hooks

Hooks 通过 Python API 注册，本版本没有 Hook 配置文件或外部命令加载器：

```python
from gum_code.agent import run_agent
from gum_code.hooks import HookEvent, HookRegistry

hooks = HookRegistry()


def protect_generated_files(
    tool_name: str,
    arguments: dict[str, object],
) -> str | None:
    if tool_name == "delete_file" and arguments.get("path") == "generated.txt":
        return "generated.txt must be kept"
    return None


hooks.register_hook(HookEvent.PRE_TOOL_USE, protect_generated_files)

answer = run_agent(
    client=client,
    model=model,
    workspace=workspace,
    user_query=query,
    approval_callback=approval_callback,
    hook_registry=hooks,
)
```

四个事件的参数和控制语义：

| 事件 | 回调参数 | 返回值作用 |
| --- | --- | --- |
| `UserPromptSubmit` | `query` | Agent 不使用返回值；用于观察当前请求 |
| `PreToolUse` | `tool_name, arguments` | 非 `None` 阻止当前 registry 工具或根 `task` |
| `PostToolUse` | `tool_name, arguments, result` | Agent 不使用返回值；在 Handler/子 Agent 尝试后观察结果 |
| `Stop` | `messages` | 非空字符串作为新 user message，要求进入下一轮 |

同一事件的回调按注册顺序执行，第一个非 `None` 返回值会停止该事件的后续回调。传给 Hook 的可变参数是防御性副本，不能靠修改它们改变实际执行或历史。

`PreToolUse` 只是一道额外否决：硬拒绝、风险策略、无副作用 preflight 和策略要求时的终端审批仍由 Harness 强制执行，Hook 不能放宽或绕过它们。`Stop` 也不能突破 `max_steps`。Hook 是可信的进程内扩展代码，不是沙箱；回调不应打印或返回密钥、Token、Cookie、完整工具输出等敏感信息。

## 源码更新与测试

源码修改验收后，需将新版本重新安装到外部虚拟环境：

```bash
cd "/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/Gum-Code"
/Users/gumishdo/.local/share/gum-code/venv/bin/python \
  -m pip install --force-reinstall --no-deps .
```

从当前源码运行完整测试：

```bash
cd "/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/Gum-Code"
PYTHONPATH=src \
PYTHONPYCACHEPREFIX=/Users/gumishdo/.local/share/gum-code/pycache \
/Users/gumishdo/.local/share/gum-code/venv/bin/python -m pytest -q -p no:cacheprovider
```

## 已知边界

- 当前 tokenizer 路径只支持精确模型名 `deepseek-v4-flash`；其他模型或网关别名会明确失败，不会回退到字符估算。
- 本地计数基于固定的 DeepSeek V4 官方 tokenizer/编码器；第三方网关最终处理的 token 以响应 `usage` 为准。
- in-flight 工具的副作用未知时必须由使用者核对，Harness 不会自动重放。
- 压缩摘要是 Session 内的可恢复状态，不是跨 Session 长期 Memory。
- Hooks 当前只支持同步、进程内 Python 注册；尚无配置文件、外部命令 Hook、异步 Hook 或自动发现机制。
- 当前未实现 Skills、MCP、Goal Loop、并行/递归 Subagent 或子 Agent 持久化。
- `run_command` 默认关闭；显式开启需本地可信镜像和正常的 Docker daemon。
- Docker Sandbox 保护 workspace 以外的宿主边界，但获批命令仍可修改未遮蔽的
  workspace 文件；普通文件名中自行保存的密钥也无法被自动识别。
- workspace 是宿主读写 bind mount，Docker 的内存/CPU/PID 限额不会限制它占用的
  宿主磁盘空间；不要批准会无界生成文件的命令。
