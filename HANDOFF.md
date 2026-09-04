# Gum Code 开发交接文档

> 最后更新：2026-09-04
> 当前实现检查点：Gum Code 运行时策略与 Agent Loop 真实进度反馈
> 当前源码验证：见本文第 7 节的最新验证记录
> 当前下一步：审查并发布 `feat/agent-loop-progress-feedback`

## 1. 新对话如何续接

在新对话中直接输入：

```text
请先完整阅读
/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/Gum-Code/HANDOFF.md
然后检查当前代码和测试。Gate 0、s03 Permission、终端精简显示、Todo 单项上限、Shell 控制符/重定向报错和一次性隔离 HOME 已通过验收。
当前已按 s04 思路加入 `UserPromptSubmit`、`PreToolUse`、`PostToolUse` 和 `Stop`；Hooks 只能追加否决，不能取代或绕过 Permission。
当前上下文预算已从字符估算迁移到固定版本的 DeepSeek V4 官方 tokenizer/消息编码器；本地数值用于请求前决策，第三方网关 `usage.prompt_tokens` 是请求后权威数值。
`run_command` 默认隐藏；显式开启时必须提供本地可信镜像，命令只能在断网、受限、敏感路径遮蔽的 Docker 容器中执行，失败不回退宿主。
当前已新增默认开放的 `task` Subagent：仅做同步、单层、只读调查，父 Session 只保存最终结构化结果；可用 `--no-subagent` 在本轮关闭。
一次只推进一个边界明确的步骤，先讲清目的和运行链路，再让我实现，完成后帮我验收。
每次我输入“已完成”，验证通过后直接进入下一步；发现当前步骤直接相关的问题可以做最小修复并补回归测试。
权限架构继续对照 shareAI-lab/learn-claude-code 的 s03_permission，但不机械复制它的教学性宽松规则；必须适配本项目的 OpenAI tool_calls 消息协议和更严格安全边界。
```

## 2. 用户目标和协作方式

GuMiShDo 正在从零编写一个可长期使用的本地 CLI Vibe Coding Agent Harness。两个目标同等重要：

1. 通过亲手实现理解 Agent 的完整运行机制。
2. 最终得到可真实使用、有权限边界、可恢复会话的本地编程 Agent。

后续协作必须遵守：

- 一次只讲和实现一个边界明确的概念组。
- 先讲目的、直觉和运行链路，再给出文件与测试任务。
- GuMiShDo 亲手编写为主；他说“已完成”后，必须检查真实源码并运行测试，不能只接受口头完成。
- 验证通过后直接进入下一步，不再等待“继续”。
- 发现与当前任务直接相关的缺陷时，可以直接做最小修复并增加回归测试。
- 不要大范围重构、顺手改无关代码、提前引入依赖或复杂框架。
- 这是教学性 Harness，不应声称为 Claude Code 或其他商业 Agent 的内部源码。
- GuMiShDo 是初学者，但不希望助手迎合；如果他提出的安全设计不成立，必须用专业视角明确指出，给出证据和更稳妥方案。
- 不得把危险命令字符串黑名单宣称为任意代码执行的完整安全边界。

## 3. 项目环境与安全约束

项目根目录（源码仍在当前项目，未移动）：

```text
/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/Gum-Code
```

环境：

- Python 3.11
- OpenAI Python SDK，使用 OpenAI 兼容网关
- 目标虚拟环境为 `/Users/gumishdo/.local/share/gum-code/venv`（非 editable 安装）
- 打包仅声明 `gum-code = "gum_code.cli:main"`；外部安装由发布阶段验证

安全约束：

- 敏感配置在 `.env`；绝对不得读取、输出、复述或提交其内容。
- `.env.example` 只能包含占位符和公开配置名。
- 不要在项目源码目录中做会修改文件的真实 E2E；应使用独立临时 workspace。
- GitHub 仓库为私有仓库 `GuMiShDo666/Gum-Code`；功能更新使用分支、测试、Conventional Commit、push 和 PR，不直接提交到 `main`。
- 当前工作分支为 `feat/agent-loop-progress-feedback`。

常用验证命令（源码测试继续使用外部 Python，并设置 `PYTHONPATH=src`）：

```bash
cd "/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/Gum-Code"
PYTHONPATH=src /Users/gumishdo/.local/share/gum-code/venv/bin/python -m pytest -q -p no:cacheprovider
```

正式 CLI（使用虚拟环境中的 `gum-code`，不再要求 `PYTHONPATH=src python -m gum_code`）：

