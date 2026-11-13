"""网页：仪表盘、账户明细与编辑、修改日志、同步状态。

- 所有路由都是 async：在事件循环线程里串行访问同一个 SQLite 连接，单用户足够快，也不用处理并发
- 页面只渲染骨架，表格和图表由 app.js 从 /api/v1 取数
- 写入：表单经 HTMX 提交，成功后跳转并显示"已保存 / 撤销"，失败时在表单里显示原因
- 不做登录（由 Cloudflare Access 把关）；写入请求必须来自本站，防止别的网站借登录状态提交
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import escape

from portfolio_monitor import __version__
from portfolio_monitor.app import App
from portfolio_monitor.domain.instruments import MARKETS
from portfolio_monitor.domain.models import AssetClass
from portfolio_monitor.entry.formatting import ACCOUNT_KIND_LABELS, ASSET_CLASS_LABELS, money, pct
from portfolio_monitor.entry.mcp import build_mcp, mcp_route
from portfolio_monitor.entry.web import api
from portfolio_monitor.services.edit import EditError
from portfolio_monitor.services.query import RANGES

HERE = Path(__file__).parent
STATIC = HERE / "static"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@functools.cache
def _hash(name: str, mtime: float) -> str:
    return hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:10]


def _static_url(name: str) -> str:
    """带内容哈希的静态文件地址：文件一改，浏览器就会重新下载。"""
    return f"/static/{name}?v={_hash(name, (STATIC / name).stat().st_mtime)}"


def _sign(v: float | None) -> str:
    if v is None or abs(v) < 1e-12:
        return ""
    return "up" if v > 0 else "down"


def _is_cross_site(request: Request) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return False  # 非浏览器客户端（curl、测试）
    hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
    return urlparse(origin).netloc not in hosts


def create_app(pm: App, *, mcp_token: str | None = None) -> FastAPI:
    """mcp_token 为空时不开放 /mcp。"""
    mcp_server = mcp = None
    if mcp_token:
        mcp_server = build_mcp(pm)
        mcp = mcp_route(mcp_server, mcp_token)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if mcp_server is None:
            yield
            return
        async with mcp_server.session_manager.run():
            yield

    web = FastAPI(title="portfolio-monitor", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url="/api/v1/openapi.json", lifespan=lifespan)
    web.state.pm = pm
    web.mount("/static", StaticFiles(directory=STATIC), name="static")
    web.include_router(api.router)
    if mcp is not None:
        web.router.routes.append(mcp)

    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters.update(money=money, pct=lambda v: pct(v, signed=False), spct=pct, sign=_sign)
    env.globals.update(static=_static_url, version=__version__)

    def render(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        latest = pm.query.latest_date()
        return templates.TemplateResponse(request, name, {"latest": latest, **ctx}, status_code=status)

    def form_error(message: str, status: int = 422) -> HTMLResponse:
        return HTMLResponse(f'<p class="form-error" role="alert">{escape(message)}</p>', status_code=status)

    # ----- 中间件与错误 -----

    @web.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        if request.method not in SAFE_METHODS and _is_cross_site(request):
            return JSONResponse({"error": "拒绝跨站请求"}, status_code=403)
        return await call_next(request)

    @web.exception_handler(EditError)
    async def edit_error(request: Request, exc: EditError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": str(exc)}, status_code=400)
        return form_error(str(exc))

    @web.exception_handler(LookupError)
    async def not_found(request: Request, exc: LookupError):
        message = str(exc.args[0]) if exc.args else "没有找到"
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": message}, status_code=404)
        return render(request, "message.html", status=404, title="没有找到", message=message)

    @web.exception_handler(ValueError)
    async def bad_request(request: Request, exc: ValueError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": str(exc)}, status_code=400)
        return render(request, "message.html", status=400, title="请求有误", message=str(exc))

    # ----- 页面 -----

    @web.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if pm.query.latest_date() is None:
            return render(request, "message.html", title="还没有数据", message=(
                "先运行一次同步：uv run portfolio-monitor sync；"
                "想先看看效果：uv run portfolio-monitor demo --serve"))
        overview = pm.query.overview()
        failed = [s for s in overview.sources if s.status != "ok"]
        return render(request, "dashboard.html", page="dashboard", o=overview, failed=failed,
                      risk=pm.risk.alerts(overview.as_of), ranges=RANGES, asset_labels=ASSET_CLASS_LABELS)

    @web.get("/accounts/{account_id}", response_class=HTMLResponse)
    async def account(request: Request, account_id: str, change: int | None = None,
                      undone: int | None = None):
        summary = next((a for a in pm.query.accounts() if a.id == account_id), None)
        if summary is None:
            raise LookupError(f"没有这个账户：{account_id}")
        currencies = sorted({summary.currency, "USD", "HKD", "CNY"})
        return render(
            request, "account.html", page="account", a=summary,
            kind_label=ACCOUNT_KIND_LABELS.get(summary.kind, summary.kind),
            flash=_flash(change, undone), effective=pm.edit.effective_date(),
            markets=MARKETS, asset_classes=[(c.value, ASSET_CLASS_LABELS[c.value]) for c in AssetClass
                                            if c is not AssetClass.CASH],
            currencies=currencies,
        )

    @web.get("/changes", response_class=HTMLResponse)
    async def changes(request: Request, change: int | None = None, undone: int | None = None):
        return render(request, "changes.html", page="changes", flash=_flash(change, undone))

    @web.get("/sync", response_class=HTMLResponse)
    async def sync(request: Request):
        return render(request, "sync.html", page="sync")

    def _flash(change: int | None, undone: int | None) -> dict[str, Any] | None:
        cid = change or undone
        if cid is None:
            return None
        try:
            view = pm.edit.get(cid)
        except EditError:
            return None
        return {"view": view, "undone": undone is not None}

    # ----- 写入（HTMX 表单） -----

    @web.post("/accounts/{account_id}/edit")
    async def edit(request: Request, account_id: str):
        form = await request.form()
        data = {k: v.strip() for k, v in form.items() if isinstance(v, str) and v.strip() != ""}
        req: dict[str, Any] = {"account_id": account_id, "kind": data.pop("kind", "")}
        if req["kind"] == "record_flow":
            direction = data.pop("direction", "in")
            data["adjust_cash"] = data.pop("adjust_cash", "") == "on"
            amount = data.get("amount")
            if amount is not None and direction == "out" and not amount.startswith("-"):
                data["amount"] = f"-{amount}"
        req.update(data)
        view = pm.edit.apply(req, actor="web")
        if view.refresh_error:
            return form_error(view.refresh_error, status=200)
        return _redirect(f"/accounts/{account_id}?change={view.id}")

    @web.post("/changes/{change_id}/undo")
    async def undo(request: Request, change_id: int):
        form = await request.form()
        view = pm.edit.undo(change_id)
        back = str(form.get("next") or f"/accounts/{view.account_id}")
        if not back.startswith("/") or back.startswith("//"):
            back = "/changes"
        return _redirect(f"{back}?undone={view.id}")

    def _redirect(url: str) -> Response:
        return Response(status_code=200, headers={"HX-Redirect": url})

    @web.get("/favicon.ico")
    async def favicon():
        return RedirectResponse("/static/favicon.svg")

    return web

