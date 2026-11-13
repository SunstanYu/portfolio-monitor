"""统一标的编号：<市场>:<代码>。

    US:NVDA   US:BRK.B
    HK:00700  港股补足 5 位
    CN:600519 A 股 6 位，不区分沪深（代码本身不重复）
    SG:D05    新加坡交易所，字母数字 2–5 位
    CRYPTO:BTC

各数据源在接入层调用 normalize() 把自己的写法转成统一编号；
识别不了的用 unknown_id() 生成 UNKNOWN:<数据源>:<原始写法>，计入资产但登记为待处理。

无市场提示时的推断规则（有提示时以提示为准）：
    带 .HK 后缀 → HK；带 .SS / .SH / .SZ / .BJ 后缀 → CN；带 .SI 后缀 → SG
    纯数字 1–5 位 → HK；6 位 → CN
    字母代码 → US（加密货币必须由数据源给出 CRYPTO 提示）
"""

from __future__ import annotations

import re

MARKETS = ("US", "HK", "CN", "SG", "CRYPTO")
MARKET_CURRENCY = {"US": "USD", "HK": "HKD", "CN": "CNY", "SG": "SGD", "CRYPTO": "USD"}
UNKNOWN = "UNKNOWN"
CASH = "CASH"

_US_CODE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")
_CRYPTO_CODE = re.compile(r"^[A-Z0-9]{2,10}$")
_SG_CODE = re.compile(r"^[A-Z0-9]{2,5}$")
_CN_SUFFIXES = (".SS", ".SH", ".SZ", ".BJ")


class UnresolvedInstrument(ValueError):
    """无法识别为统一编号的标的写法。"""


def normalize(raw: str, market: str | None = None) -> str:
    text = raw.strip().upper()
    if not text:
        raise UnresolvedInstrument(raw)
    if ":" in text:
        prefix, code = text.split(":", 1)
        return _canonical(prefix, code, raw)
    if text.endswith(".HK"):
        return _canonical("HK", text[:-3], raw)
    if text.endswith(".SI"):
        return _canonical("SG", text[:-3], raw)
    for suffix in _CN_SUFFIXES:
        if text.endswith(suffix):
            return _canonical("CN", text[: -len(suffix)], raw)
    if market:
        return _canonical(market.strip().upper(), text, raw)
    if text.isdigit():
        return _canonical("HK" if len(text) <= 5 else "CN", text, raw)
    return _canonical("US", text, raw)


def _canonical(market: str, code: str, raw: str) -> str:
    code = code.strip().upper()
    if market == "HK":
        if not code.isdigit() or not 1 <= len(code) <= 5:
            raise UnresolvedInstrument(raw)
        return f"HK:{int(code):05d}"
    if market == "CN":
        if not code.isdigit() or len(code) != 6:
            raise UnresolvedInstrument(raw)
        return f"CN:{code}"
    if market == "US":
        code = code.replace("-", ".").replace("/", ".").replace(" ", ".")
        if not _US_CODE.match(code):
            raise UnresolvedInstrument(raw)
        return f"US:{code}"
    if market == "SG":
        if not _SG_CODE.match(code):
            raise UnresolvedInstrument(raw)
        return f"SG:{code}"
    if market == "CRYPTO":
        if not _CRYPTO_CODE.match(code):
            raise UnresolvedInstrument(raw)
        return f"CRYPTO:{code}"
    raise UnresolvedInstrument(raw)


def unknown_id(source: str, raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip()) or "_"
    return f"{UNKNOWN}:{source}:{cleaned}"


def market_of(instrument_id: str) -> str:
    return instrument_id.split(":", 1)[0]


def is_canonical(instrument_id: str) -> bool:
    """统一编号（含 UNKNOWN 占位）为合法写法。"""
    if instrument_id.startswith(f"{UNKNOWN}:"):
        return instrument_id.count(":") >= 2
    try:
        return normalize(instrument_id) == instrument_id
    except UnresolvedInstrument:
        return False


def default_currency(instrument_id: str) -> str | None:
    return MARKET_CURRENCY.get(market_of(instrument_id))


def to_yahoo(instrument_id: str) -> str | None:
    """转换为 Yahoo 行情代码；不支持的返回 None。"""
    market, _, code = instrument_id.partition(":")
    if market == "US":
        return code.replace(".", "-")
    if market == "HK":
        return f"{int(code):04d}.HK"
    if market == "CN":
        if code[0] in "69":
            return f"{code}.SS"
        if code[0] in "023":
            return f"{code}.SZ"
        if code[0] in "48":
            return f"{code}.BJ"
        return None
    if market == "SG":
        return f"{code}.SI"
    if market == "CRYPTO":
        return f"{code}-USD"
    return None
