"""数据库读写。所有 SQL 都集中在这里，上层只调用方法。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from portfolio_monitor.domain.models import (
    Account,
    AccountValue,
    CashBalance,
    CashFlow,
    Instrument,
    Position,
    UnresolvedSymbol,
)
from portfolio_monitor.domain.rules.base import Alert
from portfolio_monitor.storage.db import transaction, utc_now


def _d(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else value


def _placeholders(items: Sequence[Any]) -> str:
    return ",".join("?" * len(items))


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---------- 账户 ----------

    def sync_accounts(self, accounts: Sequence[Account]) -> None:
        """以配置为准：配置里的账户写入并激活，其余账户标记为停用（数据保留）。"""
        now = utc_now()
        with transaction(self.conn):
            for a in accounts:
                self.conn.execute(
                    "INSERT INTO accounts (id, name, institution, kind, source, currency,"
                    " include_in_returns, active, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                    " institution=excluded.institution, kind=excluded.kind,"
                    " source=excluded.source, currency=excluded.currency,"
                    " include_in_returns=excluded.include_in_returns,"
                    " active=excluded.active, updated_at=excluded.updated_at",
                    (a.id, a.name, a.institution, a.kind.value, a.source, a.currency,
                     int(a.include_in_returns), int(a.active), now),
                )
            ids = [a.id for a in accounts]
            if ids:
                self.conn.execute(
                    f"UPDATE accounts SET active=0, updated_at=? WHERE id NOT IN ({_placeholders(ids)})",
                    (now, *ids),
                )
            else:
                self.conn.execute("UPDATE accounts SET active=0, updated_at=?", (now,))

    def list_accounts(self, active_only: bool = True) -> list[Account]:
        sql = "SELECT * FROM accounts" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
        return [self._account(r) for r in self.conn.execute(sql)]

    def get_account(self, account_id: str) -> Account | None:
        row = self.conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return self._account(row) if row else None

    @staticmethod
    def _account(r: sqlite3.Row) -> Account:
        return Account(
            id=r["id"], name=r["name"], institution=r["institution"], kind=r["kind"],
            source=r["source"], currency=r["currency"],
            include_in_returns=bool(r["include_in_returns"]), active=bool(r["active"]),
        )

    # ---------- 标的 ----------

    def upsert_instruments(self, instruments: Iterable[Instrument]) -> None:
        now = utc_now()
        with transaction(self.conn):
            for i in instruments:
                self.conn.execute(
                    "INSERT INTO instruments (id, name, asset_class, region, sector, currency, extra,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
                    " name=COALESCE(excluded.name, instruments.name),"
                    " asset_class=excluded.asset_class,"
                    " region=COALESCE(excluded.region, instruments.region),"
                    " sector=COALESCE(excluded.sector, instruments.sector),"
                    " currency=excluded.currency, extra=excluded.extra, updated_at=excluded.updated_at",
                    (i.id, i.name, i.asset_class.value, i.region, i.sector, i.currency,
                     json.dumps(i.extra, ensure_ascii=False, sort_keys=True), now),
                )

    def get_instrument(self, instrument_id: str) -> Instrument | None:
        r = self.conn.execute("SELECT * FROM instruments WHERE id=?", (instrument_id,)).fetchone()
        if not r:
            return None
        return Instrument(
            id=r["id"], name=r["name"], asset_class=r["asset_class"], region=r["region"],
            sector=r["sector"], currency=r["currency"], extra=json.loads(r["extra"]),
        )

    # ---------- 同步记录 ----------

    def start_sync_run(self, as_of: date, source: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO sync_runs (as_of, source, status, started_at) VALUES (?,?,'running',?)",
            (_d(as_of), source, utc_now()),
        )
        return int(cur.lastrowid)

    def finish_sync_run(
        self,
        run_id: int,
        status: str,
        *,
        error: str | None = None,
        raw_path: str | None = None,
        counts: tuple[int, int, int] | None = None,
    ) -> None:
        n_pos, n_cash, n_flows = counts or (None, None, None)
        self.conn.execute(
            "UPDATE sync_runs SET status=?, finished_at=?, error=?, raw_path=?,"
            " n_positions=?, n_cash=?, n_flows=? WHERE id=?",
            (status, utc_now(), error, raw_path, n_pos, n_cash, n_flows, run_id),
        )

    def sync_run_history(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def latest_sync_runs(self, as_of: date) -> list[dict[str, Any]]:
        """每个数据源在该日最近一次运行。"""
        rows = self.conn.execute(
            "SELECT * FROM sync_runs WHERE id IN ("
            " SELECT MAX(id) FROM sync_runs WHERE as_of=? GROUP BY source) ORDER BY source",
            (_d(as_of),),
        )
        return [dict(r) for r in rows]

    # ---------- 快照 ----------

    def replace_snapshot(
        self,
        as_of: date,
        account_ids: Sequence[str],
        positions: Sequence[Position],
        cash: Sequence[CashBalance],
        sync_run_id: int | None,
        *,
        carried_forward: bool = False,
    ) -> None:
        """覆盖这些账户在该日的快照（同一天重复同步以最后一次为准）。"""
        day = _d(as_of)
        cf = int(carried_forward)
        with transaction(self.conn):
            if account_ids:
                ph = _placeholders(account_ids)
                self.conn.execute(
                    f"DELETE FROM positions WHERE as_of=? AND account_id IN ({ph})", (day, *account_ids)
                )
                self.conn.execute(
                    f"DELETE FROM cash_balances WHERE as_of=? AND account_id IN ({ph})",
                    (day, *account_ids),
                )
            self.conn.executemany(
                "INSERT INTO positions (as_of, account_id, instrument_id, quantity, price,"
                " market_value, cost_basis, currency, carried_forward, sync_run_id, notional)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(day, p.account_id, p.instrument_id, p.quantity, p.price, p.market_value,
                  p.cost_basis, p.currency, cf, sync_run_id, p.notional) for p in positions],
            )
            self.conn.executemany(
                "INSERT INTO cash_balances (as_of, account_id, currency, amount, carried_forward,"
                " sync_run_id) VALUES (?,?,?,?,?,?)",
                [(day, c.account_id, c.currency, c.amount, cf, sync_run_id) for c in cash],
            )

    def latest_snapshot_before(
        self, account_id: str, as_of: date
    ) -> tuple[str, list[Position], list[CashBalance]] | None:
        """该账户在 as_of 之前最近一天的快照，用于数据源失败时沿用。"""
        day = _d(as_of)
        row = self.conn.execute(
            "SELECT MAX(as_of) FROM ("
            " SELECT as_of FROM positions WHERE account_id=? AND as_of<?"
            " UNION SELECT as_of FROM cash_balances WHERE account_id=? AND as_of<?)",
            (account_id, day, account_id, day),
        ).fetchone()
        prev = row[0]
        if prev is None:
            return None
        positions = [
            Position(account_id=r["account_id"], instrument_id=r["instrument_id"],
                     quantity=r["quantity"], price=r["price"], market_value=r["market_value"],
                     cost_basis=r["cost_basis"], currency=r["currency"], notional=r["notional"])
            for r in self.conn.execute(
                "SELECT * FROM positions WHERE account_id=? AND as_of=?", (account_id, prev)
            )
        ]
        cash = [
            CashBalance(account_id=r["account_id"], currency=r["currency"], amount=r["amount"])
            for r in self.conn.execute(
                "SELECT * FROM cash_balances WHERE account_id=? AND as_of=?", (account_id, prev)
            )
        ]
        return prev, positions, cash

    def snapshot_rows(self, as_of: date) -> list[dict[str, Any]]:
        """该日全部持仓 + 现金，已关联账户和标的属性；现金行的 instrument_id 为 CASH:<币种>。

        当天没有快照、只有历史总值（account_history）的账户，以一行 row_type='summary'、
        instrument_id 为 ACCOUNT:<账户> 的总值代替。
        """
        day = _d(as_of)
        rows = self.conn.execute(
            "SELECT 'position' AS row_type, p.account_id, a.name AS account_name,"
            " a.kind AS account_kind, a.include_in_returns, p.instrument_id, i.name,"
            " i.asset_class, i.region, i.sector, p.currency, p.quantity, p.price,"
            " p.market_value, p.cost_basis, p.carried_forward, p.notional"
            " FROM positions p JOIN accounts a ON a.id=p.account_id"
            " JOIN instruments i ON i.id=p.instrument_id WHERE p.as_of=?"
            " UNION ALL"
            " SELECT 'cash', c.account_id, a.name, a.kind, a.include_in_returns,"
            " 'CASH:' || c.currency, c.currency || ' 现金', 'cash', NULL, NULL, c.currency,"
            " c.amount, 1.0, c.amount, c.amount, c.carried_forward, NULL"
            " FROM cash_balances c JOIN accounts a ON a.id=c.account_id WHERE c.as_of=?"
            " UNION ALL"
            " SELECT 'summary', h.account_id, a.name, a.kind, a.include_in_returns,"
            " 'ACCOUNT:' || h.account_id, a.name || ' 总值', 'other', NULL, NULL, h.currency,"
            " NULL, NULL, h.value, NULL, 0, NULL"
            " FROM account_history h JOIN accounts a ON a.id=h.account_id WHERE h.as_of=?"
            " AND NOT EXISTS (SELECT 1 FROM positions p WHERE p.as_of=h.as_of AND p.account_id=h.account_id)"
            " AND NOT EXISTS (SELECT 1 FROM cash_balances c WHERE c.as_of=h.as_of AND c.account_id=h.account_id)",
            (day, day, day),
        )
        return [dict(r) for r in rows]

    def snapshot_dates(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT as_of FROM positions UNION SELECT as_of FROM cash_balances"
            " UNION SELECT as_of FROM account_history ORDER BY as_of"
        )
        return [r[0] for r in rows]

    def first_value_date(self, account_id: str) -> str | None:
        """该账户最早有估值（快照或历史总值）的日期。"""
        row = self.conn.execute(
            "SELECT MIN(as_of) FROM ("
            " SELECT as_of FROM positions WHERE account_id=?"
            " UNION SELECT as_of FROM cash_balances WHERE account_id=?"
            " UNION SELECT as_of FROM account_history WHERE account_id=?)",
            (account_id, account_id, account_id),
        ).fetchone()
        return row[0]

    def upsert_account_history(self, values: Iterable[AccountValue], origin: str) -> int:
        """写入账户历史总值（同一天同一账户以最后一次为准），返回写入条数。"""
        now = utc_now()
        rows = [(_d(v.date), v.account_id, v.currency, v.value, origin, now) for v in values]
        with transaction(self.conn):
            self.conn.executemany(
                "INSERT INTO account_history (as_of, account_id, currency, value, origin, updated_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(as_of, account_id) DO UPDATE SET"
                " currency=excluded.currency, value=excluded.value, origin=excluded.origin,"
                " updated_at=excluded.updated_at",
                rows,
            )
        return len(rows)

    def latest_as_of(self) -> str | None:
        row = self.conn.execute("SELECT MAX(as_of) FROM daily_nav").fetchone()
        return row[0]

    # ---------- 资金进出 ----------

    def insert_cash_flows(self, flows: Iterable[CashFlow], origin: str) -> int:
        """写入资金进出，按 (账户, external_id) 去重；返回实际新增条数。"""
        now = utc_now()
        inserted = 0
        with transaction(self.conn):
            for f in flows:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO cash_flows (date, account_id, currency, amount, origin,"
                    " external_id, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (_d(f.date), f.account_id, f.currency, f.amount, origin, f.external_id,
                     f.note, now),
                )
                inserted += cur.rowcount
        return inserted

    def cash_flows(
        self, *, after: date | None = None, until: date | None = None,
        account_ids: Sequence[str] | None = None,
    ) -> list[CashFlow]:
        sql, args = "SELECT * FROM cash_flows WHERE 1=1", []
        if after is not None:
            sql += " AND date>?"
            args.append(_d(after))
        if until is not None:
            sql += " AND date<=?"
            args.append(_d(until))
        if account_ids is not None:
            if not account_ids:
                return []
            sql += f" AND account_id IN ({_placeholders(account_ids)})"
            args.extend(account_ids)
        sql += " ORDER BY date, id"
        return [
            CashFlow(date=r["date"], account_id=r["account_id"], currency=r["currency"],
                     amount=r["amount"], external_id=r["external_id"], note=r["note"])
            for r in self.conn.execute(sql, args)
        ]

    def cash_flow_rows(self, account_id: str) -> list[dict[str, Any]]:
        """某账户全部资金进出（含来源、录入时间），新的在前。"""
        rows = self.conn.execute(
            "SELECT * FROM cash_flows WHERE account_id=? ORDER BY date DESC, id DESC", (account_id,)
        )
        return [dict(r) for r in rows]

    def get_cash_flow(self, account_id: str, external_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT date, currency, amount, note FROM cash_flows WHERE account_id=? AND external_id=?",
            (account_id, external_id),
        ).fetchone()
        return dict(row) if row else None

    def delete_cash_flow(self, account_id: str, external_id: str) -> None:
        self.conn.execute(
            "DELETE FROM cash_flows WHERE account_id=? AND external_id=?", (account_id, external_id)
        )

    # ---------- 汇率 ----------

    def upsert_fx(
        self, day: date, currency: str, usd_per_unit: float, effective_date: date | str,
        provider: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO fx_rates (date, currency, usd_per_unit, effective_date, provider)"
            " VALUES (?,?,?,?,?) ON CONFLICT(date, currency) DO UPDATE SET"
            " usd_per_unit=excluded.usd_per_unit, effective_date=excluded.effective_date,"
            " provider=excluded.provider",
            (_d(day), currency, usd_per_unit, _d(effective_date), provider),
        )

    def fx_on_or_before(self, day: date | str, currency: str) -> tuple[str, float] | None:
        if currency == "USD":
            return _d(day), 1.0
        row = self.conn.execute(
            "SELECT date, usd_per_unit FROM fx_rates WHERE currency=? AND date<=?"
            " ORDER BY date DESC LIMIT 1",
            (currency, _d(day)),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def fx_nearest(self, day: date | str, currency: str) -> tuple[str, float] | None:
        """优先取当日或之前最近的汇率；之前没有则取之后最近的（用于早于首个汇率日的资金进出）。"""
        found = self.fx_on_or_before(day, currency)
        if found:
            return found
        row = self.conn.execute(
            "SELECT date, usd_per_unit FROM fx_rates WHERE currency=? AND date>?"
            " ORDER BY date LIMIT 1",
            (currency, _d(day)),
        ).fetchone()
        return (row[0], row[1]) if row else None

    # ---------- 净值 ----------

    def upsert_nav(self, as_of: date, scope: str, nav_usd: float, stale: bool) -> None:
        self.conn.execute(
            "INSERT INTO daily_nav (as_of, scope, nav_usd, stale, computed_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(as_of, scope) DO UPDATE SET nav_usd=excluded.nav_usd,"
            " stale=excluded.stale, computed_at=excluded.computed_at",
            (_d(as_of), scope, nav_usd, int(stale), utc_now()),
        )

    def nav_series(
        self, scope: str, *, start: date | None = None, end: date | None = None
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT as_of, nav_usd, stale FROM daily_nav WHERE scope=?", [scope]
        if start is not None:
            sql += " AND as_of>=?"
            args.append(_d(start))
        if end is not None:
            sql += " AND as_of<=?"
            args.append(_d(end))
        sql += " ORDER BY as_of"
        return [dict(r) for r in self.conn.execute(sql, args)]

    # ---------- 手动账户 ----------

    def set_manual_holding(
        self, account_id: str, instrument_id: str, quantity: float, price: float,
        currency: str, cost_basis: float | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO manual_holdings (account_id, instrument_id, quantity, price, cost_basis,"
            " currency, updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_id, instrument_id)"
            " DO UPDATE SET quantity=excluded.quantity, price=excluded.price,"
            " cost_basis=excluded.cost_basis, currency=excluded.currency,"
            " updated_at=excluded.updated_at",
            (account_id, instrument_id, quantity, price, cost_basis, currency, utc_now()),
        )

    def remove_manual_holding(self, account_id: str, instrument_id: str) -> None:
        self.conn.execute(
            "DELETE FROM manual_holdings WHERE account_id=? AND instrument_id=?",
            (account_id, instrument_id),
        )

    def set_manual_cash(self, account_id: str, currency: str, amount: float) -> None:
        self.conn.execute(
            "INSERT INTO manual_cash (account_id, currency, amount, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(account_id, currency) DO UPDATE SET amount=excluded.amount,"
            " updated_at=excluded.updated_at",
            (account_id, currency, amount, utc_now()),
        )

    def remove_manual_cash(self, account_id: str, currency: str) -> None:
        self.conn.execute(
            "DELETE FROM manual_cash WHERE account_id=? AND currency=?", (account_id, currency)
        )

    def get_manual_holding(self, account_id: str, instrument_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT quantity, price, cost_basis, currency FROM manual_holdings"
            " WHERE account_id=? AND instrument_id=?",
            (account_id, instrument_id),
        ).fetchone()
        return dict(row) if row else None

    def get_manual_cash(self, account_id: str, currency: str) -> float | None:
        row = self.conn.execute(
            "SELECT amount FROM manual_cash WHERE account_id=? AND currency=?", (account_id, currency)
        ).fetchone()
        return row[0] if row else None

    def manual_holdings(self, account_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM manual_holdings WHERE account_id=? ORDER BY instrument_id", (account_id,)
        )
        return [dict(r) for r in rows]

    def manual_cash(self, account_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM manual_cash WHERE account_id=? ORDER BY currency", (account_id,)
        )
        return [dict(r) for r in rows]

    # ---------- 修改日志 ----------

    _CHANGE_JSON = ("request", "before", "after", "instrument")

    def insert_change(self, **fields: Any) -> int:
        fields = {k: self._json_out(k, v) for k, v in fields.items()}
        cols = list(fields)
        cur = self.conn.execute(
            f"INSERT INTO change_log ({', '.join(cols)}) VALUES ({_placeholders(cols)})",
            [fields[c] for c in cols],
        )
        return int(cur.lastrowid)

    def update_change(self, change_id: int, **fields: Any) -> None:
        fields = {k: self._json_out(k, v) for k, v in fields.items()}
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE change_log SET {sets} WHERE id=?", (*fields.values(), change_id))

    def get_change(self, change_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM change_log WHERE id=?", (change_id,)).fetchone()
        return self._change(row) if row else None

    def list_changes(
        self, *, account_id: str | None = None, statuses: Sequence[str] | None = None,
        after_id: int | None = None, limit: int = 200,
    ) -> list[dict[str, Any]]:
        """新的在前。"""
        sql, args = "SELECT * FROM change_log WHERE 1=1", []
        if account_id is not None:
            sql += " AND account_id=?"
            args.append(account_id)
        if statuses:
            sql += f" AND status IN ({_placeholders(statuses)})"
            args.extend(statuses)
        if after_id is not None:
            sql += " AND id>?"
            args.append(after_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [self._change(r) for r in self.conn.execute(sql, args)]

    def _json_out(self, key: str, value: Any) -> Any:
        if key in self._CHANGE_JSON and value is not None:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return value

    def _change(self, row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        for k in self._CHANGE_JSON:
            if out.get(k) is not None:
                out[k] = json.loads(out[k])
        return out

    # ---------- 待处理标的 ----------

    def record_unresolved(self, source: str, items: Iterable[UnresolvedSymbol], as_of: date) -> None:
        day = _d(as_of)
        with transaction(self.conn):
            for u in items:
                self.conn.execute(
                    "INSERT INTO unresolved_symbols (source, account_id, raw_symbol, first_seen,"
                    " last_seen, sample) VALUES (?,?,?,?,?,?) ON CONFLICT(source, account_id,"
                    " raw_symbol) DO UPDATE SET last_seen=excluded.last_seen, sample=excluded.sample",
                    (source, u.account_id, u.raw_symbol, day, day,
                     json.dumps(u.sample, ensure_ascii=False, default=str)),
                )

    def list_unresolved(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM unresolved_symbols ORDER BY last_seen DESC")
        return [dict(r) for r in rows]

    # ---------- 风险提示 ----------

    def replace_alerts(self, as_of: date, alerts: Iterable[Alert]) -> None:
        """覆盖该估值日的规则结果，并记下这天算过规则。"""
        day = _d(as_of)
        alerts = list(alerts)
        with transaction(self.conn):
            self.conn.execute("DELETE FROM alerts WHERE as_of=?", (day,))
            self.conn.executemany(
                "INSERT INTO alerts (as_of, rule_id, subject, subject_name, severity, message, value,"
                " threshold) VALUES (?,?,?,?,?,?,?,?)",
                [(day, a.rule_id, a.subject, a.subject_name, a.severity.value, a.message, a.value,
                  a.threshold) for a in alerts],
            )
            self.conn.execute(
                "INSERT INTO alert_runs (as_of, evaluated_at, n_alerts) VALUES (?,?,?)"
                " ON CONFLICT(as_of) DO UPDATE SET evaluated_at=excluded.evaluated_at,"
                " n_alerts=excluded.n_alerts",
                (day, utc_now(), len(alerts)),
            )

    def alert_run(self, as_of: date) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM alert_runs WHERE as_of=?", (_d(as_of),)).fetchone()
        return dict(row) if row else None

    def previous_alert_run(self, as_of: date) -> str | None:
        return self.conn.execute("SELECT MAX(as_of) FROM alert_runs WHERE as_of<?", (_d(as_of),)).fetchone()[0]

    def alerts_on(self, as_of: date | str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM alerts WHERE as_of=? ORDER BY rule_id, subject",
                                 (as_of if isinstance(as_of, str) else _d(as_of),))
        return [dict(r) for r in rows]

    def alert_since(self, as_of: date, rule_id: str, subject: str) -> str:
        """这条提示从哪个估值日起连续出现（只看算过规则的日子；as_of 当天应有这条提示）。"""
        day = _d(as_of)
        gap = self.conn.execute(
            "SELECT MAX(r.as_of) FROM alert_runs r WHERE r.as_of<? AND NOT EXISTS ("
            " SELECT 1 FROM alerts a WHERE a.as_of=r.as_of AND a.rule_id=? AND a.subject=?)",
            (day, rule_id, subject),
        ).fetchone()[0]
        first = self.conn.execute(
            "SELECT MIN(as_of) FROM alert_runs WHERE as_of<=? AND as_of>?", (day, gap or "")
        ).fetchone()[0]
        return first or day

    def consecutive_failures(self, as_of: date, sources: Iterable[str]) -> dict[str, tuple[int, str | None]]:
        """各数据源截至 as_of 连续失败的估值日数和最近一次的错误（每个估值日看最后一次运行）。"""
        wanted = set(sources)
        rows = self.conn.execute(
            "SELECT source, as_of, status, error FROM sync_runs WHERE id IN ("
            " SELECT MAX(id) FROM sync_runs WHERE as_of<=? AND status!='running' GROUP BY source, as_of)"
            " ORDER BY source, as_of DESC",
            (_d(as_of),),
        )
        out: dict[str, tuple[int, str | None]] = {}
        stopped: set[str] = set()
        for r in rows:
            src = r["source"]
            if src not in wanted or src in stopped:
                continue
            if r["status"] != "failed":
                stopped.add(src)
                out.setdefault(src, (0, None))
                continue
            n, err = out.get(src, (0, None))
            out[src] = (n + 1, err if n else r["error"])
        return out
