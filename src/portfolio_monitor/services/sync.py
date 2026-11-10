"""每日同步：各数据源 → 快照 → 汇率 → 净值。

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

from portfolio_monitor.domain import valuation
from portfolio_monitor.domain.models import Account
from portfolio_monitor.sources.base import Source, validate_snapshot
from portfolio_monitor.sources.fx import FxProvider
from portfolio_monitor.storage.db import transaction
from portfolio_monitor.storage.repository import Repository


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


@dataclass
class SyncReport:
    as_of: dt.date
    sources: list[SourceResult]
    fx_status: str  # ok | carried_forward | missing | not_needed
    fx_detail: str | None
    nav_total: float | None
    nav_investable: float | None
    stale: bool

    @property
    def ok(self) -> bool:
        return (
            all(s.status == "ok" for s in self.sources)
            and self.fx_status in ("ok", "not_needed")
            and self.nav_total is not None
        )


class SyncService:
    def __init__(
        self,
        repo: Repository,
        sources: Mapping[str, Source],
        fx: FxProvider,
        raw_dir: Path,
    ) -> None:
        self.repo = repo
        self.sources = sources
        self.fx = fx
        self.raw_dir = raw_dir

    def run(self, as_of: dt.date) -> SyncReport:
        by_source: dict[str, list[Account]] = defaultdict(list)
        for account in self.repo.list_accounts(active_only=True):
            by_source[account.source].append(account)

        results = [self._run_source(name, accts, as_of) for name, accts in sorted(by_source.items())]
        fx_status, fx_detail = self._ensure_fx(as_of)
        nav_total, nav_investable, stale, nav_detail = self._compute_nav(as_of)
        if nav_detail:
            fx_detail = "；".join(x for x in (fx_detail, nav_detail) if x)
        return SyncReport(as_of, results, fx_status, fx_detail, nav_total, nav_investable, stale)

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
            raw_path = self._archive(as_of, name, snap.raw)
            with transaction(self.repo.conn):
                self.repo.upsert_instruments(snap.instruments)
                self.repo.replace_snapshot(
                    as_of, [a.id for a in accounts], snap.positions, snap.cash, run_id
                )
                new_flows = self.repo.insert_cash_flows(snap.flows, origin="api")
                self.repo.record_unresolved(name, snap.unresolved, as_of)
                self.repo.finish_sync_run(
                    run_id, "ok", raw_path=raw_path,
                    counts=(len(snap.positions), len(snap.cash), new_flows),
                )
            return SourceResult(
                name, "ok", positions=len(snap.positions), cash=len(snap.cash),
                new_flows=new_flows, unresolved=len(snap.unresolved),
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

    def _archive(self, as_of: dt.date, name: str, raw: object) -> str | None:
        if raw is None:
            return None
        folder = self.raw_dir / as_of.isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.json"
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return str(path)

    # ---------- 汇率与净值 ----------

    def _needed_currencies(self, as_of: dt.date) -> set[str]:
        return {r["currency"] for r in self.repo.snapshot_rows(as_of)} - {"USD"}

    def _ensure_fx(self, as_of: dt.date) -> tuple[str, str | None]:
        needed = self._needed_currencies(as_of)
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
