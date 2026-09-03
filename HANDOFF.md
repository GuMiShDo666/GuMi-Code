# Vibe Harness 开发交接文档

> 最后更新：2026-09-03
> 当前稳定检查点：DeepSeek V4 真实 tokenizer 已接入全部上下文预算和压缩链路
> 当前源码验证：见本文第 7 节的最新验证记录
> 当前下一步：审阅并合并 tokenizer context budget PR

## 1. 新对话如何续接

在新对话中直接输入：

```text
请先完整阅读
/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/vibe-harness/HANDOFF.md
然后检查当前代码和测试。Gate 0、s03 Permission、终端精简显示、Todo 单项上限、Shell 控制符/重定向报错和一次性隔离 HOME 已通过验收。
当前已按 s04 思路加入 `UserPromptSubmit`、`PreToolUse`、`PostToolUse` 和 `Stop`；Hooks 只能追加否决，不能取代或绕过 Permission。
当前上下文预算已从字符估算迁移到固定版本的 DeepSeek V4 官方 tokenizer/消息编码器；本地数值用于请求前决策，第三方网关 `usage.prompt_tokens` 是请求后权威数值。
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
/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/vibe-harness
```

环境：

- Python 3.11
- OpenAI Python SDK，使用 OpenAI 兼容网关
- 虚拟环境已迁移到 `/Users/gumishdo/.local/share/vibe-harness/venv`（非 editable 安装，避免运行时直接 import iCloud 中的源码文件）
- 已通过 `pip install --force-reinstall --no-deps .` 安装为 Python package，console script 为 `vibe-harness`

安全约束：

- 敏感配置在 `.env`；绝对不得读取、输出、复述或提交其内容。
- `.env.example` 只能包含占位符和公开配置名。
- 不要在项目源码目录中做会修改文件的真实 E2E；应使用独立临时 workspace。
- GitHub 仓库为私有仓库 `GuMiShDo666/Gu-Harness`；功能更新使用分支、测试、Conventional Commit、push 和 PR，不直接提交到 `main`。
- 当前工作分支为 `feat/tokenizer-context-budget`。

常用验证命令（源码测试继续使用外部 Python，并设置 `PYTHONPATH=src`）：

```bash
cd "/Users/gumishdo/Desktop/01_AI与编程项目/Learn-Agent/vibe-harness"
PYTHONPATH=src /Users/gumishdo/.local/share/vibe-harness/venv/bin/python -m pytest -q -p no:cacheprovider
```

正式 CLI（使用虚拟环境中的 `vibe-harness`，不再要求 `PYTHONPATH=src python -m vibe_harness`）：

