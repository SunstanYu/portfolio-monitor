-- 0003：账户历史总值（M5，IBKR 回填）
-- 只有总值、没有持仓明细的历史数据。某账户某日没有快照（positions / cash_balances）时，
-- 估值和净值计算用这里的总值代替；有快照的日子以快照为准。

CREATE TABLE account_history (
    as_of      TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts (id),
    currency   TEXT NOT NULL,
    value      REAL NOT NULL,
    origin     TEXT NOT NULL,             -- 数据来源，如 ibkr:backfill
    updated_at TEXT NOT NULL,
    PRIMARY KEY (as_of, account_id)
);
