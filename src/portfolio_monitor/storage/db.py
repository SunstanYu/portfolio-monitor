"""SQLite 连接、事务与迁移。

迁移脚本放在 migrations/，文件名形如 0001_init.sql，按序号执行一次；
每个脚本在一个事务里执行，失败整体回滚。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    """打开数据库。path 为 ":memory:" 时使用内存库（测试用）。"""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：自动提交模式，事务由 transaction() 显式控制
    # check_same_thread=False：网页的路由都在同一个事件循环里串行执行，但这个循环未必跑在
    # 建立连接的线程上（如测试客户端），所以关掉 sqlite3 的线程检查；不要在多个线程里并发使用
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务；嵌套调用时只由最外层提交。"""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _migration_files() -> list[tuple[int, str, str]]:
    folder = resources.files("portfolio_monitor.storage").joinpath("migrations")
    found: list[tuple[int, str, str]] = []
    for entry in folder.iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    versions = [v for v, _, _ in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"迁移脚本序号重复：{versions}")
    return found


def migrate(conn: sqlite3.Connection) -> list[str]:
    """执行尚未执行的迁移，返回本次执行的脚本名。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    executed: list[str] = []
    for version, name, sql in _migration_files():
        if version in applied:
            continue
        script = (
            "BEGIN;\n"
            f"{sql}\n"
            f"INSERT INTO schema_migrations (version, name, applied_at) "
            f"VALUES ({version}, '{name}', '{utc_now()}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        executed.append(name)
    return executed


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0])


# ---------- 备份 ----------

BACKUP_NAME = re.compile(r"^portfolio-(\d{8})-(\d{6})\.sqlite$")  # 只清理这种名字；手动起名的备份不动


def backup(conn: sqlite3.Connection, folder: Path, *, keep_days: int = 30,
           now: datetime | None = None) -> tuple[Path, list[Path]]:
    """在线备份（不停服务）到 folder/portfolio-YYYYMMDD-HHMMSS.sqlite，校验后删掉超过 keep_days 天的旧备份。

    返回 (新备份路径, 删掉的旧备份)。备份文件权限 600。
    """
    now = now or datetime.now(UTC)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"portfolio-{now:%Y%m%d-%H%M%S}.sqlite"
    tmp = target.with_suffix(".tmp")
    dest = sqlite3.connect(str(tmp))
    try:
        conn.backup(dest)
        dest.execute("PRAGMA journal_mode = DELETE")  # 备份是单个文件，不带 -wal
        result = dest.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dest.close()
    if result != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"备份校验失败：{result}")
    tmp.chmod(0o600)
    tmp.replace(target)

    cutoff = now.date().toordinal() - keep_days
    removed = []
    for path in sorted(folder.iterdir()):
        m = BACKUP_NAME.match(path.name)
        if m and path != target and datetime.strptime(m.group(1), "%Y%m%d").date().toordinal() < cutoff:
            path.unlink()
            removed.append(path)
    return target, removed
