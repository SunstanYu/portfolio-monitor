from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from portfolio_monitor.app import App, open_app
from portfolio_monitor.config import parse_settings
from portfolio_monitor.sources.fx import StaticFx
from portfolio_monitor.storage.db import connect, migrate
from portfolio_monitor.storage.repository import Repository


@pytest.fixture
def repo() -> Repository:
    conn = connect(":memory:")
    migrate(conn)
    return Repository(conn)


def make_app(tmp_path: Path, config: dict) -> App:
    settings = parse_settings({"fx": {"provider": "static"}, **config}, config_path=None, root=tmp_path)
    settings = dataclasses.replace(settings, data_dir=tmp_path / "data")
    return open_app(settings, fx=StaticFx())


@pytest.fixture
def make_test_app(tmp_path: Path):
    apps: list[App] = []

    def factory(config: dict) -> App:
        app = make_app(tmp_path, config)
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.close()
