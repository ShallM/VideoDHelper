# 构建与开发

本仓库交付源码、测试、构建脚本及开发 `.app` 的构建记录。应用仍依赖项目 `.venv`、本机 FFmpeg 与模型，**尚不是可搬到另一台 Mac 直接运行的安装包**。

## 依赖与首次运行

| 依赖 | 说明 |
|---|---|
| macOS 14+ | 最低部署目标，验证环境见各版验收记录 |
| Apple Silicon | MLX / Metal 转录需要；Intel 字幕路径未实机验收 |
| Swift 6 工具链 | 近期 Xcode / Command Line Tools；应用为 Swift 5 语言模式，测试使用 Swift Testing |
| Python 3.12 建议 | 其他版本需确认 MLX / PyTorch 兼容依赖 |
| uv / FFmpeg | 项目虚拟环境、媒体合流与音轨提取 |

```sh
xcode-select --install     # 已安装时跳过，完成系统安装后再继续
brew install python@3.12 uv ffmpeg
git clone https://github.com/ShallM/VideoDHelper.git
cd VideoDHelper
zsh scripts/setup-tools.sh
zsh scripts/build-app.sh
open build/VideoDHelper.app
```

`setup-tools.sh` 默认选择 PATH 中的 `python3`。如需明确使用 3.12，可先执行 `uv venv .venv --python 3.12` 和 `uv pip install --python .venv/bin/python -r requirements.txt`，再构建。顶层依赖固定在 `requirements.txt`，尚未为不同芯片 / Python 组合制作完整传递依赖锁文件。

复用已有 `.pt` Whisper 权重的转换需要 PyTorch：`uv pip install --python .venv/bin/python torch`。没有对应 `.pt` 时直接准备 MLX 权重，不必为该路径安装 PyTorch。模型与媒体不随仓库分发。

## 构建产物

`build-app.sh` 编译 Swift Release 可执行文件，生成 `build/VideoDHelper.app`，复制 Python 工作进程与入口脚本，并进行本机 ad-hoc 签名。

Python 路径仍指向构建机器的项目虚拟环境，移动源码或虚拟环境后应重建。复制到“应用程序”不会自动消除依赖；当前签名不是 Developer ID 签名或 Apple 公证。

有本机 `.env` 时，开发构建记录其位置用于首次配置迁移，不打包密钥内容；CI 中没有该文件。日常使用请在设置页填写凭据。

## 测试

```sh
zsh scripts/swift-project.sh test
PYTHONPATH=backend .venv/bin/python -m unittest discover -s backend/tests -v
python3 scripts/check-publication.py
```

单元测试使用临时目录和模拟 API，不消耗模型额度，也不要求登录或下载权重。`swift-project.sh` 在特定 CLT 安装上使用项目内的 SwiftPM 接口覆盖与测试库路径，不修改系统工具链。

`validate-*`、`benchmark-summary.py` 是另外的真实样本工具，需要本机原稿和服务配置，可能产生实际用量，不属于 CI。历史样本 UUID 用于维护者机器复测，其他机器应换成自己的任务；原始媒体与全文原稿不公开分发。

## GitHub Actions

[macOS CI](https://github.com/ShallM/VideoDHelper/actions/workflows/ci.yml) 在 `macos-15` Apple Silicon runner 上检查公开文件、文档链接和 JSON，运行 Python / Swift 离线测试，构建开发 `.app` 并校验签名，保存日志、环境与 SHA-256，上传开发构建 ZIP。

产物只用于构建验证，未携带 Python 运行时、FFmpeg、模型或用户配置，**不是终端用户安装包**。保留30天，可在成功运行的 Artifacts 区下载；GitHub 下载可能需要登录。正式 Release 待下一阶段可分发打包后提供。

README 的性能图来自公开的验收 JSON，可用 `uv run --no-project --with matplotlib python scripts/render-benchmark.py` 重新生成；绘图库仅用于文档，不是应用运行依赖。

## 常见问题

- **Swift Testing 或工具链接口错误：**确认 `xcode-select -p` 和 `swift --version` 指向近期工具链，再用 `swift-project.sh`。
- **找不到处理引擎：**准备 `.venv` 后重新构建，确认项目和环境没有被移动。
- **Metal 不可用：**在 Apple Silicon 原生 Python 下安装 MLX，不要用 Rosetta 的 x86_64 环境转录。
- **字幕 / 登录 / 下载失败：**更新 B 站会话并确认视频可访问，平台变化可能需要更新 yt-dlp。网络失败不等于没有字幕。
- **模型报错或预算达到上限：**检查默认模型、参数和用量后继续；原稿和已完成阶段保留，更换模型可能需要重新总结。

## 可分发应用的下一阶段

处理运行时与 FFmpeg 的打包及许可、可重定位入口、首次模型准备、不同 Mac 的安装与恢复、应用身份与图标、Developer ID / 公证和发布校验。在干净环境验收后再制作 GitHub Release。
