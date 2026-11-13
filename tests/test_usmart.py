from __future__ import annotations

import datetime as dt
import json
import stat

import pytest
from fastapi.testclient import TestClient
from usmart_mock import CLIENT_KEY, NOW, PASSWORD, PHONE, PLATFORM_KEY, UsmartMock, env

from portfolio_monitor.domain.models import Account, AssetClass
from portfolio_monitor.entry.web import create_app
from portfolio_monitor.services.edit import EditError
from portfolio_monitor.sources.base import SourceContext, SourceError
from portfolio_monitor.sources.usmart import (
    UsmartError,
    UsmartSource,
    load_key,
    parse_holdings,
    parse_option,
    reconcile,
)

AS_OF = dt.date(2026, 3, 2)  # 样本的“现在”是纽约 3 月 2 日 16:00
MAIN = Account(id="usm", name="盈立", kind="broker", source="usmart", currency="USD")
OPTION = "UNKNOWN:usmart:TSLA270115P200000"


def source(repo, mock: UsmartMock, data_dir=None, now: dt.datetime = NOW, **env_over) -> UsmartSource:
    repo.sync_accounts([MAIN])
    return UsmartSource(SourceContext(repo=repo, env=env(**env_over), data_dir=data_dir),
                        client=mock.client(now=lambda: now), now=lambda: now)


# ---------- 映射 ----------

def test_holdings(repo):
    snap = source(repo, UsmartMock()).fetch(AS_OF, [MAIN])
    pos = {p.instrument_id: p for p in snap.positions}
    assert set(pos) == {"HK:09988", "US:AAPL", "US:QQQ", OPTION, "UNKNOWN:usmart:5-12AB"}  # 数量为 0 的 TQQQ 不列
    assert (pos["HK:09988"].market_value, pos["HK:09988"].cost_basis, pos["HK:09988"].currency) == (20000, 18000, "HKD")
    assert pos["US:AAPL"].market_value == 2000 and pos["US:AAPL"].cost_basis == 1500
    opt = pos[OPTION]  # 卖出 1 张看跌期权：接口给每股价格，市值和成本都要乘 100
    assert (opt.quantity, opt.price, opt.market_value, opt.cost_basis) == (-1, 12.5, -1250, -1500)
    assert opt.notional == 20000  # |数量| × 行权价 200 × 100

    inst = {i.id: i for i in snap.instruments}
    assert inst["US:QQQ"].asset_class == AssetClass.ETF and inst["US:AAPL"].asset_class == AssetClass.EQUITY
    assert inst["HK:09988"].region == "HK" and inst[OPTION].asset_class == AssetClass.OPTION
    assert [u.raw_symbol for u in snap.unresolved] == ["TSLA270115P200000", "12AB"]
    assert snap.data_date is None


def test_cash_is_total_minus_holdings_and_reconciles(repo):
    snap = source(repo, UsmartMock()).fetch(AS_OF, [MAIN])
    assert [(c.account_id, c.currency) for c in snap.cash] == [("usm", "USD")]
    assert snap.cash[0].amount == pytest.approx(16135.90)
    assert snap.warnings == []  # 自己算的持仓市值（港股按盈立的 7.8 换算）与盈立的一致
    assert set(snap.raw) == {"holdings", "asset_usd", "asset_hkd"}


def test_reconcile_warns_when_holdings_disagree(repo):
    # 如果期权没乘 100，盈立的持仓市值会和我们的差一大截
    mock = UsmartMock(overrides={"asset_usd": {**UsmartMock().sample["asset_usd"], "marketValue": "2614.10"}})
    warnings = source(repo, mock).fetch(AS_OF, [MAIN]).warnings
    assert any("相差 +32.3%" in w for w in warnings)


