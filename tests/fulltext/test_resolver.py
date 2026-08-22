"""The open-access full-text ladder."""

from types import SimpleNamespace

import pytest
import requests
from omegaconf import OmegaConf

from zotero_arxiv_daily.fulltext.resolver import download_fulltext, resolve_pdf
from zotero_arxiv_daily.protocol import Paper

PDF_BYTES = b"%PDF-1.7\nfake pdf body"


def make_paper(**kw) -> Paper:
    base = dict(source="pubmed", title="A paper", authors=[], abstract="abs", url="https://example.org/1")
    base.update(kw)
    return Paper(**base)


@pytest.fixture()
def cfg():
    return OmegaConf.create(
        {"fulltext": {"enabled": True, "unpaywall_email": "someone@example.org", "max_bytes": 20_000_000}}
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("zotero_arxiv_daily.utils.sleep", lambda _: None)


def pdf_response(body=PDF_BYTES):
    return SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        content=body,
        headers={"Content-Type": "application/pdf"},
    )


def test_a_known_oa_pdf_url_is_used_directly(cfg, monkeypatch):
    seen = []

    def _patched(url, **kw):
        seen.append(url)
        return pdf_response()

    monkeypatch.setattr(requests, "get", _patched)
    result = resolve_pdf(make_paper(pdf_url="https://example.org/direct.pdf", oa_status="open"), cfg)
    assert result.pdf_bytes == PDF_BYTES
    assert result.source == "direct"
    assert seen == ["https://example.org/direct.pdf"]


def test_unpaywall_is_consulted_when_there_is_no_direct_url(cfg, monkeypatch):
    def _patched(url, **kw):
        if "unpaywall" in url:
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {"best_oa_location": {"url_for_pdf": "https://oa.example.org/p.pdf"}},
            )
        return pdf_response()

    monkeypatch.setattr(requests, "get", _patched)
    result = resolve_pdf(make_paper(doi="10.1016/a"), cfg)
    assert result.pdf_bytes == PDF_BYTES
    assert result.source == "unpaywall"
    assert result.oa_status == "open"


def test_unpaywall_is_skipped_without_a_contact_email(monkeypatch):
    cfg = OmegaConf.create({"fulltext": {"enabled": True, "unpaywall_email": None, "max_bytes": 1000}})
    calls = []

    def _patched(url, **kw):
        calls.append(url)
        return pdf_response()

    monkeypatch.setattr(requests, "get", _patched)
    resolve_pdf(make_paper(doi="10.1016/a"), cfg)
    assert not any("unpaywall" in c for c in calls)


def test_a_closed_paper_yields_no_bytes(cfg, monkeypatch):
    def _patched(url, **kw):
        if "unpaywall" in url:
            return SimpleNamespace(
                status_code=200, raise_for_status=lambda: None, json=lambda: {"best_oa_location": None}
            )
        raise requests.HTTPError("403")

    monkeypatch.setattr(requests, "get", _patched)
    result = resolve_pdf(make_paper(doi="10.1016/a"), cfg)
    assert result.pdf_bytes is None
    assert result.oa_status == "closed"


def test_a_non_pdf_body_is_rejected(cfg, monkeypatch):
    html = SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        content=b"<html>paywall</html>",
        headers={"Content-Type": "text/html"},
    )
    monkeypatch.setattr(requests, "get", lambda url, **kw: html)
    assert resolve_pdf(make_paper(pdf_url="https://example.org/x.pdf"), cfg).pdf_bytes is None


def test_an_oversized_pdf_is_rejected(monkeypatch):
    cfg = OmegaConf.create({"fulltext": {"enabled": True, "unpaywall_email": None, "max_bytes": 5}})
    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())
    assert resolve_pdf(make_paper(pdf_url="https://example.org/x.pdf"), cfg).pdf_bytes is None


def test_disabling_fulltext_short_circuits_the_ladder(monkeypatch):
    cfg = OmegaConf.create({"fulltext": {"enabled": False, "unpaywall_email": None, "max_bytes": 1000}})
    monkeypatch.setattr(requests, "get", lambda url, **kw: pytest.fail("no request may be made"))
    assert resolve_pdf(make_paper(pdf_url="https://example.org/x.pdf"), cfg).pdf_bytes is None


