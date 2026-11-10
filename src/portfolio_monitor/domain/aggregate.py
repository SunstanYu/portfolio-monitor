"""通用聚合：所有表格都是"筛选 + 分组 + 求和"。纯计算，不做 IO。"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any

DEFAULT_SUM_FIELDS = ("market_value_usd", "cost_basis_usd", "unrealized_pnl_usd")


def _matches(value: Any, wanted: Any) -> bool:
    if isinstance(wanted, Collection) and not isinstance(wanted, str):
        return value in wanted
    return value == wanted


def _sum(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def aggregate(
    rows: Iterable[Mapping[str, Any]],
    group_by: Sequence[str] = (),
    *,
    filters: Mapping[str, Any] | None = None,
    sum_fields: Sequence[str] = DEFAULT_SUM_FIELDS,
    keep: Sequence[str] = (),
    weight_of: str = "market_value_usd",
) -> list[dict[str, Any]]:
    """按 group_by 分组求和，返回带 weight（占筛选后总额比例）的行，按 weight_of 降序。

    - filters：{字段: 值 或 值集合}
    - keep：组内取第一个非空值的字段（如按标的分组时保留 name）
    - 分组为空时不合并，逐行返回
    - 求和时 None 视为缺失；整组都缺失则结果为 None
    """
    selected = [r for r in rows if all(_matches(r.get(k), v) for k, v in (filters or {}).items())]
    total = sum(r.get(weight_of) or 0.0 for r in selected)

    if not group_by:
        result = [dict(r) for r in selected]
    else:
        groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
        for r in selected:
            groups.setdefault(tuple(r.get(k) for k in group_by), []).append(r)
        result = []
        for key, members in groups.items():
            out: dict[str, Any] = dict(zip(group_by, key, strict=True))
            for f in keep:
                out[f] = next((m.get(f) for m in members if m.get(f) is not None), None)
            for f in sum_fields:
                out[f] = _sum(m.get(f) for m in members)
            out["count"] = len(members)
            result.append(out)

    for r in result:
        r["weight"] = (r.get(weight_of) or 0.0) / total if total else 0.0
    result.sort(key=lambda r: r.get(weight_of) or 0.0, reverse=True)
    return result
