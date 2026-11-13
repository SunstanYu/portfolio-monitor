"""每日同步：各数据源 → 快照 → 汇率 → 净值 → 风险规则。

- 每个数据源独立运行、独立事务：一个失败不影响其他
- 数据源失败时沿用该账户最近一次快照（标记 carried_forward），避免净值凭空下跌
- 汇率获取失败时沿用最近一次汇率；完全没有汇率时不计算净值，并在报告里说明
- 每次同步的原始返回存档到 raw_dir/<日期>/<数据源>.json，便于排查和重放
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from portfolio_monitor.domain import valuation
from portfolio_monitor.domain.models import Account
from portfolio_monitor.sources.base import HistorySource, Source, validate_history, validate_snapshot
from portfolio_monitor.sources.fx import FxProvider
from portfolio_monitor.storage.db import transaction
from portfolio_monitor.storage.repository import Repository

if TYPE_CHECKING:
    from portfolio_monitor.domain.rules import Alert
    from portfolio_monitor.services.risk import RiskService


@dataclass
class SourceResult:
    source: str
    status: str  # ok | failed
    error: str | None = None
    positions: int = 0
    cash: int = 0
    new_flows: int = 0
    unresolved: int = 0
    carried_forward: list[str] = field(default_factory=list)
    note: str | None = None


@dataclass
class BackfillReport:
    source: str
    start: dt.date
    end: dt.date  # 实际截止日（数据源可能还没有最近几天的数据）
    values: int
    new_flows: int
    nav_days: int
    fx_problems: list[str]
    raw_path: str | None


def lagging(data_date: dt.date | None, as_of: dt.date) -> bool:
    """数据日期和估值日之间隔着工作日 → 数据落后（周末拿到周五的数据不算）。"""
    if data_date is None or data_date >= as_of:
        return False
    day = data_date + dt.timedelta(days=1)
    while day <= as_of:
        if day.weekday() < 5:
            return True
        day += dt.timedelta(days=1)
    return False


@dataclass
class SyncReport:
    as_of: dt.date
    sources: list[SourceResult]
    fx_status: str  # ok | carried_forward | missing | not_needed
    fx_detail: str | None
    nav_total: float | None
    nav_investable: float | None
    stale: bool
    alerts: list[Alert] = field(default_factory=list)
    alerts_error: str | None = None  # 规则执行出错（同步结果照常写入）

    @property
    def ok(self) -> bool:
        return (
            all(s.status == "ok" for s in self.sources)
            and self.fx_status in ("ok", "not_needed")
            and self.nav_total is not None
            and self.alerts_error is None
        )


class SyncService:
    def __init__(
        self,
        repo: Repository,
        sources: Mapping[str, Source],
        fx: FxProvider,
        raw_dir: Path,
        risk: RiskService | None = None,
    ) -> None:
        self.repo = repo
        self.sources = sources
        self.fx = fx
        self.raw_dir = raw_dir
        self.risk = risk  # 算完净值后执行风险规则

    def run(self, as_of: dt.date, only: str | None = None, *, refetch_fx: bool = True) -> SyncReport:
        """同步全部数据源。

        only：只重跑这一个数据源（如手动账户被修改后），其他数据源的快照不动
        refetch_fx=False：当天已有汇率的币种不再重新获取
        """
        by_source: dict[str, list[Account]] = defaultdict(list)
        for account in self.repo.list_accounts(active_only=True):
            if only is None or account.source == only:
                by_source[account.source].append(account)

        results = [self._run_source(name, accts, as_of) for name, accts in sorted(by_source.items())]
        fx_status, fx_detail = self._ensure_fx(as_of, only_missing=not refetch_fx)
        nav_total, nav_investable, stale, nav_detail = self._compute_nav(as_of)
        if nav_detail:
            fx_detail = "；".join(x for x in (fx_detail, nav_detail) if x)
        alerts: list[Alert] = []
        alerts_error = None
        if self.risk is not None and nav_total is not None:
            try:
                alerts = self.risk.evaluate(as_of)
            except Exception as e:  # 规则出错不影响已写入的快照和净值
                alerts_error = f"{type(e).__name__}: {e}"
        return SyncReport(as_of, results, fx_status, fx_detail, nav_total, nav_investable, stale,
                          alerts, alerts_error)

    def recompute_nav(self) -> int:
        """按现有快照和汇率重算全部净值，返回成功的天数。"""
        done = 0
        for day in self.repo.snapshot_dates():
            total, _, _, _ = self._compute_nav(dt.date.fromisoformat(day))
            done += total is not None
        return done

    # ---------- 数据源 ----------

    def _run_source(self, name: str, accounts: list[Account], as_of: dt.date) -> SourceResult:
        run_id = self.repo.start_sync_run(as_of, name)
        try:
            source = self.sources.get(name)
            if source is None:
                raise KeyError(f"未注册的数据源：{name}")
            snap = source.fetch(as_of, accounts)
            validate_snapshot(snap, name, as_of, accounts)
            behind = lagging(snap.data_date, as_of)
            raw_path = self._archive(self.raw_dir / as_of.isoformat(), name, snap.raw)
            with transaction(self.repo.conn):
                self.repo.upsert_instruments(snap.instruments)
                self.repo.replace_snapshot(
                    as_of, [a.id for a in accounts], snap.positions, snap.cash, run_id,
                    carried_forward=behind,
                )
                new_flows = self.repo.insert_cash_flows(snap.flows, origin="api")
                self.repo.record_unresolved(name, snap.unresolved, as_of)
                self.repo.finish_sync_run(
                    run_id, "ok", raw_path=raw_path,
                    counts=(len(snap.positions), len(snap.cash), new_flows),
                )
            notes = ([f"数据截至 {snap.data_date}，按沿用标记"] if behind else []) + snap.warnings
            return SourceResult(
                name, "ok", positions=len(snap.positions), cash=len(snap.cash),
                new_flows=new_flows, unresolved=len(snap.unresolved),
                note="；".join(notes) or None,
            )
        except Exception as e:  # 隔离单个数据源的任何失败
            error = f"{type(e).__name__}: {e}"
            carried = self._carry_forward(as_of, accounts, run_id)
            self.repo.finish_sync_run(run_id, "failed", error=error)
            return SourceResult(name, "failed", error=error, carried_forward=carried)

    def _carry_forward(self, as_of: dt.date, accounts: list[Account], run_id: int) -> list[str]:
        carried = []
        for account in accounts:
            prev = self.repo.latest_snapshot_before(account.id, as_of)
            if prev is None:
                continue
            _, positions, cash = prev
            self.repo.replace_snapshot(
                as_of, [account.id], positions, cash, run_id, carried_forward=True
            )
            carried.append(account.id)
        return carried

    def _archive(self, folder: Path, name: str, raw: object) -> str | None:
        """原始返回存档：XML 文本存 .xml，其余存 .json。"""
        if raw is None:
            return None
        folder.mkdir(parents=True, exist_ok=True)
        if isinstance(raw, str) and raw.lstrip().startswith("<"):
            path = folder / f"{name}.xml"
            path.write_text(raw, encoding="utf-8")
        else:
            path = folder / f"{name}.json"
            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return str(path)

    # ---------- 历史回填 ----------

    def backfill(self, name: str, start: dt.date, end: dt.date) -> BackfillReport:
        """用数据源的历史（每日账户总值 + 资金进出）回填净值。

        有快照的日子以快照为准，历史总值只补没有快照的日子；可重复运行（覆盖同一天的总值，流水去重）。
        """
        accounts = [a for a in self.repo.list_accounts(active_only=True) if a.source == name]
        if not accounts:
            raise LookupError(f"没有使用数据源 {name} 的账户")
        source = self.sources.get(name)
        if not isinstance(source, HistorySource):
            raise ValueError(f"数据源 {name} 不支持回填历史")

        hist = source.history(start, end, accounts)
        validate_history(hist, name, accounts)
        raw_path = self._archive(self.raw_dir / "backfill" / f"{hist.start}_{hist.end}", name, hist.raw)
        with transaction(self.repo.conn):
            n_values = self.repo.upsert_account_history(hist.values, origin=f"{name}:backfill")
            new_flows = self.repo.insert_cash_flows(hist.flows, origin="api")

        problems: list[str] = []
        # 资金进出按流水当天的汇率换算，先把这些日子的汇率补齐
        flow_ccys: dict[dt.date, set[str]] = defaultdict(set)
        for f in hist.flows:
            if f.currency != "USD":
                flow_ccys[f.date].add(f.currency)
        for day, ccys in sorted(flow_ccys.items()):
            status, detail = self._ensure_fx(day, only_missing=True, needed=ccys)
            if status not in ("ok", "not_needed") and detail:
                problems.append(f"{day} {detail}")

        nav_days = 0
        for day in sorted({v.date for v in hist.values}):
            status, detail = self._ensure_fx(day, only_missing=True)
            if status not in ("ok", "not_needed") and detail:
                problems.append(f"{day} {detail}")
            total, _, _, _ = self._compute_nav(day)
            nav_days += total is not None
        return BackfillReport(name, hist.start, hist.end, n_values, new_flows, nav_days, problems, raw_path)

    # ---------- 汇率与净值 ----------

    def _needed_currencies(self, as_of: dt.date) -> set[str]:
        return {r["currency"] for r in self.repo.snapshot_rows(as_of)} - {"USD"}

    def _ensure_fx(
        self, as_of: dt.date, only_missing: bool = False, needed: set[str] | None = None
    ) -> tuple[str, str | None]:
        needed = self._needed_currencies(as_of) if needed is None else needed - {"USD"}
        if only_missing:
            day = as_of.isoformat()
            if all((self.repo.fx_on_or_before(as_of, c) or ("",))[0] == day for c in needed):
                return ("ok" if needed else "not_needed"), None
        if not needed:
            return "not_needed", None
        try:
            quote = self.fx.fetch(as_of, sorted(needed))
            with transaction(self.repo.conn):
                for ccy, rate in quote.usd_per_unit.items():
                    self.repo.upsert_fx(as_of, ccy, rate, quote.effective_date, quote.provider)
            return "ok", None
        except Exception as e:
            fallback = {c: self.repo.fx_on_or_before(as_of, c) for c in needed}
            missing = sorted(c for c, v in fallback.items() if v is None)
            if missing:
                return "missing", f"汇率获取失败且无历史汇率：{', '.join(missing)}（{e}）"
            used = ", ".join(f"{c}@{v[0]}" for c, v in sorted(fallback.items()) if v)
            return "carried_forward", f"汇率获取失败，沿用最近汇率 {used}（{e}）"

    def _compute_nav(self, as_of: dt.date) -> tuple[float | None, float | None, bool, str | None]:
        rows = self.repo.snapshot_rows(as_of)
        if not rows:
            return None, None, False, "当日没有任何快照"
        rates: dict[str, float] = {}
        for ccy in {r["currency"] for r in rows} - {"USD"}:
            found = self.repo.fx_on_or_before(as_of, ccy)
            if found:
                rates[ccy] = found[1]
        try:
            valued = valuation.value_rows(rows, rates)
        except valuation.MissingFxRate as e:
            return None, None, False, f"未计算净值：{e}"
        total = valuation.nav(valued)
        investable = valuation.nav(valued, investable_only=True)
        stale = any(r["carried_forward"] for r in valued)
        with transaction(self.repo.conn):
            self.repo.upsert_nav(as_of, "total", total, stale)
            self.repo.upsert_nav(as_of, "investable", investable, stale)
        return total, investable, stale, None
