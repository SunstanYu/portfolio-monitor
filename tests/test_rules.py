from __future__ import annotations

import argparse
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from portfolio_monitor.config import parse_settings
from portfolio_monitor.domain.models import Account, CashBalance, Instrument, Position
from portfolio_monitor.domain.returns import PerfPoint
from portfolio_monitor.domain.rules import (
    RULES,
    Alert,
    PortfolioContext,
    Severity,
    evaluate_all,
    parse_rule_config,
)
from portfolio_monitor.entry.cli import cmd_alerts, cmd_sync
from portfolio_monitor.entry.web import create_app

AS_OF = dt.date(2026, 3, 2)
DEFAULTS = parse_rule_config({}, {})


def row(account="a", iid="US:AAA", mv=0.0, *, row_type="position", asset_class="equity", investable=True,
        notional=None):
    return {"row_type": row_type, "account_id": account, "account_name": f"账户{account}", "instrument_id": iid,
            "name": iid.split(":")[-1], "asset_class": asset_class, "include_in_returns": investable,
            "market_value_usd": mv, "notional_usd": notional}


def cash(account="a", mv=0.0, investable=True):
    return row(account, "CASH:USD", mv, row_type="cash", asset_class="cash", investable=investable)


def perf(*index: float, end: dt.date = AS_OF) -> list[PerfPoint]:
    points, peak = [], 0.0
    for i, x in enumerate(index):
        peak = max(peak, x)
        points.append(PerfPoint(date=end - dt.timedelta(days=len(index) - 1 - i), nav=x * 100, net_flow=0.0,
                                daily_return=x / index[i - 1] - 1 if i else None, index=x, drawdown=x / peak - 1))
    return points


def ctx(rows=(), points=(), **kw) -> PortfolioContext:
    return PortfolioContext(as_of=AS_OF, rows=list(rows), perf=list(points), **kw)


def run(rule_id: str, c: PortfolioContext, **overrides) -> list[Alert]:
    rule = next(r for r in RULES if r.id == rule_id)
    return rule.evaluate(c, {**DEFAULTS.settings[rule_id], **overrides})


def brief(alerts: list[Alert]) -> list[tuple[str, str, float | None]]:
    return [(a.subject, a.severity.value, round(a.value, 4) if a.value is not None else None) for a in alerts]


# ---------- 各条规则 ----------

def test_instrument_concentration():
    rows = [row("a", "US:AAA", 30), row("b", "US:AAA", 10), row("a", "US:BBB", 25),
            row("o", "CRYPTO:USDT", 30, asset_class="cash"),  # 稳定币不算集中度
            cash("a", 5), cash("bank", 1000, investable=False)]  # 投资资产 = 100
    alerts = run("instrument_concentration", ctx(rows))
    assert brief(alerts) == [("US:AAA", "critical", 0.4), ("US:BBB", "warn", 0.25)]
    assert "分布在 2 个账户" in alerts[0].message and alerts[0].threshold == 0.35
    assert brief(run("instrument_concentration", ctx(rows), exempt=["US:AAA"])) == [("US:BBB", "warn", 0.25)]


def test_account_concentration():
    rows = [row("a", "US:AAA", 70), cash("b", 20), cash("bank", 10, investable=False)]  # 总资产 100，银行也算
    assert brief(run("account_concentration", ctx(rows))) == [("a", "warn", 0.7)]
    assert brief(run("account_concentration", ctx(rows), warn=0.75, critical=0.9)) == []


def test_allocation_drift_needs_targets():
    rows = [row("a", "US:AAA", 70), cash("a", 30)]
    assert run("allocation_drift", ctx(rows)) == []  # 没填目标配置不检查
    alerts = run("allocation_drift", ctx(rows, targets={"equity": 0.5, "cash": 0.4, "crypto": 0.1}))
    assert brief(alerts) == [("equity", "warn", 0.2), ("crypto", "warn", -0.1), ("cash", "warn", -0.1)]
    assert alerts[0].message == "股票 占总资产 70.0%，目标 50%，高 20.0 个百分点"


