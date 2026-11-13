"""盈立新加坡（uSMART SG）：官方 Open API，登录拿 token 后只读查询。

接入细节以官方 Python 示例和实测为准（文档 api-doc.usmart.sg 有几处与实际不符）：
- 交易服务器 https://open-jy.usmartsg.com；文档写的 open-jy.yxzq.com 登录不了新加坡账户
- 每个请求带 X-Channel（渠道号）、X-Lang、X-Type=12（文档没写，缺了报 107005 非法请求）、X-Request-Id、
  X-Time，以及 X-Sign = base64(MD5withRSA(自己的私钥, 请求体 JSON))；登录后再带 Authorization: <token>
- 登录：手机号和登录密码用盈立公钥加密（RSA PKCS#1 v1.5，urlsafe base64），token 约 30 天有效
- token 缓存在 <data_dir>/secrets/usmart_token.json（权限 600），过期前不重复登录（免得把手机 App 挤下线）；
  查询返回 token 失效时重新登录一次
- 防锁号：登录返回账号或密码错误等时，记下这组凭据的指纹，之后的同步不再尝试，
  直到 .env 里的手机号或密码改过（文档有“账户被锁”的错误码，但没写失败几次会锁）

映射规则：
- 持仓：stock-holding（exchangeType=100 查全部市场），数量为 0 的跳过；按每行的 exchangeType 归入
  HK（0）/ US（5）/ SG（8）/ CN（67）；名称含 ETF 的记为 ETF
- 美股期权按代码识别（标的 + YYMMDD + C/P + 行权价×1000，如 MSFT270618P350000）：接口给的是每股价格，
  市值 = 数量 × 价格 × 100；名义价值 = |数量| × 行权价 × 100（卖出看跌期权的接货义务），供杠杆规则用；
  编号 UNKNOWN:usmart:<代码>，登记待处理
- 现金：stock-asset（exchangeType=5）给出美元口径的总资产和持仓市值，现金 = 总资产 − 持仓市值，记为美元
  （接口不给各币种现金）。账户总值与盈立一致；被卖出期权占用的保证金也在这笔现金里
- 对账：自己算的持仓市值（港股按盈立自己的港币汇率换算）与盈立的持仓市值相差超过 reconcile_tolerance 时提示
- 资金流水：新加坡服务器上的流水接口不可用（450404 路径错误）；入金出金在网页 / MCP 手动记（只记流水）
- 只能同步当天（接口只给当前持仓），不支持回填；首次同步那天的市值按晚加入账户算转入

配置 [sources.usmart]：
    host = "https://open-jy.usmartsg.com"
    reconcile_tolerance = 0.01
凭据（环境变量）：USMART_CHANNEL（渠道号）、USMART_AREA_CODE（手机区号，如 86）、USMART_PHONE、
USMART_LOGIN_PASSWORD（登录密码，不是交易密码）、USMART_PRIVATE_KEY（自己的私钥）、
USMART_PUBLIC_KEY（盈立公钥）；钥匙可以是“我的 API”页面给的一整段 base64，也可以是 PEM
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from portfolio_monitor import __version__
from portfolio_monitor.domain.instruments import UnresolvedInstrument, normalize, unknown_id
from portfolio_monitor.domain.models import (
    Account,
    AssetClass,
    CashBalance,
    Instrument,
    Position,
    SourceSnapshot,
    UnresolvedSymbol,
)
from portfolio_monitor.sources.base import SourceContext, SourceError

ENV = {
    "channel": "USMART_CHANNEL",
    "area": "USMART_AREA_CODE",
    "phone": "USMART_PHONE",
    "password": "USMART_LOGIN_PASSWORD",
    "private_key": "USMART_PRIVATE_KEY",
    "public_key": "USMART_PUBLIC_KEY",
}
DEFAULT_HOST = "https://open-jy.usmartsg.com"
NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

LOGIN_PATH = "/user-server-sg/open-api/login"
HOLDING_PATH = "/asset-center-sg/open-api/stock-holding"
ASSET_PATH = "/asset-center-sg/open-api/stock-asset"

TOKEN_ERRORS = {"300101", "107003"}  # 非法 TOKEN、Token 不能为空
# 登录被拒、继续尝试可能锁号的错误：账号或密码错误、手机号未注册、账户冻结 / 锁定 / 注销、需要短信验证
LOGIN_BLOCKING = {"300709", "300703", "300701", "300702", "300102", "300103", "300705", "300809"}

MARKETS = {"0": ("HK", "HKD"), "5": ("US", "USD"), "8": ("SG", "SGD"), "67": ("CN", "CNY")}
OPTION_MULTIPLIER = 100
_OPTION = re.compile(r"^([A-Z][A-Z0-9.]*?)(\d{6})([CP])(\d+)$")


class UsmartError(SourceError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"盈立错误 {code}：{message}")


# ---------- 钥匙与签名 ----------

def load_key(text: str, *, private: bool, name: str) -> Any:
    """“我的 API”页面给的一整段 base64（DER），或带 BEGIN / END 的 PEM。"""
    text = text.strip()
    try:
        if "-----BEGIN" in text:
            data = text.encode()
            return (serialization.load_pem_private_key(data, None) if private
                    else serialization.load_pem_public_key(data))
        der = base64.b64decode("".join(text.split()))
        return serialization.load_der_private_key(der, None) if private else serialization.load_der_public_key(der)
    except (ValueError, TypeError):
        kind = "私钥" if private else "公钥"
        raise SourceError(f"{name} 不是有效的 RSA {kind}") from None


class UsmartClient:
    """签名、加密、发请求；返回 data，不解析内容。"""

    def __init__(
        self,
        channel: str,
        private_key: rsa.RSAPrivateKey,
        public_key: rsa.RSAPublicKey,
        *,
        host: str = DEFAULT_HOST,
        client: httpx.Client | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.channel = channel
        self.private_key = private_key
        self.public_key = public_key
        self.host = host.rstrip("/")
        self.client = client or httpx.Client(timeout=20)
        self.now = now or (lambda: dt.datetime.now(UTC))

    def encrypt(self, text: str) -> str:
        return base64.urlsafe_b64encode(self.public_key.encrypt(text.encode(), padding.PKCS1v15())).decode()

    def sign(self, body: str) -> str:
        return base64.b64encode(self.private_key.sign(body.encode(), padding.PKCS1v15(), hashes.MD5())).decode()

    def post(self, path: str, params: Mapping[str, Any], token: str | None = None) -> Any:
        body = json.dumps(params)  # 签名的内容必须和发出去的请求体完全一致
        stamp = self.now()
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"portfolio-monitor/{__version__}",
            "X-Lang": "1",
            "X-Channel": self.channel,
            "X-Type": "12",
            "X-Request-Id": f"{int(stamp.timestamp() * 10**6)}{secrets.randbelow(1000):03d}",
            "X-Time": str(int(stamp.timestamp())),
            "X-Sign": self.sign(body),
        }
        if token:
            headers["Authorization"] = token
        try:
            r = self.client.post(self.host + path, content=body, headers=headers)
        except httpx.HTTPError as e:
            raise SourceError(f"盈立 {path} 请求失败：{type(e).__name__}") from None
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise SourceError(f"盈立 {path} 返回 HTTP {r.status_code}（不是 JSON）")
        code = str(payload.get("code"))
        if code != "0":
            raise UsmartError(code, str(payload.get("msg") or ""))
        return payload.get("data")

    def login(self, area: str, phone: str, password: str) -> tuple[str, dt.datetime]:
        data = self.post(LOGIN_PATH, {"phoneNumber": self.encrypt(phone), "password": self.encrypt(password),
                                      "areaCode": area})
        token = (data or {}).get("token")
        if not token:
            raise SourceError("盈立登录成功但没有返回 token")
        ms = int((data or {}).get("expiration") or 0)
        expires = dt.datetime.fromtimestamp(ms / 1000, UTC) if ms else self.now() + dt.timedelta(days=1)
        return token, expires


# ---------- token 缓存 ----------

class TokenStore:
    """token 和登录失败记录存在本地文件（权限 600）；没有数据目录时只存在内存里。"""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._memory: dict[str, Any] = {}

    def load(self) -> dict[str, Any]:
        if self.path is None:
            return dict(self._memory)
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def save(self, state: dict[str, Any]) -> None:
        if self.path is None:
            self._memory = dict(state)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


# ---------- 解析 ----------

def _f(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        raise SourceError(f"盈立数值格式无法识别：{value!r}") from None


def parse_option(code: str) -> tuple[str, dt.date, str, float] | None:
    """美股期权代码 → (标的, 到期日, C/P, 行权价)；不是期权返回 None。"""
    m = _OPTION.match(code.strip().upper())
    if not m:
        return None
    try:
        expiry = dt.datetime.strptime(m.group(2), "%y%m%d").date()
    except ValueError:
        return None
    return m.group(1), expiry, m.group(3), int(m.group(4)) / 1000


def parse_holdings(
    account_id: str, rows: Sequence[Mapping[str, Any]]
) -> tuple[list[Position], list[Instrument], list[UnresolvedSymbol], list[str]]:
    merged: dict[str, dict[str, Any]] = {}
    instruments: dict[str, Instrument] = {}
    unresolved: dict[str, UnresolvedSymbol] = {}
    warnings: list[str] = []
    for r in rows:
        qty = _f(r.get("currentAmount"))
        if abs(qty) < 1e-12:
            continue  # 已清仓的记录
        code = str(r.get("stockCode") or "").strip().upper()
        name = str(r.get("stockName") or code) or None
        et = str(r.get("exchangeType"))
        sample = {k: r.get(k) for k in ("exchangeType", "stockCode", "stockName") if r.get(k) is not None}
        market, currency = MARKETS.get(et, (None, "USD"))
        multiplier, notional = 1, None
        option = parse_option(code) if market == "US" else None
        if option:
            multiplier = OPTION_MULTIPLIER
            notional = abs(qty) * option[3] * OPTION_MULTIPLIER
            inst = Instrument(id=unknown_id("usmart", code), name=name, asset_class=AssetClass.OPTION,
                              region="US", currency=currency, extra={"usmart": sample})
            pending = True
        else:
            try:
                if market is None:
                    raise UnresolvedInstrument(code)
                iid = normalize(code, market)
                etf = "ETF" in (name or "").upper()
                inst = Instrument(id=iid, name=name, asset_class=AssetClass.ETF if etf else AssetClass.EQUITY,
                                  region=market, currency=currency)
                pending = False
            except UnresolvedInstrument:
                if market is None:
                    warnings.append(f"{code} 的市场代码 {et} 不认识，按美元计价")
                inst = Instrument(id=unknown_id("usmart", f"{et}-{code}"), name=name, asset_class=AssetClass.OTHER,
                                  currency=currency, extra={"usmart": sample})
                pending = True
        instruments[inst.id] = inst
        if pending:
            unresolved[inst.id] = UnresolvedSymbol(account_id=account_id, raw_symbol=code, sample=sample)
        price, cost = _f(r.get("lastPrice")), _f(r.get("costPriceAccurate"))
        m = merged.setdefault(inst.id, {"qty": 0.0, "value": 0.0, "cost": 0.0, "has_cost": False,
                                        "notional": None, "price": price, "currency": currency})
        m["qty"] += qty
        m["value"] += qty * price * multiplier
        if cost:
            m["cost"] += qty * cost * multiplier
            m["has_cost"] = True
        if notional is not None:
            m["notional"] = (m["notional"] or 0.0) + notional
    positions = [
        Position(account_id=account_id, instrument_id=iid, quantity=m["qty"], price=m["price"] or None,
                 market_value=m["value"], cost_basis=m["cost"] if m["has_cost"] else None,
                 currency=m["currency"], notional=m["notional"])
        for iid, m in merged.items()
    ]
    return positions, list(instruments.values()), list(unresolved.values()), warnings


def reconcile(positions: Sequence[Position], asset_usd: Mapping[str, Any], asset_hkd: Mapping[str, Any],
              tolerance: float) -> str | None:
    """自己算的持仓市值与盈立的持仓市值（美元口径）比较；港股按盈立自己的港币汇率换算。"""
    theirs = _f(asset_usd.get("marketValue"))
    total_usd, total_hkd = _f(asset_usd.get("asset")), _f(asset_hkd.get("asset"))
    others = sorted({p.currency for p in positions} - {"USD", "HKD"})
    if others:
        return f"持仓含 {'、'.join(others)}，没有盈立的汇率，未对账"
    if not total_usd or not total_hkd:
        return None
    hkd_per_usd = total_hkd / total_usd
    ours = sum(p.market_value / (hkd_per_usd if p.currency == "HKD" else 1.0) for p in positions)
    base = max(abs(theirs), abs(ours))
    if base < 1:
        return None
    diff = (ours - theirs) / base
    if abs(diff) > tolerance:
        return f"持仓市值与盈立的持仓市值相差 {diff:+.1%}，可能有估值不对的持仓（如期权乘数）"
    return None


# ---------- 数据源 ----------

class UsmartSource:
    name = "usmart"

    def __init__(self, ctx: SourceContext, *, client: UsmartClient | None = None,
                 now: Callable[[], dt.datetime] | None = None) -> None:
        s = ctx.settings
        self.tolerance = float(s.get("reconcile_tolerance", 0.01))
        self.now = now or (lambda: dt.datetime.now(UTC))
        self.env = {k: (ctx.env.get(v) or "") for k, v in ENV.items()}
        self.store = TokenStore(Path(ctx.data_dir) / "secrets" / "usmart_token.json" if ctx.data_dir else None)
        self.host = str(s.get("host", DEFAULT_HOST))
        self._client = client
        self._injected = client is not None  # 测试注入客户端时，渠道号和钥匙已在客户端里
        self._fresh: str | None = None  # 本次同步刚登录拿到的 token

    def _missing(self) -> list[str]:
        needed = ("area", "phone", "password") if self._injected else tuple(ENV)
        return [ENV[k] for k in needed if not self.env[k].strip()]

    @property
    def client(self) -> UsmartClient:
        """用到时才读钥匙：凭据有误只让这个数据源同步失败，不影响程序启动。"""
        if missing := self._missing():
            raise SourceError(f"缺少环境变量 {' / '.join(missing)}（见 .env.example）")
        if self._client is None:
            self._client = UsmartClient(
                self.env["channel"].strip(),
                load_key(self.env["private_key"], private=True, name=ENV["private_key"]),
                load_key(self.env["public_key"], private=False, name=ENV["public_key"]),
                host=self.host, now=self.now,
            )
        return self._client

    # ----- 登录与 token -----

    def _fingerprint(self) -> str:
        """这组登录凭据的指纹：用私钥做 HMAC，拿到缓存文件也推不出密码。"""
        key = self.client.private_key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption())
        creds = "|".join(self.env[k].strip() for k in ("channel", "area", "phone")) + "|" + self.env["password"]
        return hmac.new(key, creds.encode(), hashlib.sha256).hexdigest()

    def _token(self, *, force_login: bool = False) -> str:
        if self._fresh and not force_login:
            return self._fresh
        state, fp, now = self.store.load(), self._fingerprint(), self.now()
        failed = state.get("failed") or {}
        if failed.get("fingerprint") == fp:
            raise SourceError(
                f"盈立登录在 {failed.get('at', '?')[:10]} 被拒（{failed.get('code')} {failed.get('message')}），"
                "为免账户被锁不再自动重试；核对并修改 .env 里的手机号或登录密码后会再试")
        if (not force_login and state.get("fingerprint") == fp and state.get("token")
                and dt.datetime.fromisoformat(state["expires"]) > now + dt.timedelta(hours=12)):
            return state["token"]
        try:
            token, expires = self.client.login(self.env["area"].strip(), self.env["phone"].strip(), self.env["password"])
        except UsmartError as e:
            if e.code in LOGIN_BLOCKING:
                msg = str(e).split("：", 1)[-1]
                self.store.save({"failed": {"fingerprint": fp, "at": now.isoformat(), "code": e.code, "message": msg}})
            raise
        self.store.save({"fingerprint": fp, "token": token, "expires": expires.isoformat()})
        self._fresh = token
        return token

    def _query(self, path: str, params: Mapping[str, Any]) -> Any:
        try:
            return self.client.post(path, params, self._token())
        except UsmartError as e:
            if e.code not in TOKEN_ERRORS:
                raise
        return self.client.post(path, params, self._token(force_login=True))  # token 失效：重新登录一次

    # ----- 同步 -----

    def fetch(self, as_of: dt.date, accounts: Sequence[Account]) -> SourceSnapshot:
        if not accounts:
            return SourceSnapshot(source=self.name, as_of=as_of)
        if len(accounts) > 1:
            raise SourceError("盈立只对应一个账户")
        _ = self.client  # 先检查凭据
        self._fresh = None
        today = self.now().astimezone(NY).date()
        if as_of != today:
            raise SourceError(f"盈立只能取当前持仓，不能同步 {as_of}（纽约今天是 {today}）")
        account_id = accounts[0].id
        rows = self._query(HOLDING_PATH, {"exchangeType": "100"}) or []
        asset_usd = self._query(ASSET_PATH, {"exchangeType": "5"}) or {}
        asset_hkd = self._query(ASSET_PATH, {"exchangeType": "0"}) or {}

        positions, instruments, unresolved, warnings = parse_holdings(account_id, rows)
        cash = _f(asset_usd.get("asset")) - _f(asset_usd.get("marketValue"))
        if w := reconcile(positions, asset_usd, asset_hkd, self.tolerance):
            warnings.append(w)
        return SourceSnapshot(
            source=self.name, as_of=as_of, positions=positions,
            cash=[CashBalance(account_id=account_id, currency="USD", amount=cash)] if abs(cash) > 1e-9 else [],
            instruments=instruments, unresolved=unresolved, warnings=warnings,
            raw={"holdings": rows, "asset_usd": asset_usd, "asset_hkd": asset_hkd},
        )

