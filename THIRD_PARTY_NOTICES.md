# 第三方许可与设计参考

VideoDHelper 的原创部分使用 [PolyForm Noncommercial 1.0.0](LICENSE)。该许可不改变第三方代码、工具或模型的原有许可。仓库不包含模型权重、Python 环境、FFmpeg 或 yt-dlp 二进制。

## 代码来源与直接依赖

| 组件 | 用途与许可 |
|---|---|
| [MLX Examples / mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) | Apple Silicon 转录；`backend/local_models.py` 的权重转换布局参考其转换实现。[MIT 许可副本](docs/licenses/mlx-examples-MIT.txt)。 |
| [OpenAI Whisper](https://github.com/openai/whisper) | Whisper 模型架构、模型下载地址、摘要与 alignment-head 元数据；这些模型常量用于本地权重校验和转换。[MIT 许可副本](docs/licenses/whisper-MIT.txt)。 |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 获取媒体和字幕信息；项目源码主要使用 [Unlicense](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE)，部分文件及发行二进制含不同许可组件。当前由用户在虚拟环境安装，未将其打包进仓库。 |
| [FFmpeg](https://ffmpeg.org/) | 音轨提取与媒体合流，由本机提供。具体许可取决于构建选项及所含组件；分发前需核对 [FFmpeg 法律说明](https://ffmpeg.org/legal.html) 和实际二进制。 |
| [PyTorch](https://github.com/pytorch/pytorch) | 可选，仅复用 `.pt` 模型时用于转换；遵循其 [许可及第三方条款](https://github.com/pytorch/pytorch/blob/main/LICENSE)。 |

应用构建会携带本项目许可、此说明及上列 MIT 许可副本。下一阶段若内置第三方运行时、依赖或媒体工具，需按实际版本补齐许可、源代码提供方式及其他分发要求。

## 设计参考

下载交互参考 [下载器调研](docs/downloader-research.md)，模型管理参考 [设置设计](docs/settings-and-usage.md)，总结架构参考 [全文理解调研](docs/summarization-research-2026-10-25.md)。这些记录区分产品设计经验与代码来源；引用产品名称或链接不表示获得对方背书。

使用者处理的 B 站视频、字幕和生成笔记不由本项目授予内容版权。使用与分享相关内容时，应遵守原作者授权及平台规则。本项目与 Bilibili、Apple、OpenAI 或上述项目均无官方关联。