```bash
/Users/gumishdo/.local/share/gum-code/venv/bin/gum-code \
  --workspace /absolute/workspace/path \
  [--max-steps N] \
  [--max-context-tokens N] \
  [--tokenizer-file /absolute/path/tokenizer.json] \
  [--no-subagent] \
  [--allow-run-command --sandbox-image local-trusted-image] \
  [--session-file /path/session.json | --resume-session /path/session.json] \
  "任务描述"
```

## 4. 核心心智模型与协议不变量

```text
Agent 产品 = Model + Harness
```

- Model 根据 messages 和 Tool Schema 决定下一步。
- Harness 负责调用模型、执行工具、追加 Tool Result、循环、限步、权限、会话和上下文管理。
- Schema 是给模型看的工具接口；Handler 是 Harness 中真正执行操作的函数。
- “模型能否看到工具”和“Harness 是否允许执行”是两个独立安全层。
- Assistant 的 tool call 必须与带相同 `tool_call_id` 的 tool message 成对追加。
- 不得在一批多工具调用的中间删除、压缩或替换消息，否则会留下孤立的 tool result。
- 对模型可见的任何摘要和历史都视为不可信数据；当前用户请求必须保持最高优先级。

## 5. 当前代码结构

```text
src/gum_code/
├── __main__.py              # python -m gum_code 入口
├── agent.py                 # Agent Loop、工具批次、Todo reminder、in-flight marker 快照
├── cli.py                   # TTY/one-shot 路由、显式 Session 和错误输出
├── interactive.py           # 工作区/Session 选择、REPL、配置向导和 Rich UI
├── user_state.py            # XDG 信任/最近状态和自动 Session 目录
├── config.py                # 逐键配置优先级、私有写入与 provider 验证
├── context.py               # token 预算、分层压缩和 transcript
├── token_counting.py        # DeepSeek V4 请求编码、token 计数和固定版本加载
├── _vendor/
│   └── encoding_dsv4.py    # 固定的官方 DeepSeek V4 消息编码器快照
├── events.py                # 终端事件渲染
├── hooks.py                 # 四事件 HookRegistry（进程内同步回调）
├── session.py               # Session v2、进程锁、原子快照与中断恢复
├── subagent.py              # task 契约、只读子循环、报告校验与请求预算
├── todo.py                  # TodoManager（MAX_TODOS=20，全量校验原子替换）
├── permissions/
│   ├── policy.py            # 工具风险和 ALLOW/ASK/DENY
│   └── terminal.py          # 终端 y/N 审批
└── tools/
    ├── common.py             # 路径、敏感文件和输出边界
    ├── filesystem.py         # list/read/write/edit/delete
    ├── search.py             # find_files/search_text
    ├── git.py                # git_status/git_diff
    ├── command.py            # Docker Sandbox 配置、遮蔽、有界执行与 run_command
    └── registry.py           # Schema、Handler、Risk、调度和审批

tests/
├── test_agent.py
├── test_cli.py
├── test_config.py
├── test_context.py
├── test_events.py
├── test_hooks.py
├── test_session.py
├── test_todo.py
├── test_packaging.py
├── test_terminal_permission.py
├── test_permission_policy.py
├── test_tool_registry.py
└── test_*_tools.py / test_tool_*.py
```

## 6. 已完成的能力

### 6.1 工具、权限与终端审批

最后已验证稳定基线注册并向模型开放 11 个工具：

| 工具 | 风险 | 执行策略 |
| --- | --- | --- |
| `list_files` / `read_file` / `find_files` / `search_text` / `git_status` / `git_diff` / `read_tool_output` | READ | 直接允许 |
| `write_file` / `edit_file` | WRITE | 通过安全检查后直接执行 |
| `delete_file` | DESTRUCTIVE | 终端审批 |
| `run_command` | EXECUTE | Docker Sandbox 预检 + 终端审批 |

- `ALL_TOOL_RISKS` 是风险唯一数据源，Schema、Handler 和 Risk 的 key 一致性有测试保护。
- `MODEL_VISIBLE_TOOL_NAMES` 默认排除 `EXECUTION_TOOL_NAMES`；`--allow-run-command`
  必须与 `--sandbox-image` 同时提供，且只为当前运行动态加入
  `run_command` Schema。镜像必须已存在本地，启动时被锁定为 image ID，
  Harness 使用 `--pull=never`。
- 审批函数只有在去除空白、忽略大小写后等于 `y`/`yes` 才返回 True。
- 空输入、其他输入、EOF 和 `KeyboardInterrupt` 默认拒绝。
- 审批参数预览限长，不会无限刷屏。

已有工具边界：workspace 相对路径、越界/符号链接/敏感路径拒绝、安全 glob、
`rg --json` 结果二次过滤、原子写入。`run_command` 额外使用断网 Docker
容器、只读根文件系统、敏感路径空挂载、固定资源限制、有界流式输出和精确容器清理。

