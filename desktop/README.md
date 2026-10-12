# GuMi-Code 桌面端

沿用 GuMi-Workbench 当前的对话和设置界面，仅保留 GuMi-Code 和设置。Python 源码来自本仓库 `src/gum_code`，CLI 命令和 Docker 服务能力继续保留；桌面命令在本机执行并逐次审批。

## 开发和打包

当前支持 macOS Apple Silicon，Node.js 24。依赖版本与工作台保持一致。

```sh
cd desktop
npm ci
npm run prepare:runtime
npm run build
npm start
npm test
npm run package
```

打包默认生成 `desktop/release/GuMi-Code.app`，拒绝覆盖现有应用。也可指定全新的绝对 `.app` 输出路径：`node package-app.cjs --output /absolute/path/GuMi-Code.app`。程序携带 Python 和引擎源码，运行时不依赖工作台目录。运行 `node scripts/check-bundle.cjs /absolute/path/GuMi-Code.app` 检查搬移后的签名、离线合成模型请求和历史恢复；这不代表真实模型或第二台电脑验收。

## 本机数据迁移

1. 保存编辑并关闭 GuMi-Workbench。
2. 打开 GuMi-Code → 设置 → GuMi-Code 设置 → 从工作台迁移。
3. 选择原工作台应用数据目录，通常为 `~/Library/Application Support/GuMi-Workbench`，确认原程序已关闭。macOS 可能要求钥匙串授权。

复制会话、分段历史、记忆和当前共用 API 配置，保留会话/工作区 UUID 和关联关系。旧格式历史只在目标暂存副本中转换。API 凭据经主进程复制到 GuMi-Code 独立钥匙串槽位，界面和文件不包含密钥。源数据和源槽位保留，两端之后独立保存；不会复制项目源码、工作间、账本或助手数据。

目标必须为空。重复导入显示已完成，不覆盖已有记录。源目录有未完成的 SQLite 日志、活跃会话、未知文件、符号链接或迁移中变化时会拒绝导入；失败撤销新建凭据及暂存副本。请处理原程序状态后重试，不手动删除数据库侧文件。

默认数据目录为 `~/Library/Application Support/GuMi-Code`。开发验收使用 `GUMI_CODE_PROFILE=/absolute/disposable/profile npm start`；测试必须创建新合成目录，不能复制真实凭据到测试环境。工作区移动后需要重新选择文件夹。语言设置在本机独立保存。

## 检查

```sh
npm test
npm run build
# 仓库根目录，使用已有 Python/pytest 环境
PYTHONPATH=src python3 -m pytest -q checks
```

桌面呈现沿用 Spectrum UI 的 Apache-2.0 授权素材，见 `third-party/Spectrum-UI-NOTICE.md`。Python 和 Electron 的依赖许可证保留在打包分发中。应用未公证；不包含 Windows/Linux 构建。

实现边界与检查要求见 [contracts.md](docs/contracts.md)。
