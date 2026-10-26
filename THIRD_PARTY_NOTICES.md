# 第三方许可与设计参考

VideoDHelper 的原创部分使用 [PolyForm Noncommercial 1.0.0](LICENSE)。该许可不改变第三方代码、工具或模型的原有许可，也不把非商业限制加到第三方组件上。

Git 仓库包含源码和打包配置；0.6.0 DMG 另外携带下列运行组件。模型权重、个人配置与媒体不随仓库或安装包分发。

## 发行包的组件

| 组件 | 用途与许可 |
|---|---|
| [CPython / python-build-standalone](https://github.com/astral-sh/python-build-standalone) | 可重定位 Python 3.12.15；PSF 及构建依赖各自许可。应用 `Contents/Resources/licenses/` 保留 Python 和构建依赖许可。 |
| [MLX](https://github.com/ml-explore/mlx) / [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) | MLX 0.32.4、mlx-whisper 0.4.3，Apple Silicon / Metal 推理，MIT。`backend/local_models.py` 的转换布局参考其实现；[MIT 许可副本](docs/licenses/mlx-examples-MIT.txt)。 |
| [OpenAI Whisper](https://github.com/openai/whisper) | 模型架构、下载地址、摘要与 alignment-head 元数据；模型常量用于本地校验和转换。[MIT 许可副本](docs/licenses/whisper-MIT.txt)。模型首次使用时单独准备。 |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 2026.10.21 Python wheel，获取媒体和字幕；主要为 Unlicense，部分文件适用 MIT、BSD 或 ISC 等许可，完整声明保留在 wheel 的许可文件中。未使用其他项目的打包下载器二进制。 |
| [FFmpeg](https://ffmpeg.org/) | 9.0.2，独立的 `ffmpeg` / `ffprobe` 命令行程序，由本项目从对应源码构建；LGPL 2.1 或更新版本。禁用 GPL、nonfree、version3 与自动外部库探测，不依赖 Homebrew。 |
| [PyTorch](https://github.com/pytorch/pytorch) | 2.14.1，用于复用 `.pt` 模型时的转换；推理使用 MLX。BSD 风格主许可及所含第三方条款均保留。 |
| 其他 Python 依赖 | 完整版本及 wheel SHA-256 见 [依赖锁文件](packaging/requirements.lock)；完整清单在 Release 的 `third-party-components.json`，许可文件保留在应用各包的 `dist-info` 中。 |

应用的“帮助 → 许可证与第三方组件”打开离线索引 `Contents/Resources/LICENSES.html`，包含许可证与包内相对路径。Python 的所有构建依赖许可另列于该页，不能只以主包许可替代它们。

## FFmpeg 源码、修改与重建

本项目与 FFmpeg 通过子进程、文件和命令行交互，未把 FFmpeg 库链接进 VideoDHelper。FFmpeg 的内部组件静态链接在其自身可执行文件中，没有将 GPL / nonfree 编码库加入发行包；用于媒体合流的流复制和 PCM 音频提取。

每个二进制 Release 同时提供 `VideoDHelper-0.6.0-third-party-sources.tar.xz`，含 **FFmpeg n9.0.2 完整对应源码归档**、Python standalone 构建项目源码、构建脚本、运行时版本配置和 Python 依赖锁。源码下载与应用二进制在同一 Release，均提供 SHA-256。

FFmpeg 上游功能源码没有补丁；构建前将上游 `RELEASE` 复制为 `VERSION`，防止所在父目录的 Git 信息被误识别为 FFmpeg 版本。确切选项见 [build-ffmpeg.sh](packaging/build-ffmpeg.sh)，下载地址与摘要见 [runtime.json](packaging/runtime.json)。源码包解压后可用 Apple Silicon 原生 Clang / make 运行 `zsh build/build-ffmpeg.sh <解压后的 FFmpeg 源码目录> <输出目录>` 重建；架构与最低系统均为 arm64 / macOS 14。

可以依据 FFmpeg 自身 LGPL 条款修改和重新分发 FFmpeg，包括对第三方组件享有的调试与修改权利；VideoDHelper 的非商业许可不限制这些权利。修改应用内组件会使现有包签名失效，应自行重新签名测试。

## 设计参考

下载交互参考 [下载器调研](docs/downloader-research.md)，模型管理参考 [设置设计](docs/settings-and-usage.md)，总结架构参考 [全文理解调研](docs/summarization-research-2026-10-25.md)。这些记录区分产品设计经验与代码来源；引用名称或链接不表示获得对方背书。

使用者处理的视频、字幕和生成笔记不由本项目授予内容版权。使用与分享相关内容时，应遵守原作者授权及平台规则。本项目与 Bilibili、Apple、OpenAI 或上述项目均无官方关联。
