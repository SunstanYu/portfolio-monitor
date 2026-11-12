"""模拟 IBKR Flex Web Service：用虚构样本 fixtures/ibkr_flex.xml，不访问网络。

- SendRequest：token / query 不对返回 1015 / 1014；td 晚于 available_until 返回 1003
- GetStatement：每个 ReferenceCode 第一次返回 1019（生成中），之后返回报表，
  报表的 toDate 改成请求的 td，并去掉晚于 td 的每日净值
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import httpx

from portfolio_monitor.sources.ibkr import FlexClient

SAMPLE = (Path(__file__).parent / "fixtures" / "ibkr_flex.xml").read_text(encoding="utf-8")
TOKEN = "test-flex-token-123456"
QUERY = "987654"


def _status(ok: bool, *, ref: str = "", code: str = "", message: str = "") -> bytes:
    if ok:
        return (f"<FlexStatementResponse timestamp='x'><Status>Success</Status>"
                f"<ReferenceCode>{ref}</ReferenceCode><Url>x</Url></FlexStatementResponse>").encode()
    return (f"<FlexStatementResponse timestamp='x'><Status>Fail</Status><ErrorCode>{code}</ErrorCode>"
            f"<ErrorMessage>{message}</ErrorMessage></FlexStatementResponse>").encode()


class FlexMock:
    def __init__(self, available_until: dt.date = dt.date(2026, 3, 2), *, http_status: int = 200,
                 sample: str = SAMPLE) -> None:
        self.available_until = available_until
        self.http_status = http_status
        self.sample = sample
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self._polls: Counter[str] = Counter()

    def sent_to_dates(self) -> list[str]:
        return [r.url.params["td"] for r in self.requests if r.url.path.endswith("/SendRequest")]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.http_status != 200:
            return httpx.Response(self.http_status, content=b"oops")
        p = request.url.params
        if p.get("t") != TOKEN:
            return httpx.Response(200, content=_status(False, code="1015", message="Token is invalid."))
        if request.url.path.endswith("/SendRequest"):
            if p.get("q") != QUERY:
                return httpx.Response(200, content=_status(False, code="1014", message="Query is invalid."))
            td = dt.datetime.strptime(p["td"], "%Y%m%d").date()
            if td > self.available_until:
                return httpx.Response(200, content=_status(False, code="1003",
                                                           message="Statement is not available."))
            return httpx.Response(200, content=_status(True, ref=f"REF-{p['fd']}-{p['td']}"))
        ref = p["q"]
        self._polls[ref] += 1
        if self._polls[ref] == 1:
            return httpx.Response(200, content=_status(False, code="1019",
                                                       message="Statement generation in progress."))
        _, fd, td = ref.split("-")
        return httpx.Response(200, content=self._statement(fd, td))

    def _statement(self, fd: str, td: str) -> bytes:
        root = ET.fromstring(self.sample)
        for st in root.iter("FlexStatement"):
            st.set("fromDate", fd)
            st.set("toDate", td)
            summary = st.find("EquitySummaryInBase")
            if summary is not None:
                for row in list(summary):
                    if not fd <= row.get("reportDate", "") <= td:
                        summary.remove(row)
        return ET.tostring(root, encoding="utf-8")

    def client(self) -> FlexClient:
        return FlexClient(TOKEN, QUERY, client=httpx.Client(transport=httpx.MockTransport(self.handler)),
                          sleep=self.sleeps.append, poll_interval=0.01)
