"""Composite scoring and the two gates."""

from omegaconf import OmegaConf

from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.scoring import passing_papers, score_papers
from zotero_arxiv_daily.triage import TriageResult


def make_config(**overrides):
    report = {
        "min_relevance": 55,
        "min_score": 60,
        "journals": {"bonus": 10, "allow": ["mAbs", "Separations"]},
        "industry": {"bonus": 8, "names": ["Amgen"]},
    }
    report.update(overrides)
    return OmegaConf.create({"report": report})


def make_paper(relevance=None, journal=None, institutions=None, companies=None) -> Paper:
    paper = Paper(source="pubmed", title="A paper", authors=[], abstract="a", url="u", journal=journal)
    paper.institutions = institutions or []
    paper.company_institutions = companies or []
    if relevance is not None:
        paper.triage = TriageResult(relevance=relevance, reason="r", modalities=[])
    return paper


def test_rank_score_is_relevance_when_nothing_matches():
    paper = make_paper(relevance=70, journal="Poultry Science")
    score_papers([paper], make_config())
    assert paper.scoring.rank_score == 70
    assert paper.scoring.journal_hit is None
    assert paper.scoring.industry_hit is None


def test_a_listed_journal_adds_its_bonus():
    paper = make_paper(relevance=70, journal="Separations")
    score_papers([paper], make_config())
    assert paper.scoring.rank_score == 80
    assert paper.scoring.journal_hit == "Separations"


def test_a_listed_company_adds_its_bonus():
    paper = make_paper(relevance=70, institutions=["Amgen Inc., Thousand Oaks"])
    score_papers([paper], make_config())
    assert paper.scoring.rank_score == 78
    assert paper.scoring.industry_hit == "Amgen"


def test_both_bonuses_stack():
    paper = make_paper(relevance=70, journal="mAbs", institutions=["Amgen"])
    score_papers([paper], make_config())
    assert paper.scoring.rank_score == 88


def test_an_unjudged_paper_gets_no_breakdown():
    paper = make_paper(relevance=None, journal="mAbs")
    score_papers([paper], make_config())
    assert paper.scoring is None


def test_an_unjudged_paper_never_passes():
    papers = [make_paper(relevance=None, journal="mAbs")]
    score_papers(papers, make_config())
    assert passing_papers(papers, make_config()) == []


def test_a_paper_at_both_thresholds_passes():
    papers = [make_paper(relevance=60, journal="Poultry Science")]
    score_papers(papers, make_config())
    assert len(passing_papers(papers, make_config())) == 1


def test_a_paper_below_the_composite_line_is_dropped():
    papers = [make_paper(relevance=59, journal="Poultry Science")]
    score_papers(papers, make_config())
    assert passing_papers(papers, make_config()) == []


# The reason min_relevance exists at all: without it a 42-point paper —
# squarely in the rubric's "only nouns overlap" band — reaches 60 on
# bonuses alone and lands in the digest.
def test_bonuses_cannot_lift_a_paper_below_the_relevance_floor():
    papers = [make_paper(relevance=42, journal="mAbs", institutions=["Amgen"])]
    score_papers(papers, make_config())
    assert papers[0].scoring.rank_score == 60  # would clear min_score on its own
    assert passing_papers(papers, make_config()) == []


def test_zeroed_thresholds_pass_everything_that_was_judged():
    papers = [make_paper(relevance=5, journal="Poultry Science")]
    config = make_config(min_relevance=0, min_score=0)
    score_papers(papers, config)
    assert len(passing_papers(papers, config)) == 1


def test_survivors_come_back_best_first():
    papers = [make_paper(relevance=60), make_paper(relevance=90), make_paper(relevance=75)]
    config = make_config()
    score_papers(papers, config)
    assert [p.scoring.rank_score for p in passing_papers(papers, config)] == [90, 75, 60]


def test_an_empty_journal_list_costs_no_bonus():
    config = make_config(journals={"bonus": 10, "allow": []})
    paper = make_paper(relevance=70, journal="mAbs")
    score_papers([paper], config)
    assert paper.scoring.rank_score == 70


def test_a_missing_industry_block_is_not_an_error():
    config = OmegaConf.create({"report": {"min_relevance": 55, "min_score": 60}})
    paper = make_paper(relevance=70, journal="mAbs", institutions=["Amgen"])
    score_papers([paper], config)
    assert paper.scoring.rank_score == 70


def test_a_source_flagged_company_earns_the_bonus_without_being_listed():
    paper = make_paper(relevance=70, companies=["Genentech"], institutions=["Genentech"])
    score_papers([paper], make_config())
    assert paper.scoring.industry_hit == "Genentech"
    assert paper.scoring.rank_score == 78
