# Bilibili 下载工具调研与本项目取舍

核对日期：2026-10-25。关注度用 GitHub Star 作为可复核的近似指标，再结合维护状态和 macOS 适配；它不是全社区用户数，也不能直接证明下载成功率。数字来自 GitHub 官方仓库 API，当日快照保存于本机 `local-data/research/github.json`。

| 项目 | Star | 当日状态 | 对本项目的意义 |
| --- | ---: | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 201,619 | 活跃；最近推送 10 月 23 日 | 通用多站点获取引擎，关注度最高，但不能把全部用户等同于 B 站用户。继续作为可替换的获取组件。 |
| [Bilibili-Evolved](https://github.com/the1812/Bilibili-Evolved) | 30,882 | 活跃；最近推送 10 月 17 日 | 浏览器增强脚本；功能按组件组织，适合参考渐进展开设置的方式。不是专门的下载应用。 |
| [哔哩下载姬 downkyi](https://github.com/leiurayer/downkyi) | 24,371 | README 明确停止维护，默认分支为 Deprecated | B 站专用桌面下载器中历史关注度很高。维护状态比历史热度更影响今天的依赖选择。 |
| [BBDown](https://github.com/nilaoda/BBDown) | 13,858 | 2026-05-14 归档，默认分支 DELETED | 知名 B 站 CLI 的历史参考，本次不引入为运行依赖。 |
| [Bili23-Downloader](https://github.com/ScottSloan/Bili23-Downloader) | 8,560 | 活跃；最近推送 10 月 24 日；支持 macOS | 本轮重点交互参考：真实规格预览、音视频分离、任务恢复与历史。 |
| [BiliTools](https://github.com/btjawa/BiliTools) | 5,043 | 2026-07-07 归档，明确停止维护 | 旧资料仍可能推荐它；本次不作为持续维护的技术底座。 |

结论：没有合理依据宣布某一个工具在所有类别里“认可度绝对最高”。通用引擎优先看 yt-dlp；浏览器增强看 Bilibili-Evolved；在此次核查的仍活跃且支持 macOS 的 B 站下载应用中，Bili23 最值得优先研究。搜索到的 `FairyWorld/bili_downkyicore` 是低关注度派生仓库，不能混作原 downkyi 的社区认可度。

## 实际查看的设计与本次采用方式

已阅读 Bili23 官方 README、官方主界面截图及上述仓库维护说明。Bili23 当前文档已经包含 MCP 下载任务接口，因此“这些工具完全没有 AI 接入”并不准确；它所描述的能力主要是让 AI 客户端操作下载任务，不等于本项目需要的本地语音识别、精简台词和来源可查的学习笔记。

| 观察到的设计 | VideoDHelper 的实现决策 |
| --- | --- |
| 先解析链接，再展示可选择的下载项目与规格 | 先显示标题、作者、时长与当前会话实际返回的格式，不预设不可用的 4K / 8K 按钮。 |
| 画质、视频编码、音质与封装分别表达 | 视频选项同时写明分辨率、帧率、编码、估算体积；音频和 MP4 / MKV 单独选择。所选格式失效时明确失败，不静默换成另一种。 |
| 解析与任务列表分开，任务可恢复 | 左侧保留最近任务；逐阶段保存原稿、模型分段结果和媒体下载进度。取消后可继续。 |
| 音视频流分开获取并合并 | 复用 yt-dlp 的 Bilibili 提取器、断点下载与 FFmpeg 合并；固定项目依赖版本，隔离为后台工作进程。 |
| 较多高级选项逐步展开 | 原生界面保留“文字来源”和“媒体下载”两组处理选项，开始后折叠，阅读三份文字成为主区域。 |

本项目额外实现：字幕优先、中文本地 Whisper、AI 整理、相邻字幕上下文、来源编号检查、超限拆分、长文分层汇总、检查点复用、文字落盘后的媒体清理。这些能力不能用下载器的功能列表代替验收。

## 技术边界

- 借鉴交互和工作流，没有复制上述桌面应用的代码、图标或界面资源。Bili23 和 downkyi 标为 GPL-3.0；后续若直接引入代码，需重新评估对应分发条件。
- 本次支持普通投稿视频或明确的单个分 P 链接；合集队列、番剧、课程、批量处理不在当前验收范围。
- 登录会话只用于 B 站获取。获取能力以当前账号与接口返回为准，不实现越权访问。
- yt-dlp 作为进程和 Python 库使用，FFmpeg 负责媒体处理；版本更新和依赖打包是单独可替换的边界。
- 开发版本依赖当前 Mac 的 Python 环境与 FFmpeg。能从 Finder 启动不代表已经做成可分发、签名公证的安装包。

## 一手来源

- [Bili23 官方 README](https://github.com/ScottSloan/Bili23-Downloader/blob/main/README.md) 与 [官方界面图](https://bili23.scott-sloan.cn/main_interface_cn.png)。
- [Bilibili-Evolved 官方 README](https://github.com/the1812/Bilibili-Evolved/blob/master/README.md)。
- [downkyi 停止维护说明](https://github.com/leiurayer/downkyi/blob/Deprecated/README.md)。
- [BBDown 归档仓库](https://github.com/nilaoda/BBDown)、[BiliTools 归档仓库](https://github.com/btjawa/BiliTools)。
- [yt-dlp 官方文档](https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp) 与 [Bilibili 提取器](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py)。
- [MLX Whisper 官方说明](https://github.com/ml-explore/mlx-examples/blob/main/whisper/README.md) 与 [Turbo 模型页](https://huggingface.co/mlx-community/whisper-large-v3-turbo)。
- [DeepSeek 思考模式参数](https://api-docs.deepseek.com/guides/thinking_mode/)：只能证明官方协议；积算是否转发某项参数必须实测。本次积算响应在设置 thinking.disabled 后仍返回 reasoning tokens，未把“参数被接受”当作关闭生效。