后续安全复验已确认：

- `.transcripts` 和 `.task_outputs` 对普通文件工具不可见/不可写；转存结果只能经 `read_tool_output` 分页读取。
- 写、编辑、删除和命令均在 Hook/副作用前运行无副作用 preflight；仅删除和命令进入终端审批。
- 工具错误保留可安全公开的具体原因，不再统一吞成 `Tool execution failed`。
- 显式 Shell 解释器、明显的敏感/绝对/逃逸路径命令参数会在 preflight 拒绝。
- 工具返回、文件、搜索结果、命令输出、transcript 和转存结果均被 system instruction 标记为不可信数据。
- 相同工具名 + 规范化后相同参数的调用被用户拒绝后，同一 `run_agent` 内不再重复弹审批；参数或工具改变时仍重新审批。
- 显式用 `ToolExecutionDenied` 字符串子类区分真实用户拒绝与“长得像拒绝”的普通工具输出。

权限系统下一参考是 `shareAI-lab/learn-claude-code/s03_permission`：工具执行前依次经过硬拒绝、规则匹配和用户审批。但 s03 的字符串黑名单是教学示例，其 README 明确说明不是完整安全边界；本项目不能因此将任意命令执行宣称为安全。

### 6.2 正式 CLI、事件与 E2E

- 正式 shell 入口为 `gum-code`；TTY 无任务时进入持久 REPL，位置参数 task 仍为 one-shot。
- workspace 必须是已存在目录。
- 交互模式在每轮请求准备时立即启动 Rich `dots` 动画：`准备中...`；模型请求前切换为 `思考中...`，历史摘要请求使用 `整理上下文中...`。Rich 自动检测 TTY，`NO_COLOR` 仍受尊重。
- one-shot 使用带 `第 N 步` 的静态行并立即 flush；不输出 ANSI 动画帧。主 Agent、Subagent、上下文错误重试和步数耗尽后的最终请求均覆盖。
- 交互模式的最终 Markdown 回答仅在渲染期间将行内代码设为 bold，前景与背景由终端决定；fenced code block、其他样式和 one-shot 保持不变。
- `tool_call` / `subagent_tool` 只保留为内部观测事件，不再误报正在执行。只有完成允许集、JSON、策略、preflight、`PreToolUse` 和必要审批后，才发送 `tool_execution` 并显示 `执行中：正在调用<工具名>`。Session in-flight 快照仍位于事件之前。
- 普通工具参数、成功结果和 reasoning 始终隐藏；进度负载只含有界 step 和经终端控制字符清理的工具名。
- CLI 统一捕获已知运行错误，写入 stderr 并返回 1。
- 写 stderr 错误前先 flush stdout，错误行不再因缓冲出现在已有日志之前。

删除/命令的权限审批仍显示限长参数预览。删除成功后立即显示一条经控制字符清理和限长的相对路径回执。

### 6.3 Session 保存与恢复

- Session v2 JSON，UTF-8 + `ensure_ascii=False`；读取时仍兼容 v1。
- 原子替换，Session 文件 mode 为 `0600`，5 MB 字节上限。
- 保存 workspace、model 和已序列化 messages。
- SDK assistant message 会转成纯 dict，保留 tool-call id/type/function/name/arguments。
- Thinking mode assistant message 的 `reasoning_content` 也会转成纯字符串并随 Session 保存/恢复。正常 Agent 请求始终带 tools，因此后续请求必须回传该字段，否则官方 DeepSeek thinking-mode tool-calling 协议会返回 400。
- 恢复时校验文件类型、符号链接、大小、UTF-8、JSON、版本、workspace、model 和消息结构。
- 恢复时深拷贝历史并追加当前新任务。

中断工具批次的安全恢复（最新完成）：

- `load_session(path, *, allow_incomplete_tool_batch=False)`：默认拒绝未完成批次；显式 True 才允许加载，并返回 `SessionData.tool_batch_state`。
- 纯函数 `analyze_tool_batch_tail()` 把尾部批次划分为 `completed` / `pending` / `in_flight`。
- 恢复语义：
  - `completed` → 结果保留（真实结果已在 Session 中）。
  - `pending` → 不直接重放，而是补一条“上次未执行，重新规划”的 tool result，让模型重新规划并重新走审批。
  - 只读 `in_flight` → 没有副作用，按被中断的 pending 项交给模型重新规划。
  - 写/删/执行 `in_flight` → 副作用结果未知，绝不自动重放；`plan_tool_batch_recovery()` 直接抛 `ValueError`，CLI 退出码 1，不调用 run_agent、不覆盖 Session。
  - 重新规划出的操作仍重新经过当前权限链；`write_file`/`edit_file` 通过安全检查后直接执行，删除和命令仍需终端审批。

