"""表格定义：新增一种表格 = 写一份 TableSpec，在页面上放一个 <div data-table="id">。

数据走服务层，格式（金额、百分比、正负着色）由前端 app.js 按 fmt 统一渲染，
同一份数据也给图表用（如资产分配条形图和持仓表）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from portfolio_monitor.app import App
from portfolio_monitor.entry.formatting import (
    ACCOUNT_KIND_LABELS,
    ACTOR_LABELS,
    ASSET_CLASS_LABELS,
    FIELD_LABELS,
    FLOW_ORIGIN_LABELS,
    label,
)

# fmt：text | money（美元）| local（原币，读行内 currency）| number | pct | spct（带正负号）
#      | date | datetime | link（读行内 url）| status | actions（由前端按行内 actions 渲染按钮）
NUMERIC = {"money", "local", "number", "pct", "spct"}


@dataclass(frozen=True)
class Params:
    group_by: str | None = None
    account_id: str | None = None
    as_of: dt.date | None = None


@dataclass(frozen=True)
class Col:
    field: str
    title: str
    fmt: str = "text"
    color: str | None = None                       # "sign"：正绿负红
    when: Callable[[Params], bool] | None = None   # 只在满足条件时显示
    width: int | None = None                       # 固定宽度（像素）
    grow: int | None = None                        # 分到的剩余宽度倍数（长文本列用）

    def spec(self, params: Params) -> dict[str, Any]:
        title = self.title.replace("{group}", FIELD_LABELS.get(params.group_by or "", ""))
        out = {"field": self.field, "title": title, "fmt": self.fmt,
               "align": "right" if self.fmt in NUMERIC else "left"}
        if self.color:
            out["color"] = self.color
        if self.width:
            out["width"] = self.width
        if self.grow:
            out["grow"] = self.grow
        return out


@dataclass(frozen=True)
class TableSpec:
    id: str
    columns: tuple[Col, ...]
    rows: Callable[[App, Params], list[dict[str, Any]]]
    group_by_options: tuple[str, ...] = ()
    sort: tuple[str, str] | None = None            # (字段, asc | desc)
    labels: dict[str, dict[str, str]] = field(default_factory=dict)  # 字段 → 取值的中文名

    def payload(self, app: App, params: Params) -> dict[str, Any]:
        if self.group_by_options:
            if params.group_by is None:
                params = Params(self.group_by_options[0], params.account_id, params.as_of)
            elif params.group_by not in self.group_by_options:
                raise ValueError(f"{self.id} 不支持按 {params.group_by} 分组")
        rows = self.rows(app, params)
        for r in rows:
            for f, names in self.labels.items():
                if f in r and r[f] is not None:
                    r[f] = names.get(str(r[f]), str(r[f]))
        return {
            "id": self.id,
            "group_by": params.group_by,
            "group_by_options": [{"value": g, "label": FIELD_LABELS[g]} for g in self.group_by_options],
            "columns": [c.spec(params) for c in self.columns if c.when is None or c.when(params)],
            "sort": list(self.sort) if self.sort else None,
            "rows": rows,
        }


# ---------- 各表的数据 ----------

def _holdings(app: App, p: Params) -> list[dict[str, Any]]:
    filters = {"account_id": p.account_id} if p.account_id else None
    rows = app.query.holdings(p.as_of, group_by=[p.group_by], filters=filters)
    for r in rows:
        r["group"] = label(p.group_by, r[p.group_by], r)
    return rows


def _accounts(app: App, p: Params) -> list[dict[str, Any]]:
    rows = []
    for a in app.query.accounts(p.as_of):
        r = a.model_dump()
        r["url"] = f"/accounts/{a.id}"
        r["returns"] = "计入" if a.include_in_returns else "不计入"
        r["status"] = "stale" if a.stale else "ok"
        r["source_label"] = "手动" if a.editable else a.source
        rows.append(r)
    return rows


def _positions(app: App, p: Params) -> list[dict[str, Any]]:
    account = app.repo.get_account(p.account_id or "")
    if account is None:
        raise LookupError(f"没有这个账户：{p.account_id}")
    rows = app.query.account_rows(account.id, p.as_of)
    editable = account.source == "manual" and account.active
    for r in rows:
        is_cash = r["row_type"] == "cash"
        r["asset_class_value"] = r["asset_class"]  # 编辑表单回填用原值，asset_class 会被换成中文名
        r["display_name"] = r["name"] if is_cash else (r.get("name") or r["instrument_id"])
        if editable:
            r["actions"] = ["edit_cash"] if is_cash else ["edit_holding", "remove_holding"]
    return rows


def _flows(app: App, p: Params) -> list[dict[str, Any]]:
    return app.query.account_flows(p.account_id or "")


def _monthly(app: App, p: Params) -> list[dict[str, Any]]:
    return [m.model_dump() for m in app.query.monthly_returns(p.as_of)]


def _changes(app: App, p: Params) -> list[dict[str, Any]]:
    rows = []
    for c in app.edit.list(account_id=p.account_id):
        r = c.model_dump()
        r["url"] = f"/accounts/{c.account_id}"
        r["actions"] = ["undo"] if c.can_undo else []
        if c.undo_blocked_by:
            r["note"] = f"需先撤销 #{c.undo_blocked_by}"
        rows.append(r)
    return rows


def _sync_runs(app: App, p: Params) -> list[dict[str, Any]]:
    return app.query.sync_history()


def _is(group: str) -> Callable[[Params], bool]:
    return lambda p: p.group_by == group


def _is_not(group: str) -> Callable[[Params], bool]:
    return lambda p: p.group_by != group


HOLDINGS = TableSpec(
    id="holdings",
    group_by_options=("asset_class", "instrument_id", "account_id", "currency", "region"),
    rows=_holdings,
    sort=("market_value_usd", "desc"),
    columns=(
        Col("name", "名称", when=_is("instrument_id")),
        Col("instrument_id", "编号", when=_is("instrument_id")),
        Col("group", "{group}", when=_is_not("instrument_id")),
        Col("market_value_usd", "市值", "money"),
        Col("weight", "占比", "pct"),
        Col("cost_basis_usd", "成本", "money"),
        Col("unrealized_pnl_usd", "浮盈", "money", color="sign"),
    ),
)

ACCOUNTS = TableSpec(
    id="accounts",
    rows=_accounts,
    sort=("value_usd", "desc"),
    labels={"kind": ACCOUNT_KIND_LABELS},
    columns=(
        Col("name", "账户", "link"),
        Col("kind", "类型"),
        Col("source_label", "数据来源"),
        Col("currency", "币种"),
        Col("returns", "收益率"),
        Col("value_usd", "市值", "money"),
        Col("weight", "占比", "pct"),
        Col("status", "数据", "status"),
    ),
)

POSITIONS = TableSpec(
    id="positions",
    rows=_positions,
    sort=("market_value_usd", "desc"),
    labels={"asset_class": ASSET_CLASS_LABELS},
    columns=(
        Col("display_name", "名称", grow=2),
        Col("instrument_id", "编号"),
        Col("asset_class", "类别"),
        Col("quantity", "数量", "number"),
        Col("price", "价格", "local"),
        Col("market_value", "原币市值", "local"),
        Col("market_value_usd", "市值", "money"),
        Col("cost_price", "成本价", "local"),
        Col("unrealized_pnl_usd", "浮盈", "money", color="sign"),
        Col("weight", "占比", "pct"),
        Col("actions", "", "actions", width=110),
    ),
)

FLOWS = TableSpec(
    id="flows",
    rows=_flows,
    labels={"origin": FLOW_ORIGIN_LABELS},
    columns=(
        Col("date", "日期", "date"),
        Col("currency", "币种"),
        Col("amount", "金额", "local", color="sign"),
        Col("origin", "来源"),
        Col("note", "备注", grow=2),
    ),
)

MONTHLY = TableSpec(
    id="monthly",
    rows=_monthly,
    columns=(
        Col("month", "月份"),
        Col("twr", "收益率（TWR）", "spct", color="sign"),
        Col("net_flow", "净入金", "money"),
        Col("nav_end", "月末投资资产", "money"),
    ),
)

CHANGES = TableSpec(
    id="changes",
    rows=_changes,
    labels={"actor": ACTOR_LABELS},
    columns=(
        Col("id", "#", "number", width=60),
        Col("created_at", "时间", "datetime"),
        Col("account_name", "账户", "link"),
        Col("summary", "内容", grow=5),
        Col("actor", "来源"),
        Col("status_label", "状态"),
        Col("note", "备注"),
        Col("actions", "", "actions", width=70),
    ),
)

SYNC_RUNS = TableSpec(
    id="sync_runs",
    rows=_sync_runs,
    columns=(
        Col("as_of", "估值日", "date"),
        Col("source", "数据源"),
        Col("status", "状态", "status"),
        Col("n_positions", "持仓", "number"),
        Col("n_cash", "现金", "number"),
        Col("n_flows", "新流水", "number"),
        Col("error", "错误", grow=4),
        Col("started_at", "开始", "datetime"),
    ),
)

TABLES = {t.id: t for t in (HOLDINGS, ACCOUNTS, POSITIONS, FLOWS, MONTHLY, CHANGES, SYNC_RUNS)}
