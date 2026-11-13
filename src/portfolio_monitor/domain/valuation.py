"""估值：把快照行按汇率换算成 USD。纯计算，不做 IO。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


class MissingFxRate(LookupError):
    def __init__(self, currencies: Iterable[str]) -> None:
        self.currencies = sorted(set(currencies))
        super().__init__(f"缺少汇率：{', '.join(self.currencies)}")


def to_usd(amount: float | None, currency: str, usd_per_unit: Mapping[str, float]) -> float | None:
    if amount is None:
        return None
    if currency == "USD":
        return amount
    return amount * usd_per_unit[currency]


def value_rows(
    rows: Iterable[Mapping[str, Any]], usd_per_unit: Mapping[str, float]
) -> list[dict[str, Any]]:
    """给每一行加上 *_usd 字段和浮盈；缺汇率时整体报错，不静默跳过。"""
    rows = list(rows)
    missing = {r["currency"] for r in rows if r["currency"] != "USD" and r["currency"] not in usd_per_unit}
    if missing:
        raise MissingFxRate(missing)
    valued: list[dict[str, Any]] = []
    for r in rows:
        ccy = r["currency"]
        mv_usd = to_usd(r["market_value"], ccy, usd_per_unit)
        cost_usd = to_usd(r.get("cost_basis"), ccy, usd_per_unit)
        out = dict(r)
        out["market_value_usd"] = mv_usd
        out["cost_basis_usd"] = cost_usd
        out["notional_usd"] = to_usd(r.get("notional"), ccy, usd_per_unit)
        out["unrealized_pnl_usd"] = (
            None if r["row_type"] == "cash" or cost_usd is None else mv_usd - cost_usd
        )
        out["include_in_returns"] = bool(r["include_in_returns"])
        out["carried_forward"] = bool(r["carried_forward"])
        valued.append(out)
    return valued


def nav(valued: Iterable[Mapping[str, Any]], *, investable_only: bool = False) -> float:
    return sum(
        r["market_value_usd"] for r in valued if not investable_only or r["include_in_returns"]
    )
