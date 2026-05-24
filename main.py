#!/usr/bin/env python3
"""BibEval CLI — Evaluate correctness of a .bib bibliography file.

Usage:
    python main.py <path/to/file.bib> [--json] [--url-concurrency N] [--api-concurrency N]
"""

from __future__ import annotations

import argparse
import sys
import os

# Add the project root to path so we can import bib_eval
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bib_eval.orchestrator import BibEvalOrchestrator, format_report


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate correctness of a .bib bibliography file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python main.py references.bib                    # text report to stdout
    python main.py references.bib --json             # JSON to stdout
    python main.py references.bib -o report.json     # save JSON to file
    python main.py references.bib --json -o out.json # JSON to both stdout and file
    python main.py references.bib --url-concurrency 20 --api-concurrency 3

Checks performed:
    1. URL validity — verifies each URL actually resolves (HEAD/GET request).
    2. Author correctness — compares bib authors against dblp, Semantic Scholar,
       Crossref, ACL Anthology, NeurIPS Proceedings, CVPR/CVF Open Access,
       and ACM Digital Library.

Output categories:
    ❌ Wrong References:    URL invalid OR author list differs from source.
    ⚠️  Unknown Style:       Reference not found in dblp/ACL/ACM (possibly arXiv, manual entry, etc.)
    ✅ Correct References:   All checks passed.
        """,
    )
    parser.add_argument("bibfile", help="Path to the .bib file to evaluate")
    parser.add_argument("--json", action="store_true", help="Output report as JSON to stdout")
    parser.add_argument(
        "-o", "--output",
        type=str,
        default=None,
        metavar="FILE",
        help="Save JSON report to FILE (auto-generates name if omitted with --json)",
    )
    parser.add_argument(
        "--url-concurrency",
        type=int,
        default=10,
        help="Max concurrent URL checks (default: 10)",
    )
    parser.add_argument(
        "--api-concurrency",
        type=int,
        default=5,
        help="Max concurrent API calls for author verification (default: 5)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print debug info for each API call (helps diagnose 'Unknown Style' issues)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.bibfile):
        print(f"Error: File not found: {args.bibfile}", file=sys.stderr)
        sys.exit(1)

    if not args.bibfile.endswith(".bib"):
        print(f"Warning: File does not have .bib extension: {args.bibfile}", file=sys.stderr)

    orchestrator = BibEvalOrchestrator(
        filepath=args.bibfile,
        url_concurrency=args.url_concurrency,
        api_concurrency=args.api_concurrency,
        verbose=args.verbose,
    )
    report = orchestrator.run()

    # Determine output format and file
    use_json = args.json or args.output is not None
    output = format_report(report, output_format="json" if use_json else "text")

    if use_json:
        # Decide output file path
        out_path = args.output
        if out_path is None:
            # Auto-generate: references.bib → references_report.json
            base = os.path.splitext(args.bibfile)[0]
            out_path = f"{base}_report.json"

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Report saved to: {out_path}")

        # Also print a short summary to stdout
        print(f"Summary: {report.summary()}")
    else:
        print(output)

    # Exit with non-zero if any wrong references found
    if report.wrong_references:
        sys.exit(1)


if __name__ == "__main__":
    main()
