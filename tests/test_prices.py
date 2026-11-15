from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import httpx
import pytest

from portfolio_monitor.domain.models import Account, AssetClass, Instrument
from portfolio_monitor.sources.base import SourceContext
from portfolio_monitor.sources.manual import ManualSource
from portfolio_monitor.sources.prices import PriceError, PriceQuote, YahooPrices

AS_OF = dt.date(2026, 3, 2)  # 周一


def chart(bars, *, tz="Asia/Hong_Kong", currency="HKD", hour=9) -> dict:
    """虚构的 Yahoo 日线：bars = [(交易日, 收盘价或 None)]，时间戳为交易所当地 hour 点。"""
    zone = ZoneInfo(tz)
    stamps = [int(dt.datetime.combine(d, dt.time(hour), zone).timestamp()) for d, _ in bars]
    return {"chart": {"error": None, "result": [{
        "meta": {"currency": currency, "symbol": "X", "exchangeTimezoneName": tz, "gmtoffset": 28800},
        "timestamp": stamps, "indicators": {"quote": [{"close": [c for _, c in bars]}]}}]}}


def yahoo(*responses) -> tuple[YahooPrices, list[httpx.Request], list[float]]:
    requests, sleeps, queue = [], [], list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)

    return YahooPrices(httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append), requests, sleeps


# ---------- Yahoo ----------

def test_close_on_or_before_as_of():
    feb26, feb27, mar3 = dt.date(2026, 2, 26), dt.date(2026, 2, 27), dt.date(2026, 3, 3)
    p, requests, _ = yahoo(chart([(feb26, 100.0), (feb27, 101.0), (AS_OF, None), (mar3, 103.0)]))
    q = p.close("0700.HK", AS_OF)
    assert q == PriceQuote(symbol="0700.HK", date=feb27, price=101.0, currency="HKD")  # 停牌日跳过，晚于估值日的不要
    params = requests[0].url.params
    assert requests[0].url.path.endswith("/0700.HK") and params["interval"] == "1d"
    assert int(params["period1"]) < int(dt.datetime(2026, 3, 2, tzinfo=dt.timezone.utc).timestamp()) < int(params["period2"])


def test_trading_day_uses_exchange_timezone():
    # 香港 3 月 3 日 0 点 = UTC 3 月 2 日 16 点：按 UTC 算会误当成 3 月 2 日的价格
    p, _, _ = yahoo(chart([(dt.date(2026, 2, 27), 50.0), (dt.date(2026, 3, 3), 60.0)], hour=0))
    assert p.close("0700.HK", AS_OF).price == 50.0


def test_errors():
    p, _, _ = yahoo(httpx.Response(404, json={"chart": {"result": None, "error": {"code": "Not Found"}}}))
    with pytest.raises(PriceError, match="没有这个代码"):
        p.close("NOPE", AS_OF)
    p, _, _ = yahoo({"chart": {"result": None, "error": {"code": "x", "description": "No data found"}}})
    with pytest.raises(PriceError, match="No data found"):
        p.close("NOPE", AS_OF)
    p, _, _ = yahoo(chart([(dt.date(2026, 3, 3), 1.0)]))
    with pytest.raises(PriceError, match="没有收盘价"):
        p.close("X", AS_OF)


def test_retries_rate_limit_and_network_errors():
    p, requests, sleeps = yahoo(httpx.Response(429), chart([(AS_OF, 10.0)]))
    assert p.close("X", AS_OF).price == 10.0 and len(requests) == 2 and sleeps == [2.0]
    p, _, _ = yahoo(httpx.ConnectError("down"), httpx.ConnectError("down"))
    with pytest.raises(PriceError, match="请求失败：ConnectError") as e:
        p.close("X", AS_OF)
    assert e.value.__cause__ is None


# ---------- 手动账户自动更新价格 ----------

class FakePrices:
    name = "fake"

    def __init__(self, quotes):
        self.quotes = quotes
        self.calls: list[str] = []

    def close(self, symbol: str, as_of: dt.date) -> PriceQuote:
        self.calls.append(symbol)
        q = self.quotes[symbol]
        if isinstance(q, Exception):
            raise q
        return q


INSTRUMENTS = [
    Instrument(id="US:NVDA", name="英伟达", asset_class=AssetClass.EQUITY, currency="USD"),
    Instrument(id="HK:00700", name="腾讯", asset_class=AssetClass.EQUITY, currency="HKD"),
    Instrument(id="HK:09988", name="阿里", asset_class=AssetClass.EQUITY, currency="HKD"),
    Instrument(id="US:OLD", name="停牌股", asset_class=AssetClass.EQUITY, currency="USD"),
    Instrument(id="US:BAD", name="取不到", asset_class=AssetClass.EQUITY, currency="USD"),
    Instrument(id="UNKNOWN:manual:fund", name="私募基金", asset_class=AssetClass.FUND, currency="USD"),
]
QUOTES = {
    "NVDA": PriceQuote("NVDA", AS_OF, 130.0, "USD"),
    "0700.HK": PriceQuote("0700.HK", dt.date(2026, 2, 27), 420.0, "HKD"),
    "9988.HK": PriceQuote("9988.HK", AS_OF, 15.0, "USD"),  # 币种对不上
    "OLD": PriceQuote("OLD", dt.date(2026, 2, 1), 5.0, "USD"),
    "BAD": PriceError("Yahoo 没有这个代码"),
}


