"""DOI normalisation and cross-source / cross-week de-duplication."""

import json

from zotero_arxiv_daily.dedup import (
    dedup_papers,
    drop_seen,
    load_seen,
    normalize_doi,
    save_seen,
    title_key,
)
from zotero_arxiv_daily.protocol import Paper


def make_paper(title="A paper", doi=None, source="pubmed") -> Paper:
    return Paper(
        source=source,
        title=title,
        authors=[],
        abstract="abs",
        url="https://example.org/1",
        doi=doi,
    )


def test_normalize_strips_resolver_prefix_and_lowercases():
    assert normalize_doi("https://doi.org/10.1016/J.Chroma.2026.01.001") == "10.1016/j.chroma.2026.01.001"


def test_normalize_strips_doi_scheme_and_whitespace():
    assert normalize_doi("  doi:10.1021/acs.analchem.6b00001 ") == "10.1021/acs.analchem.6b00001"


def test_normalize_rejects_non_dois():
    assert normalize_doi(None) is None
    assert normalize_doi("") is None
    assert normalize_doi("not-a-doi") is None


def test_normalize_rejects_a_registrant_code_that_is_too_short():
    """Real DOI prefixes carry a 4-9 digit registrant code.

    Anything shorter is a malformed identifier, and treating it as a DOI
    would let two unrelated papers collide on a garbage key.
    """
    assert normalize_doi("10.1/x") is None
    assert normalize_doi("10.1000/x") == "10.1000/x"


def test_title_key_ignores_case_punctuation_and_spacing():
    assert title_key("Charge  Variants: A Review!") == title_key("charge variants a review")


def test_dedup_keeps_the_first_paper_per_doi():
    papers = [
        make_paper(title="First", doi="10.1000/x", source="pubmed"),
        make_paper(title="Second", doi="https://doi.org/10.1000/X", source="crossref"),
    ]
    result = dedup_papers(papers)
    assert len(result) == 1
    assert result[0].title == "First"


def test_dedup_falls_back_to_title_for_papers_without_a_doi():
    papers = [
        make_paper(title="Charge Variants: A Review", doi=None),
        make_paper(title="charge variants a review", doi=None),
    ]
    assert len(dedup_papers(papers)) == 1


def test_dedup_keeps_distinct_papers():
    papers = [make_paper(title="A", doi="10.1000/a"), make_paper(title="B", doi="10.1000/b")]
    assert len(dedup_papers(papers)) == 2


def test_a_doi_bearing_paper_never_collapses_into_a_different_doi():
    papers = [make_paper(title="Same Title", doi="10.1000/a"), make_paper(title="Same Title", doi="10.1000/b")]
    assert len(dedup_papers(papers)) == 2


def test_drop_seen_removes_papers_whose_doi_was_already_sent():
    papers = [make_paper(doi="10.1000/a"), make_paper(doi="10.1000/b")]
    assert [p.doi for p in drop_seen(papers, {"10.1000/a"})] == ["10.1000/b"]


def test_drop_seen_keeps_papers_without_a_doi():
    papers = [make_paper(doi=None)]
    assert len(drop_seen(papers, {"10.1000/a"})) == 1


def test_seen_state_round_trips(tmp_path):
    path = str(tmp_path / "seen.json")
    save_seen(path, {"10.1000/b", "10.1000/a"})
    assert load_seen(path) == {"10.1000/a", "10.1000/b"}


def test_load_seen_on_a_missing_file_is_empty():
    assert load_seen("/nonexistent/seen.json") == set()


def test_saved_seen_state_is_sorted_for_stable_diffs(tmp_path):
    path = str(tmp_path / "seen.json")
    save_seen(path, {"10.1000/c", "10.1000/a", "10.1000/b"})
    with open(path, encoding="utf-8") as handle:
        assert json.load(handle) == ["10.1000/a", "10.1000/b", "10.1000/c"]


def test_corpus_doi_set_collects_normalised_dois_from_corpus_papers():
    from zotero_arxiv_daily.dedup import corpus_doi_set
    from zotero_arxiv_daily.protocol import CorpusPaper
    from datetime import datetime

    corpus = [
        CorpusPaper(title="a", abstract="x", added_date=datetime(2026, 1, 1), paths=[], doi="https://doi.org/10.1016/A"),
        CorpusPaper(title="b", abstract="x", added_date=datetime(2026, 1, 1), paths=[], doi=None),
        CorpusPaper(title="c", abstract="x", added_date=datetime(2026, 1, 1), paths=[], doi="not-a-doi"),
    ]
    assert corpus_doi_set(corpus) == {"10.1016/a"}


def test_dedup_merges_richer_fields_from_the_discarded_duplicate():
    """PubMed carries no OA data; Europe PMC does. Losing it wastes a PDF."""
    from zotero_arxiv_daily.protocol import Paper

    first = Paper(source="pubmed", title="T", authors=[], abstract="a", url="u",
                  doi="10.1016/x", journal="J Chromatogr A")
    second = Paper(source="europepmc", title="T", authors=[], abstract="a", url="u2",
                   doi="10.1016/X", pdf_url="https://oa.example.org/p.pdf", oa_status="open")
    merged = dedup_papers([first, second])
    assert len(merged) == 1
    assert merged[0].source == "pubmed"          # first wins the identity
    assert merged[0].journal == "J Chromatogr A"  # and keeps its own data
    assert merged[0].pdf_url == "https://oa.example.org/p.pdf"
    assert merged[0].oa_status == "open"


def test_dedup_does_not_let_a_duplicate_overwrite_existing_values():
    from zotero_arxiv_daily.protocol import Paper

    first = Paper(source="europepmc", title="T", authors=[], abstract="a", url="u",
                  doi="10.1016/x", pdf_url="https://first.example.org/p.pdf", oa_status="open")
    second = Paper(source="openalex", title="T", authors=[], abstract="a", url="u2",
                   doi="10.1016/x", pdf_url="https://second.example.org/p.pdf", cited_by_count=42)
    merged = dedup_papers([first, second])
    assert merged[0].pdf_url == "https://first.example.org/p.pdf"
    assert merged[0].cited_by_count == 42  # only the gaps are filled


def test_a_doi_less_record_collapses_into_its_doi_bearing_twin():
    """PubMed often omits the DOI that Crossref supplies for the same paper."""
    from zotero_arxiv_daily.protocol import Paper

    no_doi = Paper(source="pubmed", title="Charge Variants: A Review", authors=[], abstract="a", url="u")
    with_doi = Paper(
        source="crossref",
        title="charge variants a review",
        authors=[],
        abstract="a",
        url="u2",
        doi="10.1016/x",
    )
    merged = dedup_papers([no_doi, with_doi])
    assert len(merged) == 1
    assert merged[0].doi == "10.1016/x", "the DOI must survive so seen_dois records it"


def test_the_same_collapse_works_in_the_other_order():
    from zotero_arxiv_daily.protocol import Paper

    with_doi = Paper(source="crossref", title="Charge Variants", authors=[], abstract="a", url="u", doi="10.1016/x")
    no_doi = Paper(source="pubmed", title="charge variants", authors=[], abstract="a", url="u2")
    merged = dedup_papers([with_doi, no_doi])
    assert len(merged) == 1
    assert merged[0].doi == "10.1016/x"
