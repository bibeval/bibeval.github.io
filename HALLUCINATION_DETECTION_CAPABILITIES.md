# Hallucination Detection Capabilities

**Date:** 2026-05-24  
**Status:** Current as of all implemented fixes

---

## Detection Coverage — Full Table

| Hallucination Type | Detected | Mechanism |
|---|---|---|
| Broken or fabricated URL | ✅ Yes | HTTP HEAD→GET, timeout retry, 403-blocked fallback via Semantic Scholar |
| Wrong authors (missing/extra) | ✅ Yes | Token-set matching with initial-aware Jaccard, 10-source comparison |
| Wrong author order | ✅ Yes | Full positional check — all positions verified with token overlap |
| Wrong year (>1 year off) | ✅ Yes | ±1 tolerance across all sources including DBLP |
| Subtly wrong title (similarity 0.60–0.85) | ✅ Yes | Flagged in `wrong_reasons` with similarity score and source name |
| Very wrong title (similarity <0.60) | ⚠️ Flagged | Falls to "Unverifiable" — no source can match the title |
| Wrong venue/conference/journal | ✅ Yes | Fuzzy venue comparison (DBLP `venue`, Crossref `container-title`, Semantic Scholar `venue`) |
| DOI resolves to wrong paper | ✅ Yes | Independent Crossref DOI lookup post-match — title/author cross-check |
| Completely fabricated paper | ⚠️ Flagged | "Unverifiable" label with explicit warning across 10 sources |
| `et al.` in author lists | ✅ Yes | Prefix truncation — compares only authors before `et al.` |
| Name formatting variations | ✅ Yes | Initials (F.↔Fei), comma format, middle names, DBLP disambiguation suffixes |
| German umlauts (ü↔ue, ö↔oe, ä↔ae) | ✅ Yes | NFKD normalization + umlaut ASCII fallback |
| Romanization variants (Yucheng↔Yuchen) | ✅ Yes | Fuzzy SequenceMatcher fallback (≥0.85) |

---

## Verification Pipeline

For each `.bib` entry, the tool runs these checks in order:

```
1. URL validity — HEAD→GET, retry, 403 fallback
2. Author verification — 10-source sequential lookup
3. Year check — ±1 tolerance
4. Title check — similarity < 0.85 flagged
5. Venue check — bib booktitle vs source venue
6. DOI cross-check — independent Crossref resolution
```

---

## What Each Check Catches

### URL Check
- **404/410:** The URL or DOI definitively does not exist
- **Timeout/DNS failure:** Server unreachable after retry
- **403:** Server blocks automated access → confirmed valid via Semantic Scholar title search or author verification cross-reference

### Author Verification
- **Missing authors:** Author in source not found in bib → flagged
- **Extra authors:** Author in bib not found in source → flagged
- **Wrong order:** Authors match but positions differ → flagged
- **Initial-vs-full-name:** "F. Xia" ↔ "Fei Xia" handled by initial-aware matching
- **Middle names:** "Lucy Shi" ↔ "Lucy Xiaoyang Shi" handled by Jaccard similarity ≥0.5
- **et al.:** Bib has 5 authors + "et al." → only first 5 compared against source

### Year Check
- **Exact matches and ±1 year:** Pass — handles arXiv preprints (year N) vs conference publication (year N+1)
- **>1 year off:** Flagged
- **Missing year in either bib or source:** Not flagged (can't check)

### Title Check
- **≥0.85 similarity:** Not flagged (normal formatting differences)
- **0.60–0.85 similarity:** Flagged as potential hallucination with the score and source name for manual review
- **<0.60 similarity:** Source can't match the title → entry becomes "Unverifiable"
- **Always reported:** Title mismatch appears alongside any other errors, never suppressed

### Venue Check
- **Exact match after normalization:** Pass
- **One contains the other** (e.g., "Proceedings of ACL 2023" contains "ACL 2023"): Pass
- **Fuzzy similarity ≥0.5** (e.g., "NeurIPS" vs "Advances in Neural Information Processing Systems"): Pass
- **No venue data in source or bib:** Pass (can't check)
- **Fuzzy similarity <0.5:** Flagged

### DOI Cross-Check
- **After source match:** Independently resolves bib's DOI via Crossref
- **DOI title matches bib title (≥0.7):** Pass
- **At least one author overlaps (≥0.5):** Pass — same paper, title variation
- **Neither:** DOI points to a different paper → flagged

---

## Sources Used

All 10 sources are free and require no API keys:

| Source | Venue data | DOI resolution |
|--------|-----------|----------------|
| DBLP | `info.venue` | — |
| Semantic Scholar | `paper.venue` | — |
| NeurIPS Proceedings | — | — |
| CVF Open Access | — | — |
| arXiv API | — | — |
| Crossref | `container-title` | DOI exact lookup + title fallback |
| ACL Anthology | — | — |
| ACM Digital Library | — | — |
| OpenAlex | — | DOI exact lookup |
| OpenReview | — | — |

---

## What Is NOT Caught

| Scenario | Why |
|----------|-----|
| Direct (non-DOI) URL points to a different paper | Only HTTP status is checked for non-DOI URLs — content is not fetched |
| Bib entry has no URL, DOI, or sufficient title for lookup | Falls to "Unverifiable" — can't verify without identifiers |
| Intermittent API rate limiting causing false "Unverifiable" | Run with `--api-concurrency 1` for maximum reliability |

---

## Practical Guidance

- **"Wrong" references** require correction before use
- **"Unverifiable" references** require manual verification — not found in 10 comprehensive databases is a strong signal, but not conclusive
- **"Correct" references with `title_match_score` 0.85–0.92** are worth a quick manual spot-check
- **The `source_url` field in JSON** links directly to the matched record — one click to verify
- **For critical work**, run with `--api-concurrency 1` for maximum reliability (slower, avoids rate limits)
