from __future__ import annotations

import datetime as dt
import stat

import pytest
from mcp import Client

from portfolio_monitor.entry.mcp import build_mcp
from portfolio_monitor.services.journal import JournalService

UTC = dt.UTC
NOW = dt.datetime(2026, 11, 21, 14, 16, 0, tzinfo=UTC)


@pytest.fixture
def journal(tmp_path):
    return JournalService(tmp_path / "data" / "journal.txt", now=lambda: NOW)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_entries_use_send_time_in_new_york(journal):
    e = journal.add("  减仓是因为仓位太重\n下周再看  \n", sent_at=dt.datetime(2026, 11, 21, 14, 15, 32, tzinfo=UTC))
    assert (e.recorded_at.isoformat(), e.used_sent_at, e.chars) == ("2026-11-21T09:15:32-05:00", True, 14)
    journal.add("夏令时", sent_at=dt.datetime(2026, 7, 1, 13, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))))
    assert journal.path.read_text(encoding="utf-8") == (
        "=== 2026-11-21 周六 09:15:32 EST ===\n减仓是因为仓位太重\n下周再看\n\n"
        "=== 2026-07-01 周三 01:00:00 EDT ===\n夏令时\n\n"  # 只追加，不按时间重排
    )
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600


def test_without_send_time_uses_now(journal):
    e = journal.add("没给时间")
    assert (e.recorded_at.isoformat(), e.used_sent_at) == ("2026-11-21T09:16:00-05:00", False)


def test_rejects_bad_input(journal):
    with pytest.raises(ValueError, match="为空"):
        journal.add(" \n ")
    with pytest.raises(ValueError, match="时区"):
        journal.add("x", sent_at=dt.datetime(2026, 11, 21, 14, 15))
    with pytest.raises(ValueError, match="晚于当前时间"):
        journal.add("x", sent_at=NOW + dt.timedelta(minutes=6))
    journal.add("时钟差几分钟可以", sent_at=NOW + dt.timedelta(minutes=4))
    assert journal.path.read_text(encoding="utf-8").count("===") == 2


@pytest.mark.anyio
async def test_mcp_add_journal_entry(make_test_app):
    app = make_test_app({"accounts": [{"id": "m", "name": "M", "kind": "broker", "source": "manual",
                                       "currency": "USD"}]})
    async with Client(build_mcp(app), raise_exceptions=True) as c:
        r = await c.call_tool("add_journal_entry", {"content": "复盘：拿住了", "sent_at": "2026-11-21T14:15:32Z"})
        assert not r.is_error and r.structured_content["recorded_at"] == "2026-11-21T09:15:32-05:00"
        bad = await c.call_tool("add_journal_entry", {"content": "x", "sent_at": "2026-11-21T14:15:32"})
        assert bad.is_error and "时区" in bad.content[0].text
    text = (app.settings.data_dir / "journal.txt").read_text(encoding="utf-8")
    assert text == "=== 2026-11-21 周六 09:15:32 EST ===\n复盘：拿住了\n\n"
