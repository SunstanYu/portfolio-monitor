from __future__ import annotations

import datetime as dt

import pytest

from portfolio_monitor.services.edit import EditError, parse_request

START = dt.date(2026, 1, 1)
CONFIG = {
    "accounts": [
        {"id": "us", "name": "美股", "kind": "broker", "source": "fake", "currency": "USD"},
        {"id": "cn", "name": "A 股", "kind": "broker", "source": "manual", "currency": "CNY"},
        {"id": "bank", "name": "银行", "kind": "bank", "source": "manual", "currency": "HKD",
         "include_in_returns": False},
    ],
    # 价格不动，便于验证收益率
    "sources": {"fake": {"start": START.isoformat(), "vol_scale": 0, "drift_scale": 0,
                         "monthly_deposit": 0}},
}


def weekdays(n: int) -> list[dt.date]:
    out, d = [], START
    while len(out) < n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


@pytest.fixture
def app(make_test_app):
    app = make_test_app(CONFIG)
    app.repo.set_manual_cash("cn", "CNY", 10_000)
    for d in weekdays(5):
        assert app.sync.run(d).ok
    return app


def nav(app) -> float:
    return app.query.overview().nav_total_usd


def holding(qty, price, cost=None, symbol="600036", **kw):
    return {"kind": "set_holding", "account_id": "cn", "symbol": symbol, "quantity": qty,
            "price": price, "cost_price": cost, **kw}


def test_add_holding_creates_instrument_and_refreshes_nav(app):
    before = nav(app)
    ch = app.edit.apply(holding(100, 40, 38, name="招商银行"), actor="web")

    assert ch.status == "applied" and ch.refresh_error is None
    assert ch.summary == "新增持仓 招商银行（CN:600036）：数量 100，价格 40 CNY，成本价 38"
    inst = app.repo.get_instrument("CN:600036")
    assert inst.currency == "CNY" and inst.region == "CN"
    # 立即反映到最新估值日
    assert nav(app) == pytest.approx(before + 100 * 40 / 7.2)
    rows = {r["instrument_id"]: r for r in app.query.holdings(filters={"account_id": "cn"})}
    assert rows["CN:600036"]["cost_basis_usd"] == pytest.approx(3800 / 7.2)


def test_update_keeps_cost_price_when_not_given(app):
    app.edit.apply(holding(100, 40, 38), actor="web")
    ch = app.edit.apply(holding(200, 41), actor="web")
    assert ch.summary == "修改持仓 CN:600036：数量 100 → 200；价格 40 → 41 CNY"
    assert app.repo.get_manual_holding("cn", "CN:600036")["cost_basis"] == pytest.approx(200 * 38)


@pytest.mark.parametrize("request_, message", [
    (holding(1, 1) | {"account_id": "us"}, "只能修改手动账户"),
    (holding(1, 1) | {"account_id": "nope"}, "没有这个账户"),
    (holding(1, 1, symbol="!!"), "认不出这个代码"),
    (holding(-1, 1), "数量：必须大于 0"),
    (holding("abc", 1), "数量：必须是数字"),
    ({"kind": "set_cash", "account_id": "cn", "currency": "CNY", "amount": 10_000}, "没有变化"),
    ({"kind": "remove_holding", "account_id": "cn", "instrument_id": "CN:600519"}, "没有持仓"),
    ({"kind": "record_flow", "account_id": "cn", "currency": "CNY", "amount": -20_000}, "现金为负"),
    ({"kind": "record_flow", "account_id": "cn", "currency": "CNY", "amount": 0}, "金额不能为 0"),
    ({"kind": "oops", "account_id": "cn"}, "未知的修改类型"),
])
def test_rejections_are_explained(app, request_, message):
    with pytest.raises(EditError, match=message):
        app.edit.apply(request_, actor="web")
    assert app.repo.list_changes(statuses=["applied"]) == []


