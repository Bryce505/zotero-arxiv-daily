"""Top up a thin week with established, highly-cited work.

A week that yields fewer than the configured minimum is padded from OpenAlex
by citation count rather than by loosening the relevance bar — the digest
would rather show a known classic than a weak new match.  Backfilled papers
are tagged so the report can list them separately (spec 8.5).
"""

from loguru import logger

from .dedup import dedup_papers, normalize_doi
from .protocol import Paper
from .search.profile import QueryProfile

_OVERSAMPLE = 3


def backfill_papers(
    profiles: list[QueryProfile],
    retriever,
    needed: int,
    exclude_dois: set[str],
    gate=None,
) -> list[Paper]:
    """Return up to *needed* highly-cited papers across *profiles*.

    *gate* filters the oversampled pool before the citation sort.  Sorting by
    citations without it is how a 2005 virology paper reached a CMC digest:
    highly cited is not the same as relevant.
    """
    if needed <= 0 or not profiles:
        return []

    per_cluster = max(1, needed * _OVERSAMPLE // len(profiles))
    pool: list[Paper] = []
    for profile in profiles:
        found = retriever.search_highly_cited(profile.plain_query, per_cluster)
        for paper in found:
            paper.cluster = profile.cluster
            paper.is_backfill = True
        pool.extend(found)

    pool = [p for p in pool if (normalize_doi(p.doi) or "") not in exclude_dois]
    pool = dedup_papers(pool)
    if gate is not None:
        pool = gate(pool)
    pool.sort(key=lambda p: p.cited_by_count or 0, reverse=True)
    chosen = pool[:needed]
    logger.info(f"Backfilled {len(chosen)} highly-cited papers (needed {needed})")
    return chosen
