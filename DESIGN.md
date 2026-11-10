# portfolio-monitor 设计文档

最后更新：2026-11-10

## 1. 目标与范围

- 每天自动汇总所有账户的持仓和现金，统一换算为 **USD**
- 网页：资产分配现状、历史收益率曲线、回撤、风险提示；手动账户可直接在网页编辑
- MCP：Hermes / Claude Code 查询组合状况，并能在确认后修改手动账户
- Telegram（经 Hermes）：看日报、问问题、改持仓
- **不做**：交易下单、投资建议、多用户、注册登录

## 2. 账户与接入方式

| 账户 | 类型 | 接入 | 频率 | 计入收益率 |
|---|---|---|---|---|
| IBKR | 券商 | Flex Web Service（只读 token），可回填最多一年 | 每日自动 | 是 |
| 盈立 HK | 券商 | 官方 Open API「查询持仓」（需申请，审批中） | 每日自动 | 是 |
| OKX | 加密 | API v5 只读 key + 服务器 IP 白名单 | 每日自动 | 是 |
| 手动券商账户（如 A 股） | 券商 | 手动（网页 / Telegram） | 有交易时 | 是 |
| 手动银行账户 | 银行 | 手动 | 每月 | 否，只计入资产分配 |

自动同步账户在系统里**只读**（手改会被下一次同步覆盖）；手动账户才能编辑。

具体的机构清单和服务器信息记在本地 `local/private.md`，不进 git。

## 3. 数据模型：快照，而不是流水

每天对每个账户存一次"当前状态"。市值优先用券商报告的数字，不自己重算。

| 表 | 内容 |
|---|---|
| `accounts` | 账户：名称、机构、类型、数据源（ibkr / usmart / okx / manual）、币种、是否计入收益率 |
| `instruments` | 标的：统一编号、名称、资产类别、地区、行业、币种、扩展属性（JSON，留给期权 / 基金 / 房产等新类型） |
| `positions` | 持仓快照，最细粒度长表（标的 × 账户 × 日期）：数量、价格、市值、成本 |
| `cash_balances` | 现金快照：日期、账户、币种、金额 |
| `manual_holdings` | 手动账户的**当前**持仓和现金；每日快照时复制进上面两张表 |
| `cash_flows` | 资金进出：日期、账户、币种、金额（入正出负）、来源（api / manual） |
| `fx_rates` | 每日汇率（USD 基准） |
| `daily_nav` | 每日组合净值、净入金、日收益、TWR 指数 |
| `sync_runs` | 每次同步：数据源、状态、耗时、错误、原始返回存档路径 |
| `change_log` | 所有修改：时间、来源（web / mcp / sync）、改前、改后、是否已撤销 |
| `pending_changes` | MCP 两步写入的待确认改动（10 分钟过期） |
| `daily_reports` | Hermes 写好的日报正文（网页同步展示） |

- 存储：SQLite 单文件 `data/portfolio.sqlite`
- 原始返回：每次同步的原始 JSON / XML 存档到 `data/raw/<日期>/<数据源>`，便于排查
- 估值日：按美东日期；每日任务在美股收盘后运行
- 手动持仓若填写了行情代码（如 `600519.SS`），每日尝试从 Yahoo 更新价格，失败则沿用上次价格
- **统一标的编号** `<市场>:<代码>`：`US:NVDA`、`HK:00700`（港股补足 5 位）、`CN:600519`、`CRYPTO:BTC`。各数据源在接入层把自己的写法转换成统一编号；识别不了的标的进入待处理列表，不静默丢弃。跨账户合并和集中度计算都依赖它
- **迁移**：数据库结构带版本号，变更用按序号排列的 SQL 迁移脚本（`storage/migrations/0001_init.sql` …），启动时自动执行尚未执行的迁移
- **可重放**：解析逻辑改进或新增字段后，可从 `data/raw/` 的原始存档重新生成历史快照，不必重新抓取

## 4. 收益率口径

- 方法：**时间加权收益率（TWR）**，排除入金 / 出金的影响
  ```
  日收益 r_t = (NAV_t − F_t) / NAV_{t−1} − 1     # F_t：当日净入金，视为收盘时发生
  TWR 指数   = Π (1 + r_t)
  ```
