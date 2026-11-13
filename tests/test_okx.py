from __future__ import annotations

import datetime as dt
import json

import pytest
from okx_mock import KEY, NOW, PASSPHRASE, SECRET, OkxMock

from portfolio_monitor.domain.models import Account, AssetClass
from portfolio_monitor.sources import okx as okx_module
from portfolio_monitor.sources.base import SourceContext, SourceError
from portfolio_monitor.sources.okx import OkxError, OkxSource

AS_OF = dt.date(2026, 3, 2)  # 样本的“现在”是纽约 3 月 2 日 16:00
MAIN = Account(id="okx", name="OKX", kind="crypto", source="okx", currency="USD")


def source(repo, mock: OkxMock, **client_kw) -> OkxSource:
    repo.sync_accounts([MAIN])
    return OkxSource(SourceContext(repo=repo), client=mock.client(**client_kw))


# ---------- 持仓 ----------

def test_holdings(repo):
    snap = source(repo, OkxMock()).fetch(AS_OF, [MAIN])
    pos = {p.instrument_id: p for p in snap.positions}
    assert set(pos) == {"CRYPTO:ABCXYZ", "CRYPTO:BETH", "CRYPTO:BTC", "CRYPTO:ETH", "CRYPTO:NOPRICE",
                        "CRYPTO:USDC", "CRYPTO:USDT", "UNKNOWN:okx:BTC-USDT-SWAP"}  # 余额为 0 的币、杠杆持仓不列
    assert pos["CRYPTO:ETH"].quantity == 3  # 交易账户 2 + 简单赚币 1
    assert pos["CRYPTO:BETH"].quantity == pytest.approx(0.6)  # 交易账户 0.1 + ETH 质押 0.5
    assert pos["CRYPTO:USDT"].quantity == 12000  # 10150 − 合约未实现盈亏 150 + 赚币 2000
    assert pos["CRYPTO:USDC"].market_value == pytest.approx(499.9)  # 按指数价，不按 1
    assert pos["CRYPTO:ABCXYZ"].price == 0.5  # 没有指数价，用交易账户自己的 eqUsd
    assert pos["CRYPTO:NOPRICE"].price is None and pos["CRYPTO:NOPRICE"].market_value == 0

    swap = pos["UNKNOWN:okx:BTC-USDT-SWAP"]
    assert swap.quantity == 2 and swap.market_value == 150  # 市值 = 未实现盈亏
    assert sum(p.market_value for p in snap.positions) == pytest.approx(57793.9)  # 总值不因合约重复
    assert not snap.cash and snap.data_date is None

    inst = {i.id: i for i in snap.instruments}
    assert inst["CRYPTO:USDT"].asset_class == AssetClass.CASH
    assert inst["CRYPTO:USDC"].asset_class == AssetClass.CASH
    assert inst["CRYPTO:BTC"].asset_class == AssetClass.CRYPTO and inst["CRYPTO:BTC"].region == "GLOBAL"
    assert [u.raw_symbol for u in snap.unresolved] == ["BTC-USDT-SWAP"]
    assert snap.warnings == ["NOPRICE 没有 USD 价格，市值按 0 计",
                             "2026-02-28 的提现 NOPRICE 没有 USD 价格，没有记入资金进出"]


def test_flows(repo):
    flows = source(repo, OkxMock()).fetch(AS_OF, [MAIN]).flows
    assert [(f.external_id, f.date, round(f.amount, 2)) for f in flows] == [
        ("okx:dep:D1", dt.date(2026, 2, 25), 4997.5),   # 当天 USDT 日线 0.9995
        ("okx:dep:D2", dt.date(2026, 2, 26), 6400.0),   # UTC 27 日凌晨 = 纽约 26 日晚；价格用 UTC 27 日日线
        ("okx:wd:W1", dt.date(2026, 2, 28), -1700.0),   # 手续费不算出金
        ("okx:dep:D4", AS_OF, 100.0),                   # 当天的用当前指数价
    ]  # 未完成的 D3、撤销的 W2、回看窗口外的 D0 不计
    assert {f.currency for f in flows} == {"USD"}
    assert flows[0].note == "OKX 充值 USDT" and flows[2].note == "OKX 提现 ETH"


def test_archive_drops_personal_fields(repo):
    raw = source(repo, OkxMock()).fetch(AS_OF, [MAIN]).raw
    text = json.dumps(raw, ensure_ascii=False)
    for secret in ("someone@example.com", "TEST-ADDR", "0xTESTADDRESS", "0xtest", "测试备注"):
        assert secret not in text
    assert raw["positions"][0]["notionalUsd"] == "13000"  # 名义价值留在存档里
    assert set(raw["prices"]) == {"BTC", "ETH", "USDT", "USDC", "BETH"}


def test_reconcile_warning(repo):
    mock = OkxMock(overrides={"/api/v5/asset/asset-valuation": [{"totalBal": "70000", "details": {}}]})
    warnings = source(repo, mock).fetch(AS_OF, [MAIN]).warnings
    assert any("相差 -17.4%" in w for w in warnings)


