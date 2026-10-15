# voice-style-web

本地配音 Web：输入普通话文案和参考人物音频，确认武汉话表达稿后生成、试听、返修并导出音频。预留其他语言与模型接口。

当前仅完成 M00 项目治理；尚无业务 Web、真实语音生成或声音效果验收结果。

- [方案、形成过程和决策记录](docs/DECISIONS.md)
- [优化与修复记录](docs/OPTIMIZATIONS.md)
- [分模块开发及验收](docs/DEVELOPMENT.md)
- [团队与方案监督员](docs/TEAM.md)
- [代理必须遵守的规则](AGENTS.md)

## 开发检查

需要 Python 3.9+、Git。治理检查只使用 Python 标准库。

```sh
sh scripts/install-hooks.sh
python3 scripts/quality_gate.py quick
python3 scripts/quality_gate.py full --no-review
python3 scripts/quality_gate.py digest
python3 scripts/quality_gate.py full
```

每次完成一个有意义的代码片段执行 quick；模块结束运行 full，并取得独立方案监督员对当前内容摘要的审查。Git 提交与推送 Hook 会复查。

Codex 项目 Hook 定义在 `.codex/hooks.json`。首次或定义变化后，需在 Codex CLI 的 `/hooks` 中审阅并信任；未信任时不会自动执行。Git Hook 安装与 Codex 信任是两件事。详见 [OpenAI 官方 Hooks 文档](https://learn.chatgpt.com/docs/hooks)。

公开仓库仅存源码、合成测试和脱敏文档；真实声音样本、输出音频、模型、数据库与密钥放在忽略目录。未选择开源许可证前，不宣称额外授予开源使用权。
