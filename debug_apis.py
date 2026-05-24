#!/usr/bin/env python3
"""Quick connectivity test for all APIs used by BibEval.

Usage: python3 debug_apis.py
"""

import asyncio
import sys
import aiohttp

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
}

APIS = [
    ("DBLP", "https://dblp.org/search/publ/api?q=attention+is+all+you+need&format=json&h=1"),
    ("SemanticScholar", "https://api.semanticscholar.org/graph/v1/paper/search?query=attention+is+all+you+need&limit=1&fields=title"),
    ("Crossref", "https://api.crossref.org/works?query.title=attention+is+all+you+need&rows=1"),
    ("ACL", "https://api.aclanthology.org/search?query=attention+is+all+you+need&limit=1"),
    ("NeurIPS", "https://proceedings.neurips.cc/"),
    ("CVF", "https://openaccess.thecvf.com/"),
]


async def check_api(session: aiohttp.ClientSession, name: str, url: str) -> tuple[str, int, str]:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            body = await resp.text()
            preview = body[:200].replace("\n", " ")
            return (name, resp.status, preview)
    except Exception as e:
        return (name, -1, str(e))


async def main():
    print("Testing API connectivity...\n")
    connector = aiohttp.TCPConnector(limit=5, ssl=False)
    async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
        tasks = [check_api(session, name, url) for name, url in APIS]
        results = await asyncio.gather(*tasks)

    all_ok = True
    for name, status, preview in results:
        if 200 <= status < 400:
            symbol = "✅"
        elif status == -1:
            symbol = "❌"
            all_ok = False
        else:
            symbol = "⚠️"
            all_ok = False
        print(f"  {symbol} {name:20s}  HTTP {status:>4}  | {preview[:100]}")

    print(f"\n{'All APIs reachable!' if all_ok else 'Some APIs are unreachable — this explains unknown references.'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