def test_optional_endpoint_failure_is_a_warning(repo):
    mock = OkxMock(errors={"/api/v5/finance/savings/balance": ("50030", "No permission to use this API")})
    snap = source(repo, mock).fetch(AS_OF, [MAIN])
    assert next(p for p in snap.positions if p.instrument_id == "CRYPTO:ETH").quantity == 2
    assert any(w.startswith("读取简单赚币失败") for w in snap.warnings)
    assert any("相差" in w for w in snap.warnings)  # 少了赚币，对账会发现


# ---------- 请求 ----------

def test_requests(repo):
    mock = OkxMock()
    source(repo, mock).fetch(AS_OF, [MAIN])
    public = [r for r in mock.requests if r.url.path.startswith("/api/v5/market/")]
    assert public and all("OK-ACCESS-KEY" not in r.headers for r in public)  # 公开接口不带凭据
    val = next(r for r in mock.requests if r.url.path.endswith("/asset-valuation"))
    assert val.url.params["ccy"] == "USD"
    candles = [r for r in mock.requests if r.url.path.endswith("/history-index-candles")]
    assert len(candles) == 4  # USDT、BTC、ETH 各一次，NOPRICE 一次；同一币同一天只取一次


def test_pagination(repo, monkeypatch):
    monkeypatch.setattr(okx_module, "PAGE", 2)
    mock = OkxMock()
    flows = source(repo, mock).fetch(AS_OF, [MAIN]).flows
    assert mock.paths().count("/api/v5/asset/deposit-history") == 3
    assert mock.paths().count("/api/v5/asset/withdrawal-history") == 2
    assert len(flows) == 4


def test_bad_signature_is_not_retried(repo):
    mock = OkxMock()
    with pytest.raises(OkxError, match="50113") as e:
        source(repo, mock, secret="F" * 32).fetch(AS_OF, [MAIN])
    assert len(mock.requests) == 1
    assert SECRET not in str(e.value) and KEY not in str(e.value) and PASSPHRASE not in str(e.value)


def test_clock_skew_is_corrected(repo):
    mock = OkxMock()
    snap = source(repo, mock, now=lambda: NOW - dt.timedelta(minutes=5)).fetch(AS_OF, [MAIN])
    assert snap.positions
    assert mock.paths()[:3] == ["/api/v5/account/balance", "/api/v5/public/time", "/api/v5/account/balance"]


def test_rate_limit_is_retried(repo):
    mock = OkxMock(rate_limit_once=True)
    snap = source(repo, mock).fetch(AS_OF, [MAIN])
    assert snap.positions and mock.sleeps == [1.0]


def test_http_error_hides_credentials(repo):
    with pytest.raises(SourceError) as e:
        source(repo, OkxMock(http_status=502)).fetch(AS_OF, [MAIN])
    assert "502" in str(e.value)
    assert KEY not in str(e.value) and PASSPHRASE not in str(e.value)
    assert e.value.__cause__ is None


def test_ip_is_masked_in_errors(repo):
    mock = OkxMock(errors={"/api/v5/account/balance": (
        "50110", "Your IP 203.0.113.7 is not included in your API key's IP whitelist.")})
    with pytest.raises(OkxError) as e:
        source(repo, mock).fetch(AS_OF, [MAIN])
    assert "203.0.113.7" not in str(e.value) and "<IP>" in str(e.value)


# ---------- 限制 ----------

def test_only_today(repo):
    with pytest.raises(SourceError, match="只能取当前余额"):
        source(repo, OkxMock()).fetch(AS_OF - dt.timedelta(days=1), [MAIN])


def test_one_account_only(repo):
    other = Account(id="okx2", name="OKX 2", kind="crypto", source="okx", currency="USD")
    repo.sync_accounts([MAIN, other])
    src = OkxSource(SourceContext(repo=repo), client=OkxMock().client())
    with pytest.raises(SourceError, match="一个账户"):
        src.fetch(AS_OF, [MAIN, other])


def test_missing_credentials(repo):
    repo.sync_accounts([MAIN])
    src = OkxSource(SourceContext(repo=repo, env={"OKX_API_KEY": KEY}))
    with pytest.raises(SourceError, match="OKX_API_PASSPHRASE"):
        src.fetch(AS_OF, [MAIN])


# ---------- 同步 ----------

def test_sync(make_test_app):
    app = make_test_app({"accounts": [
        {"id": "okx", "name": "OKX", "kind": "crypto", "source": "okx", "currency": "USD"}]})
    app.sync.sources["okx"] = OkxSource(SourceContext(repo=app.repo), client=OkxMock().client())
    report = app.sync.run(AS_OF)
    r = report.sources[0]
    assert (r.status, r.new_flows, r.unresolved) == ("ok", 4, 1)
    assert "NOPRICE 没有 USD 价格" in r.note  # 提示写进同步结果
    assert report.nav_total == pytest.approx(57793.9)
    archived = app.settings.raw_dir / AS_OF.isoformat() / "okx.json"
    assert archived.exists() and "someone@example.com" not in archived.read_text(encoding="utf-8")
    assert app.sync.run(AS_OF).sources[0].new_flows == 0  # 重跑不重复记资金进出
