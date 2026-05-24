"""Author verification — compares bib authors against known sources (dblp, ACL, ACM)."""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional
from enum import Enum

import aiohttp

from .parser import BibEntry


class Source(Enum):
    DBLP = "dblp"
    ACL = "acl"
    ACM = "acm"
    NEURIPS = "neurips"
    CVPR = "cvpr"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    CROSSREF = "crossref"
    ARXIV = "arxiv"
    OPENALEX = "openalex"
    OPENREVIEW = "openreview"
    GOOGLE_SCHOLAR = "google_scholar"
    UNKNOWN = "unknown"


@dataclass
class AuthorCheckResult:
    """Result of comparing bib authors against a known source."""

    entry_id: str
    source: Optional[Source] = None  # which source was matched
    bib_authors: list[str] = field(default_factory=list)
    source_authors: list[str] = field(default_factory=list)
    is_match: Optional[bool] = None  # True/False/None if couldn't check
    missing_authors: list[str] = field(default_factory=list)
    extra_authors: list[str] = field(default_factory=list)
    order_wrong: bool = False
    year_mismatch: bool = False  # True if bib year differs from source
    source_year: str = ""  # year from the matched source
    venue_mismatch: bool = False  # True if bib venue differs from source
    source_venue: str = ""  # venue from the matched source
    error: Optional[str] = None
    source_url: Optional[str] = None  # URL to the matched record
    title_match_score: float = 0.0  # how well the title matched


def _check_year(bib_year: str, source_year: str) -> bool:
    """Check if bib year matches source year (within ±1 tolerance).

    Returns True if years match, False if they don't.
    Returns True if either year is empty (can't check).
    """
    if not bib_year or not source_year:
        return True
    try:
        by = int(bib_year.strip())
        sy = int(str(source_year).strip())
        return abs(by - sy) <= 1
    except (ValueError, TypeError):
        return True  # can't parse, assume match


def _venue_similarity(bib_venue: str, source_venue: str) -> bool:
    """Check if bib venue matches source venue with loose fuzzy matching.

    Handles abbreviations: 'NeurIPS' ↔ 'Advances in Neural Information Processing Systems'
    Returns True if venues likely refer to the same event/journal.
    """
    if not bib_venue or not source_venue:
        return True  # can't check, assume match

    # Normalize
    bv = _normalize_title(bib_venue)
    sv = _normalize_title(source_venue)

    # Exact match after normalization
    if bv == sv:
        return True

    # One contains the other (e.g., "ACL 2023" in "Proceedings of ACL 2023")
    if bv in sv or sv in bv:
        return True

    # Fuzzy similarity
    sim = SequenceMatcher(None, bv, sv).ratio()
    return sim >= 0.5


# Common browser headers
HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
}

# Rate limiters — DBLP is strict about concurrent calls
_dblp_semaphore = asyncio.Semaphore(1)
_dblp_last_call = 0.0
_DBLP_MIN_INTERVAL = 0.8  # seconds between DBLP calls (be gentle)


