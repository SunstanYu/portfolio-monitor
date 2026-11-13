"""数据健康：数据源连续失败、单日变化大得不正常（疑似数据错误）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from portfolio_monitor.domain.rules.base import (
    Alert,
    PortfolioContext,
    RatioRule,
    Severity,
    check_thresholds,
    level,
    threshold_pct,
)


class SourceFailures:
    """某数据源截至估值日连续失败的估值日数（失败时会沿用上次快照，净值不准）。"""

    id = "source_failures"
    name = "数据源连续失败"
    defaults = {"warn": 2, "critical": 5}

    def check(self, settings: Mapping[str, Any]) -> list[str]:
        return check_thresholds(self.id, settings, days=True)

    def evaluate(self, ctx: PortfolioContext, settings: Mapping[str, Any]) -> list[Alert]:
        crit, warn = settings.get("critical"), settings.get("warn")
        alerts = []
        for source, (days, error) in sorted(ctx.failures.items()):
            if crit is not None and days >= crit:
                severity, threshold = Severity.CRITICAL, crit
            elif warn is not None and days >= warn:
                severity, threshold = Severity.WARN, warn
            else:
                continue
            reason = f"，最近一次：{error[:120]}" if error else ""
            alerts.append(Alert(rule_id=self.id, subject=source, subject_name=source, severity=severity,
                                value=days, threshold=threshold,
                                message=f"{source} 已连续 {days} 个估值日同步失败，期间沿用旧数据{reason}"))
        return alerts


class DataJump(RatioRule):
    """投资资产单日变化（TWR，已剔除资金进出和晚加入账户）大得不正常。"""

    id = "data_jump"
    name = "疑似数据错误"
    defaults = {"warn": 0.15, "critical": None}

    def check(self, settings: Mapping[str, Any]) -> list[str]:
        return check_thresholds(self.id, settings, upper=10.0)

    def evaluate(self, ctx: PortfolioContext, settings: Mapping[str, Any]) -> list[Alert]:
        today = ctx.perf[-1] if ctx.perf and ctx.perf[-1].date == ctx.as_of else None
        if today is None or today.daily_return is None:
            return []
        move = today.daily_return
        if hit := level(abs(move), settings):
            return [Alert(rule_id=self.id, severity=hit[0], value=move, threshold=hit[1],
                          message=(f"投资资产今日变化 {move:+.1%}，已剔除资金进出，可能是数据错误，"
                                   f"请核对（阈值 {threshold_pct(hit[1])}）"))]
        return []

