-- 0001：初始结构（M2 核心）
-- 日期一律存 ISO 字符串 YYYY-MM-DD；时间戳存 UTC ISO 字符串。
-- 金额以各自币种存储，换算 USD 在读取时按当日汇率进行。

CREATE TABLE accounts (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    institution        TEXT,
    kind               TEXT NOT NULL CHECK (kind IN ('broker', 'bank', 'crypto', 'other')),
    source             TEXT NOT NULL,
    currency           TEXT NOT NULL,
    include_in_returns INTEGER NOT NULL DEFAULT 1,
    active             INTEGER NOT NULL DEFAULT 1,
    updated_at         TEXT NOT NULL
);

-- 统一标的编号 <市场>:<代码>；识别不了的为 UNKNOWN:<数据源>:<原始写法>
CREATE TABLE instruments (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    asset_class TEXT NOT NULL,
    region      TEXT,
    sector      TEXT,
    currency    TEXT NOT NULL,
    extra       TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL
);

CREATE TABLE sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of       TEXT NOT NULL,
    source      TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    error       TEXT,
    raw_path    TEXT,
    n_positions INTEGER,
    n_cash      INTEGER,
    n_flows     INTEGER
);
CREATE INDEX idx_sync_runs_as_of ON sync_runs (as_of, source);

-- 持仓快照：最细粒度长表（标的 × 账户 × 日期）
CREATE TABLE positions (
    as_of           TEXT NOT NULL,
    account_id      TEXT NOT NULL REFERENCES accounts (id),
    instrument_id   TEXT NOT NULL REFERENCES instruments (id),
    quantity        REAL NOT NULL,
    price           REAL,
    market_value    REAL NOT NULL,
    cost_basis      REAL,
    currency        TEXT NOT NULL,
    carried_forward INTEGER NOT NULL DEFAULT 0,
    sync_run_id     INTEGER REFERENCES sync_runs (id),
    PRIMARY KEY (as_of, account_id, instrument_id)
);
CREATE INDEX idx_positions_instrument ON positions (instrument_id, as_of);

CREATE TABLE cash_balances (
    as_of           TEXT NOT NULL,
    account_id      TEXT NOT NULL REFERENCES accounts (id),
    currency        TEXT NOT NULL,
    amount          REAL NOT NULL,
    carried_forward INTEGER NOT NULL DEFAULT 0,
    sync_run_id     INTEGER REFERENCES sync_runs (id),
    PRIMARY KEY (as_of, account_id, currency)
);

-- 资金进出：入正出负。external_id 为数据源流水号，用于去重
CREATE TABLE cash_flows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    account_id  TEXT NOT NULL REFERENCES accounts (id),
    currency    TEXT NOT NULL,
    amount      REAL NOT NULL,
    origin      TEXT NOT NULL CHECK (origin IN ('api', 'manual')),
    external_id TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (account_id, external_id)
);
CREATE INDEX idx_cash_flows_date ON cash_flows (date);

-- 汇率：1 单位外币 = usd_per_unit 美元。date 为需要汇率的估值日，effective_date 为汇率实际所属日期
CREATE TABLE fx_rates (
    date           TEXT NOT NULL,
    currency       TEXT NOT NULL,
    usd_per_unit   REAL NOT NULL,
    effective_date TEXT NOT NULL,
    provider       TEXT NOT NULL,
    PRIMARY KEY (date, currency)
);

-- 每日净值缓存。scope：total 全部账户；investable 只含计入收益率的账户
CREATE TABLE daily_nav (
    as_of       TEXT NOT NULL,
    scope       TEXT NOT NULL CHECK (scope IN ('total', 'investable')),
    nav_usd     REAL NOT NULL,
    stale       INTEGER NOT NULL DEFAULT 0,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (as_of, scope)
);

-- 手动账户的当前状态；每日快照时复制进 positions / cash_balances
CREATE TABLE manual_holdings (
    account_id    TEXT NOT NULL REFERENCES accounts (id),
    instrument_id TEXT NOT NULL REFERENCES instruments (id),
    quantity      REAL NOT NULL,
    price         REAL NOT NULL,
    cost_basis    REAL,
    currency      TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (account_id, instrument_id)
);

CREATE TABLE manual_cash (
    account_id TEXT NOT NULL REFERENCES accounts (id),
    currency   TEXT NOT NULL,
    amount     REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, currency)
);

-- 识别不了的标的：不丢弃，计入 UNKNOWN 标的，同时登记在这里待处理
CREATE TABLE unresolved_symbols (
    source     TEXT NOT NULL,
    account_id TEXT NOT NULL,
    raw_symbol TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    sample     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source, account_id, raw_symbol)
);
