"""数据源接口与快照校验。

新增数据源：实现 Source（name + fetch），在 registry.py 里注册一行，
并让它通过 tests/test_source_contract.py 的契约测试。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from portfolio_monitor.domain.instruments import is_canonical
from portfolio_monitor.domain.models import Account, SourceHistory, SourceSnapshot

if TYPE_CHECKING:
    from portfolio_monitor.storage.repository import Repository


class SourceError(RuntimeError):
    """数据源拉取失败（网络、鉴权、对方接口变化等）。"""


class SnapshotInvalid(ValueError):
    def __init__(self, source: str, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__(f"{source} 返回的数据不合法：" + "；".join(self.problems))


@dataclass
class SourceContext:
    repo: Repository
    settings: Mapping[str, Any] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)


class Source(Protocol):
    name: str

    def fetch(self, as_of: dt.date, accounts: Sequence[Account]) -> SourceSnapshot:
        """返回这些账户在 as_of 的持仓、现金，以及近期资金进出（可重复，按 external_id 去重）。"""
        ...


@runtime_checkable
class HistorySource(Source, Protocol):
    """能回填历史的数据源（可选能力）：每日账户总值 + 资金进出。"""

    def history(self, start: dt.date, end: dt.date, accounts: Sequence[Account]) -> SourceHistory:
        """返回 [start, end] 内能拿到的历史；实际区间可能比请求的短（如 end 当天还没有数据）。"""
        ...


def validate_history(hist: SourceHistory, source: str, accounts: Sequence[Account]) -> None:
    problems: list[str] = []
    if hist.source != source:
        problems.append(f"source 应为 {source}，实际为 {hist.source}")
    allowed = {a.id for a in accounts}
    used = {v.account_id for v in hist.values} | {f.account_id for f in hist.flows}
    if stray := sorted(used - allowed):
        problems.append(f"包含不属于本数据源的账户：{stray}")
    if outside := [v for v in hist.values if not hist.start <= v.date <= hist.end]:
        problems.append(f"有 {len(outside)} 条历史总值不在 {hist.start} ~ {hist.end} 内")
    seen: set[tuple[dt.date, str]] = set()
    for v in hist.values:
        if (v.date, v.account_id) in seen:
            problems.append(f"历史总值重复：{v.date} {v.account_id}")
        seen.add((v.date, v.account_id))
    if problems:
        raise SnapshotInvalid(source, problems)


def validate_snapshot(
    snap: SourceSnapshot, source: str, as_of: dt.date, accounts: Sequence[Account]
) -> None:
    problems: list[str] = []
    if snap.source != source:
        problems.append(f"source 应为 {source}，实际为 {snap.source}")
    if snap.as_of != as_of:
        problems.append(f"as_of 应为 {as_of}，实际为 {snap.as_of}")
    if snap.data_date is not None and snap.data_date > as_of:
        problems.append(f"数据日期 {snap.data_date} 晚于估值日 {as_of}")

    allowed = {a.id for a in accounts}
    used = (
        {p.account_id for p in snap.positions}
        | {c.account_id for c in snap.cash}
        | {f.account_id for f in snap.flows}
        | {u.account_id for u in snap.unresolved}
    )
    if stray := sorted(used - allowed):
        problems.append(f"包含不属于本数据源的账户：{stray}")

    instruments = {i.id: i for i in snap.instruments}
    if bad := sorted(i for i in instruments if not is_canonical(i)):
        problems.append(f"标的编号不规范：{bad}")

    seen_pos: set[tuple[str, str]] = set()
    for p in snap.positions:
        key = (p.account_id, p.instrument_id)
        if key in seen_pos:
            problems.append(f"持仓重复：{key}")
        seen_pos.add(key)
        inst = instruments.get(p.instrument_id)
        if inst and inst.currency != p.currency:
            problems.append(f"{p.instrument_id} 持仓币种 {p.currency} 与标的币种 {inst.currency} 不一致")

    seen_cash: set[tuple[str, str]] = set()
    for c in snap.cash:
        key = (c.account_id, c.currency)
        if key in seen_cash:
            problems.append(f"现金重复：{key}")
        seen_cash.add(key)

    if future := [f for f in snap.flows if f.date > as_of]:
        problems.append(f"有 {len(future)} 条资金进出晚于估值日")

    if problems:
        raise SnapshotInvalid(source, problems)
