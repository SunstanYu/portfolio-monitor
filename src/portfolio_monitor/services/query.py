"""查询服务：网页、JSON 接口、MCP、命令行共用。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from portfolio_monitor.domain import valuation
from portfolio_monitor.domain.aggregate import DEFAULT_SUM_FIELDS, aggregate
from portfolio_monitor.domain.returns import Flow, NavPoint, PerfPoint, max_drawdown, period_return, twr_series
from portfolio_monitor.storage.repository import Repository

_INSTRUMENT_FIELDS = ("name", "asset_class", "region", "sector", "currency")


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
        return aggregate(
            self.valued_rows(as_of),
            group_by,
            filters=filters,
            sum_fields=(*DEFAULT_SUM_FIELDS, "quantity") if by_instrument else DEFAULT_SUM_FIELDS,
            keep=_INSTRUMENT_FIELDS if by_instrument else (),
        )

    def allocation(self, by: str = "asset_class", as_of: dt.date | None = None) -> list[AllocationSlice]:
        return [
            AllocationSlice(key=r[by], value_usd=r["market_value_usd"] or 0.0, weight=r["weight"])
            for r in aggregate(self.valued_rows(as_of), [by], sum_fields=("market_value_usd",))
        ]

    def performance(self, end: dt.date | None = None) -> list[PerfPoint]:
        """只含计入收益率的账户；资金进出按流水日期的汇率换算。"""
        navs = [
            NavPoint(dt.date.fromisoformat(r["as_of"]), r["nav_usd"])
            for r in self.repo.nav_series("investable", end=end)
        ]
        ids = [a.id for a in self.repo.list_accounts(active_only=False) if a.include_in_returns]
        flows = []
        for f in self.repo.cash_flows(account_ids=ids, until=end):
            rate = self.repo.fx_nearest(f.date, f.currency)
            if rate is None:
                raise valuation.MissingFxRate([f.currency])
            flows.append(Flow(f.date, f.amount * rate[1]))
        return twr_series(navs, flows)

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
