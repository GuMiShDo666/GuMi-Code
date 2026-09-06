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

直接提出任务即可，无需选择开发或只读模式。旧 `--task-type` 参数仅保留兼容提示，不再改变执行流程。

主 Agent 每次运行默认最多 512 个模型步骤，可用 `--max-steps` 覆盖；正常完成时提前结束。实现、自查和失败修复共享该预算；用尽后停止并报告未完成项，不追加模型、工具或自检调用。主步骤数不等于全部模型请求数、token 或费用上限，子 Agent 和视觉调用配额保持独立。

模型回复被截断或 Todo 仍有未完成项时，Gum Code 会在剩余预算内提醒续做。连续两次提醒后仍未推进而再次结束，会保留结果并报告未完成；正常读取、修改和续写不受两次限制。该检查不能证明模型已覆盖全部需求。

请求预算默认为已解析模型上下文窗口的 80%。可用 `--max-context-tokens` 设置更小的正整数预算；超过模型窗口会直接报配置错误。从 provider 自动识别的窗口只用于当前运行，不会写入配置。

### 可选：启用 Docker 命令沙箱

普通命令工具需要先启动 Docker，并准备可信的本地镜像。下面的显式配置不会自动 pull 或 build 镜像。

```bash
.venv/bin/gum-code \
  --workspace /path/to/project \
  --allow-run-command \
  --sandbox-image trusted-local-image:tag
```

Windows 用户将可执行文件替换为 `.\.venv\Scripts\gum-code.exe`。普通命令逐次审批；自动自检仅执行宿主发现的项目检查，不授予模型任意命令许可。

## 自动自检与修复

只有当前请求实际修改源码、测试或运行/构建配置后，Agent 才需要调用无参数的 `self_check`。它会运行项目适用的语法、编译或测试检查，并根据失败结果修复、复测；模型直接结束时，宿主会在剩余步骤内补上检查。读取、问答、纯文档修改和内容未变化的写入不会触发自检。你无需登记检查计划或提交验收结果。

检查结果绑定当时的代码和环境。再次修改代码、检查写回源码或无法确认输入时，旧结果不能代表当前版本。安装成功、模型说“已完成”、输出中的 PASS 字样均不能代替真实检查。

自检区分测试用例执行、语法/类型/构建检查和普通脚本运行。pytest/unittest 只有执行了非跳过、非预期失败的成功用例，才提供测试通过证据；零用例或全部跳过不算通过测试。未知脚本、空脚本和 npm test 的零退出只能证明脚本运行成功。只有语法/类型/构建检查通过时可以限定完成，但会说明功能尚未验证；只有普通脚本成功则不能通过自检。测试通过仍不保证需求覆盖、断言质量或生产就绪。

一次性任务正常完成返回 `0`；已改代码但检查失败、无法执行或预算耗尽返回 `1`，同时保留改动和有用回答。REPL 单轮失败不会关闭会话。步骤耗尽后不追加模型请求，相同输入也不会反复空跑失败检查。

### 缺少运行环境时

已有可用隔离环境时，自检不逐条询问。需要下载运行时或安装项目依赖时，Gum Code 会先说明下载、磁盘占用和依赖安装脚本的风险，再询问是否创建环境；空输入、拒绝或无法交互均不会批准准备。同一请求拒绝后不重复询问。

首版支持标准 Python/pip 清单和 Node.js/npm 项目（含 JS/TS），维护项目已有依赖清单和版本，不从宿主全局环境生成清单。已存在的锁文件不会静默重写；新 npm 项目缺锁时，可在这次准备许可内生成并安全回写。其他包管理器、私有/VCS/本地依赖、动态依赖或不匹配锁文件会报告不能自动准备。

环境准备使用项目专属 Docker 依赖卷，联网容器只读取筛选后的最小依赖元数据，不挂载工作区源码或用户配置。依赖安装脚本仍可能执行第三方代码和访问网络。检查阶段断网、依赖卷只读，源码和构建输出仍可写；要求写入依赖目录的工具可能需要调整缓存配置。Gum Code 不自动安装或启动 Docker、不创建数据库/Redis/云服务、不安装宿主全局依赖。自动准备目前依赖 POSIX 文件锁与安全文件操作，Windows 尚不支持。

已有脏文件不会自动归到本次请求；扫描输入受每次 20,000 个目录项、128 MiB、5 秒的限制。源码超限、链接输入和并发修改会阻止确认通过。只排除已验证的受管依赖路径和明确缓存/输出范围，不把所有 Git 忽略项当成无关产物。

新版 Session 使用 `self_check.state` 保存本轮变化事实。恢复或 fork 不继承准备许可或当前通过结论；旧 `task.state` 仅作历史读取。旧二进制不能读取含新事件的日志，降级时请保留原件并使用兼容会话或新建会话。

### 构建日志

