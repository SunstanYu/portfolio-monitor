"""JSON 接口 /api/v1/...：网页的图表和表格用它取数，以后换前端或做 App 也直接用。

写入与 MCP 语义一致：preview 返回"改前 → 改后"和 change_id，confirm 后才生效。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from portfolio_monitor.app import App
from portfolio_monitor.entry.web.tables import TABLES, Params

router = APIRouter(prefix="/api/v1")


def _app(request: Request) -> App:
    return request.app.state.pm


def _ok(data: Any) -> JSONResponse:
    return JSONResponse(jsonable_encoder(data))


@router.get("/overview")
async def overview(request: Request, date: dt.date | None = None):
    return _ok(_app(request).query.overview(date))


@router.get("/accounts")
async def accounts(request: Request, date: dt.date | None = None):
    return _ok(_app(request).query.accounts(date))


@router.get("/accounts/{account_id}")
async def account(request: Request, account_id: str, date: dt.date | None = None):
    app = _app(request)
    summary = next((a for a in app.query.accounts(date) if a.id == account_id), None)
    if summary is None:
        raise LookupError(f"没有这个账户：{account_id}")
    return _ok({"account": summary, "rows": app.query.account_rows(account_id, date),
                "flows": app.query.account_flows(account_id)})


@router.get("/holdings")
async def holdings(request: Request, group_by: str = "instrument_id",
                   account_id: str | None = None, date: dt.date | None = None):
    filters = {"account_id": account_id} if account_id else None
    return _ok(_app(request).query.holdings(date, group_by=[group_by], filters=filters))


@router.get("/alerts")
async def alerts(request: Request, date: dt.date | None = None):
    return _ok(_app(request).risk.alerts(date))


@router.get("/curve")
async def curve(request: Request, range: str = "all", date: dt.date | None = None):
    return _ok(_app(request).query.curve(range, date))


@router.get("/monthly-returns")
async def monthly_returns(request: Request, date: dt.date | None = None):
    return _ok(_app(request).query.monthly_returns(date))


@router.get("/tables/{table_id}")
async def table(request: Request, table_id: str, group_by: str | None = None,
                account_id: str | None = None, date: dt.date | None = None):
    spec = TABLES.get(table_id)
    if spec is None:
        raise LookupError(f"没有这个表格：{table_id}")
    return _ok(spec.payload(_app(request), Params(group_by, account_id, date)))


@router.get("/changes")
async def changes(request: Request, account_id: str | None = None, limit: int = 200):
    return _ok(_app(request).edit.list(account_id=account_id, limit=limit))


@router.get("/sync-runs")
async def sync_runs(request: Request, limit: int = 200):
    return _ok(_app(request).query.sync_history(limit))


@router.post("/changes/preview")
async def preview(request: Request, body: dict[str, Any] = Body(...)):
    return _ok(_app(request).edit.preview(body, actor="api"))


@router.post("/changes/{change_id}/confirm")
async def confirm(request: Request, change_id: int):
    return _ok(_app(request).edit.confirm(change_id))


@router.post("/changes/{change_id}/undo")
async def undo(request: Request, change_id: int):
    return _ok(_app(request).edit.undo(change_id))


@router.post("/changes/{change_id}/discard")
async def discard(request: Request, change_id: int):
    return _ok(_app(request).edit.discard(change_id))
