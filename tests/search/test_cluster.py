"""LLM corpus clustering, fingerprint caching, and candidate assignment."""

import json
from datetime import datetime
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
            added_date=datetime(2026, 1, i + 1),
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
