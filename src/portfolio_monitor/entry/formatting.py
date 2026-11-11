"""命令行和网页共用的显示格式与中文标签。"""

from __future__ import annotations

ASSET_CLASS_LABELS = {
    "equity": "股票", "etf": "ETF", "fund": "基金", "bond": "债券", "crypto": "加密货币",
    "cash": "现金", "option": "期权", "other": "其他",
}
ACCOUNT_KIND_LABELS = {"broker": "券商", "bank": "银行", "crypto": "加密", "other": "其他"}
REGION_LABELS = {"US": "美国", "HK": "香港", "CN": "中国内地", "GLOBAL": "全球"}
FIELD_LABELS = {
    "instrument_id": "标的", "account_id": "账户", "asset_class": "类别", "currency": "币种",
    "region": "地区", "sector": "行业",
}
ACTOR_LABELS = {"web": "网页", "api": "接口", "mcp": "MCP", "cli": "命令行"}
FLOW_ORIGIN_LABELS = {"api": "同步", "manual": "手动"}


def money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"-${-v:,.0f}" if v < 0 else f"${v:,.0f}"


def pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v:+.2%}" if signed else f"{v:.1%}"


def label(field: str, value: object, row: dict | None = None) -> str:
    """分组字段的显示名；按账户分组时优先用 row 里的账户名。"""
    if value is None:
        return "—"
    if field == "asset_class":
        return ASSET_CLASS_LABELS.get(str(value), str(value))
    if field == "region":
        return REGION_LABELS.get(str(value), str(value))
    if field == "account_id" and row and row.get("account_name"):
        return str(row["account_name"])
    if field == "instrument_id" and row and row.get("name"):
        return str(row["name"])
    return str(value)
