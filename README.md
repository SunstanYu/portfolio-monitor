# portfolio-monitor

个人多账户投资组合监测：每天汇总 IBKR、盈立、OKX 和手动维护的银行 / A 股账户，生成资产分配、收益率曲线和风险提示；网页可编辑手动账户；通过 MCP 供 Hermes / Claude Code 查询和修改，Telegram 里看日报。

设计与已定决策见 [DESIGN.md](DESIGN.md)。

## 快速体验（虚构数据）

```bash
uv sync
uv run portfolio-monitor demo --days 180 --serve
```

用假数据源 + 手动账户跑完整流程（快照 → 汇率 → 净值 → TWR → 聚合），数据写在 `data/demo/`；加 `--serve` 后打开 http://127.0.0.1:8000 看网页，手动账户（示例 A 股、示例银行）可以直接在网页上修改。MCP 也会开启：没设 `MCP_TOKEN` 时临时生成一个，终端里会打印接入 Claude Code 的命令。

## 命令

```bash
uv run portfolio-monitor sync [--date YYYY-MM-DD] [--source ibkr]   # 每日同步；有失败时退出码为 1
uv run portfolio-monitor backfill ibkr [--days 365]     # 回填历史净值和资金进出（IBKR 最多一年）
uv run portfolio-monitor overview [--date ...]          # 概览：总资产、收益、回撤、资产分配
uv run portfolio-monitor holdings --by asset_class      # 持仓：instrument_id / account_id / asset_class / currency / region / sector
uv run portfolio-monitor performance --last 20          # TWR 收益率序列
uv run portfolio-monitor migrate                        # 初始化 / 升级数据库
uv run portfolio-monitor serve [--host 127.0.0.1 --port 8000]   # 启动网页和 MCP（默认只监听本机）
```

`serve` 从环境变量读 `MCP_TOKEN`，本机可以用 uv 直接加载 `.env`：`uv run --env-file .env portfolio-monitor serve`。

## 网页

| 页面 | 内容 |
|---|---|
| `/` | 总资产与收益、收益率和回撤曲线、资产分配、账户、月度收益 |
| `/accounts/<id>` | 持仓、现金、资金进出；手动账户可新增 / 修改 / 删除持仓、改现金、记入金出金 |
| `/changes` | 修改记录，可撤销 |
| `/sync` | 各数据源每次同步的结果 |

JSON 接口在 `/api/v1/...`（概览、持仓、曲线、月度收益、表格、修改记录）；写入分两步：`POST /api/v1/changes/preview` 返回改前 → 改后，`POST /api/v1/changes/<id>/confirm` 后才生效。

## MCP

`/mcp`（Streamable HTTP），设置了 `MCP_TOKEN` 才开放，只接受本机访问。工具列表见 [DESIGN.md §8](DESIGN.md#8-mcp-工具)；写入同样两步：写入类工具返回预览和 `change_id`，`confirm_change` 后才生效。

接入 Claude Code（token 用 `openssl rand -hex 32` 生成，写进 `.env`）：

```bash
claude mcp add --transport http portfolio http://127.0.0.1:8000/mcp --header "Authorization: Bearer $MCP_TOKEN"
```

## IBKR

用 Flex Web Service 只读取数（Client Portal → Performance & Reports → Flex Queries）：

1. 新建 Activity Flex Query，勾选 Open Positions、Cash Report、Cash Transactions、Transfers、Net Asset Value (NAV) in Base；Account Information 不需要（含个人信息）
2. 开启 Flex Web Service，生成 token；把 token 和 Query ID 写进 `.env`（`IBKR_FLEX_TOKEN`、`IBKR_FLEX_QUERY_ID`）
3. `local/config.toml` 里加一个 `source = "ibkr"` 的账户（写法见 `config.example.toml`）
4. 先回填一年，再每日同步：

```bash
uv run --env-file .env portfolio-monitor backfill ibkr
uv run --env-file .env portfolio-monitor sync
```

IBKR 当天的报表次日才能取到：当天同步时自动退到最近一个有数据的交易日，并标记为沿用；次日早上可以只给 IBKR 补跑前一天（`sync --date <前一日> --source ibkr`）。

## 配置与凭据

- 配置：复制 `config.example.toml` 到 `local/config.toml` 后修改（`local/` 不进 git）
- 凭据：复制 `.env.example` 为 `.env` 后填写（`.env` 不进 git）
- 数据：默认在 `data/`，可用环境变量 `PM_DATA_DIR` 指定

## 测试

```bash
uv run pytest
```

新增数据源必须在 `tests/test_source_contract.py` 里登记并通过契约测试。