def test_download_writes_pdfs_and_records_the_path(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())
    monkeypatch.setattr(
        "zotero_arxiv_daily.fulltext.resolver.extract_markdown_from_pdf",
        lambda path: "# extracted markdown",
    )
    papers = [make_paper(doi="10.1016/j.chroma.2026.01.001", pdf_url="https://example.org/a.pdf")]
    download_fulltext(papers, cfg, str(tmp_path))
    assert papers[0].pdf_path is not None
    assert papers[0].pdf_path.endswith(".pdf")
    assert papers[0].full_text == "# extracted markdown"
    assert papers[0].oa_status == "open"


def test_download_names_the_file_year_author_title(cfg, tmp_path, monkeypatch):
    import os
    from datetime import date

    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())
    monkeypatch.setattr("zotero_arxiv_daily.fulltext.resolver.extract_markdown_from_pdf", lambda path: "md")
    papers = [
        make_paper(
            doi="10.1016/j.chroma.2026.01.001",
            pdf_url="https://example.org/a.pdf",
            authors=["Zhang Wei", "Doe Jane"],
            title="Charge Variant Analysis of Monoclonal Antibodies",
            pub_date=date(2026, 3, 4),
        )
    ]
    download_fulltext(papers, cfg, str(tmp_path))
    name = os.path.basename(papers[0].pdf_path)
    assert "/" not in name
    assert name.startswith("2026-Zhang_Wei-Charge_Variant_Analysis_of_Monoclonal_Antibodies-")
    assert name.endswith(".pdf")


def test_filename_falls_back_to_unknown_year_when_pub_date_is_missing(cfg, tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())
    monkeypatch.setattr("zotero_arxiv_daily.fulltext.resolver.extract_markdown_from_pdf", lambda path: "md")
    papers = [
        make_paper(
            doi="10.1016/no-date",
            pdf_url="https://example.org/a.pdf",
            authors=["Roe Ann"],
            title="A Paper With No Publication Date",
        )
    ]
    download_fulltext(papers, cfg, str(tmp_path))
    name = os.path.basename(papers[0].pdf_path)
    assert name.startswith("unknown-Roe_Ann-A_Paper_With_No_Publication_Date-")


def test_filename_falls_back_to_unknown_author_when_authors_is_empty(cfg, tmp_path, monkeypatch):
    import os
    from datetime import date

    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())
    monkeypatch.setattr("zotero_arxiv_daily.fulltext.resolver.extract_markdown_from_pdf", lambda path: "md")
    papers = [
        make_paper(
            doi="10.1016/no-authors",
            pdf_url="https://example.org/a.pdf",
            authors=[],
            title="A Paper With No Listed Authors",
            pub_date=date(2026, 1, 1),
        )
    ]
    download_fulltext(papers, cfg, str(tmp_path))
    name = os.path.basename(papers[0].pdf_path)
    assert name.startswith("2026-unknown-A_Paper_With_No_Listed_Authors-")


def test_filename_truncates_a_long_title(cfg, tmp_path, monkeypatch):
    import os
    from datetime import date

    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())
    monkeypatch.setattr("zotero_arxiv_daily.fulltext.resolver.extract_markdown_from_pdf", lambda path: "md")
    long_title = "A" * 200
    papers = [
        make_paper(
            doi="10.1016/long-title",
            pdf_url="https://example.org/a.pdf",
            authors=["Roe Ann"],
            title=long_title,
            pub_date=date(2026, 1, 1),
        )
    ]
    download_fulltext(papers, cfg, str(tmp_path))
    name = os.path.basename(papers[0].pdf_path)
    # Well under the filesystem's ~255-byte limit even with a long title, a
    # long author list, and the disambiguating suffix all present at once.
    assert len(name) < 150
    assert "A" * 200 not in name


def test_filename_disambiguates_same_year_author_and_title_prefix(cfg, tmp_path, monkeypatch):
    """Two different papers, same year/author, and titles identical in their
    first 80 characters, must not silently overwrite each other on disk."""
    import os
    from datetime import date

    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())
    monkeypatch.setattr("zotero_arxiv_daily.fulltext.resolver.extract_markdown_from_pdf", lambda path: "md")
    shared_prefix = "A Comparative Study of Charge Heterogeneity in Therapeutic Monoclonal Antibodies "
    papers = [
        make_paper(
            doi="10.1016/part-one",
            pdf_url="https://example.org/a.pdf",
            authors=["Roe Ann"],
            title=shared_prefix + "Part One",
            pub_date=date(2026, 1, 1),
        ),
        make_paper(
            doi="10.1016/part-two",
            pdf_url="https://example.org/b.pdf",
            authors=["Roe Ann"],
            title=shared_prefix + "Part Two",
            pub_date=date(2026, 1, 1),
        ),
    ]
    download_fulltext(papers, cfg, str(tmp_path))
    name_one = os.path.basename(papers[0].pdf_path)
    name_two = os.path.basename(papers[1].pdf_path)
    assert name_one != name_two


