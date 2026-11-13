"""表现类规则：从高点回撤、单日跌幅。看投资资产的 TWR 序列（已剔除资金进出和晚加入账户）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from portfolio_monitor.domain.rules.base import Alert, PortfolioContext, RatioRule, level, pct, threshold_pct


def _today(ctx: PortfolioContext):
    return ctx.perf[-1] if ctx.perf and ctx.perf[-1].date == ctx.as_of else None


class Drawdown(RatioRule):
    id = "drawdown"
    name = "回撤"
    defaults = {"warn": 0.20, "critical": 0.30}

    def evaluate(self, ctx: PortfolioContext, settings: Mapping[str, Any]) -> list[Alert]:
        today = _today(ctx)
        if today is None:
            return []
        peak = max(ctx.perf, key=lambda p: p.index)
        drop = -today.drawdown
        if hit := level(drop, settings):
            return [Alert(rule_id=self.id, severity=hit[0], value=drop, threshold=hit[1],
                          message=f"投资资产距高点（{peak.date}）回撤 {pct(drop)}（阈值 {threshold_pct(hit[1])}）")]
        return []


class DailyDrop(RatioRule):
    id = "daily_drop"
    name = "单日跌幅"
    defaults = {"warn": 0.05, "critical": None}

    def evaluate(self, ctx: PortfolioContext, settings: Mapping[str, Any]) -> list[Alert]:
        today = _today(ctx)
        if today is None or today.daily_return is None or today.daily_return >= 0:
            return []
        drop = -today.daily_return
        if hit := level(drop, settings):
            return [Alert(rule_id=self.id, severity=hit[0], value=drop, threshold=hit[1],
                          message=f"投资资产今日下跌 {pct(drop)}，已剔除资金进出（阈值 {threshold_pct(hit[1])}）")]
        return []
