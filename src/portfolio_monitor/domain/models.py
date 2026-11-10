"""领域模型。只描述数据，不做任何 IO。"""

from __future__ import annotations

import datetime as dt
import math
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


class AccountKind(StrEnum):
    BROKER = "broker"
    BANK = "bank"
    CRYPTO = "crypto"
    OTHER = "other"


class AssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    FUND = "fund"
    BOND = "bond"
    CRYPTO = "crypto"
    CASH = "cash"
    OPTION = "option"
    OTHER = "other"


def _fiat(code: str) -> str:
    code = code.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError(f"币种必须是 3 位字母代码：{code!r}")
    return code


def _finite(value: float | None) -> float | None:
    if value is not None and not math.isfinite(value):
        raise ValueError("数值必须是有限数")
    return value


Currency = Annotated[str, AfterValidator(_fiat)]
Finite = Annotated[float, AfterValidator(_finite)]
OptFinite = Annotated[float | None, AfterValidator(_finite)]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Account(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str
    institution: str | None = None
    kind: AccountKind
    source: str
    currency: Currency
    include_in_returns: bool = True
    active: bool = True


class Instrument(_Model):
    id: str
    name: str | None = None
    asset_class: AssetClass
    region: str | None = None
    sector: str | None = None
    currency: Currency
    extra: dict[str, Any] = Field(default_factory=dict)


class Position(_Model):
    account_id: str
    instrument_id: str
    quantity: Finite
    price: OptFinite = None
    market_value: Finite
    cost_basis: OptFinite = None
    currency: Currency


class CashBalance(_Model):
    account_id: str
    currency: Currency
    amount: Finite


class CashFlow(_Model):
    """资金进出：入正出负。external_id 用于跨次同步去重。"""

    date: dt.date
    account_id: str
    currency: Currency
    amount: Finite
    external_id: str | None = None
    note: str | None = None


class UnresolvedSymbol(_Model):
    account_id: str
    raw_symbol: str
    sample: dict[str, Any] = Field(default_factory=dict)


class SourceSnapshot(_Model):
    """一个数据源在某个估值日返回的全部内容。"""

    source: str
    as_of: dt.date
    positions: list[Position] = Field(default_factory=list)
    cash: list[CashBalance] = Field(default_factory=list)
    flows: list[CashFlow] = Field(default_factory=list)
    instruments: list[Instrument] = Field(default_factory=list)
    unresolved: list[UnresolvedSymbol] = Field(default_factory=list)
    raw: Any = None

    @model_validator(mode="after")
    def _instruments_cover_positions(self) -> SourceSnapshot:
        known = {i.id for i in self.instruments}
        missing = sorted({p.instrument_id for p in self.positions} - known)
        if missing:
            raise ValueError(f"持仓引用了未提供资料的标的：{missing}")
        return self
