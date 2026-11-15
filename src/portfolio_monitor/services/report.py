"""日报：给 Hermes 写日报用的结构化摘要（get_daily_summary），以及保存、读取写好的日报。

日报正文由 Hermes 写（语言层交给 Hermes），这里只提供确定性的数据和存储。
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from portfolio_monitor.services.query import QueryService
from portfolio_monitor.services.risk import AlertView, RiskService
from portfolio_monitor.storage.repository import Repository

MAX_REPORT_CHARS = 20_000
TOP_MOVERS = 5


class SourceLine(BaseModel):
    source: str
    status: str = Field(description="ok / failed / running")
    error: str | None = None


class AccountLine(BaseModel):
    id: str
    name: str
    kind: str
    value_usd: float
    weight: float = Field(description="占总资产")
    change_usd: float | None = Field(description="比上一估值日的市值变动（含入金出金）；没有上一估值日时为空")
    stale: bool = Field(description="今天的数据是沿用上次的（数据源失败或延迟）")


class Mover(BaseModel):
    instrument_id: str
    name: str | None
    asset_class: str | None
    value_usd: float
    change_usd: float = Field(description="比上一估值日的市值变动（跨账户合并，含买卖）")
    change_pct: float | None = Field(description="变动 / 上一估值日市值；新买入的为空")
    accounts: list[str]


class AllocationLine(BaseModel):
    asset_class: str
    value_usd: float
    weight: float


class FlowLine(BaseModel):
    date: dt.date
    account_id: str
    currency: str
    amount: float = Field(description="原币，入金为正、出金为负")
    note: str | None


class DailySummary(BaseModel):
    as_of: dt.date
    previous_date: dt.date | None = Field(description="上一个估值日")
    nav_total_usd: float
    nav_investable_usd: float
    day_change_usd: float | None = Field(description="投资资产当日盈亏（已剔除资金进出）")
    day_return: float | None = Field(description="投资资产当日收益率（TWR）")
    ytd_return: float | None
    since_inception_return: float | None
    drawdown: float | None = Field(description="当前距高点的回撤（负数）")
    max_drawdown: float
    stale: bool = Field(description="今天的数据含沿用的快照")
    sources: list[SourceLine]
    accounts: list[AccountLine]
    movers: list[Mover] = Field(description=f"市值变动绝对值最大的 {TOP_MOVERS} 个持仓")
    allocation: list[AllocationLine]
    alerts: list[AlertView] = Field(description="风险提示；new / escalated 为今天新出现 / 升级")
    flows: list[FlowLine] = Field(description="上一估值日之后到今天的资金进出")
    unresolved_count: int
    report_saved: bool = Field(description="这个估值日是否已经保存过日报")


class DailyReport(BaseModel):
    as_of: dt.date
    content: str
    source: str
    updated_at: str


class SavedReport(BaseModel):
    as_of: dt.date
    chars: int
    replaced: bool = Field(description="是否覆盖了这天已有的日报")


class ReportService:
    def __init__(self, repo: Repository, query: QueryService, risk: RiskService) -> None:
        self.repo = repo
        self.query = query
        self.risk = risk

    def summary(self, as_of: dt.date | None = None) -> DailySummary:
        day = as_of or self.query.latest_date()
        if day is None:
            raise LookupError("还没有任何净值数据，请先运行同步")
        overview = self.query.overview(day)
        series = self.query.performance(end=day)
        today = series[-1] if series and series[-1].date == day else None
        prev = series[-2] if today is not None and len(series) >= 2 else None
        prev_date = prev.date if prev else None

        rows = self.query.valued_rows(day)
        prev_rows = self.query.valued_rows(prev_date) if prev_date else []
        prev_by_account: dict[str, float] = defaultdict(float)
        for r in prev_rows:
            prev_by_account[r["account_id"]] += r["market_value_usd"] or 0.0

        accounts = [
            AccountLine(id=a.id, name=a.name, kind=a.kind, value_usd=a.value_usd, weight=a.weight,
                        change_usd=(a.value_usd - prev_by_account.get(a.id, 0.0)) if prev_date else None,
                        stale=a.stale)
            for a in self.query.accounts(day) if a.value_usd or prev_by_account.get(a.id)
        ]

        return DailySummary(
            as_of=day, previous_date=prev_date,
            nav_total_usd=overview.nav_total_usd, nav_investable_usd=overview.nav_investable_usd,
            day_change_usd=(today.nav - prev.nav - today.net_flow) if today and prev else None,
            day_return=today.daily_return if today else None,
            ytd_return=overview.ytd_return, since_inception_return=overview.since_inception_return,
            drawdown=today.drawdown if today else None, max_drawdown=overview.max_drawdown,
            stale=overview.stale,
            sources=[SourceLine(source=s.source, status=s.status, error=s.error) for s in overview.sources],
            accounts=accounts,
            movers=_movers(rows, prev_rows) if prev_date else [],
            allocation=[AllocationLine(asset_class=a.key or "other", value_usd=a.value_usd, weight=a.weight)
                        for a in overview.allocation],
            alerts=self.risk.alerts(day).alerts,
            flows=[FlowLine(date=f.date, account_id=f.account_id, currency=f.currency, amount=f.amount, note=f.note)
                   for f in self.repo.cash_flows(after=prev_date or day - dt.timedelta(days=1), until=day)],
            unresolved_count=overview.unresolved_count,
            report_saved=self.repo.get_report(day) is not None,
        )

    def save(self, as_of: dt.date, content: str, source: str = "mcp") -> SavedReport:
        text = content.strip()
        if not text:
            raise ValueError("日报内容为空")
        if len(text) > MAX_REPORT_CHARS:
            raise ValueError(f"日报太长（{len(text)} 字），上限 {MAX_REPORT_CHARS}")
        if not self.repo.nav_series("total", start=as_of, end=as_of):
            raise LookupError(f"{as_of} 没有净值数据，请确认日期（日报应写给 get_daily_summary 返回的 as_of）")
        replaced = self.repo.save_report(as_of, text, source)
        return SavedReport(as_of=as_of, chars=len(text), replaced=replaced)

    def get(self, as_of: dt.date | None = None) -> DailyReport | None:
        if as_of is None:
            latest = self.repo.list_reports(limit=1)
            row = latest[0] if latest else None
        else:
            row = self.repo.get_report(as_of)
        return DailyReport(**_report_fields(row)) if row else None

    def recent(self, limit: int = 30) -> list[DailyReport]:
        return [DailyReport(**_report_fields(r)) for r in self.repo.list_reports(limit)]


def _report_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in ("as_of", "content", "source", "updated_at")}


def _movers(rows: list[dict[str, Any]], prev_rows: list[dict[str, Any]]) -> list[Mover]:
    """跨账户合并每个持仓的市值，和上一估值日比；现金和只有总值的行不算。"""
    now: dict[str, dict[str, Any]] = {}
    before: dict[str, float] = defaultdict(float)
    for r in rows:
        if r["row_type"] != "position":
            continue
        m = now.setdefault(r["instrument_id"], {"value": 0.0, "name": r.get("name"),
                                                "asset_class": r.get("asset_class"), "accounts": set()})
        m["value"] += r["market_value_usd"] or 0.0
        m["accounts"].add(r.get("account_name") or r["account_id"])
    for r in prev_rows:
        if r["row_type"] == "position":
            before[r["instrument_id"]] += r["market_value_usd"] or 0.0
            now.setdefault(r["instrument_id"], {"value": 0.0, "name": r.get("name"),
                                                "asset_class": r.get("asset_class"), "accounts": set()})
    movers = []
    for iid, m in now.items():
        change = m["value"] - before.get(iid, 0.0)
        if abs(change) < 0.005:
            continue
        prev_value = before.get(iid, 0.0)
        movers.append(Mover(instrument_id=iid, name=m["name"], asset_class=m["asset_class"], value_usd=m["value"],
                            change_usd=change, change_pct=change / prev_value if prev_value > 0 else None,
                            accounts=sorted(m["accounts"])))
    return sorted(movers, key=lambda x: -abs(x.change_usd))[:TOP_MOVERS]
