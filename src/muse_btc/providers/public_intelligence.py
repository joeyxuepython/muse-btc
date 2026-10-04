"""Bounded public fetches. Remote research links are data, never executable instructions."""

import asyncio
import csv
import io
import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from ..async_io import run_sync
from ..models import utc_now
from .common import ProviderError

RESEARCH_DOMAINS = {
    "Glassnode": {"research.glassnode.com", "insights.glassnode.com", "glassnode.com"},
    "Coinbase": {"www.coinbase.com", "coinbase.com"},
    "CoinShares": {"coinshares.com", "www.coinshares.com"},
    "Santiment": {"insights.santiment.net", "santiment.net"},
    "arXiv": {"export.arxiv.org", "arxiv.org"},
}


def allowed_url(url: str, domains: set[str]) -> str:
    u = urlsplit(url)
    if (
        u.scheme != "https"
        or u.hostname not in domains
        or u.username
        or u.password
        or u.port not in (None, 443)
    ):
        raise ProviderError("来源链接超出允许的公开域名")
    return url


class Page(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.meta, self.rows = [], [], {}, []
        self.ignored = 0
        self.anchor = None
        self.row = None
        self.cell = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self.ignored += 1
        if tag == "meta":
            self.meta[a.get("property", a.get("name", ""))] = a.get("content", "")
        if tag == "a":
            self.anchor = [a.get("href", ""), []]
        if tag == "tr":
            self.row = []
        if tag in {"td", "th"}:
            self.cell = []
        if tag in {"p", "div", "br", "h1", "h2", "h3", "li"} and not self.ignored:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)
            if self.anchor:
                self.anchor[1].append(data)
            if self.cell is not None:
                self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.ignored = max(0, self.ignored - 1)
        if tag == "a" and self.anchor:
            self.links.append((self.anchor[0], " ".join(self.anchor[1]).strip()))
            self.anchor = None
        if tag in {"td", "th"} and self.row is not None and self.cell is not None:
            self.row.append(" ".join(self.cell).strip())
            self.cell = None
        if tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = None

    @property
    def text(self):
        return "\n".join(
            " ".join(line.split()) for line in "".join(self.parts).splitlines() if line.strip()
        )


class PublicIntelligence:
    def __init__(self, providers):
        self.providers = providers
        self.settings, self.store = providers.settings, providers.store
        self.lock = asyncio.Lock()
        self.arxiv_next = 0.0

    async def fetch(self, source: str, url: str, domains: set[str], params=None):
        allowed_url(url, domains)
        if source in self.providers.backoff and self.providers.backoff[source] > utc_now():
            raise ProviderError("来源正在限流退避")
        if source == "arXiv":
            async with self.lock:
                loop = asyncio.get_running_loop()
                await asyncio.sleep(max(0, self.arxiv_next - loop.time()))
                self.arxiv_next = loop.time() + 3
        for _ in range(4):
            try:
                async with (
                    self.providers.semaphore,
                    self.providers.client.stream(
                        "GET",
                        url,
                        params=params,
                        headers={"User-Agent": "muse-btc/0.2 research-monitor"},
                    ) as response,
                ):
                    if response.is_redirect:
                        url = allowed_url(
                            urljoin(url, response.headers.get("location", "")), domains
                        )
                        params = None
                        continue
                    if response.status_code == 429:
                        from datetime import timedelta

                        self.providers.backoff[source] = utc_now() + timedelta(seconds=120)
                    if not response.is_success:
                        raise ProviderError(f"公开来源返回 HTTP {response.status_code}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.settings.research_max_bytes:
                            raise ProviderError("来源正文超过大小限制")
                    at = utc_now()
                    text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
                    raw = await run_sync(
                        self.store.save_raw, source, str(response.url), {"text": text}, at
                    )
                    return text, raw, at
            except httpx.HTTPError as exc:
                raise ProviderError(f"来源连接失败：{type(exc).__name__}") from exc
        raise ProviderError("来源重定向次数过多")

    async def fred(self, series: str):
        url = self.settings.fred_url + "/graph.csv"
        text, raw, at = await self.fetch(
            "FRED", url, {"fred.stlouisfed.org"}, {"id": series, "cosd": "2020-01-01"}
        )
        rows = list(csv.reader(io.StringIO(text)))
        values = []
        for row in rows[1:]:
            if len(row) != 2 or row[1] in {".", ""}:
                continue
            try:
                day, value = datetime.fromisoformat(row[0]).replace(tzinfo=UTC), float(row[1])
                if day <= at:
                    values.append((day, value))
            except ValueError:
                continue
        if not values:
            raise ProviderError("FRED 未返回有效的两列 CSV 数据")
        return values, raw, at

    async def stablecoins(self):
        url = self.settings.stablecoins_url + "/stablecoins"
        text, raw, at = await self.fetch(
            "DeFiLlama", url, {"stablecoins.llama.fi"}, {"includePrices": "true"}
        )
        try:
            rows = json.loads(text)["peggedAssets"]
            if not isinstance(rows, list):
                raise ValueError
            return rows, raw, at
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("稳定币响应结构不符") from exc
