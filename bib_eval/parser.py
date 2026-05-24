"""BibTeX file parser — extracts structured reference entries from .bib files."""

import re
from dataclasses import dataclass, field
from typing import Optional

import bibtexparser
from bibtexparser.bparser import BibTexParser
from bibtexparser.customization import convert_to_unicode


@dataclass
class BibEntry:
    """Structured representation of a single BibTeX entry."""

    entry_id: str
    entry_type: str  # article, inproceedings, misc, etc.
    title: str = ""
    authors: list[str] = field(default_factory=list)
    author_string: str = ""  # raw author field from bib
    url: str = ""
    doi: str = ""
    year: str = ""
    journal: str = ""
    booktitle: str = ""
    publisher: str = ""
    raw: dict = field(default_factory=dict)  # all fields as-is

    @property
    def primary_url(self) -> Optional[str]:
        """Return the best URL available: explicit url > DOI-derived."""
        if self.url:
            return self.url
        if self.doi:
            doi_clean = self.doi.strip()
            if doi_clean.startswith("http"):
                return doi_clean
            return f"https://doi.org/{doi_clean}"
        return None


def parse_authors(raw_authors: str) -> list[str]:
    """Parse a BibTeX author field into a list of cleaned author names.

    Handles 'and' separators, strips extra whitespace/newlines,
    and removes leading/trailing braces.
    """
    if not raw_authors:
        return []
    authors = []
    for author in re.split(r"\s+and\s+", raw_authors.strip()):
        author = author.strip()
        # Remove surrounding braces if present
        author = re.sub(r"^\{|\}$", "", author)
        # Collapse whitespace
        author = " ".join(author.split())
        if author:
            authors.append(author)
    return authors


class BibParser:
    """Parses .bib files into structured BibEntry objects."""

    def __init__(self, filepath: str):
        self.filepath = filepath

    def parse(self) -> list[BibEntry]:
        """Parse the .bib file and return a list of BibEntry objects."""
        parser = BibTexParser(common_strings=True)
        parser.customization = convert_to_unicode
        parser.ignore_nonstandard_types = False

        with open(self.filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Fix common bib parsing issues: normalize line endings
        content = content.replace("\r\n", "\n")

        try:
            bib_db = bibtexparser.loads(content, parser=parser)
        except Exception as e:
            raise ValueError(f"Failed to parse {self.filepath}: {e}")

        entries: list[BibEntry] = []
        for entry in bib_db.entries:
            raw_authors = entry.get("author", entry.get("authors", ""))
            authors = parse_authors(raw_authors)

            bib_entry = BibEntry(
                entry_id=entry.get("ID", ""),
                entry_type=entry.get("ENTRYTYPE", "misc"),
                title=entry.get("title", "").strip("{} "),
                authors=authors,
                author_string=raw_authors.strip(),
                url=entry.get("url", "").strip(),
                doi=entry.get("doi", "").strip(),
                year=entry.get("year", "").strip(),
                journal=entry.get("journal", entry.get("booktitle", "")).strip(),
                booktitle=entry.get("booktitle", "").strip(),
                publisher=entry.get("publisher", "").strip(),
                raw=dict(entry),
            )
            entries.append(bib_entry)

        return entries
