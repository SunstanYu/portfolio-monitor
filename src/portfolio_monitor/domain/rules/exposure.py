"""敞口类规则：单一标的、单一账户、偏离目标配置、现金占比、杠杆。看估值日当天的持仓。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from portfolio_monitor.domain.models import AssetClass
from portfolio_monitor.domain.rules.base import (
    Alert,
    PortfolioContext,
    RatioRule,
    check_thresholds,
    level,
    pct,
    threshold_pct,
)

CLASS_LABELS = {"equity": "股票", "etf": "ETF", "fund": "基金", "bond": "债券", "crypto": "加密货币",
                "cash": "现金", "option": "期权", "other": "其他"}


def asset_class_of(row: Mapping[str, Any]) -> str:
    return "cash" if row["row_type"] == "cash" else (row["asset_class"] or "other")


def _is_cash(row: Mapping[str, Any]) -> bool:
    return asset_class_of(row) == "cash"


class InstrumentConcentration(RatioRule):
    """同一标的跨账户合计占投资资产；现金和稳定币不算。"""

    id = "instrument_concentration"
    name = "单一标的集中度"
    defaults = {"warn": 0.20, "critical": 0.35, "exempt": []}

    def check(self, settings: Mapping[str, Any]) -> list[str]:
        problems = check_thresholds(self.id, settings)
        exempt = settings.get("exempt", [])
        if not isinstance(exempt, list) or not all(isinstance(x, str) for x in exempt):
            problems.append(f"[rules.{self.id}].exempt 必须是标的编号列表，如 [\"US:VOO\"]")
        return problems

    def evaluate(self, ctx: PortfolioContext, settings: Mapping[str, Any]) -> list[Alert]:
        base = ctx.investable
        if base <= 0:
            return []
        exempt = set(settings.get("exempt", []))
        value: dict[str, float] = defaultdict(float)
        names: dict[str, str] = {}
        accounts: dict[str, set[str]] = defaultdict(set)
        for r in ctx.rows:
            if not r["include_in_returns"] or r["row_type"] != "position" or _is_cash(r):
                continue
            iid = r["instrument_id"]
            if iid in exempt:
                continue
            value[iid] += r["market_value_usd"] or 0.0
            names[iid] = r.get("name") or iid
            accounts[iid].add(r["account_id"])
        alerts = []
        for iid, v in sorted(value.items(), key=lambda kv: -kv[1]):
            weight = v / base
            if hit := level(weight, settings):
                where = f"，分布在 {len(accounts[iid])} 个账户" if len(accounts[iid]) > 1 else ""
                alerts.append(Alert(
                    rule_id=self.id, subject=iid, subject_name=names[iid], severity=hit[0],
                    value=weight, threshold=hit[1],
                    message=f"{names[iid]} 占投资资产 {pct(weight)}{where}（阈值 {threshold_pct(hit[1])}）"))
        return alerts


class AccountConcentration(RatioRule):
    """单一账户（平台）占总资产。"""

    id = "account_concentration"
    name = "单一账户集中度"
    defaults = {"warn": 0.60, "critical": 0.80}

    def evaluate(self, ctx: PortfolioContext, settings: Mapping[str, Any]) -> list[Alert]:
        base = ctx.total
        if base <= 0:
            return []
        value: dict[str, float] = defaultdict(float)
        names: dict[str, str] = {}
        for r in ctx.rows:
            value[r["account_id"]] += r["market_value_usd"] or 0.0
            names[r["account_id"]] = r.get("account_name") or r["account_id"]
        alerts = []
        for aid, v in sorted(value.items(), key=lambda kv: -kv[1]):
            weight = v / base
            if hit := level(weight, settings):
                alerts.append(Alert(
                    rule_id=self.id, subject=aid, subject_name=names[aid], severity=hit[0],
                    value=weight, threshold=hit[1],
                    message=f"{names[aid]} 占总资产 {pct(weight)}（阈值 {threshold_pct(hit[1])}）"))
        return alerts


class AllocationDrift(RatioRule):
    """各资产类别占总资产与目标配置（[targets]）的偏离，单位为百分点；没填目标配置时不检查。"""

    id = "allocation_drift"
    name = "偏离目标配置"
    defaults = {"warn": 0.05, "critical": None}

    def evaluate(self, ctx: PortfolioContext, settings: Mapping[str, Any]) -> list[Alert]:
        base = ctx.total
        if not ctx.targets or base <= 0:
            return []
        actual: dict[str, float] = defaultdict(float)
        for r in ctx.rows:
            actual[asset_class_of(r)] += (r["market_value_usd"] or 0.0) / base
        alerts = []
        for cls in [c.value for c in AssetClass]:
            target, now = ctx.targets.get(cls, 0.0), actual.get(cls, 0.0)
            drift = now - target
            if hit := level(abs(drift), settings):
                label = CLASS_LABELS.get(cls, cls)
                alerts.append(Alert(
                    rule_id=self.id, subject=cls, subject_name=label, severity=hit[0],
                    value=drift, threshold=hit[1],
                    message=(f"{label} 占总资产 {pct(now)}，目标 {threshold_pct(target)}，"
                             f"{'高' if drift > 0 else '低'} {abs(drift) * 100:.1f} 个百分点")))
        return alerts


class IdleCash(RatioRule):
    """现金（含稳定币）占总资产。"""

    id = "idle_cash"
    name = "现金占比"
    defaults = {"warn": 0.30, "critical": None}

    def evaluate(self, ctx: PortfolioContext, settings: Mapping[str, Any]) -> list[Alert]:
        base = ctx.total
        if base <= 0:
            return []
        fiat = sum(r["market_value_usd"] or 0.0 for r in ctx.rows if r["row_type"] == "cash")
        stable = sum(r["market_value_usd"] or 0.0 for r in ctx.rows
                     if r["row_type"] == "position" and _is_cash(r))
        weight = (fiat + stable) / base
        if hit := level(weight, settings):
            detail = f"法币 {pct(fiat / base)}，稳定币 {pct(stable / base)}；" if stable else ""
            return [Alert(rule_id=self.id, severity=hit[0], value=weight, threshold=hit[1],
                          message=f"现金占总资产 {pct(weight)}（{detail}阈值 {threshold_pct(hit[1])}）")]
        return []


class Leverage(RatioRule):
    """杠杆敞口占投资资产：合约等衍生品的名义价值（取绝对值）+ 账户融资（现金合计为负的部分）。"""

    id = "leverage"
    name = "杠杆敞口"
    defaults = {"warn": 0.50, "critical": 1.00}

    def check(self, settings: Mapping[str, Any]) -> list[str]:
        return check_thresholds(self.id, settings, upper=10.0)  # 杠杆可以超过 100%

    def evaluate(self, ctx: PortfolioContext, settings: Mapping[str, Any]) -> list[Alert]:
        base = ctx.investable
        if base <= 0:
            return []
        derivatives = 0.0
        cash: dict[str, float] = defaultdict(float)
        for r in ctx.rows:
            if not r["include_in_returns"]:
                continue
            if r["row_type"] == "position" and r.get("notional_usd"):
                derivatives += abs(r["notional_usd"])
            if r["row_type"] == "cash":
                cash[r["account_id"]] += r["market_value_usd"] or 0.0
        borrowed = sum(-v for v in cash.values() if v < 0)
        weight = (derivatives + borrowed) / base
        if hit := level(weight, settings):
            parts = [f"合约名义价值 {pct(derivatives / base)}"] if derivatives else []
            parts += [f"融资 {pct(borrowed / base)}"] if borrowed else []
            return [Alert(rule_id=self.id, severity=hit[0], value=weight, threshold=hit[1],
                          message=f"杠杆敞口占投资资产 {pct(weight)}（{'，'.join(parts)}；阈值 {threshold_pct(hit[1])}）")]
        return []

