"""汇率：1 单位外币 = 多少美元。

FrankfurterFx：欧洲央行参考汇率（api.frankfurter.dev，免费、无需 key）。
周末和节假日返回最近一个工作日的汇率，effective_date 会早于请求日期。
StaticFx：固定汇率，用于测试和演示。
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx


class FxError(RuntimeError):
    pass


@dataclass(frozen=True)
class FxQuote:
    effective_date: dt.date
    usd_per_unit: dict[str, float]
    provider: str


class FxProvider(Protocol):
    name: str

    def fetch(self, day: dt.date, currencies: Sequence[str]) -> FxQuote: ...


class FrankfurterFx:
    name = "frankfurter"
    base_url = "https://api.frankfurter.dev/v1"

    def __init__(self, client: httpx.Client | None = None, retries: int = 2, backoff: float = 1.0):
        self.client = client or httpx.Client(timeout=10.0)
        self.retries = retries
        self.backoff = backoff

    def fetch(self, day: dt.date, currencies: Sequence[str]) -> FxQuote:
        wanted = sorted(set(currencies) - {"USD"})
        if not wanted:
            return FxQuote(day, {}, self.name)
        url = f"{self.base_url}/{day.isoformat()}"
        params = {"base": "USD", "symbols": ",".join(wanted)}
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.client.get(url, params=params)
                if resp.status_code >= 500:
                    raise FxError(f"汇率服务返回 {resp.status_code}")
                resp.raise_for_status()
                data = resp.json()
                rates = data["rates"]
                missing = [c for c in wanted if c not in rates]
                if missing:
                    raise FxError(f"汇率服务不支持：{missing}")
                return FxQuote(
                    effective_date=dt.date.fromisoformat(data["date"]),
                    usd_per_unit={c: 1.0 / float(rates[c]) for c in wanted},
                    provider=self.name,
                )
            except (httpx.TransportError, FxError) as e:
                last_error = e
                if attempt < self.retries:
                    time.sleep(self.backoff * (attempt + 1))
            except (httpx.HTTPStatusError, KeyError, ValueError) as e:
                raise FxError(f"汇率接口返回异常：{e}") from e
        raise FxError(f"汇率获取失败：{last_error}") from last_error


class StaticFx:
    name = "static"

    def __init__(self, usd_per_unit: Mapping[str, float] | None = None) -> None:
        self.rates = dict(usd_per_unit or {"HKD": 1 / 7.8, "CNY": 1 / 7.2})

    def fetch(self, day: dt.date, currencies: Sequence[str]) -> FxQuote:
        wanted = sorted(set(currencies) - {"USD"})
        missing = [c for c in wanted if c not in self.rates]
        if missing:
            raise FxError(f"固定汇率表里没有：{missing}")
        return FxQuote(day, {c: self.rates[c] for c in wanted}, self.name)


def build_fx(name: str) -> FxProvider:
    if name == "frankfurter":
        return FrankfurterFx()
    if name == "static":
        return StaticFx()
    raise KeyError(f"未知的汇率来源：{name}")
