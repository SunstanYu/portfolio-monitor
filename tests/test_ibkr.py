from __future__ import annotations

import argparse
import datetime as dt

import pytest
from flex_mock import TOKEN, FlexMock

from portfolio_monitor.domain.models import Account, CashBalance
from portfolio_monitor.entry.cli import cmd_backfill
from portfolio_monitor.services.sync import lagging
from portfolio_monitor.sources.base import SourceContext, SourceError
from portfolio_monitor.sources.ibkr import FlexError, IbkrSource, parse_date

AS_OF = dt.date(2026, 3, 2)  # 周一
MAIN = Account(id="ib", name="IBKR", kind="broker", source="ibkr", currency="USD")


def source(repo, mock: FlexMock, accts=(MAIN,), **settings) -> tuple[IbkrSource, list[Account]]:
    repo.sync_accounts(list(accts))
    return IbkrSource(SourceContext(repo=repo, settings=settings), client=mock.client()), list(accts)


# ---------- 解析 ----------

def test_parse_date_formats():
    assert parse_date("20260302") == AS_OF
    assert parse_date("2026-03-02") == AS_OF
    assert parse_date("20260302;153000") == AS_OF
    assert parse_date("2026-03-02 15:30:00") == AS_OF
    assert parse_date("") is None
    with pytest.raises(SourceError):
        parse_date("03/02/2026")


def test_snapshot_mapping(repo):
    src, accounts = source(repo, FlexMock())
    snap = src.fetch(AS_OF, accounts)
    assert snap.data_date == AS_OF

    pos = {p.instrument_id: p for p in snap.positions}
    assert set(pos) == {"US:NVDA", "US:VOO", "US:BRK.B", "HK:00700", "CN:600519",
                        "UNKNOWN:ibkr:OPT-900001"}
    assert pos["US:NVDA"].quantity == 50  # LOT 明细行不重复计入
    assert pos["US:NVDA"].market_value == 6000 and pos["US:NVDA"].cost_basis == 4500
    assert pos["CN:600519"].currency == "CNY"  # CNH 按 CNY 计
    assert pos["UNKNOWN:ibkr:OPT-900001"].market_value == 500

    inst = {i.id: i for i in snap.instruments}
    assert inst["US:VOO"].asset_class == "etf" and inst["US:NVDA"].asset_class == "equity"
    assert inst["HK:00700"].region == "HK" and inst["UNKNOWN:ibkr:OPT-900001"].asset_class == "option"
    assert [u.raw_symbol for u in snap.unresolved] == ["AAPL  260320C00200000"]
    assert snap.unresolved[0].account_id == "ib"

    assert snap.cash == [CashBalance(account_id="ib", currency="HKD", amount=10000),
                         CashBalance(account_id="ib", currency="USD", amount=3000)]


def test_flows_only_money_in_and_out(repo):
    src, accounts = source(repo, FlexMock())
    flows = {f.external_id: f for f in src.fetch(AS_OF, accounts).flows}
    # 股息、预扣税、利息是收益，不是资金进出；汇总行不计入
    assert set(flows) == {"ibkr:7001", "ibkr:7002", "ibkr:7101", "ibkr:7102"}
    # 按 reportDate 记（IBKR 净值从这天起才包含这笔钱），不是 dateTime
    assert flows["ibkr:7001"].amount == 5000 and flows["ibkr:7001"].date == dt.date(2026, 2, 26)
    assert flows["ibkr:7002"].amount == -1000 and flows["ibkr:7002"].date == dt.date(2026, 2, 27)
    assert flows["ibkr:7002"].note == "IBKR 出金"  # 原始描述里可能有姓名，不保存
    assert flows["ibkr:7101"].amount == 2000  # 转入持仓按市值
    assert flows["ibkr:7102"].amount == -3000 and flows["ibkr:7102"].currency == "HKD"  # 转出为负


def test_archive_drops_personal_info(repo):
    src, accounts = source(repo, FlexMock())
    raw = src.fetch(AS_OF, accounts).raw
    assert "OpenPositions" in raw
    assert "AccountInformation" not in raw and "nobody@example.com" not in raw and "19000101" not in raw


# ---------- 取数 ----------

def test_waits_for_statement_and_sends_dates(repo):
    mock = FlexMock()
    src, accounts = source(repo, mock, flow_lookback_days=7)
    src.fetch(AS_OF, accounts)
    send = [r for r in mock.requests if r.url.path.endswith("/SendRequest")][0]
    assert send.url.params["fd"] == "20260223" and send.url.params["td"] == "20260302"
    assert send.url.params["v"] == "3" and send.url.params["q"] == "987654"
    polls = [r for r in mock.requests if r.url.path.endswith("/GetStatement")]
    assert len(polls) == 2  # 第一次 1019 生成中，第二次拿到
    assert mock.sleeps  # 轮询之间有等待


def test_steps_back_when_today_not_ready(repo):
    mock = FlexMock(available_until=dt.date(2026, 2, 27))  # 周五
    src, accounts = source(repo, mock)
    snap = src.fetch(AS_OF, accounts)
    assert mock.sent_to_dates() == ["20260302", "20260301", "20260228", "20260227"]
    assert snap.data_date == dt.date(2026, 2, 27)


def test_gives_up_after_max_lag(repo):
    src, accounts = source(repo, FlexMock(available_until=dt.date(2026, 2, 1)), max_lag_days=2)
    with pytest.raises(SourceError, match="都没有可用报表"):
        src.fetch(AS_OF, accounts)


