"""模拟 OKX API v5：用虚构样本 fixtures/okx_sample.json，不访问网络。

- 私有接口校验 key、passphrase 和签名：不对分别返回 50111 / 50105 / 50113（HTTP 401）
- 请求时间戳与服务器时间（NOW）相差超过 30 秒返回 50102；/api/v5/public/time 返回 NOW
- rate_limit_once：第一次请求返回 50011（HTTP 429）
- errors：{路径: (错误码, 信息)}，该接口固定返回这个错误
- 充提记录按 after（早于该时间戳）和 limit 翻页；历史日线按 instId 和 after 从样本的 candles 里取
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import hmac
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from portfolio_monitor.sources.okx import OkxClient

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "okx_sample.json").read_text(encoding="utf-8"))
KEY = "00000000-0000-4000-8000-000000000001"
SECRET = "0123456789ABCDEF0123456789ABCDEF"
PASSPHRASE = "Test$pass1"
NOW = dt.datetime(2026, 3, 2, 21, 0, tzinfo=dt.timezone.utc)  # 纽约 3 月 2 日 16:00
UTC = dt.timezone.utc


def _ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"code": "0", "msg": "", "data": data})


def _fail(status: int, code: str, msg: str) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "msg": msg, "data": []})


class OkxMock:
    def __init__(self, *, overrides: dict[str, Any] | None = None,
                 errors: dict[str, tuple[str, str]] | None = None,
                 rate_limit_once: bool = False, http_status: int = 200) -> None:
        self.sample = copy.deepcopy(SAMPLE)
        self.sample.update(overrides or {})
        self.errors = errors or {}
        self.rate_limit_once = rate_limit_once
        self.http_status = http_status
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.http_status != 200:
            return httpx.Response(self.http_status, content=b"<html>oops</html>")
        if self.rate_limit_once:
            self.rate_limit_once = False
            return _fail(429, "50011", "Too Many Requests")
        path, params = request.url.path, request.url.params
        if path == "/api/v5/public/time":
            return _ok([{"ts": str(int(NOW.timestamp() * 1000))}])
        if path.startswith("/api/v5/market/"):
            return self._market(path, params)
        if denied := self._auth(request):
            return denied
        if path in self.errors:
            code, msg = self.errors[path]
            return _fail(200, code, msg)
        if path in ("/api/v5/asset/deposit-history", "/api/v5/asset/withdrawal-history"):
            rows = sorted(self.sample[path], key=lambda r: -int(r["ts"]))
            if "after" in params:
                rows = [r for r in rows if int(r["ts"]) < int(params["after"])]
            return _ok(rows[: int(params.get("limit", 100))])
        if path not in self.sample:
            return _fail(404, "50000", f"unknown path {path}")
        return _ok(self.sample[path])

    def _auth(self, request: httpx.Request) -> httpx.Response | None:
        h = request.headers
        if h.get("OK-ACCESS-KEY") != KEY:
            return _fail(401, "50111", "Invalid OK-ACCESS-KEY.")
        if h.get("OK-ACCESS-PASSPHRASE") != PASSPHRASE:
            return _fail(401, "50105", "Request header OK-ACCESS-PASSPHRASE incorrect.")
        ts = h.get("OK-ACCESS-TIMESTAMP", "")
        target = request.url.raw_path.decode()
        expected = base64.b64encode(
            hmac.new(SECRET.encode(), f"{ts}GET{target}".encode(), hashlib.sha256).digest()).decode()
        if h.get("OK-ACCESS-SIGN") != expected:
            return _fail(401, "50113", "Invalid Sign")
        sent = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        if abs((sent - NOW).total_seconds()) > 30:
            return _fail(401, "50102", "Timestamp request expired")
        return None

    def _market(self, path: str, params: httpx.QueryParams) -> httpx.Response:
        if path == "/api/v5/market/index-tickers":
            return _ok(self.sample[path])
        if path == "/api/v5/market/history-index-candles":
            before = dt.datetime.fromtimestamp(int(params["after"]) / 1000, UTC)
            day = (before - dt.timedelta(days=1)).date()
            close = self.sample["candles"].get(params["instId"], {}).get(day.isoformat())
            if close is None:
                return _ok([])
            ts = str(int(dt.datetime.combine(day, dt.time(), UTC).timestamp() * 1000))
            return _ok([[ts, close, close, close, close, "1"]])
        return _fail(404, "50000", f"unknown path {path}")

    def client(self, *, now: Callable[[], dt.datetime] | None = None, secret: str = SECRET) -> OkxClient:
        return OkxClient(KEY, secret, PASSPHRASE,
                         client=httpx.Client(transport=httpx.MockTransport(self.handler)),
                         sleep=self.sleeps.append, now=now or (lambda: NOW))
