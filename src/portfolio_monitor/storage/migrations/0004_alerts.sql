-- 0004：风险提示；持仓的名义价值（合约等衍生品的敞口，原币）

ALTER TABLE positions ADD COLUMN notional REAL;

-- 每个估值日的规则结果；同一天重算时整体覆盖。subject：标的 / 账户 / 资产类别 / 数据源，组合层面为空
CREATE TABLE alerts (
    as_of        TEXT NOT NULL,
    rule_id      TEXT NOT NULL,
    subject      TEXT NOT NULL DEFAULT '',
    subject_name TEXT,
    severity     TEXT NOT NULL CHECK (severity IN ('warn', 'critical')),
    message      TEXT NOT NULL,
    value        REAL,
    threshold    REAL,
    PRIMARY KEY (as_of, rule_id, subject)
);

-- 哪些估值日算过规则（没有提示的日子也记，才能判断提示是不是新出现的）
CREATE TABLE alert_runs (
    as_of        TEXT PRIMARY KEY,
    evaluated_at TEXT NOT NULL,
    n_alerts     INTEGER NOT NULL
);
