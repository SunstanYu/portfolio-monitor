"""交易复盘 / 投资思考记录：用户在 Telegram 上让 Hermes 记一段复盘，Hermes 调用 add_journal_entry，
把原话按消息的发送时间追加到数据目录下的 journal.txt（只有本用户可读写）。每条：

    === 2026-11-21 周六 09:15:32 EST ===
    原话

时间一律换算成纽约时间。现阶段只写这个文本文件，不进数据库、不在网页显示，也不在每日备份里。
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

NY = ZoneInfo("America/New_York")
WEEKDAYS = "一二三四五六日"
FUTURE_TOLERANCE = dt.timedelta(minutes=5)  # 服务器和 Telegram 的时钟差


class JournalEntry(BaseModel):
    recorded_at: dt.datetime = Field(description="这条记录的时间（纽约时间）")
    used_sent_at: bool = Field(description="true：用的是消息发送时间；false：没给发送时间，用了当前时间")
    chars: int


class JournalService:
    def __init__(self, path: Path, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        self.path = path
        self.now = now

    def add(self, content: str, sent_at: dt.datetime | None = None) -> JournalEntry:
        text = content.strip()
        if not text:
            raise ValueError("复盘内容为空")
        now = self.now()
        if sent_at is not None:
            if sent_at.tzinfo is None:
                raise ValueError("sent_at 要带时区，如 2026-11-21T14:15:32+00:00")
            if sent_at - now > FUTURE_TOLERANCE:
                raise ValueError(f"sent_at {sent_at.isoformat()} 晚于当前时间，请用消息开头的发送时间")
        at = (sent_at or now).astimezone(NY).replace(microsecond=0)
        header = f"=== {at:%Y-%m-%d} 周{WEEKDAYS[at.weekday()]} {at:%H:%M:%S %Z} ==="
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"{header}\n{text}\n\n")
        return JournalEntry(recorded_at=at, used_sent_at=sent_at is not None, chars=len(text))
