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
            {"enabled": False, "user_name": "b", "user_email": "b@e.org", "branch": ""}
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
