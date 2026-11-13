"""风险规则：注册表、配置解析、统一执行。

新增规则：写一个类（id、name、defaults、check、evaluate），加进 RULES；阈值写在配置 [rules.<id>]。
比例一律写小数（0.20 表示 20%）；没写的项用 defaults。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from portfolio_monitor.domain.models import AssetClass
from portfolio_monitor.domain.rules.base import Alert, PortfolioContext, Rule, Severity
from portfolio_monitor.domain.rules.data_health import DataJump, SourceFailures
from portfolio_monitor.domain.rules.exposure import (
    AccountConcentration,
    AllocationDrift,
    IdleCash,
    InstrumentConcentration,
    Leverage,
)
from portfolio_monitor.domain.rules.market import DailyDrop, Drawdown

RULES: list[Rule] = [
    InstrumentConcentration(),
    AccountConcentration(),
    AllocationDrift(),
    IdleCash(),
    Leverage(),
    Drawdown(),
    DailyDrop(),
    SourceFailures(),
    DataJump(),
]
RULE_NAMES = {r.id: r.name for r in RULES}


@dataclass(frozen=True)
class RuleConfig:
    settings: dict[str, dict[str, Any]] = field(default_factory=lambda: parse_rule_config({}, {}).settings)
    targets: dict[str, float] = field(default_factory=dict)

    def enabled(self, rule_id: str) -> bool:
        return bool(self.settings[rule_id].get("enabled", True))


def parse_rule_config(rules: Mapping[str, Any], targets: Mapping[str, Any]) -> RuleConfig:
    """合并默认值并校验 [rules.*] 和 [targets]；有问题时一次列出全部。"""
    problems: list[str] = []
    known = {r.id: r for r in RULES}
    for rid in rules:
        if rid not in known:
            problems.append(f"未知规则 [rules.{rid}]（可用：{', '.join(known)}）")
    merged: dict[str, dict[str, Any]] = {}
    for rule in RULES:
        raw = dict(rules.get(rule.id) or {})
        if extra := sorted(set(raw) - set(rule.defaults) - {"enabled"}):
            problems.append(f"[rules.{rule.id}] 不认识的设置：{', '.join(extra)}")
        if "enabled" in raw and not isinstance(raw["enabled"], bool):
            problems.append(f"[rules.{rule.id}].enabled 必须是 true 或 false")
        s = {**rule.defaults, **raw}
        problems += rule.check(s)
        merged[rule.id] = s

    classes = {c.value for c in AssetClass}
    parsed: dict[str, float] = {}
    for cls, v in targets.items():
        if cls not in classes:
            problems.append(f"[targets] 不认识的资产类别：{cls}（可用：{', '.join(sorted(classes))}）")
        elif isinstance(v, bool) or not isinstance(v, int | float) or not 0 <= v <= 1:
            problems.append(f"[targets].{cls} 是比例，必须在 0 到 1 之间")
        else:
            parsed[cls] = float(v)
    if parsed and abs(sum(parsed.values()) - 1) > 0.001:
        problems.append(f"[targets] 各项加起来应为 1，现在是 {sum(parsed.values()):g}")

    if problems:
        raise ValueError("规则配置有误：" + "；".join(problems))
    return RuleConfig(settings=merged, targets=parsed)


_ORDER = {r.id: i for i, r in enumerate(RULES)}


def evaluate_all(ctx: PortfolioContext, config: RuleConfig) -> list[Alert]:
    """执行全部启用的规则；严重的在前，同级按规则顺序。"""
    alerts: list[Alert] = []
    for rule in RULES:
        if config.enabled(rule.id):
            alerts += rule.evaluate(ctx, config.settings[rule.id])
    return sorted(alerts, key=lambda a: (a.severity != Severity.CRITICAL, _ORDER[a.rule_id]))


__all__ = ["RULES", "RULE_NAMES", "Alert", "PortfolioContext", "Rule", "RuleConfig", "Severity",
           "evaluate_all", "parse_rule_config"]
