#!/usr/bin/env python3
"""Test DBLP with actual titles from the bib file."""
import asyncio
import sys
sys.path.insert(0, '.')

from bib_eval.parser import BibParser
from bib_eval.author_verifier import _query_dblp
import aiohttp

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, */*",
}

async def main():
    parser = BibParser("ccbts_vlm_custom.bib")
    entries = parser.parse()
    print(f"Total entries: {len(entries)}\n")

    connector = aiohttp.TCPConnector(limit=1, ssl=False)
    async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
        for i, entry in enumerate(entries[:5]):
            print(f"[{i+1}] {entry.entry_id}")
            print(f"    Title: {repr(entry.title[:80])}")
            result = await _query_dblp(session, entry)
            if result and result.source:
                print(f"    ✅ Matched! score={result.title_match_score:.2f}, authors={len(result.source_authors)}")
            else:
                print(f"    ❌ No DBLP match")
            print()
            await asyncio.sleep(1.0)  # be nice to DBLP

if __name__ == "__main__":
    asyncio.run(main())
