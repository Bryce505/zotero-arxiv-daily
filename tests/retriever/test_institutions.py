"""Author affiliations lifted from each source's own metadata."""

from xml.etree import ElementTree

from zotero_arxiv_daily.retriever.crossref_retriever import CrossrefRetriever
from zotero_arxiv_daily.retriever.europepmc_retriever import EuropepmcRetriever
from zotero_arxiv_daily.retriever.openalex_retriever import OpenalexRetriever
from zotero_arxiv_daily.retriever.pubmed_retriever import PubmedRetriever

OPENALEX_WORK = {
    "title": "A paper",
    "doi": "https://doi.org/10.1000/x",
    "abstract_inverted_index": {"An": [0], "abstract": [1]},
    "publication_date": "2026-08-18",
    "authorships": [
        {
            "author": {"display_name": "A Researcher"},
            "institutions": [
                {"display_name": "Amgen Inc.", "type": "company"},
                {"display_name": "Stanford University", "type": "education"},
            ],
        }
    ],
}


def test_openalex_records_every_institution(config):
    paper = OpenalexRetriever(config)._to_paper(OPENALEX_WORK, is_backfill=False)
    assert paper.institutions == ["Amgen Inc.", "Stanford University"]


def test_openalex_flags_only_the_companies(config):
    paper = OpenalexRetriever(config)._to_paper(OPENALEX_WORK, is_backfill=False)
    assert paper.company_institutions == ["Amgen Inc."]


def test_openalex_survives_a_work_with_no_institutions(config):
    work = dict(OPENALEX_WORK, authorships=[{"author": {"display_name": "A"}}])
    paper = OpenalexRetriever(config)._to_paper(work, is_backfill=False)
    assert paper.institutions == []
    assert paper.company_institutions == []


def test_openalex_deduplicates_repeated_institutions(config):
    work = dict(
        OPENALEX_WORK,
        authorships=[OPENALEX_WORK["authorships"][0], OPENALEX_WORK["authorships"][0]],
    )
    paper = OpenalexRetriever(config)._to_paper(work, is_backfill=False)
    assert paper.institutions == ["Amgen Inc.", "Stanford University"]


def test_openalex_keeps_a_company_flag_seen_only_on_a_later_occurrence(config):
    # Same institution name across two authorships: the first occurrence
    # carries no "company" type, only the second does. Dedup-by-name must
    # not suppress that second occurrence before its type is read.
    work = dict(
        OPENALEX_WORK,
        authorships=[
            {
                "author": {"display_name": "A Researcher"},
                "institutions": [{"display_name": "Amgen Inc."}],
            },
            {
                "author": {"display_name": "B Researcher"},
                "institutions": [{"display_name": "Amgen Inc.", "type": "company"}],
            },
        ],
    )
    paper = OpenalexRetriever(config)._to_paper(work, is_backfill=False)
    assert paper.institutions == ["Amgen Inc."]
    assert paper.company_institutions == ["Amgen Inc."]


PUBMED_XML = """
<PubmedArticle><MedlineCitation><PMID>1</PMID><Article>
  <ArticleTitle>A paper</ArticleTitle>
  <Abstract><AbstractText>An abstract.</AbstractText></Abstract>
  <Journal><Title>mAbs</Title></Journal>
  <AuthorList>
    <Author><LastName>Doe</LastName><ForeName>Jane</ForeName>
      <AffiliationInfo><Affiliation>Pfizer Inc., New York, NY.</Affiliation></AffiliationInfo>
    </Author>
    <Author><LastName>Roe</LastName><ForeName>Ann</ForeName>
      <AffiliationInfo><Affiliation>Pfizer Inc., New York, NY.</Affiliation></AffiliationInfo>
    </Author>
  </AuthorList>
</Article></MedlineCitation></PubmedArticle>
"""


def test_pubmed_records_affiliations_once(config):
    article = ElementTree.fromstring(PUBMED_XML)
    paper = PubmedRetriever(config)._article_to_paper(article)
    assert paper.institutions == ["Pfizer Inc., New York, NY."]


def test_pubmed_survives_an_article_with_no_affiliation(config):
    stripped = PUBMED_XML.replace(
        "<AffiliationInfo><Affiliation>Pfizer Inc., New York, NY.</Affiliation></AffiliationInfo>", ""
    )
    paper = PubmedRetriever(config)._article_to_paper(ElementTree.fromstring(stripped))
    assert paper.institutions == []


def test_europepmc_records_the_affiliation(config):
    item = {
        "title": "A paper",
        "abstractText": "An abstract.",
        "authorString": "Doe J",
        "id": "1",
        "journalTitle": "mAbs",
        "affiliation": "Lonza AG, Basel, Switzerland",
    }
    assert EuropepmcRetriever(config)._to_paper(item).institutions == ["Lonza AG, Basel, Switzerland"]


def test_europepmc_survives_a_missing_affiliation(config):
    item = {"title": "A paper", "abstractText": "An abstract.", "authorString": "Doe J", "id": "1"}
    assert EuropepmcRetriever(config)._to_paper(item).institutions == []


def test_crossref_records_author_affiliations(config):
    item = {
        "title": ["A paper"],
        "abstract": "<jats:p>An abstract.</jats:p>",
        "DOI": "10.1000/x",
        "author": [{"family": "Doe", "given": "Jane", "affiliation": [{"name": "Amgen Inc."}]}],
        "container-title": ["mAbs"],
    }
    assert CrossrefRetriever(config)._to_paper(item).institutions == ["Amgen Inc."]


def test_crossref_survives_authors_with_no_affiliation(config):
    item = {
        "title": ["A paper"],
        "abstract": "<jats:p>An abstract.</jats:p>",
        "DOI": "10.1000/x",
        "author": [{"family": "Doe", "given": "Jane"}],
        "container-title": ["mAbs"],
    }
    assert CrossrefRetriever(config)._to_paper(item).institutions == []


def test_no_source_populates_company_institutions_except_openalex(config):
    item = {"title": "A paper", "abstractText": "An abstract.", "authorString": "Doe J", "id": "1",
            "affiliation": "Lonza AG"}
    # Only OpenAlex reports an institution *type*; the others cannot know.
    assert EuropepmcRetriever(config)._to_paper(item).company_institutions == []
