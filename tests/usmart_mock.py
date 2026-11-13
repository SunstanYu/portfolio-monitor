"""模拟盈立新加坡 Open API：用虚构样本 fixtures/usmart_sample.json，不访问网络。

和真实服务器一样严格：
- 每个请求验 X-Sign（MD5withRSA，用客户端公钥验请求体）和 X-Channel，不对返回 107005 非法请求
- 登录：用平台私钥解密手机号和密码；不对返回 300709；成功发 token（30 天有效）
- 登录后的请求：缺 X-Type=12 返回 107005；没有 token 返回 107003；token 不认识返回 300101
- 持仓 exchangeType=100 返回全部，资产 exchangeType=5 / 0 分别给美元 / 港币口径，其他返回 470000
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import itertools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from portfolio_monitor.sources.usmart import UsmartClient

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "usmart_sample.json").read_text(encoding="utf-8"))
CHANNEL = "1000000001"
AREA, PHONE, PASSWORD = "86", "13800000000", "Test$pw01"
NOW = dt.datetime(2026, 3, 2, 21, 0, tzinfo=dt.timezone.utc)  # 纽约 3 月 2 日 16:00
UTC = dt.timezone.utc

# 测试用钥匙：客户端一对（签名），平台一对（加密手机号和密码）
CLIENT_KEY = rsa.generate_private_key(public_exponent=65537, key_size=1024)
PLATFORM_KEY = rsa.generate_private_key(public_exponent=65537, key_size=1024)


def b64_private(key: rsa.RSAPrivateKey) -> str:
    return base64.b64encode(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption())).decode()


def b64_public(key: rsa.RSAPrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()


def env(**overrides: str) -> dict[str, str]:
    return {"USMART_CHANNEL": CHANNEL, "USMART_AREA_CODE": AREA, "USMART_PHONE": PHONE,
            "USMART_LOGIN_PASSWORD": PASSWORD, "USMART_PRIVATE_KEY": b64_private(CLIENT_KEY),
            "USMART_PUBLIC_KEY": b64_public(PLATFORM_KEY), **overrides}


def _resp(code: int, msg: str, data: Any = None, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "msg": msg, "data": data})


class UsmartMock:
    def __init__(self, *, password: str = PASSWORD, overrides: dict[str, Any] | None = None,
                 http_status: int = 200) -> None:
        self.password = password
        self.sample = copy.deepcopy(SAMPLE)
        self.sample.update(overrides or {})
        self.http_status = http_status
        self.requests: list[httpx.Request] = []
        self.logins = 0
        self.tokens: set[str] = set()
        self._ids = itertools.count(1)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def expire_tokens(self) -> None:
        self.tokens.clear()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.http_status != 200:
            return httpx.Response(self.http_status, content=b"<html>oops</html>")
        h, body = request.headers, request.content
        try:
            CLIENT_KEY.public_key().verify(base64.b64decode(h.get("X-Sign", "")), body,
                                           padding.PKCS1v15(), hashes.MD5())
        except (InvalidSignature, ValueError):
            return _resp(107005, "非法请求")
        if h.get("X-Channel") != CHANNEL:
            return _resp(107005, "非法请求")
        params = json.loads(body)
        path = request.url.path
        if path == "/user-server-sg/open-api/login":
            return self._login(params)
        if h.get("X-Type") != "12":
            return _resp(107005, "非法请求")
        token = h.get("Authorization")
        if not token:
            return _resp(107003, "Token 不能为空")
        if token not in self.tokens:
            return _resp(300101, "非法TOKEN")
        if path == "/asset-center-sg/open-api/stock-holding":
            et = str(params.get("exchangeType"))
            rows = self.sample["holdings"]
            return _resp(0, "成功", rows if et in ("100", "8", "67") else [r for r in rows if r["exchangeType"] == et])
        if path == "/asset-center-sg/open-api/stock-asset":
            et = str(params.get("exchangeType"))
            if et not in ("0", "5"):
                return _resp(470000, "参数错误")
            return _resp(0, "成功", self.sample["asset_usd" if et == "5" else "asset_hkd"])
        return _resp(450404, "请求的路径错误")

    def _login(self, params: dict[str, Any]) -> httpx.Response:
        self.logins += 1

        def dec(v: str) -> str:
            return PLATFORM_KEY.decrypt(base64.urlsafe_b64decode(v), padding.PKCS1v15()).decode()

        try:
            phone, password = dec(params["phoneNumber"]), dec(params["password"])
        except (KeyError, ValueError):
            return _resp(300709, "账号或密码错误，请重新输入")
        if (params.get("areaCode"), phone, password) != (AREA, PHONE, self.password):
            return _resp(300709, "账号或密码错误，请重新输入")
        token = f"token-{next(self._ids)}"
        self.tokens.add(token)
        expires = int((NOW + dt.timedelta(days=30)).timestamp() * 1000)
        return _resp(0, "成功", {"token": token, "expiration": expires, "openedAccount": True,
                                "phoneNumber": "138****0000", "nickname": "测试", "uuid": "0"})

    def client(self, *, now: Callable[[], dt.datetime] | None = None) -> UsmartClient:
        return UsmartClient(CHANNEL, CLIENT_KEY, PLATFORM_KEY.public_key(),
                            client=httpx.Client(transport=httpx.MockTransport(self.handler)),
                            now=now or (lambda: NOW))
