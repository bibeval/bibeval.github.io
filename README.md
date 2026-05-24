# BibEval — Bibliography Correctness Evaluator

> **Vibecoded with DeepSeek V4 Pro (High) model.** This tool is provided for reference purposes and does not guarantee coverage of all edge cases. Always manually verify critical references before publication.

**Evaluate `.bib` files for wrong references: broken URLs, incorrect authors, wrong years, hallucinated titles, and unverifiable papers.**

```bash
$ python3 main.py references.bib --json -o report.json
Parsing: references.bib
  Found 48 entries
Checking URLs...
Verifying authors (dblp → Semantic Scholar → NeurIPS → CVPR → arXiv → Crossref → ACL → ACM)...
Done in 3.3 min
Report saved to: report.json
Summary: Total: 48 | Wrong: 4 | Unverifiable: 13 | Correct: 31 | Time: 3.3 min
```

## What It Checks

| Check | Description |
|-------|-------------|
| **URL validity** | Verifies URLs actually resolve via HEAD→GET HTTP requests with retry |
| **Author correctness** | Compares bib authors against 10 sources — missing, extra, wrong order |
| **Year accuracy** | Year must match the source within ±1 year tolerance |
| **Title accuracy** | Flags titles that are subtly wrong (similarity < 0.85 vs source) |
| **Venue accuracy** | Verifies bib venue against source record (DBLP, Crossref, Semantic Scholar) |
| **DOI cross-check** | Independently resolves bib DOI to confirm it points to the correct paper |
| **Unverifiable papers** | Flags papers not found in any of 10 comprehensive databases |

## Quick Start

**🌐 Try it online:** [bibeval.github.io/](https://bibeval.github.io/) — upload a `.bib` file and get instant **URL validation** in your browser (no installation needed). For full 10-source author verification, year checking, venue matching, and DOI cross-check, use the CLI below.

```bash
# Install
pip install -r requirements.txt

# Evaluate your .bib file
python3 main.py my_references.bib

# JSON output with timing
python3 main.py my_references.bib --json -o report.json

# Verbose mode — see which source matched each entry
python3 main.py my_references.bib --json -o report.json -v

# Maximum reliability (slower, avoids API rate limits)
python3 main.py my_references.bib --api-concurrency 1
```

## Output

Entries are classified into three categories:

| Category | Meaning |
|----------|---------|
| ❌ **Wrong References** | Broken URL, wrong/missing authors, wrong order, year mismatch, title hallucination |
| ⚠️ **Unverifiable** | Not found in any of 10 sources — may be hallucinated or extremely obscure |
| ✅ **Correct** | All checks passed |

Each flagged entry includes the specific reasons and the matched source for verification.

## How It Works

```
.bib file
    │
    ├─ 1. Parse — extract title, authors, DOI, URL, year
    │
    ├─ 2. URL check — HEAD→GET HTTP validation with timeout retry
    │      403-blocked URLs cross-checked via Semantic Scholar
    │
    ├─ 3. Author verification — query 10 sources by title/DOI
    │      Token-set matching with initial-aware Jaccard similarity
    │      Fuzzy fallback for romanization variants
    │      German umlaut normalization (ü↔ue, ö↔oe, ä↔ae)
    │      "et al." prefix comparison
    │
    ├─ 4. Venue check — compare bib venue against source record
    │
    ├─ 5. DOI cross-check — verify bib DOI points to the correct paper
    │
    └─ 6. Report — classify as Wrong / Unverifiable / Correct
```

## 10 Verification Sources

| Source | Coverage | API |
|--------|----------|-----|
| **DBLP** | CS conferences/journals | `dblp.org/search/publ/api` |
| **Semantic Scholar** | Broad ML/AI/NLP, arXiv | `api.semanticscholar.org` |
| **NeurIPS Proceedings** | NeurIPS 1987–present | `proceedings.neurips.cc` |
| **CVF Open Access** | CVPR, ICCV, WACV | `openaccess.thecvf.com` |
| **arXiv API** | All arXiv preprints | `export.arxiv.org/api` |
| **Crossref** | Published papers with DOIs | `api.crossref.org` |
| **ACL Anthology** | ACL, EMNLP, NAACL, etc. | Page scrape + search API |
| **ACM Digital Library** | ACM conferences/journals | HTML scrape |
| **OpenAlex** | 250M+ works (last resort) | `api.openalex.org` |
| **OpenReview** | ICLR, NeurIPS (OR track) | `api.openreview.net` |

All sources are **free and require no API keys**. Each source is rate-limited and retried on failure.

## Requirements

- Python 3.9+
- `bibtexparser`, `aiohttp`, `tqdm` (see `requirements.txt`)

## Full Documentation

- **[DOCUMENTATION.md](DOCUMENTATION.md)** — Architecture, data flow, all URLs, author comparison algorithm, known limitations
- **[HALLUCINATION_DETECTION_CAPABILITIES.md](HALLUCINATION_DETECTION_CAPABILITIES.md)** — What types of LLM hallucinations the tool can and cannot detect

## Testing

```bash
python3 -m pytest test_author_matching.py -v
# 91 tests covering normalization, token matching, author comparison, and regression cases
```

## License

MIT
