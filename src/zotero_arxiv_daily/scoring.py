"""Turn a triage verdict plus two curated lists into one comparable number.

Two gates, not one.  The composite line (`min_score`) is where the journal
and company bonuses do their work — they are meant to break ties between
papers that already qualify.  The relevance floor (`min_relevance`) is the
line no bonus can cross, because without it a 42-point paper — squarely in
the rubric's "only the nouns overlap" band — reaches 60 on bonuses alone and
lands in the digest.  Bonuses should promote among the qualified, never
promote the unqualified.
"""

from dataclasses import dataclass

from loguru import logger
from omegaconf import DictConfig

from .affiliation import match_industry, match_journal
from .protocol import Paper

_DEFAULT_MIN_RELEVANCE = 55
_DEFAULT_MIN_SCORE = 60


@dataclass
class ScoreBreakdown:
    relevance: int
    journal_hit: str | None
    industry_hit: str | None
    rank_score: int


def _block(config, key: str) -> dict:
    """Read one optional sub-block of ``report`` as a plain dict."""
    report = config.get("report") or {}
    value = report.get(key) or {}
    return {"bonus": int(value.get("bonus") or 0), "names": list(value.get("allow") or value.get("names") or [])}


def score_papers(papers: list[Paper], config: DictConfig) -> None:
    """Fill ``paper.scoring`` for every judged paper, in place."""
    journals = _block(config, "journals")
    industry = _block(config, "industry")
    for paper in papers:
        if paper.triage is None:
            paper.scoring = None
            continue
        journal_hit = match_journal(paper.journal, journals["names"])
        industry_hit = match_industry(paper.institutions, paper.company_institutions, industry["names"])
        paper.scoring = ScoreBreakdown(
            relevance=paper.triage.relevance,
            journal_hit=journal_hit,
            industry_hit=industry_hit,
            rank_score=paper.triage.relevance
            + (journals["bonus"] if journal_hit else 0)
            + (industry["bonus"] if industry_hit else 0),
        )


def passing_papers(papers: list[Paper], config: DictConfig) -> list[Paper]:
    """Return the papers clearing both gates, best first."""
    report = config.get("report") or {}
    min_relevance = int(report.get("min_relevance", _DEFAULT_MIN_RELEVANCE))
    min_score = int(report.get("min_score", _DEFAULT_MIN_SCORE))

    survivors, unjudged, below_floor, below_line = [], 0, 0, 0
    for paper in papers:
        if paper.scoring is None:
            unjudged += 1
        elif paper.scoring.relevance < min_relevance:
            below_floor += 1
        elif paper.scoring.rank_score < min_score:
            below_line += 1
        else:
            survivors.append(paper)

    logger.info(
        f"Relevance gate: {len(survivors)}/{len(papers)} passed "
        f"({unjudged} unjudged, {below_floor} below relevance {min_relevance}, "
        f"{below_line} below score {min_score})"
    )
    return sorted(survivors, key=lambda p: -p.scoring.rank_score)
