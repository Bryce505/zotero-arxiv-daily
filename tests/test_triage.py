"""LLM relevance triage: batching, parsing, and degradation."""

import json
from types import SimpleNamespace

from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.triage import TriageResult, triage_papers

LLM_PARAMS = {"generation_kwargs": {"model": "stub-model"}, "language": "中文"}


def make_paper(title: str) -> Paper:
    return Paper(source="pubmed", title=title, authors=[], abstract=f"Abstract of {title}.", url="u")


def stub_client(responses: list, recorder: list | None = None) -> SimpleNamespace:
    """Return a client whose Nth call yields responses[N], cycling on the last."""
    calls = {"n": 0}

    def create(**kwargs):
        if recorder is not None:
            recorder.append(kwargs)
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        payload = responses[index]
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def payload(*rows: dict) -> str:
    return json.dumps(list(rows), ensure_ascii=False)


ROW1 = {"index": 1, "relevance": 88, "reason": "ADC 载药分布表征", "modalities": ["ADC"]}
ROW2 = {"index": 2, "relevance": 12, "reason": "锂电池负极，与生物药无关", "modalities": []}


def test_triage_fills_every_paper_in_the_batch():
    papers = [make_paper("ADC paper"), make_paper("Battery paper")]
    triage_papers(papers, stub_client([payload(ROW1, ROW2)]), LLM_PARAMS)
    assert papers[0].triage == TriageResult(relevance=88, reason="ADC 载药分布表征", modalities=["ADC"])
    assert papers[1].triage.relevance == 12


def test_rows_are_matched_by_index_not_by_order():
    papers = [make_paper("ADC paper"), make_paper("Battery paper")]
    triage_papers(papers, stub_client([payload(ROW2, ROW1)]), LLM_PARAMS)
    assert papers[0].triage.relevance == 88
    assert papers[1].triage.relevance == 12


def test_a_paper_the_model_skipped_stays_unjudged():
    papers = [make_paper("ADC paper"), make_paper("Battery paper")]
    triage_papers(papers, stub_client([payload(ROW1)]), LLM_PARAMS)
    assert papers[0].triage.relevance == 88
    assert papers[1].triage is None


def test_an_out_of_range_index_is_discarded():
    papers = [make_paper("ADC paper")]
    triage_papers(papers, stub_client([payload({"index": 7, "relevance": 90, "reason": "x"})]), LLM_PARAMS)
    assert papers[0].triage is None


def test_relevance_is_clamped_into_range():
    papers = [make_paper("A"), make_paper("B")]
    rows = payload({"index": 1, "relevance": 150, "reason": "r"}, {"index": 2, "relevance": -5, "reason": "r"})
    triage_papers(papers, stub_client([rows]), LLM_PARAMS)
    assert papers[0].triage.relevance == 100
    assert papers[1].triage.relevance == 0


def test_a_row_without_a_usable_relevance_is_discarded():
    papers = [make_paper("A")]
    triage_papers(papers, stub_client([payload({"index": 1, "relevance": "很高", "reason": "r"})]), LLM_PARAMS)
    assert papers[0].triage is None


def test_missing_modalities_becomes_an_empty_list():
    papers = [make_paper("A")]
    triage_papers(papers, stub_client([payload({"index": 1, "relevance": 70, "reason": "r"})]), LLM_PARAMS)
    assert papers[0].triage.modalities == []


def test_a_malformed_batch_is_retried_once():
    papers = [make_paper("A")]
    client = stub_client(["not json at all", payload({"index": 1, "relevance": 70, "reason": "r"})])
    triage_papers(papers, client, LLM_PARAMS)
    assert papers[0].triage.relevance == 70


def test_a_batch_that_keeps_failing_degrades_to_one_call_per_paper():
    papers = [make_paper("A"), make_paper("B")]
    calls: list = []
    # Both batch attempts blow up; the per-paper retries then succeed.
    client = stub_client(
        [RuntimeError("boom"), RuntimeError("boom"), payload({"index": 1, "relevance": 61, "reason": "r"})],
        recorder=calls,
    )
    triage_papers(papers, client, LLM_PARAMS)
    assert papers[0].triage.relevance == 61
    assert papers[1].triage.relevance == 61
    assert len(calls) == 4  # two batch attempts, then one call per paper


def test_a_total_llm_outage_leaves_papers_unjudged_without_raising():
    papers = [make_paper("A"), make_paper("B")]
    triage_papers(papers, stub_client([RuntimeError("down")]), LLM_PARAMS)
    assert [p.triage for p in papers] == [None, None]


def test_papers_are_sent_in_batches_of_the_configured_size():
    papers = [make_paper(f"P{i}") for i in range(10)]
    calls: list = []
    triage_papers(papers, stub_client([payload()], recorder=calls), LLM_PARAMS, batch_size=4)
    assert len(calls) == 3  # 4 + 4 + 2


def test_an_empty_paper_list_makes_no_calls():
    calls: list = []
    triage_papers([], stub_client([payload()], recorder=calls), LLM_PARAMS)
    assert calls == []


def test_the_prompt_carries_the_counter_examples_that_slipped_through():
    # These five are the actual off-topic papers the first digest shipped.
    # Softening the rubric until they pass again should break a test.
    calls: list = []
    triage_papers([make_paper("A")], stub_client([payload()], recorder=calls), LLM_PARAMS)
    prompt = str(calls[0]["messages"])
    for counter_example in ("电池", "等离子体", "小分子", "兽医", "临床诊断"):
        assert counter_example in prompt


def test_the_prompt_carries_the_paper_title_and_abstract():
    calls: list = []
    triage_papers([make_paper("Cetuximab ADC")], stub_client([payload()], recorder=calls), LLM_PARAMS)
    prompt = str(calls[0]["messages"])
    assert "Cetuximab ADC" in prompt
    assert "Abstract of Cetuximab ADC." in prompt
