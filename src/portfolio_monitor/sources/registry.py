"""数据源注册表：新增数据源在这里加一行。"""

from __future__ import annotations

from collections.abc import Callable

from portfolio_monitor.sources.base import Source, SourceContext
from portfolio_monitor.sources.fake import FakeSource
from portfolio_monitor.sources.ibkr import IbkrSource
from portfolio_monitor.sources.manual import ManualSource
from portfolio_monitor.sources.okx import OkxSource
from portfolio_monitor.sources.usmart import UsmartSource

REGISTRY: dict[str, Callable[[SourceContext], Source]] = {
    "manual": ManualSource,
    "fake": FakeSource,
    "ibkr": IbkrSource,
    "okx": OkxSource,
    "usmart": UsmartSource,
}

# 接口拿不到资金流水的数据源：允许在网页 / MCP 手动记入金出金（只记流水，不改现金）
MANUAL_FLOW_SOURCES = {"usmart"}


def build_source(name: str, ctx: SourceContext) -> Source:
    try:
        factory = REGISTRY[name]
    except KeyError:
        raise KeyError(f"未注册的数据源：{name}（已注册：{', '.join(sorted(REGISTRY))}）") from None
    return factory(ctx)