def test_idle_cash():
    rows = [row("a", "US:AAA", 65), cash("a", 20), row("o", "CRYPTO:USDT", 15, asset_class="cash")]
    alerts = run("idle_cash", ctx(rows))
    assert brief(alerts) == [("", "warn", 0.35)]
    assert "法币 20.0%，稳定币 15.0%" in alerts[0].message
    assert run("idle_cash", ctx(rows), warn=0.4) == []


def test_leverage():
    rows = [row("o", "UNKNOWN:okx:BTC-USDT-SWAP", 1, asset_class="other", notional=-40),  # 空头按绝对值
            row("o", "CRYPTO:USDT", 49, asset_class="cash"),
            row("i", "US:AAA", 70), cash("i", -30), cash("i2", 10)]  # 投资资产 100；只有 i 是融资
    alerts = run("leverage", ctx(rows))
    assert brief(alerts) == [("", "warn", 0.7)]
    assert "合约名义价值 40.0%，融资 30.0%" in alerts[0].message
    assert brief(run("leverage", ctx(rows), warn=0.5, critical=0.6)) == [("", "critical", 0.7)]


def test_drawdown():
    points = perf(1.0, 1.3, 1.0, 0.9)
    alerts = run("drawdown", ctx(points=points))
    assert brief(alerts) == [("", "critical", 0.3077)]
    assert str(points[1].date) in alerts[0].message  # 高点日期
    assert run("drawdown", ctx(points=perf(1.0, 1.3, 1.0, 0.9, end=AS_OF - dt.timedelta(days=1)))) == []


def test_daily_drop():
    assert brief(run("daily_drop", ctx(points=perf(1.0, 0.94)))) == [("", "warn", 0.06)]
    assert run("daily_drop", ctx(points=perf(1.0, 0.97))) == []
    assert run("daily_drop", ctx(points=perf(1.0, 1.2))) == []
    assert run("daily_drop", ctx(points=perf(1.0))) == []  # 第一天没有日收益


def test_source_failures():
    failures = {"ibkr": (2, "SourceError: boom"), "okx": (5, None), "manual": (1, None), "fake": (0, None)}
    alerts = run("source_failures", ctx(failures=failures))
    assert brief(alerts) == [("ibkr", "warn", 2), ("okx", "critical", 5)]
    assert "最近一次：SourceError: boom" in alerts[0].message


def test_data_jump():
    assert brief(run("data_jump", ctx(points=perf(1.0, 1.2)))) == [("", "warn", 0.2)]
    assert brief(run("data_jump", ctx(points=perf(1.0, 0.8)))) == [("", "warn", -0.2)]
    assert run("data_jump", ctx(points=perf(1.0, 1.1))) == []


def test_evaluate_all_orders_and_disables():
    c = ctx([row("a", "US:AAA", 100)], perf(1.0, 0.5))
    ids = [(a.rule_id, a.severity.value) for a in evaluate_all(c, DEFAULTS)]
    assert ids[0][1] == "critical" and ids[-1][1] == "warn"
    assert {"drawdown", "daily_drop", "data_jump", "instrument_concentration"} <= {i for i, _ in ids}
    off = parse_rule_config({"drawdown": {"enabled": False}}, {})
    assert "drawdown" not in {a.rule_id for a in evaluate_all(c, off)}


# ---------- 配置 ----------

