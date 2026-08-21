"""Europe PMC REST retriever."""

from datetime import date
from types import SimpleNamespace

import pytest
import requests

from zotero_arxiv_daily.retriever.europepmc_retriever import EuropepmcRetriever

RESPONSE = {
    "resultList": {
        "result": [
            {
                "id": "40000001",
                "doi": "10.1016/j.chroma.2026.01.001",
                "title": "Host cell protein quantitation by LC-MS",
                "abstractText": "A validated HCP assay.",
                "authorString": "Smith J, Doe A",
                "journalTitle": "J Chromatogr A",
                "firstPublicationDate": "2026-08-18",
                "isOpenAccess": "Y",
                "pmcid": "PMC1234567",
            },
            {
                "id": "40000002",
                "title": "No abstract here",
                "authorString": "Lee K",
                "firstPublicationDate": "2026-08-19",
                "isOpenAccess": "N",
            },
        ]
    }
}


@pytest.fixture()
def mock_epmc(monkeypatch):
    calls = []

    def _patched(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: RESPONSE)

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    return calls


def test_europepmc_parses_results(config, mock_epmc):
    papers = EuropepmcRetriever(config).search("HCP", date(2026, 8, 15), date(2026, 8, 21), 20)
    assert len(papers) == 1  # abstract-less record dropped
    paper = papers[0]
    assert paper.title == "Host cell protein quantitation by LC-MS"
    assert paper.doi == "10.1016/j.chroma.2026.01.001"
    assert paper.pub_date == date(2026, 8, 18)
    assert paper.authors == ["Smith J", "Doe A"]
    assert paper.oa_status == "open"


def test_europepmc_marks_closed_access(config, monkeypatch):
    closed = {"resultList": {"result": [dict(RESPONSE["resultList"]["result"][0], isOpenAccess="N")]}}
    monkeypatch.setattr(
        requests, "get",
        lambda url, **kw: SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: closed),
    )
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    papers = EuropepmcRetriever(config).search("HCP", date(2026, 8, 15), date(2026, 8, 21), 20)
    assert papers[0].oa_status == "closed"
    assert papers[0].pdf_url is None


def test_europepmc_embeds_the_date_window_in_the_query(config, mock_epmc):
    EuropepmcRetriever(config).search("HCP", date(2026, 8, 15), date(2026, 8, 21), 20)
    _, params = mock_epmc[0]
    assert "FIRST_PDATE:[2026-08-15 TO 2026-08-21]" in params["query"]
    assert params["pageSize"] == 20


def test_europepmc_returns_nothing_for_an_empty_query(config, mock_epmc):
    assert EuropepmcRetriever(config).search("  ", date(2026, 8, 15), date(2026, 8, 21), 20) == []
    assert mock_epmc == []


def test_europepmc_tolerates_an_unparseable_date(config, monkeypatch):
    odd = {"resultList": {"result": [dict(RESPONSE["resultList"]["result"][0], firstPublicationDate="not-a-date")]}}
    monkeypatch.setattr(
        requests, "get",
        lambda url, **kw: SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: odd),
    )
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    assert EuropepmcRetriever(config).search("HCP", date(2026, 8, 15), date(2026, 8, 21), 20)[0].pub_date is None


def test_europepmc_survives_a_transport_failure(config, monkeypatch):
    def _boom(url, **kwargs):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(requests, "get", _boom)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    assert EuropepmcRetriever(config).search("HCP", date(2026, 8, 15), date(2026, 8, 21), 20) == []