@pytest.fixture
def manual(repo):
    accounts = [Account(id="m1", name="M1", kind="broker", source="manual", currency="USD"),
                Account(id="m2", name="M2", kind="broker", source="manual", currency="USD")]
    repo.sync_accounts(accounts)
    repo.upsert_instruments(INSTRUMENTS)
    for inst in INSTRUMENTS:
        repo.set_manual_holding("m1", inst.id, 10, 100.0, inst.currency, cost_basis=800.0)
    repo.set_manual_holding("m2", "US:BAD", 1, 100.0, "USD")  # 同一标的在两个账户：只取一次、只提示一次
    return repo, accounts


def make(repo, prices, clock=lambda: 0.0, **settings):
    return ManualSource(SourceContext(repo=repo, settings={"auto_price": True, **settings}), prices=prices,
                        clock=clock)


def test_auto_price_updates_snapshot_only(manual):
    repo, accounts = manual
    prices = FakePrices(QUOTES)
    snap = make(repo, prices).fetch(AS_OF, accounts)
    pos = {(p.account_id, p.instrument_id): p for p in snap.positions}
    assert (pos["m1", "US:NVDA"].price, pos["m1", "US:NVDA"].market_value) == (130.0, 1300.0)
    assert pos["m1", "US:NVDA"].cost_basis == 800.0  # 成本不变
    assert pos["m1", "HK:00700"].price == 420.0  # 周一取到上周五的收盘价
    assert pos["m1", "US:OLD"].price == 5.0
    for iid in ("HK:09988", "US:BAD", "UNKNOWN:manual:fund"):
        assert pos["m1", iid].price == 100.0  # 沿用手填价格
    assert repo.get_manual_holding("m1", "US:NVDA")["price"] == 100.0  # 手填价格不改
    assert snap.raw["prices"]["US:NVDA"] == {"symbol": "NVDA", "date": "2026-03-02", "price": 130.0,
                                             "currency": "USD", "manual_price": 100.0}
    assert sorted(prices.calls) == ["0700.HK", "9988.HK", "BAD", "NVDA", "OLD"]  # 识别不了的标的不去取
    assert sorted(snap.warnings) == sorted([
        "阿里 的 Yahoo 价格是 USD，持仓币种是 HKD，沿用手填价格",
        "停牌股 的最新价格停在 2026-02-01，可能停牌",
        "取不到 取不到 Yahoo 价格（Yahoo 没有这个代码），沿用手填价格",  # 两个账户都有，只提示一次
    ])


def test_quotes_are_cached_for_a_while(manual):
    repo, accounts = manual
    prices, now = FakePrices(QUOTES), [0.0]
    src = make(repo, prices, clock=lambda: now[0])
    src.fetch(AS_OF, accounts)
    src.fetch(AS_OF, accounts)  # 网页上改一次就刷新一次快照，不必每次都取价
    assert len(prices.calls) == 5
    now[0] = 1801.0
    src.fetch(AS_OF, accounts)
    assert len(prices.calls) == 10


def test_auto_price_can_be_turned_off(manual):
    repo, accounts = manual
    prices = FakePrices(QUOTES)
    snap = make(repo, prices, auto_price=False).fetch(AS_OF, accounts)
    assert prices.calls == [] and snap.warnings == [] and {p.price for p in snap.positions} == {100.0}


def test_sync_uses_auto_price(make_test_app):
    app = make_test_app({"accounts": [{"id": "m1", "name": "M1", "kind": "broker", "source": "manual",
                                       "currency": "USD"}]})
    app.repo.upsert_instruments(INSTRUMENTS[:1])
    app.repo.set_manual_holding("m1", "US:NVDA", 10, 100.0, "USD")
    app.sync.sources["manual"] = make(app.repo, FakePrices(QUOTES))
    report = app.sync.run(AS_OF)
    assert report.ok and report.nav_total == pytest.approx(1300.0)
    edited = app.edit.apply({"kind": "set_cash", "account_id": "m1", "currency": "USD", "amount": 50},
                            actor="web")
    assert edited.refresh_error is None  # 修改后刷新快照：价格仍是自动取到的
    assert sum(r["market_value"] for r in app.repo.snapshot_rows(app.edit.effective_date())) == pytest.approx(1350.0)
