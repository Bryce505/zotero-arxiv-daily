"""End-to-end orchestration of the weekly digest, with every I/O stubbed."""

import json
import re
from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pytest
from omegaconf import OmegaConf, open_dict

from zotero_arxiv_daily.protocol import CorpusPaper, Paper
from zotero_arxiv_daily.search.profile import QueryProfile
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
                "min_relevance": 55,
                "min_score": 60,
                "triage_pool": 60,
                "triage_batch": 8,
                "journals": {"bonus": 10, "allow": ["Journal of Chromatography A"]},
                "industry": {"bonus": 8, "names": ["Amgen"]},
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
    # A number, or one number per candidate.  Tests dial this instead of
    # replacing the stub, which also feeds clustering and profile distillation.
    # cluster_verdict is the theme-fit string ("电荷", "HCP", "无", ...), or one
    # per candidate; None (the default) omits the "cluster" key entirely, which
    # leaves _apply_theme_verdicts() a no-op and keeps every test that doesn't
    # care about theme-fit behaving exactly as before that check existed.
    state = {"sent": [], "committed": [], "pushed": False, "relevance": 90, "cluster_verdict": None}

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
        request = str(kwargs.get("messages", []))
        # Triage asks for a JSON array and says how many papers are in the
        # batch; answer every index so the gate has something to work with.
        if '"relevance"' in request:
            count = int(re.search(r"共 (\d+) 篇", request).group(1))
            setting = state["relevance"]
            verdict = state["cluster_verdict"]
            rows = []
            for i in range(1, count + 1):
                row = {
                    "index": i,
                    "relevance": setting[i - 1] if isinstance(setting, list) else setting,
                    "reason": f"理由 {i}",
                    "modalities": ["ADC"],
                }
                if verdict is not None:
                    row["cluster"] = verdict[i - 1] if isinstance(verdict, list) else verdict
                rows.append(row)
            content = json.dumps(rows, ensure_ascii=False)
        else:
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

        def get_similarity_score(self, s1, s2):
            # Uniform: leaves cluster assignment exactly as the corpus-mean
            # signal alone would produce it, so existing full-pipeline tests
            # that assert a specific cluster keep asserting real behaviour.
            return np.full((len(s1), len(s2)), 0.5)

    monkeypatch.setattr("zotero_arxiv_daily.weekly.get_reranker_cls", lambda name: StubReranker)
    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.send_digest",
        lambda config, subject, html, attachments: state["sent"].append((subject, html, attachments)),
    )
    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.git_commit_paths",
        lambda paths, message, config, cwd=".": state["committed"].append(paths) or True,
    )
    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.git_push_artefacts",
        lambda config, cwd=".": state.__setitem__("pushed", True) or True,
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


def test_gate_passes_the_real_theme_names_and_descriptions_to_triage(weekly_config, stubbed, monkeypatch):
    """_gate() must hand triage the same theme list quota allocation uses —
    built from self._clusters, set in run() right after clustering — so the
    two stages never disagree about what the library's real themes are."""
    from zotero_arxiv_daily.triage import triage_papers as real_triage

    seen: list = []

    def spy(papers, client, llm_params, batch_size=8, themes=None, require_theme_fit=True):
        seen.append(themes)
        return real_triage(
            papers, client, llm_params, batch_size, themes=themes, require_theme_fit=require_theme_fit
        )

    monkeypatch.setattr("zotero_arxiv_daily.weekly.triage_papers", spy)
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    # The stubbed cluster payload: {"name":"电荷",...}, {"name":"HCP",...}.
    assert seen and seen[0] == {"电荷": "d", "HCP": "d"}


def test_a_candidate_the_model_says_fits_no_theme_is_excluded(weekly_config, stubbed):
    """A "无" verdict must drop a candidate even though its relevance score
    alone would clear both gates — the theme-fit check is a separate,
    stricter bar layered on top of general CMC relevance, not a re-run of it."""
    stubbed["cluster_verdict"] = "无"
    assert WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21)) is None
    assert stubbed["sent"] == []


