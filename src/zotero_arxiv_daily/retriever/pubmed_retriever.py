"""PubMed retrieval over E-utilities (esearch then efetch).

An NCBI API key lifts the rate limit from 3 to 10 requests/second; it is
optional and the retriever works without one.
"""

from datetime import date
from xml.etree import ElementTree

from loguru import logger

from ..protocol import Paper
from ..utils import http_get_with_retry
from .query_base import BaseQueryRetriever, register_query_retriever

_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


@register_query_retriever("pubmed")
class PubmedRetriever(BaseQueryRetriever):

    def _common_params(self) -> dict:
        params = {"db": "pubmed", "tool": self._setting("tool", "zotero-cmc-weekly")}
        email = self._setting("email")
        if email:
            params["email"] = email
        api_key = self._setting("api_key")
        if api_key:
            params["api_key"] = api_key
        return params

    def _esearch(self, query: str, start: date, end: date, limit: int) -> list[str]:
        params = self._common_params() | {
            "term": query,
            "retmode": "json",
            "retmax": limit,
            "datetype": "edat",
            "mindate": start.strftime("%Y/%m/%d"),
            "maxdate": end.strftime("%Y/%m/%d"),
        }
        response = http_get_with_retry(_ESEARCH, params=params)
        return list(response.json().get("esearchresult", {}).get("idlist", []))

    def _efetch(self, pmids: list[str]) -> str:
        params = self._common_params() | {"id": ",".join(pmids), "retmode": "xml"}
        return http_get_with_retry(_EFETCH, params=params).text

    @staticmethod
    def _article_to_paper(article: ElementTree.Element) -> Paper | None:
        title_node = article.find(".//ArticleTitle")
        if title_node is None:
            return None
        title = "".join(title_node.itertext()).strip()

        abstract_nodes = article.findall(".//Abstract/AbstractText")
        abstract = " ".join("".join(n.itertext()).strip() for n in abstract_nodes).strip()
        if not abstract:
            # The reranker scores on abstracts; a record without one is unusable.
            return None

        authors = []
        for author in article.findall(".//AuthorList/Author"):
            name = f"{author.findtext('LastName') or ''} {author.findtext('ForeName') or ''}".strip()
            if name:
                authors.append(name)

        doi = None
        for article_id in article.findall(".//ArticleIdList/ArticleId"):
            if article_id.get("IdType") == "doi":
                doi = (article_id.text or "").strip() or None

        pub_date = None
        node = article.find('.//PubMedPubDate[@PubStatus="pubmed"]')
        if node is not None:
            try:
                pub_date = date(
                    int(node.findtext("Year")),
                    int(node.findtext("Month")),
                    int(node.findtext("Day")),
                )
            except (TypeError, ValueError):
                pub_date = None

        pmid = article.findtext(".//PMID") or ""
        return Paper(
            source="pubmed",
            title=title,
            authors=authors,
            abstract=abstract,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            doi=doi,
            journal=article.findtext(".//Journal/Title"),
            pub_date=pub_date,
        )

    def search(self, query: str, start: date, end: date, limit: int) -> list[Paper]:
        if not query.strip():
            return []
        try:
            pmids = self._esearch(query, start, end, limit)
            if not pmids:
                return []
            root = ElementTree.fromstring(self._efetch(pmids))
        except Exception as exc:  # noqa: BLE001 - one dead source must not kill the run
            logger.warning(f"PubMed search failed for {query!r}: {exc}")
            return []

        papers = []
        for article in root.findall(".//PubmedArticle"):
            try:
                paper = self._article_to_paper(article)
            except Exception as exc:  # noqa: BLE001 - skip the bad record, keep the good ones
                logger.warning(f"Skipping an unparseable PubMed record: {exc}")
                continue
            if paper is not None:
                papers.append(paper)
        return papers
