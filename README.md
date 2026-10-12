# GuMi-Code

GuMi-Code 是面向本地项目的 AI 编程助手，提供桌面端和 CLI。输入需求后，它可以阅读项目、解释代码、修改文件，并按需执行命令，帮助你开发功能、排查问题和整理文档。

使用前需准备支持 OpenAI 兼容 Chat Completions 和工具调用的模型服务。

**中文** · [English](README_en.md)

## 桌面端

当前支持 macOS Apple Silicon。已有 `GuMi-Code.app` 时，直接打开即可使用。

### 从源码启动

需要 Git 和 Node.js 24。

```bash
git clone https://github.com/GuMiShDo666/GuMi-Code.git
cd GuMi-Code/desktop
npm ci
npm run prepare:runtime
npm run build
npm start
```

首次准备运行环境需要联网下载 Python 和依赖，无需另外安装 Python。

### 配置与使用

1. 打开「设置 → GuMi-Code 设置」，填写 API 地址、模型名称和 API Key，保存配置；可用连接测试确认接口可访问。
2. 返回对话页，选择项目文件夹并确认信任该目录。
3. 输入需求并发送，例如「解释这个项目的主要功能」或「修复这个报错，并运行相关测试」。
4. 需要执行命令时，根据弹出的操作说明批准或拒绝；运行中可停止，也可从历史会话继续交流。

可以添加附件，也可以在设置中管理所选工作区的记忆、编辑 `AGENTS.md`，或切换界面语言。

配置 API：

![桌面端 API 配置](docs/assets/readme/desktop-settings.jpg)

选择项目并输入任务：

![桌面端选择项目并输入任务](docs/assets/readme/desktop-task.jpg)

截图使用示例项目和配置，任务尚未发送。

### 打包应用

在 `desktop` 目录完成上述准备和构建后执行：

```bash
npm run package
open release/GuMi-Code.app
```

应用生成在 `desktop/release/GuMi-Code.app`。如果该路径已存在，先移走旧应用再打包。

更多桌面端使用说明见 [desktop/README.md](desktop/README.md)。

## CLI

需要 Git、Python 3.11 或更高版本，以及支持 Chat Completions 和工具调用的 OpenAI 兼容模型服务。

### macOS / Linux 安装与启动

```bash
git clone https://github.com/GuMiShDo666/GuMi-Code.git
cd GuMi-Code
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/gum-code
```

`python3` 必须指向 Python 3.11 或更高版本。

### Windows PowerShell 安装与启动

```powershell
git clone https://github.com/GuMiShDo666/GuMi-Code.git
Set-Location GuMi-Code
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\gum-code.exe
```

`py -3` 选中的 Python 必须是 3.11 或更高版本。Windows 用法尚未经过实机验证。

### 首次配置

启动后，按提示选择并信任已有项目目录，再填写 API Key、API 地址和模型名称。如果无法自动识别模型的上下文窗口，按提示填写模型的上下文上限。

也可以从仓库根目录复制配置模板，再编辑其中的示例值：

```bash
cp .env.example .env
```

Windows PowerShell 使用 `Copy-Item .env.example .env`。配置字段为 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`；需要手动指定上下文上限时填写 `LLM_CONTEXT_WINDOW`。不要提交真实 API Key。

### 日常使用

启动后直接输入需求，继续输入即可追问或补充要求。常用命令：

| 命令 | 用途 |
| --- | --- |
| `/help` | 查看可用命令 |
| `/sessions` | 管理和恢复历史会话 |
| `/exit` | 退出 |

也可在启动时指定项目目录：

```bash
.venv/bin/gum-code --workspace /path/to/project
```

### 执行一次性任务

macOS / Linux：

```bash
.venv/bin/gum-code --workspace /path/to/project "检查这个项目并说明目录结构。"
```

Windows PowerShell：

```powershell
.\.venv\Scripts\gum-code.exe --workspace "C:\path\to\project" "检查这个项目并说明目录结构。"
```

工作区必须是已存在的目录；非交互终端必须同时提供工作区和任务文本。

### 可选：执行命令

CLI 的普通命令执行需要启动 Docker，并准备可信的本地镜像：

```bash
.venv/bin/gum-code \
  --workspace /path/to/project \
  --allow-run-command \
  --sandbox-image trusted-local-image:tag
```

将镜像名替换为已准备的本地镜像，程序不会自动拉取它。命令逐次审批。Windows 使用 `.\.venv\Scripts\gum-code.exe` 替换可执行文件。
