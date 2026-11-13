"""查询服务：网页、JSON 接口、MCP、命令行共用。"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from portfolio_monitor.domain import valuation
from portfolio_monitor.domain.aggregate import DEFAULT_SUM_FIELDS, aggregate
from portfolio_monitor.domain.returns import Flow, NavPoint, PerfPoint, max_drawdown, period_return, twr_series
from portfolio_monitor.sources.registry import MANUAL_FLOW_SOURCES
from portfolio_monitor.storage.repository import Repository

_INSTRUMENT_FIELDS = ("name", "asset_class", "region", "sector", "currency")
RANGES = {"1m": "近 1 月", "3m": "近 3 月", "ytd": "今年", "1y": "近 1 年", "all": "全部"}


def range_start(end: dt.date, key: str) -> dt.date | None:
    """区间起点（取起点当天或之前最近一个净值日为基准）；"all" 返回 None。"""
    if key == "all":
        return None
    if key == "ytd":
        return dt.date(end.year - 1, 12, 31)
    months = {"1m": 1, "3m": 3, "1y": 12}.get(key)
    if months is None:
        raise ValueError(f"未知区间：{key}，可选 {list(RANGES)}")
    y, m = divmod(end.month - 1 - months, 12)
    year, month = end.year + y, m + 1
    return dt.date(year, month, min(end.day, calendar.monthrange(year, month)[1]))


class AllocationSlice(BaseModel):
    key: str | None
    value_usd: float
    weight: float


class SourceStatus(BaseModel):
    source: str
    status: str
    error: str | None = None


class Overview(BaseModel):
    as_of: dt.date
    nav_total_usd: float
    nav_investable_usd: float
    day_return: float | None
    ytd_return: float | None
    since_inception_return: float | None
    max_drawdown: float
    allocation: list[AllocationSlice]
    stale: bool
    unresolved_count: int
    sources: list[SourceStatus]


class CurvePoint(BaseModel):
    date: dt.date
    nav: float
    net_flow: float
    cum_return: float  # 区间内累计 TWR
    drawdown: float    # 相对区间内高点


class Curve(BaseModel):
    range: str
    total_return: float | None
    max_drawdown: float
    points: list[CurvePoint]


class MonthReturn(BaseModel):
    month: str  # YYYY-MM
    twr: float
    nav_end: float
    net_flow: float


class AccountSummary(BaseModel):
    id: str
    name: str
    institution: str | None
    kind: str
    source: str
    currency: str
    include_in_returns: bool
    editable: bool  # 手动账户可在网页 / MCP 修改
    flows_editable: bool  # 可以手动记入金出金（手动账户，以及接口拿不到资金流水的数据源）
    value_usd: float
    weight: float
    stale: bool     # 今天的数据是沿用上次的


class QueryService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def latest_date(self) -> dt.date | None:
        latest = self.repo.latest_as_of()
        return dt.date.fromisoformat(latest) if latest else None

    def _resolve(self, as_of: dt.date | None) -> dt.date:
        day = as_of or self.latest_date()
        if day is None:
            raise LookupError("还没有任何净值数据，请先运行同步")
        return day

    def valued_rows(self, as_of: dt.date | None = None) -> list[dict[str, Any]]:
        day = self._resolve(as_of)
        rows = self.repo.snapshot_rows(day)
        rates = {}
        for ccy in {r["currency"] for r in rows} - {"USD"}:
            found = self.repo.fx_on_or_before(day, ccy)
            if found:
                rates[ccy] = found[1]
        return valuation.value_rows(rows, rates)

    def holdings(
        self,
        as_of: dt.date | None = None,
        group_by: Sequence[str] = ("instrument_id",),
        filters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        by_instrument = "instrument_id" in group_by
        keep = _INSTRUMENT_FIELDS if by_instrument else ()
        if "account_id" in group_by:
            keep = (*keep, "account_name")
        return aggregate(
            self.valued_rows(as_of),
            group_by,
            filters=filters,
            sum_fields=(*DEFAULT_SUM_FIELDS, "quantity") if by_instrument else DEFAULT_SUM_FIELDS,
            keep=keep,
        )

    def allocation(self, by: str = "asset_class", as_of: dt.date | None = None) -> list[AllocationSlice]:
        return [
            AllocationSlice(key=r[by], value_usd=r["market_value_usd"] or 0.0, weight=r["weight"])
            for r in aggregate(self.valued_rows(as_of), [by], sum_fields=("market_value_usd",))
        ]

    def performance(self, end: dt.date | None = None) -> list[PerfPoint]:
        """只含计入收益率的账户；资金进出按流水日期的汇率换算。

        比净值序列起点晚加入的账户（如 IBKR 回填了一年、其他账户从首次同步才有数据）：
        加入当天的市值视为转入，不算收益；加入当天及之前的资金进出已包含在这笔市值里，不再计入。
        """
        navs = [
            NavPoint(dt.date.fromisoformat(r["as_of"]), r["nav_usd"])
            for r in self.repo.nav_series("investable", end=end)
        ]
        if not navs:
            return []
        series_start = navs[0].date
        flows = []
        for account in self.repo.list_accounts(active_only=False):
            if not account.include_in_returns:
                continue
            first = self.repo.first_value_date(account.id)
            joined = dt.date.fromisoformat(first) if first else None
            if joined is None or (end is not None and joined > end):
                continue
            late = joined > series_start
            for f in self.repo.cash_flows(account_ids=[account.id], until=end):
                if late and f.date <= joined:
                    continue
                rate = self.repo.fx_nearest(f.date, f.currency)
                if rate is None:
                    raise valuation.MissingFxRate([f.currency])
                flows.append(Flow(f.date, f.amount * rate[1]))
            if late:
                value = sum(r["market_value_usd"] for r in self.valued_rows(joined)
                            if r["account_id"] == account.id)
                flows.append(Flow(joined, value))
        return twr_series(navs, flows)

    def curve(self, range_key: str = "all", end: dt.date | None = None) -> Curve:
        """收益率与回撤曲线，以区间起点为 0 重新起算。"""
        series = self.performance(end)
        if not series:
            return Curve(range=range_key, total_return=None, max_drawdown=0.0, points=[])
        start = range_start(series[-1].date, range_key)
        base_i = 0
        if start is not None:
            base_i = max((i for i, p in enumerate(series) if p.date <= start), default=0)
        window = series[base_i:]
        base, peak, points = window[0].index, 1.0, []
        for i, p in enumerate(window):
            cum = p.index / base
            peak = max(peak, cum)
            points.append(CurvePoint(date=p.date, nav=p.nav, net_flow=p.net_flow if i else 0.0,
                                     cum_return=cum - 1, drawdown=cum / peak - 1))
        return Curve(range=range_key, total_return=points[-1].cum_return,
                     max_drawdown=min(p.drawdown for p in points), points=points)

    def monthly_returns(self, end: dt.date | None = None) -> list[MonthReturn]:
        """每月 TWR（首月从第一个净值日起算），新的在前。"""
        series = self.performance(end)
        months: dict[str, list[PerfPoint]] = {}
        for p in series:
            months.setdefault(p.date.strftime("%Y-%m"), []).append(p)
        out, prev_index = [], series[0].index if series else 1.0
        for month, points in months.items():
            last = points[-1]
            out.append(MonthReturn(month=month, twr=last.index / prev_index - 1, nav_end=last.nav,
                                   net_flow=sum(p.net_flow for p in points)))
            prev_index = last.index
        return out[::-1]

    def accounts(self, as_of: dt.date | None = None) -> list[AccountSummary]:
        try:
            rows = self.valued_rows(as_of)
        except LookupError:
            rows = []
        total = sum(r["market_value_usd"] for r in rows)
        out = []
        for a in self.repo.list_accounts():
            mine = [r for r in rows if r["account_id"] == a.id]
            value = sum(r["market_value_usd"] for r in mine)
            out.append(AccountSummary(
                id=a.id, name=a.name, institution=a.institution, kind=a.kind.value, source=a.source,
                currency=a.currency, include_in_returns=a.include_in_returns,
                editable=a.source == "manual", flows_editable=a.source in MANUAL_FLOW_SOURCES | {"manual"},
                value_usd=value,
                weight=value / total if total else 0.0,
                stale=any(r["carried_forward"] for r in mine),
            ))
        return sorted(out, key=lambda x: x.value_usd, reverse=True)

    def account_rows(self, account_id: str, as_of: dt.date | None = None) -> list[dict[str, Any]]:
        """某账户的持仓和现金（不合并），占比为账户内占比；持仓带每单位成本价。"""
        try:
            rows = aggregate(self.valued_rows(as_of), (), filters={"account_id": account_id})
        except LookupError:
            return []
        for r in rows:
            q, cost = r.get("quantity"), r.get("cost_basis")
            r["cost_price"] = cost / q if r["row_type"] == "position" and cost is not None and q else None
        return rows

    def account_flows(self, account_id: str) -> list[dict[str, Any]]:
        return self.repo.cash_flow_rows(account_id)

    def sync_history(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.repo.sync_run_history(limit)

    def overview(self, as_of: dt.date | None = None) -> Overview:
        day = self._resolve(as_of)
        series = self.performance(end=day)
        navs = {r["scope"]: r for r in self._nav_rows(day)}
        if "total" not in navs:
            raise LookupError(f"{day} 没有净值数据")
        return Overview(
            as_of=day,
            nav_total_usd=navs["total"]["nav_usd"],
            nav_investable_usd=navs.get("investable", {"nav_usd": 0.0})["nav_usd"],
            day_return=series[-1].daily_return if series else None,
            ytd_return=period_return(series, dt.date(day.year - 1, 12, 31)),
            since_inception_return=(series[-1].index - 1) if series else None,
            max_drawdown=max_drawdown(series),
            allocation=self.allocation("asset_class", day),
            stale=bool(navs["total"]["stale"]),
            unresolved_count=len(self.repo.list_unresolved()),
            sources=[
                SourceStatus(source=r["source"], status=r["status"], error=r["error"])
                for r in self.repo.latest_sync_runs(day)
            ],
        )

    def _nav_rows(self, day: dt.date) -> list[dict[str, Any]]:
        out = []
        for scope in ("total", "investable"):
            rows = self.repo.nav_series(scope, start=day, end=day)
            if rows:
                out.append({"scope": scope, **rows[0]})
        return out
