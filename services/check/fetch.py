"""Safe page fetching for Check.

Pages are fetched only from public http(s) hosts, never from social platforms (P5.1), with a size
cap and a hop limit. Every redirect hop is re-validated, so a public URL cannot bounce the service
onto an internal address. Fetched text is data, never instructions (PRD red-team #1, #10).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from verifact_core.url_safety import is_social_url

TEXT_TYPES = frozenset({"text/html", "text/plain", "application/xhtml+xml"})
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "head"})
_BLOCK_TAGS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
                         "section", "article", "blockquote"})


class FetchError(Exception):
    """The page could not be fetched safely."""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    text: str


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Visible text of an HTML document, whitespace collapsed."""
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    return " ".join("".join(parser.parts).split())


def _default_resolver(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


def is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_global and not address.is_multicast


class HttpFetcher:
    """Fetches one page as plain text. ``resolver`` and ``transport`` are injectable for tests."""

    # ponytail: DNS is resolved before the request, so a rebinding host could still slip through;
    # pin the resolved IP in a custom transport if Check ever fetches untrusted hosts at scale.
    def __init__(
        self,
        *,
        timeout: float = 8.0,
        max_bytes: int = 1_000_000,
        max_redirects: int = 3,
        resolver: Callable[[str], list[str]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._resolver = resolver or _default_resolver
        self._transport = transport

    async def _guard(self, url: str) -> None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or not host:
            raise FetchError("only http(s) URLs are fetched")
        if parts.port not in {None, 80, 443}:
            raise FetchError("non-standard port")
        if is_social_url(url):
            raise FetchError("social platforms are not fetched")
        try:
            addresses = await asyncio.to_thread(self._resolver, host)
            if not addresses or not all(is_public_ip(a) for a in addresses):
                raise FetchError("host is not a public address")
        except FetchError:
            raise
        except Exception as exc:  # noqa: BLE001 - resolver failures are all "cannot fetch"
            raise FetchError("host could not be resolved") from exc

    async def fetch(self, url: str) -> FetchedPage:
        current = url
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                headers={"User-Agent": "VeriFactCheck/1.0"},
            ) as client:
                for _ in range(self._max_redirects + 1):
                    await self._guard(current)
                    async with client.stream("GET", current, follow_redirects=False) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise FetchError("redirect without a location")
                            current = urljoin(current, location)
                            continue
                        if response.status_code != 200:
                            raise FetchError(f"status {response.status_code}")
                        kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                        if kind not in TEXT_TYPES:
                            raise FetchError("not a text page")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) >= self._max_bytes:
                                break
                        raw = bytes(body[: self._max_bytes]).decode(
                            response.charset_encoding or "utf-8", errors="replace"
                        )
                    text = " ".join(raw.split()) if kind == "text/plain" else html_to_text(raw)
                    return FetchedPage(url=current, text=text)
                raise FetchError("too many redirects")
        except httpx.HTTPError as exc:
            raise FetchError("request failed") from exc
