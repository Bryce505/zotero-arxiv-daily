"""Europe PMC retrieval.

No API key required, and the response says outright whether a record has open
full text — which feeds straight into the full-text ladder.
"""

from datetime import date, datetime

from loguru import logger

from ..protocol import Paper
from ..utils import http_get_with_retry
from .query_base import BaseQueryRetriever, register_query_retriever

_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


@register_query_retriever("europepmc")
class EuropepmcRetriever(BaseQueryRetriever):

    @staticmethod
    def _parse_date(raw: str | None) -> date | None:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date() if raw else None
        except ValueError:
            return None

    def _to_paper(self, item: dict) -> Paper | None:
        abstract = (item.get("abstractText") or "").strip()
        if not abstract:
            return None
        is_open = item.get("isOpenAccess") == "Y"
        pmcid = item.get("pmcid")
        return Paper(
            source="europepmc",
            title=(item.get("title") or "").strip().rstrip("."),
            authors=[a.strip() for a in (item.get("authorString") or "").split(",") if a.strip()],
            abstract=abstract,
            url=f"https://europepmc.org/article/MED/{item.get('id')}",
            doi=item.get("doi"),
            journal=item.get("journalTitle"),
            pub_date=self._parse_date(item.get("firstPublicationDate")),
            oa_status="open" if is_open else "closed",
            pdf_url=(
                f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
                if pmcid and is_open
                else None
            ),
        )

    def search(self, query: str, start: date, end: date, limit: int) -> list[Paper]:
        if not query.strip():
            return []
        windowed = f"({query}) AND (FIRST_PDATE:[{start:%Y-%m-%d} TO {end:%Y-%m-%d}])"
        params = {"query": windowed, "format": "json", "pageSize": limit, "resultType": "core"}
        try:
            payload = http_get_with_retry(_SEARCH, params=params).json()
            items = payload.get("resultList", {}).get("result", [])
        except Exception as exc:  # noqa: BLE001 - one dead source must not kill the run
            logger.warning(f"Europe PMC search failed for {query!r}: {exc}")
            return []
        papers = []
        for item in items:
            paper = self._to_paper(item)
            if paper is not None:
                papers.append(paper)
        return papers
