"""End-to-end orchestration of the weekly digest, with every I/O stubbed."""

from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pytest
from omegaconf import OmegaConf, open_dict

from zotero_arxiv_daily.protocol import CorpusPaper, Paper
from zotero_arxiv_daily.weekly import WeeklyExecutor


def make_corpus(n=6) -> list[CorpusPaper]:
    return [
        CorpusPaper(
            title=f"Corpus {i}",
            abstract=f"Corpus abstract {i}",
            added_date=datetime(2026, 1, i + 1),
            paths=["文献/表征"],
        )
        for i in range(n)
    ]


def make_candidate(i: int) -> Paper:
    return Paper(
        source="pubmed",
        title=f"Candidate {i}",
        authors=["Smith J"],
        abstract=f"Candidate abstract {i}",
        url=f"https://example.org/{i}",
        doi=f"10.1000/{i}",
        journal="J Chromatogr A",
        pub_date=date(2026, 8, 18),
    )


@pytest.fixture()
def weekly_config(config, tmp_path):
    with open_dict(config):
        config.zotero.include_path = ["文献", "文献/**"]
        config.search = OmegaConf.create(
            {
                "sources": ["pubmed"],
                "n_clusters": 2,
                "per_cluster_limit": 25,
                "cluster_cache": str(tmp_path / "clusters.json"),
                "profile_cache": str(tmp_path / "profiles.json"),
                "seen_state": str(tmp_path / "seen.json"),
            }
        )
        config.fulltext = OmegaConf.create({"enabled": False, "unpaywall_email": None, "max_bytes": 1000})
        config.report = OmegaConf.create(
            {
                "min_papers": 4,
                "max_papers": 6,
                "top_picks": 2,
                "min_per_cluster": 1,
                "attach_pdfs": 0,
                "output_dir": str(tmp_path),
                "fields": [{"key": "background", "label": "背景", "instruction": "研究背景"}],
            }
        )
        config.git = OmegaConf.create(
            {"enabled": False, "user_name": "b", "user_email": "b@e.org", "include_pdfs": True}
        )
        config.email.recipients = ["team@example.org"]
    return config


@pytest.fixture()
def stubbed(monkeypatch, weekly_config):
    """Stub every network boundary the weekly run touches."""
    state = {"sent": [], "committed": []}

    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.WeeklyExecutor.fetch_zotero_corpus",
        lambda self: make_corpus(),
    )

    payloads = iter(
        [
            '{"clusters":[{"name":"电荷","description":"d","members":[0,1,2]},'
            '{"name":"HCP","description":"d","members":[3,4,5]}]}',
            '{"mesh_terms":[],"free_terms":[],"pubmed_query":"q1","plain_query":"p1"}',
            '{"mesh_terms":[],"free_terms":[],"pubmed_query":"q2","plain_query":"p2"}',
        ]
    )

    def create(**kwargs):
        try:
            content = next(payloads)
        except StopIteration:
            content = '{"background":"抽取出的背景"}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.OpenAI",
        lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )

    class StubQueryRetriever:
        name = "pubmed"

        def __init__(self, config):
            self.config = config

        def search(self, query, start, end, limit):
            return [make_candidate(i) for i in range(3)]

        def search_highly_cited(self, query, limit):
            return []

    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.get_query_retriever_cls",
        lambda name: StubQueryRetriever,
    )

    class StubReranker:
        def __init__(self, config):
            pass

        def similarity_matrix(self, candidates, corpus):
            values = np.linspace(0.1, 0.9, len(candidates) * len(corpus))
            return values.reshape(len(candidates), len(corpus))

    monkeypatch.setattr("zotero_arxiv_daily.weekly.get_reranker_cls", lambda name: StubReranker)
    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.send_digest",
        lambda config, subject, html, attachments: state["sent"].append((subject, html, attachments)),
    )
    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.git_commit_paths",
        lambda paths, message, config, cwd=".": state["committed"].append(paths) or True,
    )
    return state


def test_weekly_run_produces_a_digest(weekly_config, stubbed):
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert digest is not None
    assert digest.label == "2026-08-W3"


def test_weekly_run_sends_one_email_with_the_label_in_the_subject(weekly_config, stubbed):
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert len(stubbed["sent"]) == 1
    subject, html, _ = stubbed["sent"][0]
    assert "2026-08-W3" in subject
    assert "CMC" in subject
    assert "2026-08-W3" in html