def test_remove_then_undo_restores(app):
    app.edit.apply(holding(100, 40), actor="web")
    with_holding = nav(app)
    removed = app.edit.apply({"kind": "remove_holding", "account_id": "cn",
                              "instrument_id": "CN:600036"}, actor="web")
    assert app.repo.get_manual_holding("cn", "CN:600036") is None
    assert nav(app) < with_holding

    undone = app.edit.undo(removed.id)
    assert undone.status == "undone"
    assert app.repo.get_manual_holding("cn", "CN:600036")["quantity"] == 100
    assert nav(app) == pytest.approx(with_holding)


def test_undo_must_follow_reverse_order(app):
    a = app.edit.apply(holding(100, 40), actor="web")
    b = app.edit.apply(holding(150, 40), actor="web")
    other = app.edit.apply({"kind": "set_cash", "account_id": "bank", "currency": "HKD",
                            "amount": 500}, actor="web")

    with pytest.raises(EditError, match=f"#{b.id}"):
        app.edit.undo(a.id)
    views = {v.id: v for v in app.edit.list()}
    assert not views[a.id].can_undo and views[a.id].undo_blocked_by == b.id
    assert views[b.id].can_undo and views[other.id].can_undo   # 不相关的修改互不影响

    app.edit.undo(b.id)
    app.edit.undo(a.id)
    assert app.repo.get_manual_holding("cn", "CN:600036") is None
    with pytest.raises(EditError, match="只能撤销已生效"):
        app.edit.undo(a.id)


def test_two_step_preview_then_confirm(app):
    p = app.edit.preview(holding(100, 40), actor="mcp")
    assert p.status == "pending" and p.expires_at
    assert app.repo.get_manual_holding("cn", "CN:600036") is None   # 预览不落库

    assert app.edit.confirm(p.id).status == "applied"
    with pytest.raises(EditError, match="不能再确认"):
        app.edit.confirm(p.id)

    # 预览之后同一项被改了 → 确认失败
    stale = app.edit.preview(holding(300, 40), actor="mcp")
    app.edit.apply(holding(200, 40), actor="web")
    with pytest.raises(EditError, match="数据已经变了"):
        app.edit.confirm(stale.id)
    assert app.repo.get_manual_holding("cn", "CN:600036")["quantity"] == 200

    # 过期
    late = app.edit.preview(holding(400, 40), actor="mcp")
    app.repo.update_change(late.id, expires_at="2000-01-01T00:00:00+00:00")
    with pytest.raises(EditError, match="已过期"):
        app.edit.confirm(late.id)
    assert app.edit.get(late.id).status == "discarded"


def test_recorded_deposit_is_not_counted_as_return(app):
    flow = app.edit.apply({"kind": "record_flow", "account_id": "cn", "currency": "CNY",
                           "amount": 7_200, "note": "工资"}, actor="web")
    assert app.repo.get_manual_cash("cn", "CNY") == 17_200
    assert "入金 CNY 7,200" in flow.summary and "现金 10,000 → 17,200" in flow.summary
    last = app.query.performance()[-1]
    assert last.net_flow == pytest.approx(1_000)
    assert last.daily_return == pytest.approx(0.0)      # 价格不动，入金不算收益

    app.edit.undo(flow.id)
    assert app.repo.get_manual_cash("cn", "CNY") == 10_000
    assert app.repo.cash_flow_rows("cn") == []


def test_cash_change_without_flow_counts_as_return(app):
    app.edit.apply({"kind": "set_cash", "account_id": "cn", "currency": "CNY", "amount": 17_200},
                   actor="web")
    assert app.query.performance()[-1].daily_return > 0


def test_parse_request_accepts_form_strings():
    req = parse_request({"kind": "set_holding", "account_id": "cn", "symbol": " 0700 ",
                         "quantity": "100", "price": "380.5"})
    assert req.symbol == "0700" and req.quantity == 100.0
