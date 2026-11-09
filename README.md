# portfolio-monitor

个人多账户投资组合监测：每天汇总 IBKR、盈立、OKX 和手动维护的银行 / A 股账户，生成资产分配、收益率曲线和风险提示；网页可编辑手动账户；通过 MCP 供 Hermes / Claude Code 查询和修改，Telegram 里看日报。

设计与已定决策见 [DESIGN.md](DESIGN.md)。

## 本地开发

```bash
uv sync
uv run portfolio-monitor
```

凭据：复制 `.env.example` 为 `.env` 后填写，`.env` 不进 git。
