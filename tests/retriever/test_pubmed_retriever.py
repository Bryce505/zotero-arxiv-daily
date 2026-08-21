"""PubMed E-utilities retriever."""

from datetime import date
from types import SimpleNamespace

import pytest
import requests

from zotero_arxiv_daily.retriever.pubmed_retriever import PubmedRetriever

ESEARCH_JSON = {"esearchresult": {"idlist": ["40000001", "40000002"]}}

EFETCH_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation>
   <PMID>40000001</PMID>
   <Article>
    <ArticleTitle>Charge variant analysis of a monoclonal antibody</ArticleTitle>
    <Abstract><AbstractText>We describe a cIEF method.</AbstractText></Abstract>
    <AuthorList>
     <Author><LastName>Smith</LastName><ForeName>J</ForeName></Author>
     <Author><LastName>Doe</LastName><ForeName>A</ForeName></Author>
    </AuthorList>
    <Journal><Title>J Chromatogr A</Title></Journal>
   </Article>
  </MedlineCitation>
  <PubmedData>
   <History><PubMedPubDate PubStatus="pubmed"><Year>2026</Year><Month>8</Month><Day>18</Day></PubMedPubDate></History>
   <ArticleIdList><ArticleId IdType="doi">10.1016/j.chroma.2026.01.001</ArticleId></ArticleIdList>
  </PubmedData>
 </PubmedArticle>
 <PubmedArticle>
  <MedlineCitation>
   <PMID>40000002</PMID>
   <Article>
    <ArticleTitle>A paper with no abstract</ArticleTitle>
    <AuthorList><Author><LastName>Lee</LastName><ForeName>K</ForeName></Author></AuthorList>
    <Journal><Title>Anal Chem</Title></Journal>
   </Article>
  </MedlineCitation>
 </PubmedArticle>
</PubmedArticleSet>
"""


@pytest.fixture()
def mock_pubmed(monkeypatch):
    calls = []

    def _patched(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        if "esearch" in url:
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: ESEARCH_JSON,
                text="",
            )
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, text=EFETCH_XML)

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    return calls


def test_pubmed_search_parses_articles(config, mock_pubmed):
    papers = PubmedRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20)
    assert len(papers) == 1  # the abstract-less record is dropped
    paper = papers[0]
    assert paper.title == "Charge variant analysis of a monoclonal antibody"
    assert paper.doi == "10.1016/j.chroma.2026.01.001"
    assert paper.journal == "J Chromatogr A"
    assert paper.pub_date == date(2026, 8, 18)
    assert paper.authors == ["Smith J", "Doe A"]
    assert paper.source == "pubmed"
    assert paper.url == "https://pubmed.ncbi.nlm.nih.gov/40000001/"


def test_pubmed_sends_the_date_window_and_limit(config, mock_pubmed):
    PubmedRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20)
    _, params = mock_pubmed[0]
    assert params["mindate"] == "2026/08/15"
    assert params["maxdate"] == "2026/08/21"
    assert params["retmax"] == 20
    assert params["datetype"] == "edat"


def test_pubmed_returns_nothing_for_an_empty_query(config, mock_pubmed):
    assert PubmedRetriever(config).search("", date(2026, 8, 15), date(2026, 8, 21), 20) == []
    assert mock_pubmed == []


def test_pubmed_returns_nothing_when_no_ids_match(config, monkeypatch):
    def _patched(url, **kwargs):
        return SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"esearchresult": {"idlist": []}},
            text="",
        )

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    assert PubmedRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20) == []


def test_pubmed_passes_the_api_key_when_configured(config, mock_pubmed):
    from omegaconf import open_dict

    with open_dict(config.source):
        config.source.pubmed = {"api_key": "secret-key", "tool": "t", "email": "e@example.org"}
    PubmedRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20)
    _, params = mock_pubmed[0]
    assert params["api_key"] == "secret-key"
    assert params["email"] == "e@example.org"


def test_pubmed_omits_the_api_key_when_absent(config, mock_pubmed):
    PubmedRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20)
    _, params = mock_pubmed[0]
    assert "api_key" not in params


def test_pubmed_survives_a_malformed_xml_body(config, monkeypatch):
    def _patched(url, **kwargs):
        if "esearch" in url:
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: ESEARCH_JSON, text="")
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, text="<not-xml")

    monkeypatch.setattr(requests, "get", _patched)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    assert PubmedRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20) == []


def test_pubmed_survives_a_transport_failure(config, monkeypatch):
    def _boom(url, **kwargs):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(requests, "get", _boom)
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)
    assert PubmedRetriever(config).search("cIEF", date(2026, 8, 15), date(2026, 8, 21), 20) == []
