"""The optional user-specified focus topic."""

import json
from datetime import date
from types import SimpleNamespace

from omegaconf import OmegaConf

from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.search.focus import (
    build_focus_profile,
    collect_focus_papers,
    focus_settings,
)

LLM_PARAMS = {"generation_kwargs": {"model": "stub-model"}}

PROFILE_PAYLOAD = json.dumps(
    {
        "summary": "连续制造在单抗原液生产中的工艺控制与放行策略。",
        "mesh_terms": ["Antibodies, Monoclonal"],
        "free_terms": ["continuous manufacturing", "perfusion"],
        "pubmed_query": '("continuous manufacturing"[tiab])',
        "plain_query": "continuous manufacturing monoclonal antibody",
    },
    ensure_ascii=False,
)


def stub_client(payloads, recorder: list | None = None) -> SimpleNamespace:
    """Cycles on the last payload; an Exception payload is raised."""
    items = list(payloads)
    calls = {"n": 0}

    def create(**kwargs):
        if recorder is not None:
            recorder.append(kwargs)
        payload = items[min(calls["n"], len(items) - 1)]
        calls["n"] += 1
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def triage_payload(count: int, relevance: int = 80) -> str:
    return json.dumps(
        [{"index": i, "relevance": relevance, "reason": f"理由 {i}"} for i in range(1, count + 1)],
        ensure_ascii=False,
    )


def make_config(**focus):
    settings = {
        "topic": "连续制造",
        "background": None,
        "min_papers": 2,
        "max_papers": 4,
        "min_relevance": 75,
    }
    settings.update(focus)
    return OmegaConf.create(
        {
            "search": {"sources": ["pubmed"], "per_cluster_limit": 25, "focus": settings},
            "report": {"triage_batch": 8, "journals": {"bonus": 10, "allow": []},
                       "industry": {"bonus": 8, "names": []}},
            "llm": LLM_PARAMS,
        }
    )


def make_paper(i: int) -> Paper:
    return Paper(
        source="pubmed",
        title=f"Focus paper {i}",
        authors=["Doe J"],
        abstract=f"Abstract {i}",
        url=f"https://example.org/{i}",
        doi=f"10.1000/f{i}",
        cited_by_count=100 - i,
    )


class StubRetriever:
    def __init__(self, fresh=None, classics=None):
        self.fresh = fresh if fresh is not None else []
        self.classics = classics if classics is not None else []
        self.calls: list[tuple[str, str]] = []
        self.windows: list[tuple[date, date]] = []

    def search(self, query, start, end, limit):
        self.windows.append((start, end))
        self.calls.append(("search", query))
        return list(self.fresh)

    def search_highly_cited(self, query, limit):
        self.calls.append(("highly_cited", query))
        return list(self.classics)


# --------------------------------------------------------------------------- settings


def test_an_empty_topic_disables_the_whole_line():
    assert focus_settings(make_config(topic=None)) is None
    assert focus_settings(make_config(topic="")) is None
    assert focus_settings(make_config(topic="   ")) is None


def test_a_missing_focus_block_is_not_an_error():
    config = OmegaConf.create({"search": {"sources": ["pubmed"]}})
    assert focus_settings(config) is None


def test_settings_carry_the_topic_and_the_limits():
    settings = focus_settings(make_config(background="做工艺开发"))
    assert settings.topic == "连续制造"
    assert settings.background == "做工艺开发"
    assert (settings.min_papers, settings.max_papers, settings.min_relevance) == (2, 4, 75)


# --------------------------------------------------------------------------- profile


def test_the_topic_and_background_reach_the_prompt():
    calls: list = []
    build_focus_profile("连续制造", "做工艺开发", stub_client([PROFILE_PAYLOAD], calls), LLM_PARAMS)
    prompt = str(calls[0]["messages"])
    assert "连续制造" in prompt and "做工艺开发" in prompt


def test_the_profile_carries_every_query_form_and_a_summary():
    profile, summary = build_focus_profile("连续制造", None, stub_client([PROFILE_PAYLOAD]), LLM_PARAMS)
    assert profile.cluster == "连续制造"
    assert profile.free_terms == ["continuous manufacturing", "perfusion"]
    assert profile.plain_query == "continuous manufacturing monoclonal antibody"
    assert summary.startswith("连续制造在单抗")


def test_a_failed_digestion_still_searches_on_the_raw_topic():
    """Losing the summary is cosmetic; losing the search is not."""
    profile, summary = build_focus_profile("连续制造", None, stub_client([RuntimeError("down")]), LLM_PARAMS)
    assert profile.plain_query == "连续制造"
    assert summary == ""


# --------------------------------------------------------------------------- collection


def _collect(config, retriever, client, **kw):
    return collect_focus_papers(
        config, client, lambda source: retriever, date(2026, 8, 21), date(2026, 8, 28), **kw
    )


