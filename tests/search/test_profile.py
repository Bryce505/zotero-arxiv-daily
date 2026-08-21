"""Per-cluster query distillation."""

import json
from datetime import datetime
from types import SimpleNamespace

from zotero_arxiv_daily.protocol import CorpusPaper
from zotero_arxiv_daily.search.cluster import ThemeCluster
from zotero_arxiv_daily.search.profile import (
    QueryProfile,
    distill_profile,
    load_or_build_profiles,
)

LLM_PARAMS = {"generation_kwargs": {"model": "stub-model"}}

PAYLOAD = json.dumps(
    {
        "mesh_terms": ["Antibodies, Monoclonal", "Chromatography, Ion Exchange"],
        "free_terms": ["charge variant", "cIEF"],
        "pubmed_query": '("Antibodies, Monoclonal"[MeSH] OR "charge variant"[tiab])',
        "plain_query": "monoclonal antibody charge variant characterisation",
    },
    ensure_ascii=False,
)


def stub_client(payload: str, recorder: list | None = None) -> SimpleNamespace:
    def create(**kwargs):
        if recorder is not None:
            recorder.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def make_corpus() -> list[CorpusPaper]:
    return [
        CorpusPaper(
            title=f"Paper {i}",
            abstract=f"Abstract {i}",
            added_date=datetime(2026, 1, 1),
            paths=["文献"],
        )
        for i in range(3)
    ]


def test_distill_parses_all_four_query_forms():
    cluster = ThemeCluster(name="电荷异质性", description="d", members=[0, 1])
    profile = distill_profile(cluster, make_corpus(), stub_client(PAYLOAD), LLM_PARAMS)
    assert profile.cluster == "电荷异质性"
    assert "Antibodies, Monoclonal" in profile.mesh_terms
    assert "cIEF" in profile.free_terms
    assert "[MeSH]" in profile.pubmed_query
    assert profile.plain_query.startswith("monoclonal antibody")


def test_distill_shows_the_model_the_cluster_titles():
    recorder = []
    cluster = ThemeCluster(name="电荷异质性", description="d", members=[0, 2])
    distill_profile(cluster, make_corpus(), stub_client(PAYLOAD, recorder), LLM_PARAMS)
    prompt = recorder[0]["messages"][-1]["content"]
    assert "Paper 0" in prompt
    assert "Paper 2" in prompt
    assert "Paper 1" not in prompt


def test_distill_falls_back_to_the_cluster_name_on_bad_json():
    cluster = ThemeCluster(name="电荷异质性", description="电荷变异体", members=[0])
    profile = distill_profile(cluster, make_corpus(), stub_client("garbage"), LLM_PARAMS)
    assert profile.cluster == "电荷异质性"
    assert profile.plain_query == "电荷异质性 电荷变异体"
    assert profile.pubmed_query == ""


def test_a_profile_without_a_pubmed_query_still_has_a_plain_one():
    profile = QueryProfile(cluster="x", mesh_terms=[], free_terms=[], pubmed_query="", plain_query="x")
    assert profile.pubmed_query == ""
    assert profile.plain_query


def test_profiles_are_cached_per_cluster_set(tmp_path):
    path = str(tmp_path / "profiles.json")
    clusters = [ThemeCluster(name="a", description="d", members=[0])]
    first = load_or_build_profiles(path, clusters, make_corpus(), stub_client(PAYLOAD), LLM_PARAMS)

    def explode(**kw):
        raise AssertionError("the LLM must not be called when the cache is warm")

    cold = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=explode)))
    second = load_or_build_profiles(path, clusters, make_corpus(), cold, LLM_PARAMS)
    assert second[0].pubmed_query == first[0].pubmed_query


def test_profile_cache_is_rebuilt_when_the_cluster_names_change(tmp_path):
    path = str(tmp_path / "profiles.json")
    load_or_build_profiles(
        path, [ThemeCluster(name="a", description="d", members=[0])], make_corpus(), stub_client(PAYLOAD), LLM_PARAMS
    )
    other = json.dumps({"mesh_terms": [], "free_terms": [], "pubmed_query": "NEW", "plain_query": "new"})
    rebuilt = load_or_build_profiles(
        path, [ThemeCluster(name="b", description="d", members=[0])], make_corpus(), stub_client(other), LLM_PARAMS
    )
    assert rebuilt[0].pubmed_query == "NEW"


def test_one_profile_is_built_per_cluster(tmp_path):
    path = str(tmp_path / "profiles.json")
    clusters = [
        ThemeCluster(name="a", description="d", members=[0]),
        ThemeCluster(name="b", description="d", members=[1]),
    ]
    profiles = load_or_build_profiles(path, clusters, make_corpus(), stub_client(PAYLOAD), LLM_PARAMS)
    assert [p.cluster for p in profiles] == ["a", "b"]


def test_a_failed_distillation_is_not_cached(tmp_path):
    """A cached empty pubmed_query would silently disable PubMed for that theme."""
    import os

    path = str(tmp_path / "profiles.json")
    clusters = [ThemeCluster(name="a", description="d", members=[0])]
    degraded = load_or_build_profiles(path, clusters, make_corpus(), stub_client("garbage"), LLM_PARAMS)
    assert degraded[0].pubmed_query == ""
    assert not os.path.exists(path), "a degraded result must not be cached"

    recovered = load_or_build_profiles(path, clusters, make_corpus(), stub_client(PAYLOAD), LLM_PARAMS)
    assert "[MeSH]" in recovered[0].pubmed_query
