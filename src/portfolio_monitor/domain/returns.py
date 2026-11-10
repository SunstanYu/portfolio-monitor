"""时间加权收益率（TWR）与回撤。纯计算，不做 IO。

    r_t = (NAV_t − F_t) / NAV_{t−1} − 1
    F_t：上一个净值日（不含）到本净值日（含）之间的净入金，视为收盘时发生
    指数 = Π(1 + r_t)，首日为 1

首个净值日当天及之前的资金进出属于初始本金，不参与计算。
上一日净值 ≤ 0（账户为空）时该日收益记为 None，指数保持不变。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class NavPoint:
    date: dt.date
    nav: float


@dataclass(frozen=True)
class Flow:
    date: dt.date
    amount: float  # USD，入正出负


@dataclass(frozen=True)
class PerfPoint:
    date: dt.date
    nav: float
    net_flow: float
    daily_return: float | None
    index: float
    drawdown: float


def twr_series(navs: Sequence[NavPoint], flows: Sequence[Flow]) -> list[PerfPoint]:
    points = sorted(navs, key=lambda p: p.date)
    if not points:
        return []
    dates = [p.date for p in points]
    if len(set(dates)) != len(dates):
        raise ValueError("净值序列存在重复日期")

    flow_by_point = [0.0] * len(points)
    for f in flows:
        if f.date <= dates[0]:
            continue
        # 归入第一个 >= 流水日期的净值日；晚于最后一个净值日的暂不计入
        for i in range(1, len(points)):
            if dates[i - 1] < f.date <= dates[i]:
                flow_by_point[i] += f.amount
                break

    index, peak = 1.0, 1.0
    out = [PerfPoint(points[0].date, points[0].nav, 0.0, None, 1.0, 0.0)]
    for i in range(1, len(points)):
        prev, cur, flow = points[i - 1].nav, points[i].nav, flow_by_point[i]
        r = None if prev <= 0 else (cur - flow) / prev - 1
        if r is not None:
            index *= 1 + r
        peak = max(peak, index)
        out.append(PerfPoint(points[i].date, cur, flow, r, index, index / peak - 1))
    return out


def period_return(series: Sequence[PerfPoint], since: dt.date) -> float | None:
    """从 since 当天（或之前最近一个净值日）收盘到最后一个净值日的收益。"""
    if not series:
        return None
    base = None
    for p in series:
        if p.date <= since:
            base = p
        else:
            break
    if base is None:
        base = series[0]
    return series[-1].index / base.index - 1


def max_drawdown(series: Sequence[PerfPoint]) -> float:
    return min((p.drawdown for p in series), default=0.0)