def test_filename_is_stable_across_reruns_for_the_same_doi(cfg, tmp_path, monkeypatch):
    """A re-run must reproduce the same filename for the same paper, not a
    fresh random one — otherwise every rerun orphans the previous PDF."""
    import os
    from datetime import date

    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())
    monkeypatch.setattr("zotero_arxiv_daily.fulltext.resolver.extract_markdown_from_pdf", lambda path: "md")

    def make_batch():
        return [
            make_paper(
                doi="10.1016/rerun",
                pdf_url="https://example.org/a.pdf",
                authors=["Roe Ann"],
                title="A Reproducible Paper",
                pub_date=date(2026, 1, 1),
            )
        ]

    first = make_batch()
    download_fulltext(first, cfg, str(tmp_path))
    second = make_batch()
    download_fulltext(second, cfg, str(tmp_path))
    assert os.path.basename(first[0].pdf_path) == os.path.basename(second[0].pdf_path)


def test_filename_falls_back_to_index_when_everything_is_missing(cfg, tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())
    monkeypatch.setattr("zotero_arxiv_daily.fulltext.resolver.extract_markdown_from_pdf", lambda path: "md")
    papers = [make_paper(doi=None, pdf_url="https://example.org/a.pdf", authors=[], title="")]
    download_fulltext(papers, cfg, str(tmp_path))
    name = os.path.basename(papers[0].pdf_path)
    assert name == "paper-0.pdf"


def test_download_leaves_closed_papers_abstract_only(cfg, tmp_path, monkeypatch):
    def _patched(url, **kw):
        raise requests.HTTPError("403")

    monkeypatch.setattr(requests, "get", _patched)
    papers = [make_paper(doi="10.1016/closed", pdf_url="https://example.org/a.pdf")]
    download_fulltext(papers, cfg, str(tmp_path))
    assert papers[0].pdf_path is None
    assert papers[0].full_text is None
    assert papers[0].oa_status == "closed"


def test_a_failed_extraction_still_keeps_the_pdf(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kw: pdf_response())

    def _boom(path):
        raise RuntimeError("pymupdf exploded")

    monkeypatch.setattr("zotero_arxiv_daily.fulltext.resolver.extract_markdown_from_pdf", _boom)
    papers = [make_paper(doi="10.1016/x", pdf_url="https://example.org/a.pdf")]
    download_fulltext(papers, cfg, str(tmp_path))
    assert papers[0].pdf_path is not None
    assert papers[0].full_text is None


def test_europe_pmc_rung_looks_the_pmcid_up_by_doi(cfg, monkeypatch):
    """A DOI is not a preprint id; the PMCID has to be resolved first."""
    seen = []

    def _patched(url, **kw):
        seen.append((url, kw.get("params", {})))
        if "unpaywall" in url:
            return SimpleNamespace(
                status_code=200, raise_for_status=lambda: None, json=lambda: {"best_oa_location": None}
            )
        if "europepmc.org/webservices" in url or "ebi.ac.uk" in url:
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {"resultList": {"result": [{"pmcid": "PMC7654321", "isOpenAccess": "Y"}]}},
            )
        return pdf_response()

    monkeypatch.setattr(requests, "get", _patched)
    result = resolve_pdf(make_paper(doi="10.1016/j.chroma.2026.01.001"), cfg)
    assert result.pdf_bytes == PDF_BYTES
    assert result.source == "europepmc"
    assert any("PMC7654321" in url for url, _ in seen)


def test_the_europe_pmc_rung_is_skipped_when_no_pmcid_exists(cfg, monkeypatch):
    def _patched(url, **kw):
        if "unpaywall" in url:
            return SimpleNamespace(
                status_code=200, raise_for_status=lambda: None, json=lambda: {"best_oa_location": None}
            )
        if "ebi.ac.uk" in url:
            return SimpleNamespace(
                status_code=200, raise_for_status=lambda: None, json=lambda: {"resultList": {"result": []}}
            )
        raise AssertionError("no PDF fetch should be attempted without a PMCID")

    monkeypatch.setattr(requests, "get", _patched)
    result = resolve_pdf(make_paper(doi="10.1016/nope"), cfg)
    assert result.pdf_bytes is None
    assert result.oa_status == "closed"
