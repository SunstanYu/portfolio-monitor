import pytest

from portfolio_monitor.domain.aggregate import aggregate

ROWS = [
    {"instrument_id": "US:A", "name": "A", "account_id": "x", "currency": "USD",
     "market_value_usd": 60.0, "cost_basis_usd": 50.0, "unrealized_pnl_usd": 10.0, "quantity": 6},
    {"instrument_id": "US:A", "name": None, "account_id": "y", "currency": "USD",
     "market_value_usd": 20.0, "cost_basis_usd": None, "unrealized_pnl_usd": None, "quantity": 2},
    {"instrument_id": "CASH:HKD", "name": "HKD 现金", "account_id": "y", "currency": "HKD",
     "market_value_usd": 20.0, "cost_basis_usd": 20.0, "unrealized_pnl_usd": None, "quantity": 156},
]


def test_group_across_accounts_and_weights():
    out = aggregate(ROWS, ["instrument_id"], keep=["name"],
                    sum_fields=("market_value_usd", "cost_basis_usd", "unrealized_pnl_usd", "quantity"))
    a = out[0]
    assert a["instrument_id"] == "US:A"
    assert a["name"] == "A"                      # 取组内第一个非空
    assert a["market_value_usd"] == 80.0
    assert a["cost_basis_usd"] == 50.0           # None 视为缺失
    assert a["quantity"] == 8
    assert a["count"] == 2
    assert a["weight"] == pytest.approx(0.8)
    assert sum(r["weight"] for r in out) == pytest.approx(1.0)
    assert out[1]["unrealized_pnl_usd"] is None  # 整组缺失 → None


def test_filters_change_denominator():
    out = aggregate(ROWS, ["currency"], filters={"account_id": "y"})
    assert {r["currency"]: r["weight"] for r in out} == {"USD": 0.5, "HKD": 0.5}


def test_filter_with_collection():
    out = aggregate(ROWS, [], filters={"instrument_id": {"US:A"}})
    assert len(out) == 2
    assert out[0]["market_value_usd"] == 60.0    # 不分组 → 逐行、按市值降序


def test_empty_input():
    assert aggregate([], ["currency"]) == []
