-- 修改日志：手动账户的每一次修改（网页 / JSON 接口 / MCP），可撤销。
--
-- before / after 是 JSON 对象：{状态键: 状态}，状态键形如
--   holding|<账户>|<标的>   → {"quantity", "price", "cost_basis", "currency"}
--   cash|<账户>|<币种>      → 金额
--   flow|<账户>|<external_id> → {"date", "currency", "amount", "note"}
-- 状态为 null 表示该项不存在（新增前 / 删除后）。
-- 撤销时要求每个键的当前状态仍等于 after，然后写回 before。

CREATE TABLE change_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor      TEXT NOT NULL,                 -- web | api | mcp | cli
    kind       TEXT NOT NULL CHECK (kind IN ('set_holding', 'remove_holding', 'set_cash', 'record_flow')),
    account_id TEXT NOT NULL REFERENCES accounts (id),
    summary    TEXT NOT NULL,                 -- 一句话说明，含改前 → 改后
    request    TEXT NOT NULL,                 -- 原始请求 JSON，便于审计
    before     TEXT NOT NULL,
    after      TEXT NOT NULL,
    instrument TEXT,                          -- 生效时要写入 / 更新的标的资料 JSON
    status     TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'undone', 'discarded')),
    expires_at TEXT,                          -- 待确认改动的过期时间
    applied_at TEXT,
    undone_at  TEXT
);

CREATE INDEX change_log_account ON change_log (account_id, id);
CREATE INDEX change_log_status ON change_log (status, id);
