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


# Fields one source commonly has and another lacks: PubMed carries no
# open-access data, OpenAlex carries no PMID-based link, and so on.
_MERGEABLE_FIELDS = ("pdf_url", "journal", "pub_date", "cited_by_count", "full_text")


def _merge_into(kept: Paper, duplicate: Paper) -> None:
    """Fill gaps on *kept* from *duplicate*, never overwriting known values."""
    for field in _MERGEABLE_FIELDS:
        if getattr(kept, field, None) is None and getattr(duplicate, field, None) is not None:
            setattr(kept, field, getattr(duplicate, field))
    if kept.oa_status == "unknown" and duplicate.oa_status != "unknown":
        kept.oa_status = duplicate.oa_status


def dedup_papers(papers: list[Paper]) -> list[Paper]:
    """Collapse duplicates, keeping the first occurrence of each paper.

    Papers carrying a DOI are keyed on it first. Anything that does not match
    by DOI still collapses into an existing paper with the exact same
    (normalised) title — even when both sides carry a DOI and the two DOIs
    differ. A source occasionally attaches the wrong DOI to an otherwise
    correct record (title and abstract right, DOI pointing at an unrelated
    work); refusing to merge on that mismatch is how the same review paper
    ("Protein persulfidation in plants...") showed up twice in a delivered
    digest, once under each DOI. A verbatim, many-word title collision
    between two genuinely different papers is rare enough that trusting the
    title here is the right trade-off. The first occurrence keeps its
    identity — including its own DOI, even if the later duplicate's turns
    out to be the "real" one — but absorbs any field the later duplicate
    knew and it did not: PubMed has no open-access data, and losing Europe
    PMC's would waste a retrievable PDF.
    """
    by_doi: dict[str, Paper] = {}
    by_title: dict[str, Paper] = {}
    kept: list[Paper] = []
    for paper in papers:
        doi = normalize_doi(paper.doi)
        key = title_key(paper.title)

        existing = by_doi.get(doi) if doi else None
        if existing is None:
            existing = by_title.get(key)

        if existing is not None:
            _merge_into(existing, paper)
            if doi and normalize_doi(existing.doi) is None:
                existing.doi = paper.doi
                by_doi[doi] = existing
            continue

        if doi:
            by_doi[doi] = paper
        by_title.setdefault(key, paper)
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
