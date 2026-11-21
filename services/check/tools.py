"""Tools the ADK agents call. Deterministic code: the model chooses when to call them, never what
they return.

- ``read_page``: opens a page the search found, through the SSRF-guarded fetcher.
- ``fact_check_claim``: the Google Fact Check Tools API. Its results are recorded in session state
  exactly as the API returned them, so a fact-check URL or rating is never model output (P7.3).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from google.adk.tools import ToolContext

from services.check.pipeline import PAGE_TEXT_LIMIT, Fetcher

MAX_PAGES = 12  # a ceiling on page fetches per check; extraction itself is not capped
MAX_FACT_CHECKS = 5
FACT_CHECK_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"


def make_read_page(fetcher: Fetcher) -> Callable[..., Awaitable[dict]]:
    async def read_page(url: str, tool_context: ToolContext) -> dict:
        """Open one of the candidate web pages and return its text, so exact passages can be quoted.

        Args:
            url: a page address taken from the list of candidate pages.
        """
        state = tool_context.state
        if url not in (state.get("candidates") or []):
            return {"error": "That address is not one of the candidate pages."}
        pages = dict(state.get("pages") or {})
        if url in pages:
            return {"url": pages[url]["url"], "text": pages[url]["text"]}
        if len(pages) >= MAX_PAGES:
            return {"error": "The page limit for this check has been reached."}
        try:
            page = await fetcher.fetch(url)
        except Exception:  # noqa: BLE001 - any failure just means this page cannot be used
            return {"error": "That page could not be read."}
        pages[url] = {"url": page.url, "text": page.text[:PAGE_TEXT_LIMIT]}
        state["pages"] = pages
        return {"url": page.url, "text": pages[url]["text"]}

    return read_page


def _reviews(payload: Any) -> list[dict]:
    """Flatten the API response to the fields we show, dropping anything without an http(s) URL."""
    results: list[dict] = []
    seen: set[str] = set()
    for claim in (payload.get("claims") or []) if isinstance(payload, dict) else []:
        for review in claim.get("claimReview") or []:
            url = review.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            results.append({
                "publisher": (review.get("publisher") or {}).get("name") or "Fact-Check Source",
                "title": review.get("title") or "",
                "rating": review.get("textualRating") or "",
                "url": url,
            })
    return results[:MAX_FACT_CHECKS]


def make_fact_check(api_key: str | None, transport: httpx.AsyncBaseTransport | None = None
                    ) -> Callable[..., Awaitable[dict]]:
    async def fact_check_claim(claim_index: int, query: str, tool_context: ToolContext) -> dict:
        """Look the claim up in the Google Fact Check Tools database of published fact-checks.

        Args:
            claim_index: the number of the claim, as listed in the claims block.
            query: the claim's wording, used as the search query.
        """
        if not api_key:
            return {"error": "Fact-check lookup is not configured."}
        try:
            async with httpx.AsyncClient(transport=transport, timeout=8.0) as client:
                response = await client.get(
                    FACT_CHECK_URL,
                    params={"query": query, "pageSize": MAX_FACT_CHECKS},
                    headers={"X-Goog-Api-Key": api_key},  # a header, so the key never lands in a URL or log
                )
                response.raise_for_status()
                reviews = _reviews(response.json())
        except Exception:  # noqa: BLE001 - the lookup is optional; a failure must not fail the check
            return {"error": "The fact-check lookup failed."}
        found = dict(tool_context.state.get("fact_checks") or {})
        found[str(claim_index)] = reviews
        tool_context.state["fact_checks"] = found
        return {"reviews": reviews}

    return fact_check_claim