def test_papers_clearing_the_topic_relevance_bar_are_kept():
    retriever = StubRetriever(fresh=[make_paper(1), make_paper(2)])
    client = stub_client([PROFILE_PAYLOAD, triage_payload(2, relevance=80)])
    result = _collect(make_config(), retriever, client)
    assert result is not None
    assert [p.doi for p in result.papers] == ["10.1000/f1", "10.1000/f2"]
    assert result.topic == "连续制造"
    assert result.summary


def test_papers_below_the_topic_relevance_bar_are_dropped():
    retriever = StubRetriever(fresh=[make_paper(1)])
    client = stub_client([PROFILE_PAYLOAD, triage_payload(1, relevance=30)])
    assert _collect(make_config(), retriever, client) is None


def test_a_thin_topic_week_is_topped_up_with_highly_cited_work():
    retriever = StubRetriever(fresh=[make_paper(1)], classics=[make_paper(9)])
    client = stub_client([PROFILE_PAYLOAD, triage_payload(1), triage_payload(1)])
    result = _collect(make_config(min_papers=2), retriever, client)
    assert [p.doi for p in result.papers] == ["10.1000/f1", "10.1000/f9"]
    assert ("highly_cited", "continuous manufacturing monoclonal antibody") in retriever.calls


def test_a_full_topic_week_does_not_reach_for_classics():
    retriever = StubRetriever(fresh=[make_paper(i) for i in range(1, 4)], classics=[make_paper(9)])
    client = stub_client([PROFILE_PAYLOAD, triage_payload(3)])
    _collect(make_config(min_papers=2), retriever, client)
    assert all(kind != "highly_cited" for kind, _ in retriever.calls)


def test_the_section_is_capped_at_max_papers():
    retriever = StubRetriever(fresh=[make_paper(i) for i in range(1, 8)])
    client = stub_client([PROFILE_PAYLOAD, triage_payload(7)])
    result = _collect(make_config(max_papers=3), retriever, client)
    assert len(result.papers) == 3


def test_already_delivered_papers_are_excluded():
    retriever = StubRetriever(fresh=[make_paper(1), make_paper(2)])
    client = stub_client([PROFILE_PAYLOAD, triage_payload(2)])
    result = _collect(make_config(), retriever, client, exclude_dois={"10.1000/f1"})
    assert [p.doi for p in result.papers] == ["10.1000/f2"]


def test_a_zero_hit_topic_yields_no_section():
    retriever = StubRetriever()
    client = stub_client([PROFILE_PAYLOAD])
    assert _collect(make_config(), retriever, client) is None


def test_focus_papers_carry_their_topic_reason_for_rendering():
    retriever = StubRetriever(fresh=[make_paper(1)])
    client = stub_client([PROFILE_PAYLOAD, triage_payload(1)])
    result = _collect(make_config(), retriever, client)
    assert result.papers[0].triage.reason == "理由 1"
    assert result.papers[0].scoring.relevance == 80


# --------------------------------------------------------------------------- no date limit


def test_the_focus_search_is_not_limited_to_the_digest_week():
    """The operator named a topic, not a week. A topic narrow enough to be
    worth naming usually has nothing published in any given seven days, and
    restricting to the digest window is what left the section empty in run
    33573304939 and filled it with off-topic work in the next one."""
    retriever = StubRetriever(fresh=[make_paper(1)])
    client = stub_client([PROFILE_PAYLOAD, triage_payload(1)])
    _collect(make_config(), retriever, client)

    (window_start, window_end), = retriever.windows
    assert (window_end - window_start).days > 365 * 20, "the topic search must span the literature"


def test_a_thin_result_is_topped_up_with_highly_cited_work():
    retriever = StubRetriever(fresh=[make_paper(1)], classics=[make_paper(9)])
    client = stub_client([PROFILE_PAYLOAD, triage_payload(1), triage_payload(1)])
    result = _collect(make_config(min_papers=2), retriever, client)

    assert [p.doi for p in result.papers] == ["10.1000/f1", "10.1000/f9"]
    assert [kind for kind, _ in retriever.calls] == ["search", "highly_cited"]


def test_a_full_result_does_not_reach_for_classics():
    retriever = StubRetriever(fresh=[make_paper(i) for i in range(1, 4)], classics=[make_paper(9)])
    client = stub_client([PROFILE_PAYLOAD, triage_payload(3)])
    _collect(make_config(min_papers=2), retriever, client)
    assert [kind for kind, _ in retriever.calls] == ["search"]


def test_a_paper_the_search_already_judged_is_not_judged_again_as_a_classic():
    """Re-judging costs a call and can hand the same paper two verdicts in
    one digest."""
    repeat = make_paper(1)
    retriever = StubRetriever(fresh=[repeat], classics=[repeat, make_paper(2)])
    client = stub_client([PROFILE_PAYLOAD, triage_payload(1, relevance=30), triage_payload(1)])
    result = _collect(make_config(min_papers=2), retriever, client)
    assert [p.doi for p in result.papers] == ["10.1000/f2"]
