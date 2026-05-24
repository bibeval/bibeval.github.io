# BibEval — Bibliography Correctness Evaluator

**Evaluates `.bib` files for wrong references: broken URLs, incorrect authors, wrong years, and hallucinated titles.**

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Data Flow](#2-data-flow)
3. [BibTeX Parser](#3-bibtex-parser)
4. [URL Validation](#4-url-validation)
5. [Author & Metadata Verification](#5-author--metadata-verification)
6. [Source-by-Source Reference](#6-source-by-source-reference)
7. [Author Comparison Algorithm](#7-author-comparison-algorithm)
8. [Report Output](#8-report-output)
9. [CLI Usage](#9-cli-usage)
10. [Known Limitations](#10-known-limitations)
11. [File Index](#11-file-index)

---

## 1. Architecture Overview

```
┌─────────────────┐
│   main.py (CLI) │
└────────┬────────┘
         │
┌────────▼──────────┐
│  Orchestrator     │  orchestrator.py
│  ┌──────────────┐ │
│  │ 1. Parse     │─┤ parser.py
│  │ 2. URL check │─┤ url_checker.py (async HTTP)
│  │ 3. Author    │─┤ author_verifier.py (10 sources)
│  │ 4. Report    │─┤ JSON + text output
│  └──────────────┘ │
└───────────────────┘
```

The tool runs three async pipelines in sequence:

| Step | What it does | Output |
|------|-------------|--------|
| **Parse** | Reads `.bib` file → extracts title, authors, DOI, URL, year per entry | `list[BibEntry]` |
| **URL check** | Validates URLs via HEAD→GET HTTP requests, retries on timeout | `list[UrlCheckResult]` |
| **Author verify** | Queries 10 sources by title/DOI, compares authors via token-set matching | `list[AuthorCheckResult]` |
| **Report** | Combines results → classifies entries as Wrong / Unverifiable / Correct | JSON or text |

---

## 2. Data Flow

```
.bib file
    │
    ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Parser                                                                │
│   bibtexparser → BibEntry{.entry_id, .title, .authors, .doi, .url,  │
│                           .year, .journal, .booktitle, .primary_url} │
└──────────┬───────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│ URL Checker (async, concurrent)                                       │
│   For each entry.primary_url:                                        │
│     HEAD → 2xx/3xx? → ✅ valid                                        │
│     HEAD → 403/429/503? → ⚠️ unknown (blocked, not broken)            │
│     HEAD → 404/410? → ❌ invalid                                      │
│     HEAD→timeout? → retry with +15s                                   │
│     HEAD fails → fallback to GET                                     │
│                                                                       │
│   403-blocked URLs: cross-check via Semantic Scholar title search     │
│   → if paper exists → upgrade to ✅                                   │
└──────────┬───────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Author Verifier (async, concurrent)                                   │
│   For each BibEntry, try sources in priority order:                  │
│                                                                       │
│   DBLP (rate-limited: 0.8s, 3 retries)                               │
│     ↓ no match                                                        │
│   Semantic Scholar (rate-limited: 1.1s, 2 retries)                   │
│     ↓ no match                                                        │
│   NeurIPS Proceedings (HTML scrape)                                   │
│     ↓ no match                                                        │
│   CVPR/CVF Open Access (HTML scrape)                                  │
│     ↓ no match                                                        │
│   arXiv API (Atom XML, for arXiv papers)                              │
│     ↓ no match                                                        │
│   Crossref (DOI lookup → fallback title search)                       │
│     ↓ no match                                                        │
│   ACL Anthology (page scrape if URL → fallback search API)            │
│     ↓ no match                                                        │
│   ACM Digital Library (HTML scrape)                                   │
│     ↓ no match                                                        │
│   OpenAlex (DOI → title search, last resort)                          │
│     ↓ no match                                                        │
│   OpenReview (forum ID → title search, for ICLR papers)              │
│                                                                       │
│   → First matching source wins, returns AuthorCheckResult             │
│   → No source → "Unverifiable"                                        │
└──────────┬───────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Classification Logic                                                  │
│                                                                       │
│   ❌ Wrong Reference (any of):                                        │
│     • URL returned 404/410                                            │
│     • Missing/extra authors vs source                                 │
│     • Author order differs from source (ALL positions checked)        │
│     • Year mismatch (±1yr tolerance)                                  │
│     • Title similarity < 0.85 (hallucinated/subtly-wrong title)      │
│                                                                       │
│   ⚠️ Unverifiable:                                                    │
│     • Not found in any of the 10 sources                              │
│     • May be hallucinated or extremely obscure                        │
│                                                                       │
│   ✅ Correct:                                                         │
│     • All checks passed                                               │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. BibTeX Parser

**File:** `bib_eval/parser.py`  
**Library:** `bibtexparser` v1.4+

Extracts structured data from each entry:

| Field | Source in `.bib` |
|-------|-----------------|
| `entry_id` | Citation key (e.g., `DBLP:conf/icra/LiangHXXHIFZ23`) |
| `entry_type` | `article`, `inproceedings`, `misc`, etc. |
| `title` | `title` field, outer braces stripped |
| `authors` | `author` field, split on ` and `, braces stripped |
| `doi` | `doi` field |
| `url` | `url` field |
| `year` | `year` field |
| `primary_url` | `url` if present, else `https://doi.org/{doi}` if DOI exists |

Author names are parsed by splitting on ` and ` and stripping `{` `}` wrappers. Comma-separated names (`"Kranti, Chalamalasetti"`) are preserved as-is for later normalization.

---

## 4. URL Validation

**File:** `bib_eval/url_checker.py`  
**Transport:** `aiohttp` with `ssl=False` and browser-mimicking headers

### Strategy

```
HEAD request → 2xx/3xx → ✅ valid
             → 403/429/503 → ⚠️ unknown (server blocks, not broken)
             → 404/410 → ❌ invalid
             → timeout → retry once with +15s
             → error → ❌ invalid

HEAD fails? → GET request (same logic)
```

DOIs (`doi.org`) get 40s timeout; regular URLs get 25s. One retry on timeout with +15s extra.

### 403-blocked URL fallback

When a URL returns 403 (server blocks automated access), the tool:
1. Searches **Semantic Scholar** by paper title (`api.semanticscholar.org`)
2. If found → URL is likely valid → upgraded to ✅
3. Also cross-references with author verification results — if DBLP/Crossref found the paper, the URL is confirmed valid.

---

## 5. Author & Metadata Verification

**File:** `bib_eval/author_verifier.py`

For each bib entry, queries sources sequentially. The **first source that finds the paper** wins.

### 5.1 Title search

All sources accept the bib entry's title, normalize it (lowercase, strip punctuation, collapse whitespace), and compute a similarity score against candidate results using `SequenceMatcher`.

```python
def _normalize_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()
```

Default match threshold: **0.6** (DBLP, Crossref), **0.7** (Semantic Scholar, arXiv).

### 5.2 Author comparison

See [§7 Author Comparison Algorithm](#7-author-comparison-algorithm) for full details.

### 5.3 Year matching

Uses ±1 year tolerance (handles arXiv preprints posted in year N, published at conference in year N+1):

```python
def _check_year(bib_year, source_year) -> bool:
    return abs(int(bib_year) - int(source_year)) <= 1
```

### 5.4 False-positive prevention

When title similarity is < 0.9, the tool verifies at least one author overlaps between bib and source before accepting the match. This prevents "different paper, similar title" false positives from Crossref/Semantic Scholar title searches.

---

## 6. Source-by-Source Reference

### 6.1 DBLP — Computer Science Bibliography

| Property | Value |
|----------|-------|
| **URL** | `https://dblp.org/search/publ/api?q={title}&format=json&h=5` |
| **Auth** | None |
| **Rate limit** | 1 concurrent, 0.8s interval, 3s backoff on 429 |
| **Retries** | Up to 3 |
| **Provides** | Authors (with disambiguation suffixes like `0001`), year, URL |
| **Match threshold** | Title similarity ≥ 0.6 |
| **Coverage** | CS conferences/journals: ICRA, ICLR, NeurIPS, ICML, CVPR, ACL, EMNLP, CoRL, RSS, etc. |

**Disambiguation suffix handling:** DBLP appends `0001`, `0002` to author names (e.g., `"Fei Xia 0002"`). These are stripped by `re.sub(r"\s+\d{4}$", "", name)` before comparison.

### 6.2 Semantic Scholar

| Property | Value |
|----------|-------|
| **URL** | `https://api.semanticscholar.org/graph/v1/paper/search?query={title}&limit=5&fields=title,authors,year` |
| **Auth** | None (free tier) |
| **Rate limit** | 1 concurrent, 1.1s interval, 3s backoff on 429 |
| **Retries** | Up to 2 |
| **Provides** | Authors (`.name`), year (`.year`), paper ID |
| **Match threshold** | Title similarity ≥ 0.7 |
| **Coverage** | Broad ML/AI/NLP coverage, strong on arXiv preprints and OpenReview papers |

### 6.3 NeurIPS Proceedings

| Property | Value |
|----------|-------|
| **Primary URL** | `https://api.neurips.cc/public/v1/search?query={title}&limit=5` |
| **Fallback URL** | `https://proceedings.neurips.cc/papers/search?query={title}` |
| **Method** | JSON API → HTML scrape with JSON-LD / meta tag extraction |
| **Provides** | Authors, year, paper URL |
| **Match threshold** | Title similarity ≥ 0.6 |
| **Coverage** | NeurIPS 1987–present |

### 6.4 CVPR / CVF Open Access

| Property | Value |
|----------|-------|
| **URL** | `https://openaccess.thecvf.com/search?query={title}` |
| **Method** | HTML scrape → follow paper link → extract `citation_author` meta tags |
| **Provides** | Authors, year |
| **Match threshold** | Title similarity ≥ 0.5 |
| **Coverage** | CVPR, ICCV, WACV, and other CVF conferences |

### 6.5 arXiv API

| Property | Value |
|----------|-------|
| **URL** | `http://export.arxiv.org/api/query?search_query=ti:{title}&max_results=3` |
| **Method** | Atom XML parsing |
| **Provides** | Authors (`<author><name>`), year (from `<published>`), arxiv ID |
| **Match threshold** | Title similarity ≥ 0.7 |
| **Coverage** | All arXiv papers (resolves DataCite DOIs that Crossref can't) |

### 6.6 Crossref

| Property | Value |
|----------|-------|
| **DOI URL** | `https://api.crossref.org/works/{doi}` (slashes NOT encoded) |
| **Title URL** | `https://api.crossref.org/works?query.title={title}&rows=5` |
| **Strategy** | DOI exact lookup first → if 404. Falls back to title search |
| **Provides** | Authors (`.given` + `.family`), year, DOI |
| **Match threshold** | Title similarity ≥ 0.6 |
| **Coverage** | All published papers with Crossref DOIs (most journals/conferences) |
| **Limitation** | Does NOT resolve DataCite DOIs (arXiv prefix `10.48550`) |

### 6.7 ACL Anthology

| Property | Value |
|----------|-------|
| **Primary method** | Direct page scrape if URL contains `aclanthology.org` |
| **Fallback** | `https://api.aclanthology.org/search?query={title}&limit=5` |
| **Page scrape** | Extracts `<meta name="citation_author">` and `<meta name="citation_title">` |
| **Provides** | Authors, title, ACL ID |
| **Match threshold** | Title similarity ≥ 0.4 (scrape) / 0.6 (API) |
| **Coverage** | ACL, EMNLP, NAACL, SIGDIAL, TACL, and all ACL-affiliated venues |

**Note:** The ACL search API may be unreachable on some networks (DNS failure). The page scraper works independently and bypasses this.

### 6.8 ACM Digital Library

| Property | Value |
|----------|-------|
| **URL** | `https://dl.acm.org/action/doSearch?AllField={title}&pageSize=5` |
| **Method** | HTML scrape → regex extract authors from `dc.creator` meta tags |
| **Verification** | Author-overlap sanity check (≥1 bib author must match a scraped author at ≥0.5 score) |
| **Coverage** | ACM conferences/journals |
| **Limitation** | Best-effort HTML scraping; ACM's HTML structure changes over time. Author-overlap gate prevents wrong-paper matches. |

### 6.9 OpenAlex

| Property | Value |
|----------|-------|
| **DOI URL** | `https://api.openalex.org/works/doi:{doi}` |
| **Title URL** | `https://api.openalex.org/works?search={title}&per_page=5` |
| **Provides** | Authors (`authorships[].author.display_name`), year, DOI |
| **Match threshold** | Title similarity ≥ 0.6 |
| **Coverage** | 250M+ works — all of Crossref + DataCite + more |
| **Role** | Last-resort comprehensive fallback |

### 6.10 OpenReview

| Property | Value |
|----------|-------|
| **Forum URL** | `https://api.openreview.net/notes?forum={id}&limit=1` (if URL has forum ID) |
| **Search URL** | `https://api.openreview.net/notes/search?term={title}&limit=3` |
| **Provides** | Authors (`content.authors.value`), forum ID |
| **Match threshold** | Title similarity ≥ 0.6 |
| **Coverage** | ICLR, NeurIPS (OpenReview-track), and other OpenReview venues |

---

## 7. Author Comparison Algorithm

**File:** `bib_eval/author_verifier.py` — `_compare_authors()`, `_author_to_token_set()`, `_token_overlap_score()`, `_normalize_author_name()`

### 7.1 Name Normalization Pipeline

```
Raw name: "Fei Xia 0002"
    │
    ├─ Strip braces:          "Fei Xia 0002"
    ├─ Strip DBLP suffix:     "Fei Xia"          (re.sub(r"\s+\d{4}$", ""))
    ├─ NFKD diacritics:       "Huttenrauch"      (ü→u, ğ→g, ö→o)
    ├─ German umlaut ASCII:   "Huttenrauch"      ((?<=[aou])e removed: Hue→Hu)
    ├─ Replace commas:        "Chalamalasetti Kranti" (Kranti, Chalamalasetti)
    ├─ Strip periods:         "F Xia"             (initials lose dots)
    └─ Collapse whitespace:   "fei xia"
```

### 7.2 Token Set Creation

Each normalized author name is split into word tokens:

```
"Fei Xia"      → {"fei", "xia"}
"F. Xia"       → {"f", "xia"}           (initials kept as single-char)
"Lucy X. Shi"  → {"lucy", "x", "shi"}
```

### 7.3 Token Overlap Score (Initial-Aware Jaccard)

```
Score = (exact_matches + initial_matches + initial_initial_matches) / union_size
```

**Exact matches:** Words identical in both sets (e.g., `"xia"` ↔ `"xia"`)

**Initial matches:** Single-char token `"f"` matches multi-char token `"fei"` if the longer starts with the initial. Each initial matches at most one name.

**Examples:**

| Bib | Source | Score | Result |
|-----|--------|-------|--------|
| `"Fei Xia"` → {"fei","xia"} | `"F. Xia"` → {"f","xia"} | (1+1+0)/3=0.67 | ✅ ≥0.5 |
| `"Leonidas Guibas"` → {"leonidas","guibas"} | `"Leonidas J. Guibas"` → {"leonidas","j","guibas"} | (2+0+0)/3=0.67 | ✅ |
| `"R. Lachmy"` → {"r","lachmy"} | `"Royi Lachmy"` → {"royi","lachmy"} | (1+1+0)/3=0.67 | ✅ |
| `"John Smith"` → {"john","smith"} | `"Jane Smith"` → {"jane","smith"} | (1+0+0)/3=0.33 | ❌ <0.5 |

### 7.4 Greedy Author Matching

Authors are matched bidirectionally using the overlap score:

1. **Source → Bib:** For each source author, find the best-matching unmatched bib author (score ≥ 0.5). Unmatched source authors → `missing_authors`.
2. **Bib → Source:** For each bib author, find the best-matching unmatched source author (score ≥ 0.5). Unmatched bib authors → `extra_authors`.

### 7.5 Fuzzy Fallback

When token matching fails, whole-name `SequenceMatcher` similarity (≥ 0.85) catches romanization variants:

| Bib | Source | SeqMatcher | Result |
|-----|--------|-----------|--------|
| `"Yucheng Suo"` | `"Yuchen Suo"` | 0.96 | ✅ ≥0.85 |

### 7.6 Positional Order Check

When all authors match (no missing/extra), every position is verified:

```python
for i in range(len(bib_tokens)):
    if _token_overlap_score(bib_tokens[i], src_tokens[i]) < 0.5:
        order_wrong = True
```

`[A, B, C, D]` vs `[A, C, B, D]` → positions 1 and 2 mismatch → `order_wrong = True`.

### 7.7 "et al." Handling

If either list contains `"et al."` or `"et al"`, both lists are truncated to the earliest "et al." position before comparison. Only the prefix authors are compared.

```
Bib:    [Alayrac, Donahue, Luc, Miech, Barr, et al]  → truncate to 5
Source: [Alayrac, Donahue, Luc, Miech, Barr, Hasson, ...27 total] → truncate to 5
Compare: [Alayrac, Donahue, Luc, Miech, Barr] vs [Alayrac, Donahue, Luc, Miech, Barr]
Result: ✅ match
```

---

## 8. Report Output

### 8.1 JSON Report

```json
{
  "filepath": "references.bib",
  "total_entries": 48,
  "elapsed_seconds": 196.0,
  "elapsed_minutes": 3.3,
  "summary": "Total: 48 | Wrong: 4 | Unverifiable: 16 | Correct: 28 | Time: 3.3 min",
  "wrong_references": [ ... ],
  "unknown_style_references": [ ... ],
  "correct_references": [ ... ]
}
```

Each entry contains:

| Field | Description |
|-------|-------------|
| `entry_id` | Citation key |
| `entry_type` | `article`, `inproceedings`, etc. |
| `title` | Bib title (first 120 chars) |
| `url` | Primary URL |
| `url_valid` | `true`/`false`/`null` (null = blocked/unknown) |
| `url_status_code` | HTTP status |
| `url_error` | Error message if any |
| `source` | Which source matched (`"dblp"`, `"crossref"`, `"semantic_scholar"`, etc.) |
| `bib_authors` | Authors from bib file |
| `source_authors` | Authors from matched source |
| `authors_match` | `true`/`false`/`null` |
| `missing_authors` | In source but not in bib |
| `extra_authors` | In bib but not in source |
| `author_order_wrong` | `true` if order differs |
| `year_mismatch` | `true` if year differs by >1 |
| `source_year` | Year from source |
| `title_match_score` | 0.0–1.0 similarity |
| `wrong_reasons` | Human-readable list of issues |
| `is_wrong_reference` | Classification flag |
| `is_unknown_style` | Classification flag |

### 8.2 Text Report

```
======================================================================
  BIBLIOGRAPHY EVALUATION REPORT
======================================================================
  File: custom.bib
  Total: 48 | Wrong: 4 | Unverifiable: 16 | Correct: 28 | Time: 3.3 min
======================================================================

  ❌ WRONG REFERENCES (4)
======================================================================

  [1] DBLP:journals/corr/abs-2501-10836  (article)
      Title: BAP v2: An Enhanced Task Framework...
      Title similarity: 1.00
      Reasons:
        • Missing authors: Risham Sidhu
      Verified against: crossref
      Source: https://doi.org/10.1162/coli.a.602

  ⚠️  UNVERIFIABLE REFERENCES (16)
     Not found in any of 10 sources — may be hallucinated or extremely obscure.
     (dblp, Semantic Scholar, NeurIPS, CVPR, arXiv, Crossref, ACL, ACM, OpenAlex, OpenReview)
======================================================================

  ✅ CORRECT REFERENCES (28)
======================================================================
     DBLP:conf/icra/SinghBMGXTFTG23  [crossref]
     DBLP:conf/icra/LiangHXXHIFZ23  [crossref]
     ...
```

---

## 9. CLI Usage

```bash
# Basic usage
python3 main.py references.bib

# JSON output
python3 main.py references.bib --json

# Save JSON to specific file
python3 main.py references.bib -o report.json

# Auto-generate filename
python3 main.py references.bib --json          # → references_report.json

# Verbose mode (see per-entry source matching)
python3 main.py references.bib --json -v

# Tune concurrency (lower = fewer rate limits, slower)
python3 main.py references.bib --url-concurrency 5 --api-concurrency 3

# Single-threaded API calls (most reliable, slowest)
python3 main.py references.bib --api-concurrency 1
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | No wrong references found |
| 1 | One or more wrong references |

---

## 10. Known Limitations

### 10.1 Rate limiting causes intermittent failures

DBLP, Semantic Scholar, and Crossref apply rate limits. On fast runs with high concurrency, some API calls may return 429 or empty results. The tool retries and backs off, but adding more sources also adds more API calls. For maximum reliability, run with `--api-concurrency 1`.

### 10.2 ACL search API may be unreachable

The ACL Anthology search API (`api.aclanthology.org`) fails DNS resolution on some networks. The tool falls back to direct HTML scraping of ACL paper pages when the bib entry has an `aclanthology.org` URL.

### 10.3 Crossref does not resolve DataCite DOIs

arXiv preprints use DataCite prefix `10.48550` which Crossref's API does not resolve. These are handled by the dedicated arXiv API source and by OpenAlex.

### 10.4 "Unverifiable" ≠ definitely hallucinated

A paper not found in any of the 10 sources may be a genuine niche publication, a very recent preprint not yet indexed, or a manual entry with no identifiers. Treat "unverifiable" as requiring manual review, not as a confirmed error.

### 10.5 Title similarity < 0.85 may be a false positive

The title hallucination check catches subtly-wrong titles, but some legitimate papers have title variations between venues (e.g., workshop vs. conference version, preprint vs. published) or truncated titles in the bib. Review entries flagged for title mismatch before accepting them as errors. The title mismatch reason is always appended alongside any other errors — an entry with both a bad URL and a wrong title will show both reasons in a single pass.

### 10.6 Conference/venue is not verified

The tool checks authors, year, URL, and title — but not whether the paper actually appeared at the claimed conference. A bib entry citing the correct paper but attributing it to the wrong venue will pass all checks.

### 10.7 URL validity ≠ correct URL

A URL returning HTTP 200 only means the page exists — it does not mean the page is the cited paper. An LLM could assign the DOI of paper B to paper A's bib entry. Crossref's DOI lookup partially recovers this (if paper B's authors don't match paper A's bib authors), but only if Crossref wins the source-matching race before DBLP or another source matches paper A by title.

### 10.8 No Google Scholar integration

Google Scholar aggressively blocks automated access (CAPTCHA, IP bans). The `scholarly` Python package typically works for <10 queries before being blocked. Not viable for batch processing.

### 10.9 ACM scraper fragility

ACM Digital Library's HTML structure changes over time. The regex-based author extraction may break if ACM updates their page templates. An author-overlap sanity check (same pattern used by Crossref and OpenAlex) guards against wrong-paper matches when scraping succeeds.

### 10.10 CVPR/CVF scraper requires two HTTP requests

The CVF scraper first searches for the paper, then fetches the individual paper page for author metadata. Each paper from CVPR/ICCV/WACV incurs two HTTP round-trips. This is slower than API-based sources but necessary because CVF has no public API.

---

## 11. File Index

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point, argument parsing |
| `bib_eval/__init__.py` | Package init |
| `bib_eval/parser.py` | `.bib` file parser (bibtexparser) |
| `bib_eval/url_checker.py` | Async URL validation + Semantic Scholar fallback for 403 |
| `bib_eval/author_verifier.py` | 10-source author/metadata verification + comparison algorithm |
| `bib_eval/orchestrator.py` | Pipeline orchestration + report generation |
| `test_author_matching.py` | 85+ unit tests for comparison algorithm |
| `sample.bib` | Test `.bib` with various reference styles |
| `debug_apis.py` | Quick API connectivity test |
| `debug_dblp_single.py` | Single-entry DBLP lookup test |
| `requirements.txt` | Python dependencies |
| `CODE_REVIEW.md` | Code review findings and fix status |
| `HALLUCINATION_DETECTION_ASSESSMENT.md` | Hallucination detection capability analysis |
| `DOCUMENTATION.md` | This document |

## Testing

The comparison algorithm has a comprehensive test suite:

```bash
python3 -m pytest test_author_matching.py -v
```

**91 tests** across 8 test classes covering:

| Test class | Tests | Covers |
|-----------|-------|--------|
| `TestNormalizeAuthorName` | 10 | DBLP suffixes, Unicode diacritics, commas, periods, braces, et al. |
| `TestAuthorToTokenSet` | 7 | Token creation, initial preservation, comma equivalence |
| `TestTokenOverlapScore` | 12 | Core matching: initials, middle names, symmetry, empty sets |
| `TestCompareAuthors` | 29 | Full comparison: missing/extra authors, order checks, et al., DBLP, Unicode |
| `TestCheckYear` | 7 | Year matching with ±1 tolerance |
| `TestTitleNormalization` | 7 | Title normalization and similarity scoring |
| `TestRegression` | 16 | Real bugs: DBLP suffixes, initials, Unicode, umlauts, comma format, fuzzy fallback |
| `TestEtAl` (embedded) | 7 | et al. truncation: prefix matching, order, both sides |

Key regression tests cover every bug found during development: DBLP disambiguation suffixes, initial-vs-full-name matching, German umlaut normalization (both Unicode and ASCII forms), fuzzy name fallback for romanization variants, and "et al." author list truncation.
