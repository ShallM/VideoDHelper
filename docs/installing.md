# 安装 VideoDHelper

## 系统要求

- Apple Silicon Mac（M 系列），macOS 14 或更新版本。
- 应用约 1.1 GB；本地转录模型与下载的媒体需要额外空间。
- AI 整理需要自己配置 OpenAI 兼容的模型服务；转录在本机使用 MLX / Metal。

**不支持 Intel Mac、Windows 或 Linux。** DMG 已包含 Python、MLX、yt-dlp 和 FFmpeg，不需要安装 Homebrew、Python 或开发工具。Whisper 权重不包含在安装包中：首次转录会优先复用已有权重，否则联网准备模型；准备完成后可以离线转录本地音频。

## 拖拽安装

1. 从 [GitHub Releases](https://github.com/ShallM/VideoDHelper/releases/latest) 下载 `VideoDHelper-0.6.0-macOS-arm64.dmg`。
2. 双击 DMG，在出现的窗口里把 **VideoDHelper 拖到 Applications**。
3. 等待复制结束，在 Finder 边栏推出 **VideoDHelper** 磁盘映像。
4. 从“应用程序”打开 VideoDHelper。日常使用打开这份应用，无须再次挂载 DMG。

![实际 DMG 安装窗口](images/dmg-installer.png)

DMG 是只读磁盘映像，挂载后在 Finder 中看起来像一个外置磁盘；拖拽实际把 `.app` 文件夹复制到 `/Applications`。推出映像不会删除已安装的应用，安装完成后也可删除 DMG 文件。

## 本版未公证：首次打开

**0.6.0 使用 ad-hoc 签名，没有 Apple Developer ID 签名，也没有经过 Apple 公证。** 从网络下载后，macOS 默认安全检查可能阻止首次打开。这与本地构建能否启动是不同的检查。

仅在确认下载来源与文件可信后：先尝试打开应用，再进入 **系统设置 → 隐私与安全性**，找到该应用的提示并选择 **仍要打开 / Open Anyway**，按系统提示确认。具体按钮取决于系统版本，参见 [Apple 官方说明](https://support.apple.com/en-us/102445)。如果系统或组织策略不允许打开，本版无法提供默认验证放行。

本项目不要求全局关闭 Gatekeeper、不修改系统安全设置，也不提供批量删除隔离标记的安装脚本。ad-hoc 签名验证包内容是否完整，不证明开发者身份或 Apple 已审核。

## 首次配置

按 `⌘,` 打开设置，在“模型服务”添加 Base URL、API Key 与模型 ID，连接测试成功后保存。单价暂时未知可留空；之后在价格表手动填写，应用换算用量与费用。没有配置价格不会显示成免费。

粘贴 B 站分享链接，读取信息；需要登录时使用应用内的 B 站官方登录页。选择字幕优先或本地 Whisper、媒体规格及保留方式，然后生成学习笔记。完整操作见 [使用指南](user-guide.md)。

## 升级、数据与卸载

升级前退出应用，把新版本拖入“应用程序”并替换旧版本。任务、项目、API 配置和模型保存在 `~/Library/Application Support/VideoDHelper/`，升级不会主动清空该目录。开发版与发行版共用此目录，已有任务可以继续读取。

退出会先停止本应用启动的工作进程，并给检查点留出保存时间；再次打开可继续任务。正在请求但尚未收到结果的模型调用可能需要重试，也可能已由供应商计费；已完成的本机检查点会复用。

卸载时退出应用，再把 `/Applications/VideoDHelper.app` 移到废纸篓。个人数据默认保留；如也要移除数据，请先导出所需笔记，并在设置的存储位置中找到自己的数据目录后自行处理。数据与凭据说明见 [隐私文档](privacy.md)。

## 本机开发预览版

维护者的日常应用保持最新的已验证预览版，用于在正式合并前体验功能。每轮开发完成后更新同一个 `/Applications/VideoDHelper.app`，无须并排安装不同版本；PR 保持待合并，待维护者确认后再完成正式发行。这个流程由开发交付时执行，应用目前没有后台自动升级服务。

| 内容 | 位置与保留规则 |
| --- | --- |
| 日常使用的应用 | `/Applications/VideoDHelper.app`，始终一份，原位更新 |
| 上一版应用的回退包 | 项目内 `local-data/preview-install/previous.zip`，只保留一份压缩包；版本与校验信息在 `previous.json` |
| 任务、设置、登录信息与模型 | 原 `~/Library/Application Support/VideoDHelper/`，与应用包分开保存 |
| 开发日志与验收资料 | 保留对应开发记录、截图、报告；不再使用的候选 / 测试 `.app` 可清理 |
| 正式版本 | `dist/` 保留最近发行产物，历史版本由 GitHub Releases 和 Git 标签追溯 |

替换前验证候选包，并准备上一版的备份；安装后确认启动、任务读取、相关功能与退出正常，再轮换旧回退包。回退时退出应用，将压缩包解开后替换安装副本。回退包只恢复应用程序；如果数据格式发生不兼容迁移，需要使用对应的数据备份，不能假定旧应用一定可以读取新数据。

2026-10-28 的本地预览显示为 **0.6.1（构建 8）**，对应 `c9adfa6` 的任务归档与删除功能；其渠道记录为 `local-preview`。当前 GitHub 正式 Release 仍为 **0.6.0**，两者不混同。后续预览以实际安装记录为准。

## 核验与其他附件

Release 提供 `SHA256SUMS.txt`、`build-manifest.json`、`third-party-components.json` 及对应第三方源码压缩包。下载到同一目录后可用 `shasum -a 256 -c SHA256SUMS.txt` 核对；只下载 DMG 时，可用 `shasum -a 256 VideoDHelper-0.6.0-macOS-arm64.dmg` 与清单对应行比较。

源码附件供许可核对与重建使用，**安装只需要 DMG**。构建过程见 [构建说明](building.md)，实测范围见 [0.6 验收](validation-0.6.md)。