```bash
/Users/gumishdo/.local/share/vibe-harness/venv/bin/vibe-harness \
  --workspace /absolute/workspace/path \
  [--max-steps N] \
  [--max-context-tokens N] \
  [--tokenizer-file /absolute/path/tokenizer.json] \
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
src/vibe_harness/
├── __main__.py              # python -m vibe_harness 入口
├── agent.py                 # Agent Loop、工具批次、Todo reminder、in-flight marker 快照
├── cli.py                   # CLI、Session 接入和错误输出
├── config.py                # 从环境加载 API 配置
├── context.py               # token 预算、分层压缩和 transcript
├── token_counting.py        # DeepSeek V4 请求编码、token 计数和固定版本加载
├── _vendor/
│   └── encoding_dsv4.py    # 固定的官方 DeepSeek V4 消息编码器快照
├── events.py                # 终端事件渲染
├── hooks.py                 # 四事件 HookRegistry（进程内同步回调）
├── session.py               # Session 原子保存与工具批次分析/恢复计划
├── todo.py                  # TodoManager（MAX_TODOS=20，全量校验原子替换）
├── permissions/
│   ├── policy.py            # 工具风险和 ALLOW/ASK/DENY
│   └── terminal.py          # 终端 y/N 审批
└── tools/
    ├── common.py             # 路径、敏感文件和输出边界
    ├── filesystem.py         # list/read/write/edit/delete
    ├── search.py             # find_files/search_text
    ├── git.py                # git_status/git_diff
    ├── command.py            # run_command
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
| `write_file` / `edit_file` | WRITE | 终端审批 |
| `delete_file` | DESTRUCTIVE | 终端审批 |
| `run_command` | EXECUTE | 终端审批 |

- `ALL_TOOL_RISKS` 是风险唯一数据源，Schema、Handler 和 Risk 的 key 一致性有测试保护。
- `MODEL_VISIBLE_TOOL_NAMES` 默认排除 `EXECUTION_TOOL_NAMES`；`--allow-run-command` 只为当前运行动态加入 `run_command` Schema。
- 审批函数只有在去除空白、忽略大小写后等于 `y`/`yes` 才返回 True。
- 空输入、其他输入、EOF 和 `KeyboardInterrupt` 默认拒绝。
- 审批参数预览限长，不会无限刷屏。

已有工具边界：workspace 相对路径、越界/符号链接/敏感路径拒绝、安全 glob、`rg --json` 结果二次过滤、`shell=False`、子进程环境允许列表、超时和输出上限、原子写入。

后续安全复验已确认：

- `.transcripts` 和 `.task_outputs` 对普通文件工具不可见/不可写；转存结果只能经 `read_tool_output` 分页读取。
- 写、编辑、删除和命令在审批前先运行无副作用 preflight，注定失败的操作不弹审批。
- 工具错误保留可安全公开的具体原因，不再统一吞成 `Tool execution failed`。
- 显式 Shell 解释器、明显的敏感/绝对/逃逸路径命令参数会在 preflight 拒绝。
- 工具返回、文件、搜索结果、命令输出、transcript 和转存结果均被 system instruction 标记为不可信数据。
- 相同工具名 + 规范化后相同参数的调用被用户拒绝后，同一 `run_agent` 内不再重复弹审批；参数或工具改变时仍重新审批。
- 显式用 `ToolExecutionDenied` 字符串子类区分真实用户拒绝与“长得像拒绝”的普通工具输出。

权限系统下一参考是 `shareAI-lab/learn-claude-code/s03_permission`：工具执行前依次经过硬拒绝、规则匹配和用户审批。但 s03 的字符串黑名单是教学示例，其 README 明确说明不是完整安全边界；本项目不能因此将任意命令执行宣称为安全。

### 6.2 正式 CLI、事件与 E2E

- 正式入口为 `vibe-harness --workspace PATH task`（console script，见 6.8）。
- workspace 必须是已存在目录。
- Agent 仍发送完整事件给回调，正式终端渲染只显示上下文、当前 step + 工具名、限长错误和最终回答。
- 普通工具参数、成功结果和冗余模型请求提示不再输出；压缩事件不输出原始历史或摘要。
- CLI 统一捕获已知运行错误，写入 stderr 并返回 1。
- 写 stderr 错误前先 flush stdout，错误行不再因缓冲出现在已有日志之前。

写/删/命令的权限审批仍显示限长参数预览，与普通终端日志的隐藏规则相互独立。

### 6.3 Session 保存与恢复

- version-1 JSON，UTF-8 + `ensure_ascii=False`。
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
  - 重新规划出的写操作仍需正常终端审批，不会绕过权限。

### 6.4 上下文预算与分层压缩（s08，含 OpenAI tool_calls 适配）

实现依据：

- 外部参考：`https://github.com/shareAI-lab/learn-claude-code/blob/main/s08_context_compact/README.zh.md`
- 本地参考：`../learn-claude-code/s08_context_compact/README.zh.md`
- 本项目是 OpenAI `assistant.tool_calls + role=tool` 协议，因此已做适配，未机械复制 Anthropic 消息结构。

每次正常模型请求前的管线：

```text
DeepSeekV4TokenCounter.count_request(messages, enabled_tools)
  → apply_tool_result_budget
  → apply_snip_compact
  → 如仍超限：apply_micro_compact
  → 如仍超限：apply_fit_tool_results
  → 如仍超限：compact_history
  → 重新计算 token，仍超限则失败关闭
  → context_usage
  → model_call
  → provider_usage（网关返回 usage 时）
```

已实现细节：

