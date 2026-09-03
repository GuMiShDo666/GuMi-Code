# Vibe Harness

一个面向个人日常使用和 Agent 运行机制学习的本地 CLI 编程 Agent Harness。

它使用 OpenAI 兼容的 `tool_calls` 消息协议，由 Harness 负责工具执行、终端审批、Todo、上下文压缩、Agent step 预算和 Session 恢复。

## 功能

- 支持文件读写、搜索、Git 状态检查和可选的受控命令执行。
- 读操作直接允许；写入和删除需终端审批；命令执行默认不向模型开放。
- 支持 Todo 跟踪和可配置的 Agent step 上限；Todo 数量与单项长度有硬上限。
- 支持工具结果裁剪、历史摘要、模型主动 `compact` 和上下文超限后的单次压缩重试。
- 支持 Session 保存、正常续接和中断工具批次的安全恢复。
- 支持 `UserPromptSubmit`、`PreToolUse`、`PostToolUse` 和 `Stop` 四类进程内 Hooks。

## 快速开始

### 环境要求

- Python 3.11 或更高版本。
- 可调用 OpenAI 兼容 Chat Completions 和 Tool Calling 的模型服务。

### 首次安装

源码保留在当前项目，虚拟环境放在非 iCloud 目录：

```bash
cd "/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/vibe-harness"
python3.11 -m venv /Users/gumishdo/.local/share/vibe-harness/venv
/Users/gumishdo/.local/share/vibe-harness/venv/bin/python -m pip install .
```

### 配置

```bash
cd "/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/vibe-harness"
cp -n .env.example .env
```

编辑 `.env`，配置下列公开配置名。不要将真实密钥写入源码、测试、文档或提交记录。

| 变量 | 用途 | 必需 |
| --- | --- | --- |
| `LLM_API_KEY` | 模型服务 API Key | 是 |
| `LLM_BASE_URL` | 当前模型服务的 OpenAI 兼容 API 地址 | 是 |
| `LLM_MODEL` | 当前服务支持的模型名称 | 是 |

Harness 只从启动命令的当前工作目录加载 `.env`。日常使用时应先进入本项目根目录，再执行 `vibe-harness`。

### 第一个只读任务

```bash
cd "/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/vibe-harness"
mkdir -p /tmp/vibe-harness-first-run
/Users/gumishdo/.local/share/vibe-harness/venv/bin/vibe-harness \
  --workspace /tmp/vibe-harness-first-run \
  "只读检查工作区的文件结构，不要修改任何内容。"
```

`--workspace` 必须指向已存在的目录。Harness 中的文件工具不允许越出该目录。

## 日常使用

### 执行任务

```bash
/Users/gumishdo/.local/share/vibe-harness/venv/bin/vibe-harness \
  --workspace "/absolute/path/to/workspace" \
  "检查当前项目并完成任务。"
```

当模型请求执行写入、编辑、删除或命令工具时，终端会显示工具名、原因和参数预览。只有输入 `y` 或 `yes`（忽略大小写和首尾空白）才会允许执行，其他输入默认拒绝。
普通运行日志只显示上下文情况、当前步骤与工具名、工具错误和最终回答；不重复输出普通工具参数和成功结果。权限审批中的限长参数预览不受影响。

`run_command` 默认不会出现在模型可见的工具中。只有当前运行明确需要命令执行时才开启：

```bash
/Users/gumishdo/.local/share/vibe-harness/venv/bin/vibe-harness \
  --workspace "/absolute/path/to/workspace" \
  --allow-run-command \
  "运行项目测试。"
```

开启后每次命令仍需终端审批。该选项不提供 OS 或容器级沙箱，不应对不可信项目或任务开启。
明显的直接高危命令（例如 `sudo`、关机/重启、磁盘格式化和 `rm -rf /`）会在审批前硬拒绝，不能通过输入 `y` 强制放行。该规则只是纵深防御，不能识别所有包装器或解释器绕过。
`run_command` 一次只运行一个程序。`&&`、`||`、管道、分号和重定向会在审批前返回明确错误；Harness 不会通过开启 Shell 来执行它们。

### 保存与恢复 Session

先准备一个非工作区的 Session 目录：

