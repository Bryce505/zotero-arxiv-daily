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


# --------------------------------------------------------------------------- theme-fit verdicts
#
# Relevance answers "is this CMC-adjacent at all"; these cover the stricter,
# separate question of whether a paper fits one of the library's actual
# themes -- the check that catches a paper which clears the relevance bar
# but was forced under a theme its embedding merely scored highest on.

THEMES = {"HCP": "宿主细胞蛋白检测", "电荷": "电荷变异体分析"}


def test_theme_names_and_descriptions_appear_in_the_prompt_when_given():
    calls: list = []
    triage_papers([make_paper("A")], stub_client([payload()], recorder=calls), LLM_PARAMS, themes=THEMES)
    prompt = str(calls[0]["messages"])
    assert "HCP" in prompt and "宿主细胞蛋白检测" in prompt
    assert "电荷" in prompt and "电荷变异体分析" in prompt


def test_without_themes_the_prompt_asks_for_no_cluster_field():
    calls: list = []
    triage_papers([make_paper("A")], stub_client([payload()], recorder=calls), LLM_PARAMS)
    prompt = str(calls[0]["messages"])
    assert '"cluster"' not in prompt


def test_an_empty_themes_dict_behaves_like_no_themes():
    calls: list = []
    triage_papers([make_paper("A")], stub_client([payload()], recorder=calls), LLM_PARAMS, themes={})
    prompt = str(calls[0]["messages"])
    assert '"cluster"' not in prompt


def test_a_row_naming_a_real_theme_overrides_the_papers_cluster():
    paper = make_paper("A")
    paper.cluster = "电荷"  # the provisional, embedding-based assignment
    row = {"index": 1, "relevance": 88, "reason": "r", "modalities": [], "cluster": "HCP"}
    triage_papers([paper], stub_client([payload(row)]), LLM_PARAMS, themes=THEMES)
    assert paper.triage.relevance == 88
    assert paper.cluster == "HCP"


def test_a_row_saying_no_theme_fits_rejects_the_paper_despite_high_relevance():
    paper = make_paper("A")
    paper.cluster = "HCP"
    row = {"index": 1, "relevance": 88, "reason": "写得很好但跑题了", "modalities": [], "cluster": "无"}
    triage_papers([paper], stub_client([payload(row)]), LLM_PARAMS, themes=THEMES)
    assert paper.triage is None


def test_an_unrecognized_cluster_value_leaves_the_assignment_untouched():
    """A name the model invented must not silently relabel the paper."""
    paper = make_paper("A")
    paper.cluster = "HCP"
    row = {"index": 1, "relevance": 70, "reason": "r", "modalities": [], "cluster": "一个模型编的主题名"}
    triage_papers([paper], stub_client([payload(row)]), LLM_PARAMS, themes=THEMES)
    assert paper.triage.relevance == 70
    assert paper.triage.cluster is None
    assert paper.cluster == "HCP"


def test_a_missing_cluster_field_leaves_the_assignment_untouched():
    paper = make_paper("A")
    paper.cluster = "HCP"
    row = {"index": 1, "relevance": 70, "reason": "r"}
    triage_papers([paper], stub_client([payload(row)]), LLM_PARAMS, themes=THEMES)
    assert paper.cluster == "HCP"


def test_theme_verdicts_are_matched_by_index_not_by_order():
    fits = make_paper("fits HCP")
    fits.cluster = "电荷"
    rejected = make_paper("fits nothing")
    rejected.cluster = "HCP"
    row1 = {"index": 1, "relevance": 80, "reason": "r", "cluster": "HCP"}
    row2 = {"index": 2, "relevance": 80, "reason": "r", "cluster": "无"}
    # Rows arrive out of order; index, not position, must decide who gets what.
    triage_papers([fits, rejected], stub_client([payload(row2, row1)]), LLM_PARAMS, themes=THEMES)
    assert fits.cluster == "HCP"
    assert rejected.triage is None


# --------------------------------------------------------------------------- loosened theme fit


def test_loose_mode_keeps_a_paper_the_model_says_fits_no_theme():
    """Backfill's bar. Run 33517443909 rejected 13/13 highly-cited backfill
    candidates on theme fit alone (0 on relevance, 0 on score): "does this
    belong to one of this week's five narrow themes" is a stricter and
    different question from "is this worth reading for this library"."""
    paper = make_paper("A")
    paper.cluster = "HCP"
    row = {"index": 1, "relevance": 88, "reason": "经典综述", "modalities": [], "cluster": "无"}
    triage_papers([paper], stub_client([payload(row)]), LLM_PARAMS, themes=THEMES, require_theme_fit=False)
    assert paper.triage is not None
    assert paper.triage.relevance == 88
    assert paper.cluster == "HCP"  # the provisional assignment still decides where it shows


def test_loose_mode_still_takes_a_confident_theme_correction():
    """Loosening only removes the rejection; a real verdict is still better
    routing than embedding similarity and is still applied."""
    paper = make_paper("A")
    paper.cluster = "电荷"
    row = {"index": 1, "relevance": 70, "reason": "r", "modalities": [], "cluster": "HCP"}
    triage_papers([paper], stub_client([payload(row)]), LLM_PARAMS, themes=THEMES, require_theme_fit=False)
    assert paper.cluster == "HCP"


def test_loose_mode_does_not_rescue_a_paper_the_model_never_judged():
    """Unjudged is not the same as "judged, fits nothing" — an LLM outage
    must not become a free pass, in either mode."""
    papers = [make_paper("A")]
    triage_papers(papers, stub_client([RuntimeError("down")]), LLM_PARAMS, themes=THEMES,
                  require_theme_fit=False)
    assert papers[0].triage is None


def test_theme_fit_is_required_by_default():
    paper = make_paper("A")
    paper.cluster = "HCP"
    row = {"index": 1, "relevance": 88, "reason": "r", "modalities": [], "cluster": "无"}
    triage_papers([paper], stub_client([payload(row)]), LLM_PARAMS, themes=THEMES)
    assert paper.triage is None


# --------------------------------------------------------------------------- topic rubric


def test_the_topic_rubric_refuses_a_paper_that_is_not_about_the_named_subject():
    """The 2026-08-W4 focus section shipped four papers whose own reasons said
    "but not ulinastatin" — xanthine oxidase, tyrosinase, sulfatase and
    cholinesterase inhibition kinetics, all scored 60-65 under an "adjacent
    problem" band. Naming a subject has to mean the paper is about that
    subject, however close the methodology is."""
    from zotero_arxiv_daily.triage import triage_for_topic

    calls: list = []
    triage_for_topic(
        [make_paper("A")], stub_client([payload()], recorder=calls), LLM_PARAMS, "乌司他丁：酶抑制活性动力学研究"
    )
    prompt = str(calls[0]["messages"])
    assert "乌司他丁：酶抑制活性动力学研究" in prompt
    # The rule, and the shape of failure that forced it.
    assert "研究对象" in prompt
    for lost_case in ("黄嘌呤氧化酶", "酪氨酸酶", "胆碱酯酶"):
        assert lost_case in prompt, "the rubric must name the cases that actually slipped through"


def test_the_topic_rubric_still_carries_the_optional_background():
    from zotero_arxiv_daily.triage import triage_for_topic

    calls: list = []
    triage_for_topic([make_paper("A")], stub_client([payload()], recorder=calls), LLM_PARAMS,
                     "乌司他丁", "临床用于急性胰腺炎")
    assert "临床用于急性胰腺炎" in str(calls[0]["messages"])
