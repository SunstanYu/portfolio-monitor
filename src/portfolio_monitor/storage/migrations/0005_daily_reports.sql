-- 0005：Hermes 写好的每日日报（网页同步展示）；同一估值日重复保存时覆盖
CREATE TABLE daily_reports (
    as_of      TEXT PRIMARY KEY,
    content    TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'mcp',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
