"""portfolio-monitor：个人多账户投资组合监测。"""

__version__ = "0.1.0"


def main() -> int:
    from portfolio_monitor.entry.cli import main as cli_main

    return cli_main()
