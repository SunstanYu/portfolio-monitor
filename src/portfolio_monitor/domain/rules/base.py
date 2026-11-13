"""规则的公共部分：提示、上下文、Rule 接口、阈值校验。只做计算，不访问数据库。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from portfolio_monitor.domain.returns import PerfPoint


class Severity(StrEnum):
    WARN = "warn"          # 提示
    CRITICAL = "critical"  # 严重


class Alert(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str
    subject: str = ""  # 标的 / 账户 / 资产类别 / 数据源；组合层面为空
    subject_name: str | None = None
    severity: Severity
    message: str
    value: float | None = None
    threshold: float | None = None


@dataclass(frozen=True)
class PortfolioContext:
    """规则看到的全部数据。rows 是估值日的持仓和现金行（已换算 market_value_usd、notional_usd）。"""

    as_of: dt.date
    rows: Sequence[Mapping[str, Any]]
    perf: Sequence[PerfPoint]  # 截至 as_of 的投资资产 TWR 序列
    failures: Mapping[str, tuple[int, str | None]] = field(default_factory=dict)  # 数据源 → (连续失败天数, 错误)
    targets: Mapping[str, float] = field(default_factory=dict)  # 资产类别 → 目标占比

    @property
    def total(self) -> float:
        return sum(r["market_value_usd"] or 0.0 for r in self.rows)

    @property
    def investable(self) -> float:
        return sum(r["market_value_usd"] or 0.0 for r in self.rows if r["include_in_returns"])


class Rule(Protocol):
    id: ClassVar[str]
    name: ClassVar[str]
    defaults: ClassVar[dict[str, Any]]  # 可配置项及默认值（不含 enabled）

    def check(self, settings: Mapping[str, Any]) -> list[str]:
        """配置有误时返回问题列表。"""
        ...

    def evaluate(self, ctx: PortfolioContext, settings: Mapping[str, Any]) -> list[Alert]: ...


def level(value: float, settings: Mapping[str, Any]) -> tuple[Severity, float] | None:
    """value 超过 critical → 严重，超过 warn → 提示；返回 (级别, 越过的阈值)。"""
    critical, warn = settings.get("critical"), settings.get("warn")
    if critical is not None and value > critical:
        return Severity.CRITICAL, critical
    if warn is not None and value > warn:
        return Severity.WARN, warn
    return None


def check_thresholds(rule_id: str, s: Mapping[str, Any], *, days: bool = False, upper: float = 1.0) -> list[str]:
    """warn / critical：比例在 (0, upper]，天数为正整数；critical 必须大于 warn。"""
    problems = []
    where = f"[rules.{rule_id}]"
    for key in ("warn", "critical"):
        v = s.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int | float):
            problems.append(f"{where}.{key} 必须是数字")
        elif days and (int(v) != v or v < 1):
            problems.append(f"{where}.{key} 是天数，必须是正整数")
        elif not days and not 0 < v <= upper:
            hint = f"，{v:g} 看起来是百分数，应写成 {v / 100:g}" if upper < v <= 100 else ""
            problems.append(f"{where}.{key} 是比例，必须在 0 到 {upper:g} 之间{hint}")
    w, c = s.get("warn"), s.get("critical")
    if w is None and c is None:
        problems.append(f"{where} 至少要设 warn 或 critical")
    elif isinstance(w, int | float) and isinstance(c, int | float) and c <= w:
        problems.append(f"{where} 的 critical（{c:g}）必须大于 warn（{w:g}）")
    return problems


class RatioRule:
    """阈值为比例的规则的默认校验。"""

    id: ClassVar[str]
    defaults: ClassVar[dict[str, Any]]

    def check(self, settings: Mapping[str, Any]) -> list[str]:
        return check_thresholds(self.id, settings)


def pct(x: float) -> str:
    return f"{x:.1%}"


def threshold_pct(x: float) -> str:
    return f"{x:.0%}" if round(x * 100, 6) == round(x * 100) else f"{x:.1%}"