`run_command` 持续读取 stdout/stderr，日志量超过预览或保存上限不会单独导致命令被中止。内存保留有界首尾，结束后尝试保存私有日志：每份最多 5 MiB，每工作区的新命令日志合计最多 100 MiB；只自动回收本机制最旧的已结束日志，不清理其他工具产物。临时容器固定使用 `--log-driver=none`，不另写 Docker 容器日志。

模型可用 `read_tool_output` 分页读取已保存内容；截断的中段无法恢复，旧引用可能因回收而不可用。分页字段 `complete` 只表示读完已保存文件，不代表原始输出完整。普通写入、配额或锁失败时，命令继续并明确报告日志未保存；路径及沙箱安全检查失败仍会拒绝执行。真实零退出、输入未变的检查不会仅因日志截断或未保存而失败，超时或读取错误也不能算成功。终端仍不实时展示日志正文。

### 文件、搜索与 Git 续读

列表、查找、搜索、UTF-8 文件和 Git 状态/diff 超过单页时，Gum Code 会保存有界快照，供模型按需用 `read_tool_output` 续读，无需你手动翻页。超长单行也可继续读取。模型做全量核查时应读完相关范围，或说明尚未覆盖的部分。

读取结果区分当前快照读完（`complete`）、请求范围采集完整（`scope_complete`）和文件是否还有后续行。`read_file` 默认取 200 行、最多 400 行；续读下个行窗口时携带来源版本，文件变化后不能把新旧内容静默拼接。Git 状态/diff 可按工作区相对路径缩小范围。

单次返回最多 20,000 字符，每份快照最多 5 MiB；目录扫描最多 10,000 个目录项、64 层、10 秒。文本文件完整版本扫描最多 32 MiB、10 秒，搜索/Git 子进程采集也有容量与时间限制。超限、无法保存或来源变化都会标明缺口，读完保留内容不等于读完原始范围。读取快照没有全工作区自动回收配额；只读子 Agent 的快照随子任务临时目录清理。

## PDF、DOCX 与图片

`read_file` 仍只读 UTF-8 文字。新增的 `read_document` 在本地分批提取 PDF 文字、DOCX 正文与表格，并提供图片引用；`analyze_image` 按任务需要，把一张图片、指定 PDF 页面或 DOCX 内嵌图片交给独立视觉模型，返回文字给主 Agent。主模型不需要支持图片。

首次主 API 配置保存后，可选择是否配置图片解析；跳过不影响其他功能。已有用户在交互界面输入 `/vision`，可配置接收方、启用/关闭或调整后续请求的默认次数。视觉配置单独保存在用户配置目录的私密 `.env.vision`，不继承项目 `.env` 的启用许可。保存前会说明图片外发和可能费用；向导只校验配置，不调用真实图片 API，保存成功不代表接口图片能力已验证。接口需支持 OpenAI-compatible Chat Completions 的单图输入和文字输出。

每个明确的用户请求默认最多尝试 32 次视觉调用，主、子 Agent 共用，失败和超时也计数；这是调用次数上限，不是金额上限。最多两个视觉请求同时在途，单次超时为 60 秒，不自动重试。额度不足时，主线程收齐本批结果后才询问是否追加；拒绝或非交互运行不会自动追加，也不增加主 Agent 的步骤预算。恢复原请求保留已用次数；额度记录缺失时不会自动补满。

PDF 每批最多 5 页、DOCX 每批最多 100 个正文块，返回先受 20,000 字符限制；Agent 按来源版本和续读游标继续。PDF 有文字或 OCR 层不代表图表已经读取，扫描页、矢量图和局部放大可按需做视觉分析。支持静态 PNG/JPEG/WebP，拒绝动画和不支持的格式。DOCX 普通内嵌图片可分析，但原生复杂形状、SmartArt、未还原的图片变换以及未读的非正文内容会明确标为缺失；复杂版面可由用户另提供 PDF 或截图。整个流程不依赖 Word、LibreOffice、WPS、OCR 引擎或云端办公转换。

原文档最多 25 MiB，图片最多 10 MiB；另有页数、像素、ZIP/XML 与解析时间限制。解析使用短生命周期子进程及私密临时快照，结束后清理，不长期缓存原图；这不是 OS 沙箱，也不是异常退出后的安全擦除。缺少安全文件打开能力的平台会拒绝文档读取，Windows 路径尚未验证。原图和 Base64 不进入主模型或 Session；分析文字仍按现有 Session/工具结果规则保存。来源版本变化后应重新读取，历史分析不是新版本的证据。

视觉额度使用新的 `vision.state` 事件保存，不包含外发许可或密钥。新版可读旧 v3 日志，旧版不能读取含新事件的日志；降级时保留日志原件并使用旧版可读的会话或新建会话，不要删改事件。当前验证使用合成文档和模拟服务，不能证明真实视觉模型的识别准确率或生产适用性。

## Skills 与 Session