### 6.4 上下文预算与可靠压缩

- 每次请求用固定版本 DeepSeek V4 tokenizer 和实际启用的工具 Schema 计数。消息数不再触发删除；只在达到配置预算、模型/用户显式请求或网关上下文错误时进行语义压缩。
- 正常管线为 `apply_tool_result_budget → apply_micro_compact → apply_fit_tool_results → compact_history`；每一层都在重算后才决定是否继续。
- `compact_history` 只接受完整协议批次，不拆分 assistant Tool Call 与对应 Tool Results。最新原始用户请求和未闭合批次始终保留，其他最近原文目标约占预算的 30%。
- 更早历史被编码为 v1 JSON 任务状态胶囊：目标、需求、禁止项、决策、代码状态、验证证据和待办。活跃条目用稳定 ID 跨多次压缩保留；仅新 user 原文可以作为显式替代来源。
- 胶囊响应必须通过严格字段、大小、ID、来源与活跃条目覆盖校验。首次无效时只修复一次；再失败则保留内存消息和旧 Session，不发送超预算主请求。
- 压缩前的完整 transcript 只是人工审计归档，Agent 不可读。胶囊和历史均是不可信参考，不能产生新授权。
- `reactive_compact()`、模型 `compact`、步数耗尽收尾和 REPL `/compact` 共用同一事务。网关上下文错误最多重试一次；这与胶囊校验的一次修复是两个独立上限。
- 可见上下文数据只显示相对配置上限的整数百分比，允许显示超过 100%，不显示本地或 provider 的具体 token 数。
- Session v1/v2 字段不变；旧自由文本 `[Compacted]` 仅在该 Session 下次成功压缩时懒迁移。Session 快照超过 5 MiB 时，`save_session()` 仅在深拷贝上将已完成的大 Tool Result 转存为私有文件；仍超限则不替换旧快照。

### 6.5 模型主动 `compact`

- 模型可见 `compact` 工具 Schema；`compact` 是 Agent Loop 控制信号，不加入 `tools/registry.py`、风险表或终端审批。
- 遇到 `compact` 不调用 `execute_tool()`，不请求审批；为它追加带正确 `tool_call_id` 的 tool result。
- 同一响应中的其他真实工具必须全部执行并追加结果；整个工具批次闭合后才执行 `compact_history`。
- 主动压缩后发送 `context_summary` 和 `context_compacted` 事件（stage 为 `active_compact`），不输出摘要正文。

### 6.6 Todo 系统

- `TodoManager`（`MAX_TODOS=20`、`MAX_TODO_CONTENT_CHARS=500`，全量校验原子替换）。
- `todo_write.content` 的 Schema `maxLength` 与运行时共享同一常量；按原始字符串长度检查后再 `strip()`。
- `todo_write` 工具是 Agent 控制工具，不走 `execute_tool`/审批。
- reminder 计数器：每 `TODO_REMINDER_INTERVAL=3` 批无 `todo_write` 时，在最后一个 tool result 后追加 reminder。
- `TODO_SYSTEM_INSTRUCTION` 注入 system message（新会话/恢复会话都注入，不重复，不修改调用者输入）。

### 6.7 CLI 参数

- `--max-steps`：默认 `MAX_STEPS=16`，`_positive_int` 校验，`run_agent` 自身也校验正整数并拒绝 bool。`max_steps` 限制的是正常工具型步骤；耗尽后 `_finalize_after_step_budget` 额外发起一次不带 tools 的阶段总结请求。上下文压缩和超限重试不计入这个上限。
- `--max-context-tokens`：默认 `DEFAULT_MAX_CONTEXT_TOKENS=800_000`，最大 `MODEL_MAX_CONTEXT_TOKENS=1_000_000`，透传 `run_agent(max_context_tokens=...)`。旧的 `--max-context-chars` 已移除，不做静默单位转换。
- `--tokenizer-file`：可选的本地官方 DeepSeek V4 `tokenizer.json`。未指定时从 `deepseek-ai/DeepSeek-V4-Flash` 的固定 revision 加载并缓存。

阶段总结安全边界：

- 最终请求不传 `tools`/`tool_choice`，响应中即使伪造 `tool_calls` 也不执行。
- 耗尽前已完成的工具结果用于生成“已完成/未完成”的最佳回答。
- 总结或压缩失败时不用不安全中间状态覆盖最后一个可恢复 Session 快照。
- 只有额外一次无工具总结请求受硬上限约束；摘要和上下文错误重试仍可能额外调用模型。

