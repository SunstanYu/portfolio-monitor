"""OKX：API v5 只读 key，只读主账户（子账户不读取）。

每次同步的请求（都在限速内）：
- 私有：account/balance（交易账户）、account/positions（合约）、asset/balances（资金账户）、
  finance/savings/balance（简单赚币）、finance/staking-defi/eth|sol/balance（ETH / SOL 质押）、
  asset/asset-valuation（OKX 自己的总估值，用于对账）、asset/deposit-history、asset/withdrawal-history
- 公开：market/index-tickers（各币种 USD 指数价）、market/history-index-candles（充提当天的日线）

OKX 只给当前余额，不能取历史：只能同步纽约时间的当天，也不支持回填；
首次同步那天的市值按晚加入账户算转入。

映射规则：
- 每个币一条持仓 CRYPTO:<币>，数量 = 交易账户 + 资金账户 + 简单赚币 + 质押；价格统一用 USD 指数价
  （没有时依次用交易账户自己的 eqUsd、质押凭证的底层币、稳定币按 1；都没有就市值记 0 并提示）
- 稳定币（USDT、USDC 等）资产类别记为现金
- 永续 / 交割合约：编号 UNKNOWN:okx:<合约>，登记待处理；市值 = 未实现盈亏。交易账户的权益已含这部分
  盈亏，从保证金币种的数量里扣掉，账户总值不变；名义价值（USD，空头为负）记在持仓的 notional，供杠杆规则用
- 杠杆（MARGIN）持仓已体现在各币种权益和负债里，不单独列；期权暂不支持，出现时提示
- 资金进出：已完成的充值（入）和提现（出），按纽约日期记；金额按当天（UTC）USD 日线收盘价折算，
  当天的用当前指数价；提现手续费不算出金（算亏损）；按充值 / 提现编号去重
- 对账：持仓合计与 asset-valuation 的总估值相差超过 reconcile_tolerance 时提示
- 简单赚币、质押、估值接口读取失败只提示，不让整个同步失败

配置 [sources.okx]：
    host = "www.okx.com"        美国站 us.okx.com，欧洲站 eea.okx.com（按 key 所在的站点）
    flow_lookback_days = 30     每次同步回看几天的充提记录
    reconcile_tolerance = 0.01  对账允许的偏差
凭据（环境变量）：OKX_API_KEY、OKX_API_SECRET、OKX_API_PASSPHRASE
（.env 里含 $ 的值要用单引号括起来，否则会被当成变量展开）
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import re
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from portfolio_monitor import __version__
from portfolio_monitor.domain.instruments import UnresolvedInstrument, normalize, unknown_id
from portfolio_monitor.domain.models import (
    Account,
    AssetClass,
    CashFlow,
    Instrument,
    Position,
    SourceSnapshot,
    UnresolvedSymbol,
)
from portfolio_monitor.sources.base import SourceContext, SourceError

KEY_ENV, SECRET_ENV, PASSPHRASE_ENV = "OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"
NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

STABLECOINS = frozenset({"USDT", "USDC", "USDG", "DAI", "FDUSD", "PYUSD", "TUSD"})
_ALIASES = {"BETH": "ETH", "OKSOL": "SOL"}  # 质押凭证 → 底层币（指数价缺失时用）
_DERIVATIVES = {"SWAP", "FUTURES"}

CLOCK_SKEW = "50102"
RATE_LIMITED = "50011"
DONE = "2"  # 充值 / 提现的完成状态
PAGE = 100

# 存档前删掉：地址、交易哈希、对方账户（邮箱 / 手机号）、备注、uid、IP
_PRIVATE_FIELDS = frozenset({
    "from", "to", "txId", "areaCodeFrom", "areaCodeTo", "clientId", "note", "addrEx", "tag",
    "pmtId", "memo", "fromWdId", "uid", "mainUid", "ip",
})
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


class OkxError(SourceError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"OKX 错误 {code}：{_IP.sub('<IP>', message)}")


# ---------- API v5 ----------

class OkxClient:
    """签名、重试；返回各接口的 data 列表，不解析内容。"""

    def __init__(
        self,
        key: str,
        secret: str,
        passphrase: str,
        *,
        host: str = "www.okx.com",
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.key, self.secret, self.passphrase = key, secret, passphrase
        self.base = "https://" + host.removeprefix("https://").removeprefix("http://").rstrip("/")
        self.client = client or httpx.Client(timeout=30)
        self.sleep = sleep
        self.now = now or (lambda: dt.datetime.now(UTC))
        self.offset = dt.timedelta(0)  # 服务器时间 - 本机时间（收到 50102 后校准）

    def server_now(self) -> dt.datetime:
        return self.now() + self.offset

    def private(self, path: str, **params: Any) -> list[dict[str, Any]]:
        return self._get(path, params, signed=True)

    def public(self, path: str, **params: Any) -> list[Any]:
        return self._get(path, params, signed=False)

    def _timestamp(self) -> str:
        t = self.server_now().astimezone(UTC)
        return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"

    def _headers(self, target: str) -> dict[str, str]:
        ts = self._timestamp()
        mac = hmac.new(self.secret.encode(), f"{ts}GET{target}".encode(), hashlib.sha256)
        return {
            "OK-ACCESS-KEY": self.key,
            "OK-ACCESS-SIGN": base64.b64encode(mac.digest()).decode(),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
        }

    def _get(self, path: str, params: Mapping[str, Any], *, signed: bool) -> list[Any]:
        query = urlencode({k: v for k, v in params.items() if v is not None})
        target = f"{path}?{query}" if query else path  # 签名用的路径必须和实际请求一致
        synced = False
        for attempt in range(4):
            headers = {"User-Agent": f"portfolio-monitor/{__version__}", "Content-Type": "application/json"}
            if signed:
                headers |= self._headers(target)
            try:
                r = self.client.get(self.base + target, headers=headers)
            except httpx.HTTPError as e:
                raise SourceError(f"OKX {path} 请求失败：{type(e).__name__}") from None
            try:
                body = r.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                raise SourceError(f"OKX {path} 返回 HTTP {r.status_code}（不是 JSON）")
            code = str(body.get("code", ""))
            if code == "0":
                return body.get("data") or []
            if code == CLOCK_SKEW and signed and not synced:
                self._sync_clock()
                synced = True
                continue
            if (code == RATE_LIMITED or r.status_code == 429) and attempt < 3:
                self.sleep(1.0 * (attempt + 1))
                continue
            raise OkxError(code or f"HTTP {r.status_code}", str(body.get("msg") or ""))
        raise AssertionError("unreachable")

    def _sync_clock(self) -> None:
        data = self.public("/api/v5/public/time")
        server = dt.datetime.fromtimestamp(int(data[0]["ts"]) / 1000, UTC)
        self.offset = server - self.now()


# ---------- 解析 ----------

def _f(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SourceError(f"OKX 数值格式无法识别：{value!r}") from None


def _ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def _utc(ms: Any) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(ms) / 1000, UTC)


def index_prices(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """index-tickers（quoteCcy=USD）→ {币: USD 价格}。"""
    out: dict[str, float] = {}
    for r in rows:
        base, _, quote = str(r.get("instId") or "").partition("-")
        if quote == "USD" and r.get("idxPx"):
            out[base.upper()] = _f(r["idxPx"])
    return out


def own_prices(balance: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """交易账户自己给的 USD 价格（eqUsd / eq），作为指数价缺失时的备用。"""
    out: dict[str, float] = {}
    for d in (balance[0] if balance else {}).get("details", []):
        eq, usd = _f(d.get("eq")), _f(d.get("eqUsd"))
        if eq and usd:
            out[str(d["ccy"]).upper()] = usd / eq
    return out


def price_of(ccy: str, index: Mapping[str, float], own: Mapping[str, float]) -> float | None:
    for candidate in (index.get(ccy), own.get(ccy), index.get(_ALIASES.get(ccy, ""))):
        if candidate:
            return candidate
    return 1.0 if ccy in STABLECOINS else None


def coin_instrument(ccy: str) -> tuple[Instrument, bool]:
    """币 → (标的资料, 是否需要登记待处理)。"""
    asset_class = AssetClass.CASH if ccy in STABLECOINS else AssetClass.CRYPTO
    try:
        iid, pending = normalize(ccy, "CRYPTO"), False
    except UnresolvedInstrument:
        iid, pending = unknown_id("okx", ccy), True
    return Instrument(id=iid, name=ccy, asset_class=asset_class, region="GLOBAL", currency="USD"), pending


def parse_holdings(
    account_id: str, data: Mapping[str, Any], index: Mapping[str, float]
) -> tuple[list[Position], list[Instrument], list[UnresolvedSymbol], list[str]]:
    """各钱包余额 + 合约 → 持仓。data 的键：balance、positions、funding、savings、staking。"""
    own = own_prices(data.get("balance") or [])
    quantity: dict[str, float] = defaultdict(float)
    balance = (data.get("balance") or [{}])[0]
    for d in balance.get("details", []):
        quantity[str(d["ccy"]).upper()] += _f(d.get("eq"))
    for rows, field in ((data.get("funding"), "bal"), (data.get("savings"), "amt"), (data.get("staking"), "amt")):
        for r in rows or []:
            if r.get("ccy"):
                quantity[str(r["ccy"]).upper()] += _f(r.get(field))

    warnings: list[str] = []
    instruments: dict[str, Instrument] = {}
    unresolved: list[UnresolvedSymbol] = []
    derivatives: dict[str, dict[str, Any]] = {}
    for p in data.get("positions") or []:
        kind, inst_id = str(p.get("instType") or "").upper(), str(p.get("instId") or "")
        if kind == "MARGIN":
            continue  # 已体现在各币种权益和负债里
        if kind not in _DERIVATIVES:
            warnings.append(f"暂不支持 {kind or '未知类型'} 持仓 {inst_id}，没有单独列出")
            continue
        margin_ccy = str(p.get("ccy") or "").upper()
        if not margin_ccy:
            warnings.append(f"{inst_id} 没有保证金币种，没有单独列出")
            continue
        upl = _f(p.get("upl"))
        quantity[margin_ccy] -= upl  # 权益已含未实现盈亏，挪到合约上，避免重复
        price = price_of(margin_ccy, index, own)
        if price is None:
            warnings.append(f"{inst_id} 的保证金币种 {margin_ccy} 没有 USD 价格，盈亏按 0 计")
        iid = unknown_id("okx", inst_id)
        sample = {k: p.get(k) for k in ("instType", "instId", "mgnMode", "posSide") if p.get(k)}
        instruments[iid] = Instrument(id=iid, name=inst_id, asset_class=AssetClass.OTHER, region="GLOBAL",
                                      currency="USD", extra={"okx": sample})
        m = derivatives.setdefault(iid, {"quantity": 0.0, "value": 0.0, "notional": 0.0, "raw": inst_id,
                                         "sample": sample})
        pos = _f(p.get("pos"))
        m["quantity"] += pos
        m["value"] += upl * (price or 0.0)
        m["notional"] += abs(_f(p.get("notionalUsd"))) * (-1 if pos < 0 else 1)  # 空头记负

    positions: list[Position] = []
    for ccy in sorted(quantity):
        q = quantity[ccy]
        if abs(q) < 1e-12:
            continue
        inst, pending = coin_instrument(ccy)
        instruments[inst.id] = inst
        if pending:
            unresolved.append(UnresolvedSymbol(account_id=account_id, raw_symbol=ccy, sample={"ccy": ccy}))
        price = price_of(ccy, index, own)
        if price is None:
            warnings.append(f"{ccy} 没有 USD 价格，市值按 0 计")
        positions.append(Position(account_id=account_id, instrument_id=inst.id, quantity=q, price=price,
                                  market_value=q * (price or 0.0), currency="USD"))
    for iid, m in derivatives.items():
        positions.append(Position(account_id=account_id, instrument_id=iid, quantity=m["quantity"],
                                  market_value=m["value"], currency="USD", notional=m["notional"]))
        unresolved.append(UnresolvedSymbol(account_id=account_id, raw_symbol=m["raw"], sample=m["sample"]))
    return positions, list(instruments.values()), unresolved, warnings


def reconcile(total: float, valuation: Sequence[Mapping[str, Any]], tolerance: float) -> str | None:
    """持仓合计与 OKX 自己的总估值（USD）对账。"""
    theirs = _f((valuation[0] if valuation else {}).get("totalBal"))
    if theirs <= 0:
        return "OKX 资产估值为 0，无法对账" if abs(total) > 1 else None
    diff = total / theirs - 1
    if abs(diff) > tolerance:
        return f"持仓合计与 OKX 资产估值相差 {diff:+.1%}，可能有没读取的产品（如链上赚币、双币赢）"
    return None


def scrub(value: Any) -> Any:
    """存档前递归删掉 _PRIVATE_FIELDS。"""
    if isinstance(value, Mapping):
        return {k: scrub(v) for k, v in value.items() if k not in _PRIVATE_FIELDS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


# ---------- 数据源 ----------

class OkxSource:
    name = "okx"

    def __init__(self, ctx: SourceContext, *, client: OkxClient | None = None) -> None:
        s = ctx.settings
        self.lookback = int(s.get("flow_lookback_days", 30))
        self.tolerance = float(s.get("reconcile_tolerance", 0.01))
        self._client = client
        if client is None:
            key, secret, passphrase = (ctx.env.get(n, "").strip() for n in (KEY_ENV, SECRET_ENV, PASSPHRASE_ENV))
            if key and secret and passphrase:
                self._client = OkxClient(key, secret, passphrase, host=str(s.get("host", "www.okx.com")))

    @property
    def client(self) -> OkxClient:
        if self._client is None:
            raise SourceError(f"缺少环境变量 {KEY_ENV} / {SECRET_ENV} / {PASSPHRASE_ENV}（见 .env.example）")
        return self._client

    def _optional(self, label: str, path: str, warnings: list[str], **params: Any) -> list[dict[str, Any]]:
        try:
            return self.client.private(path, **params)
        except OkxError as e:
            warnings.append(f"读取{label}失败（{e}），没有计入")
            return []

    def _history(self, path: str, start_ms: int) -> list[dict[str, Any]]:
        """按时间倒序翻页，取到 start_ms 为止。"""
        rows: list[dict[str, Any]] = []
        after = None
        while True:
            page = self.client.private(path, limit=PAGE, after=after)
            rows += page
            if len(page) < PAGE:
                break
            oldest = min(int(r["ts"]) for r in page)
            if oldest < start_ms:
                break
            after = oldest
        return [r for r in rows if int(r.get("ts") or 0) >= start_ms]

    def _flow_price(self, ccy: str, moment: dt.datetime, index: Mapping[str, float],
                    own: Mapping[str, float], cache: dict[tuple[str, dt.date], float | None]) -> float | None:
        """充提当天（UTC）的 USD 日线收盘价；当天的用当前指数价。"""
        day = moment.astimezone(UTC).date()
        if day >= self.client.server_now().astimezone(UTC).date():
            return price_of(ccy, index, own)
        key = (ccy, day)
        if key not in cache:
            cache[key] = None
            nxt = _ms(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), UTC))
            for base in dict.fromkeys((ccy, _ALIASES.get(ccy, ccy))):
                rows = self.client.public("/api/v5/market/history-index-candles", instId=f"{base}-USD",
                                          bar="1Dutc", after=nxt, limit=1)
                if rows and _utc(rows[0][0]).date() == day:
                    cache[key] = _f(rows[0][4])
                    break
            if cache[key] is None and ccy in STABLECOINS:
                cache[key] = 1.0
        return cache[key]

    def _flows(self, account_id: str, deposits: Sequence[Mapping[str, Any]],
               withdrawals: Sequence[Mapping[str, Any]], index: Mapping[str, float],
               own: Mapping[str, float], warnings: list[str]) -> list[CashFlow]:
        flows: list[CashFlow] = []
        cache: dict[tuple[str, dt.date], float | None] = {}
        for rows, sign, id_field, prefix, label in ((deposits, 1, "depId", "dep", "充值"),
                                                    (withdrawals, -1, "wdId", "wd", "提现")):
            for r in rows:
                if str(r.get("state")) != DONE:
                    continue
                ccy, amount, moment = str(r["ccy"]).upper(), _f(r.get("amt")), _utc(r["ts"])
                price = self._flow_price(ccy, moment, index, own, cache)
                if price is None:
                    warnings.append(f"{moment.astimezone(NY).date()} 的{label} {ccy} 没有 USD 价格，没有记入资金进出")
                    continue
                flows.append(CashFlow(date=moment.astimezone(NY).date(), account_id=account_id, currency="USD",
                                      amount=sign * amount * price, external_id=f"okx:{prefix}:{r[id_field]}",
                                      note=f"OKX {label} {ccy}"))
        return sorted(flows, key=lambda f: (f.date, f.external_id or ""))

    def fetch(self, as_of: dt.date, accounts: Sequence[Account]) -> SourceSnapshot:
        if not accounts:
            return SourceSnapshot(source=self.name, as_of=as_of)
        if len(accounts) > 1:
            raise SourceError("OKX 只读主账户，只能对应一个账户")
        today = self.client.server_now().astimezone(NY).date()
        if as_of != today:
            raise SourceError(f"OKX 只能取当前余额，不能同步 {as_of}（纽约今天是 {today}）")
        account_id = accounts[0].id
        c = self.client
        warnings: list[str] = []
        data: dict[str, Any] = {
            "balance": c.private("/api/v5/account/balance"),
            "positions": c.private("/api/v5/account/positions"),
            "funding": c.private("/api/v5/asset/balances"),
            "savings": self._optional("简单赚币", "/api/v5/finance/savings/balance", warnings),
            "staking": (self._optional("ETH 质押", "/api/v5/finance/staking-defi/eth/balance", warnings)
                        + self._optional("SOL 质押", "/api/v5/finance/staking-defi/sol/balance", warnings)),
            "valuation": self._optional("资产估值", "/api/v5/asset/asset-valuation", warnings, ccy="USD"),
        }
        index = index_prices(c.public("/api/v5/market/index-tickers", quoteCcy="USD"))
        positions, instruments, unresolved, more = parse_holdings(account_id, data, index)
        warnings += more
        if data["valuation"] and (w := reconcile(sum(p.market_value for p in positions),
                                                 data["valuation"], self.tolerance)):
            warnings.append(w)

        start = dt.datetime.combine(as_of - dt.timedelta(days=self.lookback), dt.time(), NY)
        deposits = self._history("/api/v5/asset/deposit-history", _ms(start))
        withdrawals = self._history("/api/v5/asset/withdrawal-history", _ms(start))
        own = own_prices(data["balance"])
        flows = self._flows(account_id, deposits, withdrawals, index, own, warnings)

        held = {i.name for i in instruments if i.name}
        raw = scrub({**data, "deposits": deposits, "withdrawals": withdrawals,
                     "prices": {k: v for k, v in sorted(index.items()) if k in held}})
        return SourceSnapshot(
            source=self.name, as_of=as_of, positions=positions, flows=[f for f in flows if f.date <= as_of],
            instruments=instruments, unresolved=unresolved, warnings=warnings, raw=raw,
        )