def test_parse_option_and_other_markets():
    assert parse_option("MSFT270618P350000") == ("MSFT", dt.date(2027, 6, 18), "P", 350.0)
    assert parse_option("BRK.B270115C00400000")[0] == "BRK.B"
    assert parse_option("AAPL") is None and parse_option("12AB") is None
    rows = [{"exchangeType": "8", "stockCode": "D05", "stockName": "星展银行", "currentAmount": "100",
             "lastPrice": "35", "costPriceAccurate": "30"},
            {"exchangeType": "99", "stockCode": "ZZZ", "stockName": "未知", "currentAmount": "1", "lastPrice": "1"}]
    positions, instruments, unresolved, warnings = parse_holdings("usm", rows)
    sg = next(i for i in instruments if i.id == "SG:D05")
    assert (sg.currency, sg.region) == ("SGD", "SG")
    assert [u.raw_symbol for u in unresolved] == ["ZZZ"] and "市场代码 99 不认识" in warnings[0]
    assert "SGD" in reconcile(positions, {"asset": "1", "marketValue": "1"}, {"asset": "7.8"}, 0.01)


# ---------- 请求与登录 ----------

def test_requests_are_signed_and_credentials_encrypted(repo):
    mock = UsmartMock()  # 模拟服务器会验签、解密，不对就拒绝
    source(repo, mock).fetch(AS_OF, [MAIN])
    assert mock.paths() == ["/user-server-sg/open-api/login", "/asset-center-sg/open-api/stock-holding",
                            "/asset-center-sg/open-api/stock-asset", "/asset-center-sg/open-api/stock-asset"]
    login, *queries = mock.requests
    assert PHONE not in login.content.decode() and PASSWORD not in login.content.decode()
    assert all(r.headers["X-Type"] == "12" and r.headers["Authorization"] for r in queries)
    assert json.loads(queries[0].content) == {"exchangeType": "100"}


def test_token_is_cached_privately(repo, tmp_path):
    mock = UsmartMock()
    source(repo, mock, tmp_path).fetch(AS_OF, [MAIN])
    source(repo, mock, tmp_path).fetch(AS_OF, [MAIN])  # 新实例也读缓存
    assert mock.logins == 1
    path = tmp_path / "secrets" / "usmart_token.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    assert PASSWORD not in text and PHONE not in text


def test_relogin_when_token_rejected_or_near_expiry(repo, tmp_path):
    mock = UsmartMock()
    source(repo, mock, tmp_path).fetch(AS_OF, [MAIN])
    mock.expire_tokens()  # 服务器那边 token 失效
    assert source(repo, mock, tmp_path).fetch(AS_OF, [MAIN]).positions
    assert mock.logins == 2
    later = NOW + dt.timedelta(days=30) - dt.timedelta(hours=6)  # 缓存的 token 快到期
    source(repo, mock, tmp_path, now=later).fetch(later.astimezone(dt.timezone(dt.timedelta(hours=-5))).date(),
                                                  [MAIN])
    assert mock.logins == 3


def test_wrong_password_is_not_retried(repo, tmp_path):
    mock = UsmartMock(password="Right$pw9")
    with pytest.raises(UsmartError, match="300709"):
        source(repo, mock, tmp_path).fetch(AS_OF, [MAIN])
    with pytest.raises(SourceError, match="不再自动重试") as e:  # 同一组凭据不再去登录，免得锁号
        source(repo, mock, tmp_path).fetch(AS_OF, [MAIN])
    assert mock.logins == 1 and PASSWORD not in str(e.value)
    # 改了 .env 里的密码才会再试
    assert source(repo, mock, tmp_path, USMART_LOGIN_PASSWORD="Right$pw9").fetch(AS_OF, [MAIN]).positions
    assert mock.logins == 2


def test_http_error_hides_credentials(repo):
    with pytest.raises(SourceError) as e:
        source(repo, UsmartMock(http_status=502)).fetch(AS_OF, [MAIN])
    assert "502" in str(e.value) and PASSWORD not in str(e.value) and PHONE not in str(e.value)
    assert e.value.__cause__ is None


# ---------- 凭据与限制 ----------

