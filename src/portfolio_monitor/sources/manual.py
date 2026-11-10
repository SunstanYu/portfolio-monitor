"""手动账户：数据在本地库（manual_holdings / manual_cash），由网页或 MCP 修改。

每日快照时把当前状态原样复制一份。价格为手填价格（自动更新行情留到 M5）。
资金进出由修改服务直接写入 cash_flows，不经过这里。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from portfolio_monitor.domain.models import (
    Account,
    CashBalance,
    Instrument,
    Position,
    SourceSnapshot,
)
from portfolio_monitor.sources.base import SourceContext, SourceError


class ManualSource:
    name = "manual"

    def __init__(self, ctx: SourceContext) -> None:
        self.repo = ctx.repo

    def fetch(self, as_of: dt.date, accounts: Sequence[Account]) -> SourceSnapshot:
        positions: list[Position] = []
        cash: list[CashBalance] = []
        instruments: dict[str, Instrument] = {}
        raw: dict[str, list[dict]] = {"holdings": [], "cash": []}

        for account in accounts:
            for h in self.repo.manual_holdings(account.id):
                inst = self.repo.get_instrument(h["instrument_id"])
                if inst is None:
                    raise SourceError(f"手动持仓引用了不存在的标的：{h['instrument_id']}")
                instruments[inst.id] = inst
                positions.append(
                    Position(
                        account_id=account.id,
                        instrument_id=inst.id,
                        quantity=h["quantity"],
                        price=h["price"],
                        market_value=h["quantity"] * h["price"],
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
            raw=raw,
        )