def test_backfill_is_gated_without_requiring_a_theme_fit(weekly_config, stubbed, monkeypatch):
    """Fresh candidates must fit one of the library's real themes. A
    highly-cited classic must not be held to that same bar: run 33517443909
    rejected 13 of 13 backfill candidates on theme fit alone, none on
    relevance and none on score, and shipped a 5-paper digest with an empty
    "经典补位" section."""
    from zotero_arxiv_daily.triage import triage_papers as real_triage

    strictness: list = []

    def spy(papers, client, llm_params, batch_size=8, themes=None, require_theme_fit=True):
        strictness.append(require_theme_fit)
        return real_triage(
            papers, client, llm_params, batch_size, themes=themes, require_theme_fit=require_theme_fit
        )

    class RetrieverWithClassics:
        name = "pubmed"

        def __init__(self, config):
            self.config = config

        def search(self, query, start, end, limit):
            return [make_candidate(i) for i in range(3)]

        def search_highly_cited(self, query, limit):
            classic = make_candidate(90)
            classic.doi, classic.cited_by_count = "10.1000/classic", 900
            return [classic]

    monkeypatch.setattr("zotero_arxiv_daily.weekly.triage_papers", spy)
    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.get_query_retriever_cls", lambda name: RetrieverWithClassics
    )
    # "无" everywhere: every candidate is CMC-relevant but fits no theme.
    stubbed["cluster_verdict"] = "无"
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))

    assert strictness[0] is True, "fresh candidates keep the strict theme-fit bar"
    assert strictness[-1] is False, "backfill is gated without requiring a theme fit"
    # The strict pass drops every fresh candidate; the loose one keeps the classic.
    assert digest is not None
    assert [p.doi for p in digest.backfill] == ["10.1000/classic"]


def test_a_candidate_the_model_reassigns_is_filed_under_its_corrected_theme(weekly_config, stubbed):
    """A verdict naming the *other* real theme overrides whichever cluster
    the embedding-only pass provisionally picked."""
    stubbed["cluster_verdict"] = "HCP"
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    delivered = [paper for _, papers in digest.clusters for paper in papers]
    assert delivered
    assert all(paper.cluster == "HCP" for paper in delivered)


# --------------------------------------------------------------------------- description-weighted assignment

def _minimal_executor(config, reranker):
    """A WeeklyExecutor with only what `_score_and_assign` touches set up."""
    executor = WeeklyExecutor.__new__(WeeklyExecutor)
    executor.config = config
    executor.reranker = reranker
    return executor


def _minimal_weekly_config(**search_overrides):
    config = OmegaConf.create(
        {
            "reranker": {"vector_cache": None},
            "executor": {"reranker": "api"},
            "search": {"n_clusters": 2, **search_overrides},
        }
    )
    return config


# These focus on plumbing — does `_score_and_assign` call the reranker with
# the right texts, and pass the right desc_sim/weight to assign_clusters —
# not on re-deriving assign_clusters' own arithmetic, which is already
# covered directly in tests/search/test_cluster.py.  Re-deriving it here
# means reasoning through _score_and_assign's corpus-by-recency reordering
# (`order`/`sim_original`), which is exactly the kind of index arithmetic
# that is easy to get wrong in the test itself; spying on the call sidesteps
# it entirely.


