"""Fetch open-access full text, in descending order of reliability.

The ladder is: a URL the source already handed us, then Unpaywall's best OA
location, then Europe PMC's OA service.  The first hit wins.  Anything that
stays behind a paywall is left abstract-only and flagged, so the report can
list it under "needs manual retrieval" — publisher-proxy automation is
deliberately out of scope (spec finding 4).
"""

import os
import re
from dataclasses import dataclass

from loguru import logger

from ..protocol import Paper
from ..utils import extract_markdown_from_pdf, http_get_with_retry

_UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"
_EPMC_PDF = "https://europepmc.org/api/fulltextRepo?pprId={doi}&type=FILE&fileName=main.pdf"
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")
_PDF_MAGIC = b"%PDF"


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
        body = _fetch_pdf(_EPMC_PDF.format(doi=paper.doi), max_bytes)
        if body:
            return FullTextResult(pdf_bytes=body, oa_status="open", source="europepmc")

    return FullTextResult(pdf_bytes=None, oa_status="closed")


def _filename_for(paper: Paper, index: int) -> str:
    stem = paper.doi or f"paper-{index}"
    return _UNSAFE_NAME_RE.sub("_", stem) + ".pdf"


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
    logger.info(f"Full text resolved for {hits}/{len(papers)} papers")
