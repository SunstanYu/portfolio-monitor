from __future__ import annotations

import datetime as dt
import math

import pytest

from portfolio_monitor.domain.returns import period_return
from portfolio_monitor.services.query import range_start

START = dt.date(2026, 1, 1)
CONFIG = {
    "accounts": [
        {"id": "us", "name": "美股", "kind": "broker", "source": "fake", "currency": "USD"},
        {"id": "hk", "name": "港股", "kind": "broker", "source": "fake", "currency": "HKD"},
        {"id": "bank", "name": "银行", "kind": "bank", "source": "manual", "currency": "HKD",
         "include_in_returns": False},
    ],
    "sources": {"fake": {"start": START.isoformat(), "monthly_deposit": 3000}},
}


@pytest.fixture
def app(make_test_app):
    app = make_test_app(CONFIG)
    app.repo.set_manual_cash("bank", "HKD", 20_000)
    d = START
    for _ in range(90):
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            app.sync.run(d)
    return app


@pytest.mark.parametrize("end, key, expected", [
    (dt.date(2026, 3, 31), "1m", dt.date(2026, 2, 28)),
    (dt.date(2026, 3, 31), "3m", dt.date(2025, 12, 31)),
    (dt.date(2026, 3, 31), "1y", dt.date(2025, 3, 31)),
    (dt.date(2024, 2, 29), "1y", dt.date(2023, 2, 28)),
    (dt.date(2026, 3, 31), "ytd", dt.date(2025, 12, 31)),
    (dt.date(2026, 3, 31), "all", None),
])
def test_range_start(end, key, expected):
    assert range_start(end, key) == expected


def test_range_start_rejects_unknown():
    with pytest.raises(ValueError, match="未知区间"):
        range_start(dt.date(2026, 1, 1), "5y")


def test_monthly_returns_compound_to_total(app):
    months = app.query.monthly_returns()
    assert [m.month for m in months] == ["2026-04", "2026-03", "2026-02", "2026-01"]
    total = math.prod(1 + m.twr for m in months) - 1
    assert total == pytest.approx(app.query.performance()[-1].index - 1)
    assert sum(m.net_flow for m in months) > 0


def test_curve_is_rebased_to_range_start(app):
    series = app.query.performance()
    curve = app.query.curve("1m")
    start = range_start(series[-1].date, "1m")
    assert curve.points[0].date <= start < curve.points[1].date
    assert curve.points[0].cum_return == 0
    assert curve.total_return == pytest.approx(period_return(series, start))
    assert all(p.drawdown <= 0 for p in curve.points)
    assert curve.max_drawdown == min(p.drawdown for p in curve.points)
    assert len(app.query.curve("all").points) == len(series)


def test_accounts_summary(app):
    accounts = {a.id: a for a in app.query.accounts()}
    assert sum(a.weight for a in accounts.values()) == pytest.approx(1.0)
    assert accounts["bank"].editable and not accounts["us"].editable
    assert accounts["bank"].value_usd == pytest.approx(20_000 / 7.8)


def test_account_rows_have_in_account_weights_and_cost_price(app):
    rows = app.query.account_rows("us")
    assert sum(r["weight"] for r in rows) == pytest.approx(1.0)
    nvda = next(r for r in rows if r["instrument_id"] == "US:NVDA")
    assert nvda["cost_price"] == pytest.approx(nvda["cost_basis"] / nvda["quantity"])
    cash = next(r for r in rows if r["row_type"] == "cash")
    assert cash["cost_price"] is None


def test_holdings_grouped_by_account_keeps_names(app):
    rows = app.query.holdings(group_by=["account_id"])
    assert {r["account_name"] for r in rows} == {"美股", "港股", "银行"}
