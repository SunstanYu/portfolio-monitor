import datetime as dt

from portfolio_monitor.domain.models import Account, CashFlow
from portfolio_monitor.storage.db import connect, migrate, schema_version


def test_migrations_run_once(tmp_path):
    conn = connect(tmp_path / "x.sqlite")
    assert migrate(conn) == ["0001_init.sql", "0002_change_log.sql"]
    assert migrate(conn) == []
    assert schema_version(conn) == 2
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"accounts", "instruments", "positions", "cash_balances", "cash_flows", "fx_rates",
            "daily_nav", "manual_holdings", "manual_cash", "sync_runs", "unresolved_symbols",
            "change_log"} <= tables


def test_accounts_follow_config(repo):
    a = Account(id="a", name="A", kind="broker", source="fake", currency="USD")
    b = Account(id="b", name="B", kind="bank", source="manual", currency="HKD")
    repo.sync_accounts([a, b])
    repo.sync_accounts([a])           # b 从配置中移除 → 停用但保留
    assert [x.id for x in repo.list_accounts()] == ["a"]
    assert not repo.get_account("b").active


def test_cash_flow_dedupe_by_external_id(repo):
    repo.sync_accounts([Account(id="a", name="A", kind="broker", source="fake", currency="USD")])
    f = CashFlow(date=dt.date(2026, 1, 2), account_id="a", currency="USD", amount=10, external_id="t1")
    assert repo.insert_cash_flows([f, f], origin="api") == 1
    # 没有 external_id 的（手动录入）不去重
    g = CashFlow(date=dt.date(2026, 1, 2), account_id="a", currency="USD", amount=10)
    assert repo.insert_cash_flows([g, g], origin="manual") == 2


def test_fx_lookup(repo):
    repo.upsert_fx(dt.date(2026, 1, 5), "HKD", 0.128, dt.date(2026, 1, 2), "test")
    assert repo.fx_on_or_before(dt.date(2026, 1, 4), "HKD") is None
    assert repo.fx_on_or_before(dt.date(2026, 1, 9), "HKD") == ("2026-01-05", 0.128)
    assert repo.fx_nearest(dt.date(2026, 1, 1), "HKD") == ("2026-01-05", 0.128)
    assert repo.fx_on_or_before(dt.date(2026, 1, 1), "USD")[1] == 1.0