在任务中写 `$skill-name` 可在第一次模型请求前加载对应的 Codex 用户 Skill。Gum Code 只扫描 `$CODEX_HOME/skills`（未设置时为 `~/.codex/skills`）的直接子目录；显式 Skill 会在当前 Session 中保持 sticky，单个任务与 Session 最多同时保留 3 个。根 Agent 也可按需使用 `find_skills`、`load_skill` 和 `read_skill_resource`，自动加载仅对当前任务有效。`/skills` 可查看、禁用或清空 sticky 名称，不显示 Skill 内容。

Skills 只作为只读指令与文本资源加载。Gum Code 不执行 Skill 脚本、不安装 Skill，也不会把 Skill 工具或内容交给 Subagent。

新 Session 使用私有、追加写入的 v3 JSONL 事件日志，文件总上限仍为 5 MiB。v1/v2 Session 在首次恢复或导入时复制迁移，源文件保持不变。`/sessions` 可查看不含提示词、参数、结果或 Skill 正文的安全审计时间线，也可从已完成 turn 创建独立 fork。上下文压缩只追加新的活动投影，不删除旧事件。

验收计划和历史结果使用独立的 `task.state` 事件保存；`turn.completed` 仍只表示对话消息边界。新版可以读取没有验收状态的旧日志，按未验收处理；旧版无法读取含 `task.state` 的新日志。回退程序时保留新日志，使用旧版可读的旧会话或新建会话，不要删改历史事件。

终端用同一行切换 `思考中...`、`正在读取...`、`正在修改文件...`，不显示工具名或累计状态行。每次调用、结果与 Session 事件仍分别保留。

同一模型步骤中相邻的 `task` 会组成并行组；非 `task` 工具是串行屏障，父 Agent 会等待整组结束并按原 Tool Call 顺序提交结果。每次根运行最多启动 4 个 Subagent。Subagent 共用当前 OpenAI 兼容 client，因此外部自定义 client 必须支持并发 `chat.completions.create()`。`Ctrl+C` 会协作取消后续步骤，但不能强杀已经进入的同步 HTTP 请求。

## 当前能力与边界

| 范围 | 当前能力 | 边界 |
| --- | --- | --- |
| 运行方式 | 支持工作区选择、持久 REPL 和一次性 CLI 任务。 | 它是本地单用户 Harness，没有 Web 服务或多用户权限系统。 |
| 模型协议 | 调用 OpenAI 兼容的 Chat Completions 和 `tool_calls`，通过通用本地估算和有效的 provider `usage` 校准管理 token 预算。 | 仅统计文本消息和工具定义；第三方网关不提供模型窗口时需要手动配置。 |
| 文件与 Git | 支持列出、读取、搜索、写入和编辑文件，以及查看 Git 状态和差异。 | 文件工具只能访问工作区；删除需要审批；路径、符号链接、敏感文件名和大小受到限制。 |
| 命令执行 | 显式启用后，可以在断网 Docker 容器中运行单个 argv 命令。 | `run_command` 默认关闭；普通命令逐次审批，固定检查可明确批准阶段内重跑。容器仍可修改工作区，也不能防御恶意镜像、Docker daemon 入侵或运行时漏洞。 |
| 自动自检 | 实际代码变化后运行项目检查，按当前版本与真实执行证据判断结果，在剩余步骤内修复重测。 | 依赖可用隔离环境；区分测试与基础检查，不保证模型能修复所有错误或覆盖全部业务要求。 |
| 上下文 | 支持工具结果裁剪、结构化压缩、主动压缩和超限后的单次重试；终端只显示上下文占上限的百分比。 | 压缩摘要属于当前 Session，不是跨 Session 长期 Memory。 |
| Session | 使用 v3 JSONL 事件日志自动保存交互记录，支持安全审计、已完成 turn fork、重命名、恢复和安全闭合中断工具批次。 | 单文件上限 5 MiB；达到上限后需新建或 fork Session。已开始工具的副作用未知时，使用者必须检查当前状态，Harness 不会自动重放。 |
| Skills | 共用 Codex 顶层用户 Skill 库，支持显式 sticky 加载和根 Agent 的渐进式只读加载。 | 不扫描 `.system`、插件或项目 Skill；不执行脚本；Subagent 不接收 Skills。 |
| Subagent | 相邻 `task` 默认并行；每次根 Agent 运行最多启动 4 个单层、只读子 Agent，每个最多使用 8 次模型请求。 | 子 Agent 使用独立上下文且不接收 Skills，不能写入、执行命令或递归委派，也不会持久化。普通工具仍串行。 |
| Hooks | Python API 支持 `UserPromptSubmit`、`PreToolUse`、`PostToolUse` 和 `Stop`。 | Hooks 只支持同步、进程内注册；没有配置文件、外部命令、异步回调或自动发现。 |
| 尚未实现 | 项目当前不包含 MCP、Goal Loop、长期 Memory、递归 Subagent 或子 Agent 持久化。 | 不要把 README 中的边界当作未来版本承诺。 |
