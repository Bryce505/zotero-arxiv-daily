"""Fetch open-access full text, in descending order of reliability.

The ladder is: a URL the source already handed us, then Unpaywall's best OA
location, then Europe PMC's OA service.  The first hit wins.  Anything that
stays behind a paywall is left abstract-only and flagged, so the report can
list it under "needs manual retrieval" — publisher-proxy automation is
deliberately out of scope (spec finding 4).
"""

import hashlib
import os
import re
from dataclasses import dataclass

from loguru import logger

from ..protocol import Paper
from ..retriever.europepmc_retriever import EPMC_PDF_URL
from ..utils import extract_markdown_from_pdf, http_get_with_retry

_UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"
_EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")
_PDF_MAGIC = b"%PDF"
_TITLE_MAX_LEN = 80

# A DOI-keyed lookup (Unpaywall, Europe PMC) can answer with a real PDF that
# is nonetheless the wrong paper: an upstream record with a stale or
# mismatched DOI, or a location service that matched loosely. The fetch
# "succeeds" and silently hands the extractor a different paper's text under
# this one's title. _matches_title() is the cheap guard against that.
_WORD_RE = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_MIN_TITLE_WORDS = 4
_TITLE_MATCH_RATIO = 0.35
_TITLE_MATCH_WINDOW = 6000
# Sources whose fetch is keyed on paper.doi: a mismatch here implicates the
# DOI itself, not just the fetch. "direct" (paper.pdf_url) carries no such
# evidence, so it is left out on purpose.
_DOI_KEYED_SOURCES = ("unpaywall", "europepmc")


@dataclass
class FullTextResult:
    pdf_bytes: bytes | None
    oa_status: str
    source: str | None = None


def _fetch_pdf(url: str, max_bytes: int) -> bytes | None:
    """GET *url* and return the body only if it really is a PDF of sane size."""
    try:
        response = http_get_with_retry(url, retries=2, timeout=60)
    except Exception as exc:  # noqa: BLE001 - a paywall is an ordinary outcome here
        logger.debug(f"Full-text fetch failed for {url}: {exc}")
        return None
    body = response.content or b""
    content_type = response.headers.get("Content-Type", "")
    if not body.startswith(_PDF_MAGIC) and "pdf" not in content_type.lower():
        logger.debug(f"{url} did not return a PDF (Content-Type: {content_type!r})")
        return None
    if len(body) > max_bytes:
        logger.debug(f"{url} returned {len(body)} bytes, over the {max_bytes} limit")
        return None
    return body


def _unpaywall_pdf_url(doi: str, email: str) -> str | None:
    try:
        payload = http_get_with_retry(
            _UNPAYWALL.format(doi=doi), params={"email": email}, retries=2
        ).json()
    except Exception as exc:  # noqa: BLE001 - Unpaywall is best-effort
        logger.debug(f"Unpaywall lookup failed for {doi}: {exc}")
        return None
    location = payload.get("best_oa_location") or {}
    return location.get("url_for_pdf") or location.get("url")


def _europepmc_pdf_url(doi: str) -> str | None:
    """Resolve a DOI to its Europe PMC open-access PDF, if one exists."""
    params = {"query": f'DOI:"{doi}"', "format": "json", "pageSize": 1, "resultType": "lite"}
    try:
        results = http_get_with_retry(_EPMC_SEARCH, params=params, retries=2).json()
        hits = results.get("resultList", {}).get("result", [])
    except Exception as exc:  # noqa: BLE001 - Europe PMC is best-effort
        logger.debug(f"Europe PMC lookup failed for {doi}: {exc}")
        return None
    if not hits:
        return None
    pmcid = hits[0].get("pmcid")
    return EPMC_PDF_URL.format(pmcid=pmcid) if pmcid else None


