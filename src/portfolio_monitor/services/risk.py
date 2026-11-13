"""风险提示：用估值日的数据执行规则，结果存进 alerts 表；查询时标出新出现的和持续多久。

每日同步算完净值后自动执行；改了阈值可以用 `alerts --recheck` 只重算规则（不重新拉数据源）。
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

from portfolio_monitor.domain.rules import RULE_NAMES, Alert, PortfolioContext, RuleConfig, Severity, evaluate_all
from portfolio_monitor.services.query import QueryService
from portfolio_monitor.storage.repository import Repository


class AlertView(BaseModel):
    rule_id: str
    rule_name: str
    subject: str = Field(description="标的 / 账户 / 资产类别 / 数据源；组合层面为空")
    subject_name: str | None
    severity: Severity = Field(description="warn 提示 / critical 严重")
    message: str
    value: float | None = Field(description="当前值；比例为小数")
    threshold: float | None = Field(description="越过的阈值")
    since: dt.date = Field(description="从这个估值日起连续出现")
    new: bool = Field(description="今天新出现（前一个估值日没有）")
    escalated: bool = Field(description="今天从提示升级为严重")


class AlertsResult(BaseModel):
    as_of: dt.date
    evaluated: bool = Field(description="这个估值日是否算过规则")
    critical: int
    warn: int
    alerts: list[AlertView]


class RiskService:
    def __init__(self, repo: Repository, query: QueryService, config: RuleConfig) -> None:
        self.repo = repo
        self.query = query
        self.config = config

    def evaluate(self, as_of: dt.date) -> list[Alert]:
        """执行全部规则并覆盖该估值日的结果。"""
        sources = {a.source for a in self.repo.list_accounts(active_only=True)}
        ctx = PortfolioContext(
            as_of=as_of,
            rows=self.query.valued_rows(as_of),
            perf=self.query.performance(end=as_of),
            failures=self.repo.consecutive_failures(as_of, sources),
            targets=self.config.targets,
        )
        alerts = evaluate_all(ctx, self.config)
        self.repo.replace_alerts(as_of, alerts)
        return alerts

    def alerts(self, as_of: dt.date | None = None) -> AlertsResult:
        day = as_of or self.query.latest_date()
        if day is None:
            raise LookupError("还没有任何净值数据，请先运行同步")
        rows = self.repo.alerts_on(day)
        prev_day = self.repo.previous_alert_run(day)
        prev = {(r["rule_id"], r["subject"]): r["severity"] for r in self.repo.alerts_on(prev_day)} if prev_day else {}
        views = []
        for r in rows:
            key = (r["rule_id"], r["subject"])
            since = dt.date.fromisoformat(self.repo.alert_since(day, *key))
            views.append(AlertView(
                rule_id=r["rule_id"], rule_name=RULE_NAMES.get(r["rule_id"], r["rule_id"]),
                subject=r["subject"], subject_name=r["subject_name"], severity=Severity(r["severity"]),
                message=r["message"], value=r["value"], threshold=r["threshold"], since=since,
                new=key not in prev,
                escalated=r["severity"] == Severity.CRITICAL and prev.get(key) == Severity.WARN,
            ))
        order = {rid: i for i, rid in enumerate(RULE_NAMES)}
        views.sort(key=lambda v: (v.severity != Severity.CRITICAL, order.get(v.rule_id, 99), -(abs(v.value or 0))))
        return AlertsResult(
            as_of=day, evaluated=self.repo.alert_run(day) is not None,
            critical=sum(v.severity == Severity.CRITICAL for v in views),
            warn=sum(v.severity == Severity.WARN for v in views), alerts=views,
        )