def test_fatal_error_is_not_retried(repo):
    mock = FlexMock()
    src, accounts = source(repo, mock)
    src._client.query_id = "000"
    with pytest.raises(FlexError, match="1014"):
        src.fetch(AS_OF, accounts)
    assert len(mock.requests) == 1


def test_http_error_does_not_leak_token(repo):
    src, accounts = source(repo, FlexMock(http_status=500))
    with pytest.raises(SourceError) as e:
        src.fetch(AS_OF, accounts)
    assert "500" in str(e.value) and TOKEN not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_missing_credentials(repo):
    repo.sync_accounts([MAIN])
    src = IbkrSource(SourceContext(repo=repo, env={}))
    with pytest.raises(SourceError, match="IBKR_FLEX_TOKEN"):
        src.fetch(AS_OF, [MAIN])


def test_account_mapping(repo):
    other = Account(id="ib2", name="IBKR 2", kind="broker", source="ibkr", currency="USD")
    src, accounts = source(repo, FlexMock(), accts=(MAIN, other))
    with pytest.raises(SourceError, match="U0000001"):  # 两个账户时必须配置对应关系
        src.fetch(AS_OF, accounts)

    src, _ = source(repo, FlexMock(), accts=(MAIN, other), accounts={"U0000001": "ib"})
    with pytest.raises(SourceError, match="ib2"):  # 配置了但报表里没有
        src.fetch(AS_OF, accounts)
    snap = src.fetch(AS_OF, [MAIN])
    assert {p.account_id for p in snap.positions} == {"ib"}


# ---------- 同步与回填 ----------

CONFIG = {
    "accounts": [
        {"id": "ib", "name": "IBKR", "kind": "broker", "source": "ibkr", "currency": "USD"},
        {"id": "cn", "name": "A 股", "kind": "broker", "source": "manual", "currency": "CNY"},
    ],
}


def ibkr_app(make_test_app, mock: FlexMock):
    app = make_test_app(CONFIG)
    app.sync.sources["ibkr"] = IbkrSource(SourceContext(repo=app.repo), client=mock.client())
    return app


def test_lagging_rule():
    fri, sat, mon = dt.date(2026, 2, 27), dt.date(2026, 2, 28), dt.date(2026, 3, 2)
    assert not lagging(None, mon) and not lagging(mon, mon)
    assert not lagging(fri, sat)  # 周末拿到周五的数据是正常的
    assert lagging(fri, mon)


def test_sync_marks_lagging_data_as_carried_forward(make_test_app):
    app = ibkr_app(make_test_app, FlexMock(available_until=dt.date(2026, 2, 27)))
    report = app.sync.run(AS_OF)
    ib = next(s for s in report.sources if s.source == "ibkr")
    assert ib.status == "ok" and "2026-02-27" in ib.note
    assert report.stale
    assert all(r["carried_forward"] for r in app.repo.snapshot_rows(AS_OF) if r["account_id"] == "ib")
    archived = app.settings.raw_dir / AS_OF.isoformat() / "ibkr.xml"
    assert archived.exists() and "nobody@example.com" not in archived.read_text(encoding="utf-8")

    fresh = ibkr_app(make_test_app, FlexMock()).sync.run(AS_OF)
    assert next(s for s in fresh.sources if s.source == "ibkr").note is None


def test_backfill_writes_history_and_nav(make_test_app):
    app = ibkr_app(make_test_app, FlexMock())
    r = app.sync.backfill("ibkr", dt.date(2026, 2, 20), AS_OF)
    assert (r.start, r.end, r.values, r.nav_days) == (dt.date(2026, 2, 20), AS_OF, 6, 6)
    assert r.new_flows == 4 and not r.fx_problems
    assert [n["nav_usd"] for n in app.repo.nav_series("investable")] == [
        99000, 100000, 105500, 107500, 106000, 107000]

    rows = app.repo.snapshot_rows(dt.date(2026, 2, 24))
    assert [(x["row_type"], x["instrument_id"], x["market_value"]) for x in rows] == [
        ("summary", "ACCOUNT:ib", 100000)]
    # 有快照的日子以快照为准，不再用历史总值
    app.sync.run(AS_OF)
    assert all(x["row_type"] != "summary" for x in app.repo.snapshot_rows(AS_OF))

    again = app.sync.backfill("ibkr", dt.date(2026, 2, 20), AS_OF)  # 可重复运行
    assert again.new_flows == 0 and again.values == 6


def test_backfill_errors(make_test_app):
    app = ibkr_app(make_test_app, FlexMock())
    with pytest.raises(ValueError, match="不支持回填"):
        app.sync.backfill("manual", AS_OF, AS_OF)
    with pytest.raises(LookupError, match="没有使用数据源"):
        app.sync.backfill("okx", AS_OF, AS_OF)
    with pytest.raises(ValueError, match="最多回填 365 天"):
        app.sync.backfill("ibkr", AS_OF - dt.timedelta(days=400), AS_OF)


def test_cli_backfill(make_test_app, capsys):
    app = ibkr_app(make_test_app, FlexMock(available_until=dt.date(2026, 2, 27)))
    code = cmd_backfill(app, argparse.Namespace(source="ibkr", days=10, end=AS_OF))
    out = capsys.readouterr().out
    assert code == 0
    assert "2026-02-21 → 2026-02-27" in out and "每日总值 5 天" in out and "资金进出 3 条" in out