def resolve_pdf(paper: Paper, config) -> FullTextResult:
    """Walk the OA ladder for *paper*, stopping at the first real PDF."""
    settings = config.fulltext
    if not settings.get("enabled", True):
        return FullTextResult(pdf_bytes=None, oa_status=paper.oa_status)

    max_bytes = int(settings.get("max_bytes") or 20_000_000)

    if paper.pdf_url:
        body = _fetch_pdf(paper.pdf_url, max_bytes)
        if body:
            return FullTextResult(pdf_bytes=body, oa_status="open", source="direct")

    email = settings.get("unpaywall_email")
    if paper.doi and email:
        url = _unpaywall_pdf_url(paper.doi, email)
        if url:
            body = _fetch_pdf(url, max_bytes)
            if body:
                return FullTextResult(pdf_bytes=body, oa_status="open", source="unpaywall")

    if paper.doi:
        url = _europepmc_pdf_url(paper.doi)
        if url:
            body = _fetch_pdf(url, max_bytes)
            if body:
                return FullTextResult(pdf_bytes=body, oa_status="open", source="europepmc")

    return FullTextResult(pdf_bytes=None, oa_status="closed")


def _filename_for(paper: Paper, index: int) -> str:
    """A filename a person can recognise in a file browser without opening it.

    A DOI is stable but meaningless at a glance; year-author-title is what a
    reader actually wants to see.  A short hash suffix — derived from the DOI
    when there is one, so a rerun reproduces the same name rather than
    orphaning the previous file — keeps two papers with the same year, same
    first author, and a title identical in its first 80 characters from
    silently overwriting each other after truncation.
    """
    if not paper.pub_date and not paper.authors and not paper.title:
        return f"paper-{index}.pdf"

    year = str(paper.pub_date.year) if paper.pub_date else "unknown"
    author = paper.authors[0] if paper.authors else "unknown"
    title = (paper.title or "")[:_TITLE_MAX_LEN]

    identity = paper.doi or paper.title or str(index)
    suffix = hashlib.md5(identity.encode("utf-8")).hexdigest()[:6]

    stem = f"{year}-{author}-{title}-{suffix}"
    return _UNSAFE_NAME_RE.sub("_", stem) + ".pdf"


def _title_words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def _matches_title(title: str, full_text: str) -> bool:
    """Best-effort check that *full_text* is actually about *title*.

    Titles too short to carry enough distinctive words are let through
    unchecked — rejecting those risks a false positive on a real match, and
    a short/generic title is exactly where that risk is highest.
    """
    words = _title_words(title)
    if len(words) < _MIN_TITLE_WORDS:
        return True
    haystack = _title_words(full_text[:_TITLE_MATCH_WINDOW])
    hits = sum(1 for w in words if w in haystack)
    return (hits / len(words)) >= _TITLE_MATCH_RATIO


def download_fulltext(papers: list[Paper], config, out_dir: str) -> None:
    """Resolve, save and text-extract full text for *papers*, in place."""
    os.makedirs(out_dir, exist_ok=True)
    hits = 0
    for index, paper in enumerate(papers):
        result = resolve_pdf(paper, config)
        paper.oa_status = result.oa_status
        if result.pdf_bytes is None:
            continue
        path = os.path.join(out_dir, _filename_for(paper, index))
        with open(path, "wb") as handle:
            handle.write(result.pdf_bytes)
        paper.pdf_path = path
        hits += 1
        try:
            paper.full_text = extract_markdown_from_pdf(path)
        except Exception as exc:  # noqa: BLE001 - keep the PDF even if extraction fails
            logger.warning(f"Failed to extract markdown from {path}: {exc}")
            paper.full_text = None

        if paper.full_text and not _matches_title(paper.title, paper.full_text):
            logger.warning(
                f"Discarding full text fetched for {paper.title!r} (source={result.source}): "
                "it does not mention the title, so it is very likely the wrong paper"
            )
            os.remove(path)
            paper.pdf_path = None
            paper.full_text = None
            paper.oa_status = "closed"
            hits -= 1
            if result.source in _DOI_KEYED_SOURCES:
                # The fetch was keyed on this DOI and landed on a different
                # paper, so the DOI itself is suspect: stop building the
                # public link from it rather than send a reader to a paper
                # other than the one shown under this title.
                paper.doi = None
    logger.info(f"Full text resolved for {hits}/{len(papers)} papers")
