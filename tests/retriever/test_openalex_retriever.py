"""OpenAlex retrieval, including the highly-cited backfill query."""

from datetime import date
from types import SimpleNamespace

import pytest
import requests

from zotero_arxiv_daily.retriever.openalex_retriever import OpenalexRetriever, invert_abstract

WORK = {
    "doi": "https://doi.org/10.1016/j.chroma.2026.01.001",
    "title": "Charge heterogeneity of therapeutic proteins",
    "abstract_inverted_index": {"A": [0], "cIEF": [1], "method": [2]},
    "authorships": [{"author": {"display_name": "J Smith"}}, {"author": {"display_name": "A Doe"}}],
    "primary_location": {"source": {"display_name": "J Chromatogr A"}},
    "publication_date": "2026-08-18",
    "cited_by_count": 137,
    "open_access": {"is_oa": True, "oa_url": "https://example.org/paper.pdf"},
}

RESPONSE = {
    "results": [
        WORK,
        dict(WORK, abstract_inverted_index=None, doi="https://doi.org/10.1016/no-abstract"),
    ]
}


@pytest.fixture()
def mock_openalex(monkeypatch):
    calls = []

    def _patched(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: RESPONSE)

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    return calls


def test_invert_abstract_restores_word_order():
    assert invert_abstract({"the": [0, 2], "cat": [1]}) == "the cat the"


def test_invert_abstract_handles_a_missing_index():
    assert invert_abstract(None) == ""
    assert invert_abstract({}) == ""


def test_openalex_parses_works(config, mock_openalex):
    papers = OpenalexRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20)
    assert len(papers) == 1  # abstract-less work dropped
    paper = papers[0]
    assert paper.doi == "10.1016/j.chroma.2026.01.001"  # resolver prefix stripped
    assert paper.abstract == "A cIEF method"
    assert paper.cited_by_count == 137
    assert paper.journal == "J Chromatogr A"
    assert paper.pub_date == date(2026, 8, 18)
    assert paper.oa_status == "open"
    assert paper.pdf_url == "https://example.org/paper.pdf"
    assert paper.authors == ["J Smith", "A Doe"]


def test_openalex_sends_the_publication_date_filter(config, mock_openalex):
    OpenalexRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20)
    _, params = mock_openalex[0]
    assert "from_publication_date:2026-08-15" in params["filter"]
    assert "to_publication_date:2026-08-21" in params["filter"]


def test_search_results_are_not_marked_as_backfill(config, mock_openalex):
    papers = OpenalexRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20)
    assert papers[0].is_backfill is False


def test_highly_cited_sorts_by_citation_count_without_a_date_bound(config, mock_openalex):
    papers = OpenalexRetriever(config).search_highly_cited("cIEF", 5)
    _, params = mock_openalex[0]
    assert params["sort"] == "cited_by_count:desc"
    assert "from_publication_date" not in params.get("filter", "")
    assert papers[0].is_backfill is True


def test_openalex_returns_nothing_for_an_empty_query(config, mock_openalex):
    assert OpenalexRetriever(config).search("", date(2026, 8, 15), date(2026, 8, 21), 20) == []
    assert OpenalexRetriever(config).search_highly_cited("  ", 5) == []
    assert mock_openalex == []


def test_openalex_survives_a_transport_failure(config, monkeypatch):
    def _boom(url, **kwargs):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(requests, "get", _boom)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    assert OpenalexRetriever(config).search("q", date(2026, 8, 15), date(2026, 8, 21), 20) == []
    assert OpenalexRetriever(config).search_highly_cited("q", 5) == []
