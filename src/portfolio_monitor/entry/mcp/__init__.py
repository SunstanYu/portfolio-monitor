"""MCP 入口：build_mcp(App) 注册工具，mcp_route() 把它挂到网页应用的 /mcp。"""

from portfolio_monitor.entry.mcp.server import build_mcp, check_token, mcp_route

__all__ = ["build_mcp", "check_token", "mcp_route"]