def _normalize_title(title: str) -> str:
    """Normalize a title for comparison: lowercase, remove punctuation, collapse whitespace."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _title_similarity(t1: str, t2: str) -> float:
    """Compute similarity between two normalized titles."""
    return SequenceMatcher(None, _normalize_title(t1), _normalize_title(t2)).ratio()


def _normalize_author_name(name: str) -> str:
    """Normalize an author name for comparison.

    Collapses whitespace, lowercases, strips braces, DBLP disambiguation,
    Unicode diacritics, and German umlaut ASCII variants (ü↔ue, ö↔oe, ä↔ae).
    """
    n = name.strip().lower()
    # Remove surrounding braces
    n = re.sub(r"^\{|\}$", "", n)
    # Strip DBLP-style disambiguation suffixes: "Name 0001" -> "Name"
    n = re.sub(r"\s+\d{4}$", "", n)
    # Strip Unicode diacritics: Oğuzhan → Oguzhan, Hüttenrauch → Huttenrauch
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c))
    # German umlaut ASCII fallbacks: "Huettenrauch" ↔ "Huttenrauch" (ü)
    # Do this AFTER diacritic stripping so both forms converge to the same string
    n = re.sub(r"(?<=[aou])e(?=\w)", "", n)  # "Huettenrauch" → "Huttenrauch"
    # Replace commas with spaces (handles "LastName, FirstName" gracefully)
    n = n.replace(",", " ")
    # Remove periods from initials: "J." → "j"
    n = n.replace(".", " ")
    # Collapse whitespace
    n = " ".join(n.split())
    return n


def _author_to_token_set(name: str) -> frozenset[str]:
    """Convert an author name to a frozenset of name-part tokens.

    Keeps single-char tokens (initials) for later matching:
    'F. Xia' becomes {'f', 'xia'} and can match {'fei', 'xia'} via
    initial-aware overlap.
    """
    n = _normalize_author_name(name)
    return frozenset(n.split())


def _token_overlap_score(tok_a: frozenset[str], tok_b: frozenset[str]) -> float:
    """Compute overlap score between two author token sets.

    Uses Jaccard similarity with special handling for initials:
    - A single-char token 'f' in set A matches a token 'fei' in set B
      if the longer token starts with the initial.
    - Returns a score in [0.0, 1.0].
    """
    if not tok_a or not tok_b:
        return 0.0

    # Split into single-char (initials) and multi-char tokens
    initials_a = {t for t in tok_a if len(t) == 1}
    initials_b = {t for t in tok_b if len(t) == 1}
    names_a = tok_a - initials_a
    names_b = tok_b - initials_b

    # Exact name matches
    exact = len(names_a & names_b)

    # Initial-to-name matches: e.g. 'f' ↔ 'fei'
    initial_matches = 0
    matched_names_a: set[str] = set()
    matched_names_b: set[str] = set()
    for init in initials_a:
        for name in names_b:
            if name.startswith(init) and name not in matched_names_b:
                initial_matches += 1
                matched_names_b.add(name)
                break
    for init in initials_b:
        for name in names_a:
            if name.startswith(init) and name not in matched_names_a:
                initial_matches += 1
                matched_names_a.add(name)
                break

    # Initial-to-initial exact match
    initial_initial = len(initials_a & initials_b)

    total_matches = exact + initial_matches + initial_initial
    total_tokens = len(tok_a | tok_b)

    return total_matches / total_tokens if total_tokens > 0 else 0.0


def _compare_authors(
    bib_authors: list[str],
    source_authors: list[str],
) -> tuple[bool, list[str], list[str], bool]:
    """Compare two author lists using token-set matching with positional order check.

    Handles 'et al.' / 'et al' — if present in either list, only the authors
    before 'et al.' are compared. If those prefix authors match, it's a success.

    Returns:
        (is_match, missing_authors, extra_authors, order_wrong)
    """
    # --- Handle "et al." / "et al" ---
    ET_AL_TOKENS = frozenset({"et", "al"})

    def _find_et_al_pos(authors: list[str]) -> int:
        """Return index of first 'et al.' entry, or len(authors) if none."""
        for i, a in enumerate(authors):
            if _author_to_token_set(a) == ET_AL_TOKENS:
                return i
        return len(authors)

    bib_et_al_pos = _find_et_al_pos(bib_authors)
    src_et_al_pos = _find_et_al_pos(source_authors)

    has_et_al = bib_et_al_pos < len(bib_authors) or src_et_al_pos < len(source_authors)
    if has_et_al:
        # Truncate both lists to the earliest 'et al.' position
        cutoff = min(bib_et_al_pos, src_et_al_pos)
        bib_authors = bib_authors[:cutoff]
        source_authors = source_authors[:cutoff]

    # --- Standard comparison ---
    bib_tokens = [_author_to_token_set(a) for a in bib_authors]
    src_tokens = [_author_to_token_set(a) for a in source_authors]

    # Check which source authors are missing from bib
    missing: list[str] = []
    matched_bib_indices: set[int] = set()

    for s_idx, s_tok in enumerate(src_tokens):
        if not s_tok:
            continue
        best_score = 0.0
        best_b_idx = -1
        for b_idx, b_tok in enumerate(bib_tokens):
            if b_idx in matched_bib_indices:
                continue
            if not b_tok:
                continue
            score = _token_overlap_score(s_tok, b_tok)
            if score > best_score and score >= 0.5:
                best_score = score
                best_b_idx = b_idx
        if best_b_idx >= 0:
            matched_bib_indices.add(best_b_idx)
        else:
            # Fuzzy fallback: try whole-name string similarity
            # (handles "Yucheng" vs "Yuchen", one-letter romanization differences)
            s_name = _normalize_author_name(source_authors[s_idx])
            best_fuzzy = 0.0
            best_fuzzy_idx = -1
            for b_idx, b_name in enumerate(bib_authors):
                if b_idx in matched_bib_indices:
                    continue
                b_norm = _normalize_author_name(b_name)
                sim = SequenceMatcher(None, s_name, b_norm).ratio()
                if sim > best_fuzzy and sim >= 0.85:
                    best_fuzzy = sim
                    best_fuzzy_idx = b_idx
            if best_fuzzy_idx >= 0:
                matched_bib_indices.add(best_fuzzy_idx)
            else:
                missing.append(source_authors[s_idx])

    # Check which bib authors are extra (not matched by any source author)
    extra: list[str] = []
    matched_src_indices: set[int] = set()

    for b_idx, b_tok in enumerate(bib_tokens):
        if not b_tok:
            continue
        best_score = 0.0
        best_s_idx = -1
        for s_idx, s_tok in enumerate(src_tokens):
            if s_idx in matched_src_indices:
                continue
            if not s_tok:
                continue
            score = _token_overlap_score(b_tok, s_tok)
            if score > best_score and score >= 0.5:
                best_score = score
                best_s_idx = s_idx
        if best_s_idx >= 0:
            matched_src_indices.add(best_s_idx)
        else:
            # Fuzzy fallback: whole-name string similarity
            b_name = _normalize_author_name(bib_authors[b_idx])
            best_fuzzy = 0.0
            best_fuzzy_idx = -1
            for s_idx, s_name in enumerate(source_authors):
                if s_idx in matched_src_indices:
                    continue
                s_norm = _normalize_author_name(s_name)
                sim = SequenceMatcher(None, b_name, s_norm).ratio()
                if sim > best_fuzzy and sim >= 0.85:
                    best_fuzzy = sim
                    best_fuzzy_idx = s_idx
            if best_fuzzy_idx >= 0:
                matched_src_indices.add(best_fuzzy_idx)
            else:
                extra.append(bib_authors[b_idx])

    # Check positional order: verify ALL positions using token overlap
    order_wrong = False
    if not missing and not extra and len(bib_tokens) == len(src_tokens) and len(bib_tokens) >= 2:
        for i in range(len(bib_tokens)):
            if _token_overlap_score(bib_tokens[i], src_tokens[i]) < 0.5:
                order_wrong = True
                break

    is_match = len(missing) == 0 and len(extra) == 0
    return is_match, missing, extra, order_wrong


# ---------------------------------------------------------------------------
# DBLP API
# ---------------------------------------------------------------------------

async def _query_dblp(
    session: aiohttp.ClientSession,
    entry: BibEntry,
) -> Optional[AuthorCheckResult]:
    """Search DBLP for a paper and compare authors. Retries once on failure."""
    if not entry.title:
        return None

    global _dblp_last_call

    for attempt in range(2):
        async with _dblp_semaphore:
            elapsed = time.time() - _dblp_last_call
            if elapsed < _DBLP_MIN_INTERVAL:
                await asyncio.sleep(_DBLP_MIN_INTERVAL - elapsed)

            query = entry.title.strip("{} ")
            url = f"https://dblp.org/search/publ/api?q={aiohttp.helpers.quote(query)}&format=json&h=5"
            data = None

            try:
                async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    _dblp_last_call = time.time()
                    if resp.status == 429:
                        await asyncio.sleep(3.0)
                        _dblp_last_call = time.time()
                    elif resp.status == 200:
                        data = await resp.json()
            except Exception:
                _dblp_last_call = time.time()

        if data is None:
            if attempt == 0:
                await asyncio.sleep(2.0)  # wait then retry
            continue

        hits = data.get("result", {}).get("hits", {}).get("hit", [])
        if not hits:
            if attempt == 0:
                await asyncio.sleep(2.0)
            continue

        best_score = 0.0
        best_hit = None
        for hit in hits:
            info = hit.get("info", {})
            score = _title_similarity(entry.title, info.get("title", ""))
            if score > best_score:
                best_score = score
                best_hit = hit

        if best_hit is None or best_score < 0.6:
            if attempt == 0:
                await asyncio.sleep(2.0)
            continue

        info = best_hit.get("info", {})
        source_authors_raw = info.get("authors", {})

        if isinstance(source_authors_raw, dict):
            author_list = source_authors_raw.get("author", [])
        elif isinstance(source_authors_raw, list):
            author_list = source_authors_raw
        else:
            author_list = []

        if isinstance(author_list, str):
            author_list = [author_list]

        source_authors: list[str] = []
        for a in author_list:
            if isinstance(a, dict):
                name = a.get("text", "")
                if name:
                    source_authors.append(name)
            elif isinstance(a, str):
                source_authors.append(a)

        if not source_authors:
            return None

        dblp_url = info.get("url", "")
        dblp_year = str(info.get("year", "") or "")
        dblp_venue = str(info.get("venue", "") or "")
        is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)
        year_ok = _check_year(entry.year, dblp_year)
        venue_ok = _venue_similarity(
            entry.booktitle or entry.journal, dblp_venue
        )

        return AuthorCheckResult(
            entry_id=entry.entry_id,
            source=Source.DBLP,
            bib_authors=entry.authors,
            source_authors=source_authors,
            is_match=is_match and year_ok and venue_ok,
            missing_authors=missing,
            extra_authors=extra,
            order_wrong=order_wrong,
            year_mismatch=not year_ok,
            source_year=dblp_year,
            venue_mismatch=not venue_ok,
            source_venue=dblp_venue,
            source_url=dblp_url,
            title_match_score=best_score,
        )

    return None


# ---------------------------------------------------------------------------
# ACL Anthology API
# ---------------------------------------------------------------------------

async def _query_acl(
    session: aiohttp.ClientSession,
    entry: BibEntry,
) -> Optional[AuthorCheckResult]:
    """Search ACL Anthology for a paper and compare authors.

    First tries direct page scraping if URL contains aclanthology.org.
    Then falls back to the ACL search API.
    """
    if not entry.title:
        return None

    # --- Strategy 1: Direct page scrape if URL is an ACL page ---
    acl_url = entry.url if "aclanthology.org" in (entry.url or "") else ""
    if not acl_url and entry.doi and "aclanthology.org" in entry.doi:
        acl_url = entry.doi
    if acl_url:
        result = await _scrape_acl_page(session, entry, acl_url)
        if result is not None:
            return result

    # --- Strategy 2: ACL search API ---
    query = entry.title.strip("{} ")
    url = f"https://api.aclanthology.org/search?query={aiohttp.helpers.quote(query)}&limit=5"

    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None

    results = data if isinstance(data, list) else []
    if not results:
        return None

    best_score = 0.0
    best_result = None

    for result in results:
        hit_title = result.get("title", "")
        score = _title_similarity(entry.title, hit_title)
        if score > best_score:
            best_score = score
            best_result = result

    if best_result is None or best_score < 0.6:
        return None

    source_authors_raw = best_result.get("author", [])
    source_authors: list[str] = []
    if isinstance(source_authors_raw, list):
        for a in source_authors_raw:
            if isinstance(a, dict):
                first = a.get("first", "")
                last = a.get("last", "")
                name = f"{first} {last}".strip()
                if name:
                    source_authors.append(name)
            elif isinstance(a, str):
                source_authors.append(a)

    acl_id = best_result.get("id", "")
    acl_url = f"https://aclanthology.org/{acl_id}" if acl_id else ""

    is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)

    return AuthorCheckResult(
        entry_id=entry.entry_id,
        source=Source.ACL,
        bib_authors=entry.authors,
        source_authors=source_authors,
        is_match=is_match,
        missing_authors=missing,
        extra_authors=extra,
        order_wrong=order_wrong,
        source_url=acl_url,
        title_match_score=best_score,
    )


async def _scrape_acl_page(
    session: aiohttp.ClientSession,
    entry: BibEntry,
    page_url: str,
) -> Optional[AuthorCheckResult]:
    """Directly scrape an ACL Anthology paper page for author metadata.

    ACL pages have <meta name="citation_author" content="..."> tags.
    This works even when the ACL search API is unreachable.
    """
    import html

    try:
        async with session.get(
            page_url,
            headers={**HEADERS, "Accept": "text/html"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
    except Exception:
        return None

    # Extract citation metadata
    # ACL pages use: <meta name="citation_author" content="Chalamalasetti, Kranti">
    citation_authors = re.findall(
        r'<meta\s+name="citation_author"\s+content="([^"]+)"',
        text, re.IGNORECASE,
    )
    citation_title_match = re.search(
        r'<meta\s+name="citation_title"\s+content="([^"]+)"',
        text, re.IGNORECASE,
    )

    # Also try dc.Creator / dc.Title
    if not citation_authors:
        dc_authors = re.findall(
            r'<meta\s+name="[^"]*dc\.creator[^"]*"\s+content="([^"]+)"',
            text, re.IGNORECASE,
        )
        citation_authors = dc_authors

    if not citation_title_match:
        citation_title_match = re.search(
            r'<meta\s+name="[^"]*dc\.title[^"]*"\s+content="([^"]+)"',
            text, re.IGNORECASE,
        )

    page_title = ""
    if citation_title_match:
        page_title = html.unescape(citation_title_match.group(1).strip())

    # Verify title match
    title_score = _title_similarity(entry.title, page_title) if page_title else 0.0

    if not citation_authors or title_score < 0.4:
        return None

    source_authors = [html.unescape(a.strip()) for a in citation_authors if a.strip()]

    is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)

    return AuthorCheckResult(
        entry_id=entry.entry_id,
        source=Source.ACL,
        bib_authors=entry.authors,
        source_authors=source_authors,
        is_match=is_match,
        missing_authors=missing,
        extra_authors=extra,
        order_wrong=order_wrong,
        source_url=page_url,
        title_match_score=title_score,
    )


# ---------------------------------------------------------------------------
# ACM DL (scrape search page)
# ---------------------------------------------------------------------------

async def _query_acm(
    session: aiohttp.ClientSession,
    entry: BibEntry,
) -> Optional[AuthorCheckResult]:
    """Search ACM Digital Library for a paper and compare authors.

    Uses ACM's search page and parses results. This is best-effort
    since ACM doesn't have a simple public API.
    """
    if not entry.title:
        return None

    query = entry.title.strip("{} ")
    url = f"https://dl.acm.org/action/doSearch?AllField={aiohttp.helpers.quote(query)}&pageSize=5"

    try:
        async with session.get(
            url,
            headers={**HEADERS, "Accept": "text/html,application/json"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
    except Exception:
        return None

    # Try to extract author info from the page
    # ACM often puts author names in <span class="hlFld-ContribAuthor"> or similar
    # This is a best-effort extraction
    author_patterns = [
        r'<span[^>]*class="[^"]*author[^"]*"[^>]*>(.*?)</span>',
        r'<a[^>]*class="[^"]*author[^"]*"[^>]*>(.*?)</a>',
        r'"dc:creator"\s*:\s*"([^"]+)"',
        r'<meta\s+name="dc\.Creator"\s+content="([^"]+)"',
    ]

    source_authors: list[str] = []
    for pattern in author_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE | re.DOTALL)
        if matches:
            for m in matches:
                m = re.sub(r"<[^>]+>", "", m).strip()
                if m:
                    source_authors.append(m)
            break

    if not source_authors:
        return None

    is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)

    # Sanity check: reject if no bib author overlaps with scraped authors
    # (ACM search page <title> tag is "Search | ACM DL", not paper title)
    if entry.authors:
        bib_sets = [_author_to_token_set(a) for a in entry.authors]
        src_sets = [_author_to_token_set(a) for a in source_authors]
        if not any(_token_overlap_score(b, s) >= 0.5 for b in bib_sets for s in src_sets):
            return None

    return AuthorCheckResult(
        entry_id=entry.entry_id,
        source=Source.ACM,
        bib_authors=entry.authors,
        source_authors=source_authors,
        is_match=is_match,
        missing_authors=missing,
        extra_authors=extra,
        order_wrong=order_wrong,
        source_url=url,
        title_match_score=0.5,
    )


# ---------------------------------------------------------------------------
# NeurIPS Proceedings
# ---------------------------------------------------------------------------

async def _query_neurips(
    session: aiohttp.ClientSession,
    entry: BibEntry,
) -> Optional[AuthorCheckResult]:
    """Search NeurIPS Proceedings for a paper and compare authors.

    Uses the NeurIPS proceedings API (api.neurips.cc) for structured data,
    falling back to HTML scraping of proceedings.neurips.cc.
    """
    if not entry.title:
        return None

    query = entry.title.strip("{} ")

    # Try the NeurIPS public API first
    api_url = f"https://api.neurips.cc/public/v1/search?query={aiohttp.helpers.quote(query)}&limit=5"
    try:
        async with session.get(api_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return _parse_neurips_api(data, entry)
    except Exception:
        pass

    # Fallback: scrape proceedings.neurips.cc search page
    search_url = f"https://proceedings.neurips.cc/papers/search?query={aiohttp.helpers.quote(query)}"
    try:
        async with session.get(
            search_url,
            headers={**HEADERS, "Accept": "text/html"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 200:
                text = await resp.text()
                return _parse_neurips_html(text, entry, search_url)
    except Exception:
        pass

    return None


def _parse_neurips_api(data: dict, entry: BibEntry) -> Optional[AuthorCheckResult]:
    """Parse NeurIPS API JSON response."""
    results = data.get("results", data.get("data", data.get("papers", [])))
    if isinstance(results, dict):
        results = results.get("papers", results.get("results", []))
    if not isinstance(results, list) or not results:
        return None

    best_score = 0.0
    best_paper = None

    for paper in results:
        if not isinstance(paper, dict):
            continue
        hit_title = paper.get("title", "")
        score = _title_similarity(entry.title, hit_title)
        if score > best_score:
            best_score = score
            best_paper = paper

    if best_paper is None or best_score < 0.6:
        return None

    # Extract authors
    raw_authors = best_paper.get("authors", best_paper.get("author", []))
    source_authors = _extract_author_list(raw_authors)

    if not source_authors:
        return None

    paper_id = best_paper.get("id", best_paper.get("paper_id", ""))
    paper_url = best_paper.get("url", best_paper.get("abstract_url", ""))
    if not paper_url and paper_id:
        paper_url = f"https://proceedings.neurips.cc/paper_files/paper/{paper_id}"

    is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)

    return AuthorCheckResult(
        entry_id=entry.entry_id,
        source=Source.NEURIPS,
        bib_authors=entry.authors,
        source_authors=source_authors,
        is_match=is_match,
        missing_authors=missing,
        extra_authors=extra,
        order_wrong=order_wrong,
        source_url=paper_url,
        title_match_score=best_score,
    )


def _parse_neurips_html(html: str, entry: BibEntry, search_url: str) -> Optional[AuthorCheckResult]:
    """Parse NeurIPS proceedings HTML search results."""
    # Try to find paper blocks in the results page
    # NeurIPS proceedings pages have structured author lists
    # Look for author patterns in meta tags, JSON-LD, or structured divs

    # Try JSON-LD structured data
    ld_pattern = r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>'
    for match in re.findall(ld_pattern, html, re.DOTALL | re.IGNORECASE):
        try:
            import json
            ld = json.loads(match)
            if isinstance(ld, dict):
                title = ld.get("name", ld.get("headline", ""))
                if _title_similarity(entry.title, title) < 0.5:
                    continue
                authors = ld.get("author", [])
                source_authors = _extract_author_list(authors)
                if source_authors:
                    is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)
                    return AuthorCheckResult(
                        entry_id=entry.entry_id,
                        source=Source.NEURIPS,
                        bib_authors=entry.authors,
                        source_authors=source_authors,
                        is_match=is_match,
                        missing_authors=missing,
                        extra_authors=extra,
                        order_wrong=order_wrong,
                        source_url=search_url,
                        title_match_score=_title_similarity(entry.title, title),
                    )
        except (json.JSONDecodeError, TypeError):
            continue

    # Try meta author tags
    author_meta = re.findall(
        r'<meta\s+name="[^"]*author[^"]*"\s+content="([^"]+)"',
        html, re.IGNORECASE,
    )
    if author_meta:
        source_authors: list[str] = []
        for a in author_meta:
            # Meta author might be semicolon-separated
            for part in re.split(r"[;,]", a):
                part = part.strip()
                if part:
                    source_authors.append(part)
        if source_authors and _title_similarity(entry.title, _extract_html_title(html)) > 0.4:
            is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)
            return AuthorCheckResult(
                entry_id=entry.entry_id,
                source=Source.NEURIPS,
                bib_authors=entry.authors,
                source_authors=source_authors,
                is_match=is_match,
                missing_authors=missing,
                extra_authors=extra,
                order_wrong=order_wrong,
                source_url=search_url,
                title_match_score=0.5,
            )

    return None


# ---------------------------------------------------------------------------
# CVPR / CVF Open Access
# ---------------------------------------------------------------------------

async def _query_cvpr(
    session: aiohttp.ClientSession,
    entry: BibEntry,
) -> Optional[AuthorCheckResult]:
    """Search CVF Open Access (CVPR/ICCV/WACV) for a paper and compare authors.

    Uses openaccess.thecvf.com search and paper pages.
    """
    if not entry.title:
        return None

    query = entry.title.strip("{} ")

    # CVF Open Access search
    search_url = f"https://openaccess.thecvf.com/search?query={aiohttp.helpers.quote(query)}"
    try:
        async with session.get(
            search_url,
            headers={**HEADERS, "Accept": "text/html"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
    except Exception:
        return None

    return await _parse_cvf_html(text, entry, search_url, session)


async def _parse_cvf_html(
    html: str,
    entry: BibEntry,
    search_url: str,
    session: aiohttp.ClientSession,
) -> Optional[AuthorCheckResult]:
    """Parse CVF Open Access HTML search results and paper pages."""
    # CVF search results link to paper pages like:
    # /content/CVPR2023/html/Author_Paper_Title_CVPR_2023_paper.html

    # Find paper links in search results
    paper_link_pattern = r'<a[^>]*href="(/content/[^"]+\.html)"[^>]*>(.*?)</a>'
    paper_matches = re.findall(paper_link_pattern, html, re.DOTALL | re.IGNORECASE)

    best_score = 0.0
    best_link = None

    for link, link_text in paper_matches:
        clean_text = re.sub(r"<[^>]+>", "", link_text).strip()
        score = _title_similarity(entry.title, clean_text)
        if score > best_score:
            best_score = score
            best_link = link

    if best_link is None or best_score < 0.5:
        # Also try meta tags
        return _parse_cvf_meta(html, entry, search_url)

    # Fetch the paper page for detailed author info
    paper_url = f"https://openaccess.thecvf.com{best_link}"
    try:
        async with session.get(
            paper_url,
            headers={**HEADERS, "Accept": "text/html"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 200:
                paper_html = await resp.text()
                result = _parse_cvf_paper_page(paper_html, entry, paper_url)
                if result:
                    return result
    except Exception:
        pass

    return _parse_cvf_meta(html, entry, search_url)


def _parse_cvf_paper_page(
    html: str,
    entry: BibEntry,
    paper_url: str,
) -> Optional[AuthorCheckResult]:
    """Parse a CVF paper detail page for authors."""
    # CVF pages have authors in: <div id="authors"> or <meta name="citation_author">
    source_authors: list[str] = []

    # Method 1: citation_author meta tags (most reliable)
    citation_authors = re.findall(
        r'<meta\s+name="citation_author"\s+content="([^"]+)"',
        html, re.IGNORECASE,
    )
    if citation_authors:
        source_authors = [a.strip() for a in citation_authors if a.strip()]
        # Also get the paper title from meta
        title_match = re.search(
            r'<meta\s+name="citation_title"\s+content="([^"]+)"',
            html, re.IGNORECASE,
        )
        page_title = title_match.group(1) if title_match else ""
        title_score = _title_similarity(entry.title, page_title)

        if source_authors and title_score > 0.4:
            is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)
            return AuthorCheckResult(
                entry_id=entry.entry_id,
                source=Source.CVPR,
                bib_authors=entry.authors,
                source_authors=source_authors,
                is_match=is_match,
                missing_authors=missing,
                extra_authors=extra,
                order_wrong=order_wrong,
                source_url=paper_url,
                title_match_score=title_score,
            )

    # Method 2: dc.Creator meta
    dc_authors = re.findall(
        r'<meta\s+name="[^"]*dc\.Creator[^"]*"\s+content="([^"]+)"',
        html, re.IGNORECASE,
    )
    if dc_authors:
        source_authors = [a.strip() for a in dc_authors if a.strip()]
        if source_authors:
            is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)
            return AuthorCheckResult(
                entry_id=entry.entry_id,
                source=Source.CVPR,
                bib_authors=entry.authors,
                source_authors=source_authors,
                is_match=is_match,
                missing_authors=missing,
                extra_authors=extra,
                order_wrong=order_wrong,
                source_url=paper_url,
                title_match_score=0.5,
            )

    return None


def _parse_cvf_meta(
    html: str,
    entry: BibEntry,
    search_url: str,
) -> Optional[AuthorCheckResult]:
    """Fallback: parse CVF search results page meta tags."""
    source_authors: list[str] = []

    citation_authors = re.findall(
        r'<meta\s+name="citation_author"\s+content="([^"]+)"',
        html, re.IGNORECASE,
    )
    if citation_authors:
        source_authors = [a.strip() for a in citation_authors if a.strip()]

    if not source_authors:
        dc_authors = re.findall(
            r'<meta\s+name="[^"]*dc\.Creator[^"]*"\s+content="([^"]+)"',
            html, re.IGNORECASE,
        )
        source_authors = [a.strip() for a in dc_authors if a.strip()]

    if source_authors and _title_similarity(entry.title, _extract_html_title(html)) > 0.3:
        is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)
        return AuthorCheckResult(
            entry_id=entry.entry_id,
            source=Source.CVPR,
            bib_authors=entry.authors,
            source_authors=source_authors,
            is_match=is_match,
            missing_authors=missing,
            extra_authors=extra,
            order_wrong=order_wrong,
            source_url=search_url,
            title_match_score=0.5,
        )

    return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _extract_author_list(raw_authors) -> list[str]:
    """Normalize various author list formats into a flat list of strings."""
    result: list[str] = []
    if isinstance(raw_authors, str):
        # Comma or semicolon separated
        for part in re.split(r"[;,]", raw_authors):
            part = part.strip()
            if part:
                result.append(part)
    elif isinstance(raw_authors, list):
        for a in raw_authors:
            if isinstance(a, str):
                result.append(a.strip())
            elif isinstance(a, dict):
                # Try common keys: name, text, full_name, first+last
                name = a.get("name", a.get("text", a.get("full_name", "")))
                if not name:
                    first = a.get("first", a.get("given", a.get("first_name", "")))
                    last = a.get("last", a.get("family", a.get("last_name", "")))
                    name = f"{first} {last}".strip()
                if name:
                    result.append(name)
    elif isinstance(raw_authors, dict):
        # {"author": [...]} pattern from DBLP
        inner = raw_authors.get("author", raw_authors.get("authors", []))
        return _extract_author_list(inner)
    return [a for a in result if a]


def _extract_html_title(html: str) -> str:
    """Extract the page title from HTML."""
    match = re.search(r"<title>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if match:
        return re.sub(r"<[^>]+>", "", match.group(1)).strip()
    return ""


# ---------------------------------------------------------------------------
# Semantic Scholar API — reliable free fallback
# ---------------------------------------------------------------------------

_ss_auth_semaphore = asyncio.Semaphore(1)
_ss_auth_last_call = 0.0
_SS_AUTH_MIN_INTERVAL = 1.1  # seconds between Semantic Scholar author API calls


async def _query_semantic_scholar(
    session: aiohttp.ClientSession,
    entry: BibEntry,
) -> Optional[AuthorCheckResult]:
    """Search Semantic Scholar for a paper and compare authors. Retries once on failure."""
    if not entry.title:
        return None

    global _ss_auth_last_call

    for attempt in range(2):
        async with _ss_auth_semaphore:
            elapsed = time.time() - _ss_auth_last_call
            if elapsed < _SS_AUTH_MIN_INTERVAL:
                await asyncio.sleep(_SS_AUTH_MIN_INTERVAL - elapsed)

            query = entry.title.strip("{} ")[:200]
            url = (
                f"https://api.semanticscholar.org/graph/v1/paper/search"
                f"?query={aiohttp.helpers.quote(query)}&limit=5"
                f"&fields=title,authors,year,venue"
            )
            data = None

            try:
                async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    _ss_auth_last_call = time.time()
                    if resp.status == 429:
                        await asyncio.sleep(3.0)
                        _ss_auth_last_call = time.time()
                    elif resp.status == 200:
                        data = await resp.json()
            except Exception:
                _ss_auth_last_call = time.time()

        if data is None:
            if attempt == 0:
                await asyncio.sleep(2.0)
            continue

        papers = data.get("data", [])
        if not papers:
            if attempt == 0:
                await asyncio.sleep(2.0)
            continue

        best_score = 0.0
        best_paper = None
        for paper in papers:
            score = _title_similarity(entry.title, paper.get("title", ""))
            if score > best_score:
                best_score = score
                best_paper = paper

        if best_paper is None or best_score < 0.7:
            if attempt == 0:
                await asyncio.sleep(2.0)
            continue

        raw_authors = best_paper.get("authors", [])
        source_authors: list[str] = []
        for a in raw_authors:
            name = a.get("name", "")
            if name:
                source_authors.append(name)

        if not source_authors:
            return None

        if best_score < 0.9 and entry.authors:
            bib_sets = [_author_to_token_set(a) for a in entry.authors]
            src_sets = [_author_to_token_set(a) for a in source_authors]
            if not any(_token_overlap_score(b, s) >= 0.5 for b in bib_sets for s in src_sets):
                return None

        paper_id = best_paper.get("paperId", "")
        ss_url = f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else ""
        ss_year = str(best_paper.get("year", "") or "")
        ss_venue = str(best_paper.get("venue", "") or "")

        is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)
        year_ok = _check_year(entry.year, ss_year)
        venue_ok = _venue_similarity(
            entry.booktitle or entry.journal, ss_venue
        )

        return AuthorCheckResult(
            entry_id=entry.entry_id,
            source=Source.SEMANTIC_SCHOLAR,
            bib_authors=entry.authors,
            source_authors=source_authors,
            is_match=is_match and year_ok and venue_ok,
            missing_authors=missing,
            extra_authors=extra,
            order_wrong=order_wrong,
            year_mismatch=not year_ok,
            source_year=ss_year,
            venue_mismatch=not venue_ok,
            source_venue=ss_venue,
            source_url=ss_url,
            title_match_score=best_score,
        )

    return None


# ---------------------------------------------------------------------------
# Crossref API — comprehensive DOI-based metadata
# ---------------------------------------------------------------------------

_crossref_semaphore = asyncio.Semaphore(1)
_crossref_last_call = 0.0
_CROSSREF_MIN_INTERVAL = 0.3  # Crossref is more generous with rate limits


async def _query_crossref(
    session: aiohttp.ClientSession,
    entry: BibEntry,
) -> Optional[AuthorCheckResult]:
    """Search Crossref for a paper by title or DOI and compare authors.

    Crossref has excellent coverage of published papers across all fields.
    Free API, no key required for polite usage.

    Tries DOI exact lookup first. If that fails (e.g., DataCite DOIs from
    arXiv), falls back to title search.
    """
    global _crossref_last_call
    async with _crossref_semaphore:
        elapsed = time.time() - _crossref_last_call
        if elapsed < _CROSSREF_MIN_INTERVAL:
            await asyncio.sleep(_CROSSREF_MIN_INTERVAL - elapsed)

        data = None
        is_doi_lookup = False

        # --- Strategy 1: DOI exact lookup ---
        if entry.doi:
            doi_query = entry.doi.strip()
            if doi_query.startswith("http"):
                doi_query = doi_query.split("doi.org/", 1)[-1] if "doi.org/" in doi_query else doi_query
            import urllib.parse
            safe_doi = urllib.parse.quote(doi_query, safe="/")
            url = f"https://api.crossref.org/works/{safe_doi}"
            is_doi_lookup = True

            try:
                async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    _crossref_last_call = time.time()
                    if resp.status == 200:
                        data = await resp.json()
                    elif resp.status == 429:
                        await asyncio.sleep(2.0)
            except Exception:
                _crossref_last_call = time.time()

        # --- Strategy 2: Title search (fallback if DOI failed or no DOI) ---
        if data is None and entry.title:
            query = entry.title.strip("{} ")[:200]
            url = f"https://api.crossref.org/works?query.title={aiohttp.helpers.quote(query)}&rows=5"
            is_doi_lookup = False

            # Re-acquire rate limit for second call
            elapsed = time.time() - _crossref_last_call
            if elapsed < _CROSSREF_MIN_INTERVAL:
                await asyncio.sleep(_CROSSREF_MIN_INTERVAL - elapsed)

            try:
                async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    _crossref_last_call = time.time()
                    if resp.status == 200:
                        data = await resp.json()
                    elif resp.status == 429:
                        await asyncio.sleep(2.0)
            except Exception:
                _crossref_last_call = time.time()

        if data is None:
            return None

    # Parse response
    items: list = []
    if is_doi_lookup and data.get("status") == "ok":
        msg = data.get("message", {})
        if msg:
            items = [msg]
    else:
        msg = data.get("message", {})
        items = msg.get("items", [])

    if not items:
        return None

    best_score = 0.0
    best_item = None

    for item in items:
        hit_title = item.get("title", [""])[0] if item.get("title") else ""
        score = _title_similarity(entry.title, hit_title) if entry.title else (1.0 if entry.doi else 0.0)
        if score > best_score:
            best_score = score
            best_item = item

    if best_item is None or best_score < 0.6:
        return None

    # Extract authors
    raw_authors = best_item.get("author", [])
    source_authors: list[str] = []
    for a in raw_authors:
        given = a.get("given", "")
        family = a.get("family", "")
        name = f"{given} {family}".strip()
        if not name:
            name = a.get("name", "")
        if name:
            source_authors.append(name)

    if not source_authors:
        return None

    # Sanity check: if title-only search with medium score and zero author
    # overlap, this is likely a false positive from Crossref.
    if not is_doi_lookup and best_score < 0.9 and entry.authors:
        bib_sets = [_author_to_token_set(a) for a in entry.authors]
        src_sets = [_author_to_token_set(a) for a in source_authors]
        any_overlap = any(
            _token_overlap_score(b, s) >= 0.5
            for b in bib_sets for s in src_sets
        )
        if not any_overlap:
            return None  # reject wrong-paper match

    cr_doi = best_item.get("DOI", "")
    cr_url = f"https://doi.org/{cr_doi}" if cr_doi else ""

    # Extract year from Crossref (try multiple date fields)
    cr_year = ""
    for date_field in ("published-print", "published-online", "issued", "created"):
        date_parts = best_item.get(date_field, {}).get("date-parts", [[None]])[0]
        if date_parts and date_parts[0] is not None:
            cr_year = str(date_parts[0])
            break

    is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)
    year_ok = _check_year(entry.year, cr_year)
    cr_venue = (best_item.get("container-title") or [""])[0] if best_item.get("container-title") else ""
    venue_ok = _venue_similarity(
        entry.booktitle or entry.journal, cr_venue
    )

    return AuthorCheckResult(
        entry_id=entry.entry_id,
        source=Source.CROSSREF,
        bib_authors=entry.authors,
        source_authors=source_authors,
        is_match=is_match and year_ok and venue_ok,
        missing_authors=missing,
        extra_authors=extra,
        order_wrong=order_wrong,
        year_mismatch=not year_ok,
        source_year=cr_year,
        venue_mismatch=not venue_ok,
        source_venue=cr_venue,
        source_url=cr_url,
        title_match_score=best_score,
    )


# ---------------------------------------------------------------------------
# arXiv API — for arXiv preprints (resolves DataCite DOIs)
# ---------------------------------------------------------------------------

_arxiv_semaphore = asyncio.Semaphore(1)
_arxiv_last_call = 0.0
_ARXIV_MIN_INTERVAL = 0.5


async def _query_arxiv(
    session: aiohttp.ClientSession,
    entry: BibEntry,
) -> Optional[AuthorCheckResult]:
    """Search arXiv for a paper by title. Handles DataCite DOIs (10.48550/arXiv.*)."""
    import xml.etree.ElementTree as ET

    if not entry.title:
        return None

    global _arxiv_last_call
    async with _arxiv_semaphore:
        elapsed = time.time() - _arxiv_last_call
        if elapsed < _ARXIV_MIN_INTERVAL:
            await asyncio.sleep(_ARXIV_MIN_INTERVAL - elapsed)

        query = entry.title.strip("{} ")[:200]
        url = (
            f"http://export.arxiv.org/api/query"
            f"?search_query=ti:{aiohttp.helpers.quote(query)}"
            f"&max_results=3"
        )

        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                _arxiv_last_call = time.time()
                if resp.status != 200:
                    return None
                text = await resp.text()
        except Exception:
            _arxiv_last_call = time.time()
            return None

    # Parse Atom XML response
    try:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(text)
        entries_elem = root.findall("atom:entry", ns)
    except ET.ParseError:
        return None

    best_score = 0.0
    best_entry_elem = None

    for elem in entries_elem:
        title_elem = elem.find("atom:title", ns)
        hit_title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
        score = _title_similarity(entry.title, hit_title)
        if score > best_score:
            best_score = score
            best_entry_elem = elem

    if best_entry_elem is None or best_score < 0.7:
        return None

    # Extract authors
    source_authors: list[str] = []
    for author_elem in best_entry_elem.findall("atom:author", ns):
        name_elem = author_elem.find("atom:name", ns)
        if name_elem is not None and name_elem.text:
            source_authors.append(name_elem.text.strip())

    if not source_authors:
        return None

    # Extract year
    arxiv_year = ""
    published_elem = best_entry_elem.find("atom:published", ns)
    if published_elem is not None and published_elem.text:
        arxiv_year = published_elem.text[:4]

    # Extract arxiv ID for URL
    arxiv_url = ""
    id_elem = best_entry_elem.find("atom:id", ns)
    if id_elem is not None and id_elem.text:
        arxiv_id = id_elem.text.split("/abs/")[-1] if "/abs/" in id_elem.text else id_elem.text
        arxiv_url = f"https://arxiv.org/abs/{arxiv_id}"

    is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)
    year_ok = _check_year(entry.year, arxiv_year)

    return AuthorCheckResult(
        entry_id=entry.entry_id,
        source=Source.ARXIV,
        bib_authors=entry.authors,
        source_authors=source_authors,
        is_match=is_match and year_ok,
        missing_authors=missing,
        extra_authors=extra,
        order_wrong=order_wrong,
        year_mismatch=not year_ok,
        source_year=arxiv_year,
        source_url=arxiv_url,
        title_match_score=best_score,
    )


# ---------------------------------------------------------------------------
# OpenAlex API — last-resort fallback, very comprehensive
# ---------------------------------------------------------------------------

_openalex_semaphore = asyncio.Semaphore(1)
_openalex_last_call = 0.0
_OPENALEX_MIN_INTERVAL = 0.1


async def _query_openalex(
    session: aiohttp.ClientSession,
    entry: BibEntry,
) -> Optional[AuthorCheckResult]:
    """Search OpenAlex for a paper by title or DOI. Last-resort fallback."""
    if not entry.title:
        return None

    global _openalex_last_call
    async with _openalex_semaphore:
        elapsed = time.time() - _openalex_last_call
        if elapsed < _OPENALEX_MIN_INTERVAL:
            await asyncio.sleep(_OPENALEX_MIN_INTERVAL - elapsed)

        if entry.doi:
            doi_query = entry.doi.strip()
            if doi_query.startswith("http"):
                doi_query = doi_query.split("doi.org/", 1)[-1] if "doi.org/" in doi_query else doi_query
            import urllib.parse
            url = f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi_query, safe='/')}"
        else:
            query = entry.title.strip("{} ")[:200]
            url = f"https://api.openalex.org/works?search={aiohttp.helpers.quote(query)}&per_page=5"

        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                _openalex_last_call = time.time()
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception:
            _openalex_last_call = time.time()
            return None

    works = [data] if (entry.doi and data.get("id")) else data.get("results", [])
    if not works:
        return None

    best_score = 0.0
    best_work = None
    for work in works:
        score = _title_similarity(entry.title, work.get("title", "")) if entry.title else 1.0
        if score > best_score:
            best_score = score
            best_work = work

    if best_work is None or best_score < 0.6:
        return None

    source_authors: list[str] = []
    for a in best_work.get("authorships", []):
        name = a.get("author", {}).get("display_name", "")
        if name:
            source_authors.append(name)

    if not source_authors:
        return None

    if best_score < 0.9 and entry.authors:
        bib_sets = [_author_to_token_set(a) for a in entry.authors]
        src_sets = [_author_to_token_set(a) for a in source_authors]
        if not any(_token_overlap_score(b, s) >= 0.5 for b in bib_sets for s in src_sets):
            return None

    oa_year = str(best_work.get("publication_year", "") or "")
    oa_url = best_work.get("doi", "")
    if oa_url and not oa_url.startswith("http"):
        oa_url = f"https://doi.org/{oa_url}"

    is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)
    year_ok = _check_year(entry.year, oa_year)

    return AuthorCheckResult(
        entry_id=entry.entry_id,
        source=Source.OPENALEX,
        bib_authors=entry.authors,
        source_authors=source_authors,
        is_match=is_match and year_ok,
        missing_authors=missing,
        extra_authors=extra,
        order_wrong=order_wrong,
        year_mismatch=not year_ok,
        source_year=oa_year,
        source_url=oa_url,
        title_match_score=best_score,
    )


# ---------------------------------------------------------------------------
# OpenReview API — for ICLR/OpenReview papers without DOIs
# ---------------------------------------------------------------------------

_openreview_semaphore = asyncio.Semaphore(1)
_openreview_last_call = 0.0
_OPENREVIEW_MIN_INTERVAL = 0.5


async def _query_openreview(
    session: aiohttp.ClientSession,
    entry: BibEntry,
) -> Optional[AuthorCheckResult]:
    """Search OpenReview for ICLR/NeurIPS papers. Handles papers without DOIs."""
    if not entry.title:
        return None

    # Extract forum ID from OpenReview URL if available
    forum_id = None
    if entry.url and "openreview.net/forum?id=" in entry.url:
        forum_id = entry.url.split("id=")[-1].split("&")[0].split("#")[0]
    elif entry.url and "openreview.net/pdf?id=" in entry.url:
        forum_id = entry.url.split("id=")[-1].split("&")[0].split("#")[0]

    global _openreview_last_call
    async with _openreview_semaphore:
        elapsed = time.time() - _openreview_last_call
        if elapsed < _OPENREVIEW_MIN_INTERVAL:
            await asyncio.sleep(_OPENREVIEW_MIN_INTERVAL - elapsed)

        if forum_id:
            # Direct lookup by forum ID
            url = f"https://api.openreview.net/notes?forum={forum_id}&limit=1"
        else:
            # Title search
            query = entry.title.strip("{} ")[:200]
            url = f"https://api.openreview.net/notes/search?term={aiohttp.helpers.quote(query)}&limit=3"

        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                _openreview_last_call = time.time()
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception:
            _openreview_last_call = time.time()
            return None

    notes = data.get("notes", [])
    if not notes:
        return None

    # The first note is usually the submission
    note = notes[0]
    content = note.get("content", {})

    # Extract title from note
    note_title = content.get("title", {}).get("value", "")
    title_score = _title_similarity(entry.title, note_title) if note_title else 0.0
    if title_score < 0.6:
        return None

    # Extract authors
    source_authors: list[str] = []
    authors_raw = content.get("authors", {}).get("value", [])
    if isinstance(authors_raw, list):
        source_authors = [str(a).strip() for a in authors_raw if a]

    if not source_authors:
        return None  # authorids are profile IDs, not human-readable names

    forum = note.get("forum", "")
    or_url = f"https://openreview.net/forum?id={forum}" if forum else ""

    is_match, missing, extra, order_wrong = _compare_authors(entry.authors, source_authors)

    return AuthorCheckResult(
        entry_id=entry.entry_id,
        source=Source.OPENREVIEW,
        bib_authors=entry.authors,
        source_authors=source_authors,
        is_match=is_match,
        missing_authors=missing,
        extra_authors=extra,
        order_wrong=order_wrong,
        source_url=or_url,
        title_match_score=title_score,
    )


# ---------------------------------------------------------------------------
# Orchestrator: try all sources, return best result
# ---------------------------------------------------------------------------

async def _verify_single_entry(
    session: aiohttp.ClientSession,
    entry: BibEntry,
    verbose: bool = False,
) -> AuthorCheckResult:
    """Try all sources for a single entry, return the best result."""
    # Priority: venue-specific sources first, then general aggregators.
    # NeurIPS/CVPR have canonical author order; Crossref sometimes doesn't.
    # DBLP → Semantic Scholar → NeurIPS → CVPR → arXiv → Crossref → ACL → ACM → OpenAlex → OpenReview
    checkers = [
        ("dblp", _query_dblp),
        ("semantic_scholar", _query_semantic_scholar),
        ("neurips", _query_neurips),
        ("cvpr", _query_cvpr),
        ("arxiv", _query_arxiv),
        ("crossref", _query_crossref),
        ("acl", _query_acl),
        ("acm", _query_acm),
        ("openalex", _query_openalex),
        ("openreview", _query_openreview),
    ]

    for name, checker in checkers:
        try:
            result = await checker(session, entry)
            if result is not None and result.source is not None:
                if verbose:
                    print(f"    ✓ {entry.entry_id[:40]} → matched {name} (title_score={result.title_match_score:.2f})")
                return result
            elif verbose and result is not None:
                print(f"    - {entry.entry_id[:40]} → {name}: no match (score={result.title_match_score:.2f})")
        except Exception as e:
            if verbose:
                print(f"    ✗ {entry.entry_id[:40]} → {name}: exception: {e}")

    # Could not find in any known source
    if verbose:
        print(f"    ? {entry.entry_id[:40]} → no source found")
    return AuthorCheckResult(
        entry_id=entry.entry_id,
        source=None,
        bib_authors=entry.authors,
        source_authors=[],
        is_match=None,
        error="Could not find paper in any of 10 sources (dblp, Semantic Scholar, NeurIPS, CVPR, arXiv, Crossref, ACL, ACM, OpenAlex, OpenReview)",
    )


async def verify_authors(
    entries: list[BibEntry],
    concurrency: int = 5,
    verbose: bool = False,
) -> list[AuthorCheckResult]:
    """Verify authors for all entries against known sources.

    Args:
        entries: List of parsed BibEntry objects.
        concurrency: Max simultaneous API calls.
        verbose: Print debug information about each API call.

    Returns:
        List of AuthorCheckResult, one per entry.
    """
    connector = aiohttp.TCPConnector(limit=concurrency, ssl=False)
    async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
        tasks = [_verify_single_entry(session, entry, verbose=verbose) for entry in entries]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    final: list[AuthorCheckResult] = []
    for i, r in enumerate(results):
        if isinstance(r, AuthorCheckResult):
            final.append(r)
        elif isinstance(r, Exception):
            final.append(AuthorCheckResult(
                entry_id=entries[i].entry_id if i < len(entries) else "unknown",
                source=None,
                is_match=None,
                error=f"Exception: {r}",
            ))
    return final
