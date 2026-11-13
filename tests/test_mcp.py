from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from portfolio_monitor.entry.mcp import build_mcp
from portfolio_monitor.entry.web import create_app

START = dt.date(2026, 1, 1)
CONFIG = {
    "accounts": [
        {"id": "us", "name": "美股", "kind": "broker", "source": "fake", "currency": "USD"},
        {"id": "cn", "name": "A 股", "kind": "broker", "source": "manual", "currency": "CNY"},
        {"id": "bank", "name": "银行", "kind": "bank", "source": "manual", "currency": "HKD",
         "include_in_returns": False},
    ],
    "sources": {"fake": {"start": START.isoformat(), "monthly_deposit": 1000}},
}
TOKEN = "test-token-" + "0" * 32
READ_TOOLS = {"get_overview", "get_accounts", "get_account", "get_holdings", "get_performance",
              "get_change_log", "get_risk_alerts"}
PREVIEW_TOOLS = {"set_holding", "remove_holding", "set_cash_balance", "record_cash_flow"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def app(make_test_app):
    app = make_test_app(CONFIG)
    app.repo.set_manual_cash("cn", "CNY", 10_000)
    app.repo.set_manual_cash("bank", "HKD", 50_000)
    d = START
    for _ in range(45):
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            assert app.sync.run(d).ok
    return app


@pytest.fixture
async def client(app):
    async with Client(build_mcp(app), raise_exceptions=True) as c:
        yield c


async def call(client: Client, tool: str, **args):
    r = await client.call_tool(tool, args)
    assert not r.is_error, r.content[0].text
    return r.structured_content


async def call_error(client: Client, tool: str, **args) -> str:
    r = await client.call_tool(tool, args)
    assert r.is_error
    return r.content[0].text


# ---------- 工具 ----------

@pytest.mark.anyio
async def test_tools_and_hints(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == READ_TOOLS | PREVIEW_TOOLS | {"confirm_change", "discard_change", "undo_change"}
    for name, t in tools.items():
        assert t.annotations.read_only_hint is (name in READ_TOOLS), name
        assert t.description and t.title
    assert tools["confirm_change"].annotations.destructive_hint
    assert tools["undo_change"].annotations.destructive_hint
    assert not tools["set_holding"].annotations.destructive_hint
    assert {"account_id", "symbol", "quantity", "price"} <= set(tools["set_holding"].input_schema["required"])


@pytest.mark.anyio
async def test_read_tools(client, app):
    o = await call(client, "get_overview")
    assert o["nav_total_usd"] > o["nav_investable_usd"] > 0 and o["allocation"]

    accounts = {a["id"]: a for a in (await call(client, "get_accounts"))["accounts"]}
    assert accounts["cn"]["editable"] and not accounts["us"]["editable"]

    detail = await call(client, "get_account", account_id="us", flows_limit=1)
    assert detail["account"]["name"] == "美股" and detail["rows"] and len(detail["flows"]) == 1
    assert "没有这个账户" in await call_error(client, "get_account", account_id="nope")

    by_ccy = await call(client, "get_holdings", group_by="currency")
    assert by_ccy["group_by"] == "currency"
    assert sum(r["weight"] for r in by_ccy["rows"]) == pytest.approx(1.0)
    only_us = await call(client, "get_holdings", account_id="us")
    assert {r["instrument_id"] for r in only_us["rows"]} >= {"US:NVDA"}


@pytest.mark.anyio
async def test_risk_alerts(client, app):
    r = await call(client, "get_risk_alerts")
    assert r["evaluated"] and r["as_of"] == app.query.latest_date().isoformat()
    assert r["critical"] + r["warn"] == len(r["alerts"])
    assert all({"rule_id", "rule_name", "severity", "message", "since", "new"} <= set(a) for a in r["alerts"])


@pytest.mark.anyio
async def test_performance_points_only_on_request(client, app):
    perf = await call(client, "get_performance", period="1m")
    assert perf["period_label"] == "近 1 月" and perf["points"] is None
    assert perf["total_return"] == pytest.approx(app.query.curve("1m").total_return)
    assert [m["month"] for m in perf["months"]] == ["2026-02", "2026-01"]

    full = await call(client, "get_performance", include_points=True)
    assert full["points"][0]["cum_return"] == 0 and len(full["points"]) == len(app.query.performance())
    assert "Input should be" in await call_error(client, "get_performance", period="5y")


@pytest.mark.anyio
async def test_preview_then_confirm_then_undo(client, app):
    pending = await call(client, "set_cash_balance", account_id="bank", currency="HKD", amount=60_000)
    assert pending["status"] == "pending" and pending["actor"] == "mcp"
    assert "50,000 → 60,000" in pending["summary"]
    assert app.repo.get_manual_cash("bank", "HKD") == 50_000  # 预览不改数据

    done = await call(client, "confirm_change", change_id=pending["id"])
    assert done["status"] == "applied" and done["can_undo"]
    assert app.repo.get_manual_cash("bank", "HKD") == 60_000
    assert "不能再确认" in await call_error(client, "confirm_change", change_id=pending["id"])

    log = (await call(client, "get_change_log", account_id="bank"))["changes"]
    assert [c["id"] for c in log] == [pending["id"]]

    assert (await call(client, "undo_change", change_id=pending["id"]))["status"] == "undone"
    assert app.repo.get_manual_cash("bank", "HKD") == 50_000


@pytest.mark.anyio
async def test_holding_lifecycle(client, app):
    p = await call(client, "set_holding", account_id="cn", symbol="600036", quantity=100, price=40,
                   name="招商银行", asset_class="equity")
    assert "新增持仓" in p["summary"]
    await call(client, "confirm_change", change_id=p["id"])
    rows = (await call(client, "get_account", account_id="cn"))["rows"]
    assert any(r["instrument_id"] == "CN:600036" and r["quantity"] == 100 for r in rows)

    p = await call(client, "remove_holding", account_id="cn", instrument_id="CN:600036")
    await call(client, "confirm_change", change_id=p["id"])
    assert app.repo.get_manual_holding("cn", "CN:600036") is None


@pytest.mark.anyio
async def test_flow_and_discard(client, app):
    p = await call(client, "record_cash_flow", account_id="cn", currency="CNY", amount=-1000, note="取现")
    assert (await call(client, "discard_change", change_id=p["id"]))["status"] == "discarded"
    assert app.repo.get_manual_cash("cn", "CNY") == 10_000 and app.repo.cash_flow_rows("cn") == []


@pytest.mark.anyio
async def test_business_errors_are_readable(client):
    assert "只能修改手动账户" in await call_error(
        client, "set_cash_balance", account_id="us", currency="USD", amount=1)
    msg = await call_error(client, "set_holding", account_id="cn", symbol="600036", quantity=-1, price=40)
    assert "数量：必须大于 0" in msg
    assert "没有修改 #999" in await call_error(client, "confirm_change", change_id=999)


@pytest.mark.anyio
async def test_empty_database(make_test_app):
    async with Client(build_mcp(make_test_app(CONFIG)), raise_exceptions=True) as c:
        assert "还没有任何净值数据" in await call_error(c, "get_overview")


# ---------- HTTP：鉴权与本机限制 ----------

MODERN = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list",
          "Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}}}


def http(app, base_url="http://127.0.0.1:8000", token=TOKEN) -> TestClient:
    return TestClient(create_app(app, mcp_token=token), base_url=base_url)


def list_tools(c: TestClient, auth: str | None):
    headers = {**MODERN, **({"Authorization": auth} if auth else {})}
    return c.post("/mcp", content=json.dumps(LIST_TOOLS), headers={**headers, "Content-Type": "application/json"})


def test_http_requires_token(app):
    with http(app) as c:
        assert list_tools(c, None).status_code == 401
        assert list_tools(c, "Bearer wrong").status_code == 401
        r = list_tools(c, f"Bearer {TOKEN}")
        assert r.status_code == 200, r.text
        assert {t["name"] for t in r.json()["result"]["tools"]} >= READ_TOOLS
        assert c.get("/").status_code == 200  # 网页不受影响


def test_http_rejects_non_local_host(app):
    with http(app, base_url="https://portfolio.example.com") as c:
        assert list_tools(c, f"Bearer {TOKEN}").status_code == 421


def test_mcp_disabled_without_token(app):
    with http(app, token=None) as c:
        assert list_tools(c, f"Bearer {TOKEN}").status_code == 404


def test_short_token_is_rejected(app):
    with pytest.raises(ValueError, match="MCP_TOKEN 太短"):
        create_app(app, mcp_token="short")
