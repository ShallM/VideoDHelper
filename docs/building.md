# 构建与开发

普通用户使用 [Release DMG](installing.md)，无需开发依赖。本文区分自包含发行包与依赖项目环境的快速开发构建，两者均仅面向 Apple Silicon。

## 依赖与首次运行

| 依赖 | 说明 |
|---|---|
| macOS 14+ | 最低部署目标，验证环境见各版验收记录 |
| Apple Silicon | M 系列 Mac 原生工具链；不支持 Intel / Rosetta 构建 |
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

## 快速开发构建产物

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

产物只用于构建验证，未携带 Python 运行时、FFmpeg、模型或用户配置，**不是终端用户安装包**。保留30天，可在成功运行的 Artifacts 区下载；GitHub 下载可能需要登录。普通用户请下载独立 [Release DMG](https://github.com/ShallM/VideoDHelper/releases/latest)。

README 的性能图来自公开的验收 JSON，可用 `uv run --no-project --with matplotlib python scripts/render-benchmark.py` 重新生成；绘图库仅用于文档，不是应用运行依赖。

## 常见问题

- **Swift Testing 或工具链接口错误：**确认 `xcode-select -p` 和 `swift --version` 指向近期工具链，再用 `swift-project.sh`。
- **找不到处理引擎：**准备 `.venv` 后重新构建，确认项目和环境没有被移动。
- **Metal 不可用：**在 Apple Silicon 原生 Python 下安装 MLX，不要用 Rosetta 的 x86_64 环境转录。
- **字幕 / 登录 / 下载失败：**更新 B 站会话并确认视频可访问，平台变化可能需要更新 yt-dlp。网络失败不等于没有字幕。
- **模型报错或预算达到上限：**检查默认模型、参数和用量后继续；原稿和已完成阶段保留，更换模型可能需要重新总结。

## 自包含发行构建

在 Apple Silicon 上安装 Xcode / CLT（Clang、Swift、make）与 uv，然后运行：

```sh
python3 scripts/build-distribution.py
python3 scripts/validate-distribution.py dist/VideoDHelper.app --report build/distribution-report.json
```

只需 `.app` 时加 `--app-only`。首次构建下载固定 SHA-256 的 CPython standalone 和 FFmpeg 源码，编译 FFmpeg，安装完整哈希锁定的 Python 依赖；之后复用 `build/distribution-deps/`。使用 `MACOSX_DEPLOYMENT_TARGET=14.0` 限制 wheel 选择，避免构建机器较新导致选中只能在 macOS 26 运行的 MLX。

`dist/` 包含 `.app`、DMG、第三方源码包、版本 / 组件清单与校验和。发行包使用相对路径、Python `-I -B`、包内 FFmpeg 和证书库，不包含或迁移开发 `.env`，不依赖项目目录或 Homebrew。模型权重和个人配置单独保留在用户数据目录。

`runtime.json` 是发行版本、最低系统和下载摘要的来源；`packaging/requirements.lock` 固定全部传递依赖及哈希。更新依赖后需重新构建、运行安装验收，并同步第三方清单。修改版本时也同步开发构建脚本与文档。

验证脚本检查所有 Mach-O 的架构、最低系统、外部动态库与运行时搜索路径、签名和符号链接，使用系统 PATH 执行包内 Metal 检查、FFmpeg 与本机模拟 API，复跑任务验证检查点不重复调用。它不调用付费模型、不需要 B 站账号。`--ui-fixture <绝对测试目录>` 可启动带延迟的本机模拟接口，供 GUI 退出 / 恢复验收；测试时用 `VIDEOHELPER_DATA_DIRECTORY` 指向该目录，避免触及个人任务。

`build-distribution.py` 使用 ad-hoc 签名，不伪装 Developer ID / 公证。发行前还需执行 Finder 拖拽安装、推出 DMG 后从 `/Applications` 启动、重复开关窗口、工作进程退出与内存观察；实际记录见 [0.6 验收](validation-0.6.md)。CI 当前仍验证开发构建，完整 DMG 在本机经过独立验收后发布。
