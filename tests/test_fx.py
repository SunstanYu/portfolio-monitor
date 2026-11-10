import datetime as dt

import httpx
import pytest

from portfolio_monitor.sources.fx import FrankfurterFx, FxError, StaticFx


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_frankfurter_parses_and_inverts_rates():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        # 周末请求，接口返回上一个工作日
        return httpx.Response(200, json={"amount": 1.0, "base": "USD", "date": "2026-11-06",
                                         "rates": {"CNY": 6.6713, "HKD": 7.8374}})

    q = FrankfurterFx(client(handler)).fetch(dt.date(2026, 11, 8), ["HKD", "USD", "CNY"])
    assert "/v1/2026-11-08" in seen["url"] and "symbols=CNY%2CHKD" in seen["url"]
    assert q.effective_date == dt.date(2026, 11, 6)
    assert q.usd_per_unit["HKD"] == pytest.approx(1 / 7.8374)
    assert set(q.usd_per_unit) == {"CNY", "HKD"}


def test_frankfurter_only_usd_needs_no_request():
    def handler(req):
        raise AssertionError("不应发请求")

    assert FrankfurterFx(client(handler)).fetch(dt.date(2026, 1, 2), ["USD"]).usd_per_unit == {}


def test_frankfurter_retries_server_errors_then_fails():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503)

    with pytest.raises(FxError):
        FrankfurterFx(client(handler), retries=2, backoff=0).fetch(dt.date(2026, 1, 2), ["HKD"])
    assert len(calls) == 3


def test_frankfurter_missing_currency():
    def handler(req):
        return httpx.Response(200, json={"date": "2026-01-02", "rates": {"HKD": 7.8}})

    with pytest.raises(FxError, match="不支持"):
        FrankfurterFx(client(handler), retries=0).fetch(dt.date(2026, 1, 2), ["HKD", "XYZ"])


def test_static_fx():
    assert StaticFx().fetch(dt.date(2026, 1, 1), ["HKD"]).usd_per_unit["HKD"] == pytest.approx(1 / 7.8)
    with pytest.raises(FxError):
        StaticFx().fetch(dt.date(2026, 1, 1), ["EUR"])
