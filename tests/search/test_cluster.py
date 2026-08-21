"""LLM corpus clustering, fingerprint caching, and candidate assignment."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np

from zotero_arxiv_daily.protocol import CorpusPaper, Paper
from zotero_arxiv_daily.search.cluster import (
    ThemeCluster,
    assign_clusters,
    cluster_corpus,
    corpus_fingerprint,
    load_or_build_clusters,
)


def make_corpus(n: int = 4) -> list[CorpusPaper]:
    return [
        CorpusPaper(
            title=f"Paper {i}",
            abstract=f"Abstract {i}",
            added_date=datetime(2026, 1, 1) + timedelta(days=i),
            paths=["文献/表征"],
        )
        for i in range(n)
    ]


def stub_client(payload: str) -> SimpleNamespace:
    """A minimal stand-in for the OpenAI client returning a fixed body."""
    message = SimpleNamespace(content=payload)
    choice = SimpleNamespace(message=message)
    response = SimpleNamespace(choices=[choice])
    completions = SimpleNamespace(create=lambda **kw: response)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


LLM_PARAMS = {"generation_kwargs": {"model": "stub-model"}, "language": "中文"}

VALID_PAYLOAD = json.dumps(
    {
        "clusters": [
            {"name": "电荷异质性", "description": "电荷变异体分析", "members": [0, 1]},
            {"name": "宿主细胞蛋白", "description": "HCP 残留检测", "members": [2, 3]},
        ]
    },
    ensure_ascii=False,
)


def test_cluster_corpus_parses_the_llm_payload():
    clusters = cluster_corpus(make_corpus(), stub_client(VALID_PAYLOAD), LLM_PARAMS)
    assert [c.name for c in clusters] == ["电荷异质性", "宿主细胞蛋白"]
    assert clusters[0].members == [0, 1]


def test_cluster_corpus_tolerates_a_fenced_payload():
    fenced = f"```json\n{VALID_PAYLOAD}\n```"
    clusters = cluster_corpus(make_corpus(), stub_client(fenced), LLM_PARAMS)
    assert len(clusters) == 2


def test_cluster_corpus_drops_out_of_range_members():
    payload = json.dumps({"clusters": [{"name": "x", "description": "d", "members": [0, 99]}]})
    clusters = cluster_corpus(make_corpus(), stub_client(payload), LLM_PARAMS)
    assert 99 not in clusters[0].members


def test_cluster_corpus_falls_back_to_one_cluster_on_bad_json():
    clusters = cluster_corpus(make_corpus(), stub_client("not json at all"), LLM_PARAMS)
    assert len(clusters) == 1
    assert clusters[0].members == [0, 1, 2, 3]


def test_every_corpus_paper_lands_in_some_cluster():
    payload = json.dumps({"clusters": [{"name": "x", "description": "d", "members": [0, 1]}]})
    clusters = cluster_corpus(make_corpus(), stub_client(payload), LLM_PARAMS)
    covered = {i for c in clusters for i in c.members}
    assert covered == {0, 1, 2, 3}


def test_fingerprint_is_stable_across_reordering():
    corpus = make_corpus()
    assert corpus_fingerprint(corpus) == corpus_fingerprint(list(reversed(corpus)))


def test_fingerprint_changes_when_the_corpus_changes():
    assert corpus_fingerprint(make_corpus(4)) != corpus_fingerprint(make_corpus(5))


def test_cached_clusters_are_reused_without_calling_the_llm(tmp_path):
    path = str(tmp_path / "clusters.json")
    corpus = make_corpus()
    first = load_or_build_clusters(path, corpus, stub_client(VALID_PAYLOAD), LLM_PARAMS)

    def explode(**kw):
        raise AssertionError("the LLM must not be called when the cache is warm")

    cold = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=explode)))
    second = load_or_build_clusters(path, corpus, cold, LLM_PARAMS)
    assert [c.name for c in second] == [c.name for c in first]


def test_cache_is_rebuilt_when_the_corpus_fingerprint_moves(tmp_path):
    path = str(tmp_path / "clusters.json")
    load_or_build_clusters(path, make_corpus(4), stub_client(VALID_PAYLOAD), LLM_PARAMS)
    other = json.dumps({"clusters": [{"name": "新主题", "description": "d", "members": [0]}]}, ensure_ascii=False)
    rebuilt = load_or_build_clusters(path, make_corpus(5), stub_client(other), LLM_PARAMS)
    assert rebuilt[0].name == "新主题"


def test_an_unreadable_cache_is_rebuilt_rather_than_fatal(tmp_path):
    path = str(tmp_path / "clusters.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")
    clusters = load_or_build_clusters(path, make_corpus(), stub_client(VALID_PAYLOAD), LLM_PARAMS)
    assert [c.name for c in clusters] == ["电荷异质性", "宿主细胞蛋白"]


def test_candidates_are_assigned_to_their_closest_cluster():
    candidates = [
        Paper(source="s", title="c0", authors=[], abstract="a", url="u0"),
        Paper(source="s", title="c1", authors=[], abstract="a", url="u1"),
    ]
    clusters = [
        ThemeCluster(name="alpha", description="", members=[0, 1]),
        ThemeCluster(name="beta", description="", members=[2, 3]),
    ]
    # c0 is closest to the alpha columns, c1 to the beta columns.
    sim = np.array([[0.9, 0.8, 0.1, 0.2], [0.1, 0.2, 0.9, 0.7]])
    assign_clusters(candidates, sim, clusters)
    assert candidates[0].cluster == "alpha"
    assert candidates[1].cluster == "beta"


def test_assignment_uses_the_mean_not_the_max_similarity():
    candidates = [Paper(source="s", title="c", authors=[], abstract="a", url="u")]
    clusters = [
        ThemeCluster(name="one_hit_wonder", description="", members=[0, 1]),
        ThemeCluster(name="consistently_close", description="", members=[2, 3]),
    ]
    # A single 0.95 spike loses to a pair that is uniformly 0.7.
    sim = np.array([[0.95, 0.05, 0.7, 0.7]])
    assign_clusters(candidates, sim, clusters)
    assert candidates[0].cluster == "consistently_close"


def test_assignment_skips_empty_clusters():
    candidates = [Paper(source="s", title="c", authors=[], abstract="a", url="u")]
    clusters = [
        ThemeCluster(name="empty", description="", members=[]),
        ThemeCluster(name="real", description="", members=[0]),
    ]
    assign_clusters(candidates, np.array([[0.5]]), clusters)
    assert candidates[0].cluster == "real"


def test_cached_membership_follows_the_papers_not_their_positions(tmp_path):
    """Zotero returns items newest-modified first, so positions move.

    The fingerprint is deliberately order-insensitive, so a reordered corpus
    still hits the cache. If membership were stored positionally, every
    candidate would then be routed to the wrong theme with no error.
    """
    path = str(tmp_path / "clusters.json")
    corpus = make_corpus(4)
    payload = json.dumps(
        {
            "clusters": [
                {"name": "front", "description": "d", "members": [0, 1]},
                {"name": "back", "description": "d", "members": [2, 3]},
            ]
        }
    )
    load_or_build_clusters(path, corpus, stub_client(payload), LLM_PARAMS)

    def explode(**kw):
        raise AssertionError("the reordered corpus must still hit the cache")

    cold = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=explode)))
    reordered = list(reversed(corpus))  # Paper 3, Paper 2, Paper 1, Paper 0
    clusters = load_or_build_clusters(path, reordered, cold, LLM_PARAMS)

    by_name = {c.name: c for c in clusters}
    front_titles = {reordered[i].title for i in by_name["front"].members}
    back_titles = {reordered[i].title for i in by_name["back"].members}
    assert front_titles == {"Paper 0", "Paper 1"}
    assert back_titles == {"Paper 2", "Paper 3"}


def test_a_paper_added_since_the_cache_was_built_is_still_clustered(tmp_path):
    path = str(tmp_path / "clusters.json")
    payload = json.dumps({"clusters": [{"name": "only", "description": "d", "members": [0, 1, 2, 3]}]})
    load_or_build_clusters(path, make_corpus(4), stub_client(payload), LLM_PARAMS)

    # A fifth paper changes the fingerprint, so the cache is rebuilt rather
    # than silently leaving the newcomer unassigned.
    rebuilt = load_or_build_clusters(path, make_corpus(5), stub_client(payload), LLM_PARAMS)
    covered = {i for c in rebuilt for i in c.members}
    assert covered == {0, 1, 2, 3, 4}


def test_a_failed_clustering_is_not_cached(tmp_path):
    """One transient API error must not permanently collapse the digest.

    The cache is committed to git and keyed only on the corpus, so a cached
    fallback would survive every later run.
    """
    path = str(tmp_path / "clusters.json")
    corpus = make_corpus()

    def boom(**kw):
        raise RuntimeError("rate limited")

    failing = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
    fallback = load_or_build_clusters(path, corpus, failing, LLM_PARAMS)
    assert len(fallback) == 1  # degraded, as designed

    import os

    assert not os.path.exists(path), "a degraded result must not be cached"

    recovered = load_or_build_clusters(path, corpus, stub_client(VALID_PAYLOAD), LLM_PARAMS)
    assert [c.name for c in recovered] == ["电荷异质性", "宿主细胞蛋白"]


def test_adding_one_paper_does_not_force_a_reclustering(tmp_path):
    """Otherwise the cache never survives: themes and headings drift weekly."""
    path = str(tmp_path / "clusters.json")
    load_or_build_clusters(path, make_corpus(20), stub_client(VALID_PAYLOAD), LLM_PARAMS)

    # Counting rather than raising: the module deliberately swallows
    # exceptions from the client, so a raise here would pass for the wrong reason.
    calls = {"n": 0}

    def counting(**kw):
        calls["n"] += 1
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=VALID_PAYLOAD))])

    cold = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=counting)))
    clusters = load_or_build_clusters(path, make_corpus(21), cold, LLM_PARAMS)
    assert calls["n"] == 0, "one new paper must not trigger a rebuild"
    covered = {i for c in clusters for i in c.members}
    assert covered == set(range(21)), "the newcomer must still be clustered"


def test_a_substantially_changed_corpus_does_force_a_reclustering(tmp_path):
    path = str(tmp_path / "clusters.json")
    load_or_build_clusters(path, make_corpus(20), stub_client(VALID_PAYLOAD), LLM_PARAMS)

    other = json.dumps({"clusters": [{"name": "新主题", "description": "d", "members": [0]}]}, ensure_ascii=False)
    rebuilt = load_or_build_clusters(path, make_corpus(60), stub_client(other), LLM_PARAMS)
    assert rebuilt[0].name == "新主题"


def test_a_large_corpus_is_sampled_rather_than_sent_whole(tmp_path):
    """The whole-corpus prompt is what breaks at scale; cap it loudly."""
    recorded = []

    def create(**kwargs):
        recorded.append(kwargs["messages"][-1]["content"])
        members = list(range(50))
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"clusters": [{"name": "x", "description": "d", "members": members}]})
                    )
                )
            ]
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    clusters = cluster_corpus(make_corpus(400), client, LLM_PARAMS, n_clusters=2, max_titles=50)
    assert "Paper 399" not in recorded[0], "the prompt must be capped"
    covered = {i for c in clusters for i in c.members}
    assert covered == set(range(400)), "every paper must still land somewhere"
