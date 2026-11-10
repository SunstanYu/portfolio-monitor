"""数据源注册表：新增数据源在这里加一行。"""

from __future__ import annotations

from collections.abc import Callable

from portfolio_monitor.sources.base import Source, SourceContext
from portfolio_monitor.sources.fake import FakeSource
from portfolio_monitor.sources.manual import ManualSource

REGISTRY: dict[str, Callable[[SourceContext], Source]] = {
    "manual": ManualSource,
    "fake": FakeSource,
}


def build_source(name: str, ctx: SourceContext) -> Source:
    try:
        factory = REGISTRY[name]
    except KeyError:
        raise KeyError(f"未注册的数据源：{name}（已注册：{', '.join(sorted(REGISTRY))}）") from None
    return factory(ctx)
