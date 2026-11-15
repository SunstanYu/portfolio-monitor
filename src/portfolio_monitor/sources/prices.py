"""行情：取某个标的在估值日当天或之前最近一个交易日的收盘价，用于手动持仓自动更新价格。

YahooPrices：Yahoo Finance 的 chart 接口（v8，免费、无需 key，但不是公开承诺的接口，可能改动或限流）。
日线的时间戳按交易所时区换算成交易日，再取不晚于估值日的最后一根；收盘价为空的（停牌等）跳过。
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from portfolio_monitor import __version__

UTC = dt.timezone.utc


class PriceError(RuntimeError):
    pass


@dataclass(frozen=True)
class PriceQuote:
    symbol: str
    date: dt.date  # 价格所属交易日（交易所当地日期）
    price: float
    currency: str


class PriceProvider(Protocol):
    name: str

    def close(self, symbol: str, as_of: dt.date) -> PriceQuote:
        """as_of 当天或之前最近一个交易日的收盘价；取不到时抛 PriceError。"""
        ...


class YahooPrices:
    name = "yahoo"
    base_url = "https://query1.finance.yahoo.com/v8/finance/chart"

    def __init__(self, client: httpx.Client | None = None, *, retries: int = 1, backoff: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.client = client or httpx.Client(timeout=10.0, headers={
            # Yahoo 会拒绝没有浏览器标识的请求
            "User-Agent": f"Mozilla/5.0 (compatible; portfolio-monitor/{__version__})",
        })
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep

    def close(self, symbol: str, as_of: dt.date) -> PriceQuote:
        start = dt.datetime.combine(as_of - dt.timedelta(days=10), dt.time(), UTC)
        end = dt.datetime.combine(as_of + dt.timedelta(days=2), dt.time(), UTC)
        params = {"period1": int(start.timestamp()), "period2": int(end.timestamp()), "interval": "1d"}
        url = f"{self.base_url}/{quote(symbol, safe='')}"
        for attempt in range(self.retries + 1):
            try:
                r = self.client.get(url, params=params)
            except httpx.HTTPError as e:
                if attempt < self.retries:
                    self.sleep(self.backoff * (attempt + 1))
                    continue
                raise PriceError(f"请求失败：{type(e).__name__}") from None
            if (r.status_code == 429 or r.status_code >= 500) and attempt < self.retries:
                self.sleep(self.backoff * (attempt + 1))
                continue
            break
        if r.status_code == 404:
            raise PriceError("Yahoo 没有这个代码")
        if r.status_code != 200:
            raise PriceError(f"Yahoo 返回 HTTP {r.status_code}")
        try:
            chart = r.json()["chart"]
        except (ValueError, KeyError, TypeError):
            raise PriceError("Yahoo 返回的不是行情数据") from None
        if chart.get("error"):
            err = chart["error"]
            raise PriceError(str(err.get("description") or err.get("code") or err) if isinstance(err, dict) else str(err))
        try:
            result = chart["result"][0]
            meta = result["meta"]
            stamps = result.get("timestamp") or []
            closes = result["indicators"]["quote"][0].get("close") or []
        except (KeyError, IndexError, TypeError):
            raise PriceError("Yahoo 返回的行情缺少字段") from None
        tz = _timezone(meta)
        best: tuple[dt.date, float] | None = None
        for ts, close in zip(stamps, closes, strict=False):
            if close is None:
                continue
            day = dt.datetime.fromtimestamp(ts, tz).date()
            if day <= as_of:
                best = (day, float(close))
        if best is None:
            raise PriceError(f"{as_of} 及之前 10 天没有收盘价")
        return PriceQuote(symbol=symbol, date=best[0], price=best[1], currency=str(meta.get("currency") or "").upper())


def _timezone(meta: dict) -> dt.tzinfo:
    name = meta.get("exchangeTimezoneName")
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    return dt.timezone(dt.timedelta(seconds=int(meta.get("gmtoffset") or 0)))