def _spy_on_assign_clusters(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.assign_clusters",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    return calls


def test_score_and_assign_asks_the_reranker_for_description_similarity(monkeypatch):
    calls = []

    class Reranker:
        def similarity_matrix(self, candidates, corpus):
            return np.zeros((len(candidates), len(corpus)))

        def get_similarity_score(self, s1, s2):
            calls.append((s1, s2))
            return np.array([[0.1, 0.9]])

    from zotero_arxiv_daily.search.cluster import ThemeCluster

    candidates = [Paper(source="s", title="c", authors=[], abstract="candidate abstract", url="u")]
    corpus = [CorpusPaper(title="t", abstract="a", added_date=datetime(2026, 1, 1), paths=[])]
    clusters = [
        ThemeCluster(name="alpha", description="d-alpha", members=[0]),
        ThemeCluster(name="beta", description="d-beta", members=[]),
    ]
    executor = _minimal_executor(_minimal_weekly_config(), Reranker())
    executor._score_and_assign(candidates, corpus, clusters)

    assert len(calls) == 1
    s1, s2 = calls[0]
    assert s1 == ["candidate abstract"]
    assert s2 == ["d-alpha", "d-beta"]


def test_score_and_assign_passes_the_description_similarity_through(monkeypatch):
    class Reranker:
        def similarity_matrix(self, candidates, corpus):
            return np.zeros((len(candidates), len(corpus)))

        def get_similarity_score(self, s1, s2):
            return np.array([[0.05, 0.95]])

    from zotero_arxiv_daily.search.cluster import ThemeCluster

    calls = _spy_on_assign_clusters(monkeypatch)
    candidates = [Paper(source="s", title="c", authors=[], abstract="a", url="u")]
    corpus = [CorpusPaper(title="t0", abstract="a", added_date=datetime(2026, 1, 1), paths=[])]
    clusters = [ThemeCluster(name="alpha", description="d", members=[0])]
    executor = _minimal_executor(_minimal_weekly_config(), Reranker())
    executor._score_and_assign(candidates, corpus, clusters)

    assert len(calls) == 1
    _, kwargs = calls[0]
    np.testing.assert_array_equal(kwargs["desc_sim"], np.array([[0.05, 0.95]]))


def test_score_and_assign_degrades_to_desc_sim_none_when_description_similarity_fails(monkeypatch):
    """A failing enhancement must not cost the digest: assign_clusters still
    gets called, just with desc_sim=None (its documented corpus-only mode)."""

    class Reranker:
        def similarity_matrix(self, candidates, corpus):
            return np.zeros((len(candidates), len(corpus)))

        def get_similarity_score(self, s1, s2):
            raise RuntimeError("embedding API down")

    from zotero_arxiv_daily.search.cluster import ThemeCluster

    calls = _spy_on_assign_clusters(monkeypatch)
    candidates = [Paper(source="s", title="c", authors=[], abstract="a", url="u")]
    corpus = [CorpusPaper(title="t0", abstract="a", added_date=datetime(2026, 1, 1), paths=[])]
    clusters = [ThemeCluster(name="alpha", description="d", members=[0])]
    executor = _minimal_executor(_minimal_weekly_config(), Reranker())
    executor._score_and_assign(candidates, corpus, clusters)

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["desc_sim"] is None


def test_score_and_assign_reads_the_description_weight_from_config(monkeypatch):
    class Reranker:
        def similarity_matrix(self, candidates, corpus):
            return np.zeros((len(candidates), len(corpus)))

        def get_similarity_score(self, s1, s2):
            return np.array([[0.5]])

    from zotero_arxiv_daily.search.cluster import ThemeCluster

    calls = _spy_on_assign_clusters(monkeypatch)
    candidates = [Paper(source="s", title="c", authors=[], abstract="a", url="u")]
    corpus = [CorpusPaper(title="t0", abstract="a", added_date=datetime(2026, 1, 1), paths=[])]
    clusters = [ThemeCluster(name="alpha", description="d", members=[0])]
    config = _minimal_weekly_config(cluster_assignment_description_weight=0.25)
    executor = _minimal_executor(config, Reranker())
    executor._score_and_assign(candidates, corpus, clusters)

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["description_weight"] == 0.25


def test_score_and_assign_defaults_the_description_weight_when_unconfigured(monkeypatch):
    class Reranker:
        def similarity_matrix(self, candidates, corpus):
            return np.zeros((len(candidates), len(corpus)))

        def get_similarity_score(self, s1, s2):
            return np.array([[0.5]])

    from zotero_arxiv_daily.search.cluster import ThemeCluster

    calls = _spy_on_assign_clusters(monkeypatch)
    candidates = [Paper(source="s", title="c", authors=[], abstract="a", url="u")]
    corpus = [CorpusPaper(title="t0", abstract="a", added_date=datetime(2026, 1, 1), paths=[])]
    clusters = [ThemeCluster(name="alpha", description="d", members=[0])]
    executor = _minimal_executor(_minimal_weekly_config(), Reranker())  # no override
    executor._score_and_assign(candidates, corpus, clusters)

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["description_weight"] == 0.6


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


def test_corpus_vectors_are_cached_between_runs(weekly_config, stubbed, monkeypatch, tmp_path):
    """The corpus barely changes; re-embedding it every week is dead time."""
    embedded: list[list[str]] = []

    class EmbeddingReranker:
        def __init__(self, config):
            pass

        def embed(self, texts):
            embedded.append(list(texts))
            return np.array([[float(len(t)), 1.0, 2.0] for t in texts])

        def similarity_matrix(self, candidates, corpus):  # pragma: no cover
            raise AssertionError("the cached path should be used")

    monkeypatch.setattr("zotero_arxiv_daily.weekly.get_reranker_cls", lambda name: EmbeddingReranker)
    weekly_config.reranker.vector_cache = "state/corpus_vectors.npz"

    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert (tmp_path / "state" / "corpus_vectors.npz").exists()
    first_corpus = [t for batch in embedded for t in batch if t.startswith("Corpus abstract")]
    assert len(first_corpus) == 6

    embedded.clear()
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 28))
    assert not [t for batch in embedded for t in batch if t.startswith("Corpus abstract")]