- 范围：只算投资账户（IBKR、盈立、OKX 和手动券商账户）；银行账户不计入，避免工资和日常开销混进收益率
- 资金进出来源：IBKR Flex 现金流水；OKX 充提记录；盈立待确认；手动账户在网页 / Telegram 记录
- 账户之间互转：在组合层面相互抵消
- 历史起点：首次运行日；IBKR 用 Flex 回填最多一年
- 基准对比（可选）：标普 500、恒生指数、沪深 300，取自 Yahoo，拉取失败不影响主流程

## 5. 风险规则（v1）

阈值写在配置里，可调。

| 规则 | 默认阈值（待你确认） |
|---|---|
| 单一标的跨账户合计占投资资产 | > 20% |
| 单一账户 / 平台占总资产 | > 50% |
| 资产类别偏离目标配置 | > 5 个百分点（目标配置待你定） |
| 组合从高点回撤 | > 10% |
| 组合单日跌幅 | > 3% |
| 闲置现金占总资产 | 待定 |
| 数据健康：某数据源连续失败 | ≥ 2 天 |
| 数据健康：总资产单日变化大但无资金进出 | > 15%，疑似数据错误 |

## 6. 架构

```
AWS 服务器（安全组不开放入站端口）
┌──────────────────────────────────────────────────────┐
│ portfolio-monitor（单个 Python 进程，监听 127.0.0.1:8000）│
│  ├─ 网页：仪表盘、账户明细与编辑、修改日志、同步状态   │
│  ├─ /mcp：MCP（streamable HTTP + Bearer token）        │
│  ├─ 定时：每日同步 → 快照 → 汇率 → 净值 → 规则         │
│  └─ SQLite                                             │
│        ▲ MCP（本机）                                   │
│ Hermes 网关 ──┤  Telegram（白名单只有你）、每日日报定时 │
│ Claude Code ──┘  临时深入分析                           │
│ cloudflared ── Cloudflare Tunnel ── portfolio.<你的域名>│
└──────────────────────────────────────────────────────┘
                     ▲ Cloudflare Access（邮箱验证码 / Google 登录）
                  你的 Mac / 手机浏览器
```

技术栈：Python 3.12 + uv、FastAPI、Pydantic、Jinja2 模板 + HTMX（无前端构建）、Tabulator（表格）、ECharts（图表）、SQLite、官方 MCP Python SDK、进程内定时任务。前端库随项目分发，不依赖 CDN。

### 6.1 分层：依赖只能向下

```
入口层（很薄）  网页 │ JSON 接口 │ MCP │ 命令行 │ 定时任务
                   ↓ 只调用服务层
服务层          查询：总览 / 持仓 / 收益 / 通用聚合
                修改：预览 / 确认 / 撤销
                同步：运行各数据源
                   ↓
领域层          纯计算，不碰数据库和网络：
                标的编号规范化、跨账户合并、币种换算、TWR、规则判断
                   ↓
数据层          SQLite 读写 + 迁移 │ 数据源接入 │ 汇率
```

- 入口层只调用服务层，不直接读数据库。新功能写成一个服务函数，网页、JSON、MCP 各包一层薄壳即可
- 领域层不知道网页和数据库的存在，是最容易测试、最稳定的一层

代码目录与分层对应：

```
src/portfolio_monitor/
├── entry/      入口层：web/（路由 + 模板 + 表格定义）  mcp/  cli.py  scheduler.py
├── services/   服务层：query.py  edit.py  sync.py
├── domain/     领域层：models.py  instruments.py  valuation.py  returns.py  rules/
├── sources/    数据源：base.py（接口）  registry.py  ibkr.py  okx.py  usmart.py  manual.py  fake.py
└── storage/    数据层：db.py  repository.py  migrations/
```

### 6.2 扩展点：只留四个

| 以后会加什么 | 扩展点 | 新增一个需要做什么 |
|---|---|---|
| 新的数据源（其他券商、银行、交易所） | `Source` 接口 | 新增一个文件，在 `sources/registry.py` 里加一行 |
| 新的风险规则或指标 | `Rule` 接口 | 新增一个类，阈值写进配置 |
| 新的查询或操作 | 服务函数 | 写一个函数，网页 / JSON / MCP 各包一层 |
| 新的资产类型 | `instruments` 的属性和扩展字段 | 不改表结构 |

```python
class Source(Protocol):
    name: str
    def fetch(self, as_of: date) -> SourceSnapshot: ...
    # SourceSnapshot：持仓、现金、资金进出、原始返回

class Rule(Protocol):
    id: str
    def evaluate(self, ctx: PortfolioContext, settings: dict) -> list[Alert]: ...
```

