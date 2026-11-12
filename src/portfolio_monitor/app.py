"""组装各层：打开数据库、执行迁移、同步账户配置、构建数据源和服务。"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from portfolio_monitor.config import Settings
from portfolio_monitor.services.edit import EditService
from portfolio_monitor.services.query import QueryService
from portfolio_monitor.services.sync import SyncService
from portfolio_monitor.sources.base import Source, SourceContext
from portfolio_monitor.sources.fx import FxProvider, build_fx
from portfolio_monitor.sources.registry import build_source
from portfolio_monitor.storage.db import connect, migrate
from portfolio_monitor.storage.repository import Repository

NY = ZoneInfo("America/New_York")


def today_ny() -> dt.date:
    """估值日以纽约时间为准。"""
    return dt.datetime.now(NY).date()


@dataclass
class App:
    settings: Settings
    repo: Repository
    sync: SyncService
    query: QueryService
    edit: EditService

    def close(self) -> None:
        self.repo.conn.close()


def open_app(settings: Settings, *, fx: FxProvider | None = None) -> App:
    conn = connect(settings.db_path)
    migrate(conn)
    repo = Repository(conn)
    repo.sync_accounts(settings.accounts)

    sources: dict[str, Source] = {}
    for name in sorted({a.source for a in settings.accounts if a.active}):
        ctx = SourceContext(repo=repo, settings=settings.sources.get(name, {}), env=os.environ)
        sources[name] = build_source(name, ctx)

    sync = SyncService(repo, sources, fx or build_fx(settings.fx_provider), settings.raw_dir)
    return App(settings, repo, sync, QueryService(repo), EditService(repo, sync, today_ny))