def test_weekly_run_writes_both_report_files(weekly_config, stubbed, tmp_path):
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert (tmp_path / "reports" / "2026" / "2026-08-W3.md").exists()
    assert (tmp_path / "reports" / "2026" / "2026-08-W3.html").exists()


def test_weekly_run_commits_the_artefacts(weekly_config, stubbed):
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert stubbed["committed"]
    assert any("2026-08-W3.md" in p for p in stubbed["committed"][0])


def test_weekly_run_records_delivered_dois_for_next_week(weekly_config, stubbed, tmp_path):
    import json

    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    with open(tmp_path / "seen.json", encoding="utf-8") as handle:
        assert "10.1000/0" in json.load(handle)


def test_papers_delivered_last_week_are_not_delivered_again(weekly_config, stubbed):
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    second = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 28))
    assert second is None or all(
        p.doi != "10.1000/0" for _, papers in second.clusters for p in papers
    )


def test_candidates_are_deduplicated_across_query_profiles(weekly_config, stubbed):
    # The stub returns the same three papers for each of the two profiles.
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    delivered = [p.doi for _, papers in digest.clusters for p in papers]
    assert sorted(delivered) == ["10.1000/0", "10.1000/1", "10.1000/2"]


def test_every_candidate_is_assigned_a_cluster(weekly_config, stubbed):
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    for _, papers in digest.clusters:
        for paper in papers:
            assert paper.cluster in {"电荷", "HCP"}


def test_every_delivered_paper_carries_its_extracted_fields(weekly_config, stubbed):
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    for _, papers in digest.clusters:
        for paper in papers:
            assert paper.extraction is not None
            assert paper.extraction["background"] == "抽取出的背景"


def test_every_candidate_is_scored(weekly_config, stubbed):
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    for _, papers in digest.clusters:
        for paper in papers:
            assert paper.score is not None


def test_an_empty_corpus_aborts_before_any_search(weekly_config, stubbed, monkeypatch):
    monkeypatch.setattr("zotero_arxiv_daily.weekly.WeeklyExecutor.fetch_zotero_corpus", lambda self: [])
    assert WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21)) is None
    assert stubbed["sent"] == []


def test_papers_already_in_the_zotero_library_are_not_recommended(weekly_config, stubbed, monkeypatch):
    """Spec 8.5: recommending something the user already collected is noise."""
    corpus = make_corpus()
    corpus[0].doi = "10.1000/1"
    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.WeeklyExecutor.fetch_zotero_corpus", lambda self: corpus
    )
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    delivered = [p.doi for _, papers in digest.clusters for p in papers]
    assert "10.1000/1" not in delivered
    assert sorted(delivered) == ["10.1000/0", "10.1000/2"]


def test_attachment_candidates_reach_beyond_the_top_picks():
    """attach_pdfs: 5 must not be silently capped at top_picks: 3."""
    from zotero_arxiv_daily.report import build_digest
    from zotero_arxiv_daily.weekly import attachment_candidates

    papers = []
    for i in range(6):
        paper = make_candidate(i)
        paper.score = float(10 - i)
        paper.cluster = "c"
        paper.pdf_path = f"/tmp/{i}.pdf"
        papers.append(paper)
    digest = build_digest(papers, [], date(2026, 8, 21), top_n=3)
    assert attachment_candidates(digest, 5) == [f"/tmp/{i}.pdf" for i in range(5)]


def test_attachment_candidates_lead_with_the_top_picks():
    from zotero_arxiv_daily.report import build_digest
    from zotero_arxiv_daily.weekly import attachment_candidates

    papers = []
    for i in range(4):
        paper = make_candidate(i)
        paper.score = float(i)  # ascending, so candidate 3 ranks first
        paper.cluster = "c"
        paper.pdf_path = f"/tmp/{i}.pdf"
        papers.append(paper)
    digest = build_digest(papers, [], date(2026, 8, 21), top_n=2)
    assert attachment_candidates(digest, 2) == ["/tmp/3.pdf", "/tmp/2.pdf"]


def test_attachment_candidates_skip_papers_without_a_pdf():
    from zotero_arxiv_daily.report import build_digest
    from zotero_arxiv_daily.weekly import attachment_candidates

    with_pdf = make_candidate(0)
    with_pdf.score, with_pdf.cluster, with_pdf.pdf_path = 9.0, "c", "/tmp/a.pdf"
    without = make_candidate(1)
    without.score, without.cluster = 8.0, "c"
    digest = build_digest([with_pdf, without], [], date(2026, 8, 21), top_n=2)
    assert attachment_candidates(digest, 5) == ["/tmp/a.pdf"]


