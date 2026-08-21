"""Crossref retrieval.

Crossref's polite pool wants a contact address in the User-Agent; supplying
one buys better latency and fewer throttles.  Abstracts arrive as JATS XML
fragments and are flattened to plain text.
"""

import re
from datetime import date

from loguru import logger

from ..protocol import Paper
from ..utils import http_get_with_retry
from .query_base import BaseQueryRetriever, register_query_retriever

_WORKS = "https://api.crossref.org/works"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@register_query_retriever("crossref")
class CrossrefRetriever(BaseQueryRetriever):

    @staticmethod
    def _strip_jats(raw: str | None) -> str:
        if not raw:
            return ""
        return _WS_RE.sub(" ", _TAG_RE.sub(" ", raw)).strip()

    @staticmethod
    def _parse_date(item: dict) -> date | None:
        parts = (item.get("created") or {}).get("date-parts") or []
        if not parts or not parts[0]:
            return None
        values = (list(parts[0]) + [1, 1])[:3]
        try:
            return date(int(values[0]), int(values[1]), int(values[2]))
        except (TypeError, ValueError):
            return None

    def _to_paper(self, item: dict) -> Paper | None:
        abstract = self._strip_jats(item.get("abstract"))
        if not abstract:
            return None
        titles = item.get("title") or []
        containers = item.get("container-title") or []
        doi = item.get("DOI")
        return Paper(
            source="crossref",
            title=(titles[0] if titles else "").strip(),
            authors=[
                f"{a.get('family', '')} {a.get('given', '')}".strip()
                for a in item.get("author") or []
                if a.get("family") or a.get("given")
            ],
            abstract=abstract,
            url=f"https://doi.org/{doi}" if doi else "",
            doi=doi,
            journal=containers[0] if containers else None,
            pub_date=self._parse_date(item),
        )

    def search(self, query: str, start: date, end: date, limit: int) -> list[Paper]:
        if not query.strip():
            return []
        params = {
            "query.bibliographic": query,
            "rows": limit,
            "filter": (
                f"from-created-date:{start:%Y-%m-%d},"
                f"until-created-date:{end:%Y-%m-%d},"
                "type:journal-article"
            ),
            "select": "DOI,title,abstract,author,container-title,created",
        }
        mailto = self._setting("mailto")
        agent = "zotero-cmc-weekly/1.0"
        headers = {"User-Agent": f"{agent} (mailto:{mailto})" if mailto else agent}
        try:
            payload = http_get_with_retry(_WORKS, params=params, headers=headers).json()
            items = payload.get("message", {}).get("items", [])
        except Exception as exc:  # noqa: BLE001 - one dead source must not kill the run
            logger.warning(f"Crossref search failed for {query!r}: {exc}")
            return []
        papers = []
        for item in items:
            paper = self._to_paper(item)
            if paper is not None:
                papers.append(paper)
        return papers
