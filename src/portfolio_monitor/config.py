"""配置加载。

查找顺序：显式路径 → 环境变量 PM_CONFIG → local/config.toml → config.example.toml
数据目录：环境变量 PM_DATA_DIR → 配置 [paths].data_dir → data/
真实配置（local/config.toml）和凭据（.env）都不进 git。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from portfolio_monitor.domain.models import Account

SUPPORTED_BASE_CURRENCIES = ("USD",)


@dataclass(frozen=True)
class Settings:
    config_path: Path | None
    data_dir: Path
    accounts: list[Account]
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    fx_provider: str = "frankfurter"
    base_currency: str = "USD"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "portfolio.sqlite"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"


def resolve_config_path(explicit: Path | None = None, root: Path | None = None) -> Path:
    root = root or Path.cwd()
    candidates = [explicit, os.environ.get("PM_CONFIG"), root / "local/config.toml",
                  root / "config.example.toml"]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    raise FileNotFoundError("找不到配置文件：local/config.toml 或 config.example.toml")


def parse_settings(data: dict[str, Any], *, config_path: Path | None, root: Path) -> Settings:
    base = data.get("base_currency", "USD")
    if base not in SUPPORTED_BASE_CURRENCIES:
        raise ValueError(f"暂只支持基准货币 {SUPPORTED_BASE_CURRENCIES}，配置为 {base}")

    accounts = [Account(**entry) for entry in data.get("accounts", [])]
    ids = [a.id for a in accounts]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise ValueError(f"账户 id 重复：{dupes}")

    data_dir = Path(os.environ.get("PM_DATA_DIR") or data.get("paths", {}).get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = root / data_dir

    return Settings(
        config_path=config_path,
        data_dir=data_dir,
        accounts=accounts,
        sources={k: dict(v) for k, v in data.get("sources", {}).items()},
        fx_provider=data.get("fx", {}).get("provider", "frankfurter"),
        base_currency=base,
    )


def load_settings(path: Path | None = None, root: Path | None = None) -> Settings:
    root = root or Path.cwd()
    config_path = resolve_config_path(path, root)
    with config_path.open("rb") as fh:
        data = tomllib.load(fh)
    return parse_settings(data, config_path=config_path, root=root)
