import datetime as dt

import pytest

from portfolio_monitor.domain.returns import Flow, NavPoint, max_drawdown, period_return, twr_series

D = dt.date


def nav(*pairs):
    return [NavPoint(D(2026, 1, d), v) for d, v in pairs]


def test_deposit_is_not_return():
    # 100 → 入金 50 → 150：收益应为 0
    s = twr_series(nav((1, 100), (2, 150)), [Flow(D(2026, 1, 2), 50)])
    assert s[1].daily_return == pytest.approx(0.0)
    assert s[1].index == pytest.approx(1.0)
    assert s[1].net_flow == 50


def test_withdrawal_is_not_loss():
    s = twr_series(nav((1, 100), (2, 60)), [Flow(D(2026, 1, 2), -40)])
    assert s[1].daily_return == pytest.approx(0.0)


def test_chained_returns_ignore_flow_size():
    # +10%，然后入金 1000，再 −10%：TWR = 1.1 × 0.9 − 1 = −1%
    s = twr_series(nav((1, 100), (2, 110), (3, 1110), (4, 999)), [Flow(D(2026, 1, 3), 1000)])
    assert s[-1].index - 1 == pytest.approx(-0.01)


def test_flow_on_gap_day_goes_to_next_nav_date():
    # 1 日、3 日有净值，2 日入金 → 归入 3 日
    s = twr_series(nav((1, 100), (3, 150)), [Flow(D(2026, 1, 2), 50)])
    assert s[1].net_flow == 50
    assert s[1].daily_return == pytest.approx(0.0)


def test_flows_on_or_before_first_day_are_initial_capital():
    s = twr_series(nav((1, 100), (2, 110)), [Flow(D(2026, 1, 1), 100), Flow(D(2025, 12, 1), 5)])
    assert s[1].net_flow == 0
    assert s[1].daily_return == pytest.approx(0.10)


def test_empty_account_day_has_no_return():
    s = twr_series(nav((1, 0), (2, 100)), [Flow(D(2026, 1, 2), 100)])
    assert s[1].daily_return is None
    assert s[1].index == 1.0


def test_drawdown_and_period_return():
    s = twr_series(nav((1, 100), (2, 120), (3, 90), (4, 108)), [])
    assert s[2].drawdown == pytest.approx(-0.25)
    assert max_drawdown(s) == pytest.approx(-0.25)
    assert period_return(s, D(2026, 1, 2)) == pytest.approx(108 / 120 - 1)
    assert period_return(s, D(2025, 12, 31)) == pytest.approx(0.08)  # 早于首日 → 从首日算


def test_duplicate_dates_rejected():
    with pytest.raises(ValueError):
        twr_series(nav((1, 100), (1, 101)), [])


def test_empty_series():
    assert twr_series([], []) == []
    assert period_return([], D(2026, 1, 1)) is None
