from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from portfolio_monitor.entry.web import create_app
from portfolio_monitor.entry.web.tables import TABLES

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
TABLE_PARAMS = {"positions": "?account_id=cn", "flows": "?account_id=us"}


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
    app.repo.set_manual_cash("bank", "HKD", 50_000)
    for d in weekdays(30):
        assert app.sync.run(d).ok
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(create_app(app))


def edit(client, account="cn", **form):
    return client.post(f"/accounts/{account}/edit", data=form)


def test_pages_render(client):
    home = client.get("/")
    assert home.status_code == 200 and "总资产" in home.text and "收益走势" in home.text
    manual = client.get("/accounts/cn")
    assert manual.status_code == 200 and "新增持仓" in manual.text
    synced = client.get("/accounts/us")
    assert synced.status_code == 200 and "新增持仓" not in synced.text and "数据来自 fake" in synced.text
    assert client.get("/changes").status_code == 200
    assert client.get("/sync").status_code == 200
    missing = client.get("/accounts/nope")
    assert missing.status_code == 404 and "没有这个账户" in missing.text


def test_empty_database_shows_hint(make_test_app):
    client = TestClient(create_app(make_test_app(CONFIG)))
    r = client.get("/")
    assert r.status_code == 200 and "还没有数据" in r.text


def test_static_assets_are_versioned_and_served(client):
    html = client.get("/").text
    urls = re.findall(r'(?:src|href)="(/static/[^"]+\?v=[0-9a-f]{10})"', html)
    assert any("app.js" in u for u in urls) and any("echarts" in u for u in urls)
    for u in urls:
        assert client.get(u).status_code == 200


@pytest.mark.parametrize("table_id", sorted(TABLES))
def test_every_table_payload(client, table_id):
    r = client.get(f"/api/v1/tables/{table_id}{TABLE_PARAMS.get(table_id, '')}")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["id"] == table_id and payload["columns"]
    fields = {c["field"] for c in payload["columns"]}
    optional = {"actions", "note", "error", "cost_price"}
    for row in payload["rows"]:
        assert fields - optional <= set(row), f"{table_id} 缺少字段：{fields - optional - set(row)}"


@pytest.mark.parametrize("group_by", TABLES["holdings"].group_by_options)
def test_holdings_group_by_options(client, group_by):
    payload = client.get(f"/api/v1/tables/holdings?group_by={group_by}").json()
    assert payload["group_by"] == group_by
    assert sum(r["weight"] for r in payload["rows"]) == pytest.approx(1.0)
    titles = [c["title"] for c in payload["columns"]]
    assert "{group}" not in "".join(titles)


def test_json_api_reads(client):
    o = client.get("/api/v1/overview").json()
    assert o["nav_total_usd"] > o["nav_investable_usd"] > 0
    curve = client.get("/api/v1/curve?range=1m").json()
    assert curve["points"][0]["cum_return"] == 0
    assert client.get("/api/v1/curve?range=nope").status_code == 400
    assert {a["id"] for a in client.get("/api/v1/accounts").json()} == {"us", "cn", "bank"}
    assert client.get("/api/v1/accounts/nope").json() == {"error": "没有这个账户：nope"}


def test_form_edit_then_undo(client, app):
    r = edit(client, kind="set_holding", symbol="600036", quantity="100", price="40", cost_price="")
    assert r.status_code == 200
    target = r.headers["HX-Redirect"]
    change_id = int(target.rsplit("=", 1)[1])
    assert target == f"/accounts/cn?change={change_id}"
    page = client.get(target).text
    assert f"已保存 #{change_id}" in page and "撤销" in page

    r = client.post(f"/changes/{change_id}/undo", data={"next": "/accounts/cn"})
    assert r.headers["HX-Redirect"] == f"/accounts/cn?undone={change_id}"
    assert app.repo.get_manual_holding("cn", "CN:600036") is None


def test_form_errors_are_shown_in_the_form(client):
    r = edit(client, kind="set_holding", symbol="600036", quantity="-1", price="x")
    assert r.status_code == 422
    assert "数量：必须大于 0" in r.text and "价格：必须是数字" in r.text
    r = edit(client, account="us", kind="set_cash", currency="USD", amount="1")
    assert r.status_code == 422 and "只能修改手动账户" in r.text


def test_withdrawal_form(client, app):
    r = edit(client, kind="record_flow", direction="out", currency="CNY", amount="1000",
             adjust_cash="on", note="")
    assert r.status_code == 200, r.text
    assert app.repo.get_manual_cash("cn", "CNY") == 9_000
    (flow,) = app.repo.cash_flow_rows("cn")
    assert flow["amount"] == -1000 and flow["origin"] == "manual"


def test_undo_redirect_stays_on_site(client):
    r = edit(client, kind="set_cash", currency="CNY", amount="12000")
    change_id = int(r.headers["HX-Redirect"].rsplit("=", 1)[1])
    r = client.post(f"/changes/{change_id}/undo", data={"next": "//evil.example/x"})
    assert r.headers["HX-Redirect"] == f"/changes?undone={change_id}"


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Origin": "https://evil.example"},
])
def test_cross_site_writes_are_rejected(client, app, headers):
    r = client.post("/accounts/cn/edit", data={"kind": "set_cash", "currency": "CNY", "amount": "1"},
                    headers=headers)
    assert r.status_code == 403
    assert app.repo.get_manual_cash("cn", "CNY") == 10_000


def test_same_origin_writes_are_allowed(client, app):
    r = client.post("/accounts/cn/edit", data={"kind": "set_cash", "currency": "CNY", "amount": "1"},
                    headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"})
    assert r.status_code == 200
    assert app.repo.get_manual_cash("cn", "CNY") == 1


def test_json_api_two_step_write(client, app):
    body = {"kind": "set_cash", "account_id": "bank", "currency": "HKD", "amount": 60_000}
    pending = client.post("/api/v1/changes/preview", json=body).json()
    assert pending["status"] == "pending" and "50,000 → 60,000" in pending["summary"]
    assert app.repo.get_manual_cash("bank", "HKD") == 50_000

    done = client.post(f"/api/v1/changes/{pending['id']}/confirm").json()
    assert done["status"] == "applied" and app.repo.get_manual_cash("bank", "HKD") == 60_000
    again = client.post(f"/api/v1/changes/{pending['id']}/confirm")
    assert again.status_code == 400 and "不能再确认" in again.json()["error"]

    assert client.post(f"/api/v1/changes/{pending['id']}/undo").json()["status"] == "undone"
    assert app.repo.get_manual_cash("bank", "HKD") == 50_000