```bash
mkdir -p /Users/gumishdo/.local/share/vibe-harness/sessions
```

启动任务并保存 Session：

```bash
/Users/gumishdo/.local/share/vibe-harness/venv/bin/vibe-harness \
  --workspace "/absolute/path/to/workspace" \
  --session-file /Users/gumishdo/.local/share/vibe-harness/sessions/daily.json \
  "只读分析这个项目。"
```

使用相同的 workspace 和模型继续任务：

```bash
/Users/gumishdo/.local/share/vibe-harness/venv/bin/vibe-harness \
  --workspace "/absolute/path/to/workspace" \
  --resume-session /Users/gumishdo/.local/share/vibe-harness/sessions/daily.json \
  "继续上一次的分析。"
```

`--session-file` 和 `--resume-session` 不能同时使用。恢复时，Session 中的 workspace 和模型必须与当前运行一致。

工具批次在进程中断后按下列规则处理：

- 已完成的工具保留真实结果。
- 尚未开始的 pending 工具不会被直接重放；Harness 会让模型根据当前状态重新规划，新的可变更操作仍需审批。
- 已开始但副作用未知的 in-flight 工具会拒绝自动恢复，不会覆盖原 Session。

### 调整运行预算

```bash
/Users/gumishdo/.local/share/vibe-harness/venv/bin/vibe-harness \
  --workspace "/absolute/path/to/workspace" \
  --max-steps 16 \
  --max-context-chars 100000 \
  "分析这个复杂任务，先维护 Todo，再逐步执行。"
```

- `--max-steps` 限制一次运行允许的正常工具型模型步数，默认为 `16`。步数耗尽后，Harness 会额外发起一次「禁止调用工具」的阶段总结请求；上下文压缩和超限重试的模型请求不计入这个上限。
- `--max-context-chars` 限制模型请求的估算字符数，默认为 `100000`。它是 token 的近似预算；超限时 Harness 会尝试分层压缩上下文。

### 程序化 Hooks

Hooks 通过 Python API 注册，本版本没有 Hook 配置文件或外部命令加载器：

```python
from vibe_harness.agent import run_agent
from vibe_harness.hooks import HookEvent, HookRegistry

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
| `PreToolUse` | `tool_name, arguments` | 非 `None` 阻止当前 registry 工具 |
| `PostToolUse` | `tool_name, arguments, result` | Agent 不使用返回值；在 Handler 尝试后观察结果 |
| `Stop` | `messages` | 非空字符串作为新 user message，要求进入下一轮 |

同一事件的回调按注册顺序执行，第一个非 `None` 返回值会停止该事件的后续回调。传给 Hook 的可变参数是防御性副本，不能靠修改它们改变实际执行或历史。

`PreToolUse` 只是一道额外否决：硬拒绝、风险策略、无副作用 preflight 和终端审批仍由 Harness 强制执行，Hook 不能放宽或绕过它们。`Stop` 也不能突破 `max_steps`。Hook 是可信的进程内扩展代码，不是沙箱；回调不应打印或返回密钥、Token、Cookie、完整工具输出等敏感信息。

## 源码更新与测试

源码修改验收后，需将新版本重新安装到外部虚拟环境：

```bash
cd "/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/vibe-harness"
/Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pip install --force-reinstall --no-deps .
```

从当前源码运行完整测试：

```bash
cd "/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/vibe-harness"
PYTHONPATH=src \
PYTHONPYCACHEPREFIX=/Users/gumishdo/.local/share/vibe-harness/pycache \
/Users/gumishdo/.local/share/vibe-harness/venv/bin/python -m pytest -q -p no:cacheprovider
```

## 已知边界

- 上下文预算使用字符数估算，不是精确 tokenizer。
- in-flight 工具的副作用未知时必须由使用者核对，Harness 不会自动重放。
- 压缩摘要是 Session 内的可恢复状态，不是跨 Session 长期 Memory。
- Hooks 当前只支持同步、进程内 Python 注册；尚无配置文件、外部命令 Hook、异步 Hook 或自动发现机制。
- 当前未实现 Skills、MCP、Subagents 或 Goal Loop。
- `run_command` 默认关闭；显式开启后仍只有 Harness 层审批和参数预检，没有 OS 或网络沙箱。
