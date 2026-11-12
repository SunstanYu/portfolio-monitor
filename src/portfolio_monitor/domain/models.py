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


class AccountValue(_Model):
    """账户在某日的总值（只有总数、没有明细），用于回填历史净值。"""

    date: dt.date
    account_id: str
    currency: Currency
    value: Finite


class SourceSnapshot(_Model):
    """一个数据源在某个估值日返回的全部内容。

    data_date：数据实际所属日期。数据源有延迟（如 IBKR 当天的报表次日才有）时早于 as_of，
    同步时据此判断是否标记为沿用；None 表示就是 as_of。
    """

    source: str
    as_of: dt.date
    data_date: dt.date | None = None
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


class SourceHistory(_Model):
    """数据源能提供的历史：每日账户总值 + 资金进出（如 IBKR 一年回填）。"""

    source: str
    start: dt.date
    end: dt.date
    values: list[AccountValue] = Field(default_factory=list)
    flows: list[CashFlow] = Field(default_factory=list)
    raw: Any = None