def test_the_vector_cache_is_archived_with_the_other_artefacts(weekly_config, stubbed, monkeypatch):
    class EmbeddingReranker:
        def __init__(self, config):
            pass

        def embed(self, texts):
            return np.array([[float(len(t)), 1.0, 2.0] for t in texts])

    monkeypatch.setattr("zotero_arxiv_daily.weekly.get_reranker_cls", lambda name: EmbeddingReranker)
    weekly_config.reranker.vector_cache = "state/corpus_vectors.npz"
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert "state/corpus_vectors.npz" in stubbed["committed"][0]


def test_caching_is_off_by_default_and_uses_the_plain_path(weekly_config, stubbed):
    """Without vector_cache set, behaviour is exactly as before."""
    assert weekly_config.reranker.get("vector_cache") is None
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert digest is not None


def test_a_reranker_without_embed_falls_back_instead_of_failing(weekly_config, stubbed, monkeypatch):
    """Caching is an optimisation; it must never break a working reranker."""

    class NoEmbedReranker:
        def __init__(self, config):
            pass

        def similarity_matrix(self, candidates, corpus):
            values = np.linspace(0.1, 0.9, len(candidates) * len(corpus))
            return values.reshape(len(candidates), len(corpus))

    monkeypatch.setattr("zotero_arxiv_daily.weekly.get_reranker_cls", lambda name: NoEmbedReranker)
    weekly_config.reranker.vector_cache = "state/corpus_vectors.npz"
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert digest is not None


def test_an_unusable_vector_cache_falls_back_instead_of_failing(weekly_config, stubbed, monkeypatch, tmp_path):
    """A cache whose vectors no longer match must not cost the week its digest."""

    class BrokenCacheReranker:
        def __init__(self, config):
            self.calls = 0

        def embed(self, texts):
            # Returns a different width each call, so stacking the cache fails.
            self.calls += 1
            width = 3 if self.calls == 1 else 5
            return np.array([[float(len(t))] * width for t in texts])

        def similarity_matrix(self, candidates, corpus):
            values = np.linspace(0.1, 0.9, len(candidates) * len(corpus))
            return values.reshape(len(candidates), len(corpus))

    monkeypatch.setattr("zotero_arxiv_daily.weekly.get_reranker_cls", lambda name: BrokenCacheReranker)
    weekly_config.reranker.vector_cache = "state/corpus_vectors.npz"
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert digest is not None


