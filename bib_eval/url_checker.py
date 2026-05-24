"""URL validity checker — verifies that URLs in references actually resolve."""

from __future__ import annotations

import asyncio
import ssl
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from .parser import BibEntry


@dataclass
class UrlCheckResult:
    """Result of a URL validity check."""

    entry_id: str
    url: str
    is_valid: bool
    status_code: Optional[int] = None
    error: Optional[str] = None


# Browser-like headers to avoid 403/406 from picky servers
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


async def _check_single_url(
    session: aiohttp.ClientSession,
    url: str,
    entry_id: str,
) -> UrlCheckResult:
    """Check a single URL with HEAD first, then GET fallback."""
    # Skip empty or obviously non-URL strings
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return UrlCheckResult(entry_id=entry_id, url=url or "(empty)", is_valid=False, error="Not a valid URL scheme")

    # DOI redirects and some sites are slow — use longer timeout
    is_doi = "doi.org" in url
    base_timeout = 40 if is_doi else 25

    for attempt in range(2):  # retry once on timeout
        for method in ("HEAD", "GET"):
            try:
                async with session.request(
                    method,
                    url,
                    headers=DEFAULT_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=base_timeout),
                    allow_redirects=True,
                    max_redirects=5,
                ) as resp:
                    # 2xx and 3xx: clearly valid
                    if 200 <= resp.status < 400:
                        return UrlCheckResult(entry_id=entry_id, url=url, is_valid=True, status_code=resp.status)

                    # 403, 406, 429, 503: server blocks or rate-limits — not a broken URL
                    if resp.status in (403, 406, 429, 503):
                        if method == "HEAD" and resp.status in (403, 405, 406):
                            continue  # fall through to GET
                        return UrlCheckResult(
                            entry_id=entry_id, url=url, is_valid=None,
                            status_code=resp.status,
                            error=f"Blocked by server (HTTP {resp.status}) — URL may still be valid",
                        )

                    # 404/410: definitively not found
                    return UrlCheckResult(
                        entry_id=entry_id, url=url, is_valid=False, status_code=resp.status,
                        error=f"Not found (HTTP {resp.status})",
                    )
            except aiohttp.ClientError as e:
                # DNS failures, connection refused, etc. — not a broken URL
                return UrlCheckResult(entry_id=entry_id, url=url, is_valid=None,
                                      error=f"Network error (URL may still be valid): {e}")
            except asyncio.TimeoutError:
                if attempt == 0:
                    base_timeout += 15  # give it more time on retry
                    break  # retry outer loop
                return UrlCheckResult(entry_id=entry_id, url=url, is_valid=False, error=f"Timeout after {base_timeout}s")
            except Exception as e:
                return UrlCheckResult(entry_id=entry_id, url=url, is_valid=False, error=f"Unexpected: {e}")
        # If HEAD failed with 403/405/406, try GET in next iteration
        # If we're here after the retry break, continue to retry
        if attempt == 0:
            continue

    # Shouldn't reach here but just in case
    return UrlCheckResult(entry_id=entry_id, url=url, is_valid=False, error="All methods failed")


