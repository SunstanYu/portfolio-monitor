from __future__ import annotations

import datetime as dt
import json

import pytest

from portfolio_monitor.domain.models import (
    Account,
    AssetClass,
    Instrument,
    Position,
    SourceSnapshot,
)
from portfolio_monitor.sources.base import SnapshotInvalid, validate_snapshot
from portfolio_monitor.sources.fx import FxError, StaticFx

START = dt.date(2026, 1, 1)
FAKE = {"id": "acct-usd", "name": "U", "kind": "broker", "source": "fake", "currency": "USD"}
FAKE_HK = {"id": "acct-hkd", "name": "H", "kind": "broker", "source": "fake", "currency": "HKD"}
BANK = {"id": "bank", "name": "B", "kind": "bank", "source": "manual", "currency": "HKD",
        "include_in_returns": False}


def weekdays(start: dt.date, n: int) -> list[dt.date]:
    out, d = [], start
    while len(out) < n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def config(**fake_settings):
    return {"accounts": [FAKE, FAKE_HK, BANK],
            "sources": {"fake": {"start": START.isoformat(), **fake_settings}}}


def test_twr_is_zero_when_prices_flat_despite_deposits(make_test_app):
    app = make_test_app(config(vol_scale=0, drift_scale=0, monthly_deposit=5000))
    app.repo.set_manual_cash("bank", "HKD", 10_000)
    for d in weekdays(START, 60):
        assert app.sync.run(d).ok

    series = app.query.performance()
    assert sum(p.net_flow for p in series) > 0, "期间应有入金"
    assert series[-1].nav > series[0].nav, "入金使净值上升"
    assert series[-1].index == pytest.approx(1.0), "价格不变时 TWR 必须为 0"
    # 银行账户不计入收益率，但计入总资产
    o = app.query.overview()
    assert o.nav_total_usd == pytest.approx(o.nav_investable_usd + 10_000 / 7.8)


def test_flows_are_deduplicated_across_runs(make_test_app):
    app = make_test_app(config(monthly_deposit=1000, flow_lookback_days=10))
    days = weekdays(START, 45)               # 到 3 月初
    for d in days:
        app.sync.run(d)
    app.sync.run(days[-1])                   # 同一天重跑
    flows = app.repo.cash_flows()
    ids = [f.external_id for f in flows]
    # 起始日（1 月 1 日）当天的入金属于初始本金；之后 2 月、3 月各一笔，
    # 每笔在回看窗口内被返回多次，但只入库一次
    assert ids == ["fake:acct-hkd:2026-02", "fake:acct-hkd:2026-03"]


def test_failed_source_carries_forward_and_others_continue(make_test_app):
    days = weekdays(START, 5)
    app = make_test_app(config(fail_on=[days[2].isoformat()]))
    app.repo.set_manual_cash("bank", "HKD", 10_000)
    reports = [app.sync.run(d) for d in days]

    bad = reports[2]
    fake = next(s for s in bad.sources if s.source == "fake")
    manual = next(s for s in bad.sources if s.source == "manual")
    assert fake.status == "failed" and "模拟故障" in fake.error
    assert sorted(fake.carried_forward) == ["acct-hkd", "acct-usd"]
    assert manual.status == "ok"
    assert bad.stale and not bad.ok

    prev = app.repo.snapshot_rows(days[1])
    cur = app.repo.snapshot_rows(days[2])
    assert {r["instrument_id"] for r in cur} == {r["instrument_id"] for r in prev}
    assert all(r["carried_forward"] for r in cur if r["account_id"] != "bank")
    assert reports[3].ok and not reports[3].stale   # 次日恢复


def test_fx_failure_uses_last_rate_then_reports_missing(make_test_app):
    class Flaky(StaticFx):
        fail = False

        def fetch(self, day, currencies):
            if self.fail:
                raise FxError("down")
            return super().fetch(day, currencies)

    app = make_test_app(config())
    flaky = Flaky()
    app.sync.fx = flaky
    days = weekdays(START, 3)
    assert app.sync.run(days[0]).fx_status == "ok"
    flaky.fail = True
    r = app.sync.run(days[1])
    assert r.fx_status == "carried_forward" and r.nav_total is not None
    assert days[0].isoformat() in r.fx_detail   # 说明沿用了哪天的汇率


def test_missing_fx_without_history_skips_nav(make_test_app):
    app = make_test_app(config())

    class Down(StaticFx):
        def fetch(self, day, currencies):
            raise FxError("down")

    app.sync.fx = Down()
    r = app.sync.run(weekdays(START, 1)[0])
    assert r.fx_status == "missing"
    assert r.nav_total is None and not r.ok
    assert "HKD" in r.fx_detail


def test_raw_archive_written(make_test_app, tmp_path):
    app = make_test_app(config())
    d = weekdays(START, 1)[0]
    app.sync.run(d)
    raw = tmp_path / "data" / "raw" / d.isoformat() / "fake.json"
    assert json.loads(raw.read_text(encoding="utf-8"))["as_of"] == d.isoformat()


def test_unregistered_source_fails_cleanly(make_test_app):
    app = make_test_app({"accounts": [FAKE], "sources": {"fake": {"start": START.isoformat()}}})
    app.repo.sync_accounts([Account(**FAKE), Account(id="x", name="X", kind="broker",
                                                     source="nope", currency="USD")])
    r = app.sync.run(weekdays(START, 1)[0])
    nope = next(s for s in r.sources if s.source == "nope")
    assert nope.status == "failed" and "未注册" in nope.error


def test_holdings_merge_across_accounts(make_test_app):
    app = make_test_app(config())
    app.repo.upsert_instruments([Instrument(id="US:NVDA", name="NVIDIA",
                                            asset_class=AssetClass.EQUITY, currency="USD")])
    # 银行账户里也放一点同一标的，验证跨账户合并
    app.repo.set_manual_holding("bank", "US:NVDA", 10, 100.0, "USD")
    d = weekdays(START, 1)[0]
    app.sync.run(d)
    rows = {r["instrument_id"]: r for r in app.query.holdings(d)}
    assert rows["US:NVDA"]["count"] == 2
    assert rows["US:NVDA"]["quantity"] == 130
    assert sum(r["weight"] for r in rows.values()) == pytest.approx(1.0)


def test_validate_snapshot_catches_problems():
    acct = Account(**FAKE)
    inst = Instrument(id="0700.HK", asset_class=AssetClass.EQUITY, currency="HKD")
    snap = SourceSnapshot(
        source="fake", as_of=START,
        positions=[Position(account_id="other", instrument_id="0700.HK", quantity=1,
                            market_value=1, currency="USD")],
        instruments=[inst],
    )
    with pytest.raises(SnapshotInvalid) as e:
        validate_snapshot(snap, "fake", START, [acct])
    text = str(e.value)
    assert "不属于本数据源的账户" in text
    assert "标的编号不规范" in text
    assert "币种" in text