@pytest.mark.parametrize(("rules", "targets", "message"), [
    ({"nope": {}}, {}, "未知规则 [rules.nope]"),
    ({"drawdown": {"warm": 0.2}}, {}, "不认识的设置：warm"),
    ({"drawdown": {"warn": 20}}, {}, "应写成 0.2"),
    ({"drawdown": {"warn": 0.3, "critical": 0.2}}, {}, "critical（0.2）必须大于 warn（0.3）"),
    ({"drawdown": {"warn": None, "critical": None}}, {}, "至少要设 warn 或 critical"),
    ({"source_failures": {"warn": 1.5}}, {}, "必须是正整数"),
    ({"idle_cash": {"enabled": "no"}}, {}, "必须是 true 或 false"),
    ({"instrument_concentration": {"exempt": "US:VOO"}}, {}, "必须是标的编号列表"),
    ({}, {"stocks": 1.0}, "不认识的资产类别：stocks"),
    ({}, {"equity": 0.6, "cash": 0.3}, "加起来应为 1，现在是 0.9"),
])
def test_config_errors(rules, targets, message):
    with pytest.raises(ValueError, match=message.replace("[", r"\[").replace("]", r"\]")
                       .replace("(", r"\(").replace(")", r"\)")):
        parse_rule_config(rules, targets)


def test_config_is_checked_at_load(tmp_path):
    with pytest.raises(ValueError, match="规则配置有误"):
        parse_settings({"rules": {"drawdown": {"warn": 20}}}, config_path=None, root=tmp_path)
    ok = parse_settings({"rules": {"leverage": {"warn": 1.5, "critical": 3}}, "targets": {"equity": 1}},
                        config_path=None, root=tmp_path)
    assert ok.rules.settings["leverage"]["critical"] == 3 and ok.rules.targets == {"equity": 1.0}


# ---------- 同步、存储、查询 ----------

START = dt.date(2026, 1, 5)  # 周一