### 6.8 打包与 console script

- `pyproject.toml` 仅定义 console script `gum-code = "gum_code.cli:main"`；distribution 与 import 名为 `gum-code` / `gum_code`。
- 虚拟环境使用非 editable 安装；源码改动验收后需重新执行 `pip install --force-reinstall --no-deps .` 同步。
- `.gitignore` 加 `*.egg-info/`。
- `test_packaging.py` 用 `tomllib` 校验。

### 6.9 真实模型 E2E

- 已完成 Todo、Context、预算和自动压缩的真实模型端到端验证（在独立临时 workspace 进行）。
- 已完成 pending 工具重新规划与 in-flight 失败关闭的真实 CLI 端到端验证。
- 2026-09-03 已用 `deepseek-v4-flash` 通过第三方网关执行隔离 workspace 只读探针：模型正确返回 `TOKENIZER_E2E_OK`，本地预检为 1753 tokens，网关 `usage.prompt_tokens` 为 1834。该 81-token 差值证明 provider usage 必须保留为请求后权威对照，不应声称本地计数与第三方网关绝对相等。
- 同日又完成 thinking-mode 双请求工具链探针：首次请求调用 `list_files`，保留 `reasoning_content` 和 Tool Result 后再次请求，网关没有返回 400，最终回答「工作区为空」，隔离 workspace 无文件变更。
- 不应把这些当成可永久重现的单元测试证据。

### 6.10 产品级 Hooks（s04 安全适配）

参考 `shareAI-lab/learn-claude-code/s04_hooks` 的“挂在循环上，不写进循环里”思路，已实现：

- `HookRegistry` 为每个调用方独立保存回调，支持 `UserPromptSubmit`、`PreToolUse`、`PostToolUse` 和 `Stop`。
- 同一事件按注册顺序执行；第一个非 `None` 返回值结束该事件的后续回调。
- `UserPromptSubmit` 在当前请求加入消息前触发，返回值不改写请求。
- `PreToolUse` 位于硬拒绝、风险判断和 preflight 之后，只能阻止本次 registry 工具；终端审批仍由 Harness 强制执行。
- `PostToolUse` 只在 Handler 确实被尝试后触发；Gate 0、策略拒绝、审批拒绝、重复拒绝和 Agent 控制工具不会触发它。
- `Stop` 在模型自然停止前触发；非空字符串作为新 user message 推动下一轮，但不能突破 `max_steps`。
- Hook 得到参数和消息的防御性副本，不能通过修改入参改变真实工具调用或历史。
- 当前只支持程序化、同步、进程内 Python Hooks；没有配置文件、外部 Shell Hook、异步加载或自动发现。

本项目没有照搬参考实现中的 `shell=True`、字符串黑名单权限或原始参数日志。Hooks 是可信扩展点，不是沙箱，也不是 Permission 替代品。

### 6.11 只读 Subagent v1

- 根 Agent 默认获得 `task(description)`；`--no-subagent` 同时从模型 Schema 和运行允许集中移除它。
- 只启动一层同步子 Agent。新建子上下文只有专用 system message 和 description，不复制父历史。
- 子工具是从 `READ_ONLY_TOOL_NAMES` 派生的七个读工具，加内部 `compact`/`submit_report`；不提供写、删、命令、Todo 或再委派。
- 每个根运行最多启动 4 个子 Agent，规范化后重复 description 不会重放；同一父工具批次只有第一个 `task` 可启动。
- 每个子 Agent 最多 8 次 provider request，摘要/反应压缩也计入；不设独立 wall-clock timeout。
- `submit_report` 必须是该 assistant message 的唯一 Tool Call。Harness 校验字段、数量、总长度、工作区路径、UTF-8 文件和可选行号，成功后直接将最多 20,000 字符的 JSON 交给父 Agent。该校验不证明 claim 语义真实。
- 子对话只存于内存；上下文产物写入工作区外的自动清理临时目录。父 Session 只记录 `task` 调用和最终结果。
- 共享 `git_status`/`git_diff` 使用最小隔离环境，禁用仓库 fsmonitor/Hook、
  global/system 配置和可选索引锁；READ 调用不继承模型凭据，也不刷新索引。
- 中断的父 `task` 被恢复为匹配 ID 的 `task_interrupted` 结果，不自动重启旧子 Agent。
- 终端对子 Agent 也统一显示准备、思考、上下文整理和真实执行状态，但不显示 description、参数、结果、报告或 reasoning。

真实第三方网关 E2E 已通过：隔离临时 workspace 中完成根
`task` → 子 Agent `read_file`/`git_status` → `submit_report` → 根回答，
工作区前后清单一致，恶意 `core.fsmonitor` 未执行。

