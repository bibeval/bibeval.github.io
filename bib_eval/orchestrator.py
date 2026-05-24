"""Orchestrator — runs all checks and produces a structured report."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from .parser import BibParser, BibEntry
from .url_checker import check_urls, UrlCheckResult, verify_blocked_urls_via_semantic_scholar
from .author_verifier import verify_authors, AuthorCheckResult, Source


@dataclass
class EntryReport:
    """Per-entry evaluation report."""

    entry_id: str
    entry_type: str
    title: str
    # URL check
    url: str = ""
    url_valid: Optional[bool] = None
    url_status_code: Optional[int] = None
    url_error: Optional[str] = None
    # Author check
    source: Optional[str] = None  # "dblp", "acl", "acm"
    bib_authors: list[str] = field(default_factory=list)
    source_authors: list[str] = field(default_factory=list)
    authors_match: Optional[bool] = None
    missing_authors: list[str] = field(default_factory=list)
    extra_authors: list[str] = field(default_factory=list)
    author_order_wrong: bool = False
    year_mismatch: bool = False
    source_year: str = ""
    venue_mismatch: bool = False
    source_venue: str = ""
    author_error: Optional[str] = None
    source_url: Optional[str] = None
    title_match_score: float = 0.0

    # Classification
    is_wrong_reference: bool = False
    wrong_reasons: list[str] = field(default_factory=list)
    is_unknown_style: bool = False


@dataclass
class EvalReport:
    """Complete evaluation report."""

    filepath: str
    total_entries: int
    elapsed_seconds: float = 0.0
    wrong_references: list[EntryReport] = field(default_factory=list)
    unknown_style_references: list[EntryReport] = field(default_factory=list)
    correct_references: list[EntryReport] = field(default_factory=list)

    def summary(self) -> str:
        mins = self.elapsed_seconds / 60.0
        return (
            f"Total: {self.total_entries} | "
            f"Wrong: {len(self.wrong_references)} | "
            f"Unverifiable: {len(self.unknown_style_references)} | "
            f"Correct: {len(self.correct_references)} | "
            f"Time: {mins:.1f} min"
        )


class BibEvalOrchestrator:
    """Runs the full bibliography evaluation pipeline."""

    def __init__(self, filepath: str, url_concurrency: int = 10, api_concurrency: int = 5, verbose: bool = False):
        self.filepath = filepath
        self.url_concurrency = url_concurrency
        self.api_concurrency = api_concurrency
        self.verbose = verbose

    def run(self) -> EvalReport:
        """Run evaluation synchronously (wraps async)."""
        return asyncio.run(self._run_async())

    async def _run_async(self) -> EvalReport:
        t0 = time.time()
        # 1. Parse
        print(f"Parsing: {self.filepath}")
        parser = BibParser(self.filepath)
        entries = parser.parse()
        print(f"  Found {len(entries)} entries")

        report = EvalReport(filepath=self.filepath, total_entries=len(entries))

        if not entries:
            return report

        # 2. Check URLs
        print("Checking URLs...")
        url_results = await check_urls(entries, concurrency=self.url_concurrency)
        url_map: dict[str, UrlCheckResult] = {r.entry_id: r for r in url_results}

        # 2b. For blocked URLs (403), try Semantic Scholar to confirm existence
        blocked = [r for r in url_results if r.is_valid is None]
        if blocked:
            print(f"  {len(blocked)} URL(s) blocked (403) — cross-checking via Semantic Scholar...")
            ss_upgrades = await verify_blocked_urls_via_semantic_scholar(
                entries, blocked, concurrency=self.api_concurrency,
            )
            for eid, upgraded in ss_upgrades.items():
                url_map[eid] = upgraded
                print(f"    ✓ {eid}: confirmed via Semantic Scholar")

            # Also cross-reference: if author verifier found it on DBLP/ACL/etc.,
            # the paper definitely exists — upgrade blocked URLs
            # We'll do this after author verification below

        # 3. Verify authors
        print("Verifying authors (dblp → Semantic Scholar → NeurIPS → CVPR → arXiv → Crossref → ACL → ACM)...")
        author_results = await verify_authors(entries, concurrency=self.api_concurrency, verbose=self.verbose)
        author_map: dict[str, AuthorCheckResult] = {r.entry_id: r for r in author_results}

        # 3b. Cross-reference: if URL was blocked (403) but author verifier found
        # the paper with high title match, the URL is valid
        for entry in entries:
            url_r = url_map.get(entry.entry_id)
            auth_r = author_map.get(entry.entry_id)
            if (
                url_r is not None
                and url_r.is_valid is None  # still blocked
                and auth_r is not None
                and auth_r.source is not None
                and auth_r.title_match_score >= 0.6
            ):
                url_map[entry.entry_id] = UrlCheckResult(
                    entry_id=entry.entry_id,
                    url=url_r.url,
                    is_valid=True,
                    status_code=url_r.status_code,
                )
                print(f"    ✓ {entry.entry_id}: URL confirmed via {auth_r.source.value} match")

        # 3c. DOI cross-check: verify bib DOI resolves to the same paper
        # (catches LLMs assigning paper B's DOI to paper A's bib entry)
        doi_issues: dict[str, str] = {}
        if entries:
            doi_issues = await _cross_check_dois(entries, author_map, self.api_concurrency)

        # 4. Build per-entry reports
        for entry in entries:
            er = EntryReport(
                entry_id=entry.entry_id,
                entry_type=entry.entry_type,
                title=entry.title[:120],
                url=entry.primary_url or "",
                bib_authors=entry.authors,
            )

            # URL check
            url_r = url_map.get(entry.entry_id)
            if url_r is not None:
                er.url_valid = url_r.is_valid
                er.url_status_code = url_r.status_code
                er.url_error = url_r.error
                # Only flag as wrong when definitively invalid (404, DNS failure, etc.)
                # is_valid=None means blocked (403) — not necessarily broken
                if url_r.is_valid is False:
                    er.wrong_reasons.append(f"Invalid URL: {url_r.error} ({url_r.url})")
                    er.is_wrong_reference = True
            # else: no URL to check — not an error

            # Author check
            auth_r = author_map.get(entry.entry_id)
            if auth_r is not None:
                if auth_r.source is not None:
                    er.source = auth_r.source.value
                    er.source_authors = auth_r.source_authors
                    er.authors_match = auth_r.is_match
                    er.missing_authors = auth_r.missing_authors
                    er.extra_authors = auth_r.extra_authors
                    er.author_order_wrong = auth_r.order_wrong
                    er.year_mismatch = auth_r.year_mismatch
                    er.source_year = auth_r.source_year
                    er.venue_mismatch = auth_r.venue_mismatch
                    er.source_venue = auth_r.source_venue
                    er.title_match_score = auth_r.title_match_score
                    er.source_url = auth_r.source_url

                    if auth_r.is_match is False:
                        if auth_r.missing_authors:
                            er.wrong_reasons.append(
                                f"Missing authors: {', '.join(auth_r.missing_authors)}"
                            )
                        if auth_r.extra_authors:
                            er.wrong_reasons.append(
                                f"Extra authors (not in source): {', '.join(auth_r.extra_authors)}"
                            )
                        if auth_r.order_wrong:
                            er.wrong_reasons.append("Author order differs from source")
                        if auth_r.year_mismatch:
                            er.wrong_reasons.append(
                                f"Year mismatch: bib says {entry.year or '?'} but source says {auth_r.source_year}"
                            )
                        if auth_r.venue_mismatch:
                            er.wrong_reasons.append(
                                f"Venue mismatch: bib says '{entry.booktitle or entry.journal or '?'}' "
                                f"but source says '{auth_r.source_venue}'"
                            )
                        er.is_wrong_reference = True
                    elif auth_r.order_wrong:
                        # Authors match but order is wrong — still a wrong reference
                        er.wrong_reasons.append("Author order differs from source")
                        er.is_wrong_reference = True

                    # Title mismatch check: catches hallucinated/subtly-wrong titles.
                    # Always append even if entry already flagged for other reasons.
                    if auth_r.title_match_score < 0.85:
                        er.wrong_reasons.append(
                            f"Title mismatch (similarity={auth_r.title_match_score:.2f}): "
                            f"bib title may differ from the actual paper — verify against {auth_r.source.value}"
                        )
                        er.is_wrong_reference = True
                else:
                    # Source is None → couldn't find in dblp/ACL/ACM
                    er.author_error = auth_r.error or "Unknown reference style"
                    er.is_unknown_style = True

            # 4b. DOI cross-check: does bib DOI resolve to the correct paper?
            if entry.doi and entry.entry_id in doi_issues:
                er.wrong_reasons.append(
                    f"DOI mismatch: bib DOI ({entry.doi}) resolves to a different paper — {doi_issues[entry.entry_id]}"
                )
                er.is_wrong_reference = True

            # Classify
            if er.is_wrong_reference:
                report.wrong_references.append(er)
            elif er.is_unknown_style:
                report.unknown_style_references.append(er)
            else:
                report.correct_references.append(er)

        report.elapsed_seconds = time.time() - t0
        print(f"\nDone in {report.elapsed_seconds / 60:.1f} min")
        return report


def format_report(report: EvalReport, output_format: str = "text") -> str:
    """Format the evaluation report as text or JSON."""
    if output_format == "json":
        return _format_json(report)
    return _format_text(report)


def _format_text(report: EvalReport) -> str:
    """Format report as colored text."""
    lines: list[str] = []
    sep = "=" * 70
    sub = "-" * 50

    lines.append(sep)
    lines.append("  BIBLIOGRAPHY EVALUATION REPORT")
    lines.append(sep)
    lines.append(f"  File: {report.filepath}")
    lines.append(f"  {report.summary()}")
    lines.append(sep)

    # ── Wrong References ──
    if report.wrong_references:
        lines.append(f"\n  ❌ WRONG REFERENCES ({len(report.wrong_references)})")
        lines.append(sep)
        for i, er in enumerate(report.wrong_references, 1):
            lines.append(f"\n  [{i}] {er.entry_id}  ({er.entry_type})")
            lines.append(f"      Title: {er.title}")
            if er.title_match_score > 0:
                lines.append(f"      Title similarity: {er.title_match_score:.2f}")
            lines.append(f"      Reasons:")
            for reason in er.wrong_reasons:
                lines.append(f"        • {reason}")
            if er.source:
                lines.append(f"      Verified against: {er.source}")
                if er.source_url:
                    lines.append(f"      Source: {er.source_url}")
    else:
        lines.append(f"\n  ✅ No wrong references found!")

    # ── Unknown Style References (now "Unverifiable") ──
    if report.unknown_style_references:
        lines.append(f"\n  ⚠️  UNVERIFIABLE REFERENCES ({len(report.unknown_style_references)})")
        lines.append(f"     Not found in any of 10 sources — may be hallucinated or extremely obscure.")
        lines.append(f"     (dblp, Semantic Scholar, NeurIPS, CVPR, arXiv, Crossref, ACL, ACM, OpenAlex, OpenReview)")
        lines.append(sep)
        for i, er in enumerate(report.unknown_style_references, 1):
            lines.append(f"\n  [{i}] {er.entry_id}  ({er.entry_type})")
            lines.append(f"      Title: {er.title}")
            if er.url:
                lines.append(f"      URL: {er.url} {'✅' if er.url_valid else '❌' if er.url_valid is False else '⊘'}")
            lines.append(f"      Bib Authors: {', '.join(er.bib_authors) if er.bib_authors else '(none)'}")
    else:
        lines.append(f"\n  ℹ️  All references verified against at least one source.")

    # ── Correct References ──
    if report.correct_references:
        lines.append(f"\n  ✅ CORRECT REFERENCES ({len(report.correct_references)})")
        lines.append(sep)
        for er in report.correct_references[:10]:  # show first 10
            lines.append(f"     {er.entry_id}  [{er.source or 'no-check-needed'}]")
        if len(report.correct_references) > 10:
            lines.append(f"     ... and {len(report.correct_references) - 10} more")

    lines.append(f"\n{sep}")
    return "\n".join(lines)


def _format_json(report: EvalReport) -> str:
    """Format report as JSON."""

    def _entry_to_dict(er: EntryReport) -> dict:
        return {
            "entry_id": er.entry_id,
            "entry_type": er.entry_type,
            "title": er.title,
            "url": er.url,
            "url_valid": er.url_valid,
            "url_status_code": er.url_status_code,
            "url_error": er.url_error,
            "source": er.source,
            "bib_authors": er.bib_authors,
            "source_authors": er.source_authors,
            "authors_match": er.authors_match,
            "missing_authors": er.missing_authors,
            "extra_authors": er.extra_authors,
            "author_order_wrong": er.author_order_wrong,
            "year_mismatch": er.year_mismatch,
            "source_year": er.source_year,
            "venue_mismatch": er.venue_mismatch,
            "source_venue": er.source_venue,
            "author_error": er.author_error,
            "source_url": er.source_url,
            "title_match_score": er.title_match_score,
            "is_wrong_reference": er.is_wrong_reference,
            "wrong_reasons": er.wrong_reasons,
            "is_unknown_style": er.is_unknown_style,
        }

    result = {
        "filepath": report.filepath,
        "total_entries": report.total_entries,
        "elapsed_seconds": round(report.elapsed_seconds, 1),
        "elapsed_minutes": round(report.elapsed_seconds / 60.0, 1),
        "summary": report.summary(),
        "wrong_references": [_entry_to_dict(e) for e in report.wrong_references],
        "unknown_style_references": [_entry_to_dict(e) for e in report.unknown_style_references],
        "correct_references": [_entry_to_dict(e) for e in report.correct_references],
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# DOI Cross-Check: verify bib DOI resolves to the correct paper
# ---------------------------------------------------------------------------

async def _cross_check_dois(
    entries: list[BibEntry],
    author_map: dict[str, AuthorCheckResult],
    concurrency: int = 3,
) -> dict[str, str]:
    """For entries with DOIs, verify the DOI resolves to the expected paper.

    If the DOI's title/authors don't match the bib entry or the matched source,
    the DOI was likely assigned to a different paper (common LLM hallucination).

    Returns: dict[entry_id → error_description]
    """
    import re
    from .author_verifier import _normalize_title, _title_similarity, _author_to_token_set, _token_overlap_score

    # Collect entries that have DOIs AND matched a source
    to_check: list[tuple[str, str, BibEntry]] = []
    for entry in entries:
        if entry.doi and entry.entry_id in author_map:
            auth_r = author_map[entry.entry_id]
            if auth_r.source is not None:
                to_check.append((entry.entry_id, entry.doi.strip(), entry))

    if not to_check:
        return {}

    issues: dict[str, str] = {}
    sem = asyncio.Semaphore(concurrency)

    async def _check_one(eid: str, doi: str, entry: BibEntry):
        async with sem:
            # Clean DOI
            doi_clean = doi
            if doi_clean.startswith("http"):
                doi_clean = doi_clean.split("doi.org/", 1)[-1] if "doi.org/" in doi_clean else doi_clean
            import urllib.parse
            safe_doi = urllib.parse.quote(doi_clean, safe="/")
            url = f"https://api.crossref.org/works/{safe_doi}"

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers={"Accept": "application/json", "User-Agent": "BibEval/1.0"},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status != 200:
                            return
                        data = await resp.json()
            except Exception:
                return

            msg = data.get("message", {})
            if not msg:
                return

            # Extract DOI title
            cr_title = (msg.get("title") or [""])[0]
            if not cr_title:
                return

            # Compare titles
            title_sim = _title_similarity(entry.title, cr_title)
            if title_sim >= 0.7:
                return  # DOI resolves to same paper, all good

            # Also check author overlap as a second signal
            cr_authors_raw = msg.get("author", [])
            cr_authors = []
            for a in cr_authors_raw:
                name = f"{a.get('given', '')} {a.get('family', '')}".strip()
                if name:
                    cr_authors.append(name)

            if entry.authors and cr_authors:
                bib_sets = [_author_to_token_set(a) for a in entry.authors]
                src_sets = [_author_to_token_set(a) for a in cr_authors]
                any_match = any(
                    _token_overlap_score(b, s) >= 0.5
                    for b in bib_sets for s in src_sets
                )
                if any_match:
                    return  # authors overlap, likely same paper with title variation

            # DOI resolves to a different paper
            issues[eid] = f"DOI title: \"{cr_title[:120]}\""

    # Run checks concurrently
    tasks = [_check_one(eid, doi, entry) for eid, doi, entry in to_check]
    await asyncio.gather(*tasks)

    return issues
