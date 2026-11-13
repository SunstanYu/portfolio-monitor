"""修改服务：手动账户的持仓、现金、资金进出。网页、JSON 接口、MCP 共用。

    preview  生成"改前 → 改后"，记为待确认（30 分钟内有效）
    confirm  写入、记日志，并刷新快照
    apply    preview + confirm，网页上点一次就生效
    undo     写回改前状态；该项之后又被改过时拒绝，提示先撤销后面那次

生效日期：修改立即反映到最新估值日（还没有任何数据时为今天）的手动账户快照和净值上。
手动账户没有盘中数据，最新估值日就代表当前状态。

资金进出 vs 直接改现金：记资金进出会同时调整现金，并在 TWR 里当作入金 / 出金剔除；
直接改现金的差额算作盈亏（如利息）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from portfolio_monitor.domain.instruments import (
    UnresolvedInstrument,
    default_currency,
    market_of,
    normalize,
)
from portfolio_monitor.domain.models import Account, AssetClass, CashFlow, Currency, Instrument
from portfolio_monitor.services.sync import SyncService
from portfolio_monitor.sources.registry import MANUAL_FLOW_SOURCES
from portfolio_monitor.storage.db import transaction, utc_now
from portfolio_monitor.storage.repository import Repository

PENDING_TTL = dt.timedelta(minutes=30)
STATUS_LABELS = {"pending": "待确认", "applied": "已生效", "undone": "已撤销", "discarded": "已作废"}
MARKET_REGION = {"US": "US", "HK": "HK", "CN": "CN", "CRYPTO": "GLOBAL"}

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Amount = Annotated[float, Field(allow_inf_nan=False)]


class EditError(ValueError):
    """用户能看懂的修改错误。"""


# ---------- 请求 ----------

class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    account_id: str


class SetHolding(_Request):
    """新增或修改一个持仓。不填成本价时沿用原来的成本价。"""

    kind: Literal["set_holding"] = "set_holding"
    symbol: str = Field(min_length=1)  # 任意写法：0700 / 600519 / NVDA / HK:00700
    market: Literal["US", "HK", "CN", "CRYPTO"] | None = None  # 加密货币必须指定
    quantity: Positive
    price: NonNegative
    cost_price: NonNegative | None = None  # 每单位成本价
    name: str | None = None
    asset_class: AssetClass | None = None
    region: str | None = None
    sector: str | None = None


class RemoveHolding(_Request):
    kind: Literal["remove_holding"] = "remove_holding"
    instrument_id: str


class SetCash(_Request):
    """把某币种现金设为指定金额；设为 0 即删除该币种。"""

    kind: Literal["set_cash"] = "set_cash"
    currency: Currency
    amount: Amount


class RecordFlow(_Request):
    """记录入金（正）/ 出金（负）。

    adjust_cash=True（默认）：同时调整现金，日期固定为最新估值日，保证净值和资金进出在同一天；
    adjust_cash=False：只记流水（现金已经反映在余额里时用），日期可选，不晚于今天。
    """

    kind: Literal["record_flow"] = "record_flow"
    currency: Currency
    amount: Amount
    date: dt.date | None = None
    note: str | None = None
    adjust_cash: bool = True

    @field_validator("amount")
    @classmethod
    def _nonzero(cls, v: float) -> float:
        if v == 0:
            raise ValueError("金额不能为 0")
        return v


EditRequest = Annotated[SetHolding | RemoveHolding | SetCash | RecordFlow, Field(discriminator="kind")]
_REQUEST = TypeAdapter(EditRequest)

_FIELD_LABELS = {
    "kind": "修改类型", "account_id": "账户", "symbol": "代码", "market": "市场", "quantity": "数量",
    "price": "价格", "cost_price": "成本价", "name": "名称", "asset_class": "类别", "region": "地区",
    "sector": "行业", "instrument_id": "标的", "currency": "币种", "amount": "金额", "date": "日期",
    "note": "备注", "adjust_cash": "调整现金",
}


def explain(error: ValidationError) -> str:
    """把校验错误翻成中文。"""
    parts = []
    for e in error.errors():
        names = [x for x in e["loc"] if isinstance(x, str) and x in _FIELD_LABELS]
        field = _FIELD_LABELS[names[-1]] if names else "请求"
        ctx = e.get("ctx") or {}
        t = e["type"]
        if t == "value_error":
            msg = str(ctx.get("error", e["msg"]))
        elif t == "missing":
            msg = "必填"
        elif t == "greater_than":
            msg = f"必须大于 {ctx['gt']:g}"
        elif t == "greater_than_equal":
            msg = f"不能小于 {ctx['ge']:g}"
        elif t in ("float_parsing", "float_type", "int_parsing"):
            msg = "必须是数字"
        elif t == "finite_number":
            msg = "必须是有限数"
        elif t.startswith("date"):
            msg = "日期格式应为 YYYY-MM-DD"
        elif t == "string_too_short":
            msg = "不能为空"
        elif t in ("literal_error", "enum"):
            msg = "取值不对"
        elif t in ("union_tag_invalid", "union_tag_not_found"):
            msg = "未知的修改类型"
        elif t == "extra_forbidden":
            msg = "不认识的字段"
        else:
            msg = e["msg"]
        parts.append(f"{field}：{msg}")
    return "；".join(parts)


def parse_request(data: Mapping[str, Any] | BaseModel) -> SetHolding | RemoveHolding | SetCash | RecordFlow:
    if isinstance(data, (SetHolding, RemoveHolding, SetCash, RecordFlow)):
        return data
    try:
        return _REQUEST.validate_python(dict(data))
    except ValidationError as e:
        raise EditError(explain(e)) from None


# ---------- 结果 ----------

class ChangeView(BaseModel):
    id: int
    created_at: str
    actor: str
    kind: str
    account_id: str
    account_name: str
    summary: str
    status: str
    status_label: str
    expires_at: str | None = None
    applied_at: str | None = None
    undone_at: str | None = None
    can_undo: bool = False
    undo_blocked_by: int | None = None  # 之后又改了同一项的那次修改
    refresh_error: str | None = None    # 生效后刷新快照失败的原因


@dataclass
class _Plan:
    account: Account
    summary: str
    before: dict[str, Any]
    after: dict[str, Any]
    instrument: dict[str, Any] | None = None


def _n(x: float | None) -> str:
    if x is None:
        return "无"
    return f"{x:,.6f}".rstrip("0").rstrip(".")


# ---------- 服务 ----------

class EditService:
    def __init__(self, repo: Repository, sync: SyncService, today: Callable[[], dt.date]) -> None:
        self.repo = repo
        self.sync = sync
        self.today = today

    def effective_date(self) -> dt.date:
        latest = self.repo.latest_as_of()
        return dt.date.fromisoformat(latest) if latest else self.today()

    # ----- 对外 -----

    def preview(self, request: Mapping[str, Any] | BaseModel, actor: str) -> ChangeView:
        req = parse_request(request)
        plan = self._plan(req)
        now = dt.datetime.now(dt.UTC)
        change_id = self.repo.insert_change(
            created_at=utc_now(), actor=actor, kind=req.kind, account_id=plan.account.id,
            summary=plan.summary, request=req.model_dump(mode="json"), before=plan.before,
            after=plan.after, instrument=plan.instrument, status="pending",
            expires_at=(now + PENDING_TTL).isoformat(timespec="seconds"),
        )
        return self.get(change_id)

    def confirm(self, change_id: int) -> ChangeView:
        ch = self._load(change_id)
        if ch["status"] != "pending":
            raise EditError(f"修改 #{change_id} {STATUS_LABELS[ch['status']]}，不能再确认")
        if ch["expires_at"] and ch["expires_at"] < utc_now():
            self.repo.update_change(change_id, status="discarded")
            raise EditError(f"修改 #{change_id} 已过期，请重新提交")
        if any(self._read(k) != v for k, v in ch["before"].items()):
            self.repo.update_change(change_id, status="discarded")
            raise EditError(f"修改 #{change_id} 预览之后数据已经变了，请重新提交")
        with transaction(self.repo.conn):
            if ch["instrument"]:
                self.repo.upsert_instruments([Instrument(**ch["instrument"])])
            for key, state in ch["after"].items():
                self._write(key, state)
            self.repo.update_change(change_id, status="applied", applied_at=utc_now())
        return self._with_refresh(change_id)

    def apply(self, request: Mapping[str, Any] | BaseModel, actor: str) -> ChangeView:
        return self.confirm(self.preview(request, actor).id)

    def discard(self, change_id: int) -> ChangeView:
        ch = self._load(change_id)
        if ch["status"] != "pending":
            raise EditError(f"修改 #{change_id} {STATUS_LABELS[ch['status']]}，不能作废")
        self.repo.update_change(change_id, status="discarded")
        return self.get(change_id)

    def undo(self, change_id: int) -> ChangeView:
        ch = self._load(change_id)
        if ch["status"] != "applied":
            raise EditError(f"修改 #{change_id} {STATUS_LABELS[ch['status']]}，只能撤销已生效的修改")
        blocker = self._later_overlap(ch)
        if blocker:
            raise EditError(
                f"之后的修改 #{blocker['id']}（{blocker['summary']}）也改了这一项，请先撤销它"
            )
        if any(self._read(k) != v for k, v in ch["after"].items()):
            raise EditError(f"修改 #{change_id} 涉及的数据已被其他方式改动，无法自动撤销")
        with transaction(self.repo.conn):
            for key, state in ch["before"].items():
                self._write(key, state)
            self.repo.update_change(change_id, status="undone", undone_at=utc_now())
        return self._with_refresh(change_id)

    def get(self, change_id: int) -> ChangeView:
        ch = self._load(change_id)
        blocker = self._later_overlap(ch) if ch["status"] == "applied" else None
        return self._view(ch, blocker["id"] if blocker else None)

    def list(self, account_id: str | None = None, limit: int = 200) -> list[ChangeView]:
        """新的在前；同时算出每条能否撤销。"""
        rows = self.repo.list_changes(account_id=account_id, limit=limit)
        touched: dict[str, int] = {}  # 状态键 → 最近一次改它的已生效修改
        out = []
        for ch in rows:
            blocker = None
            if ch["status"] == "applied":
                keys = set(ch["after"])
                blocker = next((touched[k] for k in keys if k in touched), None)
                for k in keys:
                    touched[k] = ch["id"]
            out.append(self._view(ch, blocker))
        return out

    # ----- 生成"改前 → 改后" -----

    def _plan(self, req: SetHolding | RemoveHolding | SetCash | RecordFlow) -> _Plan:
        if isinstance(req, RecordFlow):
            account = self._flow_account(req.account_id)
            if account.source != "manual":
                req = req.model_copy(update={"adjust_cash": False})  # 现金来自数据源，只记流水
            return self._plan_record_flow(account, req)
        account = self._manual_account(req.account_id)
        if isinstance(req, SetHolding):
            return self._plan_set_holding(account, req)
        if isinstance(req, RemoveHolding):
            return self._plan_remove_holding(account, req)
        if isinstance(req, SetCash):
            return self._plan_set_cash(account, req)
        return self._plan_record_flow(account, req)

    def _flow_account(self, account_id: str) -> Account:
        """能手动记资金进出的账户：手动账户，以及接口拿不到资金流水的数据源（MANUAL_FLOW_SOURCES）。"""
        account = self.repo.get_account(account_id)
        if account is not None and account.active and account.source in MANUAL_FLOW_SOURCES:
            return account
        try:
            return self._manual_account(account_id)
        except EditError as e:
            if account is not None and account.source != "manual":
                raise EditError(f"{account.name} 的资金进出由 {account.source} 同步，不能手动记") from e
            raise

    def _manual_account(self, account_id: str) -> Account:
        account = self.repo.get_account(account_id)
        if account is None:
            raise EditError(f"没有这个账户：{account_id}")
        if not account.active:
            raise EditError(f"账户已停用：{account.name}")
        if account.source != "manual":
            raise EditError(f"{account.name} 的数据来自 {account.source}，只能修改手动账户")
        return account

    def _plan_set_holding(self, account: Account, req: SetHolding) -> _Plan:
        try:
            iid = normalize(req.symbol, req.market)
        except UnresolvedInstrument:
            raise EditError(f"认不出这个代码：{req.symbol}（加密货币请把市场选为 CRYPTO）") from None
        existing = self.repo.get_instrument(iid)
        market = market_of(iid)
        currency = existing.currency if existing else default_currency(iid)
        if currency is None:
            raise EditError(f"无法确定 {iid} 的币种")
        inst = Instrument(
            id=iid,
            name=req.name or (existing.name if existing else None) or iid.split(":", 1)[1],
            asset_class=req.asset_class or (existing.asset_class if existing else AssetClass.EQUITY),
            region=req.region or (existing.region if existing else MARKET_REGION.get(market)),
            sector=req.sector or (existing.sector if existing else None),
            currency=currency,
            extra=existing.extra if existing else {},
        )
        instrument = None if inst == existing else inst.model_dump(mode="json")

        key = f"holding|{account.id}|{iid}"
        before = self.repo.get_manual_holding(account.id, iid)
        if req.cost_price is not None:
            cost = req.quantity * req.cost_price
        elif before and before["cost_basis"] is not None and before["quantity"]:
            cost = before["cost_basis"] / before["quantity"] * req.quantity
        else:
            cost = None
        after = {"quantity": req.quantity, "price": req.price, "cost_basis": cost, "currency": currency}
        if before == after and instrument is None:
            raise EditError("没有变化")

        label = f"{inst.name}（{iid}）" if inst.name != iid.split(":", 1)[1] else iid
        if before is None:
            summary = f"新增持仓 {label}：数量 {_n(req.quantity)}，价格 {_n(req.price)} {currency}"
            if cost is not None:
                summary += f"，成本价 {_n(cost / req.quantity)}"
        else:
            diffs = []
            if before["quantity"] != after["quantity"]:
                diffs.append(f"数量 {_n(before['quantity'])} → {_n(after['quantity'])}")
            if before["price"] != after["price"]:
                diffs.append(f"价格 {_n(before['price'])} → {_n(after['price'])} {currency}")
            old_cp = before["cost_basis"] / before["quantity"] if before["cost_basis"] is not None else None
            new_cp = cost / req.quantity if cost is not None else None
            cost_changed = (old_cp is None) != (new_cp is None) or (
                old_cp is not None and new_cp is not None and abs(old_cp - new_cp) > 1e-9
            )
            if cost_changed:
                diffs.append(f"成本价 {_n(old_cp)} → {_n(new_cp)}")
            if instrument is not None:
                diffs.append("更新标的资料")
            summary = f"修改持仓 {label}：" + "；".join(diffs or ["更新标的资料"])
        return _Plan(account, summary, {key: before}, {key: after}, instrument)

    def _plan_remove_holding(self, account: Account, req: RemoveHolding) -> _Plan:
        try:
            iid = normalize(req.instrument_id)
        except UnresolvedInstrument:
            iid = req.instrument_id
        before = self.repo.get_manual_holding(account.id, iid)
        if before is None:
            raise EditError(f"{account.name} 没有持仓 {iid}")
        inst = self.repo.get_instrument(iid)
        label = f"{inst.name}（{iid}）" if inst and inst.name else iid
        summary = (f"删除持仓 {label}（数量 {_n(before['quantity'])}，"
                   f"价格 {_n(before['price'])} {before['currency']}）")
        key = f"holding|{account.id}|{iid}"
        return _Plan(account, summary, {key: before}, {key: None})

    def _plan_set_cash(self, account: Account, req: SetCash) -> _Plan:
        key = f"cash|{account.id}|{req.currency}"
        before = self.repo.get_manual_cash(account.id, req.currency)
        after = None if req.amount == 0 else req.amount
        if before == after:
            raise EditError("没有变化")
        summary = f"现金 {req.currency}：{_n(before)} → {_n(after) if after is not None else '0（删除）'}"
        return _Plan(account, summary, {key: before}, {key: after})

    def _plan_record_flow(self, account: Account, req: RecordFlow) -> _Plan:
        effective = self.effective_date()
        if req.adjust_cash:
            day = effective
        else:
            day = req.date or effective
            if day > self.today():
                raise EditError(f"日期不能晚于今天（{self.today()}）")

        ext = f"manual:{uuid.uuid4().hex[:12]}"
        flow_key = f"flow|{account.id}|{ext}"
        flow = {"date": day.isoformat(), "currency": req.currency, "amount": req.amount,
                "note": req.note or None}
        before: dict[str, Any] = {flow_key: None}
        after: dict[str, Any] = {flow_key: flow}

        word = "入金" if req.amount > 0 else "出金"
        summary = f"{word} {req.currency} {_n(abs(req.amount))}（{day}）"
        if req.adjust_cash:
            cash_key = f"cash|{account.id}|{req.currency}"
            old = self.repo.get_manual_cash(account.id, req.currency)
            new = (old or 0.0) + req.amount
            if new < -1e-9:
                raise EditError(f"出金后 {req.currency} 现金为负（{_n(new)}），请先核对现金余额")
            before[cash_key] = old
            after[cash_key] = None if abs(new) < 1e-9 else new
            summary += f"，现金 {_n(old)} → {_n(max(new, 0.0))}"
            if req.date and req.date != day:
                summary += f"；按最新估值日 {day} 记账"
        else:
            summary += "，只记流水、不改现金"
        if req.note:
            summary += f"；备注：{req.note}"
        return _Plan(account, summary, before, after)

    # ----- 状态读写 -----

    def _read(self, key: str) -> Any:
        kind, account_id, item = key.split("|", 2)
        if kind == "holding":
            return self.repo.get_manual_holding(account_id, item)
        if kind == "cash":
            return self.repo.get_manual_cash(account_id, item)
        if kind == "flow":
            return self.repo.get_cash_flow(account_id, item)
        raise ValueError(f"未知状态键：{key}")

    def _write(self, key: str, state: Any) -> None:
        kind, account_id, item = key.split("|", 2)
        if kind == "holding":
            if state is None:
                self.repo.remove_manual_holding(account_id, item)
            else:
                self.repo.set_manual_holding(account_id, item, state["quantity"], state["price"],
                                             state["currency"], cost_basis=state["cost_basis"])
        elif kind == "cash":
            if state is None:
                self.repo.remove_manual_cash(account_id, item)
            else:
                self.repo.set_manual_cash(account_id, item, state)
        elif kind == "flow":
            if state is None:
                self.repo.delete_cash_flow(account_id, item)
            else:
                self.repo.insert_cash_flows(
                    [CashFlow(date=state["date"], account_id=account_id, currency=state["currency"],
                              amount=state["amount"], external_id=item, note=state["note"])],
                    origin="manual",
                )
        else:
            raise ValueError(f"未知状态键：{key}")

    # ----- 其他 -----

    def _with_refresh(self, change_id: int) -> ChangeView:
        """把修改反映到最新估值日的快照和净值上。"""
        report = self.sync.run(self.effective_date(), only="manual", refetch_fx=False)
        view = self.get(change_id)
        problems = [f"{s.source}：{s.error}" for s in report.sources if s.status != "ok"]
        if report.nav_total is None:
            problems.append(report.fx_detail or "未能计算净值")
        if problems:
            view.refresh_error = "修改已保存，但刷新快照失败：" + "；".join(problems)
        return view

    def _later_overlap(self, ch: dict[str, Any]) -> dict[str, Any] | None:
        keys = set(ch["after"])
        later = self.repo.list_changes(account_id=ch["account_id"], statuses=["applied"],
                                       after_id=ch["id"])
        return next((c for c in reversed(later) if keys & set(c["after"])), None)

    def _load(self, change_id: int) -> dict[str, Any]:
        ch = self.repo.get_change(change_id)
        if ch is None:
            raise EditError(f"没有修改 #{change_id}")
        return ch

    def _view(self, ch: dict[str, Any], blocker: int | None) -> ChangeView:
        account = self.repo.get_account(ch["account_id"])
        return ChangeView(
            id=ch["id"], created_at=ch["created_at"], actor=ch["actor"], kind=ch["kind"],
            account_id=ch["account_id"], account_name=account.name if account else ch["account_id"],
            summary=ch["summary"], status=ch["status"], status_label=STATUS_LABELS[ch["status"]],
            expires_at=ch["expires_at"], applied_at=ch["applied_at"], undone_at=ch["undone_at"],
            can_undo=ch["status"] == "applied" and blocker is None, undo_blocked_by=blocker,
        )
