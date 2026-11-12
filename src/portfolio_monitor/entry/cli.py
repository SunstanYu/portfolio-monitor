"""命令行入口：portfolio-monitor <命令>。"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import os
import secrets
import sys
import unicodedata
from collections.abc import Sequence
from pathlib import Path

from portfolio_monitor.app import App, open_app, today_ny
from portfolio_monitor.config import load_settings, parse_settings
from portfolio_monitor.domain.models import AssetClass, Instrument
from portfolio_monitor.entry.formatting import FIELD_LABELS, label, money, pct
from portfolio_monitor.sources.base import SourceError
from portfolio_monitor.sources.fx import StaticFx

GROUP_FIELDS = ("instrument_id", "account_id", "asset_class", "currency", "region", "sector")
_DEMO_MARKER = ".pm-demo"


# ---------- 输出格式 ----------

def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int, right: bool) -> str:
    gap = " " * max(width - _width(text), 0)
    return gap + text if right else text + gap


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], right: Sequence[int] = ()) -> str:
    widths = [max(_width(str(x)) for x in col) for col in zip(headers, *rows, strict=True)]
    lines = ["  ".join(_pad(str(h), w, i in right) for i, (h, w) in enumerate(zip(headers, widths)))]
    lines.append("  ".join("─" * w for w in widths))
    for row in rows:
        lines.append("  ".join(_pad(str(c), w, i in right) for i, (c, w) in enumerate(zip(row, widths))))
    return "\n".join(lines)


def section(title: str) -> None:
    print(f"\n■ {title}")


# ---------- 命令 ----------

def cmd_migrate(app: App, _: argparse.Namespace) -> int:
    from portfolio_monitor.storage.db import schema_version

    print(f"数据库：{app.settings.db_path}  结构版本：{schema_version(app.repo.conn)}")
    return 0


def cmd_sync(app: App, args: argparse.Namespace) -> int:
    day = args.date or today_ny()
    report = app.sync.run(day, only=args.source)
    print(f"估值日 {day}" + (f"（只同步 {args.source}）" if args.source else ""))
    rows = []
    for s in report.sources:
        note = s.error or s.note or ""
        if s.carried_forward:
            note += f"（沿用上次快照：{', '.join(s.carried_forward)}）"
        rows.append([s.source, "✅" if s.status == "ok" else "❌", str(s.positions), str(s.cash),
                     str(s.new_flows), note])
    print(table(["数据源", "状态", "持仓", "现金", "新流水", "备注"], rows, right=(2, 3, 4)))
    print(f"汇率：{report.fx_status}" + (f"  {report.fx_detail}" if report.fx_detail else ""))
    print(f"总资产 {money(report.nav_total)}  投资资产 {money(report.nav_investable)}"
          + ("  ⚠ 含沿用数据" if report.stale else ""))
    return 0 if report.ok else 1


def cmd_backfill(app: App, args: argparse.Namespace) -> int:
    end = args.end or today_ny()
    start = end - dt.timedelta(days=args.days - 1)
    try:
        r = app.sync.backfill(args.source, start, end)
    except (ValueError, SourceError) as e:
        print(f"⚠ {e}", file=sys.stderr)
        return 1
    print(f"{r.source} 回填 {r.start} → {r.end}（请求截止 {end}）")
    print(f"每日总值 {r.values} 天，新增资金进出 {r.new_flows} 条，重算净值 {r.nav_days} 天")
    if r.raw_path:
        print(f"原始报表：{r.raw_path}")
    for problem in r.fx_problems[:10]:
        print(f"⚠ {problem}")
    if len(r.fx_problems) > 10:
        print(f"⚠ 另有 {len(r.fx_problems) - 10} 条汇率问题")
    return 0 if not r.fx_problems else 1


def print_overview(app: App, day: dt.date | None) -> None:
    o = app.query.overview(day)
    section(f"概览（估值日 {o.as_of}{'，⚠ 含沿用数据' if o.stale else ''}）")
    print(table(
        ["总资产", "投资资产", "今日", "今年", "累计（TWR）", "最大回撤"],
        [[money(o.nav_total_usd), money(o.nav_investable_usd), pct(o.day_return), pct(o.ytd_return),
          pct(o.since_inception_return), pct(o.max_drawdown)]],
        right=range(6),
    ))
    section("资产分配（按资产类别）")
    print(table(["类别", "市值", "占比"],
                [[label("asset_class", s.key), money(s.value_usd), pct(s.weight, signed=False)]
                 for s in o.allocation],
                right=(1, 2)))
    bad = [s for s in o.sources if s.status != "ok"]
    if bad or o.unresolved_count:
        section("需要注意")
        for s in bad:
            print(f"  ❌ {s.source}：{s.error}")
        if o.unresolved_count:
            print(f"  ⚠ {o.unresolved_count} 个标的无法识别，已计入资产，待处理")


def print_holdings(app: App, day: dt.date | None, by: str, account: str | None, limit: int) -> None:
    filters = {"account_id": account} if account else None
    rows = app.query.holdings(day, group_by=[by], filters=filters)[:limit]
    if by == "instrument_id":
        print(table(
            ["名称", "编号", "市值", "占比", "浮盈"],
            [[r.get("name") or "—", r["instrument_id"], money(r["market_value_usd"]),
              pct(r["weight"], signed=False), money(r["unrealized_pnl_usd"])] for r in rows],
            right=(2, 3, 4),
        ))
    else:
        print(table(
            [FIELD_LABELS.get(by, by), "市值", "占比", "浮盈"],
            [[label(by, r[by], r), money(r["market_value_usd"]), pct(r["weight"], signed=False),
              money(r["unrealized_pnl_usd"])] for r in rows],
            right=(1, 2, 3),
        ))


def cmd_overview(app: App, args: argparse.Namespace) -> int:
    print_overview(app, args.date)
    return 0


def cmd_holdings(app: App, args: argparse.Namespace) -> int:
    print_holdings(app, args.date, args.by, args.account, args.limit)
    return 0


def cmd_performance(app: App, args: argparse.Namespace) -> int:
    series = app.query.performance()[-args.last:]
    print(table(
        ["日期", "投资资产", "当日入金", "日收益", "累计", "回撤"],
        [[str(p.date), money(p.nav), money(p.net_flow) if p.net_flow else "", pct(p.daily_return),
          pct(p.index - 1), pct(p.drawdown)] for p in series],
        right=range(1, 6),
    ))
    return 0


def cmd_serve(app: App, args: argparse.Namespace, *, demo: bool = False) -> int:
    import uvicorn

    from portfolio_monitor.entry.web import create_app

    token = os.environ.get("MCP_TOKEN") or None
    if token is None and demo:
        token = secrets.token_hex(32)
    try:
        web = create_app(app, mcp_token=token)
    except ValueError as e:
        print(f"⚠ {e}", file=sys.stderr)
        return 2

    print(f"网页：http://{args.host}:{args.port}  数据：{app.settings.data_dir}（Ctrl+C 停止）")
    mcp_url = f"http://127.0.0.1:{args.port}/mcp"
    if token is None:
        print("MCP：未开启，设置环境变量 MCP_TOKEN 后重启（见 .env.example）")
    elif demo and not os.environ.get("MCP_TOKEN"):
        print(f"MCP：{mcp_url}（只接受本机访问；token 为本次演示临时生成，重启后失效）")
        print(f'  接入 Claude Code：claude mcp add --transport http portfolio-demo {mcp_url} '
              f'--header "Authorization: Bearer {token}"')
    else:
        print(f"MCP：{mcp_url}（只接受本机访问，Authorization: Bearer $MCP_TOKEN）")
    uvicorn.run(web, host=args.host, port=args.port, log_level="warning")
    return 0


# ---------- 演示 ----------

def _demo_config(start: dt.date, fail_on: dt.date) -> dict:
    return {
        "fx": {"provider": "static"},
        "accounts": [
            {"id": "demo-us", "name": "示例美股账户", "kind": "broker", "source": "fake", "currency": "USD"},
            {"id": "demo-hk", "name": "示例港股账户", "kind": "broker", "source": "fake", "currency": "HKD"},
            {"id": "demo-cn", "name": "示例 A 股账户（手动）", "kind": "broker", "source": "manual",
             "currency": "CNY"},
            {"id": "demo-bank", "name": "示例银行账户（手动）", "kind": "bank", "source": "manual",
             "currency": "HKD", "include_in_returns": False},
        ],
        "sources": {"fake": {"start": start.isoformat(), "monthly_deposit": 2000,
                             "fail_on": [fail_on.isoformat()]}},
    }


def _prepare_demo_dir(path: Path) -> None:
    if path.exists() and any(path.iterdir()) and not (path / _DEMO_MARKER).exists():
        raise SystemExit(f"{path} 不是演示目录且非空，拒绝覆盖")
    for name in ("portfolio.sqlite", "portfolio.sqlite-wal", "portfolio.sqlite-shm"):
        (path / name).unlink(missing_ok=True)
    raw = path / "raw"
    if raw.exists():
        for f in sorted(raw.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
    path.mkdir(parents=True, exist_ok=True)
    (path / _DEMO_MARKER).touch()


def cmd_demo(_: App | None, args: argparse.Namespace) -> int:
    data_dir = args.data_dir.resolve()
    _prepare_demo_dir(data_dir)
    end = today_ny()
    start = end - dt.timedelta(days=args.days)
    days = [start + dt.timedelta(days=i) for i in range(1, args.days + 1)]
    days = [d for d in days if d.weekday() < 5]
    fail_on = days[len(days) // 2]

    settings = parse_settings(_demo_config(start, fail_on), config_path=None, root=Path.cwd())
    settings = dataclasses.replace(settings, data_dir=data_dir)
    app = open_app(settings, fx=StaticFx())
    try:
        repo = app.repo
        repo.upsert_instruments([Instrument(id="CN:600036", name="China Merchants Bank",
                                            asset_class=AssetClass.EQUITY, region="CN",
                                            sector="Financials", currency="CNY")])
        repo.set_manual_holding("demo-cn", "CN:600036", 1000, 40.0, "CNY", cost_basis=38_000.0)
        repo.set_manual_cash("demo-cn", "CNY", 5_000.0)
        repo.set_manual_cash("demo-bank", "HKD", 80_000.0)

        failures = []
        for d in days:
            report = app.sync.run(d)
            failures += [(d, s) for s in report.sources if s.status != "ok"]

        print(f"演示数据：{data_dir}（{len(days)} 个工作日，{days[0]} → {days[-1]}，全部为虚构数据）")
        print_overview(app, None)
        section("前十大持仓（跨账户合并）")
        print_holdings(app, None, "instrument_id", None, 10)
        section("按币种")
        print_holdings(app, None, "currency", None, 10)
        section("按账户")
        print_holdings(app, None, "account_id", None, 10)

        series = app.query.performance()
        flows = sum(p.net_flow for p in series[1:])
        naive = series[-1].nav / series[0].nav - 1
        section("收益率口径对照（投资资产）")
        print(table(
            ["期初", "期末", "期间净入金", "简单涨幅（含入金）", "TWR（剔除入金）"],
            [[money(series[0].nav), money(series[-1].nav), money(flows), pct(naive),
              pct(series[-1].index - 1)]],
            right=range(5),
        ))
        section("同步记录")
        print(f"  运行 {len(days)} 天，失败 {len(failures)} 次（模拟故障，当天沿用上次快照）")
        for d, s in failures:
            print(f"  {d} {s.source}：{s.error}；沿用 {', '.join(s.carried_forward)}")
        if args.serve:
            print()
            return cmd_serve(app, args, demo=True)
    finally:
        app.close()
    return 0


# ---------- 入口 ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portfolio-monitor", description="个人多账户投资组合监测")
    parser.add_argument("--config", type=Path, help="配置文件路径（默认 local/config.toml）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    date_arg = {"type": dt.date.fromisoformat, "help": "估值日 YYYY-MM-DD"}

    sub.add_parser("migrate", help="初始化 / 升级数据库")
    p = sub.add_parser("sync", help="运行一次每日同步")
    p.add_argument("--date", **date_arg)
    p.add_argument("--source", help="只同步这一个数据源（如次日早上给 IBKR 补跑前一估值日）")
    p = sub.add_parser("backfill", help="用数据源的历史回填净值和资金进出（如 IBKR 最多一年）")
    p.add_argument("source", help="数据源名称，如 ibkr")
    p.add_argument("--days", type=int, default=365, help="回填天数（默认 365）")
    p.add_argument("--end", type=dt.date.fromisoformat, help="截止日 YYYY-MM-DD（默认今天，没有数据时自动往前退）")
    p = sub.add_parser("overview", help="组合概览")
    p.add_argument("--date", **date_arg)
    p = sub.add_parser("holdings", help="持仓（可按维度汇总）")
    p.add_argument("--date", **date_arg)
    p.add_argument("--by", choices=GROUP_FIELDS, default="instrument_id")
    p.add_argument("--account")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("performance", help="收益率序列")
    p.add_argument("--last", type=int, default=20)
    p = sub.add_parser("serve", help="启动网页和 MCP（默认只监听本机；设置 MCP_TOKEN 后开放 /mcp）")
    _server_args(p)
    p = sub.add_parser("demo", help="用虚构数据跑完整流程")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--data-dir", type=Path, default=Path("data/demo"))
    p.add_argument("--serve", action="store_true", help="跑完后用演示数据启动网页和 MCP")
    _server_args(p)
    return parser


def _server_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，只允许本机访问）")
    p.add_argument("--port", type=int, default=8000)


COMMANDS = {
    "migrate": cmd_migrate, "sync": cmd_sync, "backfill": cmd_backfill, "overview": cmd_overview,
    "holdings": cmd_holdings, "performance": cmd_performance, "serve": cmd_serve,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "demo":
        return cmd_demo(None, args)
    app = open_app(load_settings(args.config))
    try:
        return COMMANDS[args.cmd](app, args)
    except LookupError as e:
        print(f"⚠ {e}", file=sys.stderr)
        return 2
    finally:
        app.close()
