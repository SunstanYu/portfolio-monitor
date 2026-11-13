"""契约测试：每个注册的数据源都必须通过。

新增数据源时，在 CASES 里加一个构造函数（用与真实返回结构一致的虚构样本，不访问网络；
仓库是公开的，不要放真实报表）。
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable

import pytest
from flex_mock import FlexMock
from okx_mock import OkxMock
from usmart_mock import NOW as USMART_NOW
from usmart_mock import UsmartMock
from usmart_mock import env as usmart_env

from portfolio_monitor.domain.instruments import is_canonical
from portfolio_monitor.domain.models import Account, AssetClass, Instrument
from portfolio_monitor.sources.base import Source, SourceContext, validate_snapshot
from portfolio_monitor.sources.fake import FakeSource
from portfolio_monitor.sources.ibkr import IbkrSource
from portfolio_monitor.sources.manual import ManualSource
from portfolio_monitor.sources.okx import OkxSource
from portfolio_monitor.sources.usmart import UsmartSource
from portfolio_monitor.sources.registry import REGISTRY
from portfolio_monitor.storage.repository import Repository

AS_OF = dt.date(2026, 3, 2)


def _fake(repo: Repository) -> tuple[Source, list[Account]]:
    accounts = [
        Account(id="f-usd", name="F1", kind="broker", source="fake", currency="USD"),
        Account(id="f-hkd", name="F2", kind="broker", source="fake", currency="HKD"),
    ]
    repo.sync_accounts(accounts)
    return FakeSource(SourceContext(repo=repo, settings={"start": "2026-01-01"})), accounts


def _manual(repo: Repository) -> tuple[Source, list[Account]]:
    accounts = [
        Account(id="m-cn", name="M1", kind="broker", source="manual", currency="CNY"),
        Account(id="m-bank", name="M2", kind="bank", source="manual", currency="HKD",
                include_in_returns=False),
    ]
    repo.sync_accounts(accounts)
    repo.upsert_instruments([Instrument(id="CN:600036", name="X", asset_class=AssetClass.EQUITY,
                                        currency="CNY")])
    repo.set_manual_holding("m-cn", "CN:600036", 100, 40.0, "CNY", cost_basis=3800.0)
    repo.set_manual_cash("m-cn", "CNY", 1000.0)
    repo.set_manual_cash("m-bank", "HKD", 5000.0)
    return ManualSource(SourceContext(repo=repo)), accounts


def _ibkr(repo: Repository) -> tuple[Source, list[Account]]:
    accounts = [Account(id="i-main", name="I1", kind="broker", source="ibkr", currency="USD")]
    repo.sync_accounts(accounts)
    return IbkrSource(SourceContext(repo=repo), client=FlexMock(AS_OF).client()), accounts


def _okx(repo: Repository) -> tuple[Source, list[Account]]:
    accounts = [Account(id="o-main", name="O1", kind="crypto", source="okx", currency="USD")]
    repo.sync_accounts(accounts)
    return OkxSource(SourceContext(repo=repo), client=OkxMock().client()), accounts


def _usmart(repo: Repository) -> tuple[Source, list[Account]]:
    accounts = [Account(id="u-main", name="U1", kind="broker", source="usmart", currency="USD")]
    repo.sync_accounts(accounts)
    src = UsmartSource(SourceContext(repo=repo, env=usmart_env()), client=UsmartMock().client(),
                       now=lambda: USMART_NOW)
    return src, accounts


CASES: dict[str, Callable[[Repository], tuple[Source, list[Account]]]] = {
    "fake": _fake,
    "manual": _manual,
    "ibkr": _ibkr,
    "okx": _okx,
    "usmart": _usmart,
}


def test_every_registered_source_has_a_contract_case():
    assert set(REGISTRY) == set(CASES), "新数据源必须在契约测试里登记"


@pytest.mark.parametrize("name", sorted(CASES))
def test_contract(name, repo):
    source, accounts = CASES[name](repo)
    assert source.name == name
    snap = source.fetch(AS_OF, accounts)

    validate_snapshot(snap, name, AS_OF, accounts)
    assert snap.positions or snap.cash, "至少返回持仓或现金之一"
    assert all(is_canonical(i.id) for i in snap.instruments)
    json.dumps(snap.raw)  # 原始返回必须能直接存档

    # 同一天重复拉取结果一致（同步可安全重跑）
    again = source.fetch(AS_OF, accounts)
    assert again.model_dump(exclude={"raw"}) == snap.model_dump(exclude={"raw"})


@pytest.mark.parametrize("name", sorted(CASES))
def test_contract_empty_accounts(name, repo):
    source, _ = CASES[name](repo)
    snap = source.fetch(AS_OF, [])
    validate_snapshot(snap, name, AS_OF, [])
    assert not snap.positions and not snap.cash