### 6.12 交互式工作区、REPL 与 Session v2

- `interactive.py` 负责非全屏 prompt_toolkit 工作区/会话选择、多轮 REPL、Rich 进度与 Markdown 回答；每次输入继续复用现有 `run_agent()`。
- `/help`、`/compact`、`/workspace`、`/new`、`/resume`、`/sessions`、`/status`、`/verbose`、`/exit` 都是本地命令；未知斜杠命令不进模型，`//` 显式转义。
- 工作区信任/最近状态在 `~/.config/gum-code/state.json`；自动 Session 在 `~/.local/share/gum-code/sessions/`。读状态不创建目录，第一条任务前不创建空 Session。
- Session v2 仅保存 workspace、model、title 和 messages；v1 在交互恢复时必须先显示源/目标并确认复制为 v2，one-shot 直接恢复 v1 会拒绝，源文件始终不回写。每个正在写入的 Session 用稳定侧边 `.lock` 文件和 POSIX `flock` 持有进程生命期独占锁。
- Ctrl+C 中断工具批次时保留已完成结果，为每个 in-flight/pending `tool_call_id` 各补一条有界错误结果，不自动重放。provider 在仅有用户尾部时失败会回滚本轮，不留下连续 user message。
- 配置逐键优先级为 process env > 用户 `.env` > 启动目录旧 `.env`。交互向导隐藏 API Key，真实最小请求通过后才原子写入 `0600`。
- 首次真正启动会从旧 `gu-harness` XDG 目录仅复制支持的配置、状态和 Session；不覆盖、不删除旧文件，且使用一次性完成标记防止复活已删除 Session。
- 首次主 Agent 回答后另发一次 10 秒标题请求，仅携带首条用户任务；失败使用本地时间+任务备选，标题最多 30 字符。

## 7. 已确认的稳定状态

- `MAX_REACTIVE_RETRIES = 1`
- `MAX_STEPS = 16`、`TODO_REMINDER_INTERVAL = 3`
- `MODEL_MAX_CONTEXT_TOKENS = 1_000_000`、`DEFAULT_MAX_CONTEXT_TOKENS = 800_000`、`SUMMARY_INPUT_TOKEN_LIMIT = 640_000`、`SUMMARY_MAX_TOKENS = 8_000`、`CONTEXT_TARGET_RATIO = 0.8`、`RECENT_CONTEXT_RATIO = 0.30`、`KEEP_RECENT_TOOL_RESULTS = 3`、`FIT_RESULT_PREVIEW_CHARS = 1000`、`TRANSCRIPT_DIR = ".transcripts"`
- tokenizer 官方仓库 `deepseek-ai/DeepSeek-V4-Flash`，revision `60d8d70770c6776ff598c94bb586a859a38244f1`；本地文件校验 SHA-256 `8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf`
- `MAX_TODOS = 20`
- `SESSION_VERSION = 2`、`MAX_SESSION_FILE_BYTES = 5MB`、`MAX_SESSION_TITLE_CHARS = 30`、新标记 `_gum_code_in_flight`；读取兼容旧 `_vibe_harness_in_flight`
- `reactive_compact()`、`active_compact`、`context_compacted` 终端渲染
- 上下文错误的单次重试
- 中断工具批次安全恢复（completed / pending / in_flight 三段式）
- 只读 in-flight 可重新规划；可变更 in-flight 仍失败关闭
- step 耗尽后无工具阶段总结
- 完全相同的已拒绝工具调用在同一运行中去重
- CLI 异常前 flush stdout
- s03 Permission 三道闸门：直接高危命令硬拒绝 → 风险规则 `ALLOW/ASK/DENY` → 终端审批
- 终端进度反馈：交互 TTY 使用自动检测的 `dots` 动画，one-shot 使用 flush 的步数行；工具只在所有执行前门禁通过后显示
- Todo 单项 `content` 最多 500 字符，Schema 与运行时双层校验，超限更新原子失败
- `run_command` 在 preflight 拒绝 Shell 控制符和明确重定向；不弹审批、不执行 Handler，且保留 `find -exec ... ;` 与原生模式参数
- `run_command` 容器 HOME/XDG 固定在受限 `/tmp`，不挂载宿主 HOME、
  Docker socket 或宿主凭据，也不向容器传入宿主环境变量
- Sandbox 固定为无网络、只读根目录、全 capability 删除、
  `no-new-privileges`、1 CPU、512 MiB、128 PID、256 nofile 和 64 MiB `/tmp`
