# portfolio-monitor

个人多账户投资组合监测：每天汇总 IBKR、盈立、OKX 和手动维护的银行 / A 股账户，生成资产分配、收益率曲线和风险提示；网页可编辑手动账户；通过 MCP 供 Hermes / Claude Code 查询和修改，Telegram 里看日报。

设计与已定决策见 [DESIGN.md](DESIGN.md)。

## 快速体验（虚构数据）

```bash
uv sync
uv run portfolio-monitor demo --days 180
```

用假数据源 + 手动账户跑完整流程（快照 → 汇率 → 净值 → TWR → 聚合），数据写在 `data/demo/`。

## 命令

```bash
uv run portfolio-monitor sync [--date YYYY-MM-DD]       # 每日同步；有失败时退出码为 1
uv run portfolio-monitor overview [--date ...]          # 概览：总资产、收益、回撤、资产分配
uv run portfolio-monitor holdings --by asset_class      # 持仓：instrument_id / account_id / asset_class / currency / region / sector
uv run portfolio-monitor performance --last 20          # TWR 收益率序列
uv run portfolio-monitor migrate                        # 初始化 / 升级数据库
```

## 配置与凭据

- 配置：复制 `config.example.toml` 到 `local/config.toml` 后修改（`local/` 不进 git）
- 凭据：复制 `.env.example` 为 `.env` 后填写（`.env` 不进 git）
- 数据：默认在 `data/`，可用环境变量 `PM_DATA_DIR` 指定

## 测试

```bash
uv run pytest
```

新增数据源必须在 `tests/test_source_contract.py` 里登记并通过契约测试。
