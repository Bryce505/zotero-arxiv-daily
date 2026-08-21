"""OpenAlex retrieval.

Doubles as the backfill source: ``cited_by_count`` lets a thin week be topped
up with the field's established papers rather than padding with weak matches.
Abstracts arrive as an inverted index and must be reassembled.
"""

from datetime import date, datetime

from loguru import logger

from ..protocol import Paper
from ..utils import http_get_with_retry
from .query_base import BaseQueryRetriever, register_query_retriever

_WORKS = "https://api.openalex.org/works"


def invert_abstract(index: dict[str, list[int]] | None) -> str:
    """Rebuild plain text from OpenAlex's inverted abstract index."""
    if not index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, spots in index.items():
        positions.extend((spot, word) for spot in spots)
    return " ".join(word for _, word in sorted(positions))


@register_query_retriever("openalex")
class OpenalexRetriever(BaseQueryRetriever):

    @staticmethod
    def _parse_date(raw: str | None) -> date | None:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date() if raw else None
        except ValueError:
            return None

    def _to_paper(self, work: dict, *, is_backfill: bool) -> Paper | None:
        abstract = invert_abstract(work.get("abstract_inverted_index"))
        if not abstract:
            return None
        doi = (work.get("doi") or "").replace("https://doi.org/", "").strip() or None
        source_block = (work.get("primary_location") or {}).get("source") or {}
        oa = work.get("open_access") or {}
        return Paper(
            source="openalex",
            title=(work.get("title") or "").strip(),
            authors=[
                (a.get("author") or {}).get("display_name", "")
                for a in work.get("authorships") or []
                if (a.get("author") or {}).get("display_name")
            ],
            abstract=abstract,
            url=f"https://doi.org/{doi}" if doi else work.get("id", ""),
            doi=doi,
            journal=source_block.get("display_name"),
            pub_date=self._parse_date(work.get("publication_date")),
            cited_by_count=work.get("cited_by_count"),
            oa_status="open" if oa.get("is_oa") else "closed",
            pdf_url=oa.get("oa_url"),
            is_backfill=is_backfill,
        )

    def _query(self, params: dict, *, is_backfill: bool) -> list[Paper]:
        mailto = self._setting("mailto")
        if mailto:
            params = params | {"mailto": mailto}
        try:
            results = http_get_with_retry(_WORKS, params=params).json().get("results", [])
        except Exception as exc:  # noqa: BLE001 - one dead source must not kill the run
            logger.warning(f"OpenAlex query failed: {exc}")
            return []
        papers = []
        for work in results:
            paper = self._to_paper(work, is_backfill=is_backfill)
            if paper is not None:
                papers.append(paper)
        return papers

    def search(self, query: str, start: date, end: date, limit: int) -> list[Paper]:
        if not query.strip():
            return []
        params = {
            "search": query,
            "per-page": min(limit, 200),
            "filter": (
                f"from_publication_date:{start:%Y-%m-%d},"
                f"to_publication_date:{end:%Y-%m-%d},"
                "type:article"
            ),
        }
        return self._query(params, is_backfill=False)

    def search_highly_cited(self, query: str, limit: int) -> list[Paper]:
        """Return the most-cited papers matching *query*, any publication date."""
        if not query.strip():
            return []
        params = {
            "search": query,
            "per-page": min(limit, 200),
            "sort": "cited_by_count:desc",
            "filter": "type:article",
        }
        return self._query(params, is_backfill=True)
