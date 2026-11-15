from __future__ import annotations

import argparse
import sqlite3
import stat
from datetime import UTC, datetime

from portfolio_monitor.entry.cli import cmd_backup
from portfolio_monitor.storage.db import backup, connect, migrate


def test_backup_is_complete_and_private(tmp_path):
    conn = connect(tmp_path / "portfolio.sqlite")
    migrate(conn)
    conn.execute("INSERT INTO accounts (id, name, kind, source, currency, updated_at) "
                 "VALUES ('a', 'A', 'broker', 'manual', 'USD', 'x')")
    target, removed = backup(conn, tmp_path / "backups", now=datetime(2026, 3, 2, 23, 30, tzinfo=UTC))
    assert target.name == "portfolio-20260302-233000.sqlite" and removed == []
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    copy = sqlite3.connect(target)
    assert copy.execute("SELECT name FROM accounts").fetchall() == [("A",)]
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # 单个文件，可直接拷走
    assert not list((tmp_path / "backups").glob("*.tmp"))


def test_backup_while_another_connection_is_open(tmp_path):
    path = tmp_path / "portfolio.sqlite"
    writer = connect(path)
    migrate(writer)
    writer.execute("BEGIN")
    writer.execute("INSERT INTO accounts (id, name, kind, source, currency, updated_at) "
                   "VALUES ('a', 'A', 'broker', 'manual', 'USD', 'x')")  # 网页进程正在写、还没提交
    target, _ = backup(connect(path), tmp_path / "backups")
    assert sqlite3.connect(target).execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    writer.execute("COMMIT")


def test_old_automatic_backups_are_pruned(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    for name in ("portfolio-20260101-233000.sqlite", "portfolio-20260130-233000.sqlite", "portfolio-20260131-233000.sqlite",
                 "portfolio-20260101-before-m6.sqlite", "notes.txt"):
        (folder / name).write_text("x")
    conn = connect(tmp_path / "portfolio.sqlite")
    migrate(conn)
    _, removed = backup(conn, folder, keep_days=30, now=datetime(2026, 3, 2, tzinfo=UTC))
    # 3 月 2 日往前 30 天是 1 月 31 日：更早的删掉，1 月 31 日起的保留
    assert [p.name for p in removed] == ["portfolio-20260101-233000.sqlite", "portfolio-20260130-233000.sqlite"]
    assert (folder / "portfolio-20260131-233000.sqlite").exists()
    assert (folder / "portfolio-20260101-before-m6.sqlite").exists()  # 手动起名的不动
    assert (folder / "notes.txt").exists()


def test_cli_backup(make_test_app, capsys):
    app = make_test_app({"accounts": [{"id": "m", "name": "M", "kind": "broker", "source": "manual",
                                       "currency": "USD"}]})
    assert cmd_backup(app, argparse.Namespace(keep=30)) == 0
    out = capsys.readouterr().out
    assert "备份：" in out and "已校验" in out
    assert len(list((app.settings.data_dir / "backups").glob("portfolio-*.sqlite"))) == 1