- 超时、输出超额或中断按随机精确容器名执行强制清理；失败不会回退到宿主机
- 四事件 `HookRegistry` 已接入 Agent/Dispatcher；Permission 顺序保持为硬拒绝 → 风险 → preflight → Hook 追加否决 → 审批 → Handler
- Git READ 子进程已隔离宿主环境并覆盖可执行仓库配置；恶意
  `core.fsmonitor`、凭据继承和可选索引刷新均有回归测试

当前源码的最新验证命令和结果：

```bash
PYTHONPATH=src PYTHONPYCACHEPREFIX=/Users/gumishdo/.local/share/gum-code/pycache \
  /Users/gumishdo/.local/share/gum-code/venv/bin/python \
  -m pytest -q -p no:cacheprovider
# 811 passed, 3 skipped（2026-09-04，当前离线全量回归）
```

另外，真实第三方网关 Subagent 链为 `1 passed in 12.41s`；本次
交互运行时的真实回答、自动 Session、标题请求和 `0600` 探针也已通过。
三个跳过项分别由 Docker 实机、真实网关和 tokenizer 金标外部门禁控制。
本分支另用真实 PTY 确认 `准备中...` / `思考中...` 会在完成标记前动态刷新，
并确认 `Ctrl+C` 后 Rich status 及时清理、进程正常退出。

## 8. 个人日常使用版发布验收（已完成）

已完成：

- 新建 `README.md`，记录外部虚拟环境安装、公开配置名、CLI、Session、运行预算和安全边界。
- 在全新临时虚拟环境中使用 `pip install .` 成功安装依赖、wheel 和 `gum-code` console script。
- `gum-code --help` 正确显示 workspace、step/context 预算与 Session 参数。
- 真实模型只调用一次 `list_files`，对空 workspace 给出正确结论，未请求审批，workspace 保持为空。
- Session 消息顺序为 `system,user,assistant,tool,assistant`，`tool_call_id` 匹配，无 in-flight 标记，mode 为 `0600`，可正常加载。

发布验收是历史事实，但新一轮安全复验发现了 `run_command` 任意代码执行边界等新问题，因此不得再声称“无已知待修复错误”。当前必须按第 9 节路线逐步收口。

## 9. 当前必须按顺序推进的修复路线

下列安全边界继续保留；Hooks 已作为独立能力完成，不改变这些边界。

1. **Gate 0（已完成并通过验收）**
   - `run_command` 默认不出现在模型 tools Schema 中。
   - CLI 显式 `--allow-run-command` 才加入 Schema。
   - Agent 执行层也必须检查本轮允许集；模型即使伪造调用也不得调用 `execute_tool` 或审批回调。
   - 被 Gate 0 拒绝仍要返回匹配 `tool_call_id` 的 OpenAI `role=tool` 消息，但不应生成 in-flight 快照。
   - 实际上下文估算和模型请求必须使用同一份动态 Schema 列表。
   - Session 不持久化该能力；恢复时以本次 CLI 参数为准。

2. **s03 Permission 三道闸门（已完成并通过验收）**
   - 对照 `https://github.com/shareAI-lab/learn-claude-code/blob/main/s03_permission/README.zh.md`。
   - Gate 1 硬拒绝优先；Gate 2 规则匹配；Gate 3 才是用户审批。
   - 未知/未启用工具拒绝，只读允许；仅显式列入安全流程的 `write_file`/`edit_file` 免审批，其他 WRITE、删除和执行仍为 ASK。
   - `shutdown`/`reboot`/`sudo`/磁盘格式化等直接危险项不能通过审批强制放行。
   - 黑名单只是纵深防御；解释器、测试和构建脚本仍是任意代码执行，如需默认开放必须另做 OS/容器沙箱。

3. **终端精简显示（已完成并通过验收）**
   - 普通日志只保留上下文、当前 step + 工具名、最终回答和错误。
   - 隐藏普通工具参数、工具结果正文和冗余模型请求提示。
   - 权限审批参数预览是安全例外，必须保留。

4. **Todo 资源上限（已完成并通过验收）**
   - 为单项 `content` 增加统一硬上限（建议 500 字符）。
   - Schema `maxLength` 和 `TodoManager.update()` 双重校验，超限更新原子失败。

