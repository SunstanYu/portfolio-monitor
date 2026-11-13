"""IBKR：Flex Web Service（只读 token + 保存好的 Flex Query）。

取数分两步：SendRequest（token、query id、日期区间）拿到 ReferenceCode → GetStatement 取 XML。
报表没生成好时对方返回错误码，等待后重试；限流为每个 token 每秒 1 次、每分钟 10 次。

IBKR 当天的报表次日才能取到（当天请求返回 1003）。截止日拿不到时往前退，最多退
max_lag_days 天；快照的 data_date 记录数据实际所属日期，由同步服务判断是否标记为沿用。

Flex Query 需要勾选：Open Positions、Cash Report、Cash Transactions、Transfers、
Net Asset Value (NAV) in Base。Account Information 不需要（含个人信息，存档前也会删掉）。

映射规则：
- 持仓：OpenPositions 的汇总行（SUMMARY）。股票 / 基金按交易所和币种归入 US / HK / CN；
  加密货币归入 CRYPTO；其他市场和衍生品记为 UNKNOWN 并登记待处理（市值照常计入）
- 现金：CashReport 各币种的 endingCash（不含 BASE_SUMMARY）
- 资金进出：CashTransactions 里的 Deposits/Withdrawals，以及 Transfers（转入为正、转出为负）；
  股息、利息、预扣税、费用属于收益，不算资金进出
- 历史：EquitySummaryByReportDateInBase 的每日 total（账户基础货币）
- 离岸人民币 CNH 按 CNY 计（欧洲央行没有 CNH 汇率，两者差异很小）

配置 [sources.ibkr]：
    accounts = { "U1234567" = "ibkr" }   Flex 账户号 → 本系统账户 id；只有一个账户时可省略
    flow_lookback_days = 7               每日同步回看几天的资金进出（补上漏跑的日子）
    max_lag_days = 4                     截止日没有数据时最多往前退几天
凭据（环境变量）：IBKR_FLEX_TOKEN、IBKR_FLEX_QUERY_ID
"""

from __future__ import annotations

import datetime as dt
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import httpx

from portfolio_monitor import __version__
from portfolio_monitor.domain.instruments import UnresolvedInstrument, normalize, unknown_id
from portfolio_monitor.domain.models import (
    Account,
    AccountValue,
    AssetClass,
    CashBalance,
    CashFlow,
    Instrument,
    Position,
    SourceHistory,
    SourceSnapshot,
    UnresolvedSymbol,
)
from portfolio_monitor.sources.base import SourceContext, SourceError

FLEX_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
TOKEN_ENV = "IBKR_FLEX_TOKEN"
QUERY_ENV = "IBKR_FLEX_QUERY_ID"
MAX_DAYS = 365  # Flex 单次最多取 365 天

NOT_AVAILABLE = "1003"
# 稍后重试可能成功的错误码：报表生成中、数据未就绪、服务繁忙、请求过多
RETRYABLE = {"1001", "1004", "1005", "1006", "1007", "1008", "1009", "1018", "1019", "1021"}

_HK_EXCHANGES = {"SEHK"}
_CN_EXCHANGES = {"SEHKNTL", "SEHKSZSE", "SSE", "SZSE"}  # 沪深港通北向
_EQUITY_CATEGORIES = {"STK", "FUND"}
_ASSET_CLASS = {
    "STK": AssetClass.EQUITY,
    "FUND": AssetClass.FUND,
    "BOND": AssetClass.BOND,
    "BILL": AssetClass.BOND,
    "OPT": AssetClass.OPTION,
    "FOP": AssetClass.OPTION,
    "WAR": AssetClass.OPTION,
    "CRYPTO": AssetClass.CRYPTO,
}
_FLOW_CASH_TYPES = {"Deposits/Withdrawals"}


class FlexError(SourceError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"IBKR Flex 错误 {code}：{message}")


# ---------- Flex Web Service ----------

