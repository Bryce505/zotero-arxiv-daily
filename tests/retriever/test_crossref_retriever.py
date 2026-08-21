"""Crossref REST retriever."""

from datetime import date
from types import SimpleNamespace

import pytest
import requests

from zotero_arxiv_daily.retriever.crossref_retriever import CrossrefRetriever

RESPONSE = {
    "message": {
        "items": [
            {
                "DOI": "10.1021/acs.analchem.6b00001",
                "title": ["Size variant analysis by SEC-MALS"],
                "abstract": "<jats:p>We report a SEC-MALS method.</jats:p>",
                "author": [{"family": "Smith", "given": "J"}, {"family": "Doe", "given": "A"}],
                "container-title": ["Anal Chem"],
                "created": {"date-parts": [[2026, 8, 18]]},
            },
            {
                "DOI": "10.1021/acs.analchem.6b00002",
                "title": ["No abstract"],
                "author": [],
                "container-title": ["Anal Chem"],
                "created": {"date-parts": [[2026, 8, 19]]},
            },
        ]
    }
}


@pytest.fixture()
def mock_crossref(monkeypatch):
    calls = []

    def _patched(url, **kwargs):
        calls.append((url, kwargs.get("params", {}), kwargs.get("headers", {})))
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: RESPONSE)

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    return calls


def test_crossref_parses_items_and_strips_jats(config, mock_crossref):
    papers = CrossrefRetriever(config).search("SEC-MALS", date(2026, 8, 15), date(2026, 8, 21), 20)
    assert len(papers) == 1
    paper = papers[0]
    assert paper.title == "Size variant analysis by SEC-MALS"
    assert paper.abstract == "We report a SEC-MALS method."
    assert paper.journal == "Anal Chem"
    assert paper.pub_date == date(2026, 8, 18)
    assert paper.authors == ["Smith J", "Doe A"]
    assert paper.url == "https://doi.org/10.1021/acs.analchem.6b00001"


def test_crossref_sends_the_date_filter_and_polite_header(config, mock_crossref):
    from omegaconf import open_dict

    with open_dict(config.source):
        config.source.crossref = {"mailto": "someone@example.org"}
    CrossrefRetriever(config).search("SEC-MALS", date(2026, 8, 15), date(2026, 8, 21), 20)
    _, params, headers = mock_crossref[0]
    assert "from-created-date:2026-08-15" in params["filter"]
    assert "until-created-date:2026-08-21" in params["filter"]
    assert params["rows"] == 20
    assert "someone@example.org" in headers["User-Agent"]


def test_crossref_sends_a_plain_agent_without_a_mailto(config, mock_crossref):
    CrossrefRetriever(config).search("SEC-MALS", date(2026, 8, 15), date(2026, 8, 21), 20)
    _, _, headers = mock_crossref[0]
    assert "mailto" not in headers["User-Agent"]


def test_crossref_tolerates_a_partial_date(config, monkeypatch):
    partial = {"message": {"items": [dict(RESPONSE["message"]["items"][0], created={"date-parts": [[2026]]})]}}
    monkeypatch.setattr(
        requests, "get",
        lambda url, **kw: SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: partial),
    )
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    assert CrossrefRetriever(config).search("q", date(2026, 8, 15), date(2026, 8, 21), 20)[0].pub_date == date(2026, 1, 1)


def test_crossref_returns_nothing_for_an_empty_query(config, mock_crossref):
    assert CrossrefRetriever(config).search("", date(2026, 8, 15), date(2026, 8, 21), 20) == []
    assert mock_crossref == []


def test_crossref_survives_a_transport_failure(config, monkeypatch):
    def _boom(url, **kwargs):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(requests, "get", _boom)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    assert CrossrefRetriever(config).search("q", date(2026, 8, 15), date(2026, 8, 21), 20) == []
