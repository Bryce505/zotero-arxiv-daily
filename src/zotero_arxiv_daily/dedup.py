"""De-duplication across sources and across weeks.

The same paper routinely surfaces from PubMed, Europe PMC, Crossref and
OpenAlex at once, so candidates are collapsed on a normalised DOI.  Papers
with no DOI — mostly preprints — fall back to a normalised title.  A
``seen_dois`` state file carries the de-duplication across weeks so a paper is
never recommended twice.
"""

import json
import os
import re
from typing import Iterable

from .protocol import CorpusPaper, Paper  # noqa: F401

_DOI_RE = re.compile(r"10\.\d{4,9}/\S+")
_TITLE_NOISE_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_doi(raw: str | None) -> str | None:
    """Return the bare lowercase DOI, or None when *raw* holds no DOI."""
    if not raw:
        return None
    match = _DOI_RE.search(raw.strip())
    return match.group(0).lower().rstrip(".") if match else None


def title_key(title: str) -> str:
    """Return a comparison key that ignores case, punctuation and spacing."""
    cleaned = _TITLE_NOISE_RE.sub(" ", title.lower())
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def dedup_papers(papers: list[Paper]) -> list[Paper]:
    """Collapse duplicates, keeping the first occurrence of each paper.

    Papers carrying a DOI are keyed on it; only DOI-less papers fall back to
    their title, so two genuinely different papers that share a title are
    never merged.
    """
    seen_dois: set[str] = set()
    seen_titles: set[str] = set()
    kept: list[Paper] = []
    for paper in papers:
        doi = normalize_doi(paper.doi)
        if doi is not None:
            if doi in seen_dois:
                continue
            seen_dois.add(doi)
        else:
            key = title_key(paper.title)
            if key in seen_titles:
                continue
            seen_titles.add(key)
        kept.append(paper)
    return kept


def drop_seen(papers: list[Paper], seen: set[str]) -> list[Paper]:
    """Drop papers whose DOI appears in *seen*; keep every DOI-less paper."""
    return [p for p in papers if (normalize_doi(p.doi) or "") not in seen]


def load_seen(path: str) -> set[str]:
    """Load the set of already-delivered DOIs; missing file means empty."""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as handle:
        return set(json.load(handle))


def save_seen(path: str, seen: set[str]) -> None:
    """Write *seen* sorted, so week-over-week diffs stay readable."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(sorted(seen), handle, indent=1, ensure_ascii=False)


def corpus_doi_set(corpus: Iterable["CorpusPaper"]) -> set[str]:
    """Collect the normalised DOIs already present in the Zotero library."""
    dois = set()
    for paper in corpus:
        doi = normalize_doi(paper.doi)
        if doi:
            dois.add(doi)
    return dois