def test_each_source_receives_the_query_form_it_can_answer(weekly_config, monkeypatch):
    """Europe PMC and OpenAlex AND the terms of a query together.

    Handing them the long natural-language query that Crossref's relevance
    ranking absorbs asks for a record containing every word, and they answer
    with nothing — measured on the first live run as 0 hits from both across
    all five clusters while Crossref returned 65.
    """
    recorded = []

    def retriever_for(name):
        class Stub:
            def __init__(self, config):
                pass

            def search(self, query, start, end, limit):
                recorded.append((name, query))
                return []

        return Stub

    monkeypatch.setattr("zotero_arxiv_daily.weekly.get_query_retriever_cls", retriever_for)
    with open_dict(weekly_config):
        weekly_config.search.sources = ["pubmed", "europepmc", "openalex", "crossref"]

    executor = WeeklyExecutor.__new__(WeeklyExecutor)
    executor.config = weekly_config
    profile = QueryProfile(
        cluster="色谱电泳纯度与含量分析",
        mesh_terms=[],
        free_terms=["size exclusion chromatography", "CE-SDS"],
        pubmed_query="BOOLEAN[tiab]",
        plain_query="SEC CE-SDS HIC HPLC purity content analysis protein size variants",
    )

    executor._search_all([profile], date(2026, 8, 14), date(2026, 8, 21))

    assert dict(recorded) == {
        "pubmed": "BOOLEAN[tiab]",
        "europepmc": '"size exclusion chromatography" OR "CE-SDS"',
        "openalex": '"size exclusion chromatography" OR "CE-SDS"',
        "crossref": "SEC CE-SDS HIC HPLC purity content analysis protein size variants",
    }


def test_the_digest_is_pushed_after_it_is_committed(weekly_config, stubbed):
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert stubbed["pushed"], "committing without pushing loses the archive with the runner"


def test_a_failed_push_is_raised_rather_than_swallowed(weekly_config, stubbed, monkeypatch):
    """The workflow used to run `git push || echo`, so a rejected push was
    invisible: the step, the job and the whole run reported success while the
    ephemeral runner carried off the only copy of the report and the seen-DOI
    state. A failure here must reach the operator."""
    monkeypatch.setattr(
        "zotero_arxiv_daily.weekly.git_push_artefacts", lambda config, cwd=".": False
    )
    with pytest.raises(RuntimeError, match="push"):
        WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))


def test_every_delivered_paper_cleared_the_gate(weekly_config, stubbed):
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    for _, papers in digest.clusters:
        for paper in papers:
            assert paper.scoring is not None
            assert paper.scoring.relevance >= weekly_config.report.min_relevance


def test_an_irrelevant_candidate_never_reaches_the_digest(weekly_config, stubbed):
    # 10 is the rubric's "no connection to biologics" band — where the
    # sodium-ion battery paper belongs.
    stubbed["relevance"] = 10
    assert WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21)) is None
    assert stubbed["sent"] == []


def test_the_quota_is_allocated_only_among_survivors(weekly_config, stubbed):
    # Three candidates, one qualifying. The quota is six slots across two
    # clusters; the old code filled the rest from the tail of the list.
    stubbed["relevance"] = [90, 10, 10]
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert sum(len(papers) for _, papers in digest.clusters) == 1


def test_an_abbreviated_journal_name_does_not_match_the_full_title(weekly_config, stubbed):
    # make_candidate() sets journal="J Chromatogr A"; the fixture lists
    # "Journal of Chromatography A", which must NOT match an abbreviation.
    digest = WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    paper = digest.clusters[0][1][0]
    assert paper.scoring.journal_hit is None
    assert paper.scoring.rank_score == 90


def test_only_the_configured_pool_size_is_triaged(weekly_config, stubbed, monkeypatch):
    from zotero_arxiv_daily.triage import triage_papers as real_triage

    with open_dict(weekly_config):
        weekly_config.report.triage_pool = 2

    seen: list[int] = []

    def counting(papers, client, llm_params, batch_size=8, **kwargs):
        seen.append(len(papers))
        return real_triage(papers, client, llm_params, batch_size, **kwargs)

    monkeypatch.setattr("zotero_arxiv_daily.weekly.triage_papers", counting)
    WeeklyExecutor(weekly_config).run(anchor=date(2026, 8, 21))
    assert seen[0] == 2
