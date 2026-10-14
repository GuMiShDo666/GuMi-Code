# GuMi-Code 桌面端

在桌面界面中选择本地项目，与 AI 交流需求、阅读和修改代码、添加附件，并管理历史会话和项目设置。

当前支持 macOS Apple Silicon。已有 `GuMi-Code.app` 时，直接打开即可。

## 从源码启动

需要 Git 和 Node.js 24。从仓库根目录执行：

```bash
cd desktop
npm ci
npm run prepare:runtime
npm run build
npm start
```

首次准备运行环境需要联网下载 Python 和依赖，无需另外安装 Python。

## 使用

1. 进入「设置 → GuMi-Code 设置」，填写 API 地址、模型名称和 API Key，保存后可测试连接。模型服务需支持 OpenAI 兼容的 Chat Completions 和工具调用。
2. 返回对话页，选择并信任项目文件夹。
3. 输入需求并发送，可添加附件；需要执行命令时，按提示批准或拒绝，运行中可停止。
4. 从历史会话继续交流。在设置中可管理所选工作区的记忆、编辑 `AGENTS.md`，或切换语言。

配置 API：

![桌面端 API 配置](../docs/assets/readme/desktop-settings.jpg)

阅读项目并输出分析：

![桌面端工作画面](../docs/assets/readme/desktop-working.jpg)

截图使用示例项目与本地模拟模型，文件工具由真实应用执行。

## 打包应用

完成上述准备和构建后，在 `desktop` 目录执行：

```bash
npm run package
open release/GuMi-Code.app
```

输出为 `desktop/release/GuMi-Code.app`。如果该路径已存在，先移走旧应用再打包。

项目作用和 CLI 使用说明见[主 README](../README.md)。
