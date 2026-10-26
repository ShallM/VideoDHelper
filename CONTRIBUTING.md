# 参与开发

欢迎提交问题、文档改进与代码补丁。请先阅读[许可证](LICENSE)：项目为非商业用途的源码可见许可，商业使用需另行授权。

## 本地检查

按[构建说明](docs/building.md)准备环境，然后运行：

```sh
zsh scripts/swift-project.sh test
PYTHONPATH=backend .venv/bin/python -m unittest discover -s backend/tests -v
python3 scripts/check-publication.py
zsh scripts/build-app.sh
```

普通测试使用模拟模型，不需要密钥。真实转录 / 总结会消耗网络、硬盘和模型额度，应单独安排，并记录来源、模型、用量、耗时与重试口径。

## 提交内容

- 说明触发操作、改后行为与验证结果；UI修改提供不含私人信息的截图。
- 总结改动保留原稿和引用映射，验证中断后复用，不以关键词命中率替代语义质量。
- 功能同步更新指南和版本日志，架构取舍写入开发记录。
- 不提交 `.env`、API Key、Cookie、个人任务、媒体、模型或原始响应。失败样本先脱敏。
- 保留第三方代码许可；本项目的非商业限制不改写依赖许可。

自动检查只发现明显误提交，不能保证覆盖所有敏感信息。提交前仍应审阅差异。
