"""Journal-literature fields added to Paper for the weekly digest."""

from datetime import date

from zotero_arxiv_daily.protocol import Paper


def make_paper(**kw) -> Paper:
    base = dict(
        source="pubmed",
        title="A paper",
        authors=["Smith, J."],
        abstract="An abstract.",
        url="https://example.org/1",
    )
    base.update(kw)
    return Paper(**base)


def test_journal_fields_default_to_empty():
    paper = make_paper()
    assert paper.doi is None
    assert paper.journal is None
    assert paper.pub_date is None
    assert paper.pdf_path is None
    assert paper.oa_status == "unknown"
    assert paper.extraction is None
    assert paper.cluster is None
    assert paper.is_backfill is False
    assert paper.cited_by_count is None


def test_journal_fields_round_trip():
    paper = make_paper(
        doi="10.1016/j.chroma.2026.01.001",
        journal="J Chromatogr A",
        pub_date=date(2026, 8, 18),
        cited_by_count=42,
        is_backfill=True,
    )
    assert paper.journal == "J Chromatogr A"
    assert paper.pub_date == date(2026, 8, 18)
    assert paper.cited_by_count == 42
    assert paper.is_backfill is True


def test_doi_url_builds_a_resolver_link():
    paper = make_paper(doi="10.1016/j.chroma.2026.01.001")
    assert paper.doi_url == "https://doi.org/10.1016/j.chroma.2026.01.001"


def test_doi_url_is_none_without_a_doi():
    assert make_paper().doi_url is None