def test_missing_and_bad_credentials(repo):
    repo.sync_accounts([MAIN])
    src = UsmartSource(SourceContext(repo=repo, env={}))  # 构造不报错：不影响程序启动
    with pytest.raises(SourceError, match="USMART_CHANNEL / USMART_AREA_CODE / USMART_PHONE"):
        src.fetch(AS_OF, [MAIN])
    bad = UsmartSource(SourceContext(repo=repo, env=env(USMART_PRIVATE_KEY="not-a-key")))
    with pytest.raises(SourceError, match="USMART_PRIVATE_KEY 不是有效的 RSA 私钥"):
        bad.fetch(AS_OF, [MAIN])


def test_keys_accept_base64_and_pem():
    from cryptography.hazmat.primitives import serialization
    pem = CLIENT_KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()).decode()
    assert load_key(pem, private=True, name="K").key_size == 1024
    pub_pem = PLATFORM_KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                                     serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    assert load_key(pub_pem, private=False, name="K").key_size == 1024
    assert load_key(env()["USMART_PRIVATE_KEY"], private=True, name="K").key_size == 1024


def test_only_today_and_one_account(repo):
    with pytest.raises(SourceError, match="只能取当前持仓"):
        source(repo, UsmartMock()).fetch(AS_OF - dt.timedelta(days=1), [MAIN])
    other = MAIN.model_copy(update={"id": "usm2"})
    repo.sync_accounts([MAIN, other])
    with pytest.raises(SourceError, match="一个账户"):
        source(repo, UsmartMock()).fetch(AS_OF, [MAIN, other])


# ---------- 同步与手动记资金进出 ----------

CONFIG = {"accounts": [
    {"id": "usm", "name": "盈立", "kind": "broker", "source": "usmart", "currency": "USD"},
    {"id": "ib", "name": "IBKR", "kind": "broker", "source": "ibkr", "currency": "USD"},
]}


def usmart_app(make_test_app):
    app = make_test_app(CONFIG)
    app.sync.sources["usmart"] = UsmartSource(
        SourceContext(repo=app.repo, env=env(), data_dir=app.settings.data_dir),
        client=UsmartMock().client(), now=lambda: NOW)
    return app


def test_sync(make_test_app):
    app = usmart_app(make_test_app)
    r = next(s for s in app.sync.run(AS_OF, only="usmart").sources if s.source == "usmart")
    assert (r.status, r.positions, r.cash, r.unresolved, r.note) == ("ok", 5, 1, 2, None)
    rows = {x["instrument_id"]: x for x in app.repo.snapshot_rows(AS_OF)}
    assert rows[OPTION]["notional"] == 20000  # 杠杆规则会用到
    archived = app.settings.raw_dir / AS_OF.isoformat() / "usmart.json"
    assert archived.exists() and PASSWORD not in archived.read_text(encoding="utf-8")


def test_flows_can_be_recorded_by_hand(make_test_app):
    app = usmart_app(make_test_app)
    app.sync.run(AS_OF, only="usmart")
    view = app.edit.apply({"kind": "record_flow", "account_id": "usm", "currency": "USD", "amount": 1000,
                           "adjust_cash": True, "note": "入金"}, actor="mcp")
    assert "只记流水" in view.summary  # 现金来自盈立，不改现金
    assert [(f.amount, f.currency) for f in app.repo.cash_flows(account_ids=["usm"])] == [(1000, "USD")]
    with pytest.raises(EditError, match="只能修改手动账户"):
        app.edit.apply({"kind": "set_cash", "account_id": "usm", "currency": "USD", "amount": 5}, actor="mcp")
    with pytest.raises(EditError, match="资金进出由 ibkr 同步，不能手动记"):
        app.edit.apply({"kind": "record_flow", "account_id": "ib", "currency": "USD", "amount": 5}, actor="mcp")

    accounts = {a.id: a for a in app.query.accounts()}
    assert accounts["usm"].flows_editable and not accounts["usm"].editable
    assert not accounts["ib"].flows_editable
    page = TestClient(create_app(app)).get("/accounts/usm").text
    assert "记入金 / 出金" in page and "新增持仓" not in page and 'name="adjust_cash"' not in page