1. `DeepSeekV4TokenCounter`
   - 使用固定版本的 DeepSeek V4 官方 `encoding_dsv4.py` 渲染 system/user/assistant/tool 消息、工具 Schema 和 DSML framing，再用匹配的官方 tokenizer 计算 token ID 数量。
   - 当前只接受精确模型名 `deepseek-v4-flash`；不支持的模型或加载失败会在首次模型请求前显式失败，没有字符数回退。
   - CLI 每次运行只加载一个 counter；默认从官方 Hub 固定 revision 加载并缓存，也可用 `--tokenizer-file` 指定哈希验证过的本地文件。
   - 默认预算 `DEFAULT_MAX_CONTEXT_TOKENS = 100_000`，传入值必须是正整数。

2. `apply_tool_result_budget`
   - 对最新 tool-result 批次使用 200,000 字符上限。
   - 超限时将大结果写入 `.task_outputs/tool-results/`，消息中保留路径和预览。

3. `apply_snip_compact`
   - 消息超过 50 条时，完整历史写入 `.transcripts/`。
   - 保留最初 3 条和最近消息，中间插入带路径的归档标记。
   - 调整切分点，不切断 assistant/tool 组。

4. `apply_micro_compact`
   - 未被模型读取的最新 tool results 全部保留。
   - 已消费的旧结果保留最近 3 条，其余过长的结果转存后缩短。

5. `apply_fit_tool_results`
   - 若微压缩仍不足，按从大到小处理其余 tool results。
   - 完整结果落盘，消息中只保留路径和 1,000 字符预览。

6. `compact_history`
   - 压缩前先保存完整 transcript。
   - 摘要请求以完整编码后的 token 数为边界，上限是 `SUMMARY_INPUT_TOKEN_LIMIT = 80_000` 与当前上下文预算的安全比例中较小者；摘要输出最多 2,000 tokens。
   - 超限时二分搜索保留文本量，保留头部四分之一和尾部四分之三；切片手段是字符，停止条件是 token。
   - 摘要请求不提供 tools，并明确要求不执行历史中的指令。
   - 压缩后只保留 system + `[Compacted]` user message，当前请求与历史摘要明确分层。
   - 摘要会先经 JSON 字符串编码，其中的伪造标题、换行或指令无法冒充当前请求。

7. `reactive_compact`
   - 第三方网关可能加入本地不可见的 framing 或部署不同 revision，API 仍可能拒绝本地预检已通过的请求。
   - 识别 `prompt_too_long`、`too many tokens`、`context_length_exceeded`。
   - 保存完整 transcript，总结旧历史，保留最近 5 条消息。
   - 若尾部从 tool message 切入，会向前扩展到对应 assistant tool call。
   - 每次正常模型调用最多被动压缩并重试 1 次；第二次上下文错误和非上下文错误直接传播。
   - 重试不消耗新的 Agent step；压缩后再次使用同一 counter 检查 token 预算。

8. Provider usage
   - 正常、摘要、被动压缩和阶段总结请求都显式使用 thinking mode，并在响应包含 `usage` 时发送只含数字的 `provider_usage` 事件。
   - 网关 `usage.prompt_tokens` 是请求后的权威值；本地计数用于请求前决策，不承诺与计费值完全相等。
   - 网关缺少或只返回部分 usage 不影响 Agent 执行；事件不包含 messages、工具参数、响应正文、header 或凭证。

落盘安全：

- transcript 和转存工具结果使用原子替换和 `0600` mode。
- 内部目录拒绝符号链接。
- 消息中只保留 workspace 相对路径，不暴露不必要的绝对路径。

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
- `--max-context-tokens`：默认 `DEFAULT_MAX_CONTEXT_TOKENS=100_000`，透传 `run_agent(max_context_tokens=...)`。旧的 `--max-context-chars` 已移除，不做静默单位转换。
- `--tokenizer-file`：可选的本地官方 DeepSeek V4 `tokenizer.json`。未指定时从 `deepseek-ai/DeepSeek-V4-Flash` 的固定 revision 加载并缓存。

阶段总结安全边界：