5. **显式命令模式的可用性与隔离（已实现并通过真实 Docker 验收）**
   - 已完成：对 `&&`/`||`/`|`/`;` 和明确重定向语法返回可操作错误，不恢复 shell。
   - 不得笼统拒绝所有含 `*`/`?`/`[]` 的 argv：它们也可能是 `rg`/`find`/解释器的原生模式参数；未展开 glob 需后续做命令感知处理或只增强 Schema 指引。
   - 已完成：使用每命令新建 Docker 容器，断网、只读根文件系统、
     敏感 workspace 路径遮蔽、固定资源限制、有界输出和异常清理。
   - 已完成：使用本地镜像并锁定 image ID；模型不能选择镜像、挂载、
     网络、容器用户、环境或资源参数；Docker 不可用时失败关闭。
   - 已添加默认跳过的真实 Docker 集成测试；需 Docker Desktop 运行且
     `GUM_CODE_DOCKER_E2E_IMAGE` 指向预先加载、内含 `python3` 的可信镜像。
   - 已使用用户授权手动拉取的 Docker 官方 `python:3.11-slim` 验证工作区写入、
     敏感路径遮蔽、非 root、断网、只读根目录和超时/输出超额后的精确清理。

6. **低风险体验/兼容收口**
   - 空 workspace 得到明确空结果后不重复换 glob 探索。
   - `read_tool_output` Schema 明确 `complete`/`next_offset` 分页语义；保留 JSON，不丢元数据。
   - 明确要求 `ripgrep >= 0.10.0`（`--json` 从 0.10.0 引入，不是 `0.16`），给出可操作的兼容错误。

7. **最终回归和文档收口**
   - 定向测试、全量测试、外置非 editable 安装探针和隔离 workspace E2E。
   - 同步 `TEST_REPORT.md`/`README.md`/本 `HANDOFF.md`，明确历史基线、当前验证和剩余边界。

更远的 Hooks 配置/外部适配、Skills、MCP、并行/递归 Subagent、worktree 并行和 Goal Loop 仍是可选路线，应分别立项。

## 10. 已知限制和勿抢先处理事项

- 当前 tokenizer 只支持精确模型名 `deepseek-v4-flash`，不支持任意网关别名或多模型自动分派。
- 本地已使用匹配的真实 tokenizer，但第三方网关可能添加隐藏 framing 或更换同名模型 revision；请求后以其 `usage.prompt_tokens` 为准，并保留被动压缩的单次重试。
- `ToolPolicy` 数据类目前没有使用，不影响正确性，不要为此单独重构。
- `in_flight` 工具副作用未知时不会自动恢复，这是有意的安全边界，不是待修复 Bug。
- 压缩摘要是可恢复的会话状态，不是跨 Session 的长期 Memory。
- Hooks 当前只有同步、进程内 Python API；没有配置文件、外部命令 Hook、异步 Hook 或自动发现。
- 当前没有 Skills、MCP、Goal Loop、并行/递归 Subagent 或子 Session。
- 不要读取 `.env`，不要在未经用户审批时运行会更改工作区的真实 Agent 任务。
- `run_command` 默认不向模型可见；显式开启需 Docker daemon 和本地可信镜像。
  容器可修改所有未遮蔽的 workspace 文件，且只能按现有敏感名/后缀策略识别凭据；
  普通文件名中的密钥、恶意镜像、Docker daemon/VM/内核漏洞不在 Harness 保证范围内。
- workspace 读写 bind mount 的宿主磁盘占用没有被 Docker 资源限额约束；对无界生成
  文件的命令仍应在 Permission 层拒绝。
- `shell=False` 不支持 Shell 运算符、重定向或 glob 展开；模型使用这些形式时可能得到误导结果。
- 容器断网，且 `.git`/`.venv` 会被遮蔽；镜像必须自带要执行的命令和依赖，
  命令不能使用宿主全局配置或在运行时下载包。
- `read_tool_output` 返回 JSON 分页对象；Todo reminder 仍追加到最后一个 tool result。
- 当前机器 `ripgrep 15.2.0` 可用；`--json` 的真实最低引入版本是 `0.10.0`。

## 11. 验收原则

每一步都应同时验证：

1. 源码确实连入运行链，不是只增加未使用的函数。
2. 有单元测试覆盖成功、拒绝和失败路径。
3. 全量测试仍通过。
4. 没有意外扩大模型可见工具、工作区权限或外部副作用。
5. 没有读取、打印、提交或写入任何敏感信息。
6. 状态型或可变更行为的测试使用独立临时 workspace。

Docker Sandbox 的历史验证为 `692 passed, 1 skipped in 40.58s`（2026-09-03，
包含真实 Docker 集成测试）；全新临时虚拟环境中正常 `pip install .`、Sandbox
模块导入和包内 CLI help 均通过。Docker Desktop Server 29.1.3 与用户授权
手动拉取的 Docker 官方 `python:3.11-slim` 已用于真实边界测试。实现提交
`f6a890f` 已通过 PR #5 合并到 `main`。当前分支为
`feat/agent-loop-progress-feedback`，最新验证见第 7 节；本地详细记录见
`TEST_REPORT.md`（该文件按用户要求不上传 GitHub）。