- 注册表就是普通列表：不做自动发现、不做动态加载
- **契约测试**：每个数据源都要通过同一套测试，用录制的真实返回样本跑。新接一家机构，测试通过即可上线
- **接口用三个差异最大的实例来定**：IBKR（XML 报表，自带历史）、OKX（只有余额，没有"持仓"）、手动账户（数据在本地库里）。M2 先做手动账户 + 假数据源 `fake.py` 验证接口，M5 接入真实数据源时再按需修订

### 6.3 配置

- 仓库里只放 `config.example.toml`（虚构示例）
- 真实配置放 `local/config.toml`：目标资产配置、规则阈值、启用的数据源、手动账户的机构名称
- 凭据只放 `.env`

### 6.4 刻意不做

插件自动发现、事件总线、依赖注入框架、微服务、前后端分离的单页应用、通用规则描述语言、拖拽式报表搭建器 / 用户自定义仪表盘 / 内置 BI。真的需要时再加。

## 7. 网页

| 页面 | 内容 |
|---|---|
| 仪表盘 `/` | 总资产、今日 / 今年收益、风险提示数；资产分配（可切换：资产类别 / 账户 / 币种 / 地区）；前十大持仓（跨账户合并）；收益率曲线（1 月 / 3 月 / 今年 / 1 年 / 全部，可叠加基准）；回撤曲线；今日日报 |
| 账户 `/accounts/<id>` | 持仓与现金明细；手动账户可在线编辑；资金进出记录 |
| 修改日志 `/changes` | 所有修改，可撤销 |
| 同步状态 `/sync` | 各数据源最近运行结果 |

网页本身不做登录，由 Cloudflare Access 把关。

### 7.1 表格与图表：新增一种表格 = 一份配置

```
positions 长表（标的 × 账户 × 日期，带维度属性）
        ↓ 服务层 aggregate(数据, 分组=[...], 筛选=...)
带类型的数据模型（Pydantic）
        ├─→ HTML：Tabulator 表格 / ECharts 图表（dataset 组件，与表格共用同一份数据）
        ├─→ JSON：/api/v1/...（以后换前端或做 App 直接用）
        └─→ MCP：工具返回值
```

各种表格都是同一份数据的不同视图：

| 表格 | 怎么得到 |
|---|---|
| 持仓明细 | 不分组 |
| 按资产类别 / 币种 / 地区 / 账户汇总 | 按对应维度分组 |
| 标的 × 账户交叉表 | 行为标的，列为账户 |
| 某个标的的历史 | 按标的筛选，按日期展开 |
| 月度收益表 | 每日净值按月汇总 |

表格由列定义驱动，放在 `entry/web/tables.py`：

```python
HOLDINGS = TableSpec(
    id="holdings",
    query="holdings",                                    # 对应的服务函数
    group_by_options=["instrument", "account", "asset_class", "currency"],
    columns=[
        Col("name", "名称"),
        Col("market_value_usd", "市值", fmt="money"),
        Col("weight", "占比", fmt="pct"),
        Col("unrealized_pnl", "浮盈", fmt="money", color="sign"),
    ],
)
```

- 新增表格：写一份 `TableSpec`，在页面上放一个位置
- 格式（金额、百分比、正负着色、币种）统一定义一次，所有表格共用
- 临时查询不进项目：在 Telegram / Claude Code 里通过 MCP 问，或本机用 Datasette 打开 SQLite 文件

## 8. MCP 工具

| 读取 | 写入（仅手动账户，两步确认） |
|---|---|
| `get_overview`：总资产、收益、资产分配 | `set_cash_balance`：改现金余额 |
| `get_holdings`：合并持仓，可按账户筛选 | `set_holding` / `remove_holding`：改持仓 |
| `get_performance`：收益率曲线、回撤 | `record_cash_flow`：记录入金 / 出金 |
| `get_risk_alerts`：触发的规则 | `confirm_change`：确认并执行待定改动 |
| `get_daily_summary`：日报所需的结构化数据 | `undo_change`：撤销某次修改 |
| `get_change_log`：修改记录 | `save_daily_report`：保存 Hermes 写好的日报 |

两步写入：写入类工具只返回"改前 → 改后"预览和 `change_id`；调用 `confirm_change(change_id)` 后才落库。所有修改记入 `change_log`。

## 9. 部署（AWS + Cloudflare）