async def check_urls(entries: list[BibEntry], concurrency: int = 10) -> list[UrlCheckResult]:
    """Check all URLs across entries concurrently.

    Args:
        entries: List of parsed BibEntry objects.
        concurrency: Max simultaneous HTTP connections.

    Returns:
        List of UrlCheckResult, one per entry that had a URL.
    """
    # Collect (entry_id, url) pairs that have a URL
    url_tasks: list[tuple[str, str]] = []
    for entry in entries:
        url = entry.primary_url
        if url:
            url_tasks.append((entry.entry_id, url))

    if not url_tasks:
        return []

    connector = aiohttp.TCPConnector(limit=concurrency, ssl=False)  # ssl=False avoids cert issues
    async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
        tasks = [
            _check_single_url(session, url, eid)
            for eid, url in url_tasks
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # Unwrap exceptions, preserving entry_id
    final: list[UrlCheckResult] = []
    for i, r in enumerate(results):
        if i < len(url_tasks):
            eid, url = url_tasks[i]
        else:
            eid, url = "(error)", ""
        if isinstance(r, UrlCheckResult):
            final.append(r)
        elif isinstance(r, Exception):
            final.append(UrlCheckResult(entry_id=eid, url=url, is_valid=False, error=str(r)))
    return final


# ---------------------------------------------------------------------------
# Semantic Scholar fallback for 403-blocked URLs
# ---------------------------------------------------------------------------

async def verify_blocked_urls_via_semantic_scholar(
    entries: list[BibEntry],
    blocked_results: list[UrlCheckResult],
    concurrency: int = 5,
) -> dict[str, UrlCheckResult]:
    """For URLs that returned 403/blocked, search Semantic Scholar by title.

    If the paper is found on Semantic Scholar, the URL is confirmed valid.
    Returns a dict mapping entry_id -> upgraded UrlCheckResult.
    """
    from difflib import SequenceMatcher

    # Build entry lookup
    entry_map: dict[str, BibEntry] = {e.entry_id: e for e in entries}

    # Collect entries with blocked URLs that have a title to search
    to_verify: list[tuple[str, str]] = []
    for r in blocked_results:
        if r.is_valid is None:  # blocked/unknown
            entry = entry_map.get(r.entry_id)
            if entry and entry.title:
                to_verify.append((r.entry_id, entry.title))

    if not to_verify:
        return {}

    connector = aiohttp.TCPConnector(limit=concurrency, ssl=False)
    async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
        tasks = [
            _search_semantic_scholar(session, eid, title)
            for eid, title in to_verify
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    upgraded: dict[str, UrlCheckResult] = {}
    for item in results:
        if isinstance(item, tuple) and len(item) == 2:
            eid, found = item
            if found:
                upgraded[eid] = UrlCheckResult(
                    entry_id=eid,
                    url=entry_map[eid].primary_url or "",
                    is_valid=True,
                    status_code=200,
                )
    return upgraded


# Semantic Scholar rate limiter
_ss_semaphore = asyncio.Semaphore(1)
_ss_last_call = 0.0
_SS_MIN_INTERVAL = 1.0  # seconds between Semantic Scholar calls


async def _search_semantic_scholar(
    session: aiohttp.ClientSession,
    entry_id: str,
    title: str,
) -> tuple[str, bool]:
    """Search Semantic Scholar for a paper title. Returns (entry_id, found)."""
    import aiohttp

    global _ss_last_call
    async with _ss_semaphore:
        elapsed = time.time() - _ss_last_call
        if elapsed < _SS_MIN_INTERVAL:
            await asyncio.sleep(_SS_MIN_INTERVAL - elapsed)

        query = title.strip("{} ")[:200]  # truncate long titles
        url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={aiohttp.helpers.quote(query)}&limit=3"

        try:
            async with session.get(
                url,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                _ss_last_call = time.time()
                if resp.status == 429:
                    await asyncio.sleep(3.0)
                    return (entry_id, False)
                if resp.status != 200:
                    return (entry_id, False)
                data = await resp.json()
        except Exception:
            _ss_last_call = time.time()
            return (entry_id, False)

    papers = data.get("data", [])
    if not papers:
        return (entry_id, False)

    # Check title similarity
    from difflib import SequenceMatcher
    import re

    def _norm(t: str) -> str:
        t = t.lower()
        t = re.sub(r"[^a-z0-9\s]", " ", t)
        return re.sub(r"\s+", " ", t).strip()

    norm_query = _norm(title)
    for paper in papers:
        paper_title = paper.get("title", "")
        sim = SequenceMatcher(None, norm_query, _norm(paper_title)).ratio()
        if sim >= 0.7:
            return (entry_id, True)

    return (entry_id, False)