def test_downloaded_pdfs_are_staged_for_the_archive(weekly_config, stubbed, monkeypatch, tmp_path):
    """The report promises the PDFs stay archived in the repository."""

    def fake_download(papers, config, out_dir):
        import os

        os.makedirs(out_dir, exist_ok=True)
        for i, paper in enumerate(papers):
            path = os.path.join(out_dir, f"{i}.pdf")
            with open(path, "wb") as handle:
                handle.write(b"%PDF-1.7")
            paper.pdf_path = path
            paper.oa_status = "open"

    monkeypatch.setattr("zotero_arxiv_daily.weekly.download_fulltext", fake_download)
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    staged = stubbed["committed"][0]
    assert any("library/2026/2026-08-W3" in p for p in staged)


def test_pdfs_are_not_staged_when_the_operator_opts_out(weekly_config, stubbed, monkeypatch):
    def fake_download(papers, config, out_dir):
        import os

        os.makedirs(out_dir, exist_ok=True)
        for i, paper in enumerate(papers):
            path = os.path.join(out_dir, f"{i}.pdf")
            with open(path, "wb") as handle:
                handle.write(b"%PDF-1.7")
            paper.pdf_path = path

    monkeypatch.setattr("zotero_arxiv_daily.weekly.download_fulltext", fake_download)
    weekly_config.git.include_pdfs = False
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert not any("library/" in p for p in stubbed["committed"][0])


def test_artefacts_are_committed_relative_to_the_output_root(weekly_config, stubbed):
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    staged = stubbed["committed"][0]
    for path in staged:
        assert not path.startswith("/"), f"{path} is absolute; git would not resolve it"


def test_the_derived_caches_are_archived_so_themes_stay_stable(weekly_config, stubbed):
    """Actions runners are ephemeral: an uncommitted cache is no cache.

    Without it the LLM re-clusters every week and the section headings —
    and the quota allocation behind them — drift.
    """
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    staged = stubbed["committed"][0]
    assert any("clusters.json" in p for p in staged)
    assert any("profiles.json" in p for p in staged)


def test_seen_state_is_read_from_where_it_was_written(weekly_config, stubbed, tmp_path):
    """A relative seen_state under a non-default output_dir must round-trip."""
    weekly_config.search.seen_state = "state/seen_dois.json"
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert (tmp_path / "state" / "seen_dois.json").exists()

    second = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 28))
    assert second is None or all(
        p.doi != "10.1000/0" for _, papers in second.clusters for p in papers
    )


def test_backfilled_papers_can_be_attached_too():
    """A thin week is mostly backfill; attaching nothing would be wrong."""
    from zotero_arxiv_daily.report import build_digest
    from zotero_arxiv_daily.weekly import attachment_candidates

    fresh = make_candidate(0)
    fresh.score, fresh.cluster, fresh.pdf_path = 9.0, "c", "/tmp/fresh.pdf"
    classic = make_candidate(1)
    classic.score, classic.cluster, classic.pdf_path = 5.0, "c", "/tmp/classic.pdf"
    classic.is_backfill, classic.cited_by_count = True, 900
    digest = build_digest([fresh], [classic], date(2026, 8, 21), top_n=1)
    assert attachment_candidates(digest, 5) == ["/tmp/fresh.pdf", "/tmp/classic.pdf"]


def test_the_caches_live_under_the_output_root(weekly_config, stubbed, tmp_path):
    """An unrooted cache path makes git add fail and loses the whole archive."""
    weekly_config.search.cluster_cache = "state/theme_clusters.json"
    weekly_config.search.profile_cache = "state/query_profiles.json"
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert (tmp_path / "state" / "theme_clusters.json").exists()
    assert (tmp_path / "state" / "query_profiles.json").exists()
    staged = stubbed["committed"][0]
    assert "state/theme_clusters.json" in staged
    assert "state/query_profiles.json" in staged


def test_a_failed_send_does_not_mark_the_papers_as_delivered(weekly_config, stubbed, monkeypatch, tmp_path):
    """Recording them before sending would bury the week's papers forever."""

    def boom(config, subject, html, attachments):
        raise RuntimeError("SMTP refused the connection")

    monkeypatch.setattr("zotero_arxiv_daily.weekly.send_digest", boom)
    with pytest.raises(RuntimeError):
        WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))

    import json
    import os

    seen_path = tmp_path / "seen.json"
    recorded = json.load(open(seen_path, encoding="utf-8")) if os.path.exists(seen_path) else []
    assert "10.1000/0" not in recorded