- 服务器：AWS（具体主机见 `local/private.md`）
- 进程托管：Docker 单容器或 systemd + uv，部署时定
- Cloudflare Tunnel：`portfolio.<你的域名>` → `http://127.0.0.1:8000`；ingress 规则对 `/mcp` 返回 404，MCP 不经隧道
- Cloudflare Access：策略只允许你的邮箱；会话时长按需设置
- 备份：每日备份 SQLite；服务器磁盘开启加密

## 10. Hermes 接入

- `~/.hermes/config.yaml` 的 `mcp_servers` 加入 `http://127.0.0.1:8000/mcp`，带 `Authorization: Bearer <MCP_TOKEN>`
- Telegram 网关只允许你的用户 ID
- 定时任务：每个交易日美东 17:45 调用 `get_daily_summary` → 写中文日报 → `save_daily_report` → 推送 Telegram
- 写入类操作一律先发预览，你在 Telegram 回复确认后再调用 `confirm_change`

## 11. 安全清单

- [ ] `.env` 权限 600，不进 git；OKX 只读 key 绑定服务器 IP
- [ ] AWS 安全组无入站规则（SSH 除外，最好限定来源 IP）
- [ ] 应用只监听 127.0.0.1
- [ ] Cloudflare Access 只放行你的邮箱；`/mcp` 不经隧道
- [ ] Telegram 白名单只有你
- [ ] MCP 写入只限手动账户，两步确认，可撤销
- [ ] SQLite 每日备份

## 12. 里程碑

| # | 内容 | 状态 |
|---|---|---|
| M1 | 项目骨架、设计文档；清理本机 Ghostfolio | ✅ |
| M2 | 核心：数据库与迁移、标的编号规范化、`Source` 接口 + 手动账户 + 假数据源、每日快照、汇率、TWR、通用聚合 | |
| M3 | 网页：仪表盘、表格组件（列定义驱动）、手动账户编辑、修改日志、JSON 接口 | |
| M4 | MCP 端点，本机用 Claude Code 测试 | |
| M5 | 数据源：IBKR（含一年回填）→ OKX → 盈立 | |
| M6 | 风险规则 | |
| M7 | 部署 AWS + Cloudflare Tunnel / Access + 备份 | |
| M8 | Hermes：MCP、Telegram 白名单、每日日报 | |

## 13. 待你确认

- 用哪台 AWS 服务器；Hermes 是否已安装
- 目标资产配置（股票 / 债券 / 现金 / 加密的比例）和第 5 节的阈值
- 日报时间（默认美东 17:45，交易日）
- 盈立 Open API 审批进度

## 14. 决策记录

| 决策 | 理由 |
|---|---|
| 自建，不用 Ghostfolio | 核心功能只占其代码约 20%；它以交易流水为模型，盈立 / OKX / 手动账户要转成虚构流水，最易出错；需要登录、3 个容器、发版频繁（近 90 天 58 次）。接入层不稳定的问题它本来就不解决 |
| 快照模型 | 所有数据源都能直接给出当前持仓；不用重建成本和流水 |
| 市值用券商报告值 | 少一个行情依赖，少一类错误 |
| TWR 只算投资账户 | 排除入金和工资、消费的干扰 |
| 网页与后端同进程，部署在 AWS | 数据、Hermes、Claude Code 都在 AWS；拆到 Cloudflare Pages 要暴露公网 API、两套鉴权、两次部署 |
| Cloudflare Tunnel + Access | 已有 Cloudflare 账号和域名；不开放入站端口；手机电脑免安装。代价：流量经 Cloudflare 解密 |
| 语言层交给 Hermes | Telegram 对话、日报撰写、推送都由 Hermes 完成；本项目只负责确定性的数据、规则和图表 |
| 四个扩展点，不做插件框架 | 单人工具、两三千行代码；通用扩展机制本身就是 bug 来源。"新增文件 + 注册一行"最简单可靠 |
| 统一标的编号 | 同一标的在不同账户写法不同（如 `0700.HK` / `00700` / `700`），事后补救最痛苦 |
| 分层，入口层只调服务层 | 新功能写一次，网页、JSON、MCP 同时可用；领域层纯计算，易测试 |
| 最细粒度长表 + 派生表格 | 各种表格都是"筛选 + 分组 + 选列"，新增表格不改数据层 |
| 表格由列定义驱动（Tabulator） | 新增表格只写配置；格式统一；无需前端构建 |