- 最终请求不传 `tools`/`tool_choice`，响应中即使伪造 `tool_calls` 也不执行。
- 耗尽前已完成的工具结果用于生成“已完成/未完成”的最佳回答。
- 总结或压缩失败时不用不安全中间状态覆盖最后一个可恢复 Session 快照。
- 只有额外一次无工具总结请求受硬上限约束；摘要和上下文错误重试仍可能额外调用模型。

### 6.8 打包与 console script

- `pyproject.toml` 定义 console script `vibe-harness = "vibe_harness.cli:main"`。
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

## 7. 已确认的稳定状态

- `MAX_REACTIVE_RETRIES = 1`
- `MAX_STEPS = 16`、`TODO_REMINDER_INTERVAL = 3`
- `DEFAULT_MAX_CONTEXT_TOKENS = 100_000`、`SUMMARY_INPUT_TOKEN_LIMIT = 80_000`、`CONTEXT_TARGET_RATIO = 0.8`、`KEEP_RECENT_TOOL_RESULTS = 3`、`FIT_RESULT_PREVIEW_CHARS = 1000`、`DEFAULT_MAX_MESSAGES = 50`、`TRANSCRIPT_DIR = ".transcripts"`
- tokenizer 官方仓库 `deepseek-ai/DeepSeek-V4-Flash`，revision `60d8d70770c6776ff598c94bb586a859a38244f1`；本地文件校验 SHA-256 `8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf`
- `MAX_TODOS = 20`
- `SESSION_VERSION = 1`、`MAX_SESSION_FILE_BYTES = 5MB`、`IN_FLIGHT_MARKER_KEY = "_vibe_harness_in_flight"`
- `reactive_compact()`、`active_compact`、`context_compacted` 终端渲染
- 上下文错误的单次重试
- 中断工具批次安全恢复（completed / pending / in_flight 三段式）
- 只读 in-flight 可重新规划；可变更 in-flight 仍失败关闭
- step 耗尽后无工具阶段总结
- 完全相同的已拒绝工具调用在同一运行中去重
- CLI 异常前 flush stdout
- s03 Permission 三道闸门：直接高危命令硬拒绝 → 风险规则 `ALLOW/ASK/DENY` → 终端审批
- 终端精简显示：隐藏普通参数/成功结果，保留上下文、step + 工具名、限长错误、最终回答和审批预览
- Todo 单项 `content` 最多 500 字符，Schema 与运行时双层校验，超限更新原子失败
- `run_command` 在 preflight 拒绝 Shell 控制符和明确重定向；不弹审批、不执行 Handler，且保留 `find -exec ... ;` 与原生模式参数
- `run_command` 使用一次性临时 HOME/XDG 环境，不读取真实用户 HOME 配置
- 四事件 `HookRegistry` 已接入 Agent/Dispatcher；Permission 顺序保持为硬拒绝 → 风险 → preflight → Hook 追加否决 → 审批 → Handler

当前源码的最新验证命令和结果：

```bash
PYTHONPATH=src PYTHONPYCACHEPREFIX=/Users/gumishdo/.local/share/vibe-harness/pycache \
  /Users/gumishdo/.local/share/vibe-harness/venv/bin/python \
  -m pytest -q -p no:cacheprovider
# 672 passed, 1 skipped in 3.71s（2026-09-03）
```

默认离线套件中跳过的一项是需要官方 tokenizer 缓存或网络的金标探针；显式设置 `VIBE_HARNESS_TOKENIZER_INTEGRATION=1` 后已单独验证为 `13 passed in 0.36s`。任何后续修改仍需重新运行验证。

## 8. 个人日常使用版发布验收（已完成）

已完成：

- 新建 `README.md`，记录外部虚拟环境安装、公开配置名、CLI、Session、运行预算和安全边界。
- 在全新临时虚拟环境中使用 `pip install .` 成功安装依赖、wheel 和 `vibe-harness` console script。
- `vibe-harness --help` 正确显示 workspace、step/context 预算与 Session 参数。
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
   - 未知/未启用工具拒绝，只读允许，写/删/执行保持更严格的 ASK；不机械照搬 s03 “普通写入可直接放行”的教学规则。
   - `shutdown`/`reboot`/`sudo`/磁盘格式化等直接危险项不能通过审批强制放行。
   - 黑名单只是纵深防御；解释器、测试和构建脚本仍是任意代码执行，如需默认开放必须另做 OS/容器沙箱。

