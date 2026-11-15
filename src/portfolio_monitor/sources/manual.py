"""手动账户：数据在本地库（manual_holdings / manual_cash），由网页或 MCP 修改。

每日快照时把当前状态复制一份。资金进出由修改服务直接写入 cash_flows，不经过这里。

自动更新价格（auto_price，默认开）：持仓能换算成 Yahoo 代码的（美股、港股、A 股、新加坡、加密货币），
用估值日当天或之前最近一个交易日的收盘价代替手填价格，只写进快照，不改手填价格；
取不到价格、或 Yahoo 的币种与持仓币种不一致时沿用手填价格，并在同步结果里提示。
同一估值日的报价缓存 30 分钟：网页上每次修改都会刷新快照，不必每次都去取价。

配置 [sources.manual]：
    auto_price = true
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Sequence
from typing import Any

from portfolio_monitor.domain.instruments import to_yahoo
from portfolio_monitor.domain.models import (
    Account,
    CashBalance,
    Instrument,
    Position,
    SourceSnapshot,
)
from portfolio_monitor.sources.base import SourceContext, SourceError
from portfolio_monitor.sources.prices import PriceError, PriceProvider, PriceQuote, YahooPrices

CACHE_SECONDS = 1800
STALE_DAYS = 5  # 报价日期比估值日早这么多天以上时提示（停牌、退市等）


class ManualSource:
    name = "manual"

    def __init__(self, ctx: SourceContext, *, prices: PriceProvider | None = None,
                 clock: Any = time.monotonic) -> None:
        self.repo = ctx.repo
        self.auto_price = bool(ctx.settings.get("auto_price", True))
        self.prices = prices if prices is not None else (YahooPrices() if self.auto_price else None)
        self.clock = clock
        self._cache: dict[tuple[str, dt.date], tuple[float, PriceQuote | str]] = {}

    def _quote(self, symbol: str, as_of: dt.date) -> PriceQuote | str:
        """报价，或取不到时的原因。"""
        key, now = (symbol, as_of), self.clock()
        hit = self._cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        try:
            result: PriceQuote | str = self.prices.close(symbol, as_of)  # type: ignore[union-attr]
        except PriceError as e:
            result = str(e)
        self._cache[key] = (now, result)
        return result

    def _price(self, inst: Instrument, currency: str, as_of: dt.date,
               warnings: list[str]) -> PriceQuote | None:
        symbol = to_yahoo(inst.id) if self.auto_price and self.prices is not None else None
        if symbol is None:
            return None
        label = inst.name or inst.id
        q = self._quote(symbol, as_of)
        if isinstance(q, str):
            warnings.append(f"{label} 取不到 Yahoo 价格（{q}），沿用手填价格")
            return None
        if q.currency != currency:
            warnings.append(f"{label} 的 Yahoo 价格是 {q.currency or '未知币种'}，持仓币种是 {currency}，沿用手填价格")
            return None
        if (as_of - q.date).days > STALE_DAYS:
            warnings.append(f"{label} 的最新价格停在 {q.date}，可能停牌")
        return q

    def fetch(self, as_of: dt.date, accounts: Sequence[Account]) -> SourceSnapshot:
        positions: list[Position] = []
        cash: list[CashBalance] = []
        instruments: dict[str, Instrument] = {}
        warnings: list[str] = []
        raw: dict[str, Any] = {"holdings": [], "cash": [], "prices": {}}

        for account in accounts:
            for h in self.repo.manual_holdings(account.id):
                inst = self.repo.get_instrument(h["instrument_id"])
                if inst is None:
                    raise SourceError(f"手动持仓引用了不存在的标的：{h['instrument_id']}")
                instruments[inst.id] = inst
                price = h["price"]
                if (q := self._price(inst, h["currency"], as_of, warnings)) is not None:
                    price = q.price
                    raw["prices"][inst.id] = {"symbol": q.symbol, "date": q.date.isoformat(), "price": q.price,
                                              "currency": q.currency, "manual_price": h["price"]}
                positions.append(
                    Position(
                        account_id=account.id,
                        instrument_id=inst.id,
                        quantity=h["quantity"],
                        price=price,
                        market_value=h["quantity"] * price,
                        cost_basis=h["cost_basis"],
                        currency=h["currency"],
                    )
                )
                raw["holdings"].append(h)
            for c in self.repo.manual_cash(account.id):
                cash.append(CashBalance(account_id=account.id, currency=c["currency"], amount=c["amount"]))
                raw["cash"].append(c)

        return SourceSnapshot(
            source=self.name,
            as_of=as_of,
            positions=positions,
            cash=cash,
            instruments=list(instruments.values()),
            warnings=list(dict.fromkeys(warnings)),  # 同一标的在几个账户里只提示一次
            raw=raw,
        )
