"""MCP 端点 /mcp：Hermes / Claude Code 查询组合状况，并在用户确认后修改手动账户。

- 工具都是 async：和网页路由一样在事件循环线程里串行访问同一个 SQLite 连接
  （同步函数写的工具会被 SDK 放进线程池，和网页同时用这个连接会出问题）
- 写入分两步：写入类工具只生成预览（change_id + 改前 → 改后），confirm_change 后才生效
- 鉴权：Authorization: Bearer <MCP_TOKEN>。另外 SDK 默认只接受发往本机的请求（Host 白名单），
  经 Cloudflare 隧道进来的请求 Host 是外部域名，会被直接拒绝（421）
"""

import datetime as dt
import functools
import hmac
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from portfolio_monitor import __version__
from portfolio_monitor.app import App
from portfolio_monitor.domain.models import AssetClass
from portfolio_monitor.services.edit import ChangeView, EditError
from portfolio_monitor.services.query import RANGES, AccountSummary, CurvePoint, MonthReturn, Overview

MIN_TOKEN_LENGTH = 32

INSTRUCTIONS = """\
个人投资组合监测。金额字段名带 _usd 的是美元，其余为原币；收益率是时间加权收益率（TWR），已剔除入金出金。
只有手动账户（get_accounts 里 editable=true）可以修改。修改分两步：
1. 调用 set_holding / remove_holding / set_cash_balance / record_cash_flow，得到 change_id 和"改前 → 改后"的预览；
2. 把预览原样告诉用户，等用户明确同意后再调用 confirm_change(change_id)。
待确认的修改 30 分钟后作废。undo_change 会立即撤销一次已生效的修改，调用前同样要先征得用户同意。"""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
PREVIEW = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                          open_world_hint=False)
APPLY = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                        open_world_hint=False)

DateArg = Annotated[dt.date | None, Field(description="估值日 YYYY-MM-DD，默认最新估值日")]
AccountArg = Annotated[str, Field(description="账户 id，见 get_accounts")]
ChangeArg = Annotated[int, Field(description="修改编号 change_id")]
CurrencyArg = Annotated[str, Field(description="币种代码，如 CNY / HKD / USD")]


# ---------- 返回值 ----------

class AccountsResult(BaseModel):
    accounts: list[AccountSummary]


class AccountDetail(BaseModel):
    account: AccountSummary
    rows: list[dict[str, Any]] = Field(description="持仓和现金，weight 为账户内占比，cost_price 为每单位成本")
    flows: list[dict[str, Any]] = Field(description="资金进出，新到旧，入金为正、出金为负")


class HoldingsResult(BaseModel):
    as_of: dt.date
    group_by: str
    rows: list[dict[str, Any]]


class PerformanceResult(BaseModel):
    period: str
    period_label: str
    start: dt.date | None
    end: dt.date | None
    total_return: float | None = Field(description="区间累计 TWR")
    max_drawdown: float = Field(description="区间最大回撤（负数）")
    months: list[MonthReturn] = Field(description="每月 TWR，全部历史，新到旧")
    points: list[CurvePoint] | None = Field(None, description="逐日数据，仅 include_points=true 时返回")


class ChangesResult(BaseModel):
    changes: list[ChangeView]


# ---------- 工具 ----------