3. **终端精简显示（已完成并通过验收）**
   - 普通日志只保留上下文、当前 step + 工具名、最终回答和错误。
   - 隐藏普通工具参数、工具结果正文和冗余模型请求提示。
   - 权限审批参数预览是安全例外，必须保留。

4. **Todo 资源上限（已完成并通过验收）**
   - 为单项 `content` 增加统一硬上限（建议 500 字符）。
   - Schema `maxLength` 和 `TodoManager.update()` 双重校验，超限更新原子失败。

5. **显式命令模式的可用性与隔离**
   - 已完成：对 `&&`/`||`/`|`/`;` 和明确重定向语法返回可操作错误，不恢复 shell。
   - 不得笼统拒绝所有含 `*`/`?`/`[]` 的 argv：它们也可能是 `rg`/`find`/解释器的原生模式参数；未展开 glob 需后续做命令感知处理或只增强 Schema 指引。
   - 已完成：使用 Harness 一次性隔离 HOME/XDG，不向命令暴露真实用户 HOME 和全局配置。
   - 补包装器/解释器绕过测试，评估 Docker 或 OS 沙箱，但不在小步骤中假装已解决架构性问题。

6. **低风险体验/兼容收口**
   - 空 workspace 得到明确空结果后不重复换 glob 探索。
   - `read_tool_output` Schema 明确 `complete`/`next_offset` 分页语义；保留 JSON，不丢元数据。
   - 明确要求 `ripgrep >= 0.10.0`（`--json` 从 0.10.0 引入，不是 `0.16`），给出可操作的兼容错误。

7. **最终回归和文档收口**
   - 定向测试、全量测试、外置非 editable 安装探针和隔离 workspace E2E。
   - 同步 `TEST_REPORT.md`/`README.md`/本 `HANDOFF.md`，明确历史基线、当前验证和剩余边界。

更远的 Hooks 配置/外部适配、Skills、MCP、Subagents、worktree 并行和 Goal Loop 仍是可选路线，应分别立项，不与本次基础 Hooks 混做。

## 10. 已知限制和勿抢先处理事项

- 当前 tokenizer 只支持精确模型名 `deepseek-v4-flash`，不支持任意网关别名或多模型自动分派。
- 本地已使用匹配的真实 tokenizer，但第三方网关可能添加隐藏 framing 或更换同名模型 revision；请求后以其 `usage.prompt_tokens` 为准，并保留被动压缩的单次重试。
- `ToolPolicy` 数据类目前没有使用，不影响正确性，不要为此单独重构。
- `in_flight` 工具副作用未知时不会自动恢复，这是有意的安全边界，不是待修复 Bug。
- 压缩摘要是可恢复的会话状态，不是跨 Session 的长期 Memory。
- Hooks 当前只有同步、进程内 Python API；没有配置文件、外部命令 Hook、异步 Hook 或自动发现。
- 当前没有 Skills、MCP、Subagents 或 Goal Loop。
- 不要读取 `.env`，不要在未经用户审批时运行会更改工作区的真实 Agent 任务。
- `run_command` 已由 Gate 0 改为默认不向模型可见，但显式开启后仍没有 OS 级文件系统/网络沙箱；它可被 `python -c`、脚本、测试运行器或包装器间接绕过 argv 路径检查。
- `shell=False` 不支持 Shell 运算符、重定向或 glob 展开；模型使用这些形式时可能得到误导结果。
- 子进程环境不含真实 `HOME`，依赖用户全局配置的 git/npm/pip 行为可能不同；不得为了便利直接恢复真实 HOME。
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

交接时的当前源码验证：`672 passed, 1 skipped in 3.71s`（2026-09-03 tokenizer 分支），官方 tokenizer 金标探针 `13 passed in 0.36s`，全新临时虚拟环境安装/console script/vendor 模块导入和第三方网关双请求工具链 E2E 均通过。任何后续修改仍需重新运行定向和全量测试。
