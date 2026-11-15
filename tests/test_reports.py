from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from portfolio_monitor.entry.mcp import build_mcp
from portfolio_monitor.entry.web import create_app

START = dt.date(2026, 1, 26)
FEB2, FEB3 = dt.date(2026, 2, 2), dt.date(2026, 2, 3)  # 2 月 2 日是 2 月第一个工作日：假数据源入金
CONFIG = {
    "accounts": [
        {"id": "us", "name": "美股", "kind": "broker", "source": "fake", "currency": "USD"},
        {"id": "cn", "name": "A 股", "kind": "broker", "source": "manual", "currency": "CNY"},
        {"id": "bank", "name": "银行", "kind": "bank", "source": "manual", "currency": "HKD",
         "include_in_returns": False},
    ],
    "sources": {"fake": {"start": START.isoformat(), "monthly_deposit": 1000}},
}


@pytest.fixture
def app(make_test_app):
    app = make_test_app(CONFIG)
    app.repo.set_manual_cash("cn", "CNY", 10_000)
    app.repo.set_manual_cash("bank", "HKD", 50_000)
    d = START
    while d <= FEB3:
        if d.weekday() < 5:
            assert app.sync.run(d).ok
        d += dt.timedelta(days=1)
    return app


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------- 摘要 ----------

def test_summary(app):
    s = app.reports.summary(FEB2)
    assert (s.as_of, s.previous_date) == (FEB2, dt.date(2026, 1, 30))
    o = app.query.overview(FEB2)
    assert (s.nav_total_usd, s.nav_investable_usd, s.day_return) == (o.nav_total_usd, o.nav_investable_usd,
                                                                     o.day_return)
    series = {p.date: p for p in app.query.performance(end=FEB2)}
    assert s.day_change_usd == pytest.approx(series[FEB2].nav - series[s.previous_date].nav - series[FEB2].net_flow)
    assert [(f.account_id, f.amount) for f in s.flows] == [("us", 1000)]  # 当日入金单列，盈亏已剔除它
    assert {a.id for a in s.accounts} == {"us", "cn", "bank"}
    us = next(a for a in s.accounts if a.id == "us")
    assert us.change_usd is not None and not us.stale
    assert 0 < len(s.movers) <= 5
    assert [abs(m.change_usd) for m in s.movers] == sorted((abs(m.change_usd) for m in s.movers), reverse=True)
    assert all(m.accounts == ["美股"] for m in s.movers)
    assert sum(a.weight for a in s.allocation) == pytest.approx(1.0)
    assert {x.source for x in s.sources} == {"fake", "manual"} and not s.report_saved


def test_summary_defaults_to_latest_and_handles_first_day(app):
    assert app.reports.summary().as_of == FEB3
    first = app.reports.summary(START)
    assert first.previous_date is None and first.movers == [] and first.day_change_usd is None
    assert all(a.change_usd is None for a in first.accounts)


# ---------- 保存与读取 ----------

def test_save_and_read_reports(app):
    saved = app.reports.save(FEB2, "  第一版  \n")
    assert (saved.as_of, saved.chars, saved.replaced) == (FEB2, 3, False)
    assert app.reports.save(FEB2, "第二版").replaced  # 同一天覆盖
    app.reports.save(FEB3, "二月三日")
    assert app.reports.get(FEB2).content == "第二版"
    assert app.reports.get().as_of == FEB3  # 默认最新一篇
    assert [r.as_of for r in app.reports.recent()] == [FEB3, FEB2]
    assert app.reports.summary(FEB2).report_saved
    with pytest.raises(ValueError, match="内容为空"):
        app.reports.save(FEB2, "   ")
    with pytest.raises(ValueError, match="太长"):
        app.reports.save(FEB2, "字" * 20_001)
    with pytest.raises(LookupError, match="没有净值数据"):
        app.reports.save(dt.date(2026, 2, 1), "周日")  # 不是估值日


# ---------- MCP ----------

@pytest.mark.anyio
async def test_mcp_daily_report_flow(app):
    async with Client(build_mcp(app), raise_exceptions=True) as c:
        summary = (await c.call_tool("get_daily_summary", {})).structured_content
        assert summary["as_of"] == FEB3.isoformat() and "movers" in summary and "alerts" in summary
        r = await c.call_tool("save_daily_report", {"date": summary["as_of"], "content": "今日概况：一切正常"})
        assert not r.is_error and r.structured_content["replaced"] is False
        got = (await c.call_tool("get_daily_report", {})).structured_content
        assert got["content"] == "今日概况：一切正常" and got["as_of"] == FEB3.isoformat()
        bad = await c.call_tool("save_daily_report", {"date": "2026-02-01", "content": "x"})
        assert bad.is_error and "没有净值数据" in bad.content[0].text
        missing = await c.call_tool("get_daily_report", {"date": "2026-01-27"})
        assert missing.is_error and "没有日报" in missing.content[0].text


# ---------- 网页与 JSON ----------

def test_web_shows_report_escaped(app):
    client = TestClient(create_app(app))
    assert 'class="report"' not in client.get("/").text  # 还没有日报时不显示这一块
    app.reports.save(FEB3, "<script>alert(1)</script>\n第二行")
    page = client.get("/").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert(1)" not in page  # AI 写的内容一律转义
    assert 'class="report"' in page and "历史日报" in page
    history = client.get("/reports").text
    assert FEB3.isoformat() in history and "&lt;script&gt;" in history


def test_json(app):
    client = TestClient(create_app(app))
    assert client.get("/api/v1/daily-summary", params={"date": FEB2.isoformat()}).json()["previous_date"] == "2026-01-30"
    app.reports.save(FEB2, "二月二日")
    assert client.get("/api/v1/reports").json()[0]["as_of"] == FEB2.isoformat()
    assert client.get(f"/api/v1/reports/{FEB2}").json()["content"] == "二月二日"
    assert client.get("/api/v1/reports/2026-01-27").status_code == 404