def _friendly[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """业务错误转成 ToolError：模型能读到中文原因并自己改参数重试（其他异常 SDK 只说"执行失败"）。"""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except (EditError, LookupError, ValueError) as e:
            raise ToolError(str(e.args[0]) if e.args else str(e)) from e

    return wrapper


def build_mcp(pm: App) -> MCPServer:
    # log_level：SDK 构造时会 logging.basicConfig(level=...)，默认 INFO 会让每个请求都刷一行日志
    mcp = MCPServer("portfolio-monitor", title="组合监测", version=__version__, instructions=INSTRUCTIONS,
                    log_level="WARNING")
    query, edit = pm.query, pm.edit

    def preview(kind: str, **fields: Any) -> ChangeView:
        return edit.preview({"kind": kind, **fields}, actor="mcp")

    # ----- 读取 -----

    @mcp.tool(title="组合总览", annotations=READ)
    @_friendly
    async def get_overview(date: DateArg = None) -> Overview:
        """组合总览：总资产和投资资产（美元）、今日 / 今年 / 累计收益、最大回撤、按资产类别的分配、各数据源同步状态。"""
        return query.overview(date)

    @mcp.tool(title="账户列表", annotations=READ)
    @_friendly
    async def get_accounts(date: DateArg = None) -> AccountsResult:
        """所有账户：id、名称、类型、数据源、市值（美元）、占总资产比例、是否可修改（editable）、数据是否沿用上次。"""
        return AccountsResult(accounts=query.accounts(date))

    @mcp.tool(title="账户明细", annotations=READ)
    @_friendly
    async def get_account(
        account_id: AccountArg,
        date: DateArg = None,
        flows_limit: Annotated[int, Field(ge=0, le=500, description="最多返回几笔资金进出")] = 20,
    ) -> AccountDetail:
        """某个账户的持仓和现金明细（数量、价格、成本价、市值、账户内占比）和最近的资金进出。修改持仓前先用它查当前状态。"""
        summary = next((a for a in query.accounts(date) if a.id == account_id), None)
        if summary is None:
            raise LookupError(f"没有这个账户：{account_id}")
        return AccountDetail(account=summary, rows=query.account_rows(account_id, date),
                             flows=query.account_flows(account_id)[:flows_limit])

    @mcp.tool(title="持仓", annotations=READ)
    @_friendly
    async def get_holdings(
        group_by: Annotated[
            Literal["instrument_id", "asset_class", "account_id", "currency", "region", "sector"],
            Field(description="instrument_id 为跨账户合并的标的明细，其余为按维度汇总"),
        ] = "instrument_id",
        account_id: Annotated[str | None, Field(description="只看某个账户")] = None,
        date: DateArg = None,
    ) -> HoldingsResult:
        """持仓，可跨账户合并到标的，或按资产类别 / 账户 / 币种 / 地区 / 行业汇总；含市值、占比、浮盈（美元）。"""
        filters = {"account_id": account_id} if account_id else None
        rows = query.holdings(date, group_by=[group_by], filters=filters)
        return HoldingsResult(as_of=date or query.latest_date(), group_by=group_by, rows=rows)

    @mcp.tool(title="收益表现", annotations=READ)
    @_friendly
    async def get_performance(
        period: Annotated[
            Literal["1m", "3m", "ytd", "1y", "all"],
            Field(description="区间：1m 近 1 月、3m 近 3 月、ytd 今年、1y 近 1 年、all 全部"),
        ] = "all",
        include_points: Annotated[bool, Field(description="是否返回逐日数据（点数多，按需开启）")] = False,
    ) -> PerformanceResult:
        """投资资产的收益表现：区间累计收益（TWR，剔除入金出金）、区间最大回撤、每月收益。"""
        curve = query.curve(period)
        points = curve.points
        return PerformanceResult(
            period=period, period_label=RANGES[period],
            start=points[0].date if points else None, end=points[-1].date if points else None,
            total_return=curve.total_return, max_drawdown=curve.max_drawdown,
            months=query.monthly_returns(), points=points if include_points else None,
        )

    @mcp.tool(title="修改记录", annotations=READ)
    @_friendly
    async def get_change_log(
        account_id: Annotated[str | None, Field(description="只看某个账户")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> ChangesResult:
        """手动账户的修改记录（新到旧）：来源、内容摘要、状态（待确认 / 已生效 / 已撤销 / 已放弃）、能否撤销。"""
        return ChangesResult(changes=edit.list(account_id=account_id, limit=limit))

    # ----- 写入：先预览，confirm_change 后生效 -----

    @mcp.tool(title="预览：改持仓", annotations=PREVIEW)
    @_friendly
    async def set_holding(
        account_id: AccountArg,
        symbol: Annotated[str, Field(description="代码，任意写法：600519 / 0700 / NVDA / HK:00700")],
        quantity: Annotated[float, Field(description="持有数量")],
        price: Annotated[float, Field(description="现价（原币）")],
        market: Annotated[
            Literal["US", "HK", "CN", "CRYPTO"] | None,
            Field(description="市场；代码有歧义时指定，加密货币必须填 CRYPTO"),
        ] = None,
        cost_price: Annotated[float | None, Field(description="每单位成本价（原币），不填则沿用原来的")] = None,
        name: Annotated[str | None, Field(description="名称，新标的时建议填")] = None,
        asset_class: AssetClass | None = None,
        region: str | None = None,
        sector: str | None = None,
    ) -> ChangeView:
        """预览：新增或修改手动账户里的一个持仓。返回 change_id 和"改前 → 改后"，用户同意后调用 confirm_change 才生效。"""
        return preview("set_holding", account_id=account_id, symbol=symbol, quantity=quantity, price=price,
                       market=market, cost_price=cost_price, name=name, asset_class=asset_class,
                       region=region, sector=sector)

    @mcp.tool(title="预览：删持仓", annotations=PREVIEW)
    @_friendly
    async def remove_holding(
        account_id: AccountArg,
        instrument_id: Annotated[str, Field(description="标的统一编号，如 CN:600519，见 get_account")],
    ) -> ChangeView:
        """预览：删除手动账户里的一个持仓。用户同意后调用 confirm_change 才生效。"""
        return preview("remove_holding", account_id=account_id, instrument_id=instrument_id)

    @mcp.tool(title="预览：改现金", annotations=PREVIEW)
    @_friendly
    async def set_cash_balance(
        account_id: AccountArg,
        currency: CurrencyArg,
        amount: Annotated[float, Field(description="新的现金余额（原币）；0 表示删除该币种")],
    ) -> ChangeView:
        """预览：把手动账户某币种的现金设为指定金额。差额计入盈亏（如利息）；存取款请用 record_cash_flow。用户同意后调用 confirm_change 才生效。"""
        return preview("set_cash", account_id=account_id, currency=currency, amount=amount)

    @mcp.tool(title="预览：记入金 / 出金", annotations=PREVIEW)
    @_friendly
    async def record_cash_flow(
        account_id: AccountArg,
        currency: CurrencyArg,
        amount: Annotated[float, Field(description="金额（原币），入金为正、出金为负")],
        note: Annotated[str | None, Field(description="备注")] = None,
        adjust_cash: Annotated[bool, Field(
            description="true（默认）：同时调整现金余额，日期为最新估值日；false：只记流水（余额已经包含这笔钱时用）",
        )] = True,
        date: Annotated[dt.date | None, Field(description="日期，仅 adjust_cash=false 时可填")] = None,
    ) -> ChangeView:
        """预览：记录一笔入金或出金，收益率计算会把它剔除。用户同意后调用 confirm_change 才生效。"""
        return preview("record_flow", account_id=account_id, currency=currency, amount=amount, note=note,
                       adjust_cash=adjust_cash, date=date)

    @mcp.tool(title="确认修改", annotations=APPLY)
    @_friendly
    async def confirm_change(change_id: ChangeArg) -> ChangeView:
        """确认并执行一个待确认的修改。只能在用户看过预览并明确同意后调用。"""
        return edit.confirm(change_id)

    @mcp.tool(title="放弃修改", annotations=PREVIEW)
    @_friendly
    async def discard_change(change_id: ChangeArg) -> ChangeView:
        """放弃一个待确认的修改（用户不同意或想重新填写时）。"""
        return edit.discard(change_id)

    @mcp.tool(title="撤销修改", annotations=APPLY)
    @_friendly
    async def undo_change(change_id: ChangeArg) -> ChangeView:
        """立即撤销一次已生效的修改，写回改前状态。调用前先向用户说明要撤销什么并征得同意；同一项之后又被改过时会被拒绝。"""
        return edit.undo(change_id)

    return mcp


# ---------- HTTP ----------

class _BearerGuard:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        got = Headers(scope=scope).get("authorization", "").encode()
        if not hmac.compare_digest(got, self.expected):
            response = JSONResponse({"error": "未授权：需要 Authorization: Bearer <MCP_TOKEN>"},
                                    status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def check_token(token: str) -> None:
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"MCP_TOKEN 太短（至少 {MIN_TOKEN_LENGTH} 个字符），可用 openssl rand -hex 32 生成")


def mcp_route(server: MCPServer, token: str) -> Route:
    """/mcp 路由。宿主应用的 lifespan 里必须进入 server.session_manager.run()。"""
    check_token(token)
    server.streamable_http_app()  # 建好 session_manager，带 SDK 默认的本机 Host 白名单
    return Route("/mcp", endpoint=_BearerGuard(StreamableHTTPASGIApp(server.session_manager), token))
