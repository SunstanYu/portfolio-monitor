"""假数据源：确定性的虚构数据，用于测试、演示和验证 Source 接口。

- 价格：从 start 日起的确定性随机游走（同一标的同一天的价格每次都一样）
- 分配：USD 账户持美股和加密货币；HKD 账户持港股和 A 股；没有对应账户时放进第一个账户
- 现金：每个账户一笔初始现金；第一个账户每月第一个工作日入金 monthly_deposit
- 资金进出：返回最近 flow_lookback_days 天内的入金（重复返回，靠 external_id 去重）

配置 [sources.fake]：start、vol_scale、drift_scale、monthly_deposit、flow_lookback_days、fail_on
"""

from __future__ import annotations

import datetime as dt
import math
import random
from collections.abc import Sequence

from portfolio_monitor.domain.models import (
    Account,
    AssetClass,
    CashBalance,
    CashFlow,
    Instrument,
    Position,
    SourceSnapshot,
)
from portfolio_monitor.sources.base import SourceContext, SourceError

# 标的, 名称, 资产类别, 地区, 行业, 币种, 数量, 初始价格, 日波动, 日漂移
_BOOK = (
    ("US:NVDA", "NVIDIA", AssetClass.EQUITY, "US", "Technology", "USD", 120, 140.0, 0.030, 0.0008),
    ("US:VOO", "Vanguard S&P 500 ETF", AssetClass.ETF, "US", None, "USD", 40, 480.0, 0.010, 0.0003),
    ("CRYPTO:BTC", "Bitcoin", AssetClass.CRYPTO, "GLOBAL", None, "USD", 0.5, 65000.0, 0.035, 0.0010),
    ("HK:00700", "Tencent", AssetClass.EQUITY, "HK", "Communication", "HKD", 300, 380.0, 0.020, 0.0002),
    ("CN:600519", "Kweichow Moutai", AssetClass.EQUITY, "CN", "Consumer", "CNY", 20, 1500.0, 0.015, 0.0),
)
_HOME = {"USD": ("US", "CRYPTO"), "HKD": ("HK", "CN")}
_BASE_CASH = {"USD": 10_000.0, "HKD": 50_000.0, "CNY": 30_000.0}


class FakeSource:
    name = "fake"

    def __init__(self, ctx: SourceContext) -> None:
        s = ctx.settings
        self.start = dt.date.fromisoformat(str(s.get("start", "2025-01-01")))
        self.vol_scale = float(s.get("vol_scale", 1.0))
        self.drift_scale = float(s.get("drift_scale", 1.0))
        self.monthly_deposit = float(s.get("monthly_deposit", 2000.0))
        self.lookback = int(s.get("flow_lookback_days", 7))
        self.fail_on = {dt.date.fromisoformat(str(d)) for d in s.get("fail_on", [])}
        self._price_cache: dict[str, dict[dt.date, float]] = {}

    # ---------- 确定性的价格与入金 ----------

    def _price(self, instrument_id: str, base: float, vol: float, drift: float, day: dt.date) -> float:
        cache = self._price_cache.setdefault(instrument_id, {})
        if day <= self.start:
            return base
        if day in cache:
            return cache[day]
        known = [d for d in cache if d < day]
        cursor = max(known) if known else self.start
        log_p = math.log(cache.get(cursor, base))
        while cursor < day:
            cursor += dt.timedelta(days=1)
            z = random.Random(f"{instrument_id}:{cursor.isoformat()}").gauss(0.0, 1.0)
            log_p += drift * self.drift_scale + vol * self.vol_scale * z
            cache[cursor] = math.exp(log_p)
        return cache[day]

    def _deposit_dates(self, until: dt.date) -> list[dt.date]:
        dates, month = [], dt.date(self.start.year, self.start.month, 1)
        while month <= until:
            d = month
            while d.weekday() >= 5:
                d += dt.timedelta(days=1)
            if self.start < d <= until:
                dates.append(d)
            month = dt.date(month.year + month.month // 12, month.month % 12 + 1, 1)
        return dates

    # ---------- Source 接口 ----------

    def fetch(self, as_of: dt.date, accounts: Sequence[Account]) -> SourceSnapshot:
        if as_of in self.fail_on:
            raise SourceError(f"模拟故障：{as_of}")
        if not accounts:
            return SourceSnapshot(source=self.name, as_of=as_of)

        ordered = sorted(accounts, key=lambda a: a.id)
        home: dict[str, Account] = {}
        for a in ordered:
            for market in _HOME.get(a.currency, ()):
                home.setdefault(market, a)

        positions, instruments, prices = [], [], {}
        for inst_id, name, asset_class, region, sector, ccy, qty, base, vol, drift in _BOOK:
            account = home.get(inst_id.split(":")[0], ordered[0])
            price = self._price(inst_id, base, vol, drift, as_of)
            prices[inst_id] = price
            instruments.append(
                Instrument(id=inst_id, name=name, asset_class=asset_class, region=region,
                           sector=sector, currency=ccy)
            )
            positions.append(
                Position(account_id=account.id, instrument_id=inst_id, quantity=qty, price=price,
                         market_value=qty * price, cost_basis=qty * base, currency=ccy)
            )

        depositor = ordered[0]
        deposits = self._deposit_dates(as_of)
        cash = []
        for a in ordered:
            amount = _BASE_CASH.get(a.currency, 10_000.0)
            if a.id == depositor.id:
                amount += self.monthly_deposit * len(deposits)
            cash.append(CashBalance(account_id=a.id, currency=a.currency, amount=amount))

        window_start = as_of - dt.timedelta(days=self.lookback)
        flows = [
            CashFlow(date=d, account_id=depositor.id, currency=depositor.currency,
                     amount=self.monthly_deposit, external_id=f"fake:{depositor.id}:{d:%Y-%m}",
                     note="模拟月度入金")
            for d in deposits
            if d > window_start
        ]

        return SourceSnapshot(
            source=self.name,
            as_of=as_of,
            positions=positions,
            cash=cash,
            flows=flows,
            instruments=instruments,
            raw={"as_of": as_of.isoformat(), "prices": prices},
        )