def weekdays(n: int, start: dt.date = START) -> list[dt.date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def only(rule_id: str, **settings) -> dict:
    return {r.id: ({"enabled": True, **settings} if r.id == rule_id else {"enabled": False}) for r in RULES}


def fake_config(rules: dict, **fake) -> dict:
    return {
        "accounts": [{"id": "us", "name": "美股", "kind": "broker", "source": "fake", "currency": "USD"},
                     {"id": "bank", "name": "银行", "kind": "bank", "source": "manual", "currency": "USD",
                      "include_in_returns": False}],
        "sources": {"fake": {"start": START.isoformat(), **fake}},
        "rules": rules,
    }


def test_sync_runs_rules_and_tracks_since(make_test_app):
    app = make_test_app(fake_config(only("account_concentration")))
    days = weekdays(3)
    for d in days:
        report = app.sync.run(d)
        assert report.ok and [a.rule_id for a in report.alerts] == ["account_concentration"]
    first, last = app.risk.alerts(days[0]), app.risk.alerts(days[-1])
    assert first.evaluated and first.alerts[0].new and first.alerts[0].since == days[0]
    a = last.alerts[0]
    assert (a.subject, a.severity, a.new, a.since) == ("us", Severity.CRITICAL, False, days[0])
    assert last.critical == 1 and last.warn == 0

    app.repo.set_manual_cash("bank", "USD", 10_000_000)  # 银行存款远大于券商账户：券商不再集中，银行变集中
    app.sync.run(days[-1], only="manual")
    assert [(x.subject, x.new) for x in app.risk.alerts(days[-1]).alerts] == [("bank", True)]


def test_escalation_and_gaps(repo):
    repo.sync_accounts([Account(id="a", name="A", kind="broker", source="fake", currency="USD")])
    d1, d2, d3, d4 = weekdays(4)
    warn = Alert(rule_id="drawdown", severity=Severity.WARN, message="w", value=0.25)
    crit = warn.model_copy(update={"severity": Severity.CRITICAL, "value": 0.35})
    repo.replace_alerts(d1, [warn])
    repo.replace_alerts(d2, [])  # 中间有一天没触发
    repo.replace_alerts(d3, [warn])
    repo.replace_alerts(d4, [crit])
    assert repo.alert_since(d4, "drawdown", "") == d3.isoformat()
    assert repo.previous_alert_run(d4) == d3.isoformat()


def test_alert_views(make_test_app):
    app = make_test_app(fake_config(only("drawdown")))
    for d in weekdays(2):
        app.sync.run(d)
    d1, d2 = weekdays(2)
    app.repo.replace_alerts(d1, [Alert(rule_id="drawdown", severity=Severity.WARN, message="w", value=0.25)])
    app.repo.replace_alerts(d2, [Alert(rule_id="drawdown", severity=Severity.CRITICAL, message="c", value=0.35)])
    v = app.risk.alerts(d2).alerts[0]
    assert (v.rule_name, v.new, v.escalated, v.since) == ("回撤", False, True, d1)


def test_source_failures_from_sync_runs(make_test_app):
    days = weekdays(5)
    app = make_test_app(fake_config(only("source_failures"), fail_on=[d.isoformat() for d in days[3:]]))
    for d in days:
        app.sync.run(d)
    alerts = app.risk.alerts(days[-1]).alerts
    assert [(a.subject, a.severity, a.value) for a in alerts] == [("fake", Severity.WARN, 2)]
    assert [a.rule_id for a in app.risk.alerts(days[3]).alerts] == []  # 只失败 1 天不提示


def test_rule_failure_keeps_sync_results(make_test_app, monkeypatch):
    app = make_test_app(fake_config({}))

    def boom(_day):
        raise RuntimeError("规则坏了")

    monkeypatch.setattr(app.risk, "evaluate", boom)
    report = app.sync.run(START)
    assert report.alerts_error == "RuntimeError: 规则坏了" and not report.ok
    assert report.nav_total is not None and app.repo.nav_series("total")


def test_notional_is_stored_and_carried_forward(repo):
    repo.sync_accounts([Account(id="o", name="OKX", kind="crypto", source="okx", currency="USD")])
    repo.upsert_instruments([Instrument(id="UNKNOWN:okx:BTC-USDT-SWAP", asset_class="other", currency="USD")])
    pos = Position(account_id="o", instrument_id="UNKNOWN:okx:BTC-USDT-SWAP", quantity=2, market_value=150,
                   currency="USD", notional=13000)
    repo.replace_snapshot(START, ["o"], [pos], [CashBalance(account_id="o", currency="USD", amount=1)], None)
    assert repo.snapshot_rows(START)[0]["notional"] == 13000
    _, positions, _ = repo.latest_snapshot_before("o", START + dt.timedelta(days=1))
    assert positions[0].notional == 13000


# ---------- 命令行、网页、JSON ----------

def test_cli(make_test_app, capsys):
    app = make_test_app(fake_config(only("account_concentration")))
    assert cmd_sync(app, argparse.Namespace(date=START, source=None)) == 0
    out = capsys.readouterr().out
    assert f"风险提示（{START}）：严重 1，提示 0" in out and "单一账户集中度" in out and "新出现" in out

    app.risk.config = parse_rule_config(only("account_concentration", warn=0.99, critical=None), {})
    cmd_alerts(app, argparse.Namespace(date=None, recheck=False))
    assert "严重 1" in capsys.readouterr().out  # 不加 --recheck 显示存下的结果
    cmd_alerts(app, argparse.Namespace(date=None, recheck=True))
    assert f"风险提示（{START}）：严重 0，提示 1" in capsys.readouterr().out

    cmd_alerts(app, argparse.Namespace(date=START - dt.timedelta(days=3), recheck=False))
    assert "还没有算过风险规则" in capsys.readouterr().out


def test_web_and_json(make_test_app):
    app = make_test_app(fake_config(only("account_concentration")))
    app.sync.run(START)
    client = TestClient(create_app(app))
    page = client.get("/").text
    assert "风险提示" in page and "单一账户集中度" in page and "risk-critical" in page
    data = client.get("/api/v1/alerts").json()
    assert data["as_of"] == START.isoformat() and data["critical"] == 1
    assert data["alerts"][0]["rule_id"] == "account_concentration" and data["alerts"][0]["new"] is True

    app.risk.config = parse_rule_config({r.id: {"enabled": False} for r in RULES}, {})
    app.risk.evaluate(START)
    assert "风险提示" not in client.get("/").text  # 没有提示时不显示这一块