class FlexClient:
    """只负责把报表 XML 取回来；不解析内容。"""

    def __init__(
        self,
        token: str,
        query_id: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = 6.0,  # 每分钟最多 10 次请求
        max_polls: int = 20,
    ) -> None:
        self.token = token
        self.query_id = query_id
        self.client = client or httpx.Client(timeout=120)
        self.sleep = sleep
        self.poll_interval = poll_interval
        self.max_polls = max_polls

    def _get(self, path: str, params: Mapping[str, str]) -> bytes:
        try:
            r = self.client.get(
                f"{FLEX_URL}/{path}",
                params={"t": self.token, "v": "3", **params},
                headers={"User-Agent": f"portfolio-monitor/{__version__}"},
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # 不带 URL：URL 里有 token
            raise SourceError(f"IBKR Flex {path} 返回 HTTP {e.response.status_code}") from None
        except httpx.HTTPError as e:
            raise SourceError(f"IBKR Flex {path} 请求失败：{type(e).__name__}") from None
        return r.content

    def fetch(self, start: dt.date, end: dt.date) -> bytes:
        """取 [start, end] 的报表 XML（覆盖 Flex Query 里保存的日期设置）。"""
        params = {"q": self.query_id, "fd": f"{start:%Y%m%d}", "td": f"{end:%Y%m%d}"}
        reference = self._send(params)
        for _ in range(self.max_polls):
            self.sleep(self.poll_interval)
            body = self._get("GetStatement", {"q": reference})
            error = _response_error(body)
            if error is None:
                return body
            if error.code not in RETRYABLE:
                raise error
        raise SourceError(f"IBKR 报表等了 {self.max_polls * self.poll_interval:.0f} 秒仍未生成")

    def _send(self, params: Mapping[str, str]) -> str:
        for attempt in range(3):
            body = self._get("SendRequest", params)
            error = _response_error(body)
            if error is None:
                reference = ET.fromstring(body).findtext("ReferenceCode")
                if not reference:
                    raise SourceError("IBKR SendRequest 没有返回 ReferenceCode")
                return reference
            if error.code not in RETRYABLE or attempt == 2:
                raise error
            self.sleep(self.poll_interval * (attempt + 2))
        raise AssertionError("unreachable")


def _response_error(body: bytes) -> FlexError | None:
    """Flex 的状态响应：Success 返回 None，失败返回 FlexError；报表正文也返回 None。"""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise SourceError("IBKR 返回的不是 XML") from None
    if root.tag != "FlexStatementResponse" or root.findtext("Status") == "Success":
        return None
    return FlexError(root.findtext("ErrorCode") or "?", root.findtext("ErrorMessage") or "")


# ---------- 解析 ----------

def parse_date(text: str | None) -> dt.date | None:
    """Flex 日期：20260302、2026-03-02、20260302;153000、2026-03-02 15:30:00 等写法。"""
    if not text or not text.strip():
        return None
    head = re.split(r"[;,T ]", text.strip())[0].replace("-", "").replace("/", "")
    try:
        return dt.datetime.strptime(head[:8], "%Y%m%d").date()
    except ValueError:
        raise SourceError(f"IBKR 日期格式无法识别：{text!r}") from None


def _num(el: ET.Element, attr: str) -> float:
    text = (el.get(attr) or "").replace(",", "").strip()
    return float(text) if text else 0.0


def _ccy(code: str | None) -> str:
    code = (code or "").strip().upper()
    return "CNY" if code == "CNH" else code


def _rows(statement: ET.Element, section: str) -> list[ET.Element]:
    found = statement.find(section)  # Element 不能直接做真假判断（无子节点时为假，且有弃用警告）
    return list(found) if found is not None else []


def _detail(rows: Iterable[ET.Element]) -> list[ET.Element]:
    """去掉汇总行（levelOfDetail=SUMMARY），没有这个属性的行保留。"""
    return [r for r in rows if (r.get("levelOfDetail") or "DETAIL").upper() != "SUMMARY"]


def parse_statements(xml: bytes | str) -> list[ET.Element]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise SourceError("IBKR 报表不是合法的 XML") from None
    statements = root.findall(".//FlexStatement")
    if not statements:
        raise SourceError("IBKR 报表里没有 FlexStatement")
    return statements


def strip_personal_info(xml: bytes | str) -> str:
    """存档前删掉 AccountInformation（姓名、地址、生日、邮箱等）。"""
    root = ET.fromstring(xml)
    for statement in root.iter("FlexStatement"):
        for info in statement.findall("AccountInformation"):
            statement.remove(info)
    return ET.tostring(root, encoding="unicode")


def instrument_of(el: ET.Element) -> tuple[Instrument, UnresolvedSymbol | None, str]:
    """OpenPosition → (标的资料, 待处理登记或 None, 币种)。"""
    category = (el.get("assetCategory") or "").upper()
    currency = _ccy(el.get("currency"))
    exchange = (el.get("listingExchange") or "").upper()
    symbol = (el.get("symbol") or "").strip()
    asset_class = _ASSET_CLASS.get(category, AssetClass.OTHER)
    if category == "STK" and (el.get("subCategory") or "").upper() == "ETF":
        asset_class = AssetClass.ETF

    market = None
    if category == "CRYPTO":
        market = "CRYPTO"
    elif category in _EQUITY_CATEGORIES:
        if exchange in _CN_EXCHANGES or currency == "CNY":
            market = "CN"
        elif exchange in _HK_EXCHANGES or currency == "HKD":
            market = "HK"
        elif currency == "USD":
            market = "US"

    name = el.get("description") or symbol or None
    if market is not None:
        try:
            iid = normalize(symbol, market)
            region = "GLOBAL" if market == "CRYPTO" else market
            return Instrument(id=iid, name=name, asset_class=asset_class, region=region,
                              currency=currency), None, currency
        except UnresolvedInstrument:
            pass

    raw = f"{category or 'X'}-{el.get('conid') or symbol}"
    sample = {k: el.get(k) for k in ("assetCategory", "symbol", "description", "currency",
                                     "listingExchange", "conid") if el.get(k)}
    inst = Instrument(id=unknown_id("ibkr", raw), name=name, asset_class=asset_class,
                      currency=currency, extra={"ibkr": sample})
    return inst, UnresolvedSymbol(account_id="", raw_symbol=symbol or raw, sample=sample), currency


def _flow(el: ET.Element, account_id: str, amount: float, note: str, date_attrs: Sequence[str]) -> CashFlow | None:
    day = next((d for d in (parse_date(el.get(a)) for a in date_attrs) if d), None)
    if day is None or amount == 0:
        return None
    txn = el.get("transactionID") or el.get("tradeID") or el.get("actionID")
    external = f"ibkr:{txn}" if txn else (
        f"ibkr:{account_id}:{day:%Y%m%d}:{_ccy(el.get('currency'))}:{amount:.2f}"
    )
    return CashFlow(date=day, account_id=account_id, currency=_ccy(el.get("currency")),
                    amount=amount, external_id=external, note=note)


def parse_flows(statement: ET.Element, account_id: str) -> list[CashFlow]:
    """资金进出。

    - 现金流水按 reportDate 记日期：IBKR 的每日净值从 reportDate 起才包含这笔钱
      （实测一笔入金 dateTime 是 1 月 7 日、reportDate 是 1 月 8 日，按 dateTime 记会造成一正一负的假收益）
    - 备注用固定写法：IBKR 的 description 里可能有姓名（如 "DISBURSEMENT INITIATED BY <姓名>"）
    """
    flows: list[CashFlow] = []
    for el in _detail(_rows(statement, "CashTransactions")):
        if el.get("type") in _FLOW_CASH_TYPES:
            amount = _num(el, "amount")
            note = "IBKR 入金" if amount > 0 else "IBKR 出金"
            if f := _flow(el, account_id, amount, note, ("reportDate", "dateTime", "settleDate")):
                flows.append(f)
    for el in _detail(_rows(statement, "Transfers")):
        value = next((v for v in (_num(el, "cashTransfer"), _num(el, "positionAmount"),
                                  _num(el, "quantity") * _num(el, "transferPrice")) if v), 0.0)
        out = (el.get("direction") or "").upper() == "OUT"
        note = f"IBKR 转{'出' if out else '入'} {el.get('type') or ''} {el.get('symbol') or ''}".strip()
        amount = -abs(value) if out else abs(value)
        if f := _flow(el, account_id, amount, note, ("date", "dateTime", "reportDate")):
            flows.append(f)
    return flows


def parse_snapshot_parts(
    statement: ET.Element, account_id: str
) -> tuple[list[Position], list[CashBalance], list[Instrument], list[UnresolvedSymbol]]:
    merged: dict[str, dict[str, Any]] = {}
    instruments: dict[str, Instrument] = {}
    unresolved: dict[str, UnresolvedSymbol] = {}
    for el in _rows(statement, "OpenPositions"):
        if (el.get("levelOfDetail") or "SUMMARY").upper() != "SUMMARY":
            continue  # 只取汇总行；LOT 是按批次拆开的明细，会重复计数
        inst, pending, currency = instrument_of(el)
        instruments[inst.id] = inst
        if pending is not None:
            unresolved[inst.id] = pending.model_copy(update={"account_id": account_id})
        m = merged.setdefault(inst.id, {"quantity": 0.0, "market_value": 0.0, "cost": 0.0,
                                        "has_cost": False, "currency": currency})
        m["quantity"] += _num(el, "position")
        m["market_value"] += _num(el, "positionValue")
        if el.get("costBasisMoney"):
            m["cost"] += _num(el, "costBasisMoney")
            m["has_cost"] = True
        m["price"] = _num(el, "markPrice") if el.get("markPrice") else None

    positions = [
        Position(account_id=account_id, instrument_id=iid, quantity=m["quantity"],
                 price=m.get("price"), market_value=m["market_value"],
                 cost_basis=m["cost"] if m["has_cost"] else None, currency=m["currency"])
        for iid, m in merged.items()
    ]

    latest: dict[str, tuple[str, float]] = {}
    for el in _rows(statement, "CashReport"):
        raw_ccy = (el.get("currency") or "").upper()
        if not raw_ccy or raw_ccy == "BASE_SUMMARY":
            continue
        stamp = el.get("toDate") or el.get("reportDate") or ""
        prev = latest.get(raw_ccy)
        if prev is None or stamp >= prev[0]:
            latest[raw_ccy] = (stamp, _num(el, "endingCash"))
    cash_by_ccy: dict[str, float] = {}
    for raw_ccy, (_, amount) in latest.items():
        cash_by_ccy[_ccy(raw_ccy)] = cash_by_ccy.get(_ccy(raw_ccy), 0.0) + amount
    cash = [CashBalance(account_id=account_id, currency=c, amount=a) for c, a in sorted(cash_by_ccy.items())]
    return positions, cash, list(instruments.values()), list(unresolved.values())


def parse_history(statement: ET.Element, account_id: str) -> list[AccountValue]:
    values: dict[dt.date, AccountValue] = {}
    for el in _rows(statement, "EquitySummaryInBase"):
        day = parse_date(el.get("reportDate"))
        currency = _ccy(el.get("currency"))
        if day is None or not currency:
            continue
        values[day] = AccountValue(date=day, account_id=account_id, currency=currency,
                                   value=_num(el, "total"))
    return [values[d] for d in sorted(values)]


# ---------- 数据源 ----------

class IbkrSource:
    name = "ibkr"

    def __init__(self, ctx: SourceContext, *, client: FlexClient | None = None) -> None:
        s = ctx.settings
        self.account_map: dict[str, str] = {str(k): str(v) for k, v in dict(s.get("accounts", {})).items()}
        self.lookback = int(s.get("flow_lookback_days", 7))
        self.max_lag = int(s.get("max_lag_days", 4))
        self._client = client
        if client is None:
            token, query = ctx.env.get(TOKEN_ENV, "").strip(), ctx.env.get(QUERY_ENV, "").strip()
            if token and query:
                self._client = FlexClient(token, query)

    @property
    def client(self) -> FlexClient:
        if self._client is None:
            raise SourceError(f"缺少环境变量 {TOKEN_ENV} / {QUERY_ENV}（见 .env.example）")
        return self._client

    def _fetch_upto(self, start: dt.date, end: dt.date) -> tuple[bytes, dt.date]:
        """取 [start, end]；end 当天还没有报表（1003）时往前退，最多退 max_lag 天。"""
        last: FlexError | None = None
        for lag in range(self.max_lag + 1):
            to = end - dt.timedelta(days=lag)
            if to < start:
                break
            if lag:
                self.client.sleep(1.5)  # 每秒最多 1 次请求
            try:
                return self.client.fetch(start, to), to
            except FlexError as e:
                if e.code != NOT_AVAILABLE:
                    raise
                last = e
        raise SourceError(f"IBKR 在 {end} 及之前 {self.max_lag} 天都没有可用报表（{last}）")

    def _statements_by_account(self, xml: bytes, accounts: Sequence[Account]) -> dict[str, ET.Element]:
        """把 Flex 报表按本系统账户 id 分好；配置的账户在报表里找不到时报错。"""
        wanted = {a.id for a in accounts}
        statements = parse_statements(xml)
        mapping = dict(self.account_map)
        if not mapping and len(accounts) == 1 and len(statements) == 1:
            mapping = {statements[0].get("accountId") or "": accounts[0].id}
        out: dict[str, ET.Element] = {}
        for st in statements:
            flex_id = st.get("accountId") or ""
            target = mapping.get(flex_id)
            if target is None:
                raise SourceError(
                    f"Flex 报表里的账户 {flex_id} 没有对应的本系统账户，请在 [sources.ibkr].accounts 里配置"
                )
            if target in wanted:
                out[target] = st
        if missing := sorted(wanted - set(out)):
            raise SourceError(f"Flex 报表里没有这些账户的数据：{missing}")
        return out

    def fetch(self, as_of: dt.date, accounts: Sequence[Account]) -> SourceSnapshot:
        if not accounts:
            return SourceSnapshot(source=self.name, as_of=as_of)
        xml, to = self._fetch_upto(as_of - dt.timedelta(days=self.lookback), as_of)
        positions, cash, flows = [], [], []
        instruments: dict[str, Instrument] = {}
        unresolved = []
        data_date = to
        for account_id, st in self._statements_by_account(xml, accounts).items():
            p, c, i, u = parse_snapshot_parts(st, account_id)
            positions += p
            cash += c
            unresolved += u
            instruments.update({x.id: x for x in i})
            flows += parse_flows(st, account_id)
            data_date = min(data_date, parse_date(st.get("toDate")) or to)
        return SourceSnapshot(
            source=self.name, as_of=as_of, data_date=data_date, positions=positions, cash=cash,
            flows=[f for f in flows if f.date <= as_of], instruments=list(instruments.values()),
            unresolved=unresolved, raw=strip_personal_info(xml),
        )

    def history(self, start: dt.date, end: dt.date, accounts: Sequence[Account]) -> SourceHistory:
        if (end - start).days + 1 > MAX_DAYS:
            raise ValueError(f"IBKR 一次最多回填 {MAX_DAYS} 天")
        xml, to = self._fetch_upto(start, end)
        values, flows = [], []
        for account_id, st in self._statements_by_account(xml, accounts).items():
            values += [v for v in parse_history(st, account_id) if start <= v.date <= to]
            flows += parse_flows(st, account_id)
        return SourceHistory(source=self.name, start=start, end=to, values=values,
                             flows=[f for f in flows if f.date <= to], raw=strip_personal_info(xml))
